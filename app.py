"""Flask application entry point for the AS400 monitor.

Routes:
  GET /                 -> dashboard HTML
  GET /lpar             -> LPAR CPU usage dashboard HTML
  GET /api/lpars        -> configured LPAR list + default
  GET /api/health       -> connectivity / mode status (honours ?lpar=)
  GET /api/metrics      -> all collectors' current data (honours ?lpar=)
  GET /api/metrics/<id> -> one collector's current data (honours ?lpar=)
  GET /api/lpar         -> per-LPAR CPU history + summary + stats (all LPARs)
"""
from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import urlparse

from flask import (Flask, jsonify, redirect, render_template, request,
                   session, url_for)

import collectors
import lpar
from config import Config
from db import set_current_lpar, test_connection
from notifications import start_alert_monitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

log = logging.getLogger("app")

app = Flask(__name__)
app.secret_key = Config.SECRET_KEY
app.permanent_session_lifetime = timedelta(minutes=Config.AUTH_SESSION_MINUTES)

if Config.AUTH_ENABLED and Config.AUTH_USING_DEFAULT_PASSWORD:
    log.warning("Authentication is using the built-in DEFAULT password "
                "(username=%r). Set APP_PASSWORD_HASH before production use.",
                Config.AUTH_USERNAME)
if Config.SECRET_KEY_IS_EPHEMERAL:
    log.warning("APP_SECRET_KEY is not set; using a per-run key. Sessions will "
                "not survive a restart. Set APP_SECRET_KEY for stable sessions.")

# Endpoints reachable without a session (plus Flask's static handler).
PUBLIC_ENDPOINTS = {"login", "static"}

# Begin recording CPU history as soon as the app is imported so the LPAR
# dashboard accumulates data even before anyone opens it.
lpar.start_sampler()

# Notification polling is opt-in and starts only when a channel is configured.
# It runs outside Flask request threads, so a slow SMTP or Teams endpoint never
# delays the dashboard.
start_alert_monitor()


def _is_authenticated() -> bool:
    return bool(session.get("authenticated"))


def _safe_next(target: str | None) -> str:
    """Only allow same-site relative redirects to avoid open-redirect abuse."""
    if not target:
        return url_for("index")
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/"):
        return url_for("index")
    return target


@app.before_request
def _require_login():
    """Gate every request behind the sign-on page when auth is enabled."""
    if not Config.AUTH_ENABLED:
        return None
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if _is_authenticated():
        return None
    # Unauthenticated: JSON 401 for API calls, redirect to sign-on for pages.
    if request.path.startswith("/api/"):
        return jsonify({"error": "authentication required"}), 401
    return redirect(url_for("login", next=request.full_path
                            if request.query_string else request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    # Already signed in -> go straight through.
    if _is_authenticated():
        return redirect(_safe_next(request.args.get("next")))

    error = None
    username = ""
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        next_url = _safe_next(request.form.get("next"))
        if Config.verify_credentials(username, password):
            session.clear()
            session.permanent = True
            session["authenticated"] = True
            session["username"] = username.strip()
            log.info("User %r signed in", username.strip())
            return redirect(next_url)
        error = "Invalid username or password. Please try again."
        log.warning("Failed sign-in attempt for username %r", username.strip())

    return render_template(
        "login.html",
        error=error,
        username=username,
        mode=Config.MODE,
        next_url=_safe_next(request.values.get("next")),
        default_password_warning=(Config.AUTH_ENABLED
                                  and Config.AUTH_USING_DEFAULT_PASSWORD),
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def _selected_lpar() -> str:
    """Resolve the LPAR key from ?lpar=, falling back to the default."""
    key = request.args.get("lpar")
    if key and Config.get_lpar(key) is not None:
        return key
    return Config.default_lpar_key()


@app.route("/")
def index():
    cards = [{"id": c.id, "title": c.title, "kind": c.kind}
             for c in collectors.all_collectors()]
    return render_template(
        "index.html",
        cards=cards,
        refresh_seconds=Config.REFRESH_SECONDS,
        mode=Config.MODE,
        lpars=Config.lpar_choices(),
        default_lpar=Config.default_lpar_key(),
        auth_enabled=Config.AUTH_ENABLED,
        username=session.get("username"),
    )


@app.route("/lpar")
def lpar_page():
    return render_template(
        "lpar.html",
        refresh_seconds=max(Config.REFRESH_SECONDS, 15),
        mode=Config.MODE,
        lpars=Config.lpar_choices(),
        auth_enabled=Config.AUTH_ENABLED,
        username=session.get("username"),
    )


@app.route("/ai-agent")
def ai_agent_page():
    default_url = (
        "https://m365.cloud.microsoft/chat/"
        "?titleId=T_bac22389-501c-acb6-6c8d-ffc3550ffae7"
        "&source=agentCenterDialog"
    )
    # Edit each entry's "url" to point to its own agent. They currently all
    # use the same M365 chat link supplied for the project.
    agents = [
        {"name": "Agent 1", "desc": "General AS400 assistant", "url": default_url},
        {"name": "Agent 2", "desc": "LPAR / performance helper", "url": default_url},
        {"name": "Agent 3", "desc": "Job & message analysis", "url": default_url},
        {"name": "Agent 4", "desc": "Storage & housekeeping", "url": default_url},
    ]
    return render_template("ai_agent.html", agents=agents,
                           auth_enabled=Config.AUTH_ENABLED,
                           username=session.get("username"))


@app.route("/api/lpars")
def api_lpars():
    return jsonify({"lpars": Config.lpar_choices(),
                    "default": Config.default_lpar_key()})


@app.route("/api/health")
def api_health():
    return jsonify(test_connection(_selected_lpar()))


@app.route("/api/metrics")
def api_metrics():
    lpar_key = _selected_lpar()
    set_current_lpar(lpar_key)
    return jsonify({"mode": Config.MODE, "lpar": lpar_key,
                    "metrics": collectors.collect_all(lpar_key)})


@app.route("/api/metrics/<collector_id>")
def api_metric(collector_id: str):
    collector = collectors.get_collector(collector_id)
    if collector is None:
        return jsonify({"error": f"Unknown collector '{collector_id}'"}), 404
    set_current_lpar(_selected_lpar())
    return jsonify(collector.collect())


@app.route("/api/lpar")
def api_lpar():
    try:
        return jsonify({"ok": True, **lpar.get_dashboard()})
    except Exception as exc:  # never break the page on a transient failure
        return jsonify({"ok": False, "error": str(exc)}), 200


if __name__ == "__main__":
    print(f"AS400 monitor starting in '{Config.MODE}' mode "
          f"on http://{Config.FLASK_HOST}:{Config.FLASK_PORT}")
    # debug/reloader is opt-in: the reloader spawns a second process which
    # would duplicate the background collector refresh threads. Keep it off
    # unless FLASK_DEBUG is explicitly enabled.
    app.run(host=Config.FLASK_HOST, port=Config.FLASK_PORT,
            debug=Config.FLASK_DEBUG, use_reloader=Config.FLASK_DEBUG,
            threaded=True)
