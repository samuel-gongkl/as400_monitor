"""Subsystem status collector.

Lists active subsystems and their job counts using QSYS2.SUBSYSTEM_INFO, plus a
summary of how many subsystems are active vs inactive. A subsystem is flagged
when it reaches its configured maximum active jobs (saturated).
"""
from __future__ import annotations

import random
from typing import Any

from collectors.base import Collector, register, build_table
from db import run_query

SQL = """
SELECT SUBSYSTEM_DESCRIPTION,
       SUBSYSTEM_DESCRIPTION_LIBRARY,
       STATUS,
       CURRENT_ACTIVE_JOBS,
       MAXIMUM_ACTIVE_JOBS
FROM QSYS2.SUBSYSTEM_INFO
WHERE STATUS = 'ACTIVE'
ORDER BY CURRENT_ACTIVE_JOBS DESC
"""


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@register
class SubsystemCollector(Collector):
    id = "subsystems"
    title = "Active Subsystems"
    kind = "table"

    columns = ["Subsystem", "Library", "Status", "Active Jobs", "Max Jobs"]

    def _row_level(self, row: list[Any]) -> str:
        active = row[3]
        max_jobs = row[4]
        # Saturated when at/over a defined (non-*NOMAX) maximum.
        if isinstance(max_jobs, int) and max_jobs > 0 and active >= max_jobs:
            return "alert"
        if isinstance(max_jobs, int) and max_jobs > 0 and active >= 0.9 * max_jobs:
            return "warn"
        return ""

    def fetch_live(self) -> dict[str, Any]:
        rows = run_query(SQL)
        out = []
        for r in rows:
            max_raw = r.get("MAXIMUM_ACTIVE_JOBS")
            out.append([
                str(r.get("SUBSYSTEM_DESCRIPTION", "")).strip(),
                str(r.get("SUBSYSTEM_DESCRIPTION_LIBRARY", "")).strip(),
                str(r.get("STATUS", "")).strip(),
                _int(r.get("CURRENT_ACTIVE_JOBS")),
                _int(max_raw) if max_raw is not None else "*NOMAX",
            ])
        payload = build_table(self.columns, out, self._row_level)
        payload["note"] = f"{len(out)} active subsystems"
        return payload

    def fetch_mock(self) -> dict[str, Any]:
        samples = [
            ("QUSRWRK", "QSYS", 526, "*NOMAX"),
            ("QSYSWRK", "QSYS", 127, "*NOMAX"),
            ("QINTER", "QSYS", 54, 60),
            ("QBATCH", "QSYS", 19, 20),
            ("QHTTPSVR", "QHTTPSVR", 18, "*NOMAX"),
            ("QSERVER", "QSYS", 24, "*NOMAX"),
            ("QCTL", "QSYS", 1, "*NOMAX"),
        ]
        out = [[name, lib, "ACTIVE", active, mx] for name, lib, active, mx in samples]
        payload = build_table(self.columns, out, self._row_level)
        payload["note"] = f"{len(out)} active subsystems (mock)"
        return payload
