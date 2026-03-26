import os
import json
import threading
from datetime import datetime, timedelta
from flask import (Flask, render_template, request, jsonify,
                   session, redirect, url_for, Response, stream_with_context)
from functools import wraps

from core.config import FLASK_SECRET_KEY
from core.database import init_db, db_get_all_trades, db_get_performance_summary
from core.monitor import trade_monitors, monitor_lock, monitor_log, run_trade_monitor
from core.database import db_get_active_monitors
from core.auth import check_token, check_rate_limit, record_failed_attempt
from agent.loop import run_agent_streaming

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

init_db()

# ── Start monitor thread (also started by scheduler; env flag lets you run
#    it here too when the scheduler process is not deployed) ──────────────
if os.getenv("RUN_MONITOR_IN_WEB", "true").lower() == "true":
    threading.Thread(target=run_trade_monitor, daemon=True).start()


# ── Auth ─────────────────────────────────────────────────────

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
        ip       = request.remote_addr or "unknown"
        password = request.form.get("password", "")
        if not check_rate_limit(ip):
            error = "Too many failed attempts. Try again in 15 minutes."
        elif check_token(password):
            session["authenticated"] = True
            session.permanent        = True
            return redirect(url_for("index"))
        else:
            record_failed_attempt(ip)
            error = "Invalid password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Routes ────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
@login_required
def chat():
    data         = request.json
    history      = data.get("history", [])
    user_message = data.get("message", "")
    images       = data.get("images", [])

    if not user_message and not images:
        return jsonify({"error": "No message provided"}), 400

    if images:
        user_content = []
        for img in images:
            user_content.append({
                "type": "image",
                "source": {
                    "type":       "base64",
                    "media_type": img.get("media_type", "image/jpeg"),
                    "data":       img.get("base64", ""),
                }
            })
        user_content.append({
            "type": "text",
            "text": user_message or "Analyze this image and give me your trading take.",
        })
    else:
        user_content = user_message

    history.append({"role": "user", "content": user_content})

    def generate():
        try:
            yield from run_agent_streaming(history)
        except Exception as e:
            yield f"data: {json.dumps({'text': f'Error: {str(e)}'})}\n\n"
            yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Access-Control-Allow-Origin": "*"},
    )


@app.route("/monitor")
@login_required
def monitor_status():
    active = db_get_active_monitors()
    active_dict = {}
    for row in active:
        symbol = row["symbol"]
        try:
            entry_time = datetime.fromisoformat(row["entry_time"])
        except Exception:
            entry_time = datetime.now()
        active_dict[symbol] = {
            **row,
            "elapsed_min": round((datetime.now() - entry_time).total_seconds() / 60, 1),
        }
    with monitor_lock:
        log = list(monitor_log[-20:])
    return jsonify({"active_monitors": active_dict, "recent_log": log})


@app.route("/trades")
@login_required
def trades_dashboard():
    return render_template("trades.html",
                           trades=db_get_all_trades(),
                           summary=db_get_performance_summary())


@app.route("/api/trades")
@login_required
def api_trades():
    return jsonify({"trades": db_get_all_trades(), "summary": db_get_performance_summary()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"FinSight running at http://0.0.0.0:{port}")
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
