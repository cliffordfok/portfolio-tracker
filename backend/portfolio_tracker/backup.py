"""Consistent, append-only ledger backup snapshots."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .ledger import (
    FileLock,
    LedgerStore,
    atomic_write_json,
    durable_write_bytes,
)


def backup_ledgers(
    root: str | Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    root_path = Path(root)
    store = LedgerStore(root_path)
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    backup_id = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
    destination = root_path / "backups" / backup_id
    manifest: dict[str, Any] = {
        "backup_id": backup_id,
        "created_at": timestamp.isoformat().replace("+00:00", "Z"),
        "ledgers": {},
    }

    with FileLock(store.lock_path):
        for portfolio in ("paper", "live", "market"):
            source = store.path_for(portfolio)
            content = source.read_bytes() if source.exists() else b""
            target = destination / source.name
            durable_write_bytes(target, content, mode=0o600)
            manifest["ledgers"][portfolio] = {
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        atomic_write_json(destination / "manifest.json", manifest, mode=0o600)
    return manifest
