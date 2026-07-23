"""QSYSOPR message-queue alert collector.

Surfaces messages sitting in the system operator message queue (QSYSOPR) that
typically need operator attention -- inquiry messages awaiting a reply and
high-severity error/diagnostic/escape messages.

Uses the IBM i SQL service QSYS2.MESSAGE_QUEUE_INFO. That service materializes
the whole queue before filtering, so it can be slow; this collector therefore
extends BackgroundTableCollector to refresh on a background thread and serve a
cached snapshot to keep the dashboard responsive.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from collectors.base import BackgroundTableCollector, register
from db import run_query_isolated

LOOKBACK_HOURS = 168  # only look back 7 days to bound the scan

SQL = """
SELECT MESSAGE_ID,
       MESSAGE_TYPE,
       SEVERITY,
       MESSAGE_TIMESTAMP,
       FROM_USER,
       MESSAGE_TEXT
FROM QSYS2.MESSAGE_QUEUE_INFO
WHERE MESSAGE_QUEUE_NAME = 'QSYSOPR'
  AND MESSAGE_QUEUE_LIBRARY = 'QSYS'
  AND MESSAGE_TIMESTAMP > CURRENT TIMESTAMP - ? HOURS
  AND (MESSAGE_TYPE = 'INQUIRY' OR SEVERITY >= 50)
ORDER BY MESSAGE_TIMESTAMP DESC
FETCH FIRST 20 ROWS ONLY
"""


def _fmt_ts(value: Any) -> str:
    if value is None:
        return ""
    return str(value).split(".")[0].replace("T", " ")


@register
class MessageQueueCollector(BackgroundTableCollector):
    id = "messages"
    title = "QSYSOPR MESSAGE"
    columns = ["Time", "Type", "Sev", "Msg ID", "From", "Text"]
    ttl_seconds = 300  # QSYSOPR scan is heavy; refresh at most every 5 min

    def row_level(self, row: list[Any]) -> str:
        msg_type = str(row[1]).upper()
        severity = row[2] if isinstance(row[2], int) else 0
        # Inquiry messages need a reply; high severity = error -> alert.
        if msg_type == "INQUIRY" or severity >= 70:
            return "alert"
        if severity >= 50:
            return "warn"
        return ""

    def query_live(self) -> list[list[Any]]:
        rows = run_query_isolated(SQL, (LOOKBACK_HOURS,))
        out = []
        for r in rows:
            text = str(r.get("MESSAGE_TEXT") or "").strip()
            if len(text) > 120:
                text = text[:117] + "..."
            out.append([
                _fmt_ts(r.get("MESSAGE_TIMESTAMP")),
                str(r.get("MESSAGE_TYPE") or "").strip(),
                int(r.get("SEVERITY") or 0),
                str(r.get("MESSAGE_ID") or "").strip(),
                str(r.get("FROM_USER") or "").strip(),
                text,
            ])
        return out

    def mock_rows(self) -> list[list[Any]]:
        samples = [
            ("INQUIRY", 99, "CPA5305", "Verify alignment on printer PRT01 (C R)"),
            ("INQUIRY", 80, "CPA4002", "Load form type into device PRT02 (C H)"),
            ("DIAGNOSTIC", 70, "CPF1393", "Subsystem QBATCH disabled - user limit"),
            ("ESCAPE", 60, "CPF5257", "Member RECS in file ORDERS damaged"),
            ("INFORMATIONAL", 50, "CPI0998", "Disk unit 3 threshold exceeded"),
        ]
        now = datetime.now()
        rows = []
        for mtype, sev, mid, text in samples:
            ts = now - timedelta(minutes=random.randint(1, 240))
            rows.append([
                ts.strftime("%Y-%m-%d %H:%M:%S"),
                mtype, sev, mid,
                random.choice(["QSYS", "QPGMR", "OPERATOR"]),
                text,
            ])
        rows.sort(key=lambda x: x[0], reverse=True)
        return rows
