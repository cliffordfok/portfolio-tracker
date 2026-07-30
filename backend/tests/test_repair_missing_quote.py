from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "repair_missing_quote.py"
)
SPEC = importlib.util.spec_from_file_location(
    "repair_missing_quote",
    SCRIPT_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load repair_missing_quote.py")
REPAIR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = REPAIR
SPEC.loader.exec_module(REPAIR)


class MissingQuoteRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "ledger").mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_market(self, events: list[dict[str, object]]) -> None:
        payload = "".join(
            json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
            for event in events
        )
        (self.root / "ledger" / "market.jsonl").write_text(
            payload,
            encoding="utf-8",
        )

    def benchmark(self, close: str = "555") -> dict[str, object]:
        return {
            "event_id": "market-spy-benchmark-2024-01-02",
            "portfolio": "market",
            "occurred_at": "2024-01-02T21:00:00Z",
            "created_at": "2024-01-02T21:00:00Z",
            "source": "cron-benchmark",
            "action": "BENCHMARK_CLOSE",
            "symbol": "SPY",
            "close": close,
            "session_date": "2024-01-02",
            "ledger_seq": 1,
        }

    def plan(self, close: str = "555") -> object:
        return REPAIR.build_plan(
            self.root,
            symbol="spy",
            session_date="2024-01-02",
            close=close,
            now=datetime(2024, 1, 3, 12, tzinfo=UTC),
        )

    def test_check_only_identifies_one_pending_regular_spy_quote(self) -> None:
        self.write_market([self.benchmark()])
        plan = self.plan()
        result = REPAIR.execute(self.root, plan, apply=False)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["benchmark_close"], "555")
        self.assertEqual(plan.event["action"], "QUOTE")
        self.assertEqual(plan.event["source"], "manual-quote")
        self.assertEqual(
            plan.event["event_id"],
            "market-manual-quote-spy-2024-01-02",
        )
        self.assertEqual(plan.event["occurred_at"], "2024-01-02T21:00:00Z")
        self.assertEqual(
            (self.root / "ledger" / "market.jsonl").read_text(encoding="utf-8"),
            json.dumps(
                self.benchmark(),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )

    def test_current_or_future_session_is_rejected(self) -> None:
        self.write_market([])
        with self.assertRaisesRegex(
            REPAIR.MissingQuoteError,
            "current or a future",
        ):
            REPAIR.build_plan(
                self.root,
                symbol="SPY",
                session_date="2024-01-03",
                close="555",
                now=datetime(2024, 1, 3, 12, tzinfo=UTC),
            )

    def test_non_session_is_rejected(self) -> None:
        self.write_market([])
        with self.assertRaisesRegex(
            REPAIR.MissingQuoteError,
            "not an NYSE session",
        ):
            REPAIR.build_plan(
                self.root,
                symbol="SPY",
                session_date="2024-01-01",
                close="555",
                now=datetime(2024, 1, 3, 12, tzinfo=UTC),
            )

    def test_early_close_uses_official_1300_new_york_timestamp(self) -> None:
        self.write_market([])
        plan = REPAIR.build_plan(
            self.root,
            symbol="SPY",
            session_date="2026-11-27",
            close="555",
            now=datetime(2026, 11, 28, 12, tzinfo=UTC),
        )
        self.assertEqual(plan.event["occurred_at"], "2026-11-27T18:00:00Z")

    def test_different_existing_close_is_rejected(self) -> None:
        quote = {
            **self.benchmark("554"),
            "event_id": "market-spy-quote-2024-01-02",
            "source": "cron-quote",
            "action": "QUOTE",
        }
        self.write_market([quote])
        with self.assertRaisesRegex(
            REPAIR.MissingQuoteError,
            "different close",
        ):
            self.plan("555")

    def test_same_existing_close_is_current_without_backup(self) -> None:
        quote = {
            **self.benchmark(),
            "event_id": "market-spy-quote-2024-01-02",
            "source": "cron-quote",
            "action": "QUOTE",
        }
        self.write_market([quote])
        result = REPAIR.execute(self.root, self.plan(), apply=True)
        self.assertEqual(result["status"], "current")
        self.assertEqual(result["pending"], 0)
        self.assertIsNone(result["backup_id"])
        self.assertFalse((self.root / "backups").exists())

    def test_apply_backs_up_then_appends_through_real_bridge(self) -> None:
        self.write_market([self.benchmark()])
        result = REPAIR.execute(self.root, self.plan(), apply=True)
        self.assertEqual(result["status"], "corrected")
        self.assertEqual(result["pending"], 0)
        self.assertIsNotNone(result["backup_id"])
        backup = self.root / "backups" / result["backup_id"]
        self.assertTrue((backup / "manifest.json").is_file())
        events = [
            json.loads(line)
            for line in (
                self.root / "ledger" / "market.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]["action"], "QUOTE")
        self.assertEqual(events[-1]["symbol"], "SPY")
        self.assertEqual(events[-1]["close"], "555")

        retry_plan = self.plan()
        retry = REPAIR.execute(self.root, retry_plan, apply=True)
        self.assertEqual(retry["status"], "current")
        self.assertEqual(
            len(list((self.root / "backups").iterdir())),
            1,
        )


if __name__ == "__main__":
    unittest.main()
