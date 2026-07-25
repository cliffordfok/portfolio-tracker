from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import base64
from pathlib import Path

from portfolio_tracker.errors import PublicationError
from portfolio_tracker.ledger import atomic_write_json
from portfolio_tracker.publisher import (
    GitHubContentsClient,
    NetworkFailure,
    PutResult,
    RemoteContent,
    SnapshotPublisher,
)


class FakeClient:
    def __init__(self) -> None:
        self.remote: RemoteContent | None = None
        self.put_statuses: list[int | str] = [200]
        self.put_headers: list[dict[str, str]] = []
        self.put_calls: list[bytes] = []
        self.exists = True
        self.branch_failures = 0

    def branch_exists(self) -> bool:
        if self.branch_failures:
            self.branch_failures -= 1
            raise NetworkFailure("temporary branch lookup timeout")
        return self.exists

    def get_content(self) -> RemoteContent | None:
        return self.remote

    def put_content(
        self, content: bytes, *, expected_blob_sha: str | None, message: str
    ) -> PutResult:
        self.put_calls.append(content)
        status = self.put_statuses.pop(0) if self.put_statuses else 200
        headers = self.put_headers.pop(0) if self.put_headers else {}
        if status == "network":
            raise NetworkFailure("PUT response timeout")
        if status in {200, 201}:
            sha = hashlib.sha1(content).hexdigest()
            self.remote = RemoteContent(sha, content, f"commit-{sha}")
            return PutResult(status, sha, f"commit-{sha}", headers)
        return PutResult(status, headers=headers)


class PublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.snapshot_path = self.root / "snapshots" / "portfolio-snapshot.json"
        self.snapshot_path.parent.mkdir(parents=True)
        self.write_snapshot(1, "first")
        self.client = FakeClient()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_snapshot(self, revision: int, marker: str) -> bytes:
        content = json.dumps(
            {"schema_version": 3, "revision": revision, "marker": marker},
            sort_keys=True,
        ).encode("utf-8")
        self.snapshot_path.write_bytes(content)
        return content

    def publisher(self) -> SnapshotPublisher:
        return SnapshotPublisher(
            root=self.root,
            client=self.client,
            sleep=lambda _: None,
        )

    def test_first_publish_writes_durable_state(self) -> None:
        result = self.publisher().publish()
        self.assertEqual(result["status"], "published")
        self.assertTrue((self.root / "state" / "published-state.json").exists())
        self.assertFalse((self.root / "state" / "publication-attempt.json").exists())
        self.assertEqual(len(self.client.put_calls), 1)

    def test_three_conflicts_exhaust_retry_budget(self) -> None:
        self.client.put_statuses = [409, 409, 409]
        with self.assertRaisesRegex(PublicationError, "retries exhausted"):
            self.publisher().publish()
        self.assertEqual(len(self.client.put_calls), 3)

    def test_branch_lookup_timeout_is_inside_retry_budget(self) -> None:
        self.client.branch_failures = 1
        result = self.publisher().publish()
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(self.client.put_calls), 1)

    def test_put_timeout_rechecks_remote_before_retry(self) -> None:
        self.client.put_statuses = ["network", 200]
        result = self.publisher().publish()
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(self.client.put_calls), 2)

    def test_rate_limit_with_retry_after_is_bounded_and_retried(self) -> None:
        self.client.put_statuses = [403, 200]
        self.client.put_headers = [{"Retry-After": "0"}, {}]
        result = self.publisher().publish()
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["attempts"], 2)

    def test_successful_put_before_crash_is_adopted(self) -> None:
        content = self.snapshot_path.read_bytes()
        intended = hashlib.sha256(content).hexdigest()
        remote_sha = "remote-success"
        self.client.remote = RemoteContent(remote_sha, content, "commit-success")
        atomic_write_json(
            self.root / "state" / "publication-attempt.json",
            {
                "intended_hash": intended,
                "expected_remote_blob_sha": None,
                "revision": 1,
            },
        )
        result = self.publisher().publish()
        self.assertEqual(result["status"], "recovered")
        self.assertEqual(len(self.client.put_calls), 0)

    def test_new_local_snapshot_replaces_stale_attempt(self) -> None:
        old = b'{"marker":"old","revision":1,"schema_version":3}'
        old_hash = hashlib.sha256(old).hexdigest()
        self.client.remote = RemoteContent("expected-sha", b"remote-old")
        atomic_write_json(
            self.root / "state" / "publication-attempt.json",
            {
                "intended_hash": old_hash,
                "expected_remote_blob_sha": "expected-sha",
                "revision": 1,
            },
        )
        latest = self.write_snapshot(2, "latest")
        result = self.publisher().publish()
        self.assertEqual(result["status"], "published")
        self.assertEqual(self.client.put_calls[-1], latest)

    def test_unknown_remote_edit_fails_closed(self) -> None:
        atomic_write_json(
            self.root / "state" / "published-state.json",
            {
                "local_snapshot_hash": "old-local",
                "remote_blob_sha": "known-sha",
                "remote_commit_sha": "known-commit",
                "published_revision": 0,
            },
        )
        self.client.remote = RemoteContent("unknown-sha", b"manual edit")
        with self.assertRaisesRegex(PublicationError, "manual edit"):
            self.publisher().publish()
        self.assertEqual(len(self.client.put_calls), 0)

    def test_github_content_accepts_wrapped_base64(self) -> None:
        client = GitHubContentsClient(
            repository="owner/repo",
            branch="portfolio-data",
            path="data/portfolio-snapshot.json",
            token="test-only",
        )
        raw = b'{"revision":1}'
        encoded = base64.b64encode(raw).decode("ascii")
        wrapped = f"{encoded[:8]}\n{encoded[8:]}\n"
        client.branch_commit_sha = "branch-commit"
        client._request = lambda url: (
            200,
            {},
            {"type": "file", "sha": "blob-sha", "content": wrapped},
        )
        remote = client.get_content()
        self.assertEqual(remote.content, raw)
        self.assertEqual(remote.commit_sha, "branch-commit")


if __name__ == "__main__":
    unittest.main()
