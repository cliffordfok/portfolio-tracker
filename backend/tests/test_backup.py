from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from portfolio_tracker.backup import backup_ledgers
from portfolio_tracker.ledger import LedgerStore

from .helpers import candidate


class BackupTests(unittest.TestCase):
    def test_backup_copies_exact_ledger_bytes_with_hash_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = LedgerStore(root)
            store.append(
                candidate(
                    "PORTFOLIO_OPEN",
                    portfolio="paper",
                    event_id="paper-open",
                    occurred_at="2024-01-01T14:00:00Z",
                    initial_cash="1000",
                )
            )
            source = store.path_for("paper").read_bytes()
            manifest = backup_ledgers(
                root,
                now=datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
            )
            backup_dir = root / "backups" / manifest["backup_id"]
            self.assertEqual((backup_dir / "paper.jsonl").read_bytes(), source)
            self.assertEqual(
                manifest["ledgers"]["paper"]["sha256"],
                hashlib.sha256(source).hexdigest(),
            )
            self.assertTrue((backup_dir / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
