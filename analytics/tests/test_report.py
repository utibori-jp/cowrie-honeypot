"""The daily Discord report.

Formatting is pure and gets the most attention here, since that is where the
message can quietly break: Discord rejects anything over 2000 characters, and
one of the commands attackers actually run is 652 bytes on its own.
"""

from __future__ import annotations

import datetime as dt
import http.server
import json
import threading
import urllib.error

import pytest

from honeypot_analytics import db, normalize, publish, report

DAY = dt.date(2026, 9, 20)

SUMMARY = {
    "sessions": 2781,
    "source_ips": 303,
    "networks": 97,
    "logins_succeeded": 2298,
    "commands": 1966,
    "top_networks": [("109.160.32.0/24", 1437), ("185.246.128.0/24", 413)],
    "top_credentials": [("root", "123456", 88), ("admin", "admin", 41)],
    "top_commands": [("uname -s -v -n -r -m", 1204), ("whoami", 33)],
    "stored_days": 9,
    "stored_sessions": 22719,
    "stored_bytes": 12_582_912,
}


def test_report_carries_the_headline_numbers():
    text = report.format_report(DAY, SUMMARY)

    assert "2026-09-20" in text
    assert "2781" in text
    assert "109.160.32.0/24" in text
    assert "uname -s -v -n -r -m" in text


def test_a_huge_command_cannot_push_the_message_past_discord_limit():
    # The fingerprinting one liner in the real data is 652 bytes, and there is
    # nothing stopping a longer one arriving tomorrow.
    summary = dict(SUMMARY, top_commands=[("x" * 4000, 12), ("y" * 4000, 3)])

    text = report.format_report(DAY, summary)

    assert len(text) <= report.DISCORD_LIMIT
    assert "12" in text


def test_a_quiet_day_still_produces_a_report():
    summary = {
        "sessions": 0,
        "source_ips": 0,
        "networks": 0,
        "logins_succeeded": 0,
        "commands": 0,
        "top_networks": [],
        "top_credentials": [],
        "top_commands": [],
        "stored_days": 0,
        "stored_sessions": 0,
        "stored_bytes": 0,
    }

    text = report.format_report(DAY, summary)

    assert "2026-09-20" in text
    assert len(text) <= report.DISCORD_LIMIT


def _event(eventid, session="s1", ts="2026-09-20 01:02:03.000000", ip="1.2.3.4", **extra):
    return {
        "eventid": eventid,
        "session": session,
        "src_ip": ip,
        "timestamp": ts,
        **extra,
    }


@pytest.mark.needs_pg
def test_daily_summary_counts_what_the_report_shows(env, write_bronze):
    write_bronze(
        env / "bronze",
        DAY,
        [
            _event("cowrie.session.connect"),
            _event("cowrie.login.success", username="root", password="123456"),
            _event("cowrie.command.input", input="uname -a"),
            _event("cowrie.session.closed", duration_ms=1000),
            _event("cowrie.session.connect", session="s2", ip="1.2.3.9"),
            _event("cowrie.login.failed", session="s2", username="root",
                   password="123456"),
            _event("cowrie.session.closed", session="s2", duration_ms=500),
            _event("cowrie.session.connect", session="s3", ip="9.9.9.9"),
            _event("cowrie.session.closed", session="s3", duration_ms=500),
        ],
    )
    con = db.connect()
    normalize.normalize_day(con, DAY)
    publish.create_schema(con)
    publish.publish_day(con, DAY)

    summary = report.daily_summary(con, DAY)

    assert summary["sessions"] == 3
    assert summary["source_ips"] == 3
    # 1.2.3.4 and 1.2.3.9 share a /24, 9.9.9.9 does not.
    assert summary["networks"] == 2
    assert summary["logins_succeeded"] == 1
    assert summary["commands"] == 1
    assert summary["top_networks"][0] == ("1.2.3.0/24", 2)
    assert summary["top_credentials"][0] == ("root", "123456", 2)
    assert summary["top_commands"][0] == ("uname -a", 1)
    # What has piled up, not just this day.
    assert summary["stored_days"] == 1
    assert summary["stored_sessions"] == 3
    assert summary["stored_bytes"] > 0


def test_post_sends_the_text_as_a_discord_content_field():
    # A real socket rather than a mocked one: what matters is the bytes that
    # leave the process, and a mock would only repeat what we told it.
    received = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received["body"] = json.loads(self.rfile.read(length))
            received["type"] = self.headers["Content-Type"]
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/webhook"

    report.post_report(url, "hello from the honeypot")

    assert received["body"] == {"content": "hello from the honeypot"}
    assert received["type"] == "application/json"


def test_post_raises_when_discord_rejects_it():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            # Drain the body before answering. Closing the socket on a client
            # that is still sending aborts the connection on Windows, and the
            # test would see that instead of the status it is about.
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(400)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/webhook"

    with pytest.raises(urllib.error.HTTPError):
        report.post_report(url, "anything")


def test_report_takes_the_same_date_arguments_as_the_other_steps():
    from honeypot_analytics import cli

    args = cli.build_parser().parse_args(["report", "--date", ""])
    assert cli._resolve_range(args) == (cli._yesterday(), cli._yesterday())


def test_a_missing_username_or_password_reads_as_absent():
    # Cowrie emits login events without a password field, and "None" in the
    # report looks like a bug rather than a fact about the attempt.
    summary = dict(SUMMARY, top_credentials=[("admin", None, 14), (None, "x", 3)])

    text = report.format_report(DAY, summary)

    assert "None" not in text
    assert "admin / -" in text


def test_report_says_how_much_has_piled_up():
    text = report.format_report(DAY, SUMMARY)

    assert "9 days" in text
    assert "22719" in text
    assert "12.0 MB" in text


def test_bronze_size_appears_only_when_it_was_measured():
    without = report.format_report(DAY, SUMMARY)
    assert "in B2" not in without

    with_b2 = report.format_report(DAY, dict(SUMMARY, bronze_bytes=7_340_032))
    assert "7.0 MB in B2" in with_b2
