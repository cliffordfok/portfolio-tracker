"""Convert split-adjusted historical quotes to session-native close prices.

Yahoo/yfinance historical ``Close`` values are normalized for later stock
splits. Broker trades and share counts in the Live ledger remain on the basis
that existed on each trade date. This migration appends corrected QUOTE events
whose close equals:

    provider_close * product(later split ratios)

The original market events are never edited. A persisted, hash-bound plan makes
the operation reviewable, idempotent, and safe to resume after a partial batch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from portfolio_tracker.backup import backup_ledgers
from portfolio_tracker.decimal_utils import price
from portfolio_tracker.errors import ConflictError, PortfolioError
from portfolio_tracker.ledger import LedgerStore, atomic_write_json
from portfolio_tracker.resolver import resolve_effective_events
from portfolio_tracker.schemas import (
    KNOWN_LIVE_ETF_SYMBOLS,
    normalize_event,
    parse_timestamp,
    validate_event,
)
from portfolio_tracker.snapshot import build_snapshot_if_needed

PLAN_SCHEMA_VERSION = 1
MARKET_EVENT_PREFIX = "market-split-basis-"
QUOTE_BASIS = "YFINANCE_CLOSE_SPLIT_ADJUSTED"


class SplitQuoteMigrationError(ValueError):
    """Raised when quote-basis correction cannot be proven safe."""


@dataclass(frozen=True)
class SplitFact:
    symbol: str
    instrument_id: str
    effective_session: str
    numerator: Decimal
    denominator: Decimal

    @property
    def ratio(self) -> Decimal:
        return self.numerator / self.denominator


@dataclass
class MigrationPlan:
    candidates: list[dict[str, Any]]
    pending: int
    duplicates: int
    corrected_sessions: int
    split_facts: int
    already_corrected: bool


def _canonical_event_digest(events: Iterable[dict[str, Any]]) -> str:
    payload = json.dumps(
        list(events),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _source_market_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if not event["event_id"].startswith(MARKET_EVENT_PREFIX)
    ]


def _source_live_digest(events: list[dict[str, Any]]) -> str:
    return _canonical_event_digest(events)


def _source_market_digest(events: list[dict[str, Any]]) -> str:
    return _canonical_event_digest(_source_market_events(events))


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SplitQuoteMigrationError(f"{field} must be decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise SplitQuoteMigrationError(f"{field} must be greater than zero")
    return parsed


def _split_facts(payload: dict[str, Any]) -> list[SplitFact]:
    raw = payload.get("splits")
    if not isinstance(raw, list):
        raise SplitQuoteMigrationError("plan.splits must be an array")
    facts: list[SplitFact] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise SplitQuoteMigrationError(f"plan split {index} must be an object")
        expected = {
            "symbol",
            "instrument_id",
            "effective_session",
            "numerator",
            "denominator",
        }
        if set(item) != expected:
            raise SplitQuoteMigrationError(
                f"plan split {index} fields do not match schema"
            )
        symbol = item["symbol"]
        instrument_id = item["instrument_id"]
        effective_session = item["effective_session"]
        if not isinstance(symbol, str) or not symbol:
            raise SplitQuoteMigrationError(f"plan split {index} symbol is invalid")
        if not isinstance(instrument_id, str) or not instrument_id:
            raise SplitQuoteMigrationError(
                f"plan split {index} instrument_id is invalid"
            )
        if not isinstance(effective_session, str):
            raise SplitQuoteMigrationError(
                f"plan split {index} effective_session is invalid"
            )
        try:
            parsed_session = date.fromisoformat(effective_session)
        except ValueError as exc:
            raise SplitQuoteMigrationError(
                f"plan split {index} effective_session is invalid"
            ) from exc
        if parsed_session.isoformat() != effective_session:
            raise SplitQuoteMigrationError(
                f"plan split {index} effective_session is invalid"
            )
        fact = SplitFact(
            symbol=symbol,
            instrument_id=instrument_id,
            effective_session=effective_session,
            numerator=_decimal(item["numerator"], field="numerator"),
            denominator=_decimal(item["denominator"], field="denominator"),
        )
        key = (fact.instrument_id, fact.effective_session)
        if key in seen:
            raise SplitQuoteMigrationError(
                "plan contains duplicate instrument/session split"
            )
        seen.add(key)
        facts.append(fact)
    return sorted(
        facts,
        key=lambda fact: (fact.instrument_id, fact.effective_session),
    )


def _load_plan(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SplitQuoteMigrationError("split quote plan is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise SplitQuoteMigrationError("split quote plan must be an object")
    expected = {
        "schema_version",
        "created_at",
        "quote_basis",
        "live_event_digest",
        "market_event_digest",
        "splits",
    }
    if set(payload) != expected:
        raise SplitQuoteMigrationError("split quote plan fields do not match schema")
    if payload["schema_version"] != PLAN_SCHEMA_VERSION:
        raise SplitQuoteMigrationError("unsupported split quote plan schema")
    parse_timestamp(payload["created_at"], field="plan.created_at")
    if payload["quote_basis"] != QUOTE_BASIS:
        raise SplitQuoteMigrationError("plan quote_basis is unsupported")
    for field in ("live_event_digest", "market_event_digest"):
        digest = payload[field]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise SplitQuoteMigrationError(f"plan.{field} is invalid")
    _split_facts(payload)
    return payload


def _quote_matches_fact(event: dict[str, Any], fact: SplitFact) -> bool:
    event_id = event.get("instrument_id")
    if event_id is not None:
        return event_id == fact.instrument_id
    return event.get("symbol") == fact.symbol


def _latest_source_quotes(
    market_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for event in _source_market_events(market_events):
        if event["action"] != "QUOTE":
            continue
        quote_key = event.get("instrument_id") or event["symbol"]
        latest[(quote_key, event["session_date"])] = event
    return list(latest.values())


def _factor_for_quote(
    event: dict[str, Any],
    facts: list[SplitFact],
) -> Decimal:
    factor = Decimal("1")
    session = event["session_date"]
    for fact in facts:
        if (
            _quote_matches_fact(event, fact)
            and session < fact.effective_session
        ):
            factor *= fact.ratio
    return factor


def _candidate_id(
    source: dict[str, Any],
    factor: Decimal,
) -> str:
    identity = "|".join(
        (
            source["event_id"],
            source["session_date"],
            format(factor, "f"),
        )
    )
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return f"{MARKET_EVENT_PREFIX}{suffix}"


def _corrected_quote(
    source: dict[str, Any],
    factor: Decimal,
    *,
    created_at: str,
) -> dict[str, Any]:
    corrected = {
        "event_id": _candidate_id(source, factor),
        "portfolio": "market",
        "occurred_at": source["occurred_at"],
        "created_at": created_at,
        "source": "manual-quote",
        "action": "QUOTE",
        "symbol": source["symbol"],
        "close": format(
            price(
                _decimal(source["close"], field="close") * factor,
                field="close",
            ),
            "f",
        ),
        "session_date": source["session_date"],
    }
    if source.get("instrument_id") is not None:
        corrected["instrument_id"] = source["instrument_id"]
    validate_event(corrected)
    return normalize_event(corrected)


def _candidate_events(
    market_events: list[dict[str, Any]],
    facts: list[SplitFact],
    *,
    created_at: str,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for source in _latest_source_quotes(market_events):
        factor = _factor_for_quote(source, facts)
        if factor != 1:
            candidates.append(
                _corrected_quote(
                    source,
                    factor,
                    created_at=created_at,
                )
            )
    return sorted(
        candidates,
        key=lambda event: (
            event["session_date"],
            event.get("instrument_id") or event["symbol"],
            event["event_id"],
        ),
    )


def _without_sequence(event: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(event)
    value.pop("ledger_seq", None)
    return normalize_event(value)


def _simulate(
    existing: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> tuple[int, int]:
    existing_by_id = {event["event_id"]: event for event in existing}
    if len(existing_by_id) != len(existing):
        raise SplitQuoteMigrationError("market ledger has duplicate event_id")
    pending = 0
    duplicates = 0
    for candidate in candidates:
        stored = existing_by_id.get(candidate["event_id"])
        if stored is None:
            pending += 1
            continue
        if _without_sequence(stored) != normalize_event(candidate):
            raise ConflictError(
                "event_id already exists with different payload: "
                f"{candidate['event_id']}"
            )
        duplicates += 1
    return pending, duplicates


def inspect_migration(root: Path, plan_payload: dict[str, Any]) -> MigrationPlan:
    store = LedgerStore(root)
    live_events = store.read("live", repair_tail=False)
    market_events = store.read("market", repair_tail=False)
    if not live_events:
        raise SplitQuoteMigrationError("Live ledger is not initialized")
    if not market_events:
        raise SplitQuoteMigrationError("market ledger is empty")
    if _source_live_digest(live_events) != plan_payload["live_event_digest"]:
        raise SplitQuoteMigrationError("Live ledger does not match approved plan")
    if _source_market_digest(market_events) != plan_payload["market_event_digest"]:
        raise SplitQuoteMigrationError("market ledger does not match approved plan")

    facts = _split_facts(plan_payload)
    candidates = _candidate_events(
        market_events,
        facts,
        created_at=plan_payload["created_at"],
    )
    pending, duplicates = _simulate(market_events, candidates)
    return MigrationPlan(
        candidates=candidates,
        pending=pending,
        duplicates=duplicates,
        corrected_sessions=len(candidates),
        split_facts=len(facts),
        already_corrected=bool(candidates) and pending == 0,
    )


def execute(
    *,
    root: Path,
    plan_path: Path,
    apply: bool,
) -> dict[str, Any]:
    plan_payload = _load_plan(plan_path)
    plan = inspect_migration(root, plan_payload)
    response: dict[str, Any] = {
        "status": "valid" if not apply else "ready",
        "migration": "split-adjusted-quote-basis",
        "split_facts": plan.split_facts,
        "corrected_sessions": plan.corrected_sessions,
        "pending": plan.pending,
        "duplicates": plan.duplicates,
        "already_corrected": plan.already_corrected,
    }
    if not apply:
        return response
    if plan.pending == 0:
        snapshot, rebuilt = build_snapshot_if_needed(root)
        response.update(
            {
                "status": "current",
                "snapshot_revision": snapshot["revision"],
                "snapshot_status": "rebuilt" if rebuilt else "current",
            }
        )
        return response

    backup = backup_ledgers(root)
    plan = inspect_migration(root, plan_payload)
    if plan.pending == 0:
        response.update({"status": "current", "backup_id": backup["backup_id"]})
        return response

    batch = LedgerStore(root).append_many(plan.candidates)
    snapshot, rebuilt = build_snapshot_if_needed(root)
    atomic_write_json(
        root / "state" / "publish.pending",
        {
            "revision": snapshot["revision"],
            "requested_by": "split-adjusted-quote-migration",
        },
    )
    response.update(
        {
            "status": "corrected",
            "backup_id": backup["backup_id"],
            "appended": batch["appended"],
            "duplicates": batch["duplicates"],
            "snapshot_revision": snapshot["revision"],
            "snapshot_status": "rebuilt" if rebuilt else "current",
        }
    )
    return response


def _listed_quote_symbols(live_events: list[dict[str, Any]]) -> set[str]:
    symbols: set[str] = set()
    for event in resolve_effective_events(live_events):
        if event["action"] not in {"BUY", "SELL", "SPLIT"}:
            continue
        if event.get("instrument_type") in {"OPTION", "PRIVATE"}:
            continue
        symbols.add(event.get("quote_symbol") or event["symbol"])
    return symbols


def _ratio_parts(value: Any) -> tuple[str, str]:
    ratio = _decimal(value, field="split ratio")
    fraction = Fraction(ratio).limit_denominator(10_000)
    normalized = Decimal(fraction.numerator) / Decimal(fraction.denominator)
    if abs(normalized - ratio) > Decimal("1e-12"):
        raise SplitQuoteMigrationError(
            f"split ratio cannot be normalized safely: {value}"
        )
    return str(fraction.numerator), str(fraction.denominator)


def generate_plan(
    *,
    root: Path,
    fetch_splits: Callable[[str], Iterable[tuple[Any, Any]]],
    created_at: str,
) -> dict[str, Any]:
    parse_timestamp(created_at, field="created_at")
    store = LedgerStore(root)
    live_events = store.read("live", repair_tail=False)
    market_events = store.read("market", repair_tail=False)
    source_market = _source_market_events(market_events)
    quote_sessions: dict[str, list[str]] = {}
    for event in source_market:
        if event["action"] != "QUOTE":
            continue
        quote_sessions.setdefault(event["symbol"], []).append(
            event["session_date"]
        )

    splits: list[dict[str, str]] = []
    for symbol in sorted(_listed_quote_symbols(live_events)):
        sessions = quote_sessions.get(symbol)
        if not sessions:
            continue
        first_session = min(sessions)
        last_session = max(sessions)
        for raw_date, raw_ratio in fetch_splits(symbol):
            session = (
                raw_date.date().isoformat()
                if hasattr(raw_date, "date")
                else date.fromisoformat(str(raw_date)[:10]).isoformat()
            )
            if session <= first_session or session > last_session:
                continue
            numerator, denominator = _ratio_parts(raw_ratio)
            instrument_type = (
                "ETF" if symbol in KNOWN_LIVE_ETF_SYMBOLS else "EQUITY"
            )
            splits.append(
                {
                    "symbol": symbol,
                    "instrument_id": f"{instrument_type}:{symbol}",
                    "effective_session": session,
                    "numerator": numerator,
                    "denominator": denominator,
                }
            )
    payload = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "created_at": created_at,
        "quote_basis": QUOTE_BASIS,
        "live_event_digest": _source_live_digest(live_events),
        "market_event_digest": _source_market_digest(market_events),
        "splits": sorted(
            splits,
            key=lambda item: (
                item["instrument_id"],
                item["effective_session"],
            ),
        ),
    }
    _split_facts(payload)
    return payload


def _yfinance_symbol(symbol: str) -> str:
    # Yahoo uses a dash for listed share classes such as BRK.B.
    return symbol.replace(".", "-")


def _yfinance_fetcher() -> Callable[[str], Iterable[tuple[Any, Any]]]:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise SplitQuoteMigrationError(
            "yfinance is required only for --generate-plan"
        ) from exc

    def fetch(symbol: str) -> Iterable[tuple[Any, Any]]:
        provider_symbol = _yfinance_symbol(symbol)
        try:
            splits = yf.Ticker(provider_symbol).splits
        except Exception as exc:
            raise SplitQuoteMigrationError(
                f"yfinance split lookup failed for {symbol}"
            ) from exc
        if splits is None or not hasattr(splits, "items"):
            raise SplitQuoteMigrationError(
                f"yfinance returned no split series for {symbol}"
            )
        return list(splits.items())

    return fetch


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="repair-split-adjusted-quotes")
    command.add_argument("--root", required=True)
    command.add_argument("--plan")
    command.add_argument("--generate-plan")
    mode = command.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check-only", action="store_true")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        root = Path(args.root).resolve()
        if args.generate_plan:
            if args.plan or args.apply:
                raise SplitQuoteMigrationError(
                    "--generate-plan cannot be combined with --plan or --apply"
                )
            created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            payload = generate_plan(
                root=root,
                fetch_splits=_yfinance_fetcher(),
                created_at=created_at,
            )
            target = Path(args.generate_plan).resolve()
            if target.exists():
                raise SplitQuoteMigrationError(
                    "refusing to overwrite an existing split quote plan"
                )
            atomic_write_json(target, payload, mode=0o600)
            result = {
                "status": "generated",
                "plan": str(target),
                "split_facts": len(payload["splits"]),
            }
        else:
            if not args.plan:
                raise SplitQuoteMigrationError("--plan is required")
            result = execute(
                root=root,
                plan_path=Path(args.plan).resolve(),
                apply=args.apply,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (
        ConflictError,
        PortfolioError,
        SplitQuoteMigrationError,
        OSError,
        ValueError,
    ) as exc:
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
