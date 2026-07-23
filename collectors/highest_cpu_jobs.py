"""Highest CPU jobs collector.

Lists the five active jobs currently consuming the most CPU, using the IBM i
SQL service QSYS2.ACTIVE_JOB_INFO. With DETAILED_INFO => 'ALL' the service
gathers CPU/storage detail for every active job, which can take several seconds
on a busy partition, so this collector extends BackgroundTableCollector to
refresh on a background thread and serve a cached snapshot -- keeping the
dashboard responsive.
"""
from __future__ import annotations

import random
from typing import Any

from collectors.base import BackgroundTableCollector, register
from db import run_query_isolated_sampled

TOP_N = 5

# Reset elapsed CPU statistics on the connection, then (after a short pause)
# sample again. ELAPSED_CPU_PERCENTAGE is only meaningful across a reset +
# re-sample on the same connection; otherwise it reports cumulative-since-start
# CPU and the same long-running jobs always top the list. See
# db.run_query_isolated_sampled for details.
RESET_SQL = """
SELECT COUNT(*) AS N
FROM TABLE(QSYS2.ACTIVE_JOB_INFO(RESET_STATISTICS => 'YES')) X
"""

SAMPLE_SQL = """
SELECT JOB_NAME,
       SUBSYSTEM,
       JOB_TYPE,
       JOB_STATUS,
       AUTHORIZATION_NAME,
       ELAPSED_CPU_PERCENTAGE,
       TEMPORARY_STORAGE
FROM TABLE(QSYS2.ACTIVE_JOB_INFO(
        RESET_STATISTICS => 'NO')) X
ORDER BY ELAPSED_CPU_PERCENTAGE DESC
FETCH FIRST 5 ROWS ONLY
"""

# Seconds to let jobs accumulate CPU between the reset and the sample.
SAMPLE_GAP_SECONDS = 3.0


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@register
class HighestCpuJobsCollector(BackgroundTableCollector):
    id = "cpu_jobs"
    title = "Highest CPU Jobs"
    columns = ["Job", "Subsystem", "Type", "Status", "User", "CPU %", "Tmp (MB)"]
    ttl_seconds = 45  # detailed scan of all jobs is heavy; refresh ~45s

    # Per-job elapsed CPU% thresholds for row colouring.
    CPU_ALERT = 25.0
    CPU_WARN = 10.0

    def row_level(self, row: list[Any]) -> str:
        cpu = row[5]
        if cpu >= self.CPU_ALERT:
            return "alert"
        if cpu >= self.CPU_WARN:
            return "warn"
        return ""

    def query_live(self) -> list[list[Any]]:
        rows = run_query_isolated_sampled(
            RESET_SQL, SAMPLE_SQL, gap_seconds=SAMPLE_GAP_SECONDS)
        out = []
        for r in rows:
            out.append([
                str(r.get("JOB_NAME", "")).strip(),
                str(r.get("SUBSYSTEM", "")).strip(),
                str(r.get("JOB_TYPE", "")).strip(),
                str(r.get("JOB_STATUS", "")).strip(),
                str(r.get("AUTHORIZATION_NAME", "")).strip(),
                round(_f(r.get("ELAPSED_CPU_PERCENTAGE")), 1),
                int(_f(r.get("TEMPORARY_STORAGE"))),
            ])
        return out

    def mock_rows(self) -> list[list[Any]]:
        subsystems = ["QINTER", "QBATCH", "QSYSWRK", "QHTTPSVR", "QUSRWRK"]
        statuses = ["RUN", "DEQW", "DLYW", "LCKW", "EVTW"]
        types = ["INT", "BCH", "SYS", "ASJ"]
        rows = []
        for i in range(TOP_N):
            rows.append([
                f"JOB{random.randint(100000, 999999)}/QUSER/WORKJOB{i:02d}",
                random.choice(subsystems),
                random.choice(types),
                random.choice(statuses),
                random.choice(["QUSER", "QSYS", "APPUSR", "WEBADMIN"]),
                round(random.uniform(5, 45), 1),
                random.randint(5, 800),
            ])
        rows.sort(key=lambda x: x[5], reverse=True)
        return rows
