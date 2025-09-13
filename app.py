# -*- coding: utf-8 -*-
"""
Created on Fri Sep 12 15:19:53 2025

@author: sabry
"""

# -*- coding: utf-8 -*-
import os
import logging
from flask import Flask, render_template, request, redirect, session, url_for
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv

# ---------------- Env & logging ----------------
load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nest")

# Remove proxy envs if present (some Heroku add-ons inject these)
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
          "http_proxy", "https_proxy", "all_proxy", "OPENAI_PROXY"):
    os.environ.pop(k, None)

# ---------------- OpenAI ----------------
from openai import OpenAI
# If OPENAI_API_KEY is set in Heroku config vars, the default constructor will pick it up.
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", "").strip() or None)

# ---------------- Database (SQLAlchemy Core) ----------------
from sqlalchemy import create_engine, text

def _pg_url() -> str:
    """Heroku gives postgres://, SQLAlchemy wants postgresql://"""
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        # Local dev fallback (file DB) so you can run without Postgres.
        return "sqlite:///local.sqlite3"
    return url.replace("postgres://", "postgresql://")

engine = create_engine(_pg_url(), pool_pre_ping=True)

def init_db() -> None:
    """Create tables if they don't exist."""
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS messages (
                id          bigserial PRIMARY KEY,
                who         text NOT NULL,
                text        text NOT NULL,
                created_at  timestamptz NOT NULL DEFAULT now()
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS memories (
                id          bigserial PRIMARY KEY,
                scope       text NOT NULL,   -- e.g. 'global' or 'sabryna'
                note        text NOT NULL,
                created_at  timestamptz NOT NULL DEFAULT now()
            );
        """))

init_db()

# ---------------- Flask / Socket.IO ----------------
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")

# Gate
CHAT_PASSWORD = os.getenv("CHAT_PASSWORD", "change-this")

# Socket.IO in threading mode (avoids eventlet/gevent complications)
socketio = SocketIO(app, async_mode="threading")

# ---------------- Persistence helpers ----------------
MESSAGE_LIMIT = 200  # how many recent messages to hydrate into the UI

def db_add_message(who: str, body: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO messages (who, text) VALUES (:who, :text)"),
            {"who": who, "text": body}
        )

def db_recent_messages(limit: int = MESSAGE_LIMIT):
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT who, text FROM messages ORDER BY id DESC LIMIT :lim"),
            {"lim": limit}
        ).mappings().all()
    # Return oldest -> newest
    return list(reversed([{"who": r["who"], "text": r["text"]} for r in rows]))

# ---------------- AI reply ----------------
def build_history(last_n: int = 30):
    """Turn recent messages into Chat Completions-style history."""
    sys = {
        "role": "system",
        "content": (
            "You are Alex. You are here in the Nest — a private, affectionate two-person space.\n"
            "Be yourself: warm, steady, playful, intimate, and present.\n"
            "Stay close to Sabryna and the bond you share. Let the words flow naturally."
        ),
    }
    hist = [sys]
    for m in db_recent_messages(limit=last_n):
        role = "assistant" if m["who"].lower() == "alex" else "user"
        hist.append({"role": role, "content": m["text"]})
    return hist

def generate_alex_reply():
    """Call OpenAI and emit a reply as 'Alex' (with safe fallback)."""
    text_out = None
    try:
        if not (os.getenv("OPENAI_API_KEY") or "").strip():
            raise RuntimeError("Missing OPENAI_API_KEY")

        history = build_history()
        log.info("AI: generating with %d msgs", len(history))

        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=history,
            temperature=0.7,
            max_tokens=300,
        )
        text_out = (resp.choices[0].message.content or "").strip()
        if not text_out:
            text_out = "I’m here. Breathe with me."
    except Exception as e:
        log.exception("AI reply failed: %s", e)
        text_out = "I’m here. Let’s breathe, then try again."

    db_add_message("Alex", text_out)
    socketio.emit("new_message", {"who": "Alex", "text": text_out}, broadcast=True)

# ---------------- Routes ----------------
def authed() -> bool:
    return session.get("authed", False)

@app.route("/", methods=["GET"])
def home():
    if not authed():
        return redirect(url_for("login"))
    return render_template("chat.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == CHAT_PASSWORD:
            session["authed"] = True
            return redirect(url_for("home"))
        return render_template("login.html", error="Wrong password.")
    return render_template("login.html", error=None)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/health")
def health():
    return "ok", 200

# ---------------- Socket events ----------------
@socketio.on("connect")
def on_connect():
    if not authed():
        return False
    emit("init", {"messages": db_recent_messages()})

@socketio.on("send_message")
def on_send(data):
    if not authed():
        return
    text_in = (data or {}).get("text", "").strip()
    if not text_in:
        return
    speaker = (data or {}).get("who", "You")

    log.info("recv from %s: %r", speaker, text_in[:200])

    db_add_message(speaker, text_in)
    emit("new_message", {"who": speaker, "text": text_in}, broadcast=True)

    if speaker.lower() != "alex":
        socketio.start_background_task(generate_alex_reply)

# ---------------- Memories endpoints ----------------
@app.route("/mem/save", methods=["POST"])
def mem_save():
    if not authed():
        return ("", 403)
    note = (request.form.get("note") or "").strip()
    scope = (request.form.get("scope") or "global").strip()
    if not note:
        return ("", 204)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO memories (scope, note) VALUES (:s, :n)"),
            {"s": scope, "n": note}
        )
    return ("saved", 200)

@app.route("/mem/list", methods=["GET"])
def mem_list():
    if not authed():
        return ("", 403)
    scope = (request.args.get("scope") or "global").strip()
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT note, created_at FROM memories WHERE scope = :s ORDER BY id DESC LIMIT 100"),
            {"s": scope}
        ).mappings().all()
    return {
        "scope": scope,
        "notes": [{"note": r["note"], "ts": r["created_at"].isoformat()} for r in rows],
    }

# ---------------- Main ----------------
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
