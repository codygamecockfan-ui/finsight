"""
FinSight Flask app — slim version.
All business logic lives in core/ and agent/ modules.
This file only handles: auth, routes, Flask setup.
"""
import os
import hashlib
import threading
from datetime import datetime
from flask import (
    Flask, render_template, request, jsonify, session,
    redirect, url_for, Response, stream_with_context
)
from functools import wraps
from dotenv import load_dotenv

from agent.loop     import run_agent_streaming
from core.database  import init_db, db_get_all_trades, db_get_performance_summary
from core.monitor   import run_trade_monitor, trade_monitors, monitor_lock, monitor_log

load_dotenv()
init_db()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(24))


# ─────────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────────
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        password     = request.form.get("password", "")
        app_password = os.getenv("APP_PASSWORD", "")
        if app_password and hash_password(password) == hash_password(app_password):
            session["authenticated"] = True
            session.permanent = False
            return redirect(url_for("index"))
        error = "Invalid password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─────────────────────────────────────────────
#  FLASK ROUTES
# ─────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
@login_required
def chat():
    import json as _json
    data         = request.json
    history      = data.get("history", [])
    user_message = data.get("message", "")
    images       = data.get("images", [])  # [{base64, media_type}]

    if not user_message and not images:
        return jsonify({"error": "No message provided"}), 400

    # Build user content — text + optional images
    if images:
        user_content = []
        for img in images:
            user_content.append({
                "type": "image",
                "source": {
                    "type":       "base64",
                    "media_type": img.get("media_type", "image/jpeg"),
                    "data":       img.get("base64", "")
                }
            })
        user_content.append({
            "type": "text",
            "text": user_message or "Analyze this image and give me your trading take."
        })
    else:
        user_content = user_message

    history.append({"role": "user", "content": user_content})

    def generate():
        try:
            yield from run_agent_streaming(history)
        except Exception as e:
            yield f"data: {_json.dumps({'text': f'Error: {str(e)}'})}\n\n"
            yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*"
        }
    )


@app.route("/monitor")
@login_required
def monitor_status():
    with monitor_lock:
        return jsonify({
            "active_monitors": {
                k: {**v,
                    "entry_time":  v["entry_time"].isoformat(),
                    "elapsed_min": round((datetime.now() - v["entry_time"]).total_seconds() / 60, 1)}
                for k, v in trade_monitors.items()
            },
            "recent_log": list(monitor_log[-20:])
        })


@app.route("/trades")
@login_required
def trades_dashboard():
    trades  = db_get_all_trades()
    summary = db_get_performance_summary()
    return render_template("trades.html", trades=trades, summary=summary)


@app.route("/api/trades")
@login_required
def api_trades():
    return jsonify({
        "trades":  db_get_all_trades(),
        "summary": db_get_performance_summary()
    })


# ─────────────────────────────────────────────
#  BACKGROUND MONITOR THREAD
# ─────────────────────────────────────────────
if os.getenv("RUN_MONITOR_IN_WEB", "true").lower() == "true":
    monitor_thread = threading.Thread(target=run_trade_monitor, daemon=True)
    monitor_thread.start()


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"FinSight running at http://0.0.0.0:{port}")
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
