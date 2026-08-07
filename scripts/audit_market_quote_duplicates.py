#!/usr/bin/env python3
"""Classify duplicate effective market keys without mutating the ledger."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from portfolio_tracker.ledger import LedgerStore


SPLIT_PREFIX = "market-split-basis-"


def _market_key(event: dict[str, Any]) -> tuple[str, str, str]:
    identity = event.get("instrument_id") or event["symbol"]
    return event["action"], identity, event["session_date"]


def audit_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    total = 0
    for event in events:
        if event.get("action") not in {"QUOTE", "BENCHMARK_CLOSE"}:
            continue
        total += 1
        groups[_market_key(event)].append(event)

    duplicate_groups = {
        key: values for key, values in groups.items() if len(values) > 1
    }
    split_overlay_groups = 0
    provider_retry_groups = 0
    other_duplicate_groups = 0
    retry_samples: list[dict[str, Any]] = []
    other_samples: list[dict[str, Any]] = []

    for key, values in sorted(duplicate_groups.items()):
        split_values = [
            event
            for event in values
            if event["event_id"].startswith(SPLIT_PREFIX)
        ]
        cron_values = [
            event
            for event in values
            if event.get("source") in {"cron-quote", "cron-benchmark"}
            and not event["event_id"].startswith(SPLIT_PREFIX)
        ]
        other_values = [
            event
            for event in values
            if event not in split_values and event not in cron_values
        ]
        has_split_overlay = bool(split_values)
        if has_split_overlay:
            split_overlay_groups += 1
        if len(cron_values) > 1:
            provider_retry_groups += 1
            if len(retry_samples) < 20:
                retry_samples.append(
                    {
                        "action": key[0],
                        "identity": key[1],
                        "session_date": key[2],
                        "cron_event_ids": [
                            event["event_id"] for event in cron_values
                        ],
                    }
                )
        unexpected = bool(other_values) or (
            has_split_overlay and len(cron_values) != 1
        ) or (not has_split_overlay and len(cron_values) <= 1)
        if unexpected:
            other_duplicate_groups += 1
            if len(other_samples) < 20:
                other_samples.append(
                    {
                        "action": key[0],
                        "identity": key[1],
                        "session_date": key[2],
                        "event_ids": [event["event_id"] for event in values],
                    }
                )

    review_required = provider_retry_groups > 0 or other_duplicate_groups > 0
    return {
        "status": "review_required" if review_required else "clean",
        "market_event_count": total,
        "effective_key_count": len(groups),
        "duplicate_group_count": len(duplicate_groups),
        "split_overlay_group_count": split_overlay_groups,
        "provider_retry_group_count": provider_retry_groups,
        "other_duplicate_group_count": other_duplicate_groups,
        "provider_retry_samples": retry_samples,
        "other_duplicate_samples": other_samples,
    }


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="audit-market-quote-duplicates")
    command.add_argument("--root", required=True)
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    report = audit_events(LedgerStore(Path(args.root)).read("market"))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if report["status"] == "review_required" else 0


if __name__ == "__main__":
    raise SystemExit(main())
