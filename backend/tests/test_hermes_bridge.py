from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from integrations.hermes_bridge import append_and_rebuild, parse_trade_command
from portfolio_tracker.ledger import LedgerStore

from .helpers import candidate


class HermesBridgeTests(unittest.TestCase):
    def test_parses_documented_telegram_trade_command(self) -> None:
        parsed = parse_trade_command(
            "/trade BUY AAPL 10 @ 180.50 fee:1.50 note:earnings play"
        )
        self.assertEqual(
            parsed,
            {
                "action": "BUY",
                "symbol": "AAPL",
                "shares": "10",
                "price": "180.50",
                "fee": "1.50",
                "note": "earnings play",
            },
        )

    def test_trade_command_supports_class_share_symbol(self) -> None:
        parsed = parse_trade_command("/trade sell brk.b 0.5 @ 400")
        self.assertEqual(parsed["action"], "SELL")
        self.assertEqual(parsed["symbol"], "BRK.B")
        self.assertEqual(parsed["fee"], "0")

    def test_invalid_trade_command_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "must use"):
            parse_trade_command("/trade BUY AAPL ten @ 180")

    def test_append_reports_recorded_when_snapshot_rebuild_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            opening = candidate(
                "PORTFOLIO_OPEN",
                portfolio="paper",
                event_id="paper-open",
                occurred_at="2024-01-01T14:00:00Z",
                initial_cash="1000",
            )
            with patch(
                "integrations.hermes_bridge.build_snapshot",
                side_effect=OSError("simulated replace failure"),
            ):
                result = append_and_rebuild(root, opening)
            self.assertEqual(result["status"], "recorded_but_rebuild_pending")
            self.assertEqual(result["snapshot_status"], "rebuild_pending")
            self.assertEqual(len(LedgerStore(root).read("paper")), 1)
            self.assertTrue((root / "state" / "rebuild.pending").exists())


if __name__ == "__main__":
    unittest.main()
