"""Durable JSONL ledger storage with process locking and idempotency."""

from __future__ import annotations

import json
import os
import time
from contextlib import AbstractContextManager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import ConflictError, LedgerCorruptionError
from .replay import replay_portfolio
from .resolver import resolve_effective_events
from .schemas import normalize_event, validate_event


_BINARY = getattr(os, "O_BINARY", 0)


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path, 0o700)


class FileLock(AbstractContextManager["FileLock"]):
    """Small cross-platform exclusive advisory lock."""

    def __init__(self, path: Path, timeout: float = 10.0) -> None:
        self.path = path
        self.timeout = timeout
        self._file: Any = None

    def __enter__(self) -> "FileLock":
        _ensure_private_dir(self.path.parent)
        self._file = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self._file.write(b"0")
            self._file.flush()
            os.fsync(self._file.fileno())
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    self._file.seek(0)
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self._file.close()
                    self._file = None
                    raise TimeoutError(f"lock timeout: {self.path}")
                time.sleep(0.25)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    _ensure_private_dir(path.parent)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    try:
        descriptor = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY,
            mode,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            os.chmod(path, mode)
        _fsync_directory(path.parent)
    finally:
        if temp.exists():
            temp.unlink()


def durable_write_bytes(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    _ensure_private_dir(path.parent)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY,
        mode,
    )
    try:
        written = 0
        while written < len(content):
            count = os.write(descriptor, content[written:])
            if count <= 0:
                raise OSError("short durable write")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if os.name != "nt":
        os.chmod(path, mode)
    _fsync_directory(path.parent)


def durable_unlink(path: Path) -> None:
    if not path.exists():
        return
    path.unlink()
    _fsync_directory(path.parent)


def _repair_incomplete_tail(
    path: Path,
    raw: bytes,
    offset: int,
    *,
    backup_dir: Path,
    quarantine_dir: Path,
    repair_records: list[dict[str, Any]] | None,
) -> None:
    """Durably preserve an incomplete JSONL tail before truncating it."""

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_dir / f"{path.name}.pre-repair-{timestamp}.bak"
    quarantine = quarantine_dir / f"{path.name}.tail-{timestamp}.quarantine"
    tail = raw[offset:]

    # The backup and quarantined bytes, plus both parent directory entries,
    # are fsynced by durable_write_bytes before the master ledger is touched.
    durable_write_bytes(backup, raw)
    durable_write_bytes(quarantine, tail)
    with path.open("r+b") as handle:
        handle.truncate(offset)
        handle.flush()
        os.fsync(handle.fileno())
    if os.name != "nt":
        os.chmod(path, 0o600)
    _fsync_directory(path.parent)

    if repair_records is not None:
        repair_records.append(
            {
                "ledger": path.name,
                "bytes_quarantined": len(tail),
                "backup": backup.name,
                "quarantine": quarantine.name,
                "repaired_at": datetime.now(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )


def read_jsonl(
    path: Path,
    *,
    repair_tail: bool = False,
    backup_dir: Path | None = None,
    quarantine_dir: Path | None = None,
    repair_records: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = path.read_bytes()
    if not raw:
        return []

    lines = raw.splitlines(keepends=True)
    parsed: list[dict[str, Any]] = []
    offset = 0
    for index, line in enumerate(lines):
        is_last = index == len(lines) - 1
        content = line.rstrip(b"\r\n")
        if not content:
            offset += len(line)
            continue
        incomplete_tail = is_last and not raw.endswith((b"\n", b"\r"))
        if incomplete_tail:
            if repair_tail:
                _repair_incomplete_tail(
                    path,
                    raw,
                    offset,
                    backup_dir=backup_dir or path.parent,
                    quarantine_dir=quarantine_dir or path.parent,
                    repair_records=repair_records,
                )
                return parsed
            raise LedgerCorruptionError(
                f"{path.name}: incomplete JSONL record at tail"
            )
        try:
            item = json.loads(content.decode("utf-8"))
            if not isinstance(item, dict):
                raise ValueError("JSONL records must be objects")
            parsed.append(item)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            location = "tail" if is_last else f"line {index + 1}"
            raise LedgerCorruptionError(f"{path.name}: invalid JSONL at {location}") from exc
        offset += len(line)
    return parsed


def _append_json_line(path: Path, event: dict[str, Any]) -> None:
    _ensure_private_dir(path.parent)
    payload = (
        json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | _BINARY,
        0o600,
    )
    try:
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("short JSONL append")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if os.name != "nt":
        os.chmod(path, 0o600)
    _fsync_directory(path.parent)


def _same_retry_payload(candidate: dict[str, Any], stored: dict[str, Any]) -> bool:
    candidate_copy = deepcopy(candidate)
    stored_copy = deepcopy(stored)
    candidate_copy.pop("ledger_seq", None)
    stored_copy.pop("ledger_seq", None)
    return (
        normalize_event(candidate_copy)
        == normalize_event(stored_copy)
    )


class LedgerStore:
    """The single append path used by cron jobs and interactive trade entry."""

    def __init__(self, root: str | Path, *, lock_timeout: float = 10.0) -> None:
        self.root = Path(root)
        self.ledger_dir = self.root / "ledger"
        self.lock_path = self.root / "locks" / "ledger-global.lock"
        self.lock_timeout = lock_timeout

    def path_for(self, portfolio: str) -> Path:
        return self.ledger_dir / f"{portfolio}.jsonl"

    def read(
        self,
        portfolio: str,
        *,
        repair_tail: bool = False,
        repair_records: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        return read_jsonl(
            self.path_for(portfolio),
            repair_tail=repair_tail,
            backup_dir=self.root / "backups",
            quarantine_dir=self.root / "quarantine",
            repair_records=repair_records,
        )

    def all_events(
        self,
        *,
        repair_tail: bool = False,
        repair_records: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        combined: list[dict[str, Any]] = []
        for portfolio in ("paper", "live", "market"):
            combined.extend(
                self.read(
                    portfolio,
                    repair_tail=repair_tail,
                    repair_records=repair_records,
                )
            )
        return combined

    def append(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Validate, replay, and durably append exactly one event."""

        validate_event(candidate)
        normalized_candidate = normalize_event(candidate)
        portfolio = normalized_candidate["portfolio"]
        with FileLock(self.lock_path, timeout=self.lock_timeout):
            repairs: list[dict[str, Any]] = []
            all_events = self.all_events(
                repair_tail=True,
                repair_records=repairs,
            )
            for existing in all_events:
                if existing["event_id"] != normalized_candidate["event_id"]:
                    continue
                if _same_retry_payload(normalized_candidate, existing):
                    result = {"status": "duplicate", "event": existing}
                    if repairs:
                        result["ledger_repairs"] = repairs
                    return result
                raise ConflictError(
                    f"event_id already exists with different payload: "
                    f"{normalized_candidate['event_id']}"
                )

            ledger_events = [
                event for event in all_events if event["portfolio"] == portfolio
            ]
            stored = deepcopy(normalized_candidate)
            stored["ledger_seq"] = (
                max((int(item["ledger_seq"]) for item in ledger_events), default=0) + 1
            )

            proposed = ledger_events + [stored]
            if portfolio in {"paper", "live"}:
                replay_portfolio(proposed, portfolio=portfolio)
            else:
                resolve_effective_events(proposed)

            _append_json_line(self.path_for(portfolio), stored)
            atomic_write_json(
                self.root / "state" / "rebuild.pending",
                {
                    "event_id": stored["event_id"],
                    "portfolio": portfolio,
                    "ledger_seq": stored["ledger_seq"],
                    "requested_at": datetime.now(UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            )
            result = {"status": "appended", "event": stored}
            if repairs:
                result["ledger_repairs"] = repairs
            return result

    def repair_tail(self, portfolio: str) -> list[dict[str, Any]]:
        with FileLock(self.lock_path, timeout=self.lock_timeout):
            return read_jsonl(
                self.path_for(portfolio),
                repair_tail=True,
                backup_dir=self.root / "backups",
                quarantine_dir=self.root / "quarantine",
            )
