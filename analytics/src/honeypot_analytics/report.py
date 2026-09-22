"""The daily Discord report."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import urllib.request

import duckdb

from .db import sql_literal
from .publish import attach

log = logging.getLogger(__name__)

TOP_N = 5

# Discord rejects a message body over this, and one of the commands in the real
# data is 652 bytes by itself.
DISCORD_LIMIT = 2000
COMMAND_WIDTH = 70


def _shorten(text: str, width: int = COMMAND_WIDTH) -> str:
    """One line, no longer than width. Attacker commands are neither."""
    flat = " ".join(text.split())
    if len(flat) <= width:
        return flat
    return flat[: width - 1] + "…"


def format_report(day: dt.date, summary: dict) -> str:
    lines = [
        f"Cowrie honeypot, {day.isoformat()} (UTC)",
        "```",
        f"sessions          {summary['sessions']}",
        f"source addresses  {summary['source_ips']} in {summary['networks']} /24s",
        f"logins succeeded  {summary['logins_succeeded']}",
        f"commands run      {summary['commands']}",
    ]

    if summary["top_networks"]:
        lines.append("")
        lines.append("busiest networks")
        for net, count in summary["top_networks"]:
            lines.append(f"  {count:>6}  {net}")

    if summary["top_credentials"]:
        lines.append("")
        lines.append("most tried logins")
        for user, password, count in summary["top_credentials"]:
            lines.append(f"  {count:>6}  {user} / {password}")

    if summary["top_commands"]:
        lines.append("")
        lines.append("most run commands")
        for command, count in summary["top_commands"]:
            lines.append(f"  {count:>6}  {_shorten(command)}")

    lines.append("```")
    text = "\n".join(lines)

    # Shortening each command bounds the usual case. This is the backstop for
    # the one that still does not fit, and it keeps the code fence closed so
    # Discord does not render the rest of the channel as code.
    if len(text) > DISCORD_LIMIT:
        keep = DISCORD_LIMIT - len("\n…\n```")
        text = text[:keep].rsplit("\n", 1)[0] + "\n…\n```"
    return text


def _pg(con: duckdb.DuckDBPyConnection, sql: str):
    """Run one statement on the postgres side and bring the rows back.

    Through the passthrough rather than the attached tables, because the
    network rollup uses inet functions that only postgres has. Every column
    needs its own alias: postgres_query refuses a result with two called
    count.
    """
    return con.execute(
        "SELECT * FROM postgres_query(?, ?)", ["pg", sql]
    ).fetchall()


def daily_summary(con: duckdb.DuckDBPyConnection, day: dt.date) -> dict:
    """The numbers one day's report is built from."""
    attach(con)
    where = f"day = CAST({sql_literal(day.isoformat())} AS DATE)"

    totals = _pg(
        con,
        "SELECT count(*) AS sessions,"
        " count(DISTINCT src_ip) AS source_ips,"
        " count(DISTINCT set_masklen(src_ip::cidr, 24)) AS networks,"
        " count(*) FILTER (WHERE login_success) AS logins_succeeded"
        f" FROM sessions WHERE {where}",
    )[0]

    commands = _pg(
        con, f"SELECT count(*) AS commands FROM commands WHERE {where}"
    )[0][0]

    # The tie breakers are there so a quiet day does not reorder itself
    # between two runs of the same report.
    top_networks = _pg(
        con,
        "SELECT set_masklen(src_ip::cidr, 24)::text AS network,"
        " count(*) AS sessions"
        f" FROM sessions WHERE {where}"
        f" GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {TOP_N}",
    )
    top_credentials = _pg(
        con,
        "SELECT username AS who, password AS secret, count(*) AS tries"
        f" FROM login_attempts WHERE {where}"
        f" GROUP BY 1, 2 ORDER BY 3 DESC, 1, 2 LIMIT {TOP_N}",
    )
    top_commands = _pg(
        con,
        "SELECT input AS command, count(*) AS runs"
        f" FROM commands WHERE {where}"
        f" GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {TOP_N}",
    )

    return {
        "sessions": totals[0],
        "source_ips": totals[1],
        "networks": totals[2],
        "logins_succeeded": totals[3],
        "commands": commands,
        "top_networks": [tuple(r) for r in top_networks],
        "top_credentials": [tuple(r) for r in top_credentials],
        "top_commands": [tuple(r) for r in top_commands],
    }


def post_report(webhook_url: str, text: str) -> None:
    """Send one message to a Discord webhook.

    urllib rather than a client library, since this is one POST and the image
    already carries enough. A non 2xx raises out of urlopen on its own, which
    is what the job should do with it.
    """
    body = json.dumps({"content": text}).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30):
        pass


def send_daily_report(
    con: duckdb.DuckDBPyConnection, day: dt.date, webhook_url: str
) -> str:
    """Build the day's report, send it, and hand back what was sent."""
    text = format_report(day, daily_summary(con, day))
    post_report(webhook_url, text)
    log.info("%s: reported %d characters to discord", day, len(text))
    return text


def webhook_url() -> str:
    """Where the report goes. No default; a job with nowhere to post fails."""
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        raise RuntimeError(
            "DISCORD_WEBHOOK_URL is not set. On the cluster it comes from the "
            "honeypot-discord sealed secret."
        )
    return url
