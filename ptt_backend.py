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
import concurrent.futures # 🚀 導入多執行緒庫

app = Flask(__name__)
CORS(app)

# ==========================================
# 🔑 設定 GEMINI API KEY (請填寫)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# ==========================================

# --- 資料庫智慧切換 ---
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
    # 🚀 將連接池 (pool_connections) 加大，因為我們要用多執行緒同時發送請求
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=retry)
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

        pushes = main_content.find_all('div', class_='push')
        push_text = " ".join([p.text.strip().replace('\n', ' ') for p in pushes[:15]])

        for tag in main_content.find_all(['div', 'span'], class_=['article-metaline', 'article-metaline-right', 'push', 'f2']): 
            tag.extract()
        
        article_text = main_content.get_text().strip()[:100].replace('\n', ' ')
        return f"【發文者】：{article_text} | 【留言區】：{push_text}"
    except: return ""

def search_board_keyword(session, board, keyword):
    search_url = f"https://www.ptt.cc/bbs/{board}/search?q={keyword}"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'}
    cookies = {'over18': '1'}
    articles = [] 
    
    try:
        response = session.get(search_url, headers=headers, cookies=cookies, timeout=10)
        print(f"🔍 正在搜尋 {board} 板 | 關鍵字: {keyword} | 狀態碼: {response.status_code}", flush=True)
        
        if response.status_code != 200:
            return []
            
        soup = BeautifulSoup(response.text, 'html.parser')
        for r_ent in soup.find_all('div', class_='r-ent'):
            t_tag = r_ent.select_one('.title a')
            p_tag = r_ent.select_one('.nrec span')
            if t_tag and t_tag.get('href'):
                s_str = p_tag.text.strip() if p_tag else '0'
                sc = 0
                if s_str == '爆': sc = 100
                elif s_str.isdigit(): sc = int(s_str)
                articles.append(Article(board, t_tag.text.strip(), "https://www.ptt.cc"+t_tag['href'], sc, 'normal'))
    except Exception as e: 
        print(f"爬取 {board} 版搜尋時發生錯誤: {e}", flush=True)
        
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
        
        # 🚀 這裡也順手加上多執行緒，讓首頁文字雲載入變飛快
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            def fetch_board(board):
                try:
                    url = f"https://www.ptt.cc/bbs/{board}/index.html"
                    response = session.get(url, headers=HEADERS, timeout=5, verify=False)
                    if response.status_code != 200: return []
                    soup = BeautifulSoup(response.text, 'html.parser')
                    res = []
                    for r_ent in soup.find_all('div', class_='r-ent'):
                        t_tag = r_ent.select_one('.title a')
                        p_tag = r_ent.select_one('.nrec span')
                        if t_tag and not t_tag.text.strip().startswith('[公告]'):
                            s_str = p_tag.text.strip() if p_tag else '0'
                            sc = 100 if s_str == '爆' else (int(s_str) if s_str.isdigit() else 0)
                            if sc >= 5: 
                                res.append(Article(board, t_tag.text.strip(), "https://www.ptt.cc"+t_tag['href'], sc, 'hot'))
                    return res
                except: return []

            futures = [executor.submit(fetch_board, b) for b in TARGET_BOARDS]
            for future in concurrent.futures.as_completed(futures):
                all_articles.extend(future.result())

        all_articles.sort(key=lambda x: x.score, reverse=True)
        
        MAX_PER_BOARD = 5  
        board_counts = {board: 0 for board in TARGET_BOARDS}
        balanced_top = []
        
        for article in all_articles:
            if board_counts[article.board] < MAX_PER_BOARD:
                balanced_top.append(article)
                board_counts[article.board] += 1
            if len(balanced_top) >= 20:
                break
                
        top = balanced_top        
        if not top:
            return jsonify({'error': '無法取得文章'}), 500
            
        words = jieba.cut("".join([a.title for a in top]))
        
        filtered_words = [
            w.strip() for w in words 
            if len(w.strip()) > 1 
            and w.strip().lower() not in STOP_WORDS 
            and not w.strip().isdigit()
        ]
        
        counts = Counter(filtered_words)
        
        payload = {
            'articles': [a.to_dict() for a in top], 
            'keywords': [[k, int(15 + math.log(v)*10)] for k, v in counts.most_common(50)]
        }
        CACHE_DATA = {'timestamp': now, 'payload': payload}
        return jsonify(payload)
        
    except Exception as e: 
        print("🔥 首頁熱門 API 發生嚴重錯誤:")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/smart_subscribe', methods=['POST'])
def smart_subscribe():
    try:
        if request.is_json: data = request.get_json(silent=True) or {}
        else: data = request.form

        keyword = data.get('keyword') or data.get('keywords') or data.get('search_text') or ''
        keyword = keyword.strip()
        user_id = data.get('user_id', 'anonymous')

        if not keyword:
            return jsonify({'error': '請輸入關鍵字'}), 400

        if user_id != 'anonymous':
            db.session.add(SearchHistory(user_id=user_id, keyword=keyword))
            db.session.commit()

        session = get_robust_session()
        all_results = []
        
        # 🚀 優化 1：使用多執行緒「同時」搜尋 9 個看板
        with concurrent.futures.ThreadPoolExecutor(max_workers=9) as executor:
            futures = [executor.submit(search_board_keyword, session, board, keyword) for board in TARGET_BOARDS]
            for future in concurrent.futures.as_completed(futures):
                all_results.extend(future.result())
            
        if not all_results: 
            return jsonify({'message': '找不到相關討論'})

        all_results.sort(key=lambda x: x.score, reverse=True)
        # 回復至上一版：保留 10 篇文章的完整分析，維持分析深度
        top_10 = all_results[:10] 
        
        articles_text_blocks = [""] * len(top_10) 
        
        def fetch_and_format(index, a):
            summary = scrape_article_content(session, a.url)[:100].replace('\n', ' ')
            return index, f"ID: {index}\n標題: {a.title}\n摘要: {summary}\n\n"

        # 使用 10 個執行緒並行加速 10 篇文章的內容爬取
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(fetch_and_format, i, a) for i, a in enumerate(top_10)]
            for future in concurrent.futures.as_completed(futures):
                idx, formatted_text = future.result()
                articles_text_blocks[idx] = formatted_text
                
        articles_text = "".join(articles_text_blocks)

        prompt = f"""
        你是一個精通 PTT 文化的輿情分析師。使用者搜尋了關鍵字：「{keyword}」。
        請閱讀以下 PTT 文章摘要，我們的情感溫度量表 (0-100) 對應了五個 PTT 專屬階級：
        
        - 80~100分: 「夯」
        - 60~79分: 「頂級」
        - 40~59分: 「人上人」
        - 20~39分: 「NPC」
        - 0~19分: 「拉玩了」

        請嚴格遵守以下 JSON 結構輸出。
        {{
          "macro_score": 數字 (0-100),
          "macro_summary": "用 PTT 口吻總結風向",
          "articles": [
            {{
              "id": 文章ID,
              "author_temp": 數字 (0-100),
              "comment_temp": 數字 (0-100),
              "tier_badge": "請給出階級稱號",
              "reason": "綜合評估發文與留言，濃縮核心論點與衝突點",
              "is_sarcasm": 布林值
            }}
          ]
        }}

        文章列表:
        {articles_text}
        """
        
        model = genai.GenerativeModel('gemini-2.5-flash')
        
        # 回復正常生成，不限制短 token，讓 AI 能輸出完整推論理由
        response = model.generate_content(prompt)
        
        try: 
            json_str = response.text.replace('```json', '').replace('```', '').strip()
            ai_data = json.loads(json_str)
            matches = ai_data.get('articles', [])
            macro_score = ai_data.get('macro_score', 50)
            macro_summary = ai_data.get('macro_summary', '目前無明顯共識')
        except Exception as e: 
            print(f"⚠️ JSON 解析失敗: {e}", flush=True)
            matches = []
            macro_score = 50
            macro_summary = "無法產生總結"

        match_dict = {
            m.get('id'): {
                'reason': m.get('reason', '相關討論'), 
                'author_temp': m.get('author_temp', 50),
                'comment_temp': m.get('comment_temp', 50),
                'stance': m.get('tier_badge', '一般討論'),
                'is_sarcasm': m.get('is_sarcasm', False)
            } for m in matches
        }
        
        final_results = []
        # 回復：針對 10 篇文章產生結果列表
        for i, a in enumerate(top_10):
            final_results.append({
                'board': a.board, 
                'title': a.title, 
                'url': a.url, 
                'score': a.score, 
                'reason': match_dict.get(i, {}).get('reason', '相關討論'),
                'author_temp': match_dict.get(i, {}).get('author_temp', 50),
                'comment_temp': match_dict.get(i, {}).get('comment_temp', 50),
                'stance': match_dict.get(i, {}).get('stance', '一般討論'),
                'is_sarcasm': match_dict.get(i, {}).get('is_sarcasm', False)
            })

        return jsonify({
            'macro_score': macro_score,
            'macro_summary': macro_summary,
            'matches': final_results
        })
    except Exception as e:
        error_msg = str(e)
        print(f"❌ AI 分析發生嚴重錯誤: {error_msg}", flush=True)
        traceback.print_exc()
        
        # 🛡️ 攔截 429 錯誤，給前端溫柔的提示
        if "429" in error_msg or "quota" in error_msg.lower():
            return jsonify({"error": "⚠️ AI 系統正在冷卻中 (免費版 API 限制)，請等待 1 分鐘後再試！"}), 429
            
        return jsonify({"error": "內部伺服器錯誤"}), 500

@app.route('/api/trend', methods=['GET'])
def get_trend():
    keyword = request.args.get('keyword', '未知名')
    dates = [(datetime.now() - timedelta(days=i)).strftime('%m/%d') for i in range(6, -1, -1)]
    
    is_hot_today = False
    if CACHE_DATA.get('payload') and CACHE_DATA['payload'].get('keywords'):
        hot_words = [item[0].lower() for item in CACHE_DATA['payload']['keywords']]
        search_kw = keyword.lower()
        if any(search_kw in w or w in search_kw for w in hot_words):
            is_hot_today = True

    random.seed(len(keyword) + sum(ord(c) for c in keyword)) 
    base_volume = random.randint(30, 120)
    
    volumes = []
    for i in range(7):
        if i == 6 and is_hot_today:
            vol = base_volume + random.randint(80, 200) 
        elif i == 6 and not is_hot_today:
            vol = base_volume + random.randint(-5, 5)
        elif i >= 4:
            vol = base_volume + random.randint(20, 80) if is_hot_today else base_volume + random.randint(-5, 5)
        else:
            vol = base_volume + random.randint(-20, 30) if is_hot_today else base_volume + random.randint(-5, 5)
            
        volumes.append(max(0, int(vol)))
        
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
    if not user_id or user_id == 'Guest': 
        return jsonify([])

    subs = Subscription.query.filter_by(user_id=user_id).all()
    sub_kws = [s.keyword for s in subs]

    histories = SearchHistory.query.filter_by(user_id=user_id).order_by(SearchHistory.search_time.desc()).limit(20).all()
    hist_kws = []
    for h in histories:
        if h.keyword not in hist_kws and h.keyword not in sub_kws:
            hist_kws.append(h.keyword)
        if len(hist_kws) >= 5: 
            break

    all_kws = list(set(sub_kws + hist_kws))
    valid_kws = [kw for kw in all_kws if not kw.isdigit() and len(kw) >= 2]

    if not valid_kws: 
        return jsonify([])

    recs = []
    seen_urls = set()

    if CACHE_DATA['payload']:
        for a in CACHE_DATA['payload'].get('articles', []):
            for kw in valid_kws:
                if kw.lower() in a['title'].lower() and a['url'] not in seen_urls:
                    reason = f"⭐ 專屬訂閱: {kw}" if kw in sub_kws else f"🔍 近期搜尋: {kw}"
                    recs.append({
                        'board': a['board'], 
                        'title': a['title'], 
                        'url': a['url'], 
                        'reason': reason, 
                        'score': a['score']
                    })
                    seen_urls.add(a['url'])
                    break

    if len(recs) < 5:
        top_keyword = valid_kws[0] if sub_kws else hist_kws[0]
        try:
            session = get_robust_session()
            fallback_boards = ['Gossiping', 'Stock', 'Tech_Job', 'NBA']
            extra_results = []
            
            # 🚀 優化 3：連被動推薦的 Fallback 搜尋也改成多執行緒！
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(search_board_keyword, session, board, top_keyword) for board in fallback_boards]
                for future in concurrent.futures.as_completed(futures):
                    extra_results.extend(future.result())
            
            extra_results.sort(key=lambda x: x.score, reverse=True)
            
            for a in extra_results:
                if a.url not in seen_urls:
                    recs.append({
                        'board': a.board, 
                        'title': a.title, 
                        'url': a.url, 
                        'reason': f"💡 猜你喜歡: {top_keyword}", 
                        'score': a.score
                    })
                    seen_urls.add(a.url)
                if len(recs) >= 5:
                    break
        except Exception as e:
            print(f"動態抓取推薦文章失敗: {e}", flush=True)

    recs.sort(key=lambda x: x['score'], reverse=True)
    return jsonify(recs[:7])

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)