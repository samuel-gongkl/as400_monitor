"""LPAR CPU usage dashboard data layer (multi-LPAR).

Provides a persistent CPU history store (SQLite), a background sampler that
records real CPU utilization for every configured LPAR at a fixed interval, an
LPAR summary query, and helpers that compute the time series / histogram /
statistics consumed by the ``/api/lpar`` endpoint.

The sampler reads instantaneous CPU from ``QSYS2.SYSTEM_ACTIVITY_INFO`` (the
reliable live source on this IBM i version) for each LPAR and persists each
point to disk, keyed by LPAR, so the 24-hour view survives restarts and
accumulates over time. In mock mode it synthesises a plausible CPU curve per
LPAR instead.
"""
from __future__ import annotations

import math
import random
import sqlite3
import threading
import time
from typing import Any

from config import Config

_CPU_SQL = ("SELECT AVERAGE_CPU_UTILIZATION "
            "FROM TABLE(QSYS2.SYSTEM_ACTIVITY_INFO())")

_SUMMARY_SQL = """
SELECT HOST_NAME,
       PARTITION_NAME,
       PARTITION_ID,
       NUMBER_OF_PARTITIONS,
       CONFIGURED_CPUS,
       VIRTUAL_PROCESSORS,
       MAXIMUM_VIRTUAL_PROCESSORS,
       PROCESSING_CAPACITY,
       MAXIMUM_PROCESSING_CAPACITY,
       CPU_SHARING_ATTRIBUTE,
       MACHINE_TYPE,
       MACHINE_MODEL,
       SERIAL_NUMBER
FROM TABLE(QSYS2.SYSTEM_STATUS(RESET_STATISTICS => 'NO')) X
"""

_OS_SQL = ("SELECT OS_NAME, OS_VERSION, OS_RELEASE, TOTAL_MEMORY "
           "FROM SYSIBMADM.ENV_SYS_INFO")

_init_lock = threading.Lock()
_sampler_started = False


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(Config.LPAR_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _has_lpar_column(conn: sqlite3.Connection) -> bool:
    cur = conn.execute("PRAGMA table_info(cpu_samples)")
    cols = [r[1] for r in cur.fetchall()]
    return not cols or "lpar" in cols


def init_store() -> None:
    """Create the samples table if needed, migrating the legacy schema.

    The original single-LPAR schema was ``cpu_samples(ts PK, cpu)``. The
    multi-LPAR schema is ``cpu_samples(lpar, ts, cpu)`` with a composite
    primary key so several partitions can share the same timestamp. If an old
    table is found, its rows are migrated under the default LPAR key.
    """
    with _connect() as conn:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='cpu_samples'")
        exists = cur.fetchone() is not None

        if exists and not _has_lpar_column(conn):
            default_key = Config.default_lpar_key()
            conn.execute("ALTER TABLE cpu_samples RENAME TO cpu_samples_old")
            conn.execute(
                "CREATE TABLE cpu_samples ("
                "  lpar TEXT NOT NULL,"
                "  ts INTEGER NOT NULL,"        # epoch seconds
                "  cpu REAL NOT NULL,"
                "  PRIMARY KEY (lpar, ts)"
                ")"
            )
            conn.execute(
                "INSERT OR REPLACE INTO cpu_samples (lpar, ts, cpu) "
                "SELECT ?, ts, cpu FROM cpu_samples_old", (default_key,))
            conn.execute("DROP TABLE cpu_samples_old")
        else:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS cpu_samples ("
                "  lpar TEXT NOT NULL,"
                "  ts INTEGER NOT NULL,"
                "  cpu REAL NOT NULL,"
                "  PRIMARY KEY (lpar, ts)"
                ")"
            )


def record_sample(lpar_key: str, cpu_pct: float, ts: int | None = None) -> None:
    """Persist one CPU sample for an LPAR and prune past the retention window."""
    ts = int(ts if ts is not None else time.time())
    cutoff = ts - Config.LPAR_HISTORY_HOURS * 3600
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cpu_samples (lpar, ts, cpu) "
            "VALUES (?, ?, ?)",
            (lpar_key, ts, round(float(cpu_pct), 2)))
        conn.execute("DELETE FROM cpu_samples WHERE lpar = ? AND ts < ?",
                     (lpar_key, cutoff))


def get_samples(lpar_key: str, hours: int | None = None
                ) -> list[tuple[int, float]]:
    """Return ``(ts, cpu)`` rows for an LPAR within the window, oldest first."""
    hours = hours or Config.LPAR_HISTORY_HOURS
    cutoff = int(time.time()) - hours * 3600
    with _connect() as conn:
        cur = conn.execute(
            "SELECT ts, cpu FROM cpu_samples "
            "WHERE lpar = ? AND ts >= ? ORDER BY ts ASC",
            (lpar_key, cutoff))
        return [(int(r[0]), float(r[1])) for r in cur.fetchall()]


def _has_any_samples(lpar_key: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "SELECT 1 FROM cpu_samples WHERE lpar = ? LIMIT 1", (lpar_key,))
        return cur.fetchone() is not None


# --------------------------------------------------------------------------- #
# Live / mock CPU read
# --------------------------------------------------------------------------- #
def read_cpu(lpar_key: str) -> float:
    """Read current CPU % for an LPAR. Live via SYSTEM_ACTIVITY_INFO, else mock."""
    if Config.is_live():
        from db import run_query
        rows = run_query(_CPU_SQL, lpar=lpar_key)
        if rows:
            val = rows[0].get("AVERAGE_CPU_UTILIZATION")
            try:
                return round(float(val), 1)
            except (TypeError, ValueError):
                return 0.0
        return 0.0
    return _mock_cpu(lpar_key)


# Per-LPAR mock state so each synthetic curve drifts independently.
_mock_state: dict[str, float] = {}


def _mock_cpu(lpar_key: str) -> float:
    """Generate a plausible, gently drifting CPU value for mock mode."""
    base = _mock_state.get(lpar_key)
    if base is None:
        base = random.uniform(30.0, 60.0)
    base = base + random.uniform(-6, 6)
    base = max(15.0, min(82.0, base))
    _mock_state[lpar_key] = base
    spike = random.uniform(20, 45) if random.random() < 0.04 else 0.0
    return round(min(118.0, base + spike), 1)


# --------------------------------------------------------------------------- #
# Background sampler
# --------------------------------------------------------------------------- #
def _sampler_loop() -> None:
    while True:
        for lpar in Config.LPARS:
            try:
                record_sample(lpar["key"], read_cpu(lpar["key"]))
            except Exception:
                # Never let a transient query/connection error on one LPAR kill
                # the sampler or starve the others.
                pass
        time.sleep(max(5, Config.LPAR_SAMPLE_SECONDS))


def start_sampler() -> None:
    """Start the background CPU sampler exactly once per process."""
    global _sampler_started
    with _init_lock:
        if _sampler_started:
            return
        init_store()
        for lpar in Config.LPARS:
            if not _has_any_samples(lpar["key"]):
                _seed_mock_history(lpar["key"])
        t = threading.Thread(target=_sampler_loop, name="lpar-sampler",
                             daemon=True)
        t.start()
        _sampler_started = True


def _seed_mock_history(lpar_key: str) -> None:
    """In mock mode, backfill a day of synthetic samples for an instant demo."""
    if Config.is_live():
        return
    now = int(time.time())
    step = max(5, Config.LPAR_SAMPLE_SECONDS)
    span = Config.LPAR_HISTORY_HOURS * 3600
    offset = random.uniform(-8, 8)  # give each LPAR a slightly different load
    rows = []
    ts = now - span
    while ts < now:
        hour = time.localtime(ts).tm_hour
        # Daytime busier than overnight.
        daily = 30 + offset + 25 * math.sin((hour - 6) / 24 * 2 * math.pi)
        cpu = max(12.0, min(118.0, daily + random.uniform(-10, 12)
                            + (30 if random.random() < 0.02 else 0)))
        rows.append((lpar_key, ts, round(cpu, 1)))
        ts += step
    with _connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO cpu_samples (lpar, ts, cpu) "
            "VALUES (?, ?, ?)", rows)


# --------------------------------------------------------------------------- #
# Summary + derived analytics
# --------------------------------------------------------------------------- #
def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _mock_summary(lpar: dict, idx: int) -> dict[str, Any]:
    cpus = [2, 4, 2][idx % 3]
    return {
        "lpar": lpar["name"], "host": lpar["host"] or f"{lpar['key']}.mock",
        "partition_id": idx + 1, "partitions": len(Config.LPARS),
        "os": "IBM i 7.5", "configured_cpus": cpus,
        "virtual_processors": cpus, "max_virtual_processors": cpus + 2,
        "entitled_capacity": round(cpus * 0.6, 2), "max_capacity": float(cpus + 2),
        "sharing": "UNCAPPED", "machine": "9105-22A",
        "serial": f"MOCK{idx + 1:03d}", "total_memory_mb": 380160,
    }


def get_summary(lpar_key: str) -> dict[str, Any]:
    """Return LPAR / partition / processor identity fields for one LPAR."""
    lpar = Config.get_lpar(lpar_key) or Config.default_lpar()
    if not Config.is_live():
        idx = Config.lpar_keys().index(lpar_key) if lpar_key in Config.lpar_keys() else 0
        return _mock_summary(lpar, idx)
    from db import run_query
    rows = run_query(_SUMMARY_SQL, lpar=lpar_key)
    r = rows[0] if rows else {}
    os_str = ""
    total_mem = 0.0
    try:
        orows = run_query(_OS_SQL, lpar=lpar_key)
        if orows:
            o = orows[0]
            os_str = (f"{o.get('OS_NAME', 'IBM i')} "
                      f"{int(_f(o.get('OS_VERSION')))}."
                      f"{int(_f(o.get('OS_RELEASE')))}")
            total_mem = _f(o.get("TOTAL_MEMORY"))
    except Exception:
        pass
    machine = (f"{str(r.get('MACHINE_TYPE', '')).strip()}-"
               f"{str(r.get('MACHINE_MODEL', '')).strip()}").strip("-")
    return {
        "lpar": (lpar["name"]
                 or str(r.get("PARTITION_NAME", "")).strip()
                 or str(r.get("HOST_NAME", "")).strip()),
        "host": str(r.get("HOST_NAME", "")).strip() or lpar["host"],
        "partition_id": int(_f(r.get("PARTITION_ID"))),
        "partitions": int(_f(r.get("NUMBER_OF_PARTITIONS"))),
        "os": os_str,
        "configured_cpus": int(_f(r.get("CONFIGURED_CPUS"))),
        "virtual_processors": int(_f(r.get("VIRTUAL_PROCESSORS"))),
        "max_virtual_processors": int(_f(r.get("MAXIMUM_VIRTUAL_PROCESSORS"))),
        "entitled_capacity": round(_f(r.get("PROCESSING_CAPACITY")), 2),
        "max_capacity": round(_f(r.get("MAXIMUM_PROCESSING_CAPACITY")), 2),
        "sharing": str(r.get("CPU_SHARING_ATTRIBUTE", "")).strip(),
        "machine": machine,
        "serial": str(r.get("SERIAL_NUMBER", "")).strip(),
        "total_memory_mb": int(total_mem),
    }


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def compute_stats(samples: list[tuple[int, float]]) -> dict[str, Any]:
    """Aggregate statistics + alert counts over the sample window."""
    values = [c for _, c in samples]
    warn = Config.LPAR_WARN_PCT
    crit = Config.LPAR_CRIT_PCT
    interval_min = max(5, Config.LPAR_SAMPLE_SECONDS) / 60.0
    if not values:
        return {
            "count": 0, "avg": 0.0, "peak": 0.0, "min": 0.0,
            "p95": 0.0, "p99": 0.0, "std": 0.0,
            "warn_alerts": 0, "crit_alerts": 0,
            "minutes_over_warn": 0.0, "minutes_over_crit": 0.0,
        }
    avg = sum(values) / len(values)
    var = sum((v - avg) ** 2 for v in values) / len(values)
    over_warn = sum(1 for v in values if v >= warn)
    over_crit = sum(1 for v in values if v >= crit)
    return {
        "count": len(values),
        "avg": round(avg, 1),
        "peak": round(max(values), 1),
        "min": round(min(values), 1),
        "p95": round(_percentile(values, 95), 1),
        "p99": round(_percentile(values, 99), 1),
        "std": round(math.sqrt(var), 1),
        "warn_alerts": over_warn,
        "crit_alerts": over_crit,
        "minutes_over_warn": round(over_warn * interval_min, 1),
        "minutes_over_crit": round(over_crit * interval_min, 1),
    }


def compute_histogram(samples: list[tuple[int, float]], bin_size: int = 5
                      ) -> dict[str, Any]:
    """Bucket CPU values into a frequency distribution."""
    values = [c for _, c in samples]
    if not values:
        return {"labels": [], "counts": []}
    top = max(120.0, max(values))
    n_bins = int(math.ceil(top / bin_size))
    counts = [0] * n_bins
    for v in values:
        idx = min(n_bins - 1, int(v // bin_size))
        counts[idx] += 1
    labels = [f"{i * bin_size}-{(i + 1) * bin_size}" for i in range(n_bins)]
    return {"labels": labels, "counts": counts, "bin_size": bin_size}


def get_lpar_dashboard(lpar_key: str) -> dict[str, Any]:
    """Assemble the per-LPAR payload (samples + histogram + stats + summary)."""
    samples = get_samples(lpar_key)
    lpar = Config.get_lpar(lpar_key) or Config.default_lpar()
    try:
        summary = get_summary(lpar_key)
        ok = True
        error = None
    except Exception as exc:
        summary = {"lpar": lpar["name"], "host": lpar["host"]}
        ok = False
        error = str(exc)
    return {
        "key": lpar_key,
        "name": lpar["name"],
        "host": lpar["host"],
        "ok": ok,
        "error": error,
        "summary": summary,
        "samples": [{"t": ts * 1000, "cpu": cpu} for ts, cpu in samples],
        "histogram": compute_histogram(samples),
        "stats": compute_stats(samples),
    }


def get_dashboard() -> dict[str, Any]:
    """Assemble the full multi-LPAR payload for the ``/api/lpar`` endpoint."""
    return {
        "mode": Config.MODE,
        "thresholds": {
            "warning": Config.LPAR_WARN_PCT,
            "critical": Config.LPAR_CRIT_PCT,
            "cap": 100.0,
        },
        "sample_seconds": max(5, Config.LPAR_SAMPLE_SECONDS),
        "history_hours": Config.LPAR_HISTORY_HOURS,
        "lpars": [get_lpar_dashboard(l["key"]) for l in Config.LPARS],
    }
