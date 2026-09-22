"""Command line entry point. One subcommand per pipeline step."""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from . import db, publish, report
from .db import connect
from .normalize import NoDataForDay, normalize_range


def _parse_date(value: str):
    # An empty string means "not given". Argo has no way to omit a parameter,
    # so the workflow passes "" on a scheduled run and a real date on a
    # backfill.
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a date in YYYY-MM-DD form"
        ) from None


def _yesterday() -> dt.date:
    # UTC, which is what the droplet rotates on despite sitting in Singapore,
    # so a day's partition holds exactly that UTC day. Storage and aggregation
    # stay in UTC everywhere and only the display converts.
    return dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="honeypot-analytics")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="log every statement"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    norm = sub.add_parser(
        "normalize", help="rewrite a day's bronze logs as silver parquet"
    )
    norm.add_argument(
        "--date", type=_parse_date, help="single day (default: yesterday, UTC)"
    )
    norm.add_argument("--from", dest="start", type=_parse_date)
    norm.add_argument("--to", dest="end", type=_parse_date)
    norm.add_argument(
        "--allow-missing",
        action="store_true",
        help="skip days with nothing in B2 instead of failing",
    )

    pub = sub.add_parser(
        "publish",
        help="load a day of silver into postgres for grafana",
    )
    pub.add_argument(
        "--date", type=_parse_date, help="single day (default: yesterday, UTC)"
    )
    pub.add_argument("--from", dest="start", type=_parse_date)
    pub.add_argument("--to", dest="end", type=_parse_date)

    rep = sub.add_parser(
        "report",
        help="post a day's summary to the discord webhook",
    )
    rep.add_argument(
        "--date", type=_parse_date, help="single day (default: yesterday, UTC)"
    )
    rep.add_argument("--from", dest="start", type=_parse_date)
    rep.add_argument("--to", dest="end", type=_parse_date)

    sub.add_parser(
        "init-views",
        help="create the notebook views in the workbench database",
    )
    sub.add_parser(
        "init-db",
        help="create the postgres tables publish writes to",
    )
    return parser


def _resolve_range(args: argparse.Namespace) -> tuple[dt.date, dt.date]:
    if args.date and (args.start or args.end):
        raise SystemExit("--date cannot be combined with --from or --to")
    if args.date:
        return args.date, args.date
    if args.start or args.end:
        if not (args.start and args.end):
            raise SystemExit("--from and --to go together")
        if args.start > args.end:
            raise SystemExit("--from is after --to")
        return args.start, args.end
    day = _yesterday()
    return day, day


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.command == "init-views":
        con = db.connect(db.workbench_db())
        try:
            names = db.init_views(con)
        finally:
            con.close()
        print("created " + ", ".join(names) + " in " + str(db.workbench_db()))
        return 0

    if args.command == "init-db":
        con = connect()
        try:
            publish.create_schema(con)
        finally:
            con.close()
        print("created the publish tables")
        return 0

    if args.command == "publish":
        start, end = _resolve_range(args)
        con = connect()
        try:
            results = publish.publish_range(con, start, end)
        finally:
            con.close()

        for day, counts in results.items():
            print(f"{day}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        return 0

    if args.command == "report":
        start, end = _resolve_range(args)
        webhook = report.webhook_url()
        con = connect()
        try:
            day = start
            while day <= end:
                report.send_daily_report(con, day, webhook)
                print(f"{day}: reported")
                day += dt.timedelta(days=1)
        finally:
            con.close()
        return 0

    if args.command == "normalize":
        start, end = _resolve_range(args)
        con = connect()
        try:
            results = normalize_range(
                con, start, end, allow_missing=args.allow_missing
            )
        except NoDataForDay as exc:
            print(exc, file=sys.stderr)
            return 1
        finally:
            con.close()

        total = sum(results.values())
        print(f"normalized {len(results)} day(s), {total} rows")
        return 0

    raise SystemExit(f"unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
