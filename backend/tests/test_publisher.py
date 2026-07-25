from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import base64
from pathlib import Path

from portfolio_tracker.errors import PublicationError
from portfolio_tracker.ledger import FileLock, atomic_write_json
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
        self.branch_calls = 0
        self.get_calls = 0
        self.exists = True
        self.branch_failures = 0
        self.branch_failure_headers: list[dict[str, str]] = []
        self.conflict_remote: RemoteContent | None = None

    def branch_exists(self) -> bool:
        self.branch_calls += 1
        if self.branch_failures:
            self.branch_failures -= 1
            headers = (
                self.branch_failure_headers.pop(0)
                if self.branch_failure_headers
                else {}
            )
            raise NetworkFailure(
                "temporary branch lookup timeout",
                headers=headers,
            )
        return self.exists

    def get_content(self) -> RemoteContent | None:
        self.get_calls += 1
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
        if status == 409 and self.conflict_remote is not None:
            self.remote = self.conflict_remote
        return PutResult(status, headers=headers)


class PublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.snapshot_path = self.root / "snapshots" / "portfolio-snapshot.json"
        self.snapshot_path.parent.mkdir(parents=True)
        self.write_snapshot(1, "first")
        self.client = FakeClient()
        atomic_write_json(
            self.root / "state" / "publish.pending",
            {"revision": 1},
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_snapshot(self, revision: int, marker: str) -> bytes:
        content = json.dumps(
            {"schema_version": 3, "revision": revision, "marker": marker},
            sort_keys=True,
        ).encode("utf-8")
        self.snapshot_path.write_bytes(content)
        return content

    def publisher(self, *, allow_bootstrap: bool = False) -> SnapshotPublisher:
        return SnapshotPublisher(
            root=self.root,
            client=self.client,
            allow_bootstrap=allow_bootstrap,
            sleep=lambda _: None,
        )

    def test_first_publish_writes_durable_state(self) -> None:
        result = self.publisher().publish()
        self.assertEqual(result["status"], "published")
        self.assertTrue((self.root / "state" / "published-state.json").exists())
        self.assertFalse((self.root / "state" / "publication-attempt.json").exists())
        self.assertFalse((self.root / "state" / "publish.pending").exists())
        self.assertEqual(len(self.client.put_calls), 1)

    def test_missing_pending_marker_exits_without_github_requests(self) -> None:
        content = self.snapshot_path.read_bytes()
        atomic_write_json(
            self.root / "state" / "published-state.json",
            {
                "local_snapshot_hash": hashlib.sha256(content).hexdigest(),
                "remote_blob_sha": "known-sha",
                "remote_commit_sha": "known-commit",
                "published_revision": 1,
            },
        )
        (self.root / "state" / "publish.pending").unlink()

        result = self.publisher().publish()

        self.assertEqual(result, {"status": "idle", "attempts": 0})
        self.assertEqual(self.client.put_calls, [])
        self.assertEqual(self.client.branch_calls, 0)
        self.assertEqual(self.client.get_calls, 0)

    def test_timer_recovers_changed_snapshot_without_pending_marker(self) -> None:
        old_content = self.snapshot_path.read_bytes()
        old_sha = "known-sha"
        self.client.remote = RemoteContent(
            old_sha,
            old_content,
            "known-commit",
        )
        atomic_write_json(
            self.root / "state" / "published-state.json",
            {
                "local_snapshot_hash": hashlib.sha256(old_content).hexdigest(),
                "remote_blob_sha": old_sha,
                "remote_commit_sha": "known-commit",
                "published_revision": 1,
            },
        )
        latest = self.write_snapshot(2, "changed-before-pending")
        (self.root / "state" / "publish.pending").unlink()

        result = self.publisher().publish()

        self.assertEqual(result["status"], "published")
        self.assertEqual(self.client.put_calls, [latest])
        self.assertEqual(self.client.branch_calls, 1)
        self.assertEqual(self.client.get_calls, 1)

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

    def test_get_rate_limit_honors_retry_after_header(self) -> None:
        sleeps: list[float] = []
        self.client.branch_failures = 1
        self.client.branch_failure_headers = [{"Retry-After": "7"}]
        publisher = SnapshotPublisher(
            root=self.root,
            client=self.client,
            sleep=sleeps.append,
        )
        result = publisher.publish()
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(sleeps, [7.0])

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

    def test_unresolved_attempt_recovers_even_if_pending_marker_is_missing(
        self,
    ) -> None:
        content = self.snapshot_path.read_bytes()
        intended = hashlib.sha256(content).hexdigest()
        self.client.remote = RemoteContent(
            "remote-success",
            content,
            "commit-success",
        )
        atomic_write_json(
            self.root / "state" / "publication-attempt.json",
            {
                "intended_hash": intended,
                "expected_remote_blob_sha": None,
                "revision": 1,
            },
        )
        (self.root / "state" / "publish.pending").unlink()

        result = self.publisher().publish()

        self.assertEqual(result["status"], "recovered")
        self.assertEqual(self.client.put_calls, [])
        self.assertFalse(
            (self.root / "state" / "publication-attempt.json").exists()
        )

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

    def test_unknown_remote_blob_with_same_content_still_fails_closed(
        self,
    ) -> None:
        content = self.snapshot_path.read_bytes()
        atomic_write_json(
            self.root / "state" / "published-state.json",
            {
                "local_snapshot_hash": hashlib.sha256(content).hexdigest(),
                "remote_blob_sha": "known-sha",
                "remote_commit_sha": "known-commit",
                "published_revision": 1,
            },
        )
        self.client.remote = RemoteContent(
            "unknown-sha",
            content,
            "unknown-commit",
        )

        with self.assertRaisesRegex(PublicationError, "manual edit"):
            self.publisher().publish()

        self.assertEqual(self.client.put_calls, [])

    def test_remote_file_without_state_requires_explicit_bootstrap(self) -> None:
        self.client.remote = RemoteContent("existing", b"old public snapshot")
        with self.assertRaisesRegex(PublicationError, "publication state"):
            self.publisher().publish()
        result = self.publisher(allow_bootstrap=True).publish()
        self.assertEqual(result["status"], "published")
        self.assertEqual(len(self.client.put_calls), 1)

    def test_busy_publisher_exits_without_waiting_or_writing(self) -> None:
        publisher = self.publisher()
        with FileLock(publisher.lock_path):
            self.assertEqual(
                publisher.publish(),
                {"status": "busy", "attempts": 0},
            )
        self.assertEqual(self.client.put_calls, [])

    def test_branch_must_be_created_manually(self) -> None:
        self.client.exists = False
        with self.assertRaisesRegex(PublicationError, "branch does not exist"):
            self.publisher().publish()
        self.assertEqual(self.client.put_calls, [])

    def test_authentication_and_permission_failures_are_not_retried(self) -> None:
        for status, message in (
            (401, "authentication failed"),
            (403, "denied the write"),
        ):
            with self.subTest(status=status):
                self.client = FakeClient()
                self.client.put_statuses = [status, 200]
                with self.assertRaisesRegex(PublicationError, message):
                    self.publisher().publish()
                self.assertEqual(len(self.client.put_calls), 1)

    def test_conflict_then_manual_edit_fails_closed_on_fresh_get(self) -> None:
        local = self.snapshot_path.read_bytes()
        old_remote = RemoteContent("known-sha", b"known remote", "known-commit")
        self.client.remote = old_remote
        self.client.put_statuses = [409, 200]
        self.client.conflict_remote = RemoteContent(
            "manual-sha",
            b"manual edit",
            "manual-commit",
        )
        atomic_write_json(
            self.root / "state" / "published-state.json",
            {
                "local_snapshot_hash": hashlib.sha256(b"old local").hexdigest(),
                "remote_blob_sha": old_remote.blob_sha,
                "remote_commit_sha": old_remote.commit_sha,
                "published_revision": 0,
            },
        )
        with self.assertRaisesRegex(PublicationError, "remote edit"):
            self.publisher().publish()
        self.assertEqual(self.client.put_calls, [local])

    def test_remote_mismatch_never_clears_pending_marker(self) -> None:
        content = self.snapshot_path.read_bytes()
        atomic_write_json(
            self.root / "state" / "published-state.json",
            {
                "local_snapshot_hash": hashlib.sha256(content).hexdigest(),
                "remote_blob_sha": "known-sha",
                "remote_commit_sha": "known-commit",
                "published_revision": 1,
            },
        )
        atomic_write_json(
            self.root / "state" / "publish.pending",
            {"revision": 1},
        )
        self.client.remote = RemoteContent("manual-sha", b"manual edit")
        with self.assertRaisesRegex(PublicationError, "manual edit"):
            self.publisher().publish()
        self.assertTrue((self.root / "state" / "publish.pending").exists())

    def test_github_content_accepts_wrapped_base64(self) -> None:
        client = GitHubContentsClient(
            repository="owner/repo",
            branch="portfolio-data",
            path="portfolio-snapshot.json",
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
