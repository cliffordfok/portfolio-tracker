"""Read-only production acceptance checks for a Portfolio Tracker runtime."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import nullcontext
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .errors import PortfolioError
from .ledger import FileLock, LedgerStore
from .replay import replay_portfolio
from .schemas import parse_timestamp, validate_event
from .snapshot import _source_head


PORTFOLIOS = ("paper", "live", "market")
SYMBOL_PATTERN = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z])?$")


def _decimal_or_none(value: Any, *, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value:
        raise PortfolioError(f"{label} must be a Decimal string or null")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise PortfolioError(f"{label} must be a Decimal string or null") from exc
    if not parsed.is_finite():
        raise PortfolioError(f"{label} must be finite")


def _snapshot_date(value: Any, *, label: str) -> None:
    if not isinstance(value, str):
        raise PortfolioError(f"{label} must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise PortfolioError(f"{label} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise PortfolioError(f"{label} must use YYYY-MM-DD")


def _snapshot_timestamp(value: Any, *, label: str, nullable: bool = True) -> None:
    if value is None and nullable:
        return
    try:
        parse_timestamp(value, field=label)
    except (PortfolioError, ValueError) as exc:
        raise PortfolioError(f"{label} must be a UTC timestamp or null") from exc


def _validate_snapshot_shape(snapshot: dict[str, Any]) -> None:
    if snapshot.get("currency") != "USD":
        raise PortfolioError("portfolio snapshot currency must be USD")
    _snapshot_timestamp(
        snapshot.get("generated_at"),
        label="snapshot.generated_at",
        nullable=False,
    )
    for field in ("data_as_of", "prices_as_of"):
        if field not in snapshot:
            raise PortfolioError(f"portfolio snapshot is missing {field}")
        _snapshot_timestamp(snapshot[field], label=f"snapshot.{field}")
    warnings = snapshot.get("warnings")
    if not isinstance(warnings, list) or not all(
        isinstance(warning, str) for warning in warnings
    ):
        raise PortfolioError("portfolio snapshot warnings must be strings")

    portfolios = snapshot.get("portfolios")
    if not isinstance(portfolios, dict):
        raise PortfolioError("portfolio snapshot has no portfolios object")
    for name in ("paper", "live"):
        portfolio = portfolios.get(name)
        if not isinstance(portfolio, dict):
            raise PortfolioError(f"portfolio snapshot has no {name} object")
        if portfolio.get("data_status") not in {
            "OK",
            "INSUFFICIENT_DATA",
            "NO_DATA",
        }:
            raise PortfolioError(f"{name} data_status is invalid")
        for field in ("holdings", "recent_trades", "daily"):
            if not isinstance(portfolio.get(field), list):
                raise PortfolioError(f"{name}.{field} must be an array")
        for field in ("cash", "initial_cash"):
            if field in portfolio:
                _decimal_or_none(portfolio[field], label=f"{name}.{field}")

        for holding in portfolio["holdings"]:
            if not isinstance(holding, dict):
                raise PortfolioError(f"{name}.holdings entries must be objects")
            if not isinstance(holding.get("symbol"), str) or not SYMBOL_PATTERN.fullmatch(
                holding["symbol"]
            ):
                raise PortfolioError(f"{name}.holdings symbol is invalid")
            for field in (
                "shares",
                "avg_cost",
                "cost_basis",
                "current_price",
                "market_value",
                "unrealized_pnl",
                "unrealized_pnl_pct",
            ):
                if field not in holding:
                    raise PortfolioError(f"{name}.holdings is missing {field}")
                _decimal_or_none(
                    holding[field],
                    label=f"{name}.holdings.{field}",
                )
            if "market_price_as_of" not in holding:
                raise PortfolioError(
                    f"{name}.holdings is missing market_price_as_of"
                )
            _snapshot_timestamp(
                holding["market_price_as_of"],
                label=f"{name}.holdings.market_price_as_of",
            )

        for trade in portfolio["recent_trades"]:
            if not isinstance(trade, dict) or trade.get("action") not in {
                "BUY",
                "SELL",
                "CASH_FLOW",
            }:
                raise PortfolioError(f"{name}.recent_trades entry is invalid")
            if not isinstance(trade.get("event_id"), str) or not trade["event_id"]:
                raise PortfolioError(f"{name}.recent_trades event_id is invalid")
            if trade.get("portfolio") != name:
                raise PortfolioError(
                    f"{name}.recent_trades contains a cross-portfolio event"
                )
            if not isinstance(trade.get("source"), str) or not trade["source"]:
                raise PortfolioError(f"{name}.recent_trades source is invalid")
            ledger_seq = trade.get("ledger_seq")
            if (
                isinstance(ledger_seq, bool)
                or not isinstance(ledger_seq, int)
                or ledger_seq < 1
            ):
                raise PortfolioError(
                    f"{name}.recent_trades ledger_seq is invalid"
                )
            _snapshot_timestamp(
                trade.get("occurred_at"),
                label=f"{name}.recent_trades.occurred_at",
                nullable=False,
            )
            _snapshot_timestamp(
                trade.get("created_at"),
                label=f"{name}.recent_trades.created_at",
                nullable=False,
            )
            if trade["action"] in {"BUY", "SELL"}:
                if (
                    not isinstance(trade.get("symbol"), str)
                    or not SYMBOL_PATTERN.fullmatch(trade["symbol"])
                ):
                    raise PortfolioError(
                        f"{name}.recent_trades symbol is invalid"
                    )
                for field in ("shares", "price", "fee", "pnl", "pnl_pct"):
                    if field not in trade:
                        raise PortfolioError(
                            f"{name}.recent_trades is missing {field}"
                        )
                    _decimal_or_none(
                        trade[field],
                        label=f"{name}.recent_trades.{field}",
                    )
            elif trade.get("symbol") != "USD":
                raise PortfolioError("CASH_FLOW snapshot symbol must be USD")
            else:
                if "amount" not in trade:
                    raise PortfolioError(
                        f"{name}.recent_trades is missing amount"
                    )
                _decimal_or_none(
                    trade["amount"],
                    label=f"{name}.recent_trades.amount",
                )

        for point in portfolio["daily"]:
            if not isinstance(point, dict):
                raise PortfolioError(f"{name}.daily entries must be objects")
            _snapshot_date(point.get("date"), label=f"{name}.daily.date")
            if point.get("data_status") not in {
                "OK",
                "INSUFFICIENT_DATA",
                "INSUFFICIENT_MARKET_DATA",
            }:
                raise PortfolioError(f"{name}.daily data_status is invalid")
            for field in (
                "nav",
                "cash",
                "external_flow",
                "daily_return",
                "cumulative_return",
                "segment_return",
                "pnl",
            ):
                if field not in point:
                    raise PortfolioError(f"{name}.daily is missing {field}")
                _decimal_or_none(point[field], label=f"{name}.daily.{field}")
            missing_symbols = point.get("missing_symbols")
            if not isinstance(missing_symbols, list) or not all(
                isinstance(symbol, str) and SYMBOL_PATTERN.fullmatch(symbol)
                for symbol in missing_symbols
            ):
                raise PortfolioError(
                    f"{name}.daily missing_symbols is invalid"
                )
            segment_id = point.get("segment_id")
            if not (
                segment_id is None
                or (
                    not isinstance(segment_id, bool)
                    and isinstance(segment_id, int)
                    and segment_id > 0
                )
            ):
                raise PortfolioError(f"{name}.daily segment_id is invalid")

        metrics = portfolio.get("metrics")
        if not isinstance(metrics, dict):
            raise PortfolioError(f"{name}.metrics must be an object")
        if metrics.get("data_status") not in {
            "OK",
            "INSUFFICIENT_DATA",
            "NO_DATA",
        }:
            raise PortfolioError(f"{name}.metrics data_status is invalid")
        for field in (
            "total_return",
            "realized_pnl",
            "win_rate",
            "max_drawdown",
            "sharpe_ratio",
        ):
            if field not in metrics:
                raise PortfolioError(f"{name}.metrics is missing {field}")
            _decimal_or_none(metrics[field], label=f"{name}.metrics.{field}")
        closed_episodes = metrics.get("closed_episodes")
        if (
            isinstance(closed_episodes, bool)
            or not isinstance(closed_episodes, int)
            or closed_episodes < 0
        ):
            raise PortfolioError(f"{name}.metrics.closed_episodes is invalid")

    benchmark = snapshot.get("benchmark")
    if not isinstance(benchmark, dict) or benchmark.get("symbol") != "SPY":
        raise PortfolioError("portfolio snapshot benchmark must be SPY")
    daily = benchmark.get("daily")
    if not isinstance(daily, list):
        raise PortfolioError("portfolio snapshot benchmark.daily must be an array")
    for point in daily:
        if not isinstance(point, dict):
            raise PortfolioError("benchmark.daily entries must be objects")
        _snapshot_date(point.get("date"), label="benchmark.daily.date")
        if point.get("data_status") not in {
            "OK",
            "INSUFFICIENT_MARKET_DATA",
        }:
            raise PortfolioError("benchmark.daily data_status is invalid")
        for field in (
            "close",
            "daily_return",
            "cumulative_return",
            "segment_return",
        ):
            if field not in point:
                raise PortfolioError(f"benchmark.daily is missing {field}")
            _decimal_or_none(point[field], label=f"benchmark.daily.{field}")
        segment_id = point.get("segment_id")
        if not (
            segment_id is None
            or (
                not isinstance(segment_id, bool)
                and isinstance(segment_id, int)
                and segment_id > 0
            )
        ):
            raise PortfolioError("benchmark.daily segment_id is invalid")


def _json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PortfolioError(f"{label} is missing or invalid") from exc
    if not isinstance(value, dict):
        raise PortfolioError(f"{label} must be a JSON object")
    return value


def _audit_ledgers(
    store: LedgerStore,
    *,
    required_initialized: set[str],
    required_uninitialized: set[str],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    events_by_portfolio: dict[str, list[dict[str, Any]]] = {}
    report: dict[str, Any] = {}
    global_ids: set[str] = set()

    for portfolio in PORTFOLIOS:
        events = store.read(portfolio)
        events_by_portfolio[portfolio] = events
        expected_sequences = list(range(1, len(events) + 1))
        actual_sequences = [event.get("ledger_seq") for event in events]
        if actual_sequences != expected_sequences:
            raise PortfolioError(
                f"{portfolio} ledger_seq must be contiguous from 1"
            )
        for event in events:
            validate_event(event)
            if event["portfolio"] != portfolio:
                raise PortfolioError(
                    f"{portfolio} ledger contains a cross-portfolio event"
                )
            event_id = event["event_id"]
            if event_id in global_ids:
                raise PortfolioError(f"duplicate global event_id: {event_id}")
            global_ids.add(event_id)

        entry: dict[str, Any] = {"events": len(events)}
        if portfolio in {"paper", "live"}:
            if not events:
                if portfolio in required_initialized:
                    raise PortfolioError(
                        f"{portfolio} portfolio has no PORTFOLIO_OPEN"
                    )
                entry["initialized"] = False
            else:
                if portfolio in required_uninitialized:
                    raise PortfolioError(
                        f"{portfolio} portfolio must remain uninitialized"
                    )
                replay_portfolio(events)
                entry["initialized"] = True
        report[portfolio] = entry
    return events_by_portfolio, report


def _audit_stage_snapshot(
    snapshot: dict[str, Any] | None,
    *,
    require_paper_initialized: bool,
    require_live_uninitialized: bool,
) -> None:
    if snapshot is None:
        return
    portfolios = snapshot["portfolios"]
    if require_paper_initialized:
        paper = portfolios["paper"]
        if (
            paper.get("data_status") == "NO_DATA"
            or "initial_cash" not in paper
            or "cash" not in paper
        ):
            raise PortfolioError(
                "paper snapshot does not represent an initialized portfolio"
            )
    if require_live_uninitialized:
        live = portfolios["live"]
        metrics = live.get("metrics", {})
        if (
            live.get("data_status") != "NO_DATA"
            or live.get("holdings") != []
            or live.get("recent_trades") != []
            or live.get("daily") != []
            or "initial_cash" in live
            or "cash" in live
            or metrics.get("data_status") != "NO_DATA"
            or metrics.get("realized_pnl") != "0"
        ):
            raise PortfolioError(
                "live snapshot must remain in the canonical NO_DATA state"
            )


def _audit_snapshot(
    root: Path,
    store: LedgerStore,
    events_by_portfolio: dict[str, list[dict[str, Any]]],
    *,
    require_current: bool,
    warnings: list[str],
) -> tuple[dict[str, Any] | None, bytes | None, dict[str, Any]]:
    path = root / "snapshots" / "portfolio-snapshot.json"
    if not path.exists():
        if require_current:
            raise PortfolioError("current snapshot is required")
        warnings.append("snapshot has not been built")
        return None, None, {"exists": False, "current": False}

    snapshot_bytes = path.read_bytes()
    snapshot = _json_object(path, label="portfolio snapshot")
    if snapshot.get("schema_version") != 3:
        raise PortfolioError("portfolio snapshot schema_version must be 3")
    if isinstance(snapshot.get("revision"), bool) or not isinstance(
        snapshot.get("revision"), int
    ):
        raise PortfolioError("portfolio snapshot revision must be an integer")
    _validate_snapshot_shape(snapshot)

    expected_heads = {
        portfolio: _source_head(
            store.path_for(portfolio),
            events_by_portfolio[portfolio],
        )
        for portfolio in PORTFOLIOS
    }
    current = snapshot.get("source_head") == expected_heads
    expected_revision = sum(head["count"] for head in expected_heads.values())
    if snapshot["revision"] != expected_revision:
        raise PortfolioError(
            "portfolio snapshot revision does not match ledger event count"
        )
    if require_current and not current:
        raise PortfolioError("portfolio snapshot source_head is stale")
    if not current:
        warnings.append("snapshot source_head is stale")

    return (
        snapshot,
        snapshot_bytes,
        {
            "exists": True,
            "current": current,
            "revision": snapshot["revision"],
            "schema_version": snapshot["schema_version"],
        },
    )


def _audit_publication(
    root: Path,
    snapshot: dict[str, Any] | None,
    snapshot_bytes: bytes | None,
    *,
    require_published: bool,
    warnings: list[str],
) -> dict[str, Any]:
    state_path = root / "state" / "published-state.json"
    attempt_path = root / "state" / "publication-attempt.json"
    pending_path = root / "state" / "publish.pending"

    if attempt_path.exists():
        if require_published:
            raise PortfolioError("publication attempt is unresolved")
        warnings.append("publication attempt is unresolved")
    if pending_path.exists():
        if require_published:
            raise PortfolioError("snapshot publication is pending")
        warnings.append("snapshot publication is pending")

    if not state_path.exists():
        if require_published:
            raise PortfolioError("published-state.json is required")
        warnings.append("snapshot has not been published")
        return {"published": False}
    if snapshot is None or snapshot_bytes is None:
        raise PortfolioError("published state exists without a local snapshot")

    state = _json_object(state_path, label="published state")
    local_hash = hashlib.sha256(snapshot_bytes).hexdigest()
    if state.get("local_snapshot_hash") != local_hash:
        raise PortfolioError("published state hash does not match local snapshot")
    if state.get("published_revision") != snapshot["revision"]:
        raise PortfolioError(
            "published state revision does not match local snapshot"
        )
    if (
        not isinstance(state.get("remote_blob_sha"), str)
        or not state["remote_blob_sha"]
    ):
        raise PortfolioError("published state has no remote blob SHA")

    return {
        "published": True,
        "revision": state["published_revision"],
        "remote_blob_sha": state["remote_blob_sha"],
    }


def _audit_backup(
    root: Path,
    *,
    require_backup: bool,
    warnings: list[str],
) -> dict[str, Any]:
    backup_root = root / "backups"
    candidates = sorted(
        path
        for path in backup_root.iterdir()
        if path.is_dir() and (path / "manifest.json").is_file()
    ) if backup_root.exists() else []
    if not candidates:
        if require_backup:
            raise PortfolioError("at least one verified ledger backup is required")
        warnings.append("no verified ledger backup exists")
        return {"verified": False}

    latest = candidates[-1]
    manifest = _json_object(latest / "manifest.json", label="backup manifest")
    ledgers = manifest.get("ledgers")
    if not isinstance(ledgers, dict):
        raise PortfolioError("backup manifest has no ledger records")
    for portfolio in PORTFOLIOS:
        record = ledgers.get(portfolio)
        path = latest / f"{portfolio}.jsonl"
        if not isinstance(record, dict) or not path.is_file():
            raise PortfolioError(f"backup is missing {portfolio} ledger")
        content = path.read_bytes()
        if record.get("bytes") != len(content):
            raise PortfolioError(f"{portfolio} backup byte count does not match")
        if record.get("sha256") != hashlib.sha256(content).hexdigest():
            raise PortfolioError(f"{portfolio} backup hash does not match")

    return {
        "verified": True,
        "backup_id": manifest.get("backup_id", latest.name),
    }


def audit_runtime(
    root: str | Path,
    *,
    require_initialized: bool = False,
    require_paper_initialized: bool = False,
    require_live_uninitialized: bool = False,
    require_current: bool = False,
    require_published: bool = False,
    require_backup: bool = False,
) -> dict[str, Any]:
    """Verify ledger, snapshot, publication, and backup invariants without writes."""

    root_path = Path(root)
    if require_initialized and require_live_uninitialized:
        raise PortfolioError(
            "live portfolio cannot be required initialized and uninitialized"
        )
    required_initialized = (
        {"paper", "live"} if require_initialized else set()
    )
    if require_paper_initialized:
        required_initialized.add("paper")
    required_uninitialized = (
        {"live"} if require_live_uninitialized else set()
    )
    store = LedgerStore(root_path)
    warnings: list[str] = []
    lock = (
        FileLock(store.lock_path)
        if store.lock_path.exists()
        else nullcontext()
    )
    with lock:
        events_by_portfolio, ledgers = _audit_ledgers(
            store,
            required_initialized=required_initialized,
            required_uninitialized=required_uninitialized,
        )
        snapshot, snapshot_bytes, snapshot_report = _audit_snapshot(
            root_path,
            store,
            events_by_portfolio,
            require_current=require_current,
            warnings=warnings,
        )
        _audit_stage_snapshot(
            snapshot,
            require_paper_initialized=require_paper_initialized,
            require_live_uninitialized=require_live_uninitialized,
        )

    publication = _audit_publication(
        root_path,
        snapshot,
        snapshot_bytes,
        require_published=require_published,
        warnings=warnings,
    )
    backup = _audit_backup(
        root_path,
        require_backup=require_backup,
        warnings=warnings,
    )
    return {
        "status": "healthy",
        "ledgers": ledgers,
        "snapshot": snapshot_report,
        "publication": publication,
        "backup": backup,
        "warnings": warnings,
    }
