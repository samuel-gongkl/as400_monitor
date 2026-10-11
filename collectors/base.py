"""Collector framework.

A *collector* is one unit of monitoring (CPU, memory, disk, jobs, ...).
Each collector knows how to fetch its data in live mode (SQL against IBM i
QSYS2 services) and how to produce believable data in mock mode.

To add a new monitor you only need to:
  1. Create a new module in this package.
  2. Subclass ``Collector`` and implement ``fetch_live`` and ``fetch_mock``.
  3. Register it with the ``@register`` decorator.
  4. Import the module in ``collectors/__init__.py``.

That's it -- the dashboard and API pick it up automatically.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from config import Config

# Ordered registry of collector instances keyed by id.
_REGISTRY: "dict[str, Collector]" = {}


class Collector:
    """Base class for a single monitor.

    Attributes
    ----------
    id: stable identifier used in the API/URL (e.g. "cpu").
    title: human-friendly name shown on the dashboard.
    kind: one of "kpi", "chart", "table" -- hints the UI how to render.
    """

    id: str = ""
    title: str = ""
    kind: str = "kpi"

    def fetch_live(self) -> Any:
        raise NotImplementedError

    def fetch_mock(self) -> Any:
        raise NotImplementedError

    def collect(self) -> dict[str, Any]:
        """Return a normalized payload, never raising.

        Shape:
            {"id", "title", "kind", "ok": bool, "data" | "error"}
        """
        try:
            data = self.fetch_live() if Config.is_live() else self.fetch_mock()
            return {"id": self.id, "title": self.title, "kind": self.kind,
                    "ok": True, "data": data}
        except Exception as exc:  # never let one collector break the page
            return {"id": self.id, "title": self.title, "kind": self.kind,
                    "ok": False, "error": str(exc)}


def register(cls: type[Collector]) -> type[Collector]:
    """Class decorator that instantiates and registers a collector."""
    instance = cls()
    if not instance.id:
        raise ValueError(f"{cls.__name__} must define a non-empty id")
    _REGISTRY[instance.id] = instance
    return cls


# Severity levels a row can carry. The dashboard maps these to CSS classes.
LEVEL_NONE = ""
LEVEL_WARN = "warn"
LEVEL_ALERT = "alert"


def build_table(columns: list[str], rows: list[list[Any]],
                row_level: "Callable[[list[Any]], str] | None" = None
                ) -> dict[str, Any]:
    """Assemble a table payload, optionally tagging each row with a severity.

    ``row_level`` is an optional callable that receives a row (list of cells)
    and returns one of "" / "warn" / "alert". The result is returned as a
    parallel ``levels`` list the UI uses to colour rows.
    """
    payload: dict[str, Any] = {"columns": columns, "rows": rows}
    if row_level is not None:
        levels = []
        for row in rows:
            try:
                levels.append(row_level(row) or LEVEL_NONE)
            except Exception:
                levels.append(LEVEL_NONE)
        payload["levels"] = levels
    return payload


class BackgroundTableCollector(Collector):
    """Base for *heavy* table monitors whose query is too slow to run inline.

    Some IBM i services (e.g. scanning QSYSOPR, or ACTIVE_JOB_INFO with detailed
    info for thousands of jobs) take many seconds. Running them on every
    dashboard refresh would block the whole page, so this base:

      * serves a cached snapshot instantly, and
      * refreshes it in a background thread (one at a time) at most every
        ``ttl_seconds``, on a dedicated connection that never blocks other
        collectors.

    Subclasses provide ``columns``, a ``query_live()`` that returns a list of
    row-lists, and ``mock_rows()``. Override ``row_level(row)`` to colour rows.
    """

    kind = "table"
    columns: list[str] = []
    ttl_seconds: int = 60

    def row_level(self, row: list[Any]) -> str:
        """Return "" / "warn" / "alert" for a row. Override to add colouring."""
        return LEVEL_NONE

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # A collector instance is shared by all requests, but requests can
        # target different LPARs. Keep one snapshot and refresh flag per LPAR;
        # otherwise switching partitions briefly shows another partition's
        # cached jobs/messages/disk rows.
        self._cache: dict[str, dict[str, Any]] = {}

    def _state_for(self, lpar_key: str) -> dict[str, Any]:
        return self._cache.setdefault(lpar_key, {
            "refreshing": False,
            "rows": [],
            "fetched_at": None,
            "error": None,
        })

    # Subclasses implement these two.
    def query_live(self) -> list[list[Any]]:
        raise NotImplementedError

    def mock_rows(self) -> list[list[Any]]:
        raise NotImplementedError

    def fetch_live(self) -> dict[str, Any]:
        from db import current_lpar
        lpar_key = current_lpar()
        self._maybe_start_refresh(lpar_key)
        with self._lock:
            state = self._state_for(lpar_key)
            rows = list(state["rows"])
            fetched_at = state["fetched_at"]
            error = state["error"]
            refreshing = state["refreshing"]

        if fetched_at is None:
            note = "Loading in background"
            if error:
                note += f" -- last attempt failed: {error}"
            else:
                note += "..."
            return {"columns": self.columns, "rows": [], "note": note}

        age = int(time.time() - fetched_at)
        note = f"Updated {age}s ago"
        if refreshing:
            note += " (refreshing...)"
        if error:
            note += f" -- last refresh error: {error}"
        payload = build_table(self.columns, rows, self.row_level)
        payload["note"] = note
        payload["count"] = len(rows)
        return payload

    def fetch_mock(self) -> dict[str, Any]:
        rows = self.mock_rows()
        payload = build_table(self.columns, rows, self.row_level)
        payload["note"] = "mock data"
        payload["count"] = len(rows)
        return payload

    def _maybe_start_refresh(self, lpar_key: str) -> None:
        now = time.time()
        with self._lock:
            state = self._state_for(lpar_key)
            fresh = (state["fetched_at"] is not None
                     and now - state["fetched_at"] < self.ttl_seconds)
            if fresh or state["refreshing"]:
                return
            state["refreshing"] = True
        # Capture the caller's current LPAR so the background thread (which has
        # its own thread-local) queries the same partition.
        threading.Thread(target=self._refresh, args=(lpar_key,),
                         daemon=True).start()

    def _refresh(self, lpar_key: str | None = None) -> None:
        from db import current_lpar, set_current_lpar
        lpar_key = lpar_key or current_lpar()
        set_current_lpar(lpar_key)
        try:
            rows = self.query_live()
            with self._lock:
                state = self._state_for(lpar_key)
                state["rows"] = rows
                state["fetched_at"] = time.time()
                state["error"] = None
        except Exception as exc:
            with self._lock:
                state = self._state_for(lpar_key)
                state["error"] = str(exc)
                state["fetched_at"] = time.time()
        finally:
            with self._lock:
                self._state_for(lpar_key)["refreshing"] = False


def all_collectors() -> list[Collector]:
    return list(_REGISTRY.values())


def get_collector(collector_id: str) -> Collector | None:
    return _REGISTRY.get(collector_id)


def collect_all(lpar: str | None = None) -> list[dict[str, Any]]:
    """Collect every registered metric, optionally targeting one LPAR."""
    if lpar is not None:
        from db import set_current_lpar
        set_current_lpar(lpar)
    return [c.collect() for c in _REGISTRY.values()]
