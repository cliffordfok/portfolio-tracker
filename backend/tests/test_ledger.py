from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import portfolio_tracker.ledger as ledger_module
from portfolio_tracker.errors import (
    BusinessInvariantError,
    ConflictError,
    LedgerCorruptionError,
    ValidationError,
)
from portfolio_tracker.ledger import FileLock, LedgerStore, read_jsonl
from portfolio_tracker.snapshot import build_snapshot

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

    def test_lock_file_permissions_do_not_depend_on_caller_umask(self) -> None:
        lock_path = self.root / "locks" / "test.lock"
        with patch("portfolio_tracker.ledger.os.chmod") as chmod:
            with FileLock(lock_path):
                pass

        chmod.assert_called_with(lock_path, 0o600)

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

    def test_batch_is_fully_validated_before_any_event_is_written(self) -> None:
        self.store.append(self.open)
        (self.root / "state" / "rebuild.pending").unlink()
        valid_buy = candidate(
            "BUY",
            portfolio="paper",
            event_id="paper-batch-buy",
            occurred_at="2024-01-02T15:00:00Z",
            symbol="AAPL",
            shares="1",
            price="100",
            fee="0",
        )
        invalid_sell = candidate(
            "SELL",
            portfolio="paper",
            event_id="paper-batch-oversell",
            occurred_at="2024-01-02T15:01:00Z",
            symbol="MSFT",
            shares="1",
            price="100",
            fee="0",
        )

        with self.assertRaises(BusinessInvariantError):
            self.store.append_many([valid_buy, invalid_sell])

        self.assertEqual(self.store.read("paper"), [
            {**self.open, "ledger_seq": 1}
        ])
        self.assertFalse((self.root / "state" / "rebuild.pending").exists())

    def test_batch_uses_one_marker_and_contiguous_ledger_sequences(self) -> None:
        events = [
            candidate(
                "QUOTE",
                portfolio="market",
                event_id="market-aapl-2024-01-02",
                occurred_at="2024-01-02T21:00:00Z",
                symbol="AAPL",
                close="100",
                session_date="2024-01-02",
            ),
            candidate(
                "QUOTE",
                portfolio="market",
                event_id="market-msft-2024-01-02",
                occurred_at="2024-01-02T21:00:01Z",
                symbol="MSFT",
                close="200",
                session_date="2024-01-02",
            ),
            candidate(
                "BENCHMARK_CLOSE",
                portfolio="market",
                event_id="market-spy-benchmark-2024-01-02",
                occurred_at="2024-01-02T21:00:02Z",
                symbol="SPY",
                close="470",
                session_date="2024-01-02",
            ),
        ]

        result = self.store.append_many(events)

        self.assertEqual(result["status"], "appended")
        self.assertEqual(result["appended"], 3)
        self.assertEqual(result["duplicates"], 0)
        self.assertEqual(
            [item["ledger_seq"] for item in self.store.read("market")],
            [1, 2, 3],
        )
        marker = json.loads(
            (self.root / "state" / "rebuild.pending").read_text()
        )
        self.assertEqual(marker["requested_by"], "ledger-append-batch")
        self.assertEqual(marker["event_count"], 3)
        self.assertEqual(marker["portfolios"], ["market"])

        (self.root / "state" / "rebuild.pending").unlink()
        retry = self.store.append_many(events)
        self.assertEqual(retry["status"], "duplicate")
        self.assertEqual(retry["appended"], 0)
        self.assertEqual(retry["duplicates"], 3)
        self.assertFalse((self.root / "state" / "rebuild.pending").exists())

    def test_partial_batch_never_rebuilds_until_stable_retry_completes_it(
        self,
    ) -> None:
        events = [
            candidate(
                "QUOTE",
                portfolio="market",
                event_id="market-aapl-2024-01-02",
                occurred_at="2024-01-02T21:00:00Z",
                symbol="AAPL",
                close="100",
                session_date="2024-01-02",
            ),
            candidate(
                "QUOTE",
                portfolio="market",
                event_id="market-msft-2024-01-02",
                occurred_at="2024-01-02T21:00:01Z",
                symbol="MSFT",
                close="200",
                session_date="2024-01-02",
            ),
            candidate(
                "BENCHMARK_CLOSE",
                portfolio="market",
                event_id="market-spy-benchmark-2024-01-02",
                occurred_at="2024-01-02T21:00:02Z",
                symbol="SPY",
                close="470",
                session_date="2024-01-02",
            ),
        ]
        real_append = ledger_module._append_json_line
        call_count = 0

        def fail_third_write(path: Path, event: dict[str, object]) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise OSError("simulated batch append interruption")
            real_append(path, event)

        with patch(
            "portfolio_tracker.ledger._append_json_line",
            side_effect=fail_third_write,
        ):
            with self.assertRaisesRegex(OSError, "append interruption"):
                self.store.append_many(events)

        self.assertEqual(len(self.store.read("market")), 2)
        with self.assertRaisesRegex(
            BusinessInvariantError,
            "incomplete ledger batch",
        ):
            build_snapshot(self.root)
        self.assertFalse(
            (self.root / "snapshots" / "portfolio-snapshot.json").exists()
        )

        with patch(
            "portfolio_tracker.ledger._append_json_line",
            side_effect=OSError("simulated lone retry interruption"),
        ):
            with self.assertRaisesRegex(OSError, "lone retry interruption"):
                self.store.append_many(events)
        marker = json.loads(
            (self.root / "state" / "rebuild.pending").read_text()
        )
        self.assertEqual(marker["requested_by"], "ledger-append-batch")
        self.assertEqual(
            marker["event_ids"],
            ["market-spy-benchmark-2024-01-02"],
        )
        with self.assertRaisesRegex(
            BusinessInvariantError,
            "incomplete ledger batch",
        ):
            build_snapshot(self.root)

        retry = self.store.append_many(events)
        self.assertEqual(retry["status"], "mixed")
        self.assertEqual(retry["appended"], 1)
        self.assertEqual(retry["duplicates"], 2)
        snapshot = build_snapshot(self.root)
        self.assertEqual(snapshot["revision"], 3)
        self.assertEqual(len(self.store.read("market")), 3)
        self.assertFalse((self.root / "state" / "rebuild.pending").exists())

    def test_numeric_inputs_are_stored_as_decimal_strings(self) -> None:
        numeric_open = dict(self.open, initial_cash=1000.0)
        self.store.append(numeric_open)
        numeric_buy = candidate(
            "BUY",
            portfolio="paper",
            event_id="paper-buy-numeric",
            occurred_at="2024-01-02T15:00:00Z",
            symbol="AAPL",
            shares=1.25,
            price=100.125,
            fee=0.5,
        )
        self.store.append(numeric_buy)
        stored = self.store.read("paper")
        self.assertEqual(stored[0]["initial_cash"], "1000.0")
        self.assertEqual(stored[1]["shares"], "1.25")
        self.assertEqual(stored[1]["price"], "100.125")
        self.assertEqual(stored[1]["fee"], "0.5")

        retry = dict(
            numeric_buy,
            shares="1.25",
            price="100.125",
            fee="0.5",
        )
        self.assertEqual(self.store.append(retry)["status"], "duplicate")

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
        marker = json.loads(
            (self.root / "state" / "rebuild.pending").read_text()
        )
        self.assertEqual(marker["requested_by"], "ledger-tail-repair")
        self.assertEqual(marker["portfolios"], ["paper"])

    def test_tail_repair_marks_rebuild_even_when_retry_is_duplicate(self) -> None:
        self.store.append(self.open)
        path = self.store.path_for("paper")
        with path.open("ab") as handle:
            handle.write(b'{"event_id":')
        (self.root / "state" / "rebuild.pending").unlink()

        result = self.store.append(self.open)

        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(len(result["ledger_repairs"]), 1)
        marker = json.loads(
            (self.root / "state" / "rebuild.pending").read_text()
        )
        self.assertEqual(marker["requested_by"], "ledger-tail-repair")

    def test_complete_json_without_newline_is_quarantined_before_retry(self) -> None:
        self.store.append(self.open)
        buy = candidate(
            "BUY",
            portfolio="paper",
            event_id="paper-buy-retry",
            occurred_at="2024-01-02T15:00:00Z",
            symbol="AAPL",
            shares="1",
            price="100",
            fee="0",
        )
        incomplete_record = json.dumps(
            {**buy, "ledger_seq": 2},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        with self.store.path_for("paper").open("ab") as handle:
            handle.write(incomplete_record)

        result = self.store.append(buy)

        self.assertEqual(result["status"], "appended")
        self.assertEqual(result["ledger_repairs"][0]["bytes_quarantined"], len(incomplete_record))
        self.assertEqual(
            [item["event_id"] for item in self.store.read("paper")],
            ["paper-open", "paper-buy-retry"],
        )
        quarantine = next((self.root / "quarantine").glob("*.quarantine"))
        self.assertEqual(quarantine.read_bytes(), incomplete_record)

    def test_complete_corrupt_record_is_never_auto_repaired(self) -> None:
        self.store.append(self.open)
        path = self.store.path_for("paper")
        before = path.read_bytes() + b'{"broken":}\n'
        path.write_bytes(before)
        cash = candidate(
            "CASH_FLOW",
            portfolio="paper",
            event_id="paper-cash-after-corruption",
            occurred_at="2024-01-02T15:00:00Z",
            amount="1",
        )
        with self.assertRaises(LedgerCorruptionError):
            self.store.append(cash)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / "quarantine").exists())

    def test_blank_jsonl_record_is_corruption_not_an_ignored_event(self) -> None:
        self.store.append(self.open)
        path = self.store.path_for("paper")
        before = path.read_bytes() + b"\n"
        path.write_bytes(before)

        with self.assertRaisesRegex(
            LedgerCorruptionError,
            "empty JSONL record",
        ):
            read_jsonl(
                path,
                repair_tail=True,
                backup_dir=self.root / "backups",
                quarantine_dir=self.root / "quarantine",
            )

        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / "quarantine").exists())

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
