"""Email and Microsoft Teams notifications for monitor alerts.

Notifications are deliberately separate from the request path.  The
:class:`AlertMonitor` polls the same collector payloads used by the dashboard,
tracks alert state per LPAR, and sends only on a new alert, a severity change,
a recovery, or an optional reminder interval.

Both transports use Python's standard library: SMTP for email and an HTTPS
POST for a Teams incoming-webhook/Workflow URL.  They are disabled by default
and are enabled explicitly with ``NOTIFY_ENABLED=1``.
"""
from __future__ import annotations

import json
import logging
import smtplib
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Iterable

from config import Config

log = logging.getLogger(__name__)

_TEAMS_COLOURS = {"warning": "FFB900", "critical": "D13438", "recovery": "107C10"}


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _split_recipients(value: str | Iterable[str] | None) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        values = value.replace(";", ",").split(",")
    else:
        values = value
    return [item.strip() for item in values if item and item.strip()]


def _severity_for(value: Any, warning: float, critical: float) -> str | None:
    number = _as_float(value)
    if number is None or number < warning:
        return None
    return "critical" if number >= critical else "warning"


@dataclass(frozen=True)
class NotificationEvent:
    """A normalized alert suitable for either notification channel."""

    key: str
    lpar: str
    title: str
    severity: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    observed_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))


class EmailNotifier:
    """Send a plain-text alert through an SMTP server."""

    def __init__(self, config: type[Config] = Config) -> None:
        self.config = config

    def send(self, event: NotificationEvent) -> None:
        recipients = _split_recipients(self.config.NOTIFY_EMAIL_TO)
        if not self.config.NOTIFY_EMAIL_ENABLED or not recipients:
            raise RuntimeError("email notifications are not configured")
        if not self.config.NOTIFY_EMAIL_SMTP_HOST:
            raise RuntimeError("NOTIFY_EMAIL_SMTP_HOST is not configured")
        if not self.config.NOTIFY_EMAIL_FROM:
            raise RuntimeError("NOTIFY_EMAIL_FROM is not configured")

        message = EmailMessage()
        message["Subject"] = self._subject(event)
        message["From"] = self.config.NOTIFY_EMAIL_FROM
        message["To"] = ", ".join(recipients)
        message.set_content(self._body(event))

        smtp_cls = smtplib.SMTP_SSL if self.config.NOTIFY_EMAIL_USE_SSL \
            else smtplib.SMTP
        with smtp_cls(self.config.NOTIFY_EMAIL_SMTP_HOST,
                      self.config.NOTIFY_EMAIL_SMTP_PORT,
                      timeout=self.config.NOTIFY_HTTP_TIMEOUT_SECONDS) as smtp:
            if (self.config.NOTIFY_EMAIL_USE_TLS
                    and not self.config.NOTIFY_EMAIL_USE_SSL):
                smtp.starttls()
            if self.config.NOTIFY_EMAIL_USER:
                smtp.login(self.config.NOTIFY_EMAIL_USER,
                           self.config.NOTIFY_EMAIL_PASSWORD)
            smtp.send_message(message)

    @staticmethod
    def _subject(event: NotificationEvent) -> str:
        prefix = {
            "critical": "CRITICAL",
            "warning": "WARNING",
            "recovery": "RECOVERED",
        }.get(event.severity, event.severity.upper())
        return f"[{prefix}] AS400 Monitor - {event.lpar} - {event.title}"

    @staticmethod
    def _body(event: NotificationEvent) -> str:
        lines = [
            f"AS400 Monitor: {event.severity.upper()}",
            f"LPAR: {event.lpar}",
            f"Alert: {event.title}",
            f"Time (UTC): {event.observed_at.isoformat()}",
            "",
            event.summary,
        ]
        for name, value in event.details.items():
            lines.append(f"{name}: {value}")
        return "\n".join(lines) + "\n"


class TeamsNotifier:
    """Post a MessageCard to a Teams incoming webhook or Workflow URL."""

    def __init__(self, config: type[Config] = Config) -> None:
        self.config = config

    def send(self, event: NotificationEvent) -> None:
        url = (self.config.NOTIFY_TEAMS_WEBHOOK_URL or "").strip()
        if not self.config.NOTIFY_TEAMS_ENABLED or not url:
            raise RuntimeError("Teams notifications are not configured")
        body = json.dumps(self._payload(event)).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                    request, timeout=self.config.NOTIFY_HTTP_TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", 200)
                if status < 200 or status >= 300:
                    raise RuntimeError(f"Teams webhook returned HTTP {status}")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Teams webhook returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Teams webhook request failed: {exc.reason}") from exc

    @staticmethod
    def _payload(event: NotificationEvent) -> dict[str, Any]:
        facts = [{"name": name, "value": str(value)}
                 for name, value in event.details.items()]
        return {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": event.summary,
            "themeColor": _TEAMS_COLOURS.get(event.severity, "0078D4"),
            "title": f"{event.severity.upper()}: {event.title}",
            "sections": [{
                "activityTitle": f"AS400 Monitor — {event.lpar}",
                "activitySubtitle": event.observed_at.isoformat(),
                "text": event.summary,
                "facts": facts,
                "markdown": True,
            }],
        }


class NotificationManager:
    """Fan an event out to all configured channels.

    A failure in one channel is logged and does not prevent the other channel
    from being attempted.  ``send`` returns the channels that succeeded.
    """

    def __init__(self, config: type[Config] = Config) -> None:
        self.config = config
        self.email = EmailNotifier(config)
        self.teams = TeamsNotifier(config)

    def send(self, event: NotificationEvent) -> list[str]:
        sent: list[str] = []
        transports = []
        if self.config.NOTIFY_EMAIL_ENABLED:
            transports.append(("email", self.email))
        if self.config.NOTIFY_TEAMS_ENABLED:
            transports.append(("teams", self.teams))
        for channel, notifier in transports:
            try:
                notifier.send(event)
                sent.append(channel)
            except Exception as exc:  # notifications must not stop monitoring
                log.error("%s notification failed for %s: %s",
                          channel, event.key, exc)
        if not sent and transports:
            log.warning("No notification channel delivered alert %s", event.key)
        return sent


def evaluate_metrics(lpar: str, metrics: list[dict[str, Any]],
                     config: type[Config] = Config) -> list[NotificationEvent]:
    """Convert dashboard collector payloads into active alert events.

    This intentionally consumes normalized collector output, so adding a new
    monitor does not require changes to the database layer or HTTP routes.
    """
    events: list[NotificationEvent] = []
    now = datetime.now(timezone.utc)

    def add(key: str, title: str, severity: str, summary: str,
            details: dict[str, Any]) -> None:
        events.append(NotificationEvent(key, lpar, title, severity, summary,
                                        details, now))

    for metric in metrics:
        if not metric.get("ok"):
            continue
        collector_id = metric.get("id")
        data = metric.get("data") or {}
        if collector_id == "system" and isinstance(data, dict):
            for key, title, value_name, warning, critical in (
                    ("cpu", "System CPU", "CPU %", config.NOTIFY_CPU_WARN_PCT,
                     config.NOTIFY_CPU_CRIT_PCT),
                    ("asp", "System ASP usage", "ASP used %",
                     config.NOTIFY_ASP_WARN_PCT, config.NOTIFY_ASP_CRIT_PCT)):
                value = data.get("cpu_pct" if key == "cpu" else "asp_used_pct")
                severity = _severity_for(value, warning, critical)
                if severity:
                    add(f"system.{key}", title, severity,
                        f"{title} is {float(value):.1f}% on {lpar}.",
                        {value_name: f"{float(value):.1f}%",
                         "Warning threshold": f"{warning:.1f}%",
                         "Critical threshold": f"{critical:.1f}%"})
            continue

        if collector_id not in {"disk", "subsystems", "messages", "cpu_jobs"}:
            continue
        rows = data.get("rows") if isinstance(data, dict) else None
        levels = data.get("levels") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, list):
                continue
            level = levels[index] if isinstance(levels, list) and index < len(levels) \
                else ""
            if level not in {"warn", "alert"}:
                continue
            severity = "critical" if level == "alert" else "warning"
            # Use an identifier that remains stable between polls. In
            # particular, a QSYSOPR timestamp must not be part of the key or
            # the same still-open message would generate a new alert each scan.
            if collector_id == "messages" and len(row) > 3:
                identity = str(row[3])
            elif collector_id in {"cpu_jobs", "disk", "subsystems"} and row:
                identity = str(row[0])
                if collector_id == "subsystems" and len(row) > 1:
                    identity += " / " + str(row[1])
            else:
                identity = " / ".join(str(value) for value in row[:2])
            add(f"{collector_id}.{identity}", metric.get("title", collector_id),
                severity,
                f"{metric.get('title', collector_id)} has a {severity} item on {lpar}: "
                f"{identity}.",
                {column: value for column, value in
                 zip(data.get("columns", []), row)})
    return events


class AlertMonitor:
    """Background state machine that emits notifications on state changes."""

    def __init__(self, config: type[Config] = Config,
                 manager: NotificationManager | None = None) -> None:
        self.config = config
        self.manager = manager or NotificationManager(config)
        self._states: dict[tuple[str, str], NotificationEvent] = {}
        self._last_sent: dict[tuple[str, str], float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._thread = threading.Thread(target=self._run,
                                        name="as400-alert-monitor", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def check_once(self) -> None:
        # Import lazily to avoid importing the collector registry when this
        # module is used only as a transport library or in unit tests.
        import collectors
        current: dict[tuple[str, str], NotificationEvent] = {}
        for lpar in self.config.LPARS:
            key = lpar["key"]
            try:
                metrics = collectors.collect_all(key)
                for event in evaluate_metrics(key, metrics, self.config):
                    state_key = (key, event.key)
                    current[state_key] = event
                    previous = self._states.get(state_key)
                    now = time.monotonic()
                    due_reminder = (
                        self.config.NOTIFY_REPEAT_MINUTES > 0
                        and now - self._last_sent.get(state_key, 0)
                        >= self.config.NOTIFY_REPEAT_MINUTES * 60)
                    if (previous is None
                            or previous.severity != event.severity
                            or state_key not in self._last_sent
                            or due_reminder):
                        if self.manager.send(event):
                            self._last_sent[state_key] = now
            except Exception:
                log.exception("Alert check failed for LPAR %s", key)

        for state_key, previous in list(self._states.items()):
            if state_key in current:
                continue
            recovery = NotificationEvent(
                key=previous.key,
                lpar=previous.lpar,
                title=previous.title,
                severity="recovery",
                summary=f"{previous.title} has returned below its alert threshold on "
                        f"{previous.lpar}.",
                details=previous.details,
            )
            if self.manager.send(recovery):
                self._last_sent.pop(state_key, None)
        self._states = current

    def _run(self) -> None:
        log.info("Alert monitor started (interval=%ss)",
                 self.config.NOTIFY_INTERVAL_SECONDS)
        while not self._stop.is_set():
            self.check_once()
            self._stop.wait(max(5, self.config.NOTIFY_INTERVAL_SECONDS))


_monitor: AlertMonitor | None = None


def start_alert_monitor() -> AlertMonitor | None:
    """Start the process-wide monitor when notifications are configured."""
    global _monitor
    if not Config.NOTIFY_ENABLED:
        return None
    if not (Config.NOTIFY_EMAIL_ENABLED or Config.NOTIFY_TEAMS_ENABLED):
        log.warning("NOTIFY_ENABLED is set but no notification channel is configured")
        return None
    if _monitor is None:
        _monitor = AlertMonitor()
        _monitor.start()
    return _monitor
