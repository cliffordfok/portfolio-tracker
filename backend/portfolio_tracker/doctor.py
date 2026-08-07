"""Read-only production acceptance checks for a Portfolio Tracker runtime."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from .errors import PortfolioError
from .ledger import FileLock, LedgerStore
from .replay import replay_portfolio
from .schemas import validate_event
from .snapshot import _source_head, validate_snapshot


PORTFOLIOS = ("paper", "live", "market")


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
    validate_snapshot(snapshot)

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
    current_heads: dict[str, dict[str, Any]],
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
    outdated_ledgers: list[str] = []
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
        if record.get("sha256") != current_heads[portfolio]["hash"]:
            outdated_ledgers.append(portfolio)

    if outdated_ledgers:
        names = ", ".join(outdated_ledgers)
        message = f"latest verified backup is outdated for ledgers: {names}"
        if require_backup:
            raise PortfolioError(message)
        warnings.append(message)

    return {
        "verified": True,
        "current": not outdated_ledgers,
        "backup_id": manifest.get("backup_id", latest.name),
        "outdated_ledgers": outdated_ledgers,
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
        current_heads = {
            portfolio: _source_head(
                store.path_for(portfolio),
                events_by_portfolio[portfolio],
            )
            for portfolio in PORTFOLIOS
        }

    publication = _audit_publication(
        root_path,
        snapshot,
        snapshot_bytes,
        require_published=require_published,
        warnings=warnings,
    )
    backup = _audit_backup(
        root_path,
        current_heads=current_heads,
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
