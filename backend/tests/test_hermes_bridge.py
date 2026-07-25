from __future__ import annotations

import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from integrations.hermes_bridge import (
    append_and_rebuild,
    base_event,
    parse_trade_command,
    parser,
)
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

    def test_source_and_created_at_are_stable_source_metadata(self) -> None:
        args = parser().parse_args(
            [
                "--root",
                "/tmp/test",
                "trade",
                "--portfolio",
                "paper",
                "--event-id",
                "paper-source-1",
                "--occurred-at",
                "2024-01-02T15:00:00Z",
                "--created-at",
                "2024-01-02T15:01:00Z",
                "--action",
                "BUY",
                "--symbol",
                "AAPL",
                "--shares",
                "1",
                "--price",
                "100",
                "--source",
                "swing-trader",
            ]
        )
        event = base_event(args, "BUY")
        self.assertEqual(event["source"], "swing-trader")
        self.assertEqual(event["created_at"], "2024-01-02T15:01:00Z")

    def test_correction_commands_require_explicit_audit_reasons(self) -> None:
        amend = parser().parse_args(
            [
                "--root",
                "/tmp/test",
                "amend",
                "--portfolio",
                "live",
                "--event-id",
                "live-amend-1",
                "--occurred-at",
                "2024-01-02T15:00:00Z",
                "--target",
                "live-buy-1",
                "--fee",
                "1",
                "--amend-reason",
                "broker fee correction",
            ]
        )
        void = parser().parse_args(
            [
                "--root",
                "/tmp/test",
                "void",
                "--portfolio",
                "live",
                "--event-id",
                "live-void-1",
                "--occurred-at",
                "2024-01-02T15:00:00Z",
                "--target",
                "live-buy-1",
                "--void-reason",
                "duplicate fill",
            ]
        )
        self.assertEqual(amend.amend_reason, "broker fee correction")
        self.assertEqual(void.void_reason, "duplicate fill")

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

    def test_paper_and_live_writes_remain_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for portfolio, initial_cash in (("paper", "100000"), ("live", "50000")):
                append_and_rebuild(
                    root,
                    candidate(
                        "PORTFOLIO_OPEN",
                        portfolio=portfolio,
                        event_id=f"{portfolio}-open",
                        occurred_at="2024-01-02T14:00:00Z",
                        initial_cash=initial_cash,
                    ),
                )
            append_and_rebuild(
                root,
                candidate(
                    "BUY",
                    portfolio="paper",
                    event_id="paper-buy-aapl",
                    occurred_at="2024-01-02T15:00:00Z",
                    symbol="AAPL",
                    shares="2",
                    price="100",
                    fee="0",
                ),
            )
            append_and_rebuild(
                root,
                candidate(
                    "BUY",
                    portfolio="live",
                    event_id="live-buy-msft",
                    occurred_at="2024-01-02T15:30:00Z",
                    symbol="MSFT",
                    shares="3",
                    price="100",
                    fee="0",
                ),
            )

            snapshot = json.loads(
                (root / "snapshots" / "portfolio-snapshot.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                snapshot["portfolios"]["paper"]["holdings"][0]["symbol"],
                "AAPL",
            )
            self.assertEqual(
                snapshot["portfolios"]["live"]["holdings"][0]["symbol"],
                "MSFT",
            )
            self.assertEqual(
                [event["event_id"] for event in LedgerStore(root).read("paper")],
                ["paper-open", "paper-buy-aapl"],
            )
            self.assertEqual(
                [event["event_id"] for event in LedgerStore(root).read("live")],
                ["live-open", "live-buy-msft"],
            )


if __name__ == "__main__":
    unittest.main()
