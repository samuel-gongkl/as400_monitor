"""Memory pool collector.

Uses QSYS2.MEMORY_POOL to report per-pool size and faulting rates, which are
key indicators of memory pressure on IBM i.
"""
from __future__ import annotations

import random
from typing import Any

from collectors.base import Collector, register
from db import run_query

SQL = """
SELECT POOL_NAME,
       CURRENT_SIZE,
       DEFINED_SIZE,
       MAXIMUM_ACTIVE_THREADS,
       ELAPSED_DATABASE_FAULTS,
       ELAPSED_NON_DATABASE_FAULTS,
       ELAPSED_TIME
FROM QSYS2.MEMORY_POOL_INFO
ORDER BY CURRENT_SIZE DESC
"""


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _per_sec(count: Any, seconds: Any) -> float:
    secs = _f(seconds)
    if secs <= 0:
        return 0.0
    return _f(count) / secs


@register
class MemoryPoolCollector(Collector):
    id = "memory"
    title = "Memory Pools"
    kind = "table"

    columns = ["Pool", "Size (MB)", "Max Threads", "DB Faults/s",
               "Non-DB Faults/s"]

    def fetch_live(self) -> dict[str, Any]:
        rows = run_query(SQL)
        out = []
        for r in rows:
            elapsed = r.get("ELAPSED_TIME")
            out.append([
                str(r.get("POOL_NAME", "")).strip(),
                round(_f(r.get("CURRENT_SIZE")) / 1024, 1),
                int(_f(r.get("MAXIMUM_ACTIVE_THREADS"))),
                round(_per_sec(r.get("ELAPSED_DATABASE_FAULTS"), elapsed), 1),
                round(_per_sec(r.get("ELAPSED_NON_DATABASE_FAULTS"), elapsed), 1),
            ])
        return {"columns": self.columns, "rows": out}

    def fetch_mock(self) -> dict[str, Any]:
        pools = ["*MACHINE", "*BASE", "*INTERACT", "*SPOOL", "QBATCH"]
        rows = []
        for p in pools:
            rows.append([
                p,
                round(random.uniform(200, 8000), 1),
                random.choice([0, 20, 50, 100, 200]),
                round(random.uniform(0, 12), 1),
                round(random.uniform(0, 20), 1),
            ])
        return {"columns": self.columns, "rows": rows}
