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
from .schemas import validate_event


class FileLock(AbstractContextManager["FileLock"]):
    """Small cross-platform exclusive advisory lock."""

    def __init__(self, path: Path, timeout: float = 10.0) -> None:
        self.path = path
        self.timeout = timeout
        self._file: Any = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
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
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
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


def read_jsonl(path: Path, *, repair_tail: bool = False) -> list[dict[str, Any]]:
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
        try:
            item = json.loads(content.decode("utf-8"))
            if not isinstance(item, dict):
                raise ValueError("JSONL records must be objects")
            parsed.append(item)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            incomplete_tail = is_last and not raw.endswith((b"\n", b"\r"))
            if repair_tail and incomplete_tail:
                timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                quarantine = path.with_name(f"{path.name}.tail-{timestamp}.quarantine")
                durable_write_bytes(quarantine, raw[offset:])
                backup = path.with_name(f"{path.name}.pre-repair-{timestamp}.bak")
                durable_write_bytes(backup, raw)
                with path.open("r+b") as handle:
                    handle.truncate(offset)
                    handle.flush()
                    os.fsync(handle.fileno())
                _fsync_directory(path.parent)
                return parsed
            location = "tail" if is_last else f"line {index + 1}"
            raise LedgerCorruptionError(f"{path.name}: invalid JSONL at {location}") from exc
        offset += len(line)
    return parsed


def _append_json_line(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
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
    return candidate_copy == stored_copy


class LedgerStore:
    """The single append path used by cron jobs and interactive trade entry."""

    def __init__(self, root: str | Path, *, lock_timeout: float = 10.0) -> None:
        self.root = Path(root)
        self.ledger_dir = self.root / "ledger"
        self.lock_path = self.root / "locks" / "ledger-global.lock"
        self.lock_timeout = lock_timeout

    def path_for(self, portfolio: str) -> Path:
        return self.ledger_dir / f"{portfolio}.jsonl"

    def read(self, portfolio: str, *, repair_tail: bool = False) -> list[dict[str, Any]]:
        return read_jsonl(self.path_for(portfolio), repair_tail=repair_tail)

    def all_events(self) -> list[dict[str, Any]]:
        combined: list[dict[str, Any]] = []
        for portfolio in ("paper", "live", "market"):
            combined.extend(self.read(portfolio))
        return combined

    def append(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Validate, replay, and durably append exactly one event."""

        validate_event(candidate)
        portfolio = candidate["portfolio"]
        with FileLock(self.lock_path, timeout=self.lock_timeout):
            all_events = self.all_events()
            for existing in all_events:
                if existing["event_id"] != candidate["event_id"]:
                    continue
                if _same_retry_payload(candidate, existing):
                    return {"status": "duplicate", "event": existing}
                raise ConflictError(
                    f"event_id already exists with different payload: "
                    f"{candidate['event_id']}"
                )

            ledger_events = self.read(portfolio)
            stored = deepcopy(candidate)
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
            return {"status": "appended", "event": stored}

    def repair_tail(self, portfolio: str) -> list[dict[str, Any]]:
        with FileLock(self.lock_path, timeout=self.lock_timeout):
            return self.read(portfolio, repair_tail=True)
