"""System status collector: CPU, job counts, and storage utilization.

Uses the IBM i SQL service QSYS2.SYSTEM_STATUS, which returns one row of
overall partition statistics.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from typing import Any

from collectors.base import Collector, register
from db import run_query

log = logging.getLogger(__name__)

SQL = """
SELECT AVERAGE_CPU_UTILIZATION,
       ELAPSED_CPU_USED,
       CONFIGURED_CPUS,
       TOTAL_JOBS_IN_SYSTEM,
       ACTIVE_JOBS_IN_SYSTEM,
       MAXIMUM_JOBS_IN_SYSTEM,
       SYSTEM_ASP_USED,
       SYSTEM_ASP_STORAGE,
       TOTAL_AUXILIARY_STORAGE,
       CURRENT_TEMPORARY_STORAGE,
       MAXIMUM_TEMPORARY_STORAGE_USED
FROM TABLE(QSYS2.SYSTEM_STATUS(RESET_STATISTICS => 'NO')) X
"""

# QSYS2.SYSTEM_ACTIVITY_INFO samples CPU over a ~1 second interval and returns
# a current utilization figure. It is the reliable source for "live" CPU:
# SYSTEM_STATUS.AVERAGE_CPU_UTILIZATION reports 0 on some IBM i versions.
CPU_SQL = "SELECT AVERAGE_CPU_UTILIZATION FROM TABLE(QSYS2.SYSTEM_ACTIVITY_INFO())"

# Uptime = now - the partition's last IPL (boot) time.
#
# Getting a reliable IPL timestamp across IBM i releases is fiddly: the SCPF
# system job's JOB_ENTERED_SYSTEM_TIME is NULL on some systems. The robust,
# release-independent signal is the EARLIEST entry time among all active jobs:
# the system jobs and subsystem monitors all start at IPL and never end, so the
# minimum JOB_ENTERED_SYSTEM_TIME across active jobs equals the IPL time.
#
# We try, in order, and use the first that yields a non-NULL timestamp:
#   1. Earliest active-job entry time (cheap, default detail).
#   2. SCPF's own start time with DETAILED_INFO => 'ALL' (populates timestamps
#      that are NULL in the default, lightweight result set).
#   3. Earliest active-job entry time with DETAILED_INFO => 'ALL' (heavier, but
#      guarantees the timestamp column is populated).
# The boot time is cached after the first success, so the heavy variants run at
# most once.
UPTIME_SQLS = (
    "SELECT MIN(JOB_ENTERED_SYSTEM_TIME) AS IPL_TIME "
    "FROM TABLE(QSYS2.ACTIVE_JOB_INFO()) X",

    "SELECT JOB_ENTERED_SYSTEM_TIME AS IPL_TIME "
    "FROM TABLE(QSYS2.ACTIVE_JOB_INFO(DETAILED_INFO => 'ALL')) X "
    "WHERE JOB_NAME LIKE '%/QSYS/SCPF' "
    "AND JOB_ENTERED_SYSTEM_TIME IS NOT NULL "
    "FETCH FIRST 1 ROW ONLY",

    "SELECT MIN(JOB_ENTERED_SYSTEM_TIME) AS IPL_TIME "
    "FROM TABLE(QSYS2.ACTIVE_JOB_INFO(DETAILED_INFO => 'ALL')) X",
)

# Fixed reference so mock uptime looks realistic and increments in real time.
_MOCK_BOOT_TS = time.time() - (12 * 86400 + 3 * 3600 + 27 * 60)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _format_uptime(seconds: float) -> str:
    """Render a duration in seconds as e.g. '12d 3h 27m'."""
    total = int(max(0, seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h {mins}m"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _coerce_datetime(value: Any) -> "datetime | None":
    """Best-effort conversion of a DB timestamp to a datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d-%H.%M.%S.%f", "%Y-%m-%d-%H.%M.%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


@register
class SystemStatusCollector(Collector):
    id = "system"
    title = "System Status"
    kind = "kpi"

    def __init__(self) -> None:
        # The IPL/boot time is constant until the next IPL, so once we discover
        # it we cache it and just recompute (now - boot) on every refresh. This
        # also means the heavier discovery queries run at most once.
        #
        # This collector is a singleton shared across all LPARs, so the cache is
        # keyed by LPAR: switching partitions must not reuse another LPAR's boot
        # time.
        self._boot_times: "dict[str, datetime]" = {}

    def fetch_live(self) -> dict[str, Any]:
        rows = run_query(SQL)
        if not rows:
            raise RuntimeError("QSYS2.SYSTEM_STATUS returned no rows")
        r = rows[0]
        cpu = self._live_cpu(r)
        uptime_secs = self._live_uptime()
        return {
            "cpu_pct": cpu,
            "configured_cpus": _to_float(r.get("CONFIGURED_CPUS")),
            "total_jobs": int(_to_float(r.get("TOTAL_JOBS_IN_SYSTEM"))),
            "active_jobs": int(_to_float(r.get("ACTIVE_JOBS_IN_SYSTEM"))),
            "max_jobs": int(_to_float(r.get("MAXIMUM_JOBS_IN_SYSTEM"))),
            "asp_used_pct": round(_to_float(r.get("SYSTEM_ASP_USED")), 1),
            "asp_storage_gb": round(
                _to_float(r.get("SYSTEM_ASP_STORAGE")) / 1024 / 1024, 1),
            "tmp_storage_mb": int(_to_float(r.get("CURRENT_TEMPORARY_STORAGE"))),
            "uptime": _format_uptime(uptime_secs) if uptime_secs is not None else "n/a",
            "uptime_seconds": int(uptime_secs) if uptime_secs is not None else None,
        }

    def _live_uptime(self) -> "float | None":
        """Return uptime in seconds for the current LPAR, or None if unknown."""
        from db import current_lpar
        lpar_key = current_lpar()
        boot = self._boot_times.get(lpar_key)
        if boot is None:
            boot = self._discover_boot_time(lpar_key)
            if boot is not None:
                self._boot_times[lpar_key] = boot
        if boot is None:
            return None
        try:
            delta = datetime.now() - boot
        except Exception:
            return None
        return max(0.0, delta.total_seconds())

    def _discover_boot_time(self, lpar_key: str = "?") -> "datetime | None":
        """Query IBM i for the last IPL (boot) time, trying each strategy."""
        last_error: str | None = None
        for sql in UPTIME_SQLS:
            try:
                rows = run_query(sql)
            except Exception as exc:
                last_error = str(exc)
                continue
            if not rows:
                last_error = "query returned no rows"
                continue
            raw = rows[0].get("IPL_TIME")
            boot = _coerce_datetime(raw)
            if boot is None:
                last_error = f"could not parse IPL_TIME={raw!r}"
                continue
            log.info("resolved IPL/boot time for LPAR %s: %s", lpar_key, boot)
            return boot
        if last_error:
            log.warning("uptime lookup failed for LPAR %s: %s", lpar_key, last_error)
        return None

    def _live_cpu(self, status_row: dict[str, Any]) -> float:
        """Return current CPU %, preferring the 1-second sampled figure.

        SYSTEM_STATUS.AVERAGE_CPU_UTILIZATION reports 0 on some IBM i versions,
        so use QSYS2.SYSTEM_ACTIVITY_INFO (samples over ~1s). Fall back to
        SYSTEM_STATUS.ELAPSED_CPU_USED, then AVERAGE_CPU_UTILIZATION.
        """
        try:
            rows = run_query(CPU_SQL)
            if rows:
                return round(_to_float(rows[0].get("AVERAGE_CPU_UTILIZATION")), 1)
        except Exception:
            pass
        elapsed = _to_float(status_row.get("ELAPSED_CPU_USED"))
        if elapsed > 0:
            return round(elapsed, 1)
        return round(_to_float(status_row.get("AVERAGE_CPU_UTILIZATION")), 1)

    def fetch_mock(self) -> dict[str, Any]:
        # Vary the synthetic boot time per LPAR so switching partitions shows a
        # visibly different uptime (mirrors the per-LPAR behaviour in live mode).
        from db import current_lpar
        offset = (hash(current_lpar()) % 9) * 86400  # 0-8 extra days per LPAR
        uptime_secs = time.time() - _MOCK_BOOT_TS + offset
        return {
            "cpu_pct": round(random.uniform(5, 85), 1),
            "configured_cpus": 4,
            "total_jobs": random.randint(1200, 1500),
            "active_jobs": random.randint(180, 260),
            "max_jobs": 163520,
            "asp_used_pct": round(random.uniform(55, 78), 1),
            "asp_storage_gb": 1024.0,
            "tmp_storage_mb": random.randint(2000, 6000),
            "uptime": _format_uptime(uptime_secs),
            "uptime_seconds": int(uptime_secs),
        }
