# AS400 / IBM i Monitor

A lightweight, extensible monitoring dashboard for IBM i (AS400) systems,
built with **Python + Flask + Chart.js**. It pulls live performance metrics
from the IBM i `QSYS2` SQL services over ODBC and renders them on an
auto-refreshing web dashboard. It can monitor **multiple LPARs/partitions** at
once — switch between them on the Overview, and compare them side by side on
the LPAR CPU page.

Built-in monitors:

| Monitor | Source (IBM i SQL service) | Shows |
|---|---|---|
| System Status | `QSYS2.SYSTEM_STATUS` | CPU %, job counts, ASP storage |
| CPU chart | `QSYS2.SYSTEM_STATUS` | Live CPU utilization trend |
| Memory Pools | `QSYS2.MEMORY_POOL` | Pool sizes & faulting rates |
| Disk Units | `QSYS2.SYSDISKSTAT` | Per-unit % used / % busy |
| Subsystems | `QSYS2.SUBSYSTEM_INFO` | Active subsystems & their job counts |

Two additional collectors ship with the project but are **disabled by default**
(not registered in `collectors/__init__.py`): **Active Jobs**
(`QSYS2.ACTIVE_JOB_INFO`, top CPU-consuming jobs) and **QSYSOPR Alerts**
(`QSYS2.MESSAGE_QUEUE_INFO`, inquiry & high-severity operator messages). Re-enable
either by adding its import back to `collectors/__init__.py`.

### Alert highlighting

Table rows are color-coded by severity so problems stand out at a glance:

| Monitor | Warn (amber) | Alert (red) |
|---|---|---|
| Disk Units | % used ≥ 80 | % used ≥ 90 |
| Subsystems | active ≥ 90% of max | active ≥ max (*saturated*) |
| Active Jobs* | CPU ≥ 10% | CPU ≥ 25% |
| QSYSOPR Alerts* | severity ≥ 50 | `INQUIRY` message or severity ≥ 70 |

<sub>* available but disabled by default (see above).</sub>

The System Status KPIs are colored too: **CPU %** turns amber at 75 / red at 90,
and **ASP Used %** turns amber at 80 / red at 90. Each card header shows a count
of flagged rows when any are present.

> **Note on CPU:** live CPU is read from `QSYS2.SYSTEM_ACTIVITY_INFO` (sampled
> over ~1s). `SYSTEM_STATUS.AVERAGE_CPU_UTILIZATION` reports 0 on some IBM i
> versions, so it is not used for the headline figure.

## Monitoring multiple LPARs

The tool supports any number of LPARs/partitions, each a separate host with its
own credentials. Define one numbered block per LPAR in `.env`:

```
AS400_LPAR1_NAME=HKPRD01
AS400_LPAR1_HOST=192.168.1.10
AS400_LPAR1_USER=USER1
AS400_LPAR1_PASSWORD=PW1

AS400_LPAR2_NAME=HKPRD02
AS400_LPAR2_HOST=192.168.1.11
AS400_LPAR2_USER=USER2
AS400_LPAR2_PASSWORD=PW2

AS400_LPAR3_NAME=HKDEV01
AS400_LPAR3_HOST=192.168.1.12
AS400_LPAR3_USER=USER3
AS400_LPAR3_PASSWORD=PW3
```

`NAME` is the label shown in the UI; `…_DRIVER` and `…_SSL` are optional per
LPAR and fall back to the top-level `AS400_DRIVER` / `AS400_SSL`. Each LPAR gets
its own pooled ODBC connection.

- **Overview** — when more than one LPAR is configured, a selector appears in
  the top bar; pick an LPAR and all cards (System / Memory / Disk / Subsystems)
  plus the live CPU chart follow it.
- **LPAR CPU page** — renders one panel per LPAR (its own trend chart,
  distribution histogram, and summary) so all partitions can be compared at a
  glance.

**Backward compatible:** if no `AS400_LPAR1_HOST` is set, the legacy single-host
variables (`AS400_HOST` / `AS400_USER` / `AS400_PASSWORD`) are used as one LPAR.

## LPAR CPU dashboard

A second page at **`/lpar`** (linked from the top nav) focuses on partition CPU
over time, with **one panel per configured LPAR**:

- **24-hour CPU trend** — area chart with warning / critical / entitled-cap
  threshold lines.
- **CPU usage distribution** — histogram of sampled CPU, bars colored by band.
- **LPAR summary** — partition identity and processor config
  (`QSYS2.SYSTEM_STATUS` + `SYSIBMADM.ENV_SYS_INFO`): host, OS, virtual
  processors, entitled / max capacity, sharing mode, machine/serial, plus
  rolling stats (avg, peak, min, P95, P99, std dev) and warning/critical sample
  counts.

A background sampler thread records CPU for **every** LPAR each
`LPAR_SAMPLE_SECONDS` (default 30) into a small SQLite file (`cpu_history.db`,
git-ignored), keyed by LPAR, and prunes anything older than
`LPAR_HISTORY_HOURS` (default 24). History therefore survives restarts and
accumulates over time. In **mock mode** a day of synthetic samples is seeded per
LPAR on startup so the page is immediately populated.

Tunable via `.env`: `LPAR_SAMPLE_SECONDS`, `LPAR_HISTORY_HOURS`,
`LPAR_WARN_PCT` (default 70), `LPAR_CRIT_PCT` (default 85), `LPAR_DB_PATH`.

## Requirements

- Python 3.10+
- **IBM i Access Client Solutions (ACS)** with the **ODBC driver** installed
  (provides the `IBM i Access ODBC Driver`). Download from IBM.
- Network access to the IBM i partition and a user profile with `*USE`
  authority on the QSYS2 services.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env   # then edit .env with your host + credentials
```

Edit `.env` (single LPAR shown; see **Monitoring multiple LPARs** above for 3):

```
AS400_MODE=live
AS400_DRIVER=IBM i Access ODBC Driver
AS400_LPAR1_NAME=HKPRD01
AS400_LPAR1_HOST=your.ibmi.host
AS400_LPAR1_USER=YOURUSER
AS400_LPAR1_PASSWORD=YOURPASSWORD
```

## Run

```powershell
python app.py
```

Open http://127.0.0.1:5000

### Mock mode (no AS400 needed)

Set `AS400_MODE=mock` in `.env` to run the full dashboard with simulated data
— useful for development or demos. The default mode is `live`.

## How it works

```
app.py            Flask routes + JSON API
config.py         Reads .env settings
db.py             ODBC connection + run_query() / run_query_isolated() helpers
lpar.py           LPAR CPU history store + background sampler + summary/stats
collectors/       One module per monitor
  base.py         Collector + BackgroundTableCollector base classes + registry
  system_status.py, memory.py, disk.py, active_jobs.py, subsystems.py, messages.py
templates/        Dashboard HTML (index.html, lpar.html)
static/           dashboard.js, lpar.js (Chart.js) + style.css
```

The dashboard polls `GET /api/metrics` on a timer and renders each collector's
payload. Each collector returns a normalized result and never raises, so one
failing query can't break the whole page.

### Fast vs. heavy collectors

* **Fast collectors** (system, memory, disk) subclass `Collector` and query
  inline on each refresh via `run_query()` (a shared, auto-commit connection).
* **Heavy collectors** (active jobs, QSYSOPR alerts) subclass
  `BackgroundTableCollector`. Their IBM i services can take many seconds
  (scanning the whole message queue, or detailed info for thousands of jobs),
  so they serve a cached snapshot instantly and refresh on a background thread
  (one at a time, on a dedicated `run_query_isolated()` connection) at most
  every `ttl_seconds`. This keeps the dashboard responsive no matter how slow a
  service is. The card shows an "Updated Ns ago" note.

## Adding a new monitor

Adding a monitor (e.g. message queue, output queue, subsystem status) takes
about 20 lines:

1. Create `collectors/my_monitor.py`:

```python
from collectors.base import Collector, register
from db import run_query

@register
class OutputQueueCollector(Collector):
    id = "outq"
    title = "Output Queues"
    kind = "table"
    columns = ["Queue", "Library", "Files"]

    def fetch_live(self):
        rows = run_query("""
            SELECT OUTPUT_QUEUE_NAME, OUTPUT_QUEUE_LIBRARY_NAME,
                   NUMBER_OF_FILES
            FROM QSYS2.OUTPUT_QUEUE_INFO
            ORDER BY NUMBER_OF_FILES DESC
            FETCH FIRST 15 ROWS ONLY
        """)
        return {"columns": self.columns,
                "rows": [[r["OUTPUT_QUEUE_NAME"], r["OUTPUT_QUEUE_LIBRARY_NAME"],
                          r["NUMBER_OF_FILES"]] for r in rows]}

    def fetch_mock(self):
        return {"columns": self.columns,
                "rows": [["QPRINT", "QGPL", 12], ["QEZJOBLOG", "QUSRSYS", 47]]}
```

2. Register the import in `collectors/__init__.py`:

```python
from collectors import my_monitor  # noqa
```

3. Restart. Table-style monitors appear automatically; for a custom KPI/chart
   card, add markup in `templates/index.html` and a renderer in
   `static/dashboard.js`.

### For slow services, use `BackgroundTableCollector`

If the IBM i service is slow (scans a large queue, gathers detail for many
jobs), subclass `BackgroundTableCollector` instead so it never blocks the
dashboard. Provide `columns`, `query_live()` (return a list of row-lists, run on
an isolated connection via `run_query_isolated`), `mock_rows()`, and optionally
`ttl_seconds`:

```python
from collectors.base import BackgroundTableCollector, register
from db import run_query_isolated

@register
class OutputQueueCollector(BackgroundTableCollector):
    id = "outq"
    title = "Output Queues"
    columns = ["Queue", "Library", "Files"]
    ttl_seconds = 60

    def query_live(self):
        rows = run_query_isolated("""
            SELECT OUTPUT_QUEUE_NAME, OUTPUT_QUEUE_LIBRARY_NAME, NUMBER_OF_FILES
            FROM QSYS2.OUTPUT_QUEUE_INFO
            ORDER BY NUMBER_OF_FILES DESC FETCH FIRST 15 ROWS ONLY
        """)
        return [[r["OUTPUT_QUEUE_NAME"], r["OUTPUT_QUEUE_LIBRARY_NAME"],
                 r["NUMBER_OF_FILES"]] for r in rows]

    def mock_rows(self):
        return [["QPRINT", "QGPL", 12], ["QEZJOBLOG", "QUSRSYS", 47]]
```

### Color-coding rows (thresholds)

To highlight problem rows, build your payload with `build_table()` and supply a
`row_level` function that returns `""`, `"warn"`, or `"alert"` for each row. The
helper attaches a `levels` array (parallel to `rows`) that the dashboard turns
into amber/red row styling and a flagged-row count:

```python
from collectors.base import Collector, register, build_table
from db import run_query

@register
class OutputQueueCollector(Collector):
    id = "outq"
    title = "Output Queues"
    kind = "table"
    columns = ["Queue", "Library", "Files"]

    def _row_level(self, row):
        files = row[2]
        if files >= 500:
            return "alert"
        if files >= 100:
            return "warn"
        return ""

    def fetch_live(self):
        rows = run_query("...")
        out = [[r["OUTPUT_QUEUE_NAME"], r["OUTPUT_QUEUE_LIBRARY_NAME"],
                r["NUMBER_OF_FILES"]] for r in rows]
        return build_table(self.columns, out, self._row_level)

    def fetch_mock(self):
        out = [["QPRINT", "QGPL", 12], ["QEZJOBLOG", "QUSRSYS", 640]]
        return build_table(self.columns, out, self._row_level)
```

`BackgroundTableCollector` subclasses get the same behavior by implementing a
`row_level(row)` method — the base class calls `build_table` for you.

## API

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard (accepts `?lpar=<key>`) |
| `GET /lpar` | LPAR CPU usage dashboard (all LPARs) |
| `GET /api/lpars` | Configured LPAR list + default key |
| `GET /api/health` | Connectivity / mode status (accepts `?lpar=`) |
| `GET /api/metrics` | All collectors' current data (accepts `?lpar=`) |
| `GET /api/metrics/<id>` | One collector's data (accepts `?lpar=`) |
| `GET /api/lpar` | Per-LPAR CPU history, summary, distribution & stats |

## Security notes

- Credentials live in `.env`, which is git-ignored. Never commit it.
- Use a least-privilege IBM i user profile for monitoring.
- For production, run behind a reverse proxy and serve with a WSGI server
  (e.g. `waitress`) instead of Flask's dev server.
