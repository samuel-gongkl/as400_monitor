import json
import unittest
from unittest.mock import MagicMock, patch

from notifications import (EmailNotifier, NotificationEvent, TeamsNotifier,
                           evaluate_metrics)


class NotificationConfig:
    NOTIFY_EMAIL_ENABLED = True
    NOTIFY_EMAIL_SMTP_HOST = "smtp.example.test"
    NOTIFY_EMAIL_SMTP_PORT = 587
    NOTIFY_EMAIL_USER = "monitor@example.test"
    NOTIFY_EMAIL_PASSWORD = "secret"
    NOTIFY_EMAIL_FROM = "monitor@example.test"
    NOTIFY_EMAIL_TO = "ops@example.test; oncall@example.test"
    NOTIFY_EMAIL_USE_TLS = True
    NOTIFY_EMAIL_USE_SSL = False
    NOTIFY_TEAMS_ENABLED = True
    NOTIFY_TEAMS_WEBHOOK_URL = "https://teams.example.test/hook"
    NOTIFY_HTTP_TIMEOUT_SECONDS = 3
    NOTIFY_CPU_WARN_PCT = 75.0
    NOTIFY_CPU_CRIT_PCT = 90.0
    NOTIFY_ASP_WARN_PCT = 80.0
    NOTIFY_ASP_CRIT_PCT = 90.0


def event(severity="critical"):
    return NotificationEvent(
        key="system.cpu", lpar="LPAR1", title="System CPU",
        severity=severity, summary="CPU is high", details={"CPU %": "95.0%"})


class NotificationTests(unittest.TestCase):
    def test_evaluate_system_and_table_alerts(self):
        metrics = [
            {"id": "system", "title": "System Status", "ok": True,
             "data": {"cpu_pct": 95, "asp_used_pct": 82}},
            {"id": "disk", "title": "Disk Units", "ok": True,
             "data": {"columns": ["Unit", "% Used"],
                      "rows": [["1", 92]], "levels": ["alert"]}},
        ]
        alerts = evaluate_metrics("lpar1", metrics, NotificationConfig)
        self.assertEqual({a.key for a in alerts}, {"system.cpu", "system.asp",
                                                   "disk.1"})
        self.assertEqual(next(a for a in alerts if a.key == "system.cpu").severity,
                         "critical")
        self.assertEqual(next(a for a in alerts if a.key == "system.asp").severity,
                         "warning")

    @patch("notifications.smtplib.SMTP")
    def test_email_uses_tls_and_all_recipients(self, smtp_cls):
        smtp = smtp_cls.return_value.__enter__.return_value
        EmailNotifier(NotificationConfig).send(event())
        smtp.starttls.assert_called_once_with()
        smtp.login.assert_called_once_with("monitor@example.test", "secret")
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "ops@example.test, oncall@example.test")
        self.assertIn("CRITICAL", message["Subject"])

    @patch("notifications.urllib.request.urlopen")
    def test_teams_posts_json_message_card(self, urlopen):
        response = MagicMock(status=200)
        urlopen.return_value.__enter__.return_value = response
        TeamsNotifier(NotificationConfig).send(event("warning"))
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["@type"], "MessageCard")
        self.assertEqual(payload["themeColor"], "FFB900")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 3)


if __name__ == "__main__":
    unittest.main()

class AlertMonitorTests(unittest.TestCase):
    def test_alerts_are_deduplicated_and_recovery_is_sent(self):
        from notifications import AlertMonitor

        config = type("MonitorConfig", (NotificationConfig,), {
            "LPARS": [{"key": "lpar1"}],
            "NOTIFY_REPEAT_MINUTES": 0,
        })
        high = [{"id": "system", "title": "System Status", "ok": True,
                 "data": {"cpu_pct": 95, "asp_used_pct": 20}}]
        clear = [{"id": "system", "title": "System Status", "ok": True,
                 "data": {"cpu_pct": 20, "asp_used_pct": 20}}]
        manager = MagicMock()
        manager.send.return_value = ["email"]
        monitor = AlertMonitor(config, manager)
        with patch("collectors.collect_all", side_effect=[high, high, clear]):
            monitor.check_once()
            monitor.check_once()
            monitor.check_once()
        self.assertEqual(manager.send.call_count, 2)
        self.assertEqual(manager.send.call_args_list[0].args[0].severity,
                         "critical")
        self.assertEqual(manager.send.call_args_list[1].args[0].severity,
                         "recovery")
