from __future__ import annotations

import json
import unittest
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from integrations.hermes_bridge import (
    _rebuild_after_write,
    append_and_rebuild,
    append_quote_batch_and_rebuild,
    base_event,
    main,
    parse_trade_command,
    parser,
    quote_batch_events,
)
from portfolio_tracker.ledger import LedgerStore
from portfolio_tracker.snapshot import build_snapshot, build_snapshot_if_needed

from .helpers import candidate


class HermesBridgeTests(unittest.TestCase):
    def quote_batch(self) -> list[dict[str, object]]:
        return [
            {
                "event_id": "market-aapl-2024-01-02",
                "occurred_at": "2024-01-02T21:00:00Z",
                "session_date": "2024-01-02",
                "symbol": "aapl",
                "close": "110",
            },
            {
                "event_id": "market-msft-2024-01-02",
                "occurred_at": "2024-01-02T21:00:01Z",
                "session_date": "2024-01-02",
                "symbol": "MSFT",
                "close": "210",
            },
            {
                "event_id": "market-spy-benchmark-2024-01-02",
                "occurred_at": "2024-01-02T21:00:02Z",
                "session_date": "2024-01-02",
                "symbol": "SPY",
                "close": "470",
                "benchmark": True,
            },
        ]

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
                "integrations.hermes_bridge.build_snapshot_if_needed",
                side_effect=OSError("simulated replace failure"),
            ):
                result = append_and_rebuild(root, opening)
            self.assertEqual(result["status"], "recorded_but_rebuild_pending")
            self.assertEqual(result["snapshot_status"], "rebuild_pending")
            self.assertEqual(len(LedgerStore(root).read("paper")), 1)
            self.assertTrue((root / "state" / "rebuild.pending").exists())

    def test_systemd_preempted_rebuild_reuses_current_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            LedgerStore(root).append(
                candidate(
                    "PORTFOLIO_OPEN",
                    portfolio="paper",
                    event_id="paper-open",
                    occurred_at="2024-01-01T14:00:00Z",
                    initial_cash="1000",
                )
            )
            systemd_snapshot = build_snapshot(root)
            target = root / "snapshots" / "portfolio-snapshot.json"
            snapshot_bytes = target.read_bytes()

            result = _rebuild_after_write(
                root,
                {"status": "appended"},
                has_new_events=True,
                requested_by="hermes-bridge",
            )

            self.assertEqual(result["snapshot_status"], "current")
            self.assertEqual(
                result["snapshot_revision"],
                systemd_snapshot["revision"],
            )
            self.assertEqual(target.read_bytes(), snapshot_bytes)
            publish_marker = json.loads(
                (root / "state" / "publish.pending").read_text()
            )
            self.assertEqual(
                publish_marker,
                {
                    "revision": systemd_snapshot["revision"],
                    "requested_by": "hermes-bridge",
                },
            )

    def test_quote_batch_requires_one_session_and_one_benchmark(self) -> None:
        events = quote_batch_events(self.quote_batch())
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["symbol"], "AAPL")
        self.assertEqual(events[0]["source"], "cron-quote")
        self.assertEqual(events[2]["source"], "cron-benchmark")

        without_benchmark = self.quote_batch()[:-1]
        with self.assertRaisesRegex(ValueError, "exactly one benchmark"):
            quote_batch_events(without_benchmark)

        multiple_sessions = self.quote_batch()
        multiple_sessions[1]["session_date"] = "2024-01-03"
        with self.assertRaisesRegex(ValueError, "one session_date"):
            quote_batch_events(multiple_sessions)

    def test_quote_batch_rebuilds_once_after_all_quotes_are_durable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = LedgerStore(root)
            for portfolio, symbol, price in (
                ("paper", "AAPL", "100"),
                ("live", "MSFT", "200"),
            ):
                store.append(
                    candidate(
                        "PORTFOLIO_OPEN",
                        portfolio=portfolio,
                        event_id=f"{portfolio}-open",
                        occurred_at="2024-01-02T14:00:00Z",
                        initial_cash="1000",
                    )
                )
                store.append(
                    candidate(
                        "BUY",
                        portfolio=portfolio,
                        event_id=f"{portfolio}-buy-{symbol.lower()}",
                        occurred_at="2024-01-02T15:00:00Z",
                        symbol=symbol,
                        shares="1",
                        price=price,
                        fee="0",
                    )
                )

            events = quote_batch_events(self.quote_batch())
            with patch(
                "integrations.hermes_bridge.build_snapshot_if_needed",
                wraps=build_snapshot_if_needed,
            ) as rebuild:
                result = append_quote_batch_and_rebuild(root, events)

            self.assertEqual(result["status"], "appended")
            self.assertEqual(result["appended"], 3)
            self.assertEqual(result["snapshot_status"], "rebuilt")
            self.assertEqual(rebuild.call_count, 1)
            self.assertFalse((root / "state" / "rebuild.pending").exists())
            publish_marker = json.loads(
                (root / "state" / "publish.pending").read_text()
            )
            self.assertEqual(
                publish_marker["requested_by"],
                "hermes-quote-batch",
            )
            snapshot = json.loads(
                (root / "snapshots" / "portfolio-snapshot.json").read_text()
            )
            self.assertEqual(
                snapshot["portfolios"]["paper"]["daily"][-1]["data_status"],
                "OK",
            )
            self.assertEqual(
                snapshot["portfolios"]["live"]["daily"][-1]["data_status"],
                "OK",
            )
            self.assertEqual(len(store.read("market")), 3)

            retry = append_quote_batch_and_rebuild(root, events)
            self.assertEqual(retry["status"], "duplicate")
            self.assertEqual(retry["appended"], 0)
            self.assertNotIn("snapshot_status", retry)
            self.assertEqual(len(store.read("market")), 3)

    def test_quote_batch_cli_accepts_stdin_without_provider_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = StringIO()
            with (
                patch("sys.stdin", StringIO(json.dumps(self.quote_batch()))),
                redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "--root",
                        temp,
                        "quote-batch",
                        "--file",
                        "-",
                    ]
                )

            self.assertEqual(exit_code, 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "appended")
            self.assertEqual(result["appended"], 3)
            self.assertEqual(result["snapshot_status"], "rebuilt")
            root = Path(temp)
            self.assertEqual(len(LedgerStore(root).read("market")), 3)
            self.assertTrue(
                (root / "snapshots" / "portfolio-snapshot.json").exists()
            )

    def test_read_rebuilds_a_stale_snapshot_and_requests_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = LedgerStore(root)
            store.append(
                candidate(
                    "PORTFOLIO_OPEN",
                    portfolio="paper",
                    event_id="paper-open",
                    occurred_at="2024-01-02T14:00:00Z",
                    initial_cash="1000",
                )
            )
            build_snapshot(root)
            store.append(
                candidate(
                    "CASH_FLOW",
                    portfolio="paper",
                    event_id="paper-deposit",
                    occurred_at="2024-01-02T15:00:00Z",
                    amount="25",
                )
            )

            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "--root",
                        temp,
                        "read",
                        "--portfolio",
                        "paper",
                    ]
                )

            self.assertEqual(exit_code, 0)
            portfolio = json.loads(output.getvalue())
            self.assertEqual(portfolio["cash"], "1025")
            marker = json.loads(
                (root / "state" / "publish.pending").read_text()
            )
            self.assertEqual(
                marker["requested_by"],
                "hermes-read-recovery",
            )
            self.assertFalse((root / "state" / "rebuild.pending").exists())

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
