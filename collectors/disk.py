"""Disk / ASP collector.

Uses QSYS2.SYSDISKSTAT to report per-unit disk utilization and busy percentage.
"""
from __future__ import annotations

import random
from typing import Any

from collectors.base import Collector, register, build_table
from db import run_query

SQL = """
SELECT UNIT_NUMBER,
       UNIT_TYPE,
       PERCENT_USED,
       ELAPSED_PERCENT_BUSY,
       UNIT_MEDIA_CAPACITY_GB
FROM QSYS2.SYSDISKSTAT
ORDER BY UNIT_NUMBER
"""


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@register
class DiskCollector(Collector):
    id = "disk"
    title = "Disk Units"
    kind = "table"

    columns = ["Unit", "Type", "% Used", "% Busy", "Capacity (GB)"]

    # Disk fullness thresholds for row colouring.
    USED_ALERT = 90.0
    USED_WARN = 80.0

    def _row_level(self, row: list[Any]) -> str:
        used = row[2]
        if used >= self.USED_ALERT:
            return "alert"
        if used >= self.USED_WARN:
            return "warn"
        return ""

    def fetch_live(self) -> dict[str, Any]:
        rows = run_query(SQL)
        out = []
        for r in rows:
            unit_type = str(r.get("UNIT_TYPE", "")).strip()
            out.append([
                str(r.get("UNIT_NUMBER", "")).strip(),
                "SSD" if unit_type == "1" else "HDD",
                round(_f(r.get("PERCENT_USED")), 1),
                round(_f(r.get("ELAPSED_PERCENT_BUSY")), 1),
                round(_f(r.get("UNIT_MEDIA_CAPACITY_GB")), 1),
            ])
        return build_table(self.columns, out, self._row_level)

    def fetch_mock(self) -> dict[str, Any]:
        rows = []
        for unit in range(1, 7):
            rows.append([
                str(unit),
                "SSD" if unit % 2 else "HDD",
                round(random.uniform(40, 95), 1),
                round(random.uniform(0, 35), 1),
                round(random.choice([139.0, 283.0, 571.0]), 1),
            ])
        return build_table(self.columns, rows, self._row_level)
