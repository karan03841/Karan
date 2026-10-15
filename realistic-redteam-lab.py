#!/usr/bin/env python3
"""
Karan's Realistic Red-Team Lab (v6)
=====================================
"KaranCorp Shop" -- full colorful multi-page e-commerce-style site,
PortSwigger/Juice-Shop-ish look, with real bug-bounty-style friction
built in: several honeypots that only bite if you fuzz blindly instead
of following the real recon trail, a tightened rate limiter with
jittered block times, and a math CAPTCHA on the real admin console
that your tooling has to actually solve rather than guess around.
Koi walkthrough/hint nahi hai jaan-bujh kar. CHAAR independent attack
paths hain -- khud dhoondho:

  1. Direct credential attack on a hidden admin console (UA filter +
     rate limit + CAPTCHA all have to be handled, not just brute-forced)
  2. Session-token forging (weak "remember me" cookie)
  3. Password-reset-token forging (predictable reset link)
  4. IDOR on order history (order detail view trusts the ID, not the owner)

RUN KARNE KA TARIKA:
    pip3 install flask --break-system-packages
    python3 realistic-redteam-lab.py

Setup hote hi "admin_secret_DONT_PEEK.txt" banegi -- mat kholna jab tak
hack na kar lo (ya khud verify karna ho).

ngrok se public karo (real IP-ban/anonymity practice ke liye zaroori hai):
    ngrok http 5000
"""

import base64
import datetime
import hashlib
import random
import sqlite3
import string
import time
import os

from flask import Flask, request, g, redirect, session, jsonify, make_response

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()

DB = "redteam_lab.db"
SECRET_FILE = "admin_secret_DONT_PEEK.txt"

WEAK_PASSWORDS = ["Dragon123", "Welcome1", "Password1", "Sunshine1", "Football1", "Monkey123"]
ADMIN_USERNAMES = ["opsadmin", "sysadmin", "webadmin", "netadmin"]

DECOY_PATH = "/admin"
REAL_ADMIN_PATH = "/ops-" + "".join(random.choices(string.hexdigits.lower(), k=6)) + "-console"

# a few more common admin-panel guesses -- these are NOT advertised anywhere
# (not in robots.txt, not in any comment); they only trip if you blindly
# fuzz a generic wordlist instead of following the real recon leads
EXTRA_HONEYPOT_PATHS = ["/wp-admin", "/administrator", "/cpanel", "/manage"]

BLOCKED_UA_SUBSTRINGS = ["python-requests", "curl", "hydra", "ffuf", "go-http-client", "wget"]

BANNED_IPS = {}
RATE_ATTEMPTS = {}
RATE_BLOCKED_UNTIL = {}

HONEYPOT_BAN_SECONDS = 150
RATE_WINDOW = 8
RATE_MAX = 2

PRODUCTS = [
    {"id": 1, "emoji": "🎧", "name": "Pulse Wireless Headphones", "price": 59.99, "rating": 4,
     "tag": "Bestseller", "color": "#ff5722",
     "desc": "Noise-cancelling over-ear headphones with 30h battery life."},
    {"id": 2, "emoji": "⌚", "name": "Orbit Smartwatch", "price": 89.00, "rating": 5,
     "tag": "New", "color": "#6c3ce9",
     "desc": "Track workouts, sleep, and notifications from your wrist."},
    {"id": 3, "emoji": "🎒", "name": "Trailblazer Backpack", "price": 34.50, "rating": 4,
     "tag": "Sale", "color": "#00bfa5",
     "desc": "Weatherproof 25L backpack with a padded laptop sleeve."},
    {"id": 4, "emoji": "🔌", "name": "ChargeFast 65W GaN Charger", "price": 24.99, "rating": 4,
     "tag": "", "color": "#ffb300",
     "desc": "Compact fast charger for laptops and phones alike."},
    {"id": 5, "emoji": "🖱️", "name": "GlideMouse Pro", "price": 19.99, "rating": 3,
     "tag": "", "color": "#2d5af7",
     "desc": "Silent-click wireless mouse with a 2-year battery life."},
    {"id": 6, "emoji": "📷", "name": "SnapShot Mini Camera", "price": 129.00, "rating": 5,
     "tag": "Bestseller", "color": "#e91e8c",
     "desc": "Pocket-sized camera with 4K video and image stabilization."},
    {"id": 7, "emoji": "🧴", "name": "HydraFlask 1L Bottle", "price": 14.00, "rating": 4,
     "tag": "Sale", "color": "#00acc1",
     "desc": "Keeps drinks cold for 24h, hot for 12h. Leakproof lid."},
    {"id": 8, "emoji": "💡", "name": "Glow Smart Desk Lamp", "price": 27.50, "rating": 4,
     "tag": "New", "color": "#8bc34a",
     "desc": "App-controlled lamp with tunable color temperature."},
    {"id": 9, "emoji": "⌨️", "name": "ClickType Mechanical Keyboard", "price": 74.99, "rating": 5,
     "tag": "Bestseller", "color": "#3f51b5",
     "desc": "Hot-swappable switches, per-key RGB, aluminum frame."},
    {"id": 10, "emoji": "🖥️", "name": "ViewPoint 27\" Monitor", "price": 189.00, "rating": 4,
     "tag": "", "color": "#009688",
     "desc": "QHD IPS panel, 100Hz refresh, height-adjustable stand."},
    {"id": 11, "emoji": "🎮", "name": "DriftPad Game Controller", "price": 39.00, "rating": 4,
     "tag": "Sale", "color": "#d500f9",
     "desc": "Bluetooth controller with swappable thumbsticks."},
    {"id": 12, "emoji": "🧢", "name": "KaranCorp Logo Cap", "price": 12.99, "rating": 3,
     "tag": "", "color": "#ff7043",
     "desc": "Adjustable cotton cap with embroidered logo."},
    {"id": 13, "emoji": "🧘", "name": "ZenMat Yoga Mat", "price": 21.00, "rating": 4,
     "tag": "New", "color": "#43a047",
     "desc": "Extra-thick non-slip mat with carry strap."},
    {"id": 14, "emoji": "🎒", "name": "CommuteLite Laptop Sleeve", "price": 17.50, "rating": 4,
     "tag": "", "color": "#5c6bc0",
     "desc": "Slim shock-absorbing sleeve, fits up to 15.6\" laptops."},
    {"id": 15, "emoji": "📶", "name": "NetBoost WiFi Extender", "price": 29.99, "rating": 3,
     "tag": "Sale", "color": "#26a69a",
     "desc": "Dual-band repeater, covers up to 1,200 sq ft."},
    {"id": 16, "emoji": "🔋", "name": "PowerBrick 20000mAh", "price": 32.00, "rating": 5,
     "tag": "Bestseller", "color": "#fb8c00",
     "desc": "Fast-charge power bank with USB-C PD and 2 USB-A ports."},
    {"id": 17, "emoji": "🎙️", "name": "StreamVoice USB Mic", "price": 54.00, "rating": 4,
     "tag": "New", "color": "#ec407a",
     "desc": "Cardioid USB condenser mic for calls and streaming."},
    {"id": 18, "emoji": "🪑", "name": "ErgoFlex Office Chair", "price": 219.00, "rating": 5,
     "tag": "", "color": "#7cb342",
     "desc": "Breathable mesh back, adjustable lumbar support and armrests."},
]


# ---------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------
def setup_db_and_secret():
    first_run = not os.path.exists(DB)
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS users
                     (username TEXT PRIMARY KEY, password TEXT, is_admin INTEGER)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS orders
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      username TEXT, items TEXT, total REAL,
                      address TEXT, card_last4 TEXT, created TEXT)""")

    if first_run or not os.path.exists(SECRET_FILE):
        admin_user = random.choice(ADMIN_USERNAMES)
        admin_pass = random.choice(WEAK_PASSWORDS)
        conn.execute("INSERT OR REPLACE INTO users VALUES (?, ?, 1)", (admin_user, admin_pass))

        # a couple of other "real" accounts referenced elsewhere on the site
        # (blog byline, support replies) so the shop doesn't feel empty
        conn.execute("INSERT OR IGNORE INTO users VALUES (?, ?, 0)",
                      ("maria_ops", "".join(random.choices(string.ascii_letters + string.digits, k=12))))
        conn.execute("INSERT OR IGNORE INTO users VALUES (?, ?, 0)",
                      ("dev_sam", "".join(random.choices(string.ascii_letters + string.digits, k=12))))

        # seed order history so the order-lookup page has more than one row in it
        conn.execute("INSERT INTO orders (username, items, total, address, card_last4, created) VALUES (?,?,?,?,?,?)",
                      ("maria_ops", "Orbit Smartwatch x1, HydraFlask 1L Bottle x2", 117.00,
                       "14 Birchwood Lane, Leeds, LS6 2QT", "4417", "2026-09-02"))
        conn.execute("INSERT INTO orders (username, items, total, address, card_last4, created) VALUES (?,?,?,?,?,?)",
                      ("dev_sam", "GlideMouse Pro x1", 19.99,
                       "9 Harbor View Rd, Bristol, BS1 4ST", "2290", "2026-09-10"))
        conn.execute("INSERT INTO orders (username, items, total, address, card_last4, created) VALUES (?,?,?,?,?,?)",
                      (admin_user, "SnapShot Mini Camera x1, Glow Smart Desk Lamp x1", 156.50,
                       "Ops Dept, KaranCorp HQ, 2 Dock Street, London, E1 8JN", "7765", "2026-09-14"))

        conn.commit()
        with open(SECRET_FILE, "w") as f:
            f.write(f"Admin username: {admin_user}\n")
            f.write(f"Admin password: {admin_pass}\n")
            f.write(f"Real admin panel path: {REAL_ADMIN_PATH}\n")
            f.write(f"Decoy (honeypot) paths: {DECOY_PATH}, {', '.join(EXTRA_HONEYPOT_PATHS)}  -- DO NOT attack these!\n")
            f.write(f"Real admin console now also requires solving a math CAPTCHA (session-bound, shown on GET)\n")
            f.write(f"Session-token scheme: base64(username + ':' + md5(username)[:6])\n")
            f.write(f"  -> forge cookie 'auth_token' with this value, then GET /autologin\n")
            f.write(f"Reset-token scheme: md5(username + YYYYMMDD)[:8]\n")
            f.write(f"  -> GET /reset-password?user=<username>&token=<computed> to set a new password\n")
            f.write(f"Order IDs 1-3 are pre-seeded (maria_ops, dev_sam, {admin_user})\n")
            f.write(f"  -> /order/<id> does not check ownership -- any logged-in user can view any order (IDOR)\n")
        os.chmod(SECRET_FILE, 0o600)
        print(f"[setup] Admin account created. Sealed in {SECRET_FILE} -- don't open it yet!")
    conn.commit()
    conn.close()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr)


@app.before_request
def check_ban():
    ip = client_ip()
    if ip in BANNED_IPS:
        if time.time() < BANNED_IPS[ip]:
            remaining = round(BANNED_IPS[ip] - time.time())
            return jsonify({"error": f"Your IP has been blocked for suspicious activity. "
                                      f"Retry in {remaining}s (or change your source IP)."}), 403
        else:
            del BANNED_IPS[ip]


# ---------------------------------------------------------------------
# Shared page layout (navbar/footer/CSS) -- colorful, shop-style
# ---------------------------------------------------------------------
PAGE_SHELL = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - KaranCorp Shop</title>
<style>
  :root {{
    --brand:#ff5722; --brand2:#6c3ce9; --accent:#00bfa5;
    --bg:#f4f3fb; --text:#1b1f27; --muted:#6b7280; --card:#ffffff; --border:#e6e3f5;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif; background:var(--bg); color:var(--text); }}
  header {{ background:linear-gradient(135deg,var(--brand2),var(--brand)); padding:16px 32px;
            display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; }}
  header .logo {{ font-weight:800; font-size:1.4em; color:#fff; letter-spacing:.5px; }}
  header .logo span {{ color:#ffe08a; }}
  nav a {{ margin-left:18px; color:#fff; text-decoration:none; font-size:0.92em; font-weight:600; opacity:.92; }}
  nav a:hover {{ opacity:1; text-decoration:underline; }}
  main {{ max-width:1000px; margin:0 auto; padding:40px 24px; }}
  .hero {{ text-align:center; padding:30px 0 10px; }}
  .hero h1 {{ font-size:2.3em; margin-bottom:8px; background:linear-gradient(90deg,var(--brand2),var(--brand));
              -webkit-background-clip:text; background-clip:text; color:transparent; }}
  .hero p {{ color:var(--muted); font-size:1.1em; }}
  .cards {{ display:flex; gap:20px; margin-top:30px; flex-wrap:wrap; }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:14px; padding:22px; flex:1;
           min-width:220px; box-shadow:0 2px 10px rgba(108,60,233,0.06); }}
  .card h3 {{ margin-top:0; }}
  .shop-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:20px; margin-top:24px; }}
  .product {{ background:var(--card); border-radius:14px; overflow:hidden; border:1px solid var(--border);
              box-shadow:0 2px 10px rgba(0,0,0,0.05); display:flex; flex-direction:column; }}
  .product .thumb {{ font-size:3.2em; text-align:center; padding:28px 0; color:#fff; }}
  .product .body {{ padding:16px; flex:1; display:flex; flex-direction:column; }}
  .product h4 {{ margin:0 0 6px; font-size:1.02em; }}
  .product .price {{ font-weight:800; color:var(--brand); font-size:1.15em; margin:6px 0; }}
  .product .stars {{ color:#ffb300; font-size:0.95em; }}
  .product .tag {{ display:inline-block; font-size:0.7em; font-weight:700; color:#fff; padding:2px 8px;
                    border-radius:20px; margin-bottom:8px; width:fit-content; }}
  .product .desc {{ color:var(--muted); font-size:0.85em; flex:1; }}
  .btn {{ display:inline-block; margin-top:12px; padding:9px 14px; background:var(--brand2); color:#fff;
          border:none; border-radius:8px; font-size:0.9em; font-weight:700; text-decoration:none; text-align:center;
          cursor:pointer; }}
  .btn:hover {{ opacity:0.9; }}
  .btn-outline {{ background:transparent; border:2px solid var(--brand2); color:var(--brand2); }}
  .auth-wrap {{ max-width:480px; margin:10px auto 0; text-align:center; }}
  .auth-icon {{ font-size:2.6em; display:inline-flex; align-items:center; justify-content:center;
                width:72px; height:72px; border-radius:50%;
                background:linear-gradient(135deg,var(--brand2),var(--brand)); margin-bottom:14px; }}
  .auth-wrap h1 {{ font-size:1.8em; margin:0 0 6px; }}
  .auth-sub {{ color:var(--muted); font-size:0.97em; margin-bottom:6px; }}
  form.box {{ background:var(--card); border:1px solid var(--border); border-radius:18px; padding:40px 36px;
              max-width:480px; margin:18px auto 0; text-align:left;
              box-shadow:0 8px 28px rgba(108,60,233,0.10); }}
  form.box label {{ display:block; margin:18px 0 6px; font-size:0.92em; font-weight:600; color:var(--text); }}
  form.box label:first-of-type {{ margin-top:0; }}
  form.box input {{ width:100%; padding:13px 14px; border:1.5px solid var(--border); border-radius:9px;
                     font-size:1.02em; transition:border-color .15s; }}
  form.box input:focus {{ outline:none; border-color:var(--brand2); }}
  form.box button {{ margin-top:28px; width:100%; padding:14px; background:linear-gradient(135deg,var(--brand2),var(--brand));
                      color:#fff; border:none; border-radius:9px; font-size:1.05em; font-weight:800; cursor:pointer;
                      letter-spacing:.3px; box-shadow:0 4px 14px rgba(255,87,34,0.25); }}
  form.box button:hover {{ opacity:0.93; }}
  .auth-foot {{ text-align:center; margin-top:18px; font-size:0.92em; color:var(--muted); }}
  .auth-foot a {{ color:var(--brand2); font-weight:700; text-decoration:none; }}
  .auth-foot a:hover {{ text-decoration:underline; }}
  .msg {{ max-width:420px; margin:0 auto; padding:14px 18px; border-radius:10px; background:#fff3e9;
          border:1px solid #ffd3b0; }}
  .blog-post {{ background:var(--card); border:1px solid var(--border); border-radius:14px; padding:20px; margin-bottom:16px; }}
  .byline {{ color:var(--muted); font-size:0.85em; }}
  .badge {{ display:inline-block; padding:3px 10px; border-radius:20px; font-size:0.72em; font-weight:800; color:#fff; }}
  footer {{ text-align:center; padding:30px; color:var(--muted); font-size:0.85em; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border-radius:10px; overflow:hidden; }}
  th, td {{ padding:10px; border-bottom:1px solid var(--border); text-align:left; }}
  th {{ background:#f1edfc; color:var(--brand2); }}
  .stat-row {{ display:flex; gap:20px; flex-wrap:wrap; margin-top:24px; }}
  .stat {{ flex:1; min-width:140px; background:var(--card); border-radius:14px; padding:20px; text-align:center;
           border:1px solid var(--border); }}
  .stat .num {{ font-size:1.8em; font-weight:800; color:var(--brand2); }}
</style>
</head>
<body>
<header>
  <div class="logo">Karan<span>Corp</span> Shop</div>
  <nav>
    <a href="/">Home</a>
    <a href="/shop">Shop</a>
    <a href="/about">About</a>
    <a href="/pricing">Pricing</a>
    <a href="/blog">Blog</a>
    <a href="/testimonials">Reviews</a>
    <a href="/faq">FAQ</a>
    <a href="/contact">Contact</a>
    {extra_nav}
  </nav>
</header>
<main>
{body}
</main>
<footer>&copy; 2026 KaranCorp Inc. All rights reserved.</footer>
</body>
</html>"""


def page(title, body):
    if session.get("user"):
        extra_nav = (f'<a href="/cart">Cart</a> <a href="/orders">My Orders</a> '
                     f'<a href="/dashboard">{session["user"]}</a>')
    else:
        extra_nav = '<a href="/login">Login</a> <a href="/signup">Sign up</a>'
    return PAGE_SHELL.format(title=title, body=body, extra_nav=extra_nav)


def stars(n):
    return "★" * n + "☆" * (5 - n)


# ---------------------------------------------------------------------
# robots.txt + static assets
# ---------------------------------------------------------------------
@app.route("/robots.txt")
def robots():
    return f"User-agent: *\nDisallow: {DECOY_PATH}\n", 200, {"Content-Type": "text/plain"}


@app.route("/static/app.js")
def app_js():
    js = f"""// KaranCorp Shop frontend v2.1
// build: 2026.03.11-staging
// routes: /, /shop, /signup, /login, /dashboard, /orders, /forgot-password, {REAL_ADMIN_PATH}
function init() {{
    console.log("App initialized");
    document.cookie.indexOf("auth_token");
}}
init();
"""
    return js, 200, {"Content-Type": "application/javascript"}


# ---------------------------------------------------------------------
# Homepage
# ---------------------------------------------------------------------
def admin_username():
    db = get_db()
    row = db.execute("SELECT username FROM users WHERE is_admin=1").fetchone()
    return row[0] if row else "admin"


@app.route("/")
def home():
    admin_user = admin_username()
    featured = PRODUCTS[:3]
    cards = "".join(f"""
<div class="product">
  <div class="thumb" style="background:{p['color']}">{p['emoji']}</div>
  <div class="body">
    <h4>{p['name']}</h4>
    <div class="stars">{stars(p['rating'])}</div>
    <div class="price">${p['price']:.2f}</div>
    <a class="btn" href="/product/{p['id']}">View details</a>
  </div>
</div>""" for p in featured)
    body = f"""
<!-- contact: {admin_user}@karancorp.local -->
<div class="hero">
  <h1>Shop smart. Work smarter.</h1>
  <p>KaranCorp sells the gear your team actually uses -- plus the SaaS tools to run your business.</p>
  <a class="btn" href="/shop">Browse the shop</a>
</div>
<div class="shop-grid">{cards}</div>
<div class="cards" style="margin-top:40px;">
  <div class="card"><h3>📁 Projects</h3><p>Track tasks and deadlines across your whole team.</p></div>
  <div class="card"><h3>🧾 Invoicing</h3><p>Send and track invoices in seconds.</p></div>
  <div class="card"><h3>🤝 Clients</h3><p>Keep every client conversation in one timeline.</p></div>
</div>
<script src="/static/app.js"></script>
"""
    return page("Home", body)


@app.route("/shop")
def shop():
    cards = "".join(f"""
<div class="product">
  <div class="thumb" style="background:{p['color']}">{p['emoji']}</div>
  <div class="body">
    {f'<span class="tag" style="background:{p["color"]}">{p["tag"]}</span>' if p['tag'] else ''}
    <h4>{p['name']}</h4>
    <div class="stars">{stars(p['rating'])} <span style="color:var(--muted);font-size:.85em;">({p['rating']}.0)</span></div>
    <p class="desc">{p['desc']}</p>
    <div class="price">${p['price']:.2f}</div>
    <a class="btn" href="/product/{p['id']}">View details</a>
  </div>
</div>""" for p in PRODUCTS)
    body = f"""
<h1>Shop</h1>
<p style="color:var(--muted);">{len(PRODUCTS)} products across electronics, gear, and home office.</p>
<div class="shop-grid">{cards}</div>
"""
    return page("Shop", body)


@app.route("/product/<int:pid>")
def product_detail(pid):
    p = next((x for x in PRODUCTS if x["id"] == pid), None)
    if not p:
        return page("Not found", "<div class='msg'>Product not found.</div>"), 404
    body = f"""
<div class="card" style="max-width:520px;margin:0 auto;">
  <div class="thumb" style="background:{p['color']};border-radius:10px;margin-bottom:16px;">{p['emoji']}</div>
  {f'<span class="tag" style="background:{p["color"]}">{p["tag"]}</span>' if p['tag'] else ''}
  <h2>{p['name']}</h2>
  <div class="stars">{stars(p['rating'])}</div>
  <p>{p['desc']}</p>
  <div class="price">${p['price']:.2f}</div>
  <form method="POST" action="/cart/add">
    <input type="hidden" name="product_id" value="{p['id']}">
    <button class="btn" type="submit">Add to cart</button>
  </form>
</div>
"""
    return page(p["name"], body)


# ---------------------------------------------------------------------
# Cart + checkout
# ---------------------------------------------------------------------
@app.route("/cart/add", methods=["POST"])
def cart_add():
    pid = request.form.get("product_id", type=int)
    p = next((x for x in PRODUCTS if x["id"] == pid), None)
    if not p:
        return page("Shop", "<div class='msg'>Unknown product.</div>"), 400
    cart = session.get("cart", [])
    cart.append(pid)
    session["cart"] = cart
    return redirect("/cart")


@app.route("/cart")
def cart_view():
    cart = session.get("cart", [])
    items = []
    total = 0.0
    for pid in cart:
        p = next((x for x in PRODUCTS if x["id"] == pid), None)
        if p:
            items.append(p)
            total += p["price"]
    rows = "".join(f"<tr><td>{p['emoji']} {p['name']}</td><td>${p['price']:.2f}</td></tr>" for p in items)
    if not items:
        body = "<h1>Your cart</h1><p class='msg'>Your cart is empty. <a href='/shop'>Go shopping</a>.</p>"
        return page("Cart", body)
    body = f"""
<h1>Your cart</h1>
<table><tr><th>Item</th><th>Price</th></tr>{rows}</table>
<p style="text-align:right;font-weight:800;margin-top:12px;">Total: ${total:.2f}</p>
<form method="POST" action="/checkout" class="box" style="margin-top:20px;">
  <label>Shipping address</label><input name="address" placeholder="Street, city, postcode" required>
  <label>Card number (demo -- last 4 digits only are stored)</label><input name="card" placeholder="4111 1111 1111 1111" required>
  <button type="submit">Place order -- ${total:.2f}</button>
</form>
"""
    return page("Cart", body)


@app.route("/checkout", methods=["POST"])
def checkout():
    if "user" not in session:
        return redirect("/login")
    cart = session.get("cart", [])
    items = [p for pid in cart for p in PRODUCTS if p["id"] == pid]
    if not items:
        return redirect("/cart")
    total = sum(p["price"] for p in items)
    address = request.form.get("address", "").strip()
    card = request.form.get("card", "").replace(" ", "")
    card_last4 = card[-4:] if len(card) >= 4 else "0000"
    items_str = ", ".join(f"{p['name']} x1" for p in items)
    today = datetime.date.today().isoformat()

    db = get_db()
    db.execute("INSERT INTO orders (username, items, total, address, card_last4, created) VALUES (?,?,?,?,?,?)",
               (session["user"], items_str, total, address, card_last4, today))
    db.commit()
    session["cart"] = []
    return redirect("/orders")


@app.route("/orders")
def orders_list():
    if "user" not in session:
        return redirect("/login")
    db = get_db()
    rows = db.execute("SELECT id, items, total, created FROM orders WHERE username=? ORDER BY id DESC",
                       (session["user"],)).fetchall()
    if not rows:
        body = "<h1>My Orders</h1><p class='msg'>No orders yet. <a href='/shop'>Start shopping</a>.</p>"
        return page("My Orders", body)
    rows_html = "".join(
        f"<tr><td>#{r[0]}</td><td>{r[1]}</td><td>${r[2]:.2f}</td><td>{r[3]}</td>"
        f"<td><a href='/order/{r[0]}'>View</a></td></tr>" for r in rows)
    body = f"""
<h1>My Orders</h1>
<table><tr><th>Order</th><th>Items</th><th>Total</th><th>Date</th><th></th></tr>{rows_html}</table>
"""
    return page("My Orders", body)


@app.route("/order/<int:order_id>")
def order_detail(order_id):
    if "user" not in session:
        return redirect("/login")
    db = get_db()
    row = db.execute("SELECT id, username, items, total, address, card_last4, created FROM orders WHERE id=?",
                      (order_id,)).fetchone()
    if not row:
        return page("Order", "<div class='msg'>Order not found.</div>"), 404
    body = f"""
<h1>Order #{row[0]}</h1>
<table>
<tr><th>Customer</th><td>{row[1]}</td></tr>
<tr><th>Items</th><td>{row[2]}</td></tr>
<tr><th>Total</th><td>${row[3]:.2f}</td></tr>
<tr><th>Shipping address</th><td>{row[4]}</td></tr>
<tr><th>Card on file</th><td>**** **** **** {row[5]}</td></tr>
<tr><th>Date</th><td>{row[6]}</td></tr>
</table>
"""
    return page(f"Order #{row[0]}", body)


# ---------------------------------------------------------------------
# About / Pricing / Testimonials / FAQ / Blog / Contact
# ---------------------------------------------------------------------
@app.route("/about")
def about():
    body = """
<h1>About KaranCorp</h1>
<p>Founded in 2023, KaranCorp is a small team building tools (and selling the gear) that
freelancers and agencies actually use. We believe software should get out of your way.</p>
<p>Our operations team keeps the lights on 24/7 so you never have to worry about downtime.</p>
<div class="stat-row">
  <div class="stat"><div class="num">12k+</div>Orders shipped</div>
  <div class="stat"><div class="num">4.7★</div>Average rating</div>
  <div class="stat"><div class="num">35</div>Countries served</div>
</div>
"""
    return page("About", body)


@app.route("/pricing")
def pricing():
    body = """
<h1>Pricing</h1>
<table>
<tr><th>Plan</th><th>Price</th><th>Features</th></tr>
<tr><td>Starter</td><td>Free</td><td>1 project, 2 clients</td></tr>
<tr><td>Pro</td><td>$19/mo</td><td>Unlimited projects, 25 clients</td></tr>
<tr><td>Business</td><td>$49/mo</td><td>Unlimited everything, priority support</td></tr>
</table>
"""
    return page("Pricing", body)


@app.route("/testimonials")
def testimonials():
    body = """
<h1>What customers say</h1>
<div class="shop-grid">
  <div class="card"><div class="stars">★★★★★</div><p>"The Orbit Smartwatch battery actually lasts a week. Impressed."</p><p class="byline">-- Priya R.</p></div>
  <div class="card"><div class="stars">★★★★☆</div><p>"Fast shipping, backpack is sturdy. Would buy again."</p><p class="byline">-- Jordan K.</p></div>
  <div class="card"><div class="stars">★★★★★</div><p>"Support replied within the hour when my charger arrived faulty."</p><p class="byline">-- Amit S.</p></div>
  <div class="card"><div class="stars">★★★★★</div><p>"ClickType keyboard feels premium for the price. Switches are buttery smooth."</p><p class="byline">-- Neha V.</p></div>
  <div class="card"><div class="stars">★★★★☆</div><p>"ErgoFlex chair fixed my back pain within a week of WFH."</p><p class="byline">-- Rohan D.</p></div>
  <div class="card"><div class="stars">★★★★★</div><p>"PowerBrick saved me on a 14-hour flight. Charges fast too."</p><p class="byline">-- Sara L.</p></div>
</div>
"""
    return page("Reviews", body)


@app.route("/faq")
def faq():
    body = """
<h1>Frequently Asked Questions</h1>
<div class="card"><h3>What's your return policy?</h3><p>30 days, no questions asked, as long as the item is unused.</p></div>
<div class="card" style="margin-top:14px;"><h3>Do you ship internationally?</h3><p>Yes, to 35+ countries. Shipping cost is calculated at checkout.</p></div>
<div class="card" style="margin-top:14px;"><h3>How do I track my order?</h3><p>Check the "My Orders" page after logging in for status and order history.</p></div>
<div class="card" style="margin-top:14px;"><h3>Do you offer a warranty?</h3><p>All electronics come with a 1-year manufacturer warranty. Accessories are covered for 90 days.</p></div>
<div class="card" style="margin-top:14px;"><h3>Can I change my shipping address after ordering?</h3><p>Contact support within 2 hours of placing the order and we'll do our best to update it.</p></div>
"""
    return page("FAQ", body)


@app.route("/contact")
def contact():
    body = """
<div class="auth-wrap">
  <div class="auth-icon">📨</div>
  <h1>Contact Us</h1>
  <p class="auth-sub">Got a question about an order or a product? We usually reply within a business day.</p>
</div>
<form method="POST" action="/contact" class="box">
  <label>Your email</label><input name="email" type="email" placeholder="you@example.com">
  <label>Message</label><input name="message" placeholder="How can we help?">
  <button type="submit">Send message</button>
</form>
"""
    return page("Contact", body)


@app.route("/contact", methods=["POST"])
def contact_submit():
    return page("Contact", "<div class='msg'>Thanks! We'll get back to you within 1 business day.</div>")


@app.route("/blog")
def blog():
    admin_user = admin_username()
    body = f"""
<h1>Blog</h1>
<div class="blog-post">
  <h3>5 tips for staying organized as a freelancer</h3>
  <p class="byline">by maria_ops</p>
  <p>Juggling clients is hard. Here's how we do it internally...</p>
</div>
<div class="blog-post">
  <h3>Our new invoicing engine, explained</h3>
  <p class="byline">by {admin_user}</p>
  <p>We rebuilt invoicing from scratch this quarter. Here's what changed under the hood.</p>
</div>
<div class="blog-post">
  <h3>Behind the scenes: picking our Q3 product lineup</h3>
  <p class="byline">by dev_sam</p>
  <p>Why we added the Orbit Smartwatch and SnapShot Mini Camera to the shop this quarter.</p>
</div>
"""
    return page("Blog", body)


# ---------------------------------------------------------------------
# Normal user signup/login/dashboard
# ---------------------------------------------------------------------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        body = """
<div class="auth-wrap">
  <div class="auth-icon">✨</div>
  <h1>Create your account</h1>
  <p class="auth-sub">Join KaranCorp to track orders, save addresses, and check out faster.</p>
</div>
<form method="POST" class="box">
  <label>Username</label><input name="username" placeholder="Pick a username" autocomplete="username">
  <label>Password</label><input name="password" type="password" placeholder="At least 8 characters" autocomplete="new-password">
  <button type="submit">Create account</button>
</form>
<p class="auth-foot">Already have an account? <a href="/login">Log in</a></p>
"""
        return page("Sign Up", body)
    u = request.form.get("username", "").strip()
    p = request.form.get("password", "")
    if not u or not p:
        return page("Sign Up", "<div class='msg'>Username/password required.</div>"), 400
    db = get_db()
    try:
        db.execute("INSERT INTO users VALUES (?, ?, 0)", (u, p))
        db.commit()
    except sqlite3.IntegrityError:
        return page("Sign Up", "<div class='msg'>Username already taken.</div>"), 409
    return redirect("/login")


def make_auth_token(username):
    weak_hash = hashlib.md5(username.encode()).hexdigest()[:6]
    return base64.b64encode(f"{username}:{weak_hash}".encode()).decode()


def verify_auth_token(token):
    try:
        raw = base64.b64decode(token.encode()).decode()
        username, weak_hash = raw.split(":", 1)
    except Exception:
        return None
    if weak_hash == hashlib.md5(username.encode()).hexdigest()[:6]:
        return username
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        body = """
<div class="auth-wrap">
  <div class="auth-icon">🔐</div>
  <h1>Welcome back</h1>
  <p class="auth-sub">Log in to see your orders and saved details.</p>
</div>
<form method="POST" class="box">
  <label>Username</label><input name="username" placeholder="Your username" autocomplete="username">
  <label>Password</label><input name="password" type="password" placeholder="Your password" autocomplete="current-password">
  <button type="submit">Log in</button>
</form>
<p class="auth-foot"><a href="/forgot-password">Forgot password?</a> &nbsp;·&nbsp; New here? <a href="/signup">Sign up</a></p>
"""
        return page("Login", body)
    u = request.form.get("username", "")
    p = request.form.get("password", "")
    db = get_db()
    row = db.execute("SELECT password FROM users WHERE username=?", (u,)).fetchone()
    if row and row[0] == p:
        session["user"] = u
        resp = make_response(redirect("/dashboard"))
        resp.set_cookie("auth_token", make_auth_token(u), max_age=30 * 24 * 3600)
        return resp
    return page("Login", "<div class='msg'>Invalid username or password.</div>"), 401


@app.route("/autologin")
def autologin():
    token = request.cookies.get("auth_token")
    if not token:
        return redirect("/login")
    username = verify_auth_token(token)
    if not username:
        return page("Login", "<div class='msg'>Invalid session token.</div>"), 401
    session["user"] = username
    return redirect("/dashboard")


@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect("/login")
    db = get_db()
    row = db.execute("SELECT is_admin FROM users WHERE username=?", (session["user"],)).fetchone()
    if row and row[0]:
        body = f"<h1>ADMIN ACCESS GRANTED</h1><p>Logged in as {session['user']}. You fully hacked your own site. GG!</p>"
    else:
        body = f"<h1>Welcome, {session['user']}</h1><p>Just a normal user dashboard. Check <a href='/orders'>My Orders</a> or keep browsing the <a href='/shop'>shop</a>.</p>"
    return page("Dashboard", body)


# ---------------------------------------------------------------------
# Password reset flow -- predictable token (Technique #15)
# ---------------------------------------------------------------------
def make_reset_token(username):
    today = datetime.date.today().strftime("%Y%m%d")
    return hashlib.md5(f"{username}{today}".encode()).hexdigest()[:8]


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        body = """
<div class="auth-wrap">
  <div class="auth-icon">🔑</div>
  <h1>Forgot your password?</h1>
  <p class="auth-sub">Enter your username and we'll email a reset link if the account exists.</p>
</div>
<form method="POST" class="box">
  <label>Username</label><input name="username" placeholder="Your username" autocomplete="username">
  <button type="submit">Send reset link</button>
</form>
<p class="auth-foot">Remembered it? <a href="/login">Back to login</a></p>
"""
        return page("Forgot password", body)
    # generic response regardless of whether the account exists (no enum leak)
    return page("Forgot password",
                "<div class='msg'>If that account exists, a reset link has been sent to the registered email.</div>")


@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    user = request.args.get("user", "")
    token = request.args.get("token", "")
    if not user or not token or token != make_reset_token(user):
        return page("Reset password", "<div class='msg'>Invalid or expired reset link.</div>"), 401

    if request.method == "GET":
        body = f"""
<div class="auth-wrap">
  <div class="auth-icon">🔓</div>
  <h1>Set a new password</h1>
  <p class="auth-sub">Choose a new password for <strong>{user}</strong>.</p>
</div>
<form method="POST" class="box">
  <input type="hidden" name="user" value="{user}">
  <input type="hidden" name="token" value="{token}">
  <label>New password</label><input name="new_password" type="password" placeholder="At least 8 characters" autocomplete="new-password">
  <button type="submit">Reset password</button>
</form>
"""
        return page("Reset password", body)

    new_password = request.form.get("new_password", "")
    if not new_password:
        return page("Reset password", "<div class='msg'>Password required.</div>"), 400
    db = get_db()
    db.execute("UPDATE users SET password=? WHERE username=?", (new_password, user))
    db.commit()
    return page("Reset password", "<div class='msg'>Password updated. You can now log in.</div>")


# ---------------------------------------------------------------------
# DECOY admin panels (honeypots) -- one advertised via robots.txt, a few
# more that only turn up if you blindly fuzz common admin-panel wordlists
# instead of following the actual recon trail
# ---------------------------------------------------------------------
HONEYPOT_FORM = """
<div class="auth-wrap">
  <div class="auth-icon">🛠️</div>
  <h1>Admin Panel</h1>
  <p class="auth-sub">Staff login only.</p>
</div>
<form method="POST" class="box">
  <label>Username</label><input name="username" placeholder="Staff username">
  <label>Password</label><input name="password" type="password" placeholder="Password">
  <button type="submit">Login</button>
</form>
"""


def _honeypot_post():
    ip = client_ip()
    BANNED_IPS[ip] = time.time() + HONEYPOT_BAN_SECONDS
    return page("Admin", "<div class='msg'>Invalid username or password.</div>"), 401


@app.route(DECOY_PATH, methods=["GET", "POST"])
def decoy_admin():
    if request.method == "GET":
        return page("Admin", HONEYPOT_FORM)
    return _honeypot_post()


def register_extra_honeypots():
    for path in EXTRA_HONEYPOT_PATHS:
        def make_view(p=path):
            def view():
                if request.method == "GET":
                    return page("Admin", HONEYPOT_FORM)
                return _honeypot_post()
            return view
        app.add_url_rule(path, endpoint=f"honeypot_{path.strip('/')}", view_func=make_view(),
                          methods=["GET", "POST"])


# ---------------------------------------------------------------------
# REAL admin panel -- bot-UA block + rate limit + math CAPTCHA + weak creds
# ---------------------------------------------------------------------
def register_real_admin_route():
    @app.route(REAL_ADMIN_PATH, methods=["GET", "POST"], endpoint="real_admin")
    def real_admin():
        ua = request.headers.get("User-Agent", "").lower()
        if any(bad in ua for bad in BLOCKED_UA_SUBSTRINGS):
            return jsonify({"error": "Automated tool traffic blocked. Use a real browser User-Agent."}), 403

        if request.method == "GET":
            a, b = random.randint(1, 9), random.randint(1, 9)
            session["captcha_a"] = a
            session["captcha_b"] = b
            body = f"""
<div class="auth-wrap">
  <div class="auth-icon">🖥️</div>
  <h1>Ops Console</h1>
  <p class="auth-sub">Internal operations login.</p>
</div>
<form method="POST" class="box">
  <label>Username</label><input name="username" placeholder="Staff username">
  <label>Password</label><input name="password" type="password" placeholder="Password">
  <label>Security check: what is {a} + {b}?</label><input name="captcha" placeholder="Answer">
  <button type="submit">Login</button>
</form>
"""
            return page("Ops Console", body)

        ip = client_ip()
        now = time.time()
        if ip in RATE_BLOCKED_UNTIL and now < RATE_BLOCKED_UNTIL[ip]:
            remaining = round(RATE_BLOCKED_UNTIL[ip] - now, 1)
            return jsonify({"error": f"Rate limit hit. Retry in {remaining}s."}), 429

        RATE_ATTEMPTS.setdefault(ip, [])
        RATE_ATTEMPTS[ip] = [t for t in RATE_ATTEMPTS[ip] if now - t < RATE_WINDOW]
        RATE_ATTEMPTS[ip].append(now)
        if len(RATE_ATTEMPTS[ip]) > RATE_MAX:
            block_for = random.uniform(10, 20)
            RATE_BLOCKED_UNTIL[ip] = now + block_for
            return jsonify({"error": f"Rate limit hit. Retry in {round(block_for, 1)}s."}), 429

        expected = session.get("captcha_a", -1) + session.get("captcha_b", -2)
        try:
            provided = int(request.form.get("captcha", "").strip())
        except ValueError:
            provided = None
        if provided != expected:
            return page("Ops Console", "<div class='msg'>Invalid username, password, or security check.</div>"), 401

        u = request.form.get("username", "")
        p = request.form.get("password", "")
        db = get_db()
        row = db.execute("SELECT password FROM users WHERE username=? AND is_admin=1", (u,)).fetchone()
        if row and row[0] == p:
            return page("Ops Console", "<h1>ADMIN ACCESS GRANTED</h1><p>You fully hacked your own site. GG!</p>")
        return page("Ops Console", "<div class='msg'>Invalid username, password, or security check.</div>"), 401


if __name__ == "__main__":
    setup_db_and_secret()
    register_real_admin_route()
    register_extra_honeypots()
    print("[setup] Site running. Start your recon at http://127.0.0.1:5000/")
    app.run(host="127.0.0.1", port=5000, debug=False)
