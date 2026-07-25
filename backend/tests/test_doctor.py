from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from portfolio_tracker.backup import backup_ledgers
from portfolio_tracker.doctor import audit_runtime
from portfolio_tracker.errors import PortfolioError
from portfolio_tracker.ledger import LedgerStore, atomic_write_json
from portfolio_tracker.snapshot import build_snapshot

from .helpers import candidate


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = LedgerStore(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def initialize(self) -> None:
        for portfolio, initial_cash in (("paper", "100000"), ("live", "50000")):
            self.store.append(
                candidate(
                    "PORTFOLIO_OPEN",
                    portfolio=portfolio,
                    event_id=f"{portfolio}-open",
                    occurred_at="2024-01-02T14:00:00Z",
                    initial_cash=initial_cash,
                )
            )

    def test_empty_runtime_is_reported_without_being_modified(self) -> None:
        report = audit_runtime(self.root)

        self.assertEqual(report["status"], "healthy")
        self.assertFalse(report["ledgers"]["paper"]["initialized"])
        self.assertFalse(report["snapshot"]["exists"])
        self.assertFalse((self.root / "locks").exists())

    def test_require_initialized_rejects_an_empty_runtime(self) -> None:
        with self.assertRaisesRegex(PortfolioError, "paper portfolio"):
            audit_runtime(self.root, require_initialized=True)

    def test_require_current_detects_a_stale_snapshot(self) -> None:
        self.initialize()
        build_snapshot(self.root)
        self.store.append(
            candidate(
                "CASH_FLOW",
                portfolio="paper",
                event_id="paper-cash-after-snapshot",
                occurred_at="2024-01-02T15:00:00Z",
                amount="1",
            )
        )

        with self.assertRaisesRegex(PortfolioError, "revision does not match"):
            audit_runtime(
                self.root,
                require_initialized=True,
                require_current=True,
            )

    def test_nested_snapshot_corruption_is_rejected(self) -> None:
        self.initialize()
        build_snapshot(self.root)
        path = self.root / "snapshots" / "portfolio-snapshot.json"
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        snapshot["portfolios"]["paper"]["holdings"] = "not-an-array"
        atomic_write_json(path, snapshot)

        with self.assertRaisesRegex(PortfolioError, "paper.holdings"):
            audit_runtime(
                self.root,
                require_initialized=True,
                require_current=True,
            )

    def test_full_acceptance_verifies_publication_and_backup(self) -> None:
        self.initialize()
        snapshot = build_snapshot(self.root)
        snapshot_path = self.root / "snapshots" / "portfolio-snapshot.json"
        snapshot_hash = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
        atomic_write_json(
            self.root / "state" / "published-state.json",
            {
                "local_snapshot_hash": snapshot_hash,
                "remote_blob_sha": "remote-blob-sha",
                "remote_commit_sha": "remote-commit-sha",
                "published_revision": snapshot["revision"],
                "published_at": "2024-01-02T21:00:00Z",
            },
        )
        backup_ledgers(self.root)

        report = audit_runtime(
            self.root,
            require_initialized=True,
            require_current=True,
            require_published=True,
            require_backup=True,
        )

        self.assertEqual(report["status"], "healthy")
        self.assertTrue(report["snapshot"]["current"])
        self.assertTrue(report["publication"]["published"])
        self.assertTrue(report["backup"]["verified"])
        self.assertEqual(report["warnings"], [])

    def test_require_published_rejects_an_unresolved_attempt(self) -> None:
        self.initialize()
        build_snapshot(self.root)
        atomic_write_json(
            self.root / "state" / "publication-attempt.json",
            {
                "intended_hash": "pending",
                "expected_remote_blob_sha": None,
                "revision": 2,
            },
        )

        with self.assertRaisesRegex(PortfolioError, "attempt is unresolved"):
            audit_runtime(
                self.root,
                require_initialized=True,
                require_current=True,
                require_published=True,
            )


if __name__ == "__main__":
    unittest.main()
