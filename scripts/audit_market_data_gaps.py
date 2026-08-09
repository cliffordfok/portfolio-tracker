#!/usr/bin/env python3
"""Classify snapshot market-data gaps against explicit accepted decisions."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from portfolio_tracker.errors import ValidationError
from portfolio_tracker.market_time import is_nyse_session
from portfolio_tracker.schemas import INSTRUMENT_ID_RE, SYMBOL_RE
from portfolio_tracker.snapshot import validate_snapshot


DEFAULT_ACCEPTANCE_FILE = REPO_ROOT / "config" / "accepted_market_data_gaps.json"
GapKey = tuple[str, str, str]


def _require_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _calendar_date(value: Any, *, label: str) -> tuple[str, date]:
    text = _require_string(value, label=label)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{label} must use YYYY-MM-DD")
    return text, parsed


def _session_date(value: Any, *, label: str) -> str:
    text, session = _calendar_date(value, label=label)
    if not is_nyse_session(session):
        raise ValueError(f"{label} must be an NYSE trading session")
    return text


def parse_acceptance_manifest(
    manifest: Mapping[str, Any],
) -> tuple[dict[GapKey, dict[str, str]], dict[str, dict[str, str]]]:
    """Validate a manifest and return exact accepted keys and metric policy."""

    if manifest.get("schema_version") != 1:
        raise ValueError("acceptance manifest schema_version must be 1")
    decisions = manifest.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("acceptance manifest decisions must be an array")

    accepted: dict[GapKey, dict[str, str]] = {}
    expected_metrics: dict[str, dict[str, str]] = {}
    decision_ids: set[str] = set()
    for index, decision in enumerate(decisions):
        label = f"decisions[{index}]"
        if not isinstance(decision, Mapping):
            raise ValueError(f"{label} must be an object")
        decision_id = _require_string(
            decision.get("decision_id"), label=f"{label}.decision_id"
        )
        if decision_id in decision_ids:
            raise ValueError(f"duplicate decision_id: {decision_id}")
        decision_ids.add(decision_id)
        if decision.get("status") != "accepted":
            raise ValueError(f"{label}.status must be accepted")
        portfolio = _require_string(
            decision.get("portfolio"), label=f"{label}.portfolio"
        )
        if decision.get("decision") != "do_not_backfill":
            raise ValueError(f"{label}.decision must be do_not_backfill")
        _calendar_date(
            decision.get("accepted_on"), label=f"{label}.accepted_on"
        )
        _require_string(decision.get("reason"), label=f"{label}.reason")

        expected = decision.get("expected_snapshot")
        if not isinstance(expected, Mapping):
            raise ValueError(f"{label}.expected_snapshot must be an object")
        expected_value = {
            "performance_effective_date": _session_date(
                expected.get("performance_effective_date"),
                label=(
                    f"{label}.expected_snapshot.performance_effective_date"
                ),
            ),
            "performance_scope": _require_string(
                expected.get("performance_scope"),
                label=f"{label}.expected_snapshot.performance_scope",
            ),
        }
        previous_expected = expected_metrics.get(portfolio)
        if previous_expected is not None and previous_expected != expected_value:
            raise ValueError(
                f"conflicting expected snapshot metrics for {portfolio}"
            )
        expected_metrics[portfolio] = expected_value

        gaps = decision.get("gaps")
        if not isinstance(gaps, list) or not gaps:
            raise ValueError(f"{label}.gaps must be a non-empty array")
        for gap_index, gap in enumerate(gaps):
            gap_label = f"{label}.gaps[{gap_index}]"
            if not isinstance(gap, Mapping):
                raise ValueError(f"{gap_label} must be an object")
            instrument_id = _require_string(
                gap.get("instrument_id"),
                label=f"{gap_label}.instrument_id",
            )
            if not INSTRUMENT_ID_RE.fullmatch(instrument_id):
                raise ValueError(f"{gap_label}.instrument_id is invalid")
            symbol = _require_string(
                gap.get("symbol"), label=f"{gap_label}.symbol"
            )
            if not SYMBOL_RE.fullmatch(symbol):
                raise ValueError(f"{gap_label}.symbol is invalid")
            session = _session_date(
                gap.get("session_date"),
                label=f"{gap_label}.session_date",
            )
            key = (portfolio, symbol, session)
            if key in accepted:
                raise ValueError(
                    "duplicate accepted market-data gap: " + "/".join(key)
                )
            accepted[key] = {
                "decision_id": decision_id,
                "instrument_id": instrument_id,
            }
    return accepted, expected_metrics


def _actual_gap_keys(snapshot: Mapping[str, Any]) -> set[GapKey]:
    actual: set[GapKey] = set()
    portfolios = snapshot.get("portfolios", {})
    if isinstance(portfolios, Mapping):
        for portfolio, payload in portfolios.items():
            if not isinstance(payload, Mapping):
                continue
            daily = payload.get("daily", [])
            if not isinstance(daily, list):
                continue
            for point in daily:
                if (
                    not isinstance(point, Mapping)
                    or point.get("data_status") != "INSUFFICIENT_MARKET_DATA"
                ):
                    continue
                session_text = point.get("date")
                try:
                    session = date.fromisoformat(session_text)
                except (TypeError, ValueError):
                    continue
                if not is_nyse_session(session):
                    continue
                for symbol in point.get("missing_symbols", []):
                    if isinstance(symbol, str):
                        actual.add((str(portfolio), symbol, session_text))

    benchmark = snapshot.get("benchmark")
    if isinstance(benchmark, Mapping):
        symbol = benchmark.get("symbol")
        for point in benchmark.get("daily", []):
            if (
                isinstance(symbol, str)
                and isinstance(point, Mapping)
                and point.get("data_status") == "INSUFFICIENT_MARKET_DATA"
            ):
                session_text = point.get("date")
                try:
                    session = date.fromisoformat(session_text)
                except (TypeError, ValueError):
                    continue
                if is_nyse_session(session):
                    actual.add(("benchmark", symbol, session_text))
    return actual


def _gap_record(
    key: GapKey, accepted: Mapping[GapKey, Mapping[str, str]]
) -> dict[str, str]:
    portfolio, symbol, session = key
    record = {
        "portfolio": portfolio,
        "symbol": symbol,
        "session_date": session,
    }
    metadata = accepted.get(key)
    if metadata is not None:
        record.update(metadata)
    return record


def audit_snapshot(
    snapshot: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Classify exact accepted, new, resolved, and metric-drift conditions."""

    accepted, expected_metrics = parse_acceptance_manifest(manifest)
    accepted_keys = set(accepted)
    actual = _actual_gap_keys(snapshot)
    accepted_present = actual & accepted_keys
    unreviewed = actual - accepted_keys
    resolved = accepted_keys - actual

    metric_mismatches: list[dict[str, Any]] = []
    portfolios = snapshot.get("portfolios", {})
    for portfolio, expected in sorted(expected_metrics.items()):
        payload = (
            portfolios.get(portfolio, {})
            if isinstance(portfolios, Mapping)
            else {}
        )
        metrics = (
            payload.get("metrics", {})
            if isinstance(payload, Mapping)
            else {}
        )
        for field, expected_value in expected.items():
            actual_value = (
                metrics.get(field) if isinstance(metrics, Mapping) else None
            )
            if actual_value != expected_value:
                metric_mismatches.append(
                    {
                        "portfolio": portfolio,
                        "field": field,
                        "expected": expected_value,
                        "actual": actual_value,
                    }
                )

    if unreviewed or metric_mismatches:
        status = "review_required"
    elif resolved:
        status = "stale_acceptance"
    elif actual:
        status = "accepted_with_known_gaps"
    else:
        status = "clean"

    return {
        "status": status,
        "snapshot_revision": snapshot.get("revision"),
        "actual_gap_count": len(actual),
        "accepted_gap_count": len(accepted_keys),
        "accepted_present_count": len(accepted_present),
        "unreviewed_gap_count": len(unreviewed),
        "resolved_acceptance_count": len(resolved),
        "metric_mismatch_count": len(metric_mismatches),
        "accepted_gaps": [
            _gap_record(key, accepted) for key in sorted(accepted_present)
        ],
        "unreviewed_gaps": [
            _gap_record(key, accepted) for key in sorted(unreviewed)
        ],
        "resolved_acceptances": [
            _gap_record(key, accepted) for key in sorted(resolved)
        ],
        "metric_mismatches": metric_mismatches,
    }


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="audit-market-data-gaps")
    source = command.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--root",
        help="portfolio runtime root containing snapshots/portfolio-snapshot.json",
    )
    source.add_argument("--snapshot", help="explicit portfolio snapshot path")
    command.add_argument(
        "--acceptance-file",
        default=str(DEFAULT_ACCEPTANCE_FILE),
        help="accepted gap manifest path",
    )
    return command


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    snapshot_path = (
        Path(args.snapshot)
        if args.snapshot
        else Path(args.root) / "snapshots" / "portfolio-snapshot.json"
    )
    try:
        snapshot = _read_json(snapshot_path)
        validate_snapshot(snapshot)
        manifest = _read_json(Path(args.acceptance_file))
        report = audit_snapshot(snapshot, manifest)
    except (OSError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        print(
            json.dumps(
                {"status": "invalid_input", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2

    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] in {"clean", "accepted_with_known_gaps"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
