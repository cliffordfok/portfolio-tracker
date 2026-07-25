from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from portfolio_tracker.errors import (
    BusinessInvariantError,
    ConflictError,
    ValidationError,
)
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
        repaired = self.store.repair_tail("paper")
        self.assertEqual(repaired, [valid])
        self.assertEqual(read_jsonl(path), [valid])
        quarantines = list((self.root / "quarantine").glob("*.quarantine"))
        backups = list((self.root / "backups").glob("*.bak"))
        self.assertEqual(len(quarantines), 1)
        self.assertEqual(len(backups), 1)
        self.assertEqual(quarantines[0].read_bytes(), b'{"event_id":')
        self.assertIn(b'{"event_id":', backups[0].read_bytes())

    def test_concurrent_writers_serialize_without_lost_events(self) -> None:
        self.store.append(
            candidate(
                "PORTFOLIO_OPEN",
                portfolio="paper",
                event_id="paper-open-concurrent",
                occurred_at="2024-01-01T14:00:00Z",
                initial_cash="10000",
            )
        )
        barrier = threading.Barrier(2)

        def append_buy(index: int) -> str:
            barrier.wait()
            result = self.store.append(
                candidate(
                    "BUY",
                    portfolio="paper",
                    event_id=f"paper-buy-{index}",
                    occurred_at=f"2024-01-0{index + 1}T15:00:00Z",
                    symbol="AAPL",
                    shares="1",
                    price="100",
                    fee="0",
                )
            )
            return result["status"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(append_buy, (1, 2)))
        stored = self.store.read("paper")
        self.assertEqual(statuses, ["appended", "appended"])
        self.assertEqual(len(stored), 3)
        self.assertEqual([item["ledger_seq"] for item in stored], [1, 2, 3])

    def test_event_id_prefix_prevents_cross_ledger_collision(self) -> None:
        self.store.append(self.open)
        market = candidate(
            "QUOTE",
            portfolio="market",
            event_id="market-quote-1",
            occurred_at="2024-01-02T21:00:00Z",
            symbol="AAPL",
            close="100",
            session_date="2024-01-02",
        )
        self.store.append(market)
        conflict = dict(market, portfolio="paper", action="CASH_FLOW", amount="1")
        conflict.pop("symbol")
        conflict.pop("close")
        conflict.pop("session_date")
        with self.assertRaisesRegex(ValidationError, "must start with 'paper-'"):
            self.store.append(conflict)


if __name__ == "__main__":
    unittest.main()
