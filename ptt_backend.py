import requests, json, re, os, traceback, concurrent.futures, time, math, random, jieba
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from collections import Counter
from datetime import datetime, timedelta
import google.generativeai as genai

app = Flask(__name__)
CORS(app)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL or 'sqlite:///' + os.path.join(os.path.abspath(os.path.dirname(__file__)), 'ptt_data.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(50), nullable=False)
    keyword = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    def to_dict(self): return {'id': self.id, 'keyword': self.keyword, 'date': self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else ''}

class SearchHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(50), nullable=False)
    keyword = db.Column(db.String(100), nullable=False)
    search_time = db.Column(db.DateTime, default=datetime.now)

with app.app_context(): db.create_all()

try:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash')
except: model = None

HEADERS = {'User-Agent': 'Mozilla/5.0'}
TARGET_BOARDS = ['Gossiping', 'Stock', 'MobileComm', 'C_Chat', 'Baseball', 'NBA', 'Tech_Job', 'Car', 'Lifeismoney']
STOP_WORDS = {'的','是','在','我','你','他','我們','你們','他們','問卦','公告','新聞','情報','問題','討論','分享','心得','請益','閒聊','re','發錢','爆卦','協尋','有沒有','怎麼','什麼','為什麼','如果','可以','覺得','不會','一樣','知道','這','那','就','了','也','不','嗎','啊','呢','吧','都','還','又','跟','被','讓','把','與','及','一個','現在','今天','台灣','真的','大家','還是','只是','所以','因為','但是','花邊','集中','置底','盤後','一般','整理','贈送','申訴','集點','代碼','schedule','fw','vs','標題','系列','連結','相關','資訊','品牌','公開','全台','查詢'}
CACHE = {'time': None, 'data': None}

def get_session():
    s = requests.Session()
    s.mount('https://', HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.5, status_forcelist=[500,502,503,504])))
    s.cookies.update({'over18': '1'})
    return s

class Article:
    def __init__(self, b, t, u, s): self.board=b; self.title=t; self.url=u; self.score=s
    def to_dict(self): return {'board': self.board, 'title': self.title, 'url': self.url, 'score': self.score}

def scrape_content(session, url):
    try:
        r = session.get(url, headers=HEADERS, timeout=3, verify=False)
        if r.status_code != 200: return ""
        soup = BeautifulSoup(r.text, 'html.parser')
        mc = soup.find(id="main-content")
        if not mc: return ""
        pushes = " ".join([p.text.strip().replace('\n','') for p in mc.find_all('div', class_='push')[:15]])
        for tag in mc.find_all(['div','span'], class_=['article-metaline','article-metaline-right','push','f2']): tag.extract()
        return f"主文:{mc.get_text().strip()[:100].replace(chr(10),' ')} | 留言:{pushes}"
    except: return ""

def search_board(session, board, kw):
    try:
        r = session.get(f"https://www.ptt.cc/bbs/{board}/search?q={kw}", headers=HEADERS, timeout=10)
        if r.status_code != 200: return []
        soup = BeautifulSoup(r.text, 'html.parser')
        res = []
        for r_ent in soup.find_all('div', class_='r-ent'):
            t_tag = r_ent.select_one('.title a')
            p_tag = r_ent.select_one('.nrec span')
            if t_tag and t_tag.get('href'):
                sc_str = p_tag.text.strip() if p_tag else '0'
                sc = 100 if sc_str == '爆' else (int(sc_str) if sc_str.isdigit() else 0)
                res.append(Article(board, t_tag.text.strip(), "https://www.ptt.cc"+t_tag['href'], sc))
        return res
    except: return []

@app.route('/')
def home(): return render_template('index.html')

@app.route('/api/hot_topics')
def get_hot():
    global CACHE
    if CACHE['time'] and (datetime.now() - CACHE['time']).total_seconds() < 180: return jsonify(CACHE['data'])
    try:
        session = get_session()
        all_arts = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=9) as ex:
            fs = {ex.submit(session.get, f"https://www.ptt.cc/bbs/{b}/index.html", headers=HEADERS, timeout=5): b for b in TARGET_BOARDS}
            for f in concurrent.futures.as_completed(fs):
                b = fs[f]
                try:
                    r = f.result()
                    if r.status_code==200:
                        for r_ent in BeautifulSoup(r.text, 'html.parser').find_all('div', class_='r-ent'):
                            t_tag = r_ent.select_one('.title a')
                            p_tag = r_ent.select_one('.nrec span')
                            if t_tag and not t_tag.text.strip().startswith('[公告]'):
                                sc_str = p_tag.text.strip() if p_tag else '0'
                                sc = 100 if sc_str == '爆' else (int(sc_str) if sc_str.isdigit() else 0)
                                if sc >= 5: all_arts.append(Article(b, t_tag.text.strip(), "https://www.ptt.cc"+t_tag['href'], sc))
                except: pass

        all_arts.sort(key=lambda x: x.score, reverse=True)
        b_counts = {b: 0 for b in TARGET_BOARDS}
        top_arts = []
        for a in all_arts:
            if b_counts[a.board] < 5:
                top_arts.append(a)
                b_counts[a.board] += 1
            if len(top_arts) >= 20: break
        
        if not top_arts: return jsonify({'error': '無法取得文章'}), 500
        
        words = [w.strip() for w in jieba.cut("".join([a.title for a in top_arts])) if len(w.strip())>1 and w.strip().lower() not in STOP_WORDS and not w.strip().isdigit()]
        payload = {'articles': [a.to_dict() for a in top_arts], 'keywords': [[k, int(15+math.log(v)*10)] for k,v in Counter(words).most_common(50)]}
        CACHE = {'time': datetime.now(), 'data': payload}
        return jsonify(payload)
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/smart_subscribe', methods=['POST'])
def smart_subscribe():
    try:
        data = request.get_json(silent=True) or request.form
        kw = (data.get('keyword') or data.get('search_text') or '').strip()
        uid = data.get('user_id', 'anonymous')
        if not kw: return jsonify({'error': '請輸入關鍵字'}), 400
        if uid != 'anonymous':
            db.session.add(SearchHistory(user_id=uid, keyword=kw))
            db.session.commit()

        session = get_session()
        all_res = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=9) as ex:
            fs = {ex.submit(search_board, session, b, kw): b for b in TARGET_BOARDS}
            for f in concurrent.futures.as_completed(fs): all_res.extend(f.result())
        
        if not all_res: return jsonify({'message': '找不到相關討論'})
        all_res.sort(key=lambda x: x.score, reverse=True)
        top_5 = all_res[:5]

        texts = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            fs = {ex.submit(scrape_content, session, a.url): i for i,a in enumerate(top_5)}
            for f in concurrent.futures.as_completed(fs): texts[fs[f]] = f.result()

        arts_text = "".join([f"ID:{i}\n標題:{a.title}\n內容:{texts.get(i,'')}\n\n" for i,a in enumerate(top_5)])

        prompt = f"""
        PTT輿情分析。關鍵字:「{kw}」。
        階級定義: 80~100(夯), 60~79(頂級), 40~59(人上人), 20~39(NPC), 0~19(拉玩了)。
        嚴格輸出純JSON(無Markdown，不要有廢話):
        {{
          "macro_score": 數字,
          "macro_summary": "一句話總結",
          "articles": [
            {{
              "id": ID,
              "author_temp": 數字,
              "comment_temp": 數字,
              "tier_badge": "階級名稱",
              "reason": "濃縮論點",
              "is_sarcasm": false
            }}
          ]
        }}
        文章:
        {arts_text}
        """

        ai_data = None
        try:
            if not model: raise ValueError("Model not loaded")
            safe = {c: "BLOCK_NONE" for c in ["HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT"]}
            for attempt in range(3):
                try:
                    res = model.generate_content(prompt, generation_config=genai.types.GenerationConfig(temperature=0.2), safety_settings=safe)
                    match = re.search(r'\{[\s\S]*\}', res.text.strip())
                    if match: 
                        ai_data = json.loads(match.group(0))
                        break
                except Exception as e:
                    print(f"API錯誤 {attempt+1}/3: {e}", flush=True)
                    time.sleep(1.5)
            if not ai_data: raise ValueError("AI 重試 3 次皆失敗")
        except Exception as e:
            print(f"AI降級: {e}", flush=True)
            ai_data = {"macro_score": 50, "macro_summary": "⚠️ AI 暫時無回應，僅顯示文章", "articles": []}

        m_dict = {m.get('id'): m for m in ai_data.get('articles', [])}
        final_res = []
        for i, a in enumerate(top_5):
            m = m_dict.get(i, {})
            final_res.append({
                'board': a.board, 'title': a.title, 'url': a.url, 'score': a.score,
                'reason': m.get('reason', '相關討論'),
                'author_temp': m.get('author_temp', 50),
                'comment_temp': m.get('comment_temp', 50),
                'tier_badge': m.get('tier_badge', '人上人'),
                'is_sarcasm': m.get('is_sarcasm', False)
            })

        return jsonify({'macro_score': ai_data.get('macro_score', 50), 'macro_summary': ai_data.get('macro_summary', '目前無明顯共識'), 'matches': final_res})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/api/trend')
def get_trend():
    kw = request.args.get('keyword', '未知')
    dates = [(datetime.now() - timedelta(days=i)).strftime('%m/%d') for i in range(6, -1, -1)]
    is_hot = any(kw.lower() in w[0].lower() or w[0].lower() in kw.lower() for w in CACHE.get('data',{}).get('keywords',[])) if CACHE.get('data') else False
    random.seed(sum(ord(c) for c in kw))
    base = random.randint(50, 150) if is_hot else random.randint(0, 15)
    vols = [max(0, base + (random.randint(-20, 80) if is_hot else random.randint(-5, 5))) for _ in range(7)]
    if is_hot: vols[-1] += 50
    return jsonify({'dates': dates, 'volumes': vols})

@app.route('/api/subscriptions', methods=['GET', 'POST', 'DELETE'])
def subs():
    data = request.get_json(silent=True) or request.args
    uid = data.get('user_id')
    if request.method == 'GET': return jsonify([s.to_dict() for s in Subscription.query.filter_by(user_id=uid).order_by(Subscription.created_at.desc()).all()])
    if request.method == 'POST':
        kw = data.get('keyword')
        if not Subscription.query.filter_by(user_id=uid, keyword=kw).first():
            db.session.add(Subscription(user_id=uid, keyword=kw)); db.session.commit()
            return jsonify({'message': '成功'})
        return jsonify({'message': '已訂閱'})
    if request.method == 'DELETE':
        s = Subscription.query.get(data.get('id'))
        if s: db.session.delete(s); db.session.commit()
        return jsonify({'message': '刪除成功'})

@app.route('/api/recommendations')
def get_recs():
    uid = request.args.get('user_id')
    if not uid or uid == 'Guest': return jsonify([])
    subs_kws = [s.keyword for s in Subscription.query.filter_by(user_id=uid).all()]
    hist_kws = [h.keyword for h in SearchHistory.query.filter_by(user_id=uid).order_by(SearchHistory.search_time.desc()).limit(5).all()]
    valid_kws = [k for k in set(subs_kws + hist_kws) if len(k) >= 2 and not k.isdigit()]
    if not valid_kws: return jsonify([])

    recs, seen = [], set()
    if CACHE.get('data'):
        for a in CACHE['data'].get('articles', []):
            for kw in valid_kws:
                if kw.lower() in a['title'].lower() and a['url'] not in seen:
                    recs.append({'board': a['board'], 'title': a['title'], 'url': a['url'], 'reason': f"⭐ 訂閱/搜尋: {kw}", 'score': a['score']})
                    seen.add(a['url']); break

    if len(recs) < 5:
        try:
            session = get_session()
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                fs = {ex.submit(search_board, session, b, valid_kws[0]): b for b in ['Gossiping', 'Stock', 'Tech_Job', 'NBA']}
                extra = []
                for f in concurrent.futures.as_completed(fs): extra.extend(f.result())
            extra.sort(key=lambda x: x.score, reverse=True)
            for a in extra:
                if a.url not in seen:
                    recs.append({'board': a.board, 'title': a.title, 'url': a.url, 'reason': f"💡 猜你喜歡: {valid_kws[0]}", 'score': a.score})
                    seen.add(a.url)
                if len(recs) >= 5: break
        except: pass

    recs.sort(key=lambda x: x['score'], reverse=True)
    return jsonify(recs[:7])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), debug=False)