"""Configuration loading for the AS400 monitor.

Reads settings from environment variables (optionally from a local .env file).
"""
import os
import secrets

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv is optional at runtime
    pass


def _get_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _lpar_entry(idx, host, name, user, password, driver, ssl):
    return {
        "key": f"lpar{idx}",
        "name": (name or host or f"LPAR {idx}"),
        "host": host,
        "user": user,
        "password": password,
        "driver": driver,
        "ssl": ssl,
    }


def _load_lpars():
    """Build the list of LPAR definitions to monitor.

    Primary source is numbered env vars (one block per LPAR)::

        AS400_LPAR1_HOST=...   AS400_LPAR1_USER=...   AS400_LPAR1_PASSWORD=...
        AS400_LPAR1_NAME=...   AS400_LPAR1_DRIVER=... AS400_LPAR1_SSL=...

    If none are defined we fall back to the legacy single-host variables
    (``AS400_HOST`` / ``AS400_USER`` / ``AS400_PASSWORD``) as one LPAR, so
    existing single-partition setups keep working unchanged.
    """
    mode = os.getenv("AS400_MODE", "live").strip().lower()
    driver_default = os.getenv("AS400_DRIVER", "IBM i Access ODBC Driver")
    ssl_default = _get_bool("AS400_SSL", False)

    lpars = []
    idx = 1
    while True:
        host = os.getenv(f"AS400_LPAR{idx}_HOST")
        if not host:
            break
        lpars.append(_lpar_entry(
            idx,
            host.strip(),
            (os.getenv(f"AS400_LPAR{idx}_NAME") or "").strip(),
            (os.getenv(f"AS400_LPAR{idx}_USER") or "").strip(),
            os.getenv(f"AS400_LPAR{idx}_PASSWORD") or "",
            (os.getenv(f"AS400_LPAR{idx}_DRIVER") or driver_default).strip(),
            _get_bool(f"AS400_LPAR{idx}_SSL", ssl_default),
        ))
        idx += 1

    if not lpars:
        # Legacy single-host fallback (still one LPAR under the new model).
        host = os.getenv("AS400_HOST", "").strip()
        if host or mode == "live":
            lpars.append(_lpar_entry(
                1, host,
                (os.getenv("AS400_NAME") or "").strip(),
                os.getenv("AS400_USER", "").strip(),
                os.getenv("AS400_PASSWORD", ""),
                driver_default, ssl_default,
            ))

    # In mock mode, synthesise a few LPARs so the multi-LPAR UI can be
    # explored without any real credentials configured.
    if mode == "mock" and len(lpars) <= 1:
        lpars = [
            _lpar_entry(1, "hkprd01.mock", "HKPRD01", "", "",
                        driver_default, ssl_default),
            _lpar_entry(2, "hkprd02.mock", "HKPRD02", "", "",
                        driver_default, ssl_default),
            _lpar_entry(3, "hkdev01.mock", "HKDEV01", "", "",
                        driver_default, ssl_default),
        ]

    if not lpars:
        # Absolute fallback so the app never has an empty registry.
        lpars.append(_lpar_entry(1, "", "", "", "", driver_default,
                                 ssl_default))
    return lpars


class Config:
    # "live" or "mock"
    MODE = os.getenv("AS400_MODE", "live").strip().lower()

    # ODBC connection
    DRIVER = os.getenv("AS400_DRIVER", "IBM i Access ODBC Driver")

    # Registry of LPARs to monitor (see _load_lpars). Always has >= 1 entry.
    LPARS = _load_lpars()

    # Legacy single-host attributes kept for backward compatibility; they
    # mirror the first/default LPAR so older code paths and messages still work.
    HOST = LPARS[0]["host"]
    USER = LPARS[0]["user"]
    PASSWORD = LPARS[0]["password"]
    SSL = LPARS[0]["ssl"]

    # UI / server
    REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "10"))
    FLASK_HOST = os.getenv("FLASK_HOST", "127.0.0.1")
    FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))
    FLASK_DEBUG = _get_bool("FLASK_DEBUG", False)

    # ------------------------------------------------------------------ #
    # Authentication (sign-on page)
    # ------------------------------------------------------------------ #
    # Whether the sign-on page protects the app. Disable only for local dev.
    AUTH_ENABLED = _get_bool("APP_AUTH_ENABLED", True)
    # Flask session signing key. Set APP_SECRET_KEY to a long random string so
    # sessions survive restarts; otherwise a per-run key is generated.
    SECRET_KEY = os.getenv("APP_SECRET_KEY") or secrets.token_hex(32)
    SECRET_KEY_IS_EPHEMERAL = not bool(os.getenv("APP_SECRET_KEY"))
    # Sign-on account. Provide APP_PASSWORD_HASH (a werkzeug hash) in production;
    # APP_PASSWORD (plaintext) is accepted for convenience in dev. If neither is
    # set, a default password is used so the app runs out of the box.
    AUTH_USERNAME = (os.getenv("APP_USERNAME") or "admin").strip()
    AUTH_PASSWORD = os.getenv("APP_PASSWORD")
    AUTH_PASSWORD_HASH = os.getenv("APP_PASSWORD_HASH")
    # Session inactivity lifetime in minutes.
    AUTH_SESSION_MINUTES = int(os.getenv("APP_SESSION_MINUTES", "60"))
    # Set when falling back to the built-in default password (dev only).
    AUTH_USING_DEFAULT_PASSWORD = not (AUTH_PASSWORD or AUTH_PASSWORD_HASH)

    # ------------------------------------------------------------------ #
    # Notifications (disabled by default)
    # ------------------------------------------------------------------ #
    # The monitor polls normalized collector data in a daemon thread and sends
    # only state changes (plus optional reminders), never from a web request.
    NOTIFY_ENABLED = _get_bool("NOTIFY_ENABLED", False)
    NOTIFY_INTERVAL_SECONDS = max(5, int(os.getenv("NOTIFY_INTERVAL_SECONDS", "60")))
    NOTIFY_REPEAT_MINUTES = max(0, int(os.getenv("NOTIFY_REPEAT_MINUTES", "0")))
    NOTIFY_HTTP_TIMEOUT_SECONDS = max(1, int(os.getenv("NOTIFY_HTTP_TIMEOUT_SECONDS", "10")))

    NOTIFY_CPU_WARN_PCT = float(os.getenv("NOTIFY_CPU_WARN_PCT", "75"))
    NOTIFY_CPU_CRIT_PCT = float(os.getenv("NOTIFY_CPU_CRIT_PCT", "90"))
    NOTIFY_ASP_WARN_PCT = float(os.getenv("NOTIFY_ASP_WARN_PCT", "80"))
    NOTIFY_ASP_CRIT_PCT = float(os.getenv("NOTIFY_ASP_CRIT_PCT", "90"))

    NOTIFY_EMAIL_ENABLED = _get_bool("NOTIFY_EMAIL_ENABLED", False)
    NOTIFY_EMAIL_SMTP_HOST = os.getenv("NOTIFY_EMAIL_SMTP_HOST", "").strip()
    NOTIFY_EMAIL_SMTP_PORT = int(os.getenv("NOTIFY_EMAIL_SMTP_PORT", "587"))
    NOTIFY_EMAIL_USER = os.getenv("NOTIFY_EMAIL_USER", "").strip()
    NOTIFY_EMAIL_PASSWORD = os.getenv("NOTIFY_EMAIL_PASSWORD", "")
    NOTIFY_EMAIL_FROM = os.getenv("NOTIFY_EMAIL_FROM", "").strip()
    NOTIFY_EMAIL_TO = os.getenv("NOTIFY_EMAIL_TO", "")
    NOTIFY_EMAIL_USE_TLS = _get_bool("NOTIFY_EMAIL_USE_TLS", True)
    NOTIFY_EMAIL_USE_SSL = _get_bool("NOTIFY_EMAIL_USE_SSL", False)

    NOTIFY_TEAMS_ENABLED = _get_bool("NOTIFY_TEAMS_ENABLED", False)
    NOTIFY_TEAMS_WEBHOOK_URL = os.getenv("NOTIFY_TEAMS_WEBHOOK_URL", "").strip()

    # LPAR CPU history dashboard
    # How often the background sampler records a CPU data point (seconds).
    LPAR_SAMPLE_SECONDS = int(os.getenv("LPAR_SAMPLE_SECONDS", "30"))
    # How much history to retain / display (hours).
    LPAR_HISTORY_HOURS = int(os.getenv("LPAR_HISTORY_HOURS", "24"))
    # CPU % thresholds for warning / critical highlighting.
    LPAR_WARN_PCT = float(os.getenv("LPAR_WARN_PCT", "70"))
    LPAR_CRIT_PCT = float(os.getenv("LPAR_CRIT_PCT", "85"))
    # Where to persist sampled CPU history.
    LPAR_DB_PATH = os.getenv("LPAR_DB_PATH",
                             os.path.join(os.path.dirname(__file__),
                                          "cpu_history.db"))

    @classmethod
    def connection_string_for(cls, lpar: dict) -> str:
        parts = [
            f"DRIVER={{{lpar.get('driver') or cls.DRIVER}}}",
            f"SYSTEM={lpar.get('host', '')}",
            f"UID={lpar.get('user', '')}",
            f"PWD={lpar.get('password', '')}",
        ]
        if lpar.get("ssl"):
            parts.append("SSL=1")
        return ";".join(parts) + ";"

    @classmethod
    def connection_string(cls) -> str:
        return cls.connection_string_for(cls.default_lpar())

    @classmethod
    def default_lpar(cls) -> dict:
        return cls.LPARS[0]

    @classmethod
    def default_lpar_key(cls) -> str:
        return cls.LPARS[0]["key"]

    @classmethod
    def get_lpar(cls, key: str) -> "dict | None":
        for lpar in cls.LPARS:
            if lpar["key"] == key:
                return lpar
        return None

    @classmethod
    def lpar_keys(cls) -> list:
        return [lpar["key"] for lpar in cls.LPARS]

    @classmethod
    def lpar_choices(cls) -> list:
        """Lightweight list for the UI: key + display name + host."""
        return [{"key": l["key"], "name": l["name"], "host": l["host"]}
                for l in cls.LPARS]

    @classmethod
    def is_live(cls) -> bool:
        return cls.MODE == "live"

    # ------------------------------------------------------------------ #
    # Authentication helpers
    # ------------------------------------------------------------------ #
    # Built-in default password used only when no APP_PASSWORD/APP_PASSWORD_HASH
    # is configured, so the app is usable on first run.
    _DEFAULT_PASSWORD = "admin"

    @classmethod
    def verify_credentials(cls, username: str, password: str) -> bool:
        """Return True if the supplied sign-on credentials are valid.

        Username check is case-insensitive. Password is validated against
        APP_PASSWORD_HASH (preferred), then APP_PASSWORD, then the built-in
        default. Uses constant-time comparisons where possible.
        """
        from werkzeug.security import check_password_hash

        username = (username or "").strip()
        password = password or ""
        if not secrets.compare_digest(username.lower(),
                                      cls.AUTH_USERNAME.lower()):
            return False

        if cls.AUTH_PASSWORD_HASH:
            try:
                return check_password_hash(cls.AUTH_PASSWORD_HASH, password)
            except Exception:
                return False
        expected = cls.AUTH_PASSWORD or cls._DEFAULT_PASSWORD
        return secrets.compare_digest(password, expected)
