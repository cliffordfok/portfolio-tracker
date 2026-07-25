"""Command line entrypoints used by Hermes and systemd."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import PortfolioError
from .ledger import LedgerStore, atomic_write_json
from .publisher import SnapshotPublisher, client_from_environment
from .schemas import validate_event
from .snapshot import build_snapshot


def _signal_publish(root: Path, revision: int) -> None:
    atomic_write_json(
        root / "state" / "publish.pending",
        {"revision": revision, "requested_by": "portfolio-tracker-cli"},
    )


def _event_from_args(args: argparse.Namespace) -> dict:
    if args.json:
        payload = args.json
    else:
        payload = Path(args.file).read_text(encoding="utf-8")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("event payload must be a JSON object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="portfolio-tracker")
    parser.add_argument(
        "--root",
        default=".",
        help="private runtime root containing ledger/, snapshots/, state/, locks/",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    validate = subcommands.add_parser("validate", help="validate one event")
    source = validate.add_mutually_exclusive_group(required=True)
    source.add_argument("--json")
    source.add_argument("--file")

    append = subcommands.add_parser("append", help="validate and append one event")
    source = append.add_mutually_exclusive_group(required=True)
    source.add_argument("--json")
    source.add_argument("--file")
    append.add_argument(
        "--rebuild",
        action="store_true",
        help="rebuild snapshot after a successful append",
    )

    repair = subcommands.add_parser("repair-tail", help="repair one truncated tail")
    repair.add_argument("portfolio", choices=("paper", "live", "market"))

    rebuild = subcommands.add_parser("rebuild", help="rebuild public snapshot")
    rebuild.add_argument("--output")

    publish = subcommands.add_parser("publish", help="publish snapshot to GitHub")
    publish.add_argument("--repository", required=True, help="owner/repository")
    publish.add_argument("--branch", default="portfolio-data")
    publish.add_argument("--path", default="data/portfolio-snapshot.json")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.command == "validate":
            event = _event_from_args(args)
            validate_event(event)
            result = {"status": "valid", "event_id": event["event_id"]}
        elif args.command == "append":
            event = _event_from_args(args)
            result = LedgerStore(root).append(event)
            if args.rebuild and result["status"] == "appended":
                snapshot = build_snapshot(root)
                _signal_publish(root, snapshot["revision"])
                result["snapshot_rebuilt"] = True
        elif args.command == "repair-tail":
            events = LedgerStore(root).repair_tail(args.portfolio)
            result = {"status": "repaired", "records": len(events)}
        elif args.command == "rebuild":
            snapshot = build_snapshot(root, output=args.output)
            _signal_publish(root, snapshot["revision"])
            result = {
                "status": "rebuilt",
                "revision": snapshot["revision"],
                "warnings": snapshot["warnings"],
            }
        elif args.command == "publish":
            client = client_from_environment(
                repository=args.repository,
                branch=args.branch,
                path=args.path,
            )
            result = SnapshotPublisher(root=root, client=client).publish()
        else:
            raise ValueError(f"unknown command: {args.command}")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except (PortfolioError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
