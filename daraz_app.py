from flask import Flask, request, jsonify, session as flask_session, redirect
from flask_cors import CORS
from bs4 import BeautifulSoup
import requests
import random
import time
import hashlib
import os
import psycopg2
import psycopg2.extras
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
app.secret_key = "daraz_finder_secret_2024_xK9mP"
CORS(app)

# ══════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════
ADMIN_USER   = os.environ.get("ADMIN_USER",  "admin")
ADMIN_PASS   = os.environ.get("ADMIN_PASS",  "DarazAdmin2024")
DATABASE_URL = os.environ.get("DATABASE_URL", "YOUR_NEON_OR_SUPABASE_URL_HERE")
CRON_SECRET  = os.environ.get("CRON_SECRET", "mysecret123")
# ══════════════════════════════════════════

USER_AGENTS = [
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(100,122)}.0.0.0 Safari/537.36",
    f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(100,122)}.0.0.0 Safari/537.36",
    f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(100,122)}.0.0.0 Safari/537.36",
    f"Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
] * 50

daraz_session = requests.Session()

def init_daraz_session():
    try:
        daraz_session.get("https://www.daraz.com.bd/",
            headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=10)
    except: pass

init_daraz_session()

# ══════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn

def init_db():
    conn = get_db(); c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id       SERIAL PRIMARY KEY,
            email    TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            name     TEXT,
            created  TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS tracked (
            id      SERIAL PRIMARY KEY,
            user_id INTEGER,
            item_id TEXT NOT NULL,
            name    TEXT,
            image   TEXT,
            url     TEXT,
            added   TIMESTAMP DEFAULT NOW(),
            UNIQUE(user_id, item_id)
        );
        CREATE TABLE IF NOT EXISTS price_history (
            id      SERIAL PRIMARY KEY,
            item_id TEXT NOT NULL,
            price   FLOAT,
            checked TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS search_logs (
            id      SERIAL PRIMARY KEY,
            user_id INTEGER,
            keyword TEXT,
            results INTEGER,
            ts      TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS click_logs (
            id      SERIAL PRIMARY KEY,
            user_id INTEGER,
            item_id TEXT,
            name    TEXT,
            price   FLOAT,
            ts      TIMESTAMP DEFAULT NOW()
        );
    """)
    conn.commit(); conn.close()

try:
    init_db()
    print("Database connected.")
except Exception as e:
    print(f"DB error: {e}")

# ══════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════
def hash_pass(p):
    return hashlib.sha256(p.encode()).hexdigest()

def logged_in():
    return flask_session.get('user_id')

def admin_in():
    return flask_session.get('is_admin')

def parse_sold(s):
    if not s: return 0
    s = str(s).lower().replace(" sold","").replace(",","").strip()
    if "k" in s:
        try: return int(float(s.replace("k","")) * 1000)
        except: return 0
    try: return int(s)
    except: return 0

def safe_float(v, d=0.0):
    try: return float(v) if v not in ('',None) else d
    except: return d

def safe_int(v, d=0):
    try: return int(v) if v not in ('',None) else d
    except: return d

def fix_image(img):
    if not img: return ""
    img = str(img).strip()
    if img.startswith("http"): return img
    elif img.startswith("//"): return "https:" + img
    elif img.startswith("/"): return "https://www.daraz.com.bd" + img
    return "https:" + img

def calc_deal_score(price, orig_price, rating, sold, reviews):
    import math
    score = 0
    if rating > 0:   score += (rating / 5.0) * 35
    if sold > 0:     score += min(math.log10(sold+1) / math.log10(100001) * 30, 30)
    if reviews > 0:  score += min(math.log10(reviews+1) / math.log10(10001) * 15, 15)
    if orig_price > 0 and price > 0 and orig_price > price:
        disc = (orig_price - price) / orig_price
        score += min(disc * 20, 20)
    return round(score)

# ══════════════════════════════════════════
#  DARAZ SCRAPER
# ══════════════════════════════════════════
def fetch_page(args):
    keyword, page, filters = args
    time.sleep(random.uniform(1.5, 4.0))
    kw = keyword.replace(" ", "+")
    url = f"https://www.daraz.com.bd/catalog/?ajax=true&page={page}&q={kw}"
    headers = {
        'Referer': 'https://www.daraz.com.bd/',
        'User-Agent': random.choice(USER_AGENTS),
        'Accept-Language': 'en-US,en;q=0.9',
    }
    try:
        resp = daraz_session.get(url, headers=headers, timeout=15)
        data = resp.json()
        items = data.get('mods', {}).get('listItems', [])
        total = len(items)
        out   = []

        for item in items:
            try:
                price      = safe_float(item.get('price'))
                orig_price = safe_float(item.get('originalPrice'))
                rating     = safe_float(item.get('ratingScore'))
                sold       = parse_sold(item.get('itemSoldCntShow'))
                reviews    = safe_int(item.get('review'))
                name       = item.get('name', 'N/A')
                item_url   = 'https:' + str(item.get('itemUrl',''))
                image      = fix_image(item.get('image',''))
                item_id    = str(item.get('itemId') or item_url.split('i')[1].split('.')[0] if 'i' in item_url else item_url[-10:])
                discount   = item.get('discount','')
                location   = item.get('location','N/A')
                seller     = item.get('sellerName','N/A')

                # filters
                if filters['min_price'] > 0 and price > 0 and price < filters['min_price']: continue
                if filters['max_price'] > 0 and price > 0 and price > filters['max_price']: continue
                if filters['min_rating'] > 0 and rating > 0 and rating < filters['min_rating']: continue
                if filters['min_sold'] > 0 and sold < filters['min_sold']: continue

                out.append({
                    'item_id':    item_id,
                    'name':       name,
                    'price':      price,
                    'orig_price': orig_price,
                    'discount':   discount,
                    'rating':     rating,
                    'reviews':    reviews,
                    'sold':       sold,
                    'sold_str':   item.get('itemSoldCntShow','0'),
                    'location':   location,
                    'seller':     seller,
                    'image':      image,
                    'url':        item_url,
                    'deal_score': calc_deal_score(price, orig_price, rating, sold, reviews),
                })
            except: continue
        return out, total
    except Exception as e:
        print(f"Page {page} error: {e}")
        return [], 0

def dedup(products):
    seen, out = set(), []
    for p in products:
        if p['item_id'] not in seen:
            seen.add(p['item_id']); out.append(p)
    return out

# ══════════════════════════════════════════
#  COMMON STYLES
# ══════════════════════════════════════════
BASE_CSS = """
<style>
:root{
  --bg:#060608;--bg2:#0E0E12;--bg3:#15151C;--bg4:#1C1C26;
  --border:#22222E;--border2:#2A2A38;
  --orange:#F97316;--orange2:#FB923C;
  --blue:#3B82F6;--green:#10B981;--red:#EF4444;--purple:#8B5CF6;
  --text:#F1F1F8;--muted:#6B6B8A;--muted2:#9999B8;
}
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box;}
html{scroll-behavior:smooth;}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;min-height:100vh;}
a{color:inherit;text-decoration:none;}
input,select,button{font-family:inherit;}
.wrap{max-width:1300px;margin:0 auto;padding:0 20px;}
nav{padding:14px 20px;border-bottom:1px solid var(--border);background:rgba(6,6,8,.92);backdrop-filter:blur(12px);position:sticky;top:0;z-index:100;}
.nav-inner{max-width:1300px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;}
.brand{font-family:'Bebas Neue',sans-serif;font-size:1.5rem;letter-spacing:1px;}
.brand span{color:var(--orange);}
.nav-links{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
.nav-link{padding:7px 14px;border-radius:8px;font-size:.82rem;font-weight:600;background:var(--bg3);border:1px solid var(--border2);color:var(--muted2);cursor:pointer;transition:all .18s;display:inline-block;}
.nav-link:hover,.nav-link.active{border-color:var(--orange);color:var(--orange);}
.nav-link.danger:hover{border-color:var(--red);color:var(--red);}
.btn{display:inline-block;padding:11px 22px;border-radius:10px;font-size:.88rem;font-weight:700;cursor:pointer;border:none;transition:all .2s;}
.btn-main{background:linear-gradient(135deg,var(--orange),var(--orange2));color:#fff;box-shadow:0 4px 16px rgba(249,115,22,.3);}
.btn-main:hover{opacity:.9;}
.btn-outline{background:transparent;border:1.5px solid var(--border2);color:var(--muted2);}
.btn-outline:hover{border-color:var(--orange);color:var(--orange);}
.btn-red{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);color:#F87171;}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:14px;padding:20px;}
.badge{display:inline-block;padding:3px 10px;border-radius:50px;font-size:.68rem;font-weight:700;}
.badge-green{background:rgba(16,185,129,.12);color:#34D399;border:1px solid rgba(16,185,129,.2);}
.badge-amber{background:rgba(249,115,22,.12);color:var(--orange2);border:1px solid rgba(249,115,22,.2);}
.badge-red{background:rgba(239,68,68,.12);color:#F87171;border:1px solid rgba(239,68,68,.2);}
.badge-blue{background:rgba(59,130,246,.12);color:#60A5FA;border:1px solid rgba(59,130,246,.2);}
.form-group{display:flex;flex-direction:column;gap:6px;margin-bottom:14px;}
.form-group label{font-size:.68rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;}
.form-group input{background:var(--bg3);border:1.5px solid var(--border2);border-radius:9px;padding:12px 14px;color:var(--text);font-size:.92rem;outline:none;transition:border-color .2s;}
.form-group input:focus{border-color:var(--orange);}
.alert{padding:12px 16px;border-radius:10px;font-size:.85rem;margin-bottom:14px;}
.alert-red{background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.2);color:#FCA5A5;}
.alert-green{background:rgba(16,185,129,.08);border:1px solid rgba(16,185,129,.2);color:#6EE7B7;}
table{width:100%;border-collapse:collapse;}
th{text-align:left;font-size:.68rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;padding:10px 12px;border-bottom:1px solid var(--border);}
td{padding:10px 12px;font-size:.83rem;border-bottom:1px solid var(--border);vertical-align:middle;}
tr:last-child td{border-bottom:none;}
tr:hover td{background:var(--bg3);}
.stat-card{background:var(--bg2);border:1px solid var(--border);border-radius:14px;padding:20px;}
.stat-num{font-size:2rem;font-weight:700;margin-bottom:4px;}
.stat-lbl{font-size:.75rem;color:var(--muted2);}
</style>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
"""

def nav_html(page=""):
    uid   = flask_session.get('user_id')
    admin = flask_session.get('is_admin')
    user  = flask_session.get('user_name','')
    links = []
    if admin:
        links += [
            f'<a class="nav-link {"active" if page=="admin" else ""}" href="/admin">Admin Panel</a>',
            f'<a class="nav-link" href="/">Search</a>',
            f'<a class="nav-link danger" href="/logout">Logout</a>',
        ]
    elif uid:
        links += [
            f'<a class="nav-link" href="/">Search</a>',
            f'<a class="nav-link {"active" if page=="tracked" else ""}" href="/my-products">My Products</a>',
            f'<span style="font-size:.8rem;color:var(--muted2);padding:0 6px;">Hi, {user}</span>',
            f'<a class="nav-link danger" href="/logout">Logout</a>',
        ]
    else:
        links += [
            f'<a class="nav-link" href="/">Search</a>',
            f'<a class="nav-link {"active" if page=="login" else ""}" href="/login">Login</a>',
            f'<a class="nav-link {"active" if page=="register" else ""}" href="/register">Register</a>',
        ]
    return f"""<nav><div class="nav-inner">
      <a class="brand" href="/">Daraz <span>Finder</span></a>
      <div class="nav-links">{''.join(links)}</div>
    </div></nav>"""

# ══════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════
@app.route('/login', methods=['GET','POST'])
def login():
    error = ""
    if request.method == 'POST':
        email = request.form.get('email','').strip()
        pwd   = request.form.get('password','')
        if email == ADMIN_USER and pwd == ADMIN_PASS:
            flask_session['is_admin']  = True
            flask_session['user_name'] = 'Admin'
            return redirect('/admin')
        try:
            conn = get_db(); c = conn.cursor()
            c.execute("SELECT * FROM users WHERE email=%s AND password=%s", (email, hash_pass(pwd)))
            user = c.fetchone(); conn.close()
            if user:
                flask_session['user_id']   = user['id']
                flask_session['user_name'] = user['name'] or user['email'].split('@')[0]
                return redirect('/')
            else: error = "Wrong email or password."
        except Exception as e: error = f"Error: {e}"

    return f"""<!DOCTYPE html><html><head><title>Login</title>{BASE_CSS}</head><body>
    {nav_html('login')}
    <div class="wrap" style="max-width:420px;margin:60px auto;padding:0 20px;">
      <div class="card">
        <h2 style="font-family:'Bebas Neue',sans-serif;font-size:1.8rem;letter-spacing:1px;margin-bottom:6px;">Welcome Back</h2>
        <p style="color:var(--muted2);font-size:.85rem;margin-bottom:24px;">Login to track Daraz prices</p>
        {f'<div class="alert alert-red">{error}</div>' if error else ''}
        <form method="POST">
          <div class="form-group"><label>Email</label><input type="text" name="email" placeholder="you@email.com" required></div>
          <div class="form-group"><label>Password</label><input type="password" name="password" placeholder="••••••••" required></div>
          <button class="btn btn-main" style="width:100%;margin-top:8px;">Login</button>
        </form>
        <p style="text-align:center;margin-top:16px;font-size:.82rem;color:var(--muted2);">
          No account? <a href="/register" style="color:var(--orange);font-weight:600;">Register →</a>
        </p>
      </div>
    </div></body></html>"""

@app.route('/register', methods=['GET','POST'])
def register():
    error = msg = ""
    if request.method == 'POST':
        name  = request.form.get('name','').strip()
        email = request.form.get('email','').strip()
        pwd   = request.form.get('password','')
        if len(pwd) < 6: error = "Password must be at least 6 characters."
        else:
            try:
                conn = get_db(); c = conn.cursor()
                c.execute("INSERT INTO users (name,email,password) VALUES (%s,%s,%s)",
                          (name, email, hash_pass(pwd)))
                conn.commit(); conn.close()
                msg = "Account created! You can now login."
            except: error = "Email already registered."

    return f"""<!DOCTYPE html><html><head><title>Register</title>{BASE_CSS}</head><body>
    {nav_html('register')}
    <div class="wrap" style="max-width:420px;margin:60px auto;padding:0 20px;">
      <div class="card">
        <h2 style="font-family:'Bebas Neue',sans-serif;font-size:1.8rem;letter-spacing:1px;margin-bottom:6px;">Create Account</h2>
        <p style="color:var(--muted2);font-size:.85rem;margin-bottom:24px;">Track Daraz prices for free</p>
        {f'<div class="alert alert-red">{error}</div>' if error else ''}
        {f'<div class="alert alert-green">{msg}</div>' if msg else ''}
        <form method="POST">
          <div class="form-group"><label>Name</label><input type="text" name="name" placeholder="Your name" required></div>
          <div class="form-group"><label>Email</label><input type="email" name="email" placeholder="you@email.com" required></div>
          <div class="form-group"><label>Password</label><input type="password" name="password" placeholder="Min 6 characters" required></div>
          <button class="btn btn-main" style="width:100%;margin-top:8px;">Create Account</button>
        </form>
        <p style="text-align:center;margin-top:16px;font-size:.82rem;color:var(--muted2);">
          Have account? <a href="/login" style="color:var(--orange);font-weight:600;">Login →</a>
        </p>
      </div>
    </div></body></html>"""

@app.route('/logout')
def logout():
    flask_session.clear()
    return redirect('/')

# ══════════════════════════════════════════
#  TRACKING API
# ══════════════════════════════════════════
@app.route('/api/track', methods=['POST'])
def track():
    if not logged_in(): return jsonify({'error':'login required'}), 401
    d = request.get_json()
    item_id = d.get('item_id','')
    if not item_id: return jsonify({'error':'item_id required'}), 400
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("INSERT INTO tracked (user_id,item_id,name,image,url) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                  (flask_session['user_id'], item_id, d.get('name'), d.get('image'), d.get('url')))
        if d.get('price',0) > 0:
            c.execute("INSERT INTO price_history (item_id,price) VALUES (%s,%s)", (item_id, d['price']))
        conn.commit(); conn.close()
        return jsonify({'tracked': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/untrack', methods=['POST'])
def untrack():
    if not logged_in(): return jsonify({'error':'login required'}), 401
    item_id = request.get_json().get('item_id','')
    conn = get_db(); c = conn.cursor()
    c.execute("DELETE FROM tracked WHERE user_id=%s AND item_id=%s", (flask_session['user_id'], item_id))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/log-click', methods=['POST'])
def log_click():
    d = request.get_json()
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("INSERT INTO click_logs (user_id,item_id,name,price) VALUES (%s,%s,%s,%s)",
                  (flask_session.get('user_id'), d.get('item_id'), d.get('name'), d.get('price',0)))
        conn.commit(); conn.close()
    except: pass
    return jsonify({'ok': True})

@app.route('/api/tracked-list')
def tracked_list():
    if not logged_in(): return jsonify({'items':[]})
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT item_id FROM tracked WHERE user_id=%s", (flask_session['user_id'],))
    rows = c.fetchall(); conn.close()
    return jsonify({'items': [r['item_id'] for r in rows]})

@app.route('/api/price-history/<item_id>')
def price_history(item_id):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT price, checked FROM price_history WHERE item_id=%s ORDER BY checked ASC LIMIT 90", (item_id,))
    rows = c.fetchall(); conn.close()
    return jsonify({'history': [{'price': r['price'], 'date': str(r['checked'])[:10]} for r in rows]})

# ══════════════════════════════════════════
#  MY PRODUCTS
# ══════════════════════════════════════════
@app.route('/my-products')
def my_products():
    if not logged_in(): return redirect('/login')
    conn = get_db(); c = conn.cursor()
    c.execute("""
        SELECT t.*,
          (SELECT price FROM price_history WHERE item_id=t.item_id ORDER BY checked DESC LIMIT 1) as last_price,
          (SELECT price FROM price_history WHERE item_id=t.item_id ORDER BY checked ASC  LIMIT 1) as first_price,
          (SELECT COUNT(*) FROM price_history WHERE item_id=t.item_id) as checks
        FROM tracked t WHERE t.user_id=%s ORDER BY t.added DESC
    """, (flask_session['user_id'],))
    items = c.fetchall(); conn.close()

    cards = ""
    for it in items:
        lp = it['last_price'] or 0
        fp = it['first_price'] or 0
        diff = lp - fp
        if diff < -1:   trend = f'<span class="badge badge-green">↓ ৳{abs(diff):.0f} সস্তা হয়েছে</span>'
        elif diff > 1:  trend = f'<span class="badge badge-red">↑ ৳{diff:.0f} বেড়েছে</span>'
        else:           trend = f'<span class="badge badge-amber">→ স্থিতিশীল</span>'

        cards += f"""
        <div class="card" style="display:flex;gap:16px;margin-bottom:14px;align-items:flex-start;">
          <img src="{it['image'] or ''}" style="width:80px;height:80px;object-fit:contain;border-radius:10px;background:var(--bg3);flex-shrink:0;"
               onerror="this.src='https://placehold.co/80x80'">
          <div style="flex:1;min-width:0;">
            <div style="font-size:.85rem;font-weight:600;margin-bottom:6px;line-height:1.4;">{(it['name'] or '')[:80]}</div>
            <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:8px;">
              <span style="font-size:1.1rem;font-weight:700;color:var(--orange);">{"৳{:.0f}".format(lp) if lp else "N/A"}</span>
              {trend}
              <span style="font-size:.72rem;color:var(--muted2);">{it['checks']} বার চেক হয়েছে</span>
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;">
              <a href="/price-graph/{it['item_id']}" class="btn btn-outline" style="padding:6px 14px;font-size:.75rem;">📈 Price Graph</a>
              <a href="{it['url']}" target="_blank" class="btn btn-main" style="padding:6px 14px;font-size:.75rem;">Daraz এ দেখুন →</a>
              <button class="btn btn-red" style="padding:6px 14px;font-size:.75rem;" onclick="untrack('{it['item_id']}',this)">সরাও</button>
            </div>
          </div>
        </div>"""

    if not items:
        cards = """<div style="text-align:center;padding:4rem 1rem;">
          <div style="font-size:3rem;margin-bottom:1rem;">📦</div>
          <h3 style="font-family:'Bebas Neue',sans-serif;font-size:1.6rem;letter-spacing:1px;margin-bottom:.5rem;">কোনো Tracked Product নেই</h3>
          <p style="color:var(--muted2);margin-bottom:20px;">Search করে 📌 ক্লিক করলে track হবে।</p>
          <a href="/" class="btn btn-main">Search করুন →</a>
        </div>"""

    return f"""<!DOCTYPE html><html><head><title>My Products</title>{BASE_CSS}</head><body>
    {nav_html('tracked')}
    <div class="wrap" style="padding-top:30px;padding-bottom:60px;">
      <h2 style="font-family:'Bebas Neue',sans-serif;font-size:2rem;letter-spacing:1px;margin-bottom:6px;">আমার Tracked Products</h2>
      <p style="color:var(--muted2);font-size:.85rem;margin-bottom:24px;">প্রতি রাতে automatically price check হয়।</p>
      {cards}
    </div>
    <script>
    async function untrack(item_id,btn){{
      if(!confirm('Remove this product?')) return;
      await fetch('/api/untrack',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{item_id}})}});
      btn.closest('.card').remove();
    }}
    </script></body></html>"""

# ══════════════════════════════════════════
#  PRICE GRAPH
# ══════════════════════════════════════════
@app.route('/price-graph/<item_id>')
def price_graph(item_id):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT name,image,url FROM tracked WHERE item_id=%s LIMIT 1", (item_id,))
    prod = c.fetchone(); conn.close()
    name  = prod['name']  if prod else item_id
    image = prod['image'] if prod else ''
    url   = prod['url']   if prod else '#'

    return f"""<!DOCTYPE html><html><head>
    <title>Price Graph</title>{BASE_CSS}
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    </head><body>
    {nav_html()}
    <div class="wrap" style="padding-top:30px;padding-bottom:60px;max-width:900px;">
      <div style="display:flex;gap:16px;align-items:center;margin-bottom:24px;flex-wrap:wrap;">
        <img src="{image}" style="width:70px;height:70px;object-fit:contain;border-radius:10px;background:var(--bg3);"
             onerror="this.src='https://placehold.co/70x70'">
        <div>
          <h2 style="font-size:1rem;font-weight:600;line-height:1.4;margin-bottom:6px;">{name}</h2>
          <a href="{url}" target="_blank" class="btn btn-main" style="padding:7px 16px;font-size:.78rem;">Daraz এ দেখুন →</a>
        </div>
      </div>
      <div class="card" style="margin-bottom:20px;">
        <canvas id="priceChart" height="100"></canvas>
        <p id="noData" style="display:none;text-align:center;padding:2rem;color:var(--muted2);">
          এখনো price data নেই। রাতের check এর পর আসুন!
        </p>
      </div>
      <div id="statsRow" style="display:grid;grid-template-columns:repeat(3,1fr);gap:14px;"></div>
    </div>
    <script>
    async function loadChart(){{
      const res  = await fetch('/api/price-history/{item_id}');
      const data = await res.json();
      const hist = data.history;
      if(!hist||hist.length===0){{
        document.getElementById('noData').style.display='block';
        document.getElementById('priceChart').style.display='none';
        return;
      }}
      const labels=hist.map(h=>h.date);
      const prices=hist.map(h=>h.price);
      const minP=Math.min(...prices),maxP=Math.max(...prices),lastP=prices[prices.length-1];
      document.getElementById('statsRow').innerHTML=`
        <div class="stat-card"><div class="stat-num" style="color:var(--green);">৳${{minP.toFixed(0)}}</div><div class="stat-lbl">সর্বনিম্ন দাম</div></div>
        <div class="stat-card"><div class="stat-num" style="color:var(--red);">৳${{maxP.toFixed(0)}}</div><div class="stat-lbl">সর্বোচ্চ দাম</div></div>
        <div class="stat-card"><div class="stat-num" style="color:var(--orange);">৳${{lastP.toFixed(0)}}</div><div class="stat-lbl">বর্তমান দাম</div></div>`;
      new Chart(document.getElementById('priceChart'),{{
        type:'line',
        data:{{labels,datasets:[{{
          label:'দাম (৳)',data:prices,
          borderColor:'#F97316',backgroundColor:'rgba(249,115,22,.08)',
          borderWidth:2.5,pointBackgroundColor:'#F97316',pointRadius:4,tension:0.3,fill:true
        }}]}},
        options:{{
          responsive:true,
          plugins:{{legend:{{labels:{{color:'#9999B8'}}}},tooltip:{{callbacks:{{label:ctx=>' ৳'+ctx.parsed.y.toFixed(0)}}}}}},
          scales:{{
            x:{{ticks:{{color:'#6B6B8A'}},grid:{{color:'#22222E'}}}},
            y:{{ticks:{{color:'#6B6B8A',callback:v=>'৳'+v}},grid:{{color:'#22222E'}}}}
          }}
        }}
      }});
    }}
    loadChart();
    </script></body></html>"""

# ══════════════════════════════════════════
#  ADMIN PANEL
# ══════════════════════════════════════════
@app.route('/admin')
def admin():
    if not admin_in(): return redirect('/login')
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT COUNT(*) as n FROM users");         total_users    = c.fetchone()['n']
    c.execute("SELECT COUNT(*) as n FROM tracked");       total_tracked  = c.fetchone()['n']
    c.execute("SELECT COUNT(*) as n FROM search_logs");   total_searches = c.fetchone()['n']
    c.execute("SELECT COUNT(*) as n FROM click_logs");    total_clicks   = c.fetchone()['n']
    c.execute("SELECT COUNT(*) as n FROM price_history"); total_prices   = c.fetchone()['n']

    c.execute("SELECT keyword,COUNT(*) as cnt FROM search_logs GROUP BY keyword ORDER BY cnt DESC LIMIT 10")
    top_kw = c.fetchall()

    c.execute("SELECT item_id,name,COUNT(*) as cnt FROM tracked GROUP BY item_id,name ORDER BY cnt DESC LIMIT 10")
    top_tracked = c.fetchall()

    c.execute("""SELECT s.keyword,s.results,s.ts,u.email FROM search_logs s
                 LEFT JOIN users u ON s.user_id=u.id ORDER BY s.ts DESC LIMIT 20""")
    recent_searches = c.fetchall()

    c.execute("""SELECT c.name,c.price,c.ts,u.email FROM click_logs c
                 LEFT JOIN users u ON c.user_id=u.id ORDER BY c.ts DESC LIMIT 20""")
    recent_clicks = c.fetchall()

    c.execute("SELECT id,name,email,created FROM users ORDER BY created DESC")
    users = c.fetchall()
    conn.close()

    kw_rows   = "".join(f"<tr><td><strong>{r['keyword']}</strong></td><td><span class='badge badge-amber'>{r['cnt']}</span></td></tr>" for r in top_kw)
    tr_rows   = "".join(f"<tr><td style='max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'>{(r['name'] or '')[:50]}</td><td><span class='badge badge-blue'>{r['cnt']} জন</span></td></tr>" for r in top_tracked)
    sr_rows   = "".join(f"<tr><td><strong>{r['keyword']}</strong></td><td>{r['results'] or '-'}</td><td>{r['email'] or 'Guest'}</td><td style='color:var(--muted2);font-size:.75rem;'>{str(r['ts'])[:16]}</td></tr>" for r in recent_searches)
    cl_rows   = "".join(f"<tr><td style='max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'>{r['name'] or '-'}</td><td style='color:var(--orange);'>৳{'{:.0f}'.format(r['price']) if r['price'] else 'N/A'}</td><td>{r['email'] or 'Guest'}</td><td style='color:var(--muted2);font-size:.75rem;'>{str(r['ts'])[:16]}</td></tr>" for r in recent_clicks)
    user_rows = "".join(f"<tr><td>{r['id']}</td><td>{r['name'] or '-'}</td><td>{r['email']}</td><td style='color:var(--muted2);font-size:.75rem;'>{str(r['created'])[:16]}</td></tr>" for r in users)

    return f"""<!DOCTYPE html><html><head><title>Admin Panel</title>{BASE_CSS}
    <style>
    .admin-grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-bottom:28px;}}
    .section{{margin-bottom:28px;}}
    .section-title{{font-family:'Bebas Neue',sans-serif;font-size:1.3rem;letter-spacing:1px;margin-bottom:14px;color:var(--orange2);}}
    @media(max-width:800px){{.admin-grid{{grid-template-columns:1fr 1fr;}}}}
    </style></head><body>
    {nav_html('admin')}
    <div class="wrap" style="padding-top:28px;padding-bottom:60px;">
      <h1 style="font-family:'Bebas Neue',sans-serif;font-size:2.2rem;letter-spacing:1px;margin-bottom:6px;">Admin Panel</h1>
      <p style="color:var(--muted2);font-size:.82rem;margin-bottom:24px;">Daraz Finder — সব তথ্য এক জায়গায়</p>

      <div class="admin-grid">
        <div class="stat-card"><div class="stat-num" style="color:var(--blue);">{total_users}</div><div class="stat-lbl">মোট Users</div></div>
        <div class="stat-card"><div class="stat-num" style="color:var(--orange);">{total_tracked}</div><div class="stat-lbl">Tracked Products</div></div>
        <div class="stat-card"><div class="stat-num" style="color:var(--green);">{total_searches}</div><div class="stat-lbl">মোট Searches</div></div>
        <div class="stat-card"><div class="stat-num" style="color:var(--purple);">{total_clicks}</div><div class="stat-lbl">Product Clicks</div></div>
        <div class="stat-card"><div class="stat-num" style="color:var(--red);">{total_prices}</div><div class="stat-lbl">Price Records</div></div>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:28px;">
        <div class="section">
          <div class="section-title">🔍 সবচেয়ে বেশি Search</div>
          <div class="card" style="padding:0;overflow:hidden;">
            <table><thead><tr><th>Keyword</th><th>বার</th></tr></thead>
            <tbody>{kw_rows or '<tr><td colspan=2 style="color:var(--muted2);text-align:center;padding:20px;">No data yet</td></tr>'}</tbody></table>
          </div>
        </div>
        <div class="section">
          <div class="section-title">📦 সবচেয়ে বেশি Tracked</div>
          <div class="card" style="padding:0;overflow:hidden;">
            <table><thead><tr><th>Product</th><th>Trackers</th></tr></thead>
            <tbody>{tr_rows or '<tr><td colspan=2 style="color:var(--muted2);text-align:center;padding:20px;">No data yet</td></tr>'}</tbody></table>
          </div>
        </div>
      </div>

      <div class="section">
        <div class="section-title">🕐 Recent Searches</div>
        <div class="card" style="padding:0;overflow:hidden;overflow-x:auto;">
          <table><thead><tr><th>Keyword</th><th>Results</th><th>User</th><th>Time</th></tr></thead>
          <tbody>{sr_rows or '<tr><td colspan=4 style="color:var(--muted2);text-align:center;padding:20px;">No searches yet</td></tr>'}</tbody></table>
        </div>
      </div>

      <div class="section">
        <div class="section-title">👆 Recent Clicks</div>
        <div class="card" style="padding:0;overflow:hidden;overflow-x:auto;">
          <table><thead><tr><th>Product</th><th>Price</th><th>User</th><th>Time</th></tr></thead>
          <tbody>{cl_rows or '<tr><td colspan=4 style="color:var(--muted2);text-align:center;padding:20px;">No clicks yet</td></tr>'}</tbody></table>
        </div>
      </div>

      <div class="section">
        <div class="section-title">👤 সব Users</div>
        <div class="card" style="padding:0;overflow:hidden;overflow-x:auto;">
          <table><thead><tr><th>#</th><th>Name</th><th>Email</th><th>Joined</th></tr></thead>
          <tbody>{user_rows or '<tr><td colspan=4 style="color:var(--muted2);text-align:center;padding:20px;">No users yet</td></tr>'}</tbody></table>
        </div>
      </div>

      <a href="/admin/run-check" class="btn btn-main" onclick="return confirm('Price check চালাবো?')">⚡ এখনই Price Check করুন</a>
    </div></body></html>"""

@app.route('/admin/run-check')
def admin_run_check():
    if not admin_in(): return redirect('/login')
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT DISTINCT item_id, url FROM tracked")
    items = c.fetchall(); conn.close()
    checked = 0
    for row in items:
        try:
            resp = daraz_session.get(row['url'],
                headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=15)
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, 'html.parser')
            price_el = soup.select_one('.pdp-price strong, .pdp-price, [class*="price"]')
            if price_el:
                txt = price_el.get_text(strip=True).replace('৳','').replace(',','').strip()
                try:
                    price = float(txt.split()[0])
                    if price > 0:
                        conn2 = get_db(); c2 = conn2.cursor()
                        c2.execute("INSERT INTO price_history (item_id,price) VALUES (%s,%s)", (row['item_id'], price))
                        conn2.commit(); conn2.close()
                        checked += 1
                except: pass
            time.sleep(random.uniform(3,6))
        except: pass
    return f"""<!DOCTYPE html><html><head><title>Done</title>{BASE_CSS}</head><body>
    {nav_html('admin')}
    <div class="wrap" style="padding-top:40px;">
      <div class="alert alert-green">Price check সম্পন্ন! {checked}/{len(items)} products আপডেট হয়েছে।</div>
      <a href="/admin" class="btn btn-main">← Admin এ ফিরুন</a>
    </div></body></html>"""

# ══════════════════════════════════════════
#  CRON JOB ROUTE (cron-job.org এর জন্য)
# ══════════════════════════════════════════
@app.route('/run-price-check')
def run_price_check():
    secret = request.args.get('secret','')
    if secret != CRON_SECRET:
        return 'Unauthorized', 401

    import threading
    def do_check():
        try:
            conn = get_db(); c = conn.cursor()
            c.execute("SELECT DISTINCT item_id, url FROM tracked")
            items = c.fetchall(); conn.close()
            print(f"Cron: checking {len(items)} products...")
            for row in items:
                try:
                    resp = daraz_session.get(row['url'],
                        headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=15)
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(resp.text, 'html.parser')
                    price_el = soup.select_one('.pdp-price strong, .pdp-price, [class*="price"]')
                    if price_el:
                        txt = price_el.get_text(strip=True).replace('৳','').replace(',','').strip()
                        try:
                            price = float(txt.split()[0])
                            if price > 0:
                                conn2 = get_db(); c2 = conn2.cursor()
                                c2.execute("INSERT INTO price_history (item_id,price) VALUES (%s,%s)",
                                           (row['item_id'], price))
                                conn2.commit(); conn2.close()
                                print(f"  {row['item_id']} → ৳{price:.0f}")
                        except: pass
                    time.sleep(random.uniform(3,7))
                except Exception as e:
                    print(f"  Error: {e}")
        except Exception as e:
            print(f"Cron error: {e}")

    threading.Thread(target=do_check, daemon=True).start()
    return jsonify({'ok': True, 'message': 'Price check started'})

# ══════════════════════════════════════════
#  SEARCH
# ══════════════════════════════════════════
@app.route('/search')
def search():
    keyword = request.args.get('q','').strip()
    if not keyword: return jsonify({'error':'keyword required'}), 400

    pages = int(request.args.get('pages', 3) or 3)
    filters = {
        'min_price':  float(request.args.get('min_price',  0) or 0),
        'max_price':  float(request.args.get('max_price',  0) or 0),
        'min_rating': float(request.args.get('min_rating', 0) or 0),
        'min_sold':   int(  request.args.get('min_sold',   0) or 0),
    }

    args_list = [(keyword, p, filters) for p in range(1, pages+1)]
    raw, total_scanned = [], 0

    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(fetch_page, a): a for a in args_list}
        for f in as_completed(futures):
            res, scanned = f.result()
            raw.extend(res); total_scanned += scanned

    total_filtered = len(raw)
    products       = dedup(raw)
    dupes          = total_filtered - len(products)

    try:
        conn = get_db(); c = conn.cursor()
        c.execute("INSERT INTO search_logs (user_id,keyword,results) VALUES (%s,%s,%s)",
                  (flask_session.get('user_id'), keyword, len(products)))
        conn.commit(); conn.close()
    except: pass

    return jsonify({
        'keyword': keyword,
        'total_scanned': total_scanned,
        'total_filtered': total_filtered,
        'duplicates_removed': dupes,
        'total': len(products),
        'products': products,
    })

# ══════════════════════════════════════════
#  MAIN PAGE
# ══════════════════════════════════════════
@app.route('/')
def index():
    is_login = 'true' if logged_in() else 'false'
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daraz Finder</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet">
{BASE_CSS}
<style>
body::before{{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background:radial-gradient(ellipse 70% 40% at 5% 0%,rgba(249,115,22,.06) 0%,transparent 55%),
             radial-gradient(ellipse 50% 35% at 95% 100%,rgba(59,130,246,.05) 0%,transparent 50%);}}
.wrap{{max-width:1400px;margin:0 auto;padding:0 18px;position:relative;z-index:1;}}
.live-badge{{display:flex;align-items:center;gap:6px;background:rgba(16,185,129,.1);border:1px solid rgba(16,185,129,.2);color:var(--green);font-size:.7rem;font-weight:600;padding:5px 12px;border-radius:50px;letter-spacing:.08em;text-transform:uppercase;}}
.live-dot{{width:6px;height:6px;border-radius:50%;background:var(--green);animation:blink 1.4s ease infinite;}}
@keyframes blink{{0%,100%{{opacity:1;}}50%{{opacity:.3;}}}}
.hero{{padding:36px 18px 24px;}}
.hero-tag{{display:inline-flex;align-items:center;gap:7px;background:rgba(249,115,22,.08);border:1px solid rgba(249,115,22,.2);color:var(--orange);font-size:.72rem;font-weight:700;letter-spacing:.1em;padding:5px 13px;border-radius:50px;text-transform:uppercase;margin-bottom:16px;}}
.hero h1{{font-family:'Bebas Neue',sans-serif;font-size:clamp(2.4rem,6vw,4.5rem);line-height:1;letter-spacing:1px;margin-bottom:10px;}}
.hero h1 .hl{{background:linear-gradient(90deg,var(--orange),var(--orange2));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;}}
.hero p{{color:var(--muted2);font-size:.95rem;max-width:500px;}}
.panel{{background:var(--bg2);border:1px solid var(--border);border-radius:16px;padding:22px;margin-bottom:18px;}}
.search-row{{display:flex;gap:10px;margin-bottom:14px;}}
.search-input{{flex:1;background:var(--bg3);border:1.5px solid var(--border2);border-radius:10px;padding:14px 18px;color:var(--text);font-size:1rem;font-family:'DM Sans',sans-serif;outline:none;transition:border-color .2s,box-shadow .2s;}}
.search-input:focus{{border-color:var(--orange);box-shadow:0 0 0 3px rgba(249,115,22,.1);}}
.search-input::placeholder{{color:var(--muted);}}
.btn-search{{background:linear-gradient(135deg,var(--orange),var(--orange2));color:#fff;border:none;border-radius:10px;padding:14px 26px;font-size:.95rem;font-weight:700;font-family:'DM Sans',sans-serif;cursor:pointer;white-space:nowrap;transition:opacity .2s;box-shadow:0 4px 18px rgba(249,115,22,.3);}}
.btn-search:hover{{opacity:.9;}}
.btn-search:disabled{{opacity:.35;cursor:not-allowed;}}
.quick-filters{{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:14px;}}
.qf{{background:var(--bg3);border:1.5px solid var(--border2);border-radius:50px;padding:5px 13px;font-size:.73rem;font-weight:600;cursor:pointer;color:var(--muted2);transition:all .18s;}}
.qf:hover,.qf.active{{border-color:var(--orange);color:var(--orange);background:rgba(249,115,22,.08);}}
.filters-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px;}}
.fg{{display:flex;flex-direction:column;gap:6px;}}
.fg label{{font-size:.62rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;}}
.fg input,.fg select{{background:var(--bg3);border:1.5px solid var(--border2);border-radius:9px;padding:10px 12px;color:var(--text);font-size:.88rem;font-family:'DM Sans',sans-serif;outline:none;transition:border-color .2s;}}
.fg input:focus,.fg select:focus{{border-color:var(--orange);}}
.stats-bar{{background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:14px 18px;margin-bottom:14px;display:none;}}
.stats-bar.on{{display:block;}}
.stats-inner{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;}}
.stat-chip{{display:flex;align-items:center;gap:8px;background:var(--bg3);border-radius:8px;padding:8px 14px;font-size:.82rem;}}
.stat-chip .n{{font-weight:700;font-size:.95rem;}}
.n-blue{{color:#60A5FA;}}.n-orange{{color:var(--orange2);}}.n-red{{color:#F87171;}}.n-green{{color:#34D399;}}
.arr{{color:var(--muted);}}
.results-hdr{{display:none;align-items:center;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:10px;}}
.results-hdr.on{{display:flex;}}
.res-count{{font-size:.85rem;color:var(--muted2);}}
.res-count strong{{color:var(--orange);font-size:1rem;font-weight:700;}}
.sort-sel{{background:var(--bg2);border:1px solid var(--border2);border-radius:9px;padding:9px 13px;color:var(--text);font-family:'DM Sans',sans-serif;font-size:.83rem;outline:none;cursor:pointer;}}
.status{{margin-bottom:14px;display:none;}}
.status.on{{display:block;}}
.status-pill{{display:inline-flex;align-items:center;gap:9px;background:var(--bg2);border:1px solid var(--border);border-radius:50px;padding:9px 18px;font-size:.82rem;color:var(--muted2);}}
.spin{{width:14px;height:14px;border:2px solid var(--border2);border-top-color:var(--orange);border-radius:50%;animation:spin .6s linear infinite;}}
@keyframes spin{{to{{transform:rotate(360deg);}}}}
.err-box{{background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.2);border-radius:10px;padding:13px 16px;color:#FCA5A5;font-size:.88rem;margin-bottom:14px;display:none;}}
.err-box.on{{display:block;}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(235px,1fr));gap:16px;margin-bottom:60px;}}
.pcard{{background:var(--bg2);border:1px solid var(--border);border-radius:16px;overflow:hidden;display:flex;flex-direction:column;transition:transform .22s cubic-bezier(.34,1.56,.64,1),border-color .2s,box-shadow .2s;animation:fadeUp .38s ease both;}}
.pcard:hover{{transform:translateY(-5px);border-color:var(--orange);box-shadow:0 12px 36px rgba(249,115,22,.12);}}
@keyframes fadeUp{{from{{opacity:0;transform:translateY(18px);}}to{{opacity:1;transform:translateY(0);}}}}
.pcard-img{{position:relative;background:var(--bg3);height:190px;overflow:hidden;flex-shrink:0;}}
.pcard-img img{{width:100%;height:100%;object-fit:contain;transition:transform .3s;padding:10px;}}
.pcard:hover .pcard-img img{{transform:scale(1.06);}}
.badge-disc{{position:absolute;top:10px;left:10px;background:linear-gradient(135deg,var(--orange),var(--orange2));color:#fff;font-size:.62rem;font-weight:800;padding:3px 8px;border-radius:6px;}}
.rating-pill{{position:absolute;bottom:10px;right:10px;background:rgba(0,0,0,.75);color:var(--orange2);font-size:.72rem;font-weight:700;padding:4px 9px;border-radius:6px;}}
.deal-badge-c{{position:absolute;bottom:10px;left:10px;font-size:.62rem;font-weight:800;padding:3px 8px;border-radius:6px;}}
.deal-hot{{background:rgba(239,68,68,.85);color:#fff;}}
.deal-good{{background:rgba(16,185,129,.85);color:#fff;}}
.deal-ok{{background:rgba(107,114,128,.75);color:#fff;}}
.pcard-body{{padding:13px;flex:1;display:flex;flex-direction:column;gap:8px;}}
.pcard-name{{font-size:.82rem;line-height:1.5;color:var(--text);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:2.5em;}}
.price-now{{font-size:1.2rem;font-weight:700;color:var(--orange);}}
.orig-price{{font-size:.8rem;color:var(--muted);text-decoration:line-through;}}
.price-na{{font-size:.85rem;color:var(--muted);font-style:italic;}}
.deal-row{{display:flex;align-items:center;gap:8px;}}
.deal-bar-bg{{flex:1;height:5px;background:var(--border2);border-radius:3px;overflow:hidden;}}
.deal-bar-fill{{height:100%;border-radius:3px;}}
.ss-grid{{display:grid;grid-template-columns:1fr 1fr;gap:5px;}}
.ss{{background:var(--bg3);border-radius:7px;padding:6px 9px;}}
.ss-label{{font-size:.57rem;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;font-weight:700;}}
.ss-val{{font-size:.8rem;font-weight:600;}}
.sv-green{{color:#34D399;}}.sv-blue{{color:#60A5FA;}}.sv-orange{{color:var(--orange2);}}
.pcard-foot{{display:flex;gap:5px;margin-top:auto;}}
.btn-view{{flex:1;background:transparent;border:1.5px solid var(--orange);color:var(--orange);border-radius:8px;padding:9px;font-size:.75rem;font-weight:700;text-align:center;transition:background .18s,color .18s;cursor:pointer;display:block;text-decoration:none;}}
.btn-view:hover{{background:var(--orange);color:#fff;}}
.icon-btn{{width:36px;height:36px;border:1.5px solid var(--border2);border-radius:8px;background:var(--bg3);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:.85rem;transition:all .18s;flex-shrink:0;}}
.icon-btn:hover{{border-color:var(--orange);}}
.icon-btn.tracked{{border-color:var(--green);background:rgba(16,185,129,.1);}}
.empty{{text-align:center;padding:5rem 1rem;display:none;}}
.empty.on{{display:block;}}
.hist-bar{{display:flex;flex-wrap:wrap;gap:7px;margin-top:10px;}}
.hist-chip{{display:flex;align-items:center;gap:6px;background:var(--bg3);border:1px solid var(--border2);border-radius:50px;padding:4px 12px;font-size:.72rem;color:var(--muted2);cursor:pointer;transition:all .18s;}}
.hist-chip:hover{{border-color:var(--orange);color:var(--orange);}}
.load-more{{text-align:center;margin:10px 0 40px;}}
.btn-load{{background:var(--bg2);border:1.5px solid var(--border2);border-radius:12px;padding:13px 36px;font-size:.9rem;font-weight:600;color:var(--muted2);cursor:pointer;transition:all .2s;}}
.btn-load:hover{{border-color:var(--orange);color:var(--orange);}}
@media(max-width:560px){{
  .search-row{{flex-direction:column;}}
  .grid{{grid-template-columns:repeat(2,1fr);}}
  .pcard-img{{height:145px;}}
  .hero h1{{font-size:2.4rem;}}
}}
</style>
</head><body>
{nav_html()}
<div class="wrap">
<div class="hero">
  <div class="hero-tag">🛒 Daraz Smart Search + Price Tracker</div>
  <h1>খুঁজুন <span class="hl">সেরা Deal</span><br>Daraz এ</h1>
  <p>বাংলাদেশের Daraz এ product search করুন, price track করুন, graph দেখুন।</p>
</div>
<div class="panel">
  <div class="search-row">
    <input type="text" id="q" class="search-input" placeholder="e.g. মোবাইল, জামা, জুতা, খেলনা..." autocomplete="off">
    <button class="btn-search" id="sbtn" onclick="doSearch()">🔍 Search</button>
  </div>
  <div class="quick-filters">
    <span class="qf" onclick="setQ('under300')">৳300 এর নিচে</span>
    <span class="qf" onclick="setQ('under500')">৳500 এর নিচে</span>
    <span class="qf" onclick="setQ('under1000')">৳1000 এর নিচে</span>
    <span class="qf" onclick="setQ('rating45')">4.5+ Rating</span>
    <span class="qf" onclick="setQ('topsold')">100+ বিক্রি</span>
    <span class="qf" onclick="setQ('clear')">Clear</span>
  </div>
  <div class="filters-grid">
    <div class="fg"><label>Min Price (৳)</label><input type="number" id="minP" placeholder="0" min="0"></div>
    <div class="fg"><label>Max Price (৳)</label><input type="number" id="maxP" placeholder="যেকোনো" min="0"></div>
    <div class="fg"><label>Min Rating ⭐</label><input type="number" id="minR" placeholder="4.0" step="0.1" min="0" max="5"></div>
    <div class="fg"><label>Min Sold</label><input type="number" id="minSold" placeholder="100" min="0"></div>
    <div class="fg"><label>Pages to Scan</label><input type="number" id="pages" value="3" min="1"></div>
    <div class="fg">
      <label>Sort By</label>
      <select id="sortSel" onchange="render()">
        <option value="deal">Best Deal Score</option>
        <option value="price_asc">দাম: কম → বেশি</option>
        <option value="price_desc">দাম: বেশি → কম</option>
        <option value="rating">সর্বোচ্চ Rating</option>
        <option value="sold">সবচেয়ে বেশি বিক্রি</option>
        <option value="discount">সর্বোচ্চ Discount</option>
      </select>
    </div>
  </div>
  <div class="hist-bar" id="histBar"></div>
</div>
<div class="status" id="stat">
  <div class="status-pill"><div class="spin"></div><span>Daraz scan করছি...</span></div>
</div>
<div class="stats-bar" id="statsBar">
  <div class="stats-inner">
    <div class="stat-chip">🔍 Scanned: <span class="n n-blue" id="s-scan">0</span></div>
    <span class="arr">→</span>
    <div class="stat-chip">🎯 Passed: <span class="n n-orange" id="s-pass">0</span></div>
    <span class="arr">→</span>
    <div class="stat-chip">🚫 Dupes: <span class="n n-red" id="s-dupe">0</span></div>
    <span class="arr">→</span>
    <div class="stat-chip">🏆 Final: <span class="n n-green" id="s-final">0</span></div>
  </div>
</div>
<div class="results-hdr" id="resHdr">
  <div class="res-count">Showing <strong id="resCount">0</strong> products</div>
  <select class="sort-sel" onchange="document.getElementById('sortSel').value=this.value;render()">
    <option value="deal">Best Deal Score</option>
    <option value="price_asc">দাম: কম → বেশি</option>
    <option value="price_desc">দাম: বেশি → কম</option>
    <option value="rating">সর্বোচ্চ Rating</option>
    <option value="sold">সবচেয়ে বেশি বিক্রি</option>
    <option value="discount">সর্বোচ্চ Discount</option>
  </select>
</div>
<div class="err-box" id="err"></div>
<div class="grid" id="grid"></div>
<div class="load-more" id="loadMore" style="display:none;">
  <button class="btn-load" onclick="loadMore()">আরো দেখুন ↓</button>
</div>
<div class="empty" id="empty">
  <div style="font-size:3.5rem;margin-bottom:1rem;opacity:.5;">🔍</div>
  <h3 style="font-family:'Bebas Neue',sans-serif;font-size:1.8rem;letter-spacing:1px;margin-bottom:.5rem;">কোনো Product পাওয়া যায়নি</h3>
  <p style="color:var(--muted2);">অন্য keyword বা filter দিয়ে চেষ্টা করুন।</p>
</div>
</div>
<script>
const IS_LOGGED={is_login};
let all=[],displayed=0,PAGE=20,trackedSet=new Set();
const $=id=>document.getElementById(id);

async function loadTracked(){{
  if(!IS_LOGGED) return;
  const r=await fetch('/api/tracked-list');
  const d=await r.json();
  trackedSet=new Set(d.items);
}}
loadTracked();

let hist=JSON.parse(localStorage.getItem('dzsh')||'[]');
function saveHist(q){{hist=[q,...hist.filter(x=>x!==q)].slice(0,7);localStorage.setItem('dzsh',JSON.stringify(hist));renderHist();}}
function renderHist(){{
  const b=$('histBar');
  if(!hist.length){{b.innerHTML='';return;}}
  b.innerHTML='<span style="font-size:.65rem;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.08em;margin-right:4px;">Recent:</span>'
    +hist.map((h,i)=>`<span class="hist-chip" onclick="useHist('${{h.replace(/'/g,"\\'")}}')">🕐 ${{h}} <span onclick="event.stopPropagation();delHist(${{i}})" style="margin-left:4px;color:var(--muted);">✕</span></span>`).join('');
}}
function useHist(q){{$('q').value=q;doSearch();}}
function delHist(i){{hist.splice(i,1);localStorage.setItem('dzsh',JSON.stringify(hist));renderHist();}}
renderHist();

function setQ(t){{
  document.querySelectorAll('.qf').forEach(e=>e.classList.remove('active'));
  if(t==='clear'){{$('minP').value='';$('maxP').value='';$('minR').value='';$('minSold').value='';return;}}
  event.target.classList.add('active');
  if(t==='under300')$('maxP').value=300;
  else if(t==='under500')$('maxP').value=500;
  else if(t==='under1000')$('maxP').value=1000;
  else if(t==='rating45')$('minR').value=4.5;
  else if(t==='topsold')$('minSold').value=100;
}}

async function trackProduct(item_id,btn){{
  if(!IS_LOGGED){{
    if(confirm('Price track করতে login করতে হবে। Login page এ যাবেন?')) window.location.href='/login';
    return;
  }}
  const p=all.find(x=>x.item_id===item_id);
  if(!p) return;
  if(trackedSet.has(item_id)){{window.location.href='/price-graph/'+item_id;return;}}
  fetch('/api/log-click',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{item_id:p.item_id,name:p.name,price:p.price}})}});
  const r=await fetch('/api/track',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{item_id:p.item_id,name:p.name,image:p.image,url:p.url,price:p.price}})}});
  const d=await r.json();
  if(d.tracked){{trackedSet.add(item_id);btn.textContent='📈';btn.classList.add('tracked');btn.title='Price Graph দেখুন';}}
}}

function scoreColor(s){{return s>=75?'#10B981':s>=50?'#F97316':'#6B7280';}}
function dealClass(s){{return s>=75?'deal-hot':s>=50?'deal-good':'deal-ok';}}
function dealLabel(s){{return s>=75?'🔥 Best Deal':s>=50?'👍 Good Deal':'• Fair';}}

function cardHTML(p,i){{
  const isT=trackedSet.has(p.item_id);
  const hasDisc=p.orig_price>0&&p.orig_price>p.price;
  return `
  <div class="pcard" style="animation-delay:${{Math.min(i*.03,.6)}}s">
    <div class="pcard-img">
      <img src="${{p.image}}" onerror="this.src='https://placehold.co/235x190/15151C/6B6B8A?text=No+Image'" loading="lazy" alt="${{p.name}}">
      ${{p.discount?`<div class="badge-disc">${{p.discount}}</div>`:''}}
      <div class="rating-pill">⭐ ${{p.rating.toFixed(1)}}</div>
      <div class="deal-badge-c ${{dealClass(p.deal_score)}}">${{dealLabel(p.deal_score)}}</div>
    </div>
    <div class="pcard-body">
      <div class="pcard-name" title="${{p.name}}">${{p.name}}</div>
      <div style="display:flex;align-items:baseline;gap:8px;">
        ${{p.price>0?`<span class="price-now">৳${{p.price.toFixed(0)}}</span>`:'<span class="price-na">দাম নেই</span>'}}
        ${{hasDisc?`<span class="orig-price">৳${{p.orig_price.toFixed(0)}}</span>`:''}}
      </div>
      <div class="deal-row">
        <span style="font-size:.68rem;color:var(--muted2);white-space:nowrap;">Deal Score</span>
        <div class="deal-bar-bg"><div class="deal-bar-fill" style="width:${{p.deal_score}}%;background:${{scoreColor(p.deal_score)}}"></div></div>
        <span style="font-size:.7rem;font-weight:700;color:var(--orange2);min-width:26px;text-align:right;">${{p.deal_score}}</span>
      </div>
      <div class="ss-grid">
        <div class="ss"><div class="ss-label">বিক্রি</div><div class="ss-val sv-green">🛒 ${{p.sold_str||p.sold}}</div></div>
        <div class="ss"><div class="ss-label">Reviews</div><div class="ss-val sv-blue">📝 ${{p.reviews.toLocaleString()}}</div></div>
      </div>
      <div class="pcard-foot">
        <a href="${{p.url}}" target="_blank" class="btn-view" onclick="logClick('${{p.item_id}}','${{p.name.replace(/'/g,"\\'")}}')">Daraz এ দেখুন →</a>
        <button class="icon-btn ${{isT?'tracked':''}}" title="${{isT?'Price Graph দেখুন':'Price Track করুন'}}"
          onclick="trackProduct('${{p.item_id}}',this)">${{isT?'📈':'📌'}}</button>
      </div>
    </div>
  </div>`;
}}

function logClick(item_id,name){{
  const p=all.find(x=>x.item_id===item_id);
  if(p) fetch('/api/log-click',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{item_id,name,price:p.price}})}});
}}

function getSorted(){{
  const s=$('sortSel').value;let list=[...all];
  if(s==='deal')list.sort((a,b)=>b.deal_score-a.deal_score);
  else if(s==='price_asc')list.sort((a,b)=>(a.price||99999)-(b.price||99999));
  else if(s==='price_desc')list.sort((a,b)=>(b.price||0)-(a.price||0));
  else if(s==='rating')list.sort((a,b)=>b.rating-a.rating);
  else if(s==='sold')list.sort((a,b)=>b.sold-a.sold);
  else if(s==='discount')list.sort((a,b)=>((b.orig_price-b.price)||0)-((a.orig_price-a.price)||0));
  return list;
}}

function render(){{
  const list=getSorted();
  $('resCount').textContent=list.length.toLocaleString();
  const g=$('grid'),em=$('empty');
  if(!list.length){{g.innerHTML='';em.classList.add('on');$('loadMore').style.display='none';return;}}
  em.classList.remove('on');
  const chunk=list.slice(0,PAGE);displayed=chunk.length;
  g.innerHTML=chunk.map((p,i)=>cardHTML(p,i)).join('');
  $('loadMore').style.display=list.length>PAGE?'block':'none';
}}

function loadMore(){{
  const list=getSorted();
  const chunk=list.slice(displayed,displayed+PAGE);
  chunk.forEach((p,i)=>$('grid').insertAdjacentHTML('beforeend',cardHTML(p,displayed+i)));
  displayed+=chunk.length;
  if(displayed>=list.length)$('loadMore').style.display='none';
}}

async function doSearch(){{
  const q=$('q').value.trim();
  if(!q){{showErr('একটি keyword লিখুন!');return;}}
  const params=new URLSearchParams({{q,pages:$('pages').value||3,
    min_price:$('minP').value||0,max_price:$('maxP').value||0,
    min_rating:$('minR').value||0,min_sold:$('minSold').value||0}});
  setLoad(true);hideErr();
  try{{
    const res=await fetch('/search?'+params);
    if(!res.ok) throw new Error();
    const d=await res.json();
    all=d.products||[];displayed=0;
    saveHist(q);
    await loadTracked();
    $('s-scan').textContent=d.total_scanned.toLocaleString();
    $('s-pass').textContent=d.total_filtered.toLocaleString();
    $('s-dupe').textContent=d.duplicates_removed.toLocaleString();
    $('s-final').textContent=d.total.toLocaleString();
    $('statsBar').classList.add('on');
    $('resHdr').classList.add('on');
    render();
  }}catch(e){{showErr('Connection failed.');}}
  finally{{setLoad(false);}}
}}

function setLoad(on){{$('sbtn').disabled=on;$('sbtn').textContent=on?'⏳ Loading...':'🔍 Search';$('stat').className='status'+(on?' on':'');}}
function showErr(m){{const e=$('err');e.textContent=m;e.className='err-box on';}}
function hideErr(){{$('err').className='err-box';}}
$('q').addEventListener('keydown',e=>{{if(e.key==='Enter')doSearch();}});
</script>
</body></html>"""

@app.route('/ping')
def ping():
    return 'pong', 200

if __name__ == '__main__':
    print("="*50)
    print("  Daraz Finder — Full Edition")
    print("="*50)
    print("  http://127.0.0.1:5000")
    print(f"  Admin: {ADMIN_USER} / {ADMIN_PASS}")
    print("="*50)
    app.run(debug=False, host='0.0.0.0', port=5000)
