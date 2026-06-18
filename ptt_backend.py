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
import math
import jieba
import random
import google.generativeai as genai
import os
import traceback
import concurrent.futures # 🚀 導入多執行緒模組
import time # 🚀 導入時間模組，用於重試機制

app = Flask(__name__)
CORS(app)

# ==========================================
# 🔑 設定 GEMINI API KEY (請填寫)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# ==========================================

# --- 資料庫智慧切換（本機用 SQLite，雲端用 PostgreSQL） ---
DATABASE_URL = os.environ.get('DATABASE_URL')

if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
    print("🌐 偵測到雲端生產環境：已成功連接雲端 PostgreSQL 資料庫！")
else:
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
TARGET_BOARDS = [
    'Gossiping', 'Stock', 'MobileComm', 'C_Chat', 'Baseball', 
    'NBA', 'Tech_Job', 'Car', 'Lifeismoney'
]
STOP_WORDS = {
    '的', '是', '在', '我', '你', '他', '我們', '你們', '他們',
    '問卦', '公告', '新聞', '情報', '問題', '討論', '分享', '心得', '請益', '閒聊', 're', '發錢', '爆卦', '協尋',
    '有沒有', '怎麼', '什麼', '為什麼', '如果', '可以', '覺得', '不會', '一樣', '知道',
    '這', '那', '就', '了', '也', '不', '嗎', '啊', '呢', '吧', '都', '還', '又', '跟', '被', '讓', '把', '與', '及',
    '一個', '現在', '今天', '台灣', '真的', '大家', '還是', '只是', '所以', '因為', '但是', '花邊',
    '集中', '置底', '盤後', '盤後閒', '一般', '整理', '贈送', '申訴', '集點', '代碼', 'schedule', 'fw', 'vs', '標題', '系列', '連結', '相關', '資訊', '品牌', '公開', '全台', '查詢'
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
        res = session.get(url, headers=HEADERS, timeout=3, verify=False)
        if res.status_code != 200: return ""
        soup = BeautifulSoup(res.text, 'html.parser')
        main_content = soup.find(id="main-content")
        if not main_content: return ""

        pushes = main_content.find_all('div', class_='push')
        push_text = " ".join([p.text.strip().replace('\n', ' ') for p in pushes[:15]])

        for tag in main_content.find_all(['div', 'span'], class_=['article-metaline', 'article-metaline-right', 'push', 'f2']): 
            tag.extract()
        
        article_text = main_content.get_text().strip()[:100].replace('\n', ' ')
        return f"【發文者】：{article_text} | 【留言區】：{push_text}"
    except: return ""

def search_board_keyword(session, board, keyword):
    search_url = f"https://www.ptt.cc/bbs/{board}/search?q={keyword}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    cookies = {'over18': '1'}
    articles = [] 
    
    try:
        response = session.get(search_url, headers=headers, cookies=cookies, timeout=10)
        if response.status_code != 200: return []
            
        soup = BeautifulSoup(response.text, 'html.parser')
        for r_ent in soup.find_all('div', class_='r-ent'):
            t_tag = r_ent.select_one('.title a')
            p_tag = r_ent.select_one('.nrec span')
            if t_tag and t_tag.get('href'):
                s_str = p_tag.text.strip() if p_tag else '0'
                sc = 100 if s_str == '爆' else (int(s_str) if s_str.isdigit() else 0)
                articles.append(Article(board, t_tag.text.strip(), "https://www.ptt.cc"+t_tag['href'], sc, 'normal'))
    except Exception: 
        pass
    return articles

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/hot_topics', methods=['GET'])
def get_hot_topics():
    global CACHE_DATA
    now = datetime.now()
    if CACHE_DATA['timestamp'] and (now - CACHE_DATA['timestamp']).total_seconds() < CACHE_DURATION: 
        return jsonify(CACHE_DATA['payload'])
        
    try:
        session = get_robust_session()
        all_articles = []
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=9) as executor:
            future_to_board = {
                executor.submit(session.get, f"https://www.ptt.cc/bbs/{board}/index.html", headers=HEADERS, timeout=5, verify=False): board 
                for board in TARGET_BOARDS
            }
            
            for future in concurrent.futures.as_completed(future_to_board):
                board = future_to_board[future]
                try:
                    response = future.result()
                    if response.status_code == 200:
                        soup = BeautifulSoup(response.text, 'html.parser')
                        for r_ent in soup.find_all('div', class_='r-ent'):
                            t_tag = r_ent.select_one('.title a')
                            p_tag = r_ent.select_one('.nrec span')
                            if t_tag and not t_tag.text.strip().startswith('[公告]'):
                                s_str = p_tag.text.strip() if p_tag else '0'
                                sc = 100 if s_str == '爆' else (int(s_str) if s_str.isdigit() else 0)
                                if sc >= 5: 
                                    all_articles.append(Article(board, t_tag.text.strip(), "https://www.ptt.cc"+t_tag['href'], sc, 'hot'))
                except Exception:
                    continue

        all_articles.sort(key=lambda x: x.score, reverse=True)
        MAX_PER_BOARD = 5  
        board_counts = {board: 0 for board in TARGET_BOARDS}
        balanced_top = []
        
        for article in all_articles:
            if board_counts[article.board] < MAX_PER_BOARD:
                balanced_top.append(article)
                board_counts[article.board] += 1
            if len(balanced_top) >= 20: break
                
        if not balanced_top: return jsonify({'error': '無法取得文章'}), 500
            
        words = jieba.cut("".join([a.title for a in balanced_top]))
        filtered_words = [w.strip() for w in words if len(w.strip()) > 1 and w.strip().lower() not in STOP_WORDS and not w.strip().isdigit()]
        counts = Counter(filtered_words)
        
        payload = {
            'articles': [a.to_dict() for a in balanced_top], 
            'keywords': [[k, int(15 + math.log(v)*10)] for k, v in counts.most_common(50)]
        }
        CACHE_DATA = {'timestamp': now, 'payload': payload}
        return jsonify(payload)
        
    except Exception as e: 
        return jsonify({'error': str(e)}), 500

@app.route('/api/smart_subscribe', methods=['POST'])
def smart_subscribe():
    try:
        data = request.get_json(silent=True) or request.form
        keyword = data.get('keyword') or data.get('keywords') or data.get('search_text') or ''
        keyword = keyword.strip()
        user_id = data.get('user_id', 'anonymous')

        if not keyword: return jsonify({'error': '請輸入關鍵字'}), 400

        if user_id != 'anonymous':
            db.session.add(SearchHistory(user_id=user_id, keyword=keyword))
            db.session.commit()

        session = get_robust_session()
        all_results = []
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=9) as executor:
            future_to_search = {executor.submit(search_board_keyword, session, board, keyword): board for board in TARGET_BOARDS}
            for future in concurrent.futures.as_completed(future_to_search):
                all_results.extend(future.result())
            
        if not all_results: return jsonify({'message': '找不到相關討論'})

        all_results.sort(key=lambda x: x.score, reverse=True)
        top_articles = all_results[:5] 
        
        articles_text_dict = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_to_url = {executor.submit(scrape_article_content, session, a.url): i for i, a in enumerate(top_articles)}
            for future in concurrent.futures.as_completed(future_to_url):
                i = future_to_url[future]
                articles_text_dict[i] = future.result()

        articles_text = ""
        for i, a in enumerate(top_articles):
            summary = articles_text_dict.get(i, "")
            articles_text += f"ID: {i}\n標題: {a.title}\n摘要: {summary}\n\n"

        prompt = f"""
        你是一個精通 PTT 文化的輿情分析師。使用者搜尋了關鍵字：「{keyword}」。
        請閱讀以下 PTT 5篇文章摘要，我們的情感溫度量表 (0-100) 對應了五個 PTT 專屬階級：
        
        - 80~100分: 「夯」 (全網爆紅，極度狂熱/支持)
        - 60~79分: 「頂級」 (討論熱烈，高度肯定/看好)
        - 40~59分: 「人上人」 (客觀情報，熱度穩定/中立)
        - 20~39分: 「NPC」 (邊緣議題，微弱負面/無感)
        - 0~19分: 「拉玩了」 (被噓爆、徹底翻車、極度負面)

        請給出精確的 0-100 之間整數溫度，並嚴格遵守以下 JSON 結構輸出（不要包含 Markdown 等標記，內部文字請避免使用雙引號以免破壞 JSON 格式）：
        {{
          "macro_score": 數字,
          "macro_summary": "用一句話總結目前整體的鄉民共識與風向",
          "articles": [
            {{
              "id": 文章ID,
              "author_temp": 數字,
              "comment_temp": 數字,
              "tier_badge": "夯 或 頂級 或 人上人 或 NPC 或 拉玩了",
              "reason": "綜合評估發文與留言，濃縮核心論點與衝突點",
              "is_sarcasm": 布林值
            }}
          ]
        }}
        
        文章列表:
        {articles_text}
        """
        
        ai_data = {}
        try:
            generation_config = genai.types.GenerationConfig(temperature=0.2)
            
            # 🛡️ 解除 Gemini 的道德與安全審查，允許分析 PTT 毒性文本
            safety_settings = {
                "HARM_CATEGORY_HARASSMENT": "BLOCK_NONE",
                "HARM_CATEGORY_HATE_SPEECH": "BLOCK_NONE",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT": "BLOCK_NONE",
                "HARM_CATEGORY_DANGEROUS_CONTENT": "BLOCK_NONE"
            }
            
            response = None
            raw_text = ""
            
            for attempt in range(3):
                try:
                    # 加入 safety_settings 強制不擋 PTT 髒話
                    response = model.generate_content(
                        prompt, 
                        generation_config=generation_config,
                        safety_settings=safety_settings
                    )
                    
                    # 如果被安全機制攔截，response.text 會拋出 ValueError
                    raw_text = response.text.strip()
                    break
                except Exception as api_e:
                    print(f"⚠️ Gemini API 發生錯誤或安全攔截 (嘗試 {attempt+1}/3): {api_e}", flush=True)
                    if attempt == 2:
                        raise api_e
                    time.sleep(1.5)

            # 多重過濾 Markdown 格式 (已修復之前截斷的問題)
            if raw_text.startswith("```"):
                raw_text = re.sub(r'^