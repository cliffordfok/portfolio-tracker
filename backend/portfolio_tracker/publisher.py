"""Crash-safe GitHub Contents API snapshot publisher."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .errors import PublicationError
from .ledger import FileLock, atomic_write_json, durable_unlink


class NetworkFailure(Exception):
    """A request may have reached GitHub but no response was received."""


@dataclass
class RemoteContent:
    blob_sha: str
    content: bytes
    commit_sha: str | None = None

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass
class PutResult:
    status: int
    blob_sha: str | None = None
    commit_sha: str | None = None
    headers: Mapping[str, str] | None = None
    message: str | None = None


class ContentClient(Protocol):
    def branch_exists(self) -> bool: ...

    def get_content(self) -> RemoteContent | None: ...

    def put_content(
        self,
        content: bytes,
        *,
        expected_blob_sha: str | None,
        message: str,
    ) -> PutResult: ...


class GitHubContentsClient:
    """Minimal standard-library GitHub client; token is never logged."""

    def __init__(
        self,
        *,
        repository: str,
        branch: str,
        path: str,
        token: str,
        timeout: float = 20.0,
    ) -> None:
        if "/" not in repository:
            raise ValueError("repository must use owner/name")
        self.repository = repository
        self.branch = branch
        self.path = path
        self.token = token
        self.timeout = timeout
        self.base = f"https://api.github.com/repos/{repository}"
        self.branch_commit_sha: str | None = None

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, str], dict[str, Any] | None]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "portfolio-tracker-c-plus",
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
                decoded = json.loads(payload) if payload else None
                return response.status, dict(response.headers.items()), decoded
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                decoded = json.loads(payload) if payload else None
            except json.JSONDecodeError:
                decoded = None
            return exc.code, dict(exc.headers.items()), decoded
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NetworkFailure(str(exc)) from exc

    def branch_exists(self) -> bool:
        branch = urllib.parse.quote(self.branch, safe="")
        status, headers, payload = self._request(f"{self.base}/git/ref/heads/{branch}")
        if status == 200:
            self.branch_commit_sha = (payload or {}).get("object", {}).get("sha")
            return True
        if status == 404:
            return False
        lowered = {key.lower(): value for key, value in headers.items()}
        if status >= 500 or (
            status in {403, 429}
            and (
                "retry-after" in lowered
                or lowered.get("x-ratelimit-remaining") == "0"
            )
        ):
            raise NetworkFailure(f"retryable branch lookup failure: {status}")
        raise PublicationError(f"cannot check data branch: GitHub returned {status}")

    def get_content(self) -> RemoteContent | None:
        encoded_path = urllib.parse.quote(self.path)
        query = urllib.parse.urlencode({"ref": self.branch})
        status, headers, payload = self._request(
            f"{self.base}/contents/{encoded_path}?{query}"
        )
        if status == 404:
            return None
        lowered = {key.lower(): value for key, value in headers.items()}
        if status >= 500 or (
            status in {403, 429}
            and (
                "retry-after" in lowered
                or lowered.get("x-ratelimit-remaining") == "0"
            )
        ):
            raise NetworkFailure(f"retryable content lookup failure: {status}")
        if status != 200 or not payload:
            raise PublicationError(f"cannot read remote snapshot: GitHub returned {status}")
        if payload.get("type") != "file":
            raise PublicationError("remote snapshot path is not a file")
        try:
            encoded = "".join(payload["content"].split())
            content = base64.b64decode(encoded, validate=True)
        except (KeyError, ValueError) as exc:
            raise PublicationError("GitHub returned invalid snapshot content") from exc
        return RemoteContent(
            blob_sha=payload["sha"],
            content=content,
            commit_sha=self.branch_commit_sha,
        )

    def put_content(
        self,
        content: bytes,
        *,
        expected_blob_sha: str | None,
        message: str,
    ) -> PutResult:
        encoded_path = urllib.parse.quote(self.path)
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
            "branch": self.branch,
        }
        if expected_blob_sha is not None:
            body["sha"] = expected_blob_sha
        status, headers, payload = self._request(
            f"{self.base}/contents/{encoded_path}",
            method="PUT",
            body=body,
        )
        if status in {200, 201} and payload:
            return PutResult(
                status=status,
                blob_sha=payload.get("content", {}).get("sha"),
                commit_sha=payload.get("commit", {}).get("sha"),
                headers=headers,
            )
        return PutResult(
            status=status,
            headers=headers,
            message=(payload or {}).get("message") if payload else None,
        )


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError(f"invalid state file: {path.name}") from exc
    if not isinstance(value, dict):
        raise PublicationError(f"invalid state file: {path.name}")
    return value


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class SnapshotPublisher:
    """Publish one snapshot with bounded retries and durable intent recovery."""

    def __init__(
        self,
        *,
        root: str | Path,
        client: ContentClient,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.root = Path(root)
        self.client = client
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.snapshot_path = self.root / "snapshots" / "portfolio-snapshot.json"
        self.state_dir = self.root / "state"
        self.published_state_path = self.state_dir / "published-state.json"
        self.attempt_path = self.state_dir / "publication-attempt.json"
        self.pending_path = self.state_dir / "publish.pending"
        self.lock_path = self.root / "locks" / "portfolio-publish.lock"

    def _snapshot(self) -> tuple[bytes, dict[str, Any], str]:
        try:
            content = self.snapshot_path.read_bytes()
            payload = json.loads(content)
        except (OSError, json.JSONDecodeError) as exc:
            raise PublicationError("local snapshot is missing or invalid") from exc
        if not isinstance(payload, dict) or "revision" not in payload:
            raise PublicationError("local snapshot has no revision")
        return content, payload, _hash(content)

    def _write_attempt(
        self, *, intended_hash: str, expected_sha: str | None, revision: Any
    ) -> None:
        atomic_write_json(
            self.attempt_path,
            {
                "intended_hash": intended_hash,
                "expected_remote_blob_sha": expected_sha,
                "revision": revision,
                "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            },
        )

    def _adopt(
        self,
        *,
        local_hash: str,
        remote: RemoteContent,
        revision: Any,
    ) -> None:
        atomic_write_json(
            self.published_state_path,
            {
                "local_snapshot_hash": local_hash,
                "remote_blob_sha": remote.blob_sha,
                "remote_commit_sha": remote.commit_sha,
                "published_revision": revision,
                "published_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            },
        )
        durable_unlink(self.attempt_path)

    def _retry_delay(self, attempt: int, result: PutResult | None = None) -> float:
        headers = {key.lower(): value for key, value in (result.headers or {}).items()} if result else {}
        if "retry-after" in headers:
            try:
                return min(max(float(headers["retry-after"]), 0), 60)
            except ValueError:
                return 1
        if headers.get("x-ratelimit-remaining") == "0":
            try:
                reset = float(headers.get("x-ratelimit-reset", "0"))
                return min(max(reset - time.time(), 0), 60)
            except ValueError:
                return 1
        return min((2**attempt) + random.random(), 8)

    def publish(self) -> dict[str, Any]:
        with FileLock(self.lock_path):
            for attempt_number in range(1, self.max_attempts + 1):
                try:
                    if not self.client.branch_exists():
                        raise PublicationError("portfolio-data branch does not exist")
                    local_bytes, local_payload, local_hash = self._snapshot()
                    published = _read_json(self.published_state_path)
                    intent = _read_json(self.attempt_path)
                    remote = self.client.get_content()
                    remote_sha = remote.blob_sha if remote else None
                    remote_hash = remote.content_hash if remote else None

                    if intent:
                        intended_hash = intent.get("intended_hash")
                        expected_sha = intent.get("expected_remote_blob_sha")
                        if remote_hash == intended_hash and remote is not None:
                            self._adopt(
                                local_hash=intended_hash,
                                remote=remote,
                                revision=intent.get("revision"),
                            )
                            if local_hash == intended_hash:
                                durable_unlink(self.pending_path)
                                return {
                                    "status": "recovered",
                                    "attempts": attempt_number,
                                    "blob_sha": remote.blob_sha,
                                }
                            published = _read_json(self.published_state_path)
                            intent = None
                        elif remote_sha == expected_sha:
                            if local_hash != intended_hash:
                                self._write_attempt(
                                    intended_hash=local_hash,
                                    expected_sha=remote_sha,
                                    revision=local_payload["revision"],
                                )
                        else:
                            raise PublicationError(
                                "unknown remote edit during publication recovery"
                            )

                    if published is None and remote is not None and intent is None:
                        if remote_hash == local_hash:
                            raise PublicationError(
                                "remote file exists without publication state; "
                                "manual adoption is required"
                            )
                        raise PublicationError(
                            "remote file exists without publication state"
                        )

                    if published and remote_sha != published.get("remote_blob_sha"):
                        if remote is not None and remote_hash == local_hash:
                            self._adopt(
                                local_hash=local_hash,
                                remote=remote,
                                revision=local_payload["revision"],
                            )
                            durable_unlink(self.pending_path)
                            return {
                                "status": "adopted",
                                "attempts": attempt_number,
                                "blob_sha": remote.blob_sha,
                            }
                        raise PublicationError("unknown manual edit on data branch")

                    if remote is not None and remote_hash == local_hash:
                        self._adopt(
                            local_hash=local_hash,
                            remote=remote,
                            revision=local_payload["revision"],
                        )
                        durable_unlink(self.pending_path)
                        return {
                            "status": "current",
                            "attempts": attempt_number,
                            "blob_sha": remote.blob_sha,
                        }

                    self._write_attempt(
                        intended_hash=local_hash,
                        expected_sha=remote_sha,
                        revision=local_payload["revision"],
                    )
                    result = self.client.put_content(
                        local_bytes,
                        expected_blob_sha=remote_sha,
                        message=f"publish snapshot rev {local_payload['revision']}",
                    )

                    if result.status in {200, 201}:
                        if not result.blob_sha:
                            raise PublicationError("GitHub success response has no blob SHA")
                        adopted = RemoteContent(
                            blob_sha=result.blob_sha,
                            content=local_bytes,
                            commit_sha=result.commit_sha,
                        )
                        self._adopt(
                            local_hash=local_hash,
                            remote=adopted,
                            revision=local_payload["revision"],
                        )
                        current_bytes, _, current_hash = self._snapshot()
                        if current_hash == local_hash and current_bytes == local_bytes:
                            durable_unlink(self.pending_path)
                        return {
                            "status": "published",
                            "attempts": attempt_number,
                            "blob_sha": result.blob_sha,
                            "commit_sha": result.commit_sha,
                        }

                    headers = {
                        key.lower(): value
                        for key, value in (result.headers or {}).items()
                    }
                    rate_limited = (
                        "retry-after" in headers
                        or headers.get("x-ratelimit-remaining") == "0"
                    )
                    if (
                        result.status == 409
                        or result.status >= 500
                        or (result.status in {403, 429} and rate_limited)
                    ):
                        if attempt_number == self.max_attempts:
                            break
                        self.sleep(self._retry_delay(attempt_number - 1, result))
                        continue
                    if result.status == 401:
                        raise PublicationError("GitHub authentication failed")
                    if result.status == 403:
                        raise PublicationError(
                            "GitHub denied the write; check PAT scope and branch protection"
                        )
                    raise PublicationError(
                        f"GitHub PUT failed with status {result.status}: "
                        f"{result.message or 'unknown error'}"
                    )

                except NetworkFailure:
                    if attempt_number == self.max_attempts:
                        break
                    self.sleep(self._retry_delay(attempt_number - 1))
                    continue

            raise PublicationError(
                f"publication retries exhausted after {self.max_attempts} attempts"
            )


def client_from_environment(
    *, repository: str, branch: str, path: str
) -> GitHubContentsClient:
    token = os.environ.get("PORTFOLIO_GITHUB_TOKEN")
    if not token:
        raise PublicationError("PORTFOLIO_GITHUB_TOKEN is not set")
    return GitHubContentsClient(
        repository=repository,
        branch=branch,
        path=path,
        token=token,
    )
