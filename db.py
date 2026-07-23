"""Database access layer for the AS400 monitor.

Provides a ``run_query`` helper that executes a SQL statement against an IBM i
partition (LPAR) over ODBC and returns a list of dict rows.

The tool can monitor several LPARs at once. Each LPAR has its own cached
connection and lock, keyed by the LPAR ``key`` from :data:`Config.LPARS`.
A thread-local "current LPAR" lets request handlers and collectors target a
specific partition without threading the key through every call site::

    set_current_lpar("lpar2")
    run_query(SQL)            # runs against lpar2

The connection layer is intentionally small and swappable: if you prefer JDBC
(jaydebeapi) or another transport, you only need to reimplement ``run_query``.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from config import Config

# Per-LPAR cached connections and locks, keyed by LPAR key.
_connections: dict[str, Any] = {}
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

# Thread-local "current LPAR" selecting which partition queries target.
_local = threading.local()


class AS400Error(RuntimeError):
    """Raised when the AS400 cannot be queried."""


# --------------------------------------------------------------------------- #
# Current-LPAR selection (thread-local)
# --------------------------------------------------------------------------- #
def set_current_lpar(key: str | None) -> None:
    _local.lpar = key


def current_lpar() -> str:
    return getattr(_local, "lpar", None) or Config.default_lpar_key()


def _resolve(key: str | None) -> tuple[str, dict]:
    key = key or current_lpar()
    lpar = Config.get_lpar(key)
    if lpar is None:
        raise AS400Error(
            f"Unknown LPAR '{key}'. Configured: {Config.lpar_keys()}")
    return key, lpar


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def _list_drivers() -> list[str]:
    try:
        import pyodbc
        return list(pyodbc.drivers())
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Connection management (per LPAR)
# --------------------------------------------------------------------------- #
def _get_connection(key: str, lpar: dict):
    """Lazily create (and cache) a pyodbc connection for one LPAR."""
    conn = _connections.get(key)
    if conn is not None:
        return conn

    try:
        import pyodbc
    except ImportError as exc:
        raise AS400Error(
            "pyodbc is not installed. Run: pip install -r requirements.txt"
        ) from exc

    driver = lpar.get("driver") or Config.DRIVER
    drivers = _list_drivers()
    if driver not in drivers:
        raise AS400Error(
            f"ODBC driver '{driver}' not found. "
            f"Available drivers: {drivers or 'none'}. "
            "Install 'IBM i Access Client Solutions' (ACS) with the ODBC "
            "component, or set AS400_DRIVER to a driver listed above."
        )

    try:
        conn = pyodbc.connect(Config.connection_string_for(lpar), timeout=10,
                              autocommit=True)
        # NOTE: do NOT set conn.timeout here. On the IBM i Access ODBC driver
        # the query timeout is interpreted as a DB2-for-i *estimated* cost
        # threshold (QQRYTIMLMT), not a wall-clock limit. The optimizer raises
        # SQL0666 immediately for service views it estimates as expensive
        # (e.g. QSYS2.SUBSYSTEM_INFO), even when the query runs in well under a
        # second.
    except Exception as exc:
        raise AS400Error(f"Failed to connect to AS400 LPAR "
                         f"'{lpar.get('name', key)}' "
                         f"(host '{lpar.get('host', '')}'): {exc}") from exc
    _connections[key] = conn
    return conn


def _close_connection(key: str) -> None:
    """Close and drop the cached connection for an LPAR.

    Caller must hold that LPAR's lock.
    """
    conn = _connections.pop(key, None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def reset_connection(lpar: str | None = None) -> None:
    """Drop cached connection(s) so the next query reconnects.

    With no argument, resets every cached LPAR connection.
    """
    if lpar is None:
        keys = list(_connections.keys())
    else:
        key, _ = _resolve(lpar)
        keys = [key]
    for key in keys:
        with _lock_for(key):
            _close_connection(key)


def run_query(sql: str, params: tuple | None = None,
              lpar: str | None = None) -> list[dict[str, Any]]:
    """Execute ``sql`` against an LPAR and return rows as a list of dicts.

    ``lpar`` selects the partition; when omitted the thread-local current LPAR
    (or the default) is used. Raises :class:`AS400Error` on any failure.
    """
    key, lp = _resolve(lpar)
    with _lock_for(key):
        try:
            conn = _get_connection(key, lp)
            cursor = conn.cursor()
            cursor.execute(sql, params or ())
            columns = [col[0] for col in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            cursor.close()
            return rows
        except AS400Error:
            raise
        except Exception as exc:
            # Connection may be stale; drop it so the next call reconnects. We
            # already hold this LPAR's lock here, so close directly.
            _close_connection(key)
            raise AS400Error(f"Query failed: {exc}") from exc


def run_query_isolated(sql: str, params: tuple | None = None,
                       lpar: str | None = None) -> list[dict[str, Any]]:
    """Execute ``sql`` on a dedicated, short-lived connection.

    Unlike :func:`run_query`, this does NOT use a shared cached connection or
    a per-LPAR lock, so a slow query here will not block other collectors. Use
    it for heavy/background queries (e.g. scanning a large message queue).
    """
    _, lp = _resolve(lpar)
    try:
        import pyodbc
    except ImportError as exc:
        raise AS400Error(
            "pyodbc is not installed. Run: pip install -r requirements.txt"
        ) from exc

    driver = lp.get("driver") or Config.DRIVER
    if driver not in _list_drivers():
        raise AS400Error(f"ODBC driver '{driver}' not found.")

    conn = None
    try:
        conn = pyodbc.connect(Config.connection_string_for(lp), timeout=10,
                              autocommit=True)
        cursor = conn.cursor()
        cursor.execute(sql, params or ())
        columns = [col[0] for col in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        return rows
    except Exception as exc:
        raise AS400Error(f"Query failed: {exc}") from exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def run_query_isolated_sampled(reset_sql: str, sample_sql: str,
                               gap_seconds: float = 2.0,
                               reset_params: tuple | None = None,
                               sample_params: tuple | None = None,
                               lpar: str | None = None) -> list[dict[str, Any]]:
    """Two-pass interval query on a single dedicated connection.

    Runs ``reset_sql`` (which resets elapsed statistics), waits ``gap_seconds``,
    then runs ``sample_sql`` and returns its rows. Both statements execute on the
    SAME short-lived connection.

    IBM i services such as QSYS2.ACTIVE_JOB_INFO compute
    ``ELAPSED_CPU_PERCENTAGE`` over the interval since statistics were last reset
    *on the calling connection*. A single call on a fresh connection therefore
    reports cumulative-since-start CPU (dominated by long-running system jobs and
    unchanging between refreshes). Resetting, pausing, then re-sampling on the
    same connection yields the CPU consumed during the interval -- i.e. which
    jobs are hot *right now*.
    """
    _, lp = _resolve(lpar)
    try:
        import pyodbc
    except ImportError as exc:
        raise AS400Error(
            "pyodbc is not installed. Run: pip install -r requirements.txt"
        ) from exc

    driver = lp.get("driver") or Config.DRIVER
    if driver not in _list_drivers():
        raise AS400Error(f"ODBC driver '{driver}' not found.")

    conn = None
    try:
        conn = pyodbc.connect(Config.connection_string_for(lp), timeout=10,
                              autocommit=True)
        cursor = conn.cursor()
        # First pass: reset elapsed statistics on this connection.
        cursor.execute(reset_sql, reset_params or ())
        try:
            cursor.fetchall()
        except Exception:
            pass  # reset statement may not return a result set
        # Let jobs accumulate CPU over a measurable interval.
        time.sleep(max(0.0, gap_seconds))
        # Second pass: read the interval statistics.
        cursor.execute(sample_sql, sample_params or ())
        columns = [col[0] for col in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        return rows
    except Exception as exc:
        raise AS400Error(f"Query failed: {exc}") from exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def test_connection(lpar: str | None = None) -> dict[str, Any]:
    """Lightweight connectivity probe used by the /api/health endpoint."""
    if not Config.is_live():
        return {"ok": True, "mode": "mock", "message": "Running in mock mode"}
    try:
        key, lp = _resolve(lpar)
    except AS400Error as exc:
        return {"ok": False, "mode": "live", "error": str(exc)}
    try:
        run_query("SELECT 1 AS OK FROM SYSIBM.SYSDUMMY1", lpar=key)
        return {"ok": True, "mode": "live", "lpar": key,
                "name": lp["name"], "host": lp["host"]}
    except AS400Error as exc:
        return {"ok": False, "mode": "live", "lpar": key,
                "name": lp["name"], "host": lp["host"],
                "error": str(exc), "drivers": _list_drivers()}
