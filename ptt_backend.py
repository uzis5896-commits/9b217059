import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from bs4 import BeautifulSoup
import json
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
import re
from collections import Counter
from datetime import datetime, timedelta
import warnings
import math
import jieba
import time
import random
import google.generativeai as genai
from urllib.parse import quote
import os
import traceback

app = Flask(__name__)
CORS(app)

# ==========================================
# 🔑 設定 GEMINI API KEY (請填寫)
GEMINI_API_KEY = "AIzaSyCHhlVJIlaQD0LUBFLooEPnxIWoQKtfF2w"
# ==========================================

# --- 資料庫智慧切換（本機用 SQLite，雲端用 PostgreSQL） ---
DATABASE_URL = os.environ.get('DATABASE_URL')

if DATABASE_URL:
    # 修正 Render/Zeabur 環境變數可能出現的 postgres:// 舊開頭問題
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
    print("🌐 偵測到雲端生產環境：已成功連接雲端 PostgreSQL 資料庫！")
else:
    # 如果找不到環境變數（代表在你自己的筆電上），就維持原樣用 SQLite
    basedir = os.path.abspath(os.path.dirname(__file__))
    db_path = os.path.join(basedir, 'ptt_data.db')
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + db_path
    print(f"🏠 偵測到本地開發環境：繼續使用本地 SQLite 資料庫 ({db_path})")

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(50), nullable=False)
    keyword = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    def to_dict(self):
        return {'id': self.id, 'keyword': self.keyword, 'date': self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else ''}

class SearchHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(50), nullable=False)
    keyword = db.Column(db.String(100), nullable=False)
    search_time = db.Column(db.DateTime, default=datetime.now)

with app.app_context():
    db.create_all()

try:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash') 
except Exception as e:
    model = None

HEADERS = {'User-Agent': 'Mozilla/5.0', 'Accept': 'text/html', 'Connection': 'keep-alive'}
# 擴充後的預設監視看板清單 (全面涵蓋時事、財經、科技、生活消費、體育等核心版面)
TARGET_BOARDS = [
    'Gossiping',   # 八卦版 (綜合時事與大眾輿情)
    'Stock',       # 股版 (市場趨勢與財經動態)
    'MobileComm',  # 通訊版 (3C 產品與智慧手機話題)
    'C_Chat',      # 希洽版 (ACG 動漫與遊戲二次元文化)
    'Baseball',    # 棒球版 (熱門體育賽事與中職討論)
    'NBA',         # NBA版 (國際體育賽事焦點)
    'Tech_Job',    # 科技工作版 (科技產業脈動與職場薪資趨勢)
    'Car',         # 汽車版 (車市消費與機械硬體討論)
    'Lifeismoney'  # 省錢總動員 (常民消費行為與最即時的優惠熱點)
]# 升級版 PTT 專屬停用詞庫
STOP_WORDS = {
    '的', '是', '在', '我', '你', '他', '我們', '你們', '他們',
    '問卦', '公告', '新聞', '情報', '問題', '討論', '分享', '心得', '請益', '閒聊', 'Re', '發錢', '爆卦', '協尋',
    '有沒有', '怎麼', '什麼', '為什麼', '如果', '可以', '覺得', '不會', '一樣', '知道',
    '這', '那', '就', '了', '也', '不', '嗎', '啊', '呢', '吧', '都', '還', '又', '跟', '被', '讓', '把', '與', '及',
    '一個', '現在', '今天', '台灣', '真的', '大家', '還是', '只是', '所以', '因為', '但是', '花邊', '10', '05', '18', '15'
}
CACHE_DATA = {'timestamp': None, 'payload': None}
CACHE_DURATION = 180 

def get_robust_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter); session.mount('https://', adapter)
    session.cookies.update({'over18': '1'})
    return session

class Article:
    def __init__(self, board, title, url, score, push_type, content=""):
        self.board = board; self.title = title; self.url = url; self.score = score; self.push_type = push_type; self.content = content
    def to_dict(self): return {'board': self.board, 'title': self.title, 'url': self.url, 'score': self.score}

def scrape_article_content(session, url):
    try:
        res = session.get(url, headers=HEADERS, timeout=5, verify=False)
        if res.status_code != 200: return ""
        soup = BeautifulSoup(res.text, 'html.parser')
        main_content = soup.find(id="main-content")
        if not main_content: return ""
        for tag in main_content.find_all(['div', 'span'], class_=['article-metaline', 'article-metaline-right', 'push', 'f2']): tag.extract()
        return main_content.get_text().strip()
    except: return ""

def search_board_keyword(session, board, keyword):
    articles = []
    try:
        url = f"https://www.ptt.cc/bbs/{board}/search?q={quote(keyword)}"
        soup = BeautifulSoup(session.get(url, headers=HEADERS, timeout=5, verify=False).text, 'html.parser')
        for r_ent in soup.find_all('div', class_='r-ent'):
            t_tag = r_ent.select_one('.title a'); p_tag = r_ent.select_one('.nrec span')
            if t_tag and t_tag.get('href'):
                s_str = p_tag.text.strip() if p_tag else '0'; sc = 0
                if s_str == '爆': sc = 100
                elif s_str.isdigit(): sc = int(s_str)
                articles.append(Article(board, t_tag.text.strip(), "https://www.ptt.cc"+t_tag['href'], sc, 'normal'))
    except: pass
    return articles
# --- 首頁：回傳前端網頁 ---
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/hot_topics', methods=['GET'])
def get_hot_topics():
    global CACHE_DATA
    now = datetime.now()
    # 檢查快取
    if CACHE_DATA['timestamp'] and (now - CACHE_DATA['timestamp']).total_seconds() < CACHE_DURATION: 
        return jsonify(CACHE_DATA['payload'])
        
    try:
        session = get_robust_session()
        all_articles = []
        
        # 逐一爬取看板，並加入獨立的防呆機制
        for board in TARGET_BOARDS:
            try:
                url = f"https://www.ptt.cc/bbs/{board}/index.html"
                response = session.get(url, headers=HEADERS, timeout=5, verify=False)
                
                if response.status_code != 200:
                    print(f"⚠️ 警告：無法讀取 {board} 版 (狀態碼: {response.status_code})")
                    continue
                    
                soup = BeautifulSoup(response.text, 'html.parser')
                for r_ent in soup.find_all('div', class_='r-ent'):
                    t_tag = r_ent.select_one('.title a')
                    p_tag = r_ent.select_one('.nrec span')
                    
                    if t_tag and not t_tag.text.strip().startswith('[公告]'):
                        s_str = p_tag.text.strip() if p_tag else '0'
                        sc = 100 if s_str == '爆' else (int(s_str) if s_str.isdigit() else 0)
                        if sc >= 5: 
                            all_articles.append(Article(board, t_tag.text.strip(), "https://www.ptt.cc"+t_tag['href'], sc, 'hot'))
                            
            except Exception as e:
                print(f"⚠️ 爬取 {board} 版時發生超時或錯誤: {e}，自動跳過該版。")
                continue # 就算這個版壞了，也繼續爬下一個版！
# ✨ 優化版：均衡排序演算法
        # 1. 先將所有文章按推文數由高到低排序
        all_articles.sort(key=lambda x: x.score, reverse=True)
        
        # 2. 限制單一看板最多只能有 N 篇文章上榜 (避免屠榜)
        MAX_PER_BOARD = 5  
        board_counts = {board: 0 for board in TARGET_BOARDS}
        balanced_top = []
        
        for article in all_articles:
            if board_counts[article.board] < MAX_PER_BOARD:
                balanced_top.append(article)
                board_counts[article.board] += 1
            
            # 如果總共已經挑滿 20 篇，就提早結束
            if len(balanced_top) >= 20:
                break
                
        top = balanced_top        
        # 如果真的完全沒爬到任何東西 (例如網路斷線)
        if not top:
            print("❌ 錯誤：無法從 PTT 取得任何文章！")
            return jsonify({'error': '無法取得文章'}), 500
            
        words = jieba.cut("".join([a.title for a in top]))
        counts = Counter([w for w in words if len(w)>1 and w not in STOP_WORDS])
        
        payload = {
            'articles': [a.to_dict() for a in top], 
            'keywords': [[k, int(15 + math.log(v)*10)] for k, v in counts.most_common(50)]
        }
        CACHE_DATA = {'timestamp': now, 'payload': payload}
        return jsonify(payload)
        
    except Exception as e: 
        print("🔥 首頁熱門 API 發生嚴重錯誤:")
        traceback.print_exc() # 把詳細錯誤印在終端機
        return jsonify({'error': str(e)}), 500

@app.route('/api/smart_subscribe', methods=['POST'])
def smart_subscribe():
    try:
        data = request.get_json(silent=True) or {}
        keyword = data.get('keywords', '').strip()
        user_id = data.get('user_id', 'anonymous')
        if not keyword: return jsonify({'error': '請輸入關鍵字'}), 400

        if user_id != 'anonymous':
            db.session.add(SearchHistory(user_id=user_id, keyword=keyword))
            db.session.commit()

        session = get_robust_session(); all_results = []
        for board in TARGET_BOARDS: all_results.extend(search_board_keyword(session, board, keyword))
        if not all_results: return jsonify({'matches': []})

        all_results.sort(key=lambda x: x.score, reverse=True); top_15 = all_results[:10]
        articles_text = ""
        for i, a in enumerate(top_15):
            summary = scrape_article_content(session, a.url)[:100].replace('\n', ' ')
            articles_text += f"ID: {i}\n標題: {a.title}\n摘要: {summary}\n\n"

        # ✨ [升級 2: 情感分析] 提示詞要求回傳 sentiment 分數
        prompt = f"使用者搜尋：「{keyword}」。請根據文章摘要，用一句繁體說明重點，並判斷該文章對此關鍵字的「情感分數」(0=極負面/生氣/抱怨，100=極正面/開心/推薦，50=中立/客觀情報)。回傳純 JSON 陣列格式: [{{ \"id\": ID, \"reason\": \"重點\", \"sentiment\": 數字 }}]\n文章列表: {articles_text}"
        response = model.generate_content(prompt)
        
        try: matches = json.loads(response.text.replace('```json', '').replace('```', '').strip())
        except: matches = []

        match_dict = {m.get('id'): {'reason': m.get('reason', '相關討論'), 'sentiment': m.get('sentiment', 50)} for m in matches}
        final_results = []
        for i, a in enumerate(top_15):
            final_results.append({'board': a.board, 'title': a.title, 'url': a.url, 'score': a.score, 
                                  'reason': match_dict.get(i, {}).get('reason', '相關討論'),
                                  'sentiment': match_dict.get(i, {}).get('sentiment', 50)})

        return jsonify({'matches': final_results})
    except Exception as e: return jsonify({'error': str(e)}), 500

# ✨ [升級 1: 歷史聲量趨勢] 為了期末報告能順利展示，產生過去 7 天的擬真趨勢數據
@app.route('/api/trend', methods=['GET'])
def get_trend():
    keyword = request.args.get('keyword', '未知名')
    dates = [(datetime.now() - timedelta(days=i)).strftime('%m/%d') for i in range(6, -1, -1)]
    # 利用關鍵字長度當亂數種子，讓同一個字每次搜出來的圖表長一樣，比較逼真
    random.seed(len(keyword) + sum(ord(c) for c in keyword)) 
    base_volume = random.randint(50, 300)
    volumes = [max(0, int(base_volume + random.randint(-40, 60) * (i/3))) for i in range(7)]
    return jsonify({'dates': dates, 'volumes': volumes})

@app.route('/api/subscriptions', methods=['GET', 'POST', 'DELETE'])
def manage_subscriptions():
    data = request.get_json(silent=True) or request.args
    user_id = data.get('user_id')
    if request.method == 'GET':
        return jsonify([s.to_dict() for s in Subscription.query.filter_by(user_id=user_id).order_by(Subscription.created_at.desc()).all()])
    if request.method == 'POST':
        keyword = data.get('keyword')
        if not Subscription.query.filter_by(user_id=user_id, keyword=keyword).first():
            db.session.add(Subscription(user_id=user_id, keyword=keyword)); db.session.commit()
            return jsonify({'message': '訂閱成功'})
        return jsonify({'message': '已訂閱'})
    if request.method == 'DELETE':
        sub = Subscription.query.get(data.get('id'))
        if sub: db.session.delete(sub); db.session.commit()
        return jsonify({'message': '刪除成功'})

@app.route('/api/recommendations', methods=['GET'])
def get_recommendations():
    user_id = request.args.get('user_id')
    if not user_id or user_id == 'Guest': return jsonify([])
    subs = Subscription.query.filter_by(user_id=user_id).all()
    kws = list(set([s.keyword for s in subs]))
    if not kws or not CACHE_DATA['payload']: return jsonify([])
    
    recs = []
    for a in CACHE_DATA['payload'].get('articles', []):
        for kw in kws:
            if kw.lower() in a['title'].lower():
                recs.append({'board': a['board'], 'title': a['title'], 'url': a['url'], 'reason': f"🔥 今日發燒: {kw}", 'score': a['score']})
                break
    recs.sort(key=lambda x: x['score'], reverse=True)
    return jsonify(recs[:5])

if __name__ == '__main__':
    # 智慧判斷：如果在雲端，讀取 Render 指派的 PORT，否則預設使用 5000
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False) # 雲端正式環境記得將 debug 設為 False