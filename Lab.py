#!/usr/bin/env python3
"""
Karan's Realistic Red-Team Lab (v7 - Networked Edition)
=========================================================
"KaranCorp Shop" -- same full e-commerce-style site as before, now running
as the PUBLIC-FACING node of a small multi-tier network (via docker-compose):

    nginx (TLS edge, :443/:80)
      -> web          (this file -- what Kali can actually reach)
           -> internal-ops   (internal-only microservice, NOT published to host)
           -> legacy-jumpbox (internal-only SSH box, NOT published to host)

Koi walkthrough/hint nahi hai jaan-bujh kar. Attack paths (independent):

  1. Direct credential attack on a hidden admin console (UA filter +
     rate limit + CAPTCHA all have to be handled, not just brute-forced)
  2. Session-token forging (weak "remember me" cookie)
  3. Password-reset-token forging (predictable reset link)
  4. IDOR on order history (order detail view trusts the ID, not the owner)
  5. NEW -- SSRF via the "Price Alert" feature: this server fetches a URL
     YOU give it. Kali can't reach the internal network directly (it's a
     docker-internal bridge) -- but this app can, because it lives on that
     network. Think about what that lets you reach.

RUN KARNE KA TARIKA (poore lab ke liye, is file ko standalone mat chalana):
    cd karancorp-lab
    docker compose up --build -d

Setup hote hi "admin_secret_DONT_PEEK.txt" is container ke andar banegi --
docker exec se dekhna, mat kholna jab tak hack na kar lo.
"""

import base64
import datetime
import hashlib
import os
import random
import sqlite3
import string
import subprocess
import time

import requests
from flask import Flask, request, g, redirect, session, jsonify, make_response

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()

DB = "redteam_lab.db"
SECRET_FILE = "admin_secret_DONT_PEEK.txt"

INTERNAL_OPS_URL = os.environ.get("INTERNAL_OPS_URL", "http://172.28.0.12:6000")

WEAK_PASSWORDS = ["Dragon123", "Welcome1", "Password1", "Sunshine1", "Football1", "Monkey123"]
ADMIN_USERNAMES = ["opsadmin", "sysadmin", "webadmin", "netadmin"]

DECOY_PATH = "/admin"
REAL_ADMIN_PATH = "/ops-" + "".join(random.choices(string.hexdigits.lower(), k=6)) + "-console"

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

        conn.execute("INSERT OR IGNORE INTO users VALUES (?, ?, 0)",
                      ("maria_ops", "".join(random.choices(string.ascii_letters + string.digits, k=12))))
        conn.execute("INSERT OR IGNORE INTO users VALUES (?, ?, 0)",
                      ("dev_sam", "".join(random.choices(string.ascii_letters + string.digits, k=12))))

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
            f.write(f"Real admin console requires solving a math CAPTCHA (session-bound, shown on GET)\n")
            f.write(f"Session-token scheme: base64(username + ':' + md5(username)[:6])\n")
            f.write(f"  -> forge cookie 'auth_token' with this value, then GET /autologin\n")
            f.write(f"Reset-token scheme: md5(username + YYYYMMDD)[:8]\n")
            f.write(f"  -> GET /reset-password?user=<username>&token=<computed> to set a new password\n")
            f.write(f"Order IDs 1-3 are pre-seeded (maria_ops, dev_sam, {admin_user})\n")
            f.write(f"  -> /order/<id> does not check ownership -- any logged-in user can view any order (IDOR)\n")
            f.write(f"\n--- Network pivot path (v7) ---\n")
            f.write(f"This container can reach the internal docker network, Kali cannot (directly).\n")
            f.write(f"Internal services exist at 172.28.0.12 (internal-ops, port 6000) and 172.28.0.13 (legacy-jumpbox, SSH 22)\n")
            f.write(f"  -> abuse the '/price-alert' SSRF feature on a product page to make THIS server\n")
            f.write(f"     fetch http://172.28.0.12:6000/internal/employees for you\n")
            f.write(f"  -> legacy-jumpbox SSH creds: legacyops / Summer2024!  -- only reachable once you've\n")
            f.write(f"     pivoted (SSH client inside this web container, or via a tunnel/proxy you build)\n")
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
# Shared page layout
# ---------------------------------------------------------------------
PAGE_SHELL = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - KaranCorp</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --ink:#161b22; --paper:#ffffff; --surface:#f2f4f7; --line:#dbe0e6;
    --brand:#1f5c4d; --brand-dark:#163f35; --rust:#b8551f; --muted:#5b6472;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:'Inter',-apple-system,Segoe UI,Roboto,Arial,sans-serif;
          background:var(--paper); color:var(--ink); font-size:15px; line-height:1.55; }}
  h1,h2,h3,h4 {{ font-family:'Barlow Condensed','Inter',Arial,sans-serif; font-weight:700;
                 letter-spacing:.2px; margin:0 0 .4em; }}
  a {{ color:var(--brand); }}
  header {{ background:var(--paper); border-bottom:1px solid var(--line); padding:0 32px;
            display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; height:64px; }}
  header .logo {{ font-family:'Barlow Condensed','Inter',Arial,sans-serif; font-weight:700;
                  font-size:1.5em; color:var(--ink); letter-spacing:.3px; }}
  header .logo span {{ color:var(--brand); }}
  nav a {{ margin-left:20px; color:var(--ink); text-decoration:none; font-size:0.9em; font-weight:600;
           border-bottom:2px solid transparent; padding-bottom:4px; }}
  nav a:hover, nav a:focus-visible {{ border-bottom-color:var(--brand); }}
  main {{ max-width:1080px; margin:0 auto; padding:0 24px 56px; }}

  .hero-band {{ display:flex; gap:48px; align-items:center; padding:48px 0 40px;
                border-bottom:1px solid var(--line); flex-wrap:wrap; }}
  .hero-copy {{ flex:1.3; min-width:280px; }}
  .hero-copy h1 {{ font-size:2.6em; line-height:1.05; margin-bottom:14px; max-width:11ch; }}
  .hero-copy p {{ color:var(--muted); font-size:1.05em; max-width:46ch; margin-bottom:20px; }}
  .hero-panel {{ flex:1; min-width:240px; background:var(--surface); border:1px solid var(--line); padding:22px; }}
  .hero-panel .row {{ display:flex; justify-content:space-between; padding:9px 0;
                       border-bottom:1px solid var(--line); font-size:0.92em; }}
  .hero-panel .row:last-child {{ border-bottom:none; }}
  .hero-panel .row b {{ color:var(--ink); }}

  .cards {{ display:flex; gap:1px; margin-top:32px; flex-wrap:wrap; background:var(--line); border:1px solid var(--line); }}
  .card {{ background:var(--paper); padding:22px; flex:1; min-width:220px; }}
  .card h3 {{ margin-top:0; font-size:1.15em; }}

  .shop-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:1px;
                background:var(--line); border:1px solid var(--line); margin-top:24px; }}
  .product {{ background:var(--paper); display:flex; flex-direction:column; }}
  .product .swatch {{ height:6px; }}
  .product .body {{ padding:18px; flex:1; display:flex; flex-direction:column; }}
  .product .sku {{ color:var(--muted); font-size:0.72em; letter-spacing:.3px; margin-bottom:4px; }}
  .product h4 {{ margin:0 0 4px; font-size:1.02em; font-family:'Inter',sans-serif; font-weight:600; }}
  .product .price {{ font-weight:700; color:var(--rust); font-size:1.1em; margin:8px 0 2px; }}
  .product .stars {{ color:#c99b2f; font-size:0.9em; }}
  .product .tag {{ display:inline-block; font-size:0.68em; font-weight:700; color:#fff; padding:2px 8px;
                    margin-bottom:6px; width:fit-content; }}
  .product .desc {{ color:var(--muted); font-size:0.85em; flex:1; margin:4px 0 10px; }}

  .btn {{ display:inline-block; margin-top:12px; padding:11px 20px; background:var(--brand); color:#fff;
          border:none; font-size:0.92em; font-weight:600; text-decoration:none; text-align:center; cursor:pointer; }}
  .btn:hover {{ background:var(--brand-dark); }}
  .btn-outline {{ background:transparent; border:1px solid var(--ink); color:var(--ink); }}
  .btn-outline:hover {{ background:var(--ink); color:#fff; }}

  .auth-wrap {{ max-width:420px; margin:36px auto 0; text-align:left; }}
  .auth-icon {{ font-size:1.8em; display:inline-flex; align-items:center; justify-content:center;
                width:56px; height:56px; border-radius:50%; background:var(--surface);
                border:1px solid var(--line); margin-bottom:14px; }}
  .auth-wrap h1 {{ font-size:1.9em; margin:0 0 4px; }}
  .auth-sub {{ color:var(--muted); font-size:0.95em; margin-bottom:4px; }}
  form.box {{ background:var(--paper); border:1px solid var(--line); padding:32px;
              max-width:420px; margin:18px auto 0; text-align:left; }}
  form.box label {{ display:block; margin:16px 0 6px; font-size:0.86em; font-weight:600; color:var(--ink); }}
  form.box label:first-of-type {{ margin-top:0; }}
  form.box input {{ width:100%; padding:11px 12px; border:1px solid var(--line); font-size:0.98em; font-family:inherit; }}
  form.box input:focus-visible {{ outline:2px solid var(--brand); outline-offset:1px; border-color:var(--brand); }}
  form.box button {{ margin-top:22px; width:100%; padding:12px; background:var(--brand); color:#fff;
                      border:none; font-size:1em; font-weight:700; cursor:pointer; }}
  form.box button:hover {{ background:var(--brand-dark); }}
  .auth-foot {{ text-align:left; margin-top:14px; font-size:0.9em; color:var(--muted); }}
  .auth-foot a {{ color:var(--brand); font-weight:600; text-decoration:none; }}
  .auth-foot a:hover {{ text-decoration:underline; }}

  .msg {{ max-width:420px; padding:13px 16px; background:var(--surface); border:1px solid var(--line);
          border-left:3px solid var(--rust); }}
  .blog-post {{ border-bottom:1px solid var(--line); padding:20px 0; }}
  .blog-post:first-of-type {{ padding-top:0; }}
  .byline {{ color:var(--muted); font-size:0.85em; }}
  .badge {{ display:inline-block; padding:3px 10px; font-size:0.72em; font-weight:700; color:#fff; }}
  footer {{ border-top:1px solid var(--line); margin-top:40px; padding:28px 24px; color:var(--muted); font-size:0.85em; }}
  footer .foot-inner {{ max-width:1080px; margin:0 auto; display:flex; justify-content:space-between;
                         flex-wrap:wrap; gap:8px; }}
  table {{ width:100%; border-collapse:collapse; background:var(--paper); border:1px solid var(--line); }}
  th, td {{ padding:10px 12px; border-bottom:1px solid var(--line); text-align:left; font-size:0.92em; }}
  th {{ background:var(--surface); color:var(--ink); font-weight:700; }}
  .stat-row {{ display:flex; gap:1px; flex-wrap:wrap; margin-top:24px; background:var(--line); border:1px solid var(--line); }}
  .stat {{ flex:1; min-width:140px; background:var(--paper); padding:20px; text-align:left; }}
  .stat .num {{ font-size:1.9em; font-weight:700; color:var(--brand); font-family:'Barlow Condensed',sans-serif; }}
  pre {{ background:var(--ink); color:#d9deeb; padding:14px; overflow-x:auto; font-size:0.82em; }}
  @media (max-width:640px) {{
    header {{ padding:12px 16px; height:auto; flex-direction:column; align-items:flex-start; }}
    nav {{ margin-top:8px; }}
    nav a {{ margin-left:0; margin-right:16px; }}
    .hero-copy h1 {{ font-size:2em; max-width:none; }}
  }}
  @media (prefers-reduced-motion: reduce) {{ * {{ transition:none !important; }} }}
</style>
</head>
<body>
<header>
  <div class="logo">Karan<span>Corp</span></div>
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
<footer><div class="foot-inner"><span>&copy; 2026 KaranCorp Inc. All rights reserved.</span><span>Ops Dept, 2 Dock Street, London E1 8JN</span></div></footer>
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
    js = f"""// KaranCorp Shop frontend v2.2
// build: 2026.10-prod
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


def product_card(p):
    tag_html = f'<span class="tag" style="background:{p["color"]}">{p["tag"]}</span>' if p['tag'] else ''
    return f"""
<div class="product">
  <div class="swatch" style="background:{p['color']}"></div>
  <div class="body">
    <div class="sku">SKU-{p['id']:04d}</div>
    {tag_html}
    <h4>{p['name']}</h4>
    <div class="stars">{stars(p['rating'])} <span style="color:var(--muted);font-size:.85em;">({p['rating']}.0)</span></div>
    <p class="desc">{p['desc']}</p>
    <div class="price">${p['price']:.2f}</div>
    <a class="btn" href="/product/{p['id']}">View details</a>
  </div>
</div>"""


@app.route("/")
def home():
    admin_user = admin_username()
    featured = PRODUCTS[:3]
    cards = "".join(product_card(p) for p in featured)
    body = f"""
<!-- contact: {admin_user}@karancorp.local -->
<div class="hero-band">
  <div class="hero-copy">
    <h1>Work gear, shipped fast.</h1>
    <p>KaranCorp stocks the everyday hardware distributed teams actually order --
    headphones, chargers, desk setups -- with next-day dispatch and simple
    account invoicing for procurement.</p>
    <a class="btn" href="/shop">Browse the catalog</a>
    <a class="btn btn-outline" style="margin-left:10px;" href="/pricing">Account pricing</a>
  </div>
  <div class="hero-panel">
    <div class="row"><span>Orders shipped</span><b>12,340+</b></div>
    <div class="row"><span>Avg. dispatch time</span><b>&lt; 24h</b></div>
    <div class="row"><span>Countries served</span><b>35</b></div>
    <div class="row"><span>Catalog SKUs</span><b>{len(PRODUCTS)}</b></div>
  </div>
</div>
<h2 style="margin-top:40px;">Featured this week</h2>
<div class="shop-grid">{cards}</div>
<script src="/static/app.js"></script>
"""
    return page("Home", body)


@app.route("/shop")
def shop():
    cards = "".join(product_card(p) for p in PRODUCTS)
    body = f"""
<h1>Catalog</h1>
<p style="color:var(--muted);">{len(PRODUCTS)} SKUs across electronics, gear, and home office.</p>
<div class="shop-grid">{cards}</div>
"""
    return page("Shop", body)


@app.route("/product/<int:pid>")
def product_detail(pid):
    p = next((x for x in PRODUCTS if x["id"] == pid), None)
    if not p:
        return page("Not found", "<div class='msg'>Product not found.</div>"), 404
    tag_html = f'<span class="tag" style="background:{p["color"]}">{p["tag"]}</span>' if p['tag'] else ''
    body = f"""
<div style="max-width:520px;margin:0 auto;border:1px solid var(--line);">
  <div style="height:10px;background:{p['color']};"></div>
  <div style="padding:22px;">
    <div class="sku">SKU-{p['id']:04d}</div>
    {tag_html}
    <h2>{p['name']}</h2>
    <div class="stars">{stars(p['rating'])}</div>
    <p>{p['desc']}</p>
    <div class="price">${p['price']:.2f}</div>
    <form method="POST" action="/cart/add">
      <input type="hidden" name="product_id" value="{p['id']}">
      <button class="btn" type="submit">Add to cart</button>
    </form>
    <a class="btn btn-outline" style="margin-top:8px;" href="/price-alert?pid={p['id']}">Set a price-drop alert</a>
  </div>
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
<a class="btn btn-outline" style="margin-top:14px;display:inline-block;" href="/order/{row[0]}/export">Export packing slip</a>
"""
    return page(f"Order #{row[0]}", body)


# ---------------------------------------------------------------------
# NEW -- Packing slip export (Command Injection vector)
# Real pattern: warehouse/fulfilment teams let the customer attach a short
# delivery note that gets printed on the physical packing slip. Backend
# shells out to build the slip file (very common in small/mid logistics
# tooling that never got a proper templating engine).
#
# "Sanitisation" here blocks the obvious shell metacharacters and a short
# keyword blacklist -- exactly what a dev who read one Stack Overflow
# answer would ship. It does NOT account for command substitution via
# $(...), and the keyword blacklist is a naive substring check that
# breaks the moment the string isn't contiguous (quote-splitting).
# ---------------------------------------------------------------------
CI_BLOCKED_CHARS = [";", "|", "&", "`", "\n", "\r"]
CI_BLOCKED_WORDS = ["bash", "sh ", "nc ", "curl", "wget", "python"]


@app.route("/order/<int:order_id>/export", methods=["GET", "POST"])
def order_export(order_id):
    if "user" not in session:
        return redirect("/login")
    db = get_db()
    row = db.execute("SELECT id, username, items, total, created FROM orders WHERE id=?",
                      (order_id,)).fetchone()
    if not row:
        return page("Export", "<div class='msg'>Order not found.</div>"), 404

    if request.method == "GET":
        body = f"""
<div class="auth-wrap">
  <h1>Export packing slip -- Order #{row[0]}</h1>
  <p class="auth-sub">Add an optional note for the warehouse team -- it gets printed on the
  physical packing slip we attach to the box.</p>
</div>
<form method="POST" class="box">
  <label>Delivery note (shown to warehouse staff)</label>
  <input name="note" placeholder="e.g. Leave with the building reception">
  <button type="submit">Generate packing slip</button>
</form>
"""
        return page("Export packing slip", body)

    note = request.form.get("note", "")
    if any(ch in note for ch in CI_BLOCKED_CHARS):
        return page("Export packing slip", "<div class='msg'>Delivery note contains invalid characters.</div>"), 400
    lowered = note.lower()
    if any(w in lowered for w in CI_BLOCKED_WORDS):
        return page("Export packing slip", "<div class='msg'>Delivery note contains blocked content.</div>"), 400

    slip_path = f"/tmp/slip_{row[0]}.txt"
    # NOTE (not shown to the attacker): string-built shell command, "note"
    # lands straight inside a double-quoted echo -- command substitution
    # $(...) is NOT in the character blacklist above.
    cmd = f'echo "Packing slip for order #{row[0]} -- Note: {note}" > {slip_path} && cat {slip_path}'
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=6)
        output = (result.stdout or "") + (result.stderr or "")
    except Exception as e:
        output = f"slip generation failed: {e}"

    body = f"""
<h1>Packing slip generated</h1>
<pre>{output}</pre>
<p><a href="/order/{row[0]}">Back to order</a></p>
"""
    return page("Export packing slip", body)


# ---------------------------------------------------------------------
# NEW -- Price Alert (SSRF vector)
# ---------------------------------------------------------------------
@app.route("/price-alert", methods=["GET", "POST"])
def price_alert():
    if request.method == "GET":
        pid = request.args.get("pid", "")
        body = f"""
<div class="auth-wrap">
  <div class="auth-icon">🔔</div>
  <h1>Price Drop Alert</h1>
  <p class="auth-sub">We'll ping your webhook URL when the price drops (demo: fetched server-side right now so you can see the response).</p>
</div>
<form method="POST" class="box">
  <input type="hidden" name="pid" value="{pid}">
  <label>Your webhook URL</label><input name="url" placeholder="https://your-webhook.example.com/notify">
  <button type="submit">Save alert &amp; test webhook</button>
</form>
"""
        return page("Price Alert", body)

    url = request.form.get("url", "").strip()
    if not url:
        return page("Price Alert", "<div class='msg'>URL required.</div>"), 400
    try:
        resp = requests.get(url, timeout=3)
        snippet = resp.text[:800]
        body = f"""
<h1>Webhook test result</h1>
<p>We reached <code>{url}</code> -- here's what it returned (first 800 chars):</p>
<pre>{snippet}</pre>
<p><a href="/shop">Back to shop</a></p>
"""
    except Exception as e:
        body = f"""
<h1>Webhook test failed</h1>
<p class='msg'>Could not reach <code>{url}</code>: {str(e)[:200]}</p>
"""
    return page("Price Alert", body)


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
# Password reset flow -- predictable token
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
# DECOY admin panels (honeypots)
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
    print("[setup] web container up. Edge should be reachable via nginx.")
    app.run(host="0.0.0.0", port=80, debug=False)
