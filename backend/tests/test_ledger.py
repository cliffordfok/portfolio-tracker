from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from portfolio_tracker.errors import BusinessInvariantError, ConflictError
from portfolio_tracker.ledger import LedgerStore, read_jsonl

from .helpers import candidate


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = LedgerStore(self.root)
        self.open = candidate(
            "PORTFOLIO_OPEN",
            portfolio="paper",
            event_id="paper-open",
            occurred_at="2024-01-01T14:00:00Z",
            initial_cash="1000",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_idempotent_retry_is_noop(self) -> None:
        first = self.store.append(self.open)
        second = self.store.append(self.open)
        self.assertEqual(first["status"], "appended")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(self.store.read("paper")), 1)
        marker = self.root / "state" / "rebuild.pending"
        self.assertTrue(marker.exists())
        self.assertEqual(json.loads(marker.read_text())["event_id"], "paper-open")

    def test_same_event_id_with_new_payload_is_conflict(self) -> None:
        self.store.append(self.open)
        changed = dict(self.open)
        changed["initial_cash"] = "2000"
        with self.assertRaises(ConflictError):
            self.store.append(changed)

    def test_invalid_business_event_is_not_appended(self) -> None:
        self.store.append(self.open)
        sell = candidate(
            "SELL",
            portfolio="paper",
            event_id="paper-sell",
            occurred_at="2024-01-02T15:00:00Z",
            symbol="AAPL",
            shares="1",
            price="10",
            fee="0",
        )
        with self.assertRaises(BusinessInvariantError):
            self.store.append(sell)
        self.assertEqual(len(self.store.read("paper")), 1)

    def test_truncated_tail_can_be_quarantined(self) -> None:
        path = self.store.path_for("paper")
        path.parent.mkdir(parents=True)
        valid = dict(self.open, ledger_seq=1)
        path.write_bytes(
            (json.dumps(valid) + "\n").encode("utf-8") + b'{"event_id":'
        )
        repaired = read_jsonl(path, repair_tail=True)
        self.assertEqual(repaired, [valid])
        self.assertEqual(read_jsonl(path), [valid])
        self.assertTrue(list(path.parent.glob("*.quarantine")))
        self.assertTrue(list(path.parent.glob("*.bak")))


if __name__ == "__main__":
    unittest.main()
