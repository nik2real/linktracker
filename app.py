"""
Minimal link shortener with click analytics.

Run:
    pip install flask
    export ADMIN_PASSWORD="choose-something-strong"
    python app.py

Then open http://localhost:5000/admin
"""
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import (Flask, Response, abort, g, redirect, render_template_string,
                   request, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

DB_PATH = os.environ.get("DB_PATH", "links.db")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
# Set to the number of reverse proxies in front of the app (0 if none).
# Only trust X-Forwarded-For when this matches your real setup.
TRUSTED_PROXIES = int(os.environ.get("TRUSTED_PROXIES", "0"))

app = Flask(__name__)
if TRUSTED_PROXIES:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=TRUSTED_PROXIES)


# ---------- database ----------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS links (
                code TEXT PRIMARY KEY,
                target TEXT NOT NULL,
                label TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clicks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL REFERENCES links(code),
                ts TEXT NOT NULL,
                ip TEXT,
                user_agent TEXT,
                referrer TEXT,
                language TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_clicks_code ON clicks(code);
            """
        )


# ---------- auth ----------
def require_admin():
    auth = request.authorization
    if not ADMIN_PASSWORD:
        abort(503, "Set the ADMIN_PASSWORD environment variable.")
    if not auth or not secrets.compare_digest(auth.password or "", ADMIN_PASSWORD):
        abort(Response("Login required", 401,
                       {"WWW-Authenticate": 'Basic realm="Admin"'}))


# ---------- templates ----------
BASE = """
<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Link Tracker</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#222}
 input,button{padding:.5rem;font-size:1rem} input[type=url],input[type=text]{width:100%;box-sizing:border-box;margin:.25rem 0}
 table{border-collapse:collapse;width:100%;font-size:.9rem} td,th{border-bottom:1px solid #ddd;padding:.4rem;text-align:left;word-break:break-all}
 .box{background:#f5f5f5;padding:1rem;border-radius:8px;margin-bottom:1.5rem}
 a{color:#0b5fff}
</style>
{{ body|safe }}
"""

ADMIN = """
<h1>Link Tracker</h1>
<form method="post" action="{{ url_for('create') }}" class="box">
  <label>Destination URL<input type="url" name="target" required placeholder="https://example.com/page"></label>
  <label>Label (optional)<input type="text" name="label" maxlength="100"></label>
  <button>Create short link</button>
</form>
<table>
 <tr><th>Link</th><th>Label</th><th>Destination</th><th>Clicks</th><th>Created</th></tr>
 {% for l in links %}
 <tr>
  <td><a href="{{ url_for('stats', code=l.code) }}">{{ request.host_url }}{{ l.code }}</a></td>
  <td>{{ l.label or '' }}</td><td>{{ l.target }}</td><td>{{ l.n }}</td><td>{{ l.created_at[:16] }}</td>
 </tr>
 {% else %}<tr><td colspan=5>No links yet.</td></tr>{% endfor %}
</table>
"""

STATS = """
<p><a href="{{ url_for('admin') }}">&larr; All links</a></p>
<h1>{{ request.host_url }}{{ link.code }}</h1>
<p>&rarr; {{ link.target }}<br>{{ clicks|length }} click(s) shown (latest 500)</p>
<table>
 <tr><th>Time (UTC)</th><th>IP</th><th>User agent</th><th>Referrer</th><th>Language</th></tr>
 {% for c in clicks %}
 <tr><td>{{ c.ts[:19] }}</td><td>{{ c.ip }}</td><td>{{ c.user_agent }}</td>
     <td>{{ c.referrer or '' }}</td><td>{{ c.language or '' }}</td></tr>
 {% else %}<tr><td colspan=5>No clicks yet.</td></tr>{% endfor %}
</table>
<form method="post" action="{{ url_for('delete', code=link.code) }}"
      onsubmit="return confirm('Delete this link and all its click data?')">
  <p><button>Delete link and data</button></p>
</form>
"""


def page(template, **ctx):
    body = render_template_string(template, **ctx)
    return render_template_string(BASE, body=body)


# ---------- routes ----------
@app.get("/")
def index():
    return redirect(url_for("admin"))


@app.get("/admin")
def admin():
    require_admin()
    links = db().execute(
        """SELECT l.*, COUNT(c.id) AS n FROM links l
           LEFT JOIN clicks c ON c.code = l.code
           GROUP BY l.code ORDER BY l.created_at DESC"""
    ).fetchall()
    return page(ADMIN, links=links)


@app.post("/admin/create")
def create():
    require_admin()
    target = request.form.get("target", "").strip()
    parsed = urlparse(target)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        abort(400, "Destination must be a valid http(s) URL.")
    code = secrets.token_urlsafe(5)
    db().execute(
        "INSERT INTO links(code, target, label, created_at) VALUES (?,?,?,?)",
        (code, target, request.form.get("label", "").strip()[:100],
         datetime.now(timezone.utc).isoformat()),
    )
    db().commit()
    return redirect(url_for("stats", code=code))


@app.get("/admin/<code>")
def stats(code):
    require_admin()
    link = db().execute("SELECT * FROM links WHERE code=?", (code,)).fetchone()
    if not link:
        abort(404)
    clicks = db().execute(
        "SELECT * FROM clicks WHERE code=? ORDER BY id DESC LIMIT 500", (code,)
    ).fetchall()
    return page(STATS, link=link, clicks=clicks)


@app.post("/admin/<code>/delete")
def delete(code):
    require_admin()
    db().execute("DELETE FROM clicks WHERE code=?", (code,))
    db().execute("DELETE FROM links WHERE code=?", (code,))
    db().commit()
    return redirect(url_for("admin"))


@app.get("/<code>")
def follow(code):
    link = db().execute("SELECT target FROM links WHERE code=?", (code,)).fetchone()
    if not link:
        abort(404)
    db().execute(
        "INSERT INTO clicks(code, ts, ip, user_agent, referrer, language) VALUES (?,?,?,?,?,?)",
        (code, datetime.now(timezone.utc).isoformat(), request.remote_addr,
         request.headers.get("User-Agent", "")[:500],
         request.headers.get("Referer", "")[:500],
         request.headers.get("Accept-Language", "")[:100]),
    )
    db().commit()
    return redirect(link["target"], code=302)


init_db()
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
