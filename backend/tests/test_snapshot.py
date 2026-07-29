from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from portfolio_tracker.cli import main as cli_main
from portfolio_tracker.errors import ValidationError
from portfolio_tracker.ledger import LedgerStore, atomic_write_json
from portfolio_tracker.snapshot import (
    _metrics,
    _session_for_event,
    build_snapshot,
    build_snapshot_if_needed,
    is_nyse_session,
    validate_snapshot,
)

from .helpers import candidate


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = LedgerStore(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def append(self, action: str, event_id: str, occurred_at: str, **fields: str) -> None:
        portfolio = "market" if action in {"QUOTE", "BENCHMARK_CLOSE"} else "paper"
        self.store.append(
            candidate(
                action,
                portfolio=portfolio,
                event_id=event_id,
                occurred_at=occurred_at,
                **fields,
            )
        )

    def test_clean_start_builds_valid_empty_snapshot(self) -> None:
        snapshot = build_snapshot(self.root)
        self.assertEqual(snapshot["schema_version"], 4)
        self.assertEqual(snapshot["revision"], 0)
        self.assertEqual(snapshot["portfolios"]["paper"]["data_status"], "NO_DATA")
        self.assertEqual(snapshot["portfolios"]["live"]["holdings"], [])
        self.assertEqual(snapshot["benchmark"]["daily"], [])
        self.assertIsNone(
            snapshot["portfolios"]["paper"]["metrics"][
                "performance_effective_date"
            ]
        )
        self.assertIsNone(
            snapshot["portfolios"]["paper"]["metrics"]["performance_scope"]
        )
        self.assertTrue(
            (self.root / "snapshots" / "portfolio-snapshot.json").exists()
        )

    def test_settlement_adjustment_reaches_snapshot_cash_and_pnl(self) -> None:
        self.store.append(
            candidate(
                "PORTFOLIO_OPEN",
                portfolio="live",
                event_id="live-open",
                occurred_at="2024-01-01T14:00:00Z",
                initial_cash="5000",
            )
        )
        self.store.append(
            candidate(
                "BUY",
                portfolio="live",
                event_id="live-buy-onds",
                occurred_at="2024-01-02T15:00:00Z",
                symbol="ONDS",
                instrument_id="EQUITY:ONDS",
                shares="150",
                price="10",
                fee="0",
            )
        )
        self.store.append(
            candidate(
                "SELL",
                portfolio="live",
                event_id="live-sell-onds",
                occurred_at="2024-01-03T15:00:00Z",
                symbol="ONDS",
                instrument_id="EQUITY:ONDS",
                shares="150",
                price="11.0001",
                fee="0.03",
                settlement_adjustment="-0.005",
            )
        )

        snapshot = build_snapshot(self.root, write=False)
        live = snapshot["portfolios"]["live"]
        self.assertEqual(live["cash"], "5149.98")
        self.assertEqual(live["metrics"]["realized_pnl"], "149.98")
        self.assertEqual(
            live["recent_trades"][0]["settlement_adjustment"],
            "-0.005",
        )

    def test_missing_quote_breaks_global_return_chain(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-01T14:00:00Z",
            initial_cash="1000",
        )
        self.append(
            "BUY",
            "paper-buy",
            "2024-01-02T15:00:00Z",
            symbol="AAPL",
            shares="1",
            price="100",
            fee="0",
        )
        for day, close in (
            ("2024-01-02", "470"),
            ("2024-01-03", "471"),
            ("2024-01-04", "472"),
            ("2024-01-05", "473"),
        ):
            self.append(
                "BENCHMARK_CLOSE",
                f"market-spy-{day}",
                f"{day}T21:00:00Z",
                symbol="SPY",
                close=close,
                session_date=day,
            )
        for day, close in (
            ("2024-01-02", "100"),
            ("2024-01-04", "105"),
            ("2024-01-05", "106"),
        ):
            self.append(
                "QUOTE",
                f"market-aapl-{day}",
                f"{day}T21:00:00Z",
                symbol="AAPL",
                close=close,
                session_date=day,
            )

        snapshot = build_snapshot(self.root, write=False)
        daily = snapshot["portfolios"]["paper"]["daily"]
        self.assertEqual(daily[0]["segment_id"], 1)
        self.assertEqual(daily[0]["daily_return"], "0")
        self.assertEqual(daily[1]["data_status"], "INSUFFICIENT_MARKET_DATA")
        self.assertIsNone(daily[1]["nav"])
        self.assertEqual(daily[2]["segment_id"], 2)
        self.assertIsNone(daily[2]["daily_return"])
        self.assertIsNone(daily[2]["cumulative_return"])
        self.assertEqual(daily[2]["segment_return"], "0")
        self.assertIsNotNone(daily[3]["daily_return"])
        self.assertIsNone(daily[3]["cumulative_return"])
        metrics = snapshot["portfolios"]["paper"]["metrics"]
        self.assertEqual(
            metrics["data_status"],
            "OK",
        )
        self.assertEqual(
            metrics["performance_effective_date"],
            "2024-01-04",
        )
        self.assertEqual(
            metrics["performance_scope"],
            "LATEST_COMPLETE_SEGMENT",
        )
        self.assertEqual(metrics["total_return"], daily[3]["segment_return"])
        self.assertTrue(
            any(
                "paper performance starts at 2024-01-04"
                in warning
                for warning in snapshot["warnings"]
            )
        )

    def test_voided_event_does_not_extend_the_performance_calendar(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-02T14:00:00Z",
            initial_cash="1000",
        )
        self.append(
            "BUY",
            "paper-buy-aapl",
            "2024-01-02T15:00:00Z",
            symbol="AAPL",
            shares="1",
            price="100",
            fee="0",
        )
        self.append(
            "BENCHMARK_CLOSE",
            "market-spy-2024-01-02",
            "2024-01-02T21:00:00Z",
            symbol="SPY",
            close="470",
            session_date="2024-01-02",
        )
        self.append(
            "QUOTE",
            "market-aapl-2024-01-02",
            "2024-01-02T21:00:00Z",
            symbol="AAPL",
            close="100",
            session_date="2024-01-02",
        )
        self.append(
            "BUY",
            "paper-buy-msft-voided",
            "2024-01-03T15:00:00Z",
            symbol="MSFT",
            shares="1",
            price="100",
            fee="0",
        )
        self.append(
            "VOID",
            "paper-void-msft",
            "2024-01-04T15:00:00Z",
            void_target="paper-buy-msft-voided",
        )

        snapshot = build_snapshot(self.root, write=False)
        self.assertEqual(
            [
                point["date"]
                for point in snapshot["portfolios"]["paper"]["daily"]
            ],
            ["2024-01-02"],
        )

    def test_rebuild_repairs_incomplete_tail_and_surfaces_warning(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-02T14:00:00Z",
            initial_cash="1000",
        )
        path = self.store.path_for("paper")
        with path.open("ab") as handle:
            handle.write(b'{"event_id":')

        snapshot = build_snapshot(self.root)

        self.assertTrue(
            any(
                "paper ledger tail repaired" in warning
                for warning in snapshot["warnings"]
            )
        )
        self.assertEqual(len(self.store.read("paper")), 1)
        self.assertEqual(len(list((self.root / "backups").glob("*.bak"))), 1)
        self.assertEqual(
            len(list((self.root / "quarantine").glob("*.quarantine"))),
            1,
        )

    def test_successful_snapshot_clears_rebuild_marker(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-01T14:00:00Z",
            initial_cash="1000",
        )
        marker = self.root / "state" / "rebuild.pending"
        self.assertTrue(marker.exists())
        snapshot = build_snapshot(self.root)
        self.assertFalse(marker.exists())
        self.assertEqual(
            snapshot["portfolios"]["paper"]["metrics"]["data_status"],
            "OK",
        )
        self.assertEqual(
            snapshot["portfolios"]["paper"]["metrics"]["total_return"],
            "0",
        )

    def test_after_hours_trade_maps_to_next_nyse_session(self) -> None:
        sessions = ["2024-01-02", "2024-01-03", "2024-01-04"]
        event = candidate(
            "BUY",
            portfolio="paper",
            event_id="paper-after-hours",
            occurred_at="2024-01-02T22:00:00Z",
            symbol="AAPL",
            shares="1",
            price="100",
        )
        self.assertEqual(_session_for_event(event, sessions), "2024-01-03")

    def test_future_paper_session_does_not_extend_live_or_benchmark(self) -> None:
        for portfolio in ("paper", "live"):
            self.store.append(
                candidate(
                    "PORTFOLIO_OPEN",
                    portfolio=portfolio,
                    event_id=f"{portfolio}-open",
                    occurred_at="2024-01-02T14:00:00Z",
                    initial_cash="1000",
                )
            )
            self.store.append(
                candidate(
                    "BUY",
                    portfolio=portfolio,
                    event_id=f"{portfolio}-buy-aapl",
                    occurred_at="2024-01-02T15:00:00Z",
                    symbol="AAPL",
                    shares="1",
                    price="100",
                    fee="0",
                )
            )
        self.store.append(
            candidate(
                "BUY",
                portfolio="paper",
                event_id="paper-after-hours-msft",
                occurred_at="2024-01-02T22:00:00Z",
                symbol="MSFT",
                shares="1",
                price="200",
                fee="0",
            )
        )
        for action, symbol, close in (
            ("QUOTE", "AAPL", "110"),
            ("BENCHMARK_CLOSE", "SPY", "470"),
        ):
            self.store.append(
                candidate(
                    action,
                    portfolio="market",
                    event_id=f"market-{symbol.lower()}-2024-01-02",
                    occurred_at="2024-01-02T21:00:00Z",
                    symbol=symbol,
                    close=close,
                    session_date="2024-01-02",
                )
            )

        snapshot = build_snapshot(self.root, write=False)

        self.assertEqual(
            snapshot["portfolios"]["live"]["daily"][-1]["date"],
            "2024-01-02",
        )
        self.assertEqual(
            snapshot["portfolios"]["live"]["daily"][-1]["data_status"],
            "OK",
        )
        self.assertEqual(
            snapshot["portfolios"]["live"]["holdings"][0]["quote_status"],
            "OK",
        )
        self.assertEqual(
            snapshot["portfolios"]["paper"]["daily"][-1]["date"],
            "2024-01-03",
        )
        self.assertEqual(
            snapshot["portfolios"]["paper"]["daily"][-1]["data_status"],
            "INSUFFICIENT_MARKET_DATA",
        )
        self.assertEqual(
            snapshot["benchmark"]["daily"][-1]["date"],
            "2024-01-02",
        )

    def test_legacy_option_never_uses_underlying_equity_quote(self) -> None:
        self.store.append(
            candidate(
                "PORTFOLIO_OPEN",
                portfolio="live",
                event_id="live-open",
                occurred_at="2024-01-02T14:00:00Z",
                initial_cash="10000",
            )
        )
        self.store.append(
            candidate(
                "BUY",
                portfolio="live",
                event_id="live-buy-option",
                occurred_at="2024-01-02T15:00:00Z",
                symbol="NVDA",
                instrument_id="OPTION:NVDA:2024-01-19:C:500",
                instrument_type="OPTION",
                quote_symbol="NVDA",
                contract_multiplier="100",
                shares="1",
                price="5",
                fee="0",
            )
        )
        for instrument_id, close, suffix in (
            (None, "500", "underlying"),
            ("OPTION:NVDA:2024-01-19:C:500", "6", "contract"),
        ):
            event = candidate(
                "QUOTE",
                portfolio="market",
                event_id=f"market-nvda-{suffix}",
                occurred_at="2024-01-02T21:00:00Z",
                symbol="NVDA",
                close=close,
                session_date="2024-01-02",
            )
            if instrument_id is not None:
                event["instrument_id"] = instrument_id
            self.store.append(event)
        self.store.append(
            candidate(
                "BENCHMARK_CLOSE",
                portfolio="market",
                event_id="market-spy-2024-01-02",
                occurred_at="2024-01-02T21:00:01Z",
                symbol="SPY",
                close="470",
                session_date="2024-01-02",
            )
        )

        snapshot = build_snapshot(self.root, write=False)
        holding = snapshot["portfolios"]["live"]["holdings"][0]

        self.assertEqual(holding["current_price"], "6")
        self.assertEqual(holding["market_value"], "600")
        self.assertEqual(holding["quote_status"], "OK")
        self.assertEqual(
            snapshot["portfolios"]["live"]["daily"][-1]["nav"],
            "10100",
        )

    def test_legacy_option_without_contract_quote_is_incomplete(self) -> None:
        self.store.append(
            candidate(
                "PORTFOLIO_OPEN",
                portfolio="live",
                event_id="live-open",
                occurred_at="2024-01-02T14:00:00Z",
                initial_cash="10000",
            )
        )
        self.store.append(
            candidate(
                "BUY",
                portfolio="live",
                event_id="live-buy-option",
                occurred_at="2024-01-02T15:00:00Z",
                symbol="NVDA",
                instrument_id="OPTION:NVDA:2024-01-19:C:500",
                instrument_type="OPTION",
                quote_symbol="NVDA",
                contract_multiplier="100",
                shares="1",
                price="5",
                fee="0",
            )
        )
        self.store.append(
            candidate(
                "QUOTE",
                portfolio="market",
                event_id="market-nvda-underlying",
                occurred_at="2024-01-02T21:00:00Z",
                symbol="NVDA",
                close="500",
                session_date="2024-01-02",
            )
        )

        snapshot = build_snapshot(self.root, write=False)
        holding = snapshot["portfolios"]["live"]["holdings"][0]

        self.assertEqual(holding["quote_status"], "MISSING")
        self.assertIsNone(holding["current_price"])
        self.assertEqual(
            snapshot["portfolios"]["live"]["daily"][-1]["data_status"],
            "INSUFFICIENT_MARKET_DATA",
        )

    def test_summer_dst_session_boundary_uses_new_york_time(self) -> None:
        sessions = ["2024-07-01", "2024-07-02"]
        before_close = candidate(
            "BUY",
            portfolio="paper",
            event_id="paper-before-close",
            occurred_at="2024-07-01T19:30:00Z",
            symbol="AAPL",
            shares="1",
            price="100",
        )
        after_close = dict(
            before_close,
            event_id="paper-after-close",
            occurred_at="2024-07-01T20:30:00Z",
        )
        self.assertEqual(_session_for_event(before_close, sessions), "2024-07-01")
        self.assertEqual(_session_for_event(after_close, sessions), "2024-07-02")

    def test_new_year_observed_holiday_is_not_a_session(self) -> None:
        self.assertFalse(is_nyse_session(date(2021, 12, 31)))
        self.assertTrue(is_nyse_session(date(2021, 12, 30)))

    def test_special_exchange_closures_are_not_sessions(self) -> None:
        closures = (
            date(2001, 9, 11),
            date(2001, 9, 12),
            date(2001, 9, 13),
            date(2001, 9, 14),
            date(2004, 6, 11),
            date(2007, 1, 2),
            date(2012, 10, 29),
            date(2012, 10, 30),
            date(2018, 12, 5),
            date(2025, 1, 9),
        )
        for closure in closures:
            with self.subTest(closure=closure):
                self.assertFalse(is_nyse_session(closure))

        self.assertTrue(is_nyse_session(date(2001, 9, 17)))
        self.assertTrue(is_nyse_session(date(2025, 1, 10)))

    def test_weekend_forward_fills_quotes_without_bridging_trading_days(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-05T14:00:00Z",
            initial_cash="1000",
        )
        self.append(
            "BUY",
            "paper-buy",
            "2024-01-05T15:00:00Z",
            symbol="AAPL",
            shares="1",
            price="100",
            fee="0",
        )
        for day, aapl, spy in (
            ("2024-01-05", "100", "470"),
            ("2024-01-08", "110", "475"),
        ):
            self.append(
                "BENCHMARK_CLOSE",
                f"market-spy-{day}",
                f"{day}T21:00:00Z",
                symbol="SPY",
                close=spy,
                session_date=day,
            )
            self.append(
                "QUOTE",
                f"market-aapl-{day}",
                f"{day}T21:00:00Z",
                symbol="AAPL",
                close=aapl,
                session_date=day,
            )

        snapshot = build_snapshot(self.root, write=False)
        daily = snapshot["portfolios"]["paper"]["daily"]
        by_day = {point["date"]: point for point in daily}
        self.assertEqual(by_day["2024-01-06"]["nav"], "1000")
        self.assertEqual(by_day["2024-01-07"]["nav"], "1000")
        self.assertEqual(by_day["2024-01-06"]["data_status"], "OK")
        self.assertEqual(by_day["2024-01-08"]["nav"], "1010")
        benchmark = {
            point["date"]: point for point in snapshot["benchmark"]["daily"]
        }
        self.assertEqual(benchmark["2024-01-06"]["close"], "470")
        self.assertEqual(
            snapshot["portfolios"]["paper"]["holdings"][0][
                "market_price_as_of"
            ],
            "2024-01-08T21:00:00Z",
        )

    def test_trade_after_latest_quote_extends_calendar_and_creates_gap(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-02T14:00:00Z",
            initial_cash="1000",
        )
        self.append(
            "BUY",
            "paper-buy-aapl",
            "2024-01-02T15:00:00Z",
            symbol="AAPL",
            shares="1",
            price="100",
            fee="0",
        )
        self.append(
            "BENCHMARK_CLOSE",
            "market-spy-2024-01-02",
            "2024-01-02T21:00:00Z",
            symbol="SPY",
            close="470",
            session_date="2024-01-02",
        )
        self.append(
            "QUOTE",
            "market-aapl-2024-01-02",
            "2024-01-02T21:00:00Z",
            symbol="AAPL",
            close="100",
            session_date="2024-01-02",
        )
        self.append(
            "BUY",
            "paper-buy-msft",
            "2024-01-03T15:00:00Z",
            symbol="MSFT",
            shares="1",
            price="100",
            fee="0",
        )

        snapshot = build_snapshot(self.root, write=False)
        by_day = {
            point["date"]: point
            for point in snapshot["portfolios"]["paper"]["daily"]
        }
        self.assertIn("2024-01-03", by_day)
        self.assertEqual(
            by_day["2024-01-03"]["data_status"],
            "INSUFFICIENT_MARKET_DATA",
        )
        self.assertCountEqual(
            by_day["2024-01-03"]["missing_symbols"],
            ["AAPL", "MSFT"],
        )
        metrics = snapshot["portfolios"]["paper"]["metrics"]
        self.assertEqual(metrics["data_status"], "INSUFFICIENT_DATA")
        self.assertIsNone(metrics["performance_effective_date"])
        self.assertIsNone(metrics["performance_scope"])
        self.assertIsNone(metrics["total_return"])

    def test_if_needed_rebuild_skips_unchanged_source_heads(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-01T14:00:00Z",
            initial_cash="1000",
        )
        first = build_snapshot(self.root)
        original_bytes = (
            self.root / "snapshots" / "portfolio-snapshot.json"
        ).read_bytes()
        current, rebuilt = build_snapshot_if_needed(self.root)
        self.assertFalse(rebuilt)
        self.assertEqual(current["generated_at"], first["generated_at"])
        self.assertEqual(
            (self.root / "snapshots" / "portfolio-snapshot.json").read_bytes(),
            original_bytes,
        )

        self.append(
            "CASH_FLOW",
            "paper-cash",
            "2024-01-02T14:00:00Z",
            amount="10",
        )
        updated, rebuilt = build_snapshot_if_needed(self.root)
        self.assertTrue(rebuilt)
        self.assertEqual(updated["revision"], first["revision"] + 1)
        self.assertFalse((self.root / "state" / "rebuild.pending").exists())

    def test_if_needed_rebuilds_snapshot_with_nested_corruption(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-01T14:00:00Z",
            initial_cash="1000",
        )
        build_snapshot(self.root)
        path = self.root / "snapshots" / "portfolio-snapshot.json"
        corrupted = json.loads(path.read_text(encoding="utf-8"))
        corrupted["portfolios"]["paper"]["holdings"] = "not-an-array"
        atomic_write_json(path, corrupted)
        marker = self.root / "state" / "rebuild.pending"
        atomic_write_json(marker, {"requested_by": "corruption-test"})

        repaired, rebuilt = build_snapshot_if_needed(self.root)

        self.assertTrue(rebuilt)
        self.assertEqual(repaired["portfolios"]["paper"]["holdings"], [])
        self.assertFalse(marker.exists())
        persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(persisted, repaired)

    def test_cli_append_reuses_a_systemd_preempted_snapshot(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-02T14:00:00Z",
            initial_cash="1000",
        )
        build_snapshot(self.root)
        cash_flow = candidate(
            "CASH_FLOW",
            portfolio="paper",
            event_id="paper-deposit",
            occurred_at="2024-01-02T15:00:00Z",
            amount="25",
        )

        def systemd_wins(root: Path, *, output: str | None = None):
            return build_snapshot(root, output=output), False

        output = StringIO()
        with (
            patch(
                "portfolio_tracker.cli.build_snapshot_if_needed",
                side_effect=systemd_wins,
            ) as rebuild,
            redirect_stdout(output),
        ):
            exit_code = cli_main(
                [
                    "--root",
                    str(self.root),
                    "append",
                    "--json",
                    json.dumps(cash_flow),
                    "--rebuild",
                ]
            )

        self.assertEqual(exit_code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "appended")
        self.assertFalse(result["snapshot_rebuilt"])
        self.assertEqual(result["snapshot_status"], "current")
        self.assertEqual(rebuild.call_count, 1)
        self.assertEqual(result["event"]["event_id"], "paper-deposit")
        self.assertTrue((self.root / "state" / "publish.pending").exists())

    def test_nav_decomposes_exactly_into_cash_and_market_value(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-02T14:00:00Z",
            initial_cash="1000",
        )
        self.append(
            "BUY",
            "paper-buy",
            "2024-01-02T15:00:00Z",
            symbol="AAPL",
            shares="2",
            price="100",
            fee="0",
        )
        self.append(
            "BENCHMARK_CLOSE",
            "market-spy-2024-01-02",
            "2024-01-02T21:00:00Z",
            symbol="SPY",
            close="470",
            session_date="2024-01-02",
        )
        self.append(
            "QUOTE",
            "market-aapl-2024-01-02",
            "2024-01-02T21:00:00Z",
            symbol="AAPL",
            close="110",
            session_date="2024-01-02",
        )
        snapshot = build_snapshot(self.root, write=False)
        point = snapshot["portfolios"]["paper"]["daily"][0]
        holding = snapshot["portfolios"]["paper"]["holdings"][0]
        self.assertEqual(point["cash"], "800")
        self.assertEqual(holding["market_value"], "220")
        self.assertEqual(point["nav"], "1020")

    def test_spy_benchmark_close_is_not_a_holding_quote(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-02T14:00:00Z",
            initial_cash="1000",
        )
        self.append(
            "BUY",
            "paper-buy-spy",
            "2024-01-02T15:00:00Z",
            symbol="SPY",
            shares="1",
            price="470",
            fee="0",
        )
        self.append(
            "BENCHMARK_CLOSE",
            "market-spy-benchmark-2024-01-02",
            "2024-01-02T21:00:00Z",
            symbol="SPY",
            close="471",
            session_date="2024-01-02",
        )

        without_quote = build_snapshot(self.root, write=False)
        paper = without_quote["portfolios"]["paper"]
        self.assertEqual(
            paper["daily"][0]["data_status"],
            "INSUFFICIENT_MARKET_DATA",
        )
        self.assertEqual(paper["daily"][0]["missing_symbols"], ["SPY"])
        self.assertIsNone(paper["holdings"][0]["current_price"])

        self.append(
            "QUOTE",
            "market-spy-quote-2024-01-02",
            "2024-01-02T21:00:01Z",
            symbol="SPY",
            close="471",
            session_date="2024-01-02",
        )
        with_quote = build_snapshot(self.root, write=False)
        paper = with_quote["portfolios"]["paper"]
        self.assertEqual(paper["daily"][0]["data_status"], "OK")
        self.assertEqual(paper["holdings"][0]["current_price"], "471")

    def test_income_split_and_private_manual_quote_reach_snapshot(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open-private",
            "2024-01-02T14:00:00Z",
            initial_cash="1000",
        )
        self.append(
            "BUY",
            "paper-buy-private",
            "2024-01-02T15:00:00Z",
            symbol="ACME",
            instrument_id="PRIVATE:ACME",
            instrument_type="PRIVATE",
            instrument_name="Example Private Company",
            quote_symbol=None,
            shares="2",
            price="100",
            fee="0",
        )
        self.append(
            "INCOME_EXPENSE",
            "paper-income-private",
            "2024-01-02T16:00:00Z",
            symbol="ACME",
            instrument_id="PRIVATE:ACME",
            amount="10",
            gross_amount="10",
            withholding_tax="0",
            income_type="DIVIDEND",
        )
        self.append(
            "SPLIT",
            "paper-split-private",
            "2024-01-02T17:00:00Z",
            symbol="ACME",
            instrument_id="PRIVATE:ACME",
            instrument_type="PRIVATE",
            quote_symbol=None,
            numerator="2",
            denominator="1",
        )
        self.append(
            "QUOTE",
            "market-private-quote",
            "2024-01-02T21:00:00Z",
            source="manual-quote",
            symbol="ACME",
            instrument_id="PRIVATE:ACME",
            close="60",
            session_date="2024-01-02",
        )
        self.append(
            "BENCHMARK_CLOSE",
            "market-private-spy",
            "2024-01-02T21:00:01Z",
            symbol="SPY",
            close="470",
            session_date="2024-01-02",
        )

        snapshot = build_snapshot(self.root, write=False)
        paper = snapshot["portfolios"]["paper"]
        holding = paper["holdings"][0]

        self.assertEqual(holding["shares"], "4")
        self.assertEqual(holding["cost_basis"], "200")
        self.assertEqual(holding["market_value"], "240")
        self.assertEqual(holding["quote_status"], "MANUAL")
        self.assertEqual(paper["cash"], "810")
        self.assertEqual(paper["metrics"]["income_expense"], "10")
        self.assertEqual(paper["daily"][0]["nav"], "1050")
        self.assertEqual(paper["daily"][0]["external_flow"], "0")

    def test_start_of_day_cash_flow_is_removed_from_daily_return(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-02T14:00:00Z",
            initial_cash="1000",
        )
        self.append(
            "CASH_FLOW",
            "paper-deposit",
            "2024-01-03T14:00:00Z",
            amount="100",
        )
        for day, close in (("2024-01-02", "470"), ("2024-01-03", "471")):
            self.append(
                "BENCHMARK_CLOSE",
                f"market-spy-{day}",
                f"{day}T21:00:00Z",
                symbol="SPY",
                close=close,
                session_date=day,
            )
        snapshot = build_snapshot(self.root, write=False)
        daily = snapshot["portfolios"]["paper"]["daily"]
        self.assertEqual(daily[1]["external_flow"], "100")
        self.assertEqual(daily[1]["daily_return"], "0")
        self.assertEqual(daily[1]["nav"], "1100")

    def test_zero_return_denominator_creates_a_new_performance_segment(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-02T14:00:00Z",
            initial_cash="0",
        )
        self.append(
            "CASH_FLOW",
            "paper-funding",
            "2024-01-04T14:00:00Z",
            amount="100",
        )
        for day, close in (
            ("2024-01-02", "470"),
            ("2024-01-03", "471"),
            ("2024-01-04", "472"),
        ):
            self.append(
                "BENCHMARK_CLOSE",
                f"market-spy-{day}",
                f"{day}T21:00:00Z",
                symbol="SPY",
                close=close,
                session_date=day,
            )

        snapshot = build_snapshot(self.root, write=False)
        daily = snapshot["portfolios"]["paper"]["daily"]
        self.assertEqual(daily[0]["daily_return"], "0")
        self.assertEqual(daily[1]["data_status"], "INSUFFICIENT_DATA")
        self.assertIsNone(daily[1]["daily_return"])
        self.assertIsNone(daily[1]["segment_id"])
        self.assertEqual(daily[2]["data_status"], "OK")
        self.assertEqual(daily[2]["segment_id"], 2)
        self.assertIsNone(daily[2]["daily_return"])
        metrics = snapshot["portfolios"]["paper"]["metrics"]
        self.assertEqual(
            metrics["data_status"],
            "OK",
        )
        self.assertEqual(
            metrics["performance_effective_date"],
            "2024-01-04",
        )
        self.assertEqual(
            metrics["performance_scope"],
            "LATEST_COMPLETE_SEGMENT",
        )
        self.assertEqual(metrics["total_return"], "0")

    def test_max_drawdown_uses_standard_peak_to_trough_ratio(self) -> None:
        self.append(
            "PORTFOLIO_OPEN",
            "paper-open",
            "2024-01-02T14:00:00Z",
            initial_cash="1000",
        )
        self.append(
            "BUY",
            "paper-buy",
            "2024-01-02T15:00:00Z",
            symbol="AAPL",
            shares="1",
            price="100",
            fee="0",
        )
        for day, aapl, spy in (
            ("2024-01-02", "100", "470"),
            ("2024-01-03", "120", "471"),
            ("2024-01-04", "90", "472"),
        ):
            self.append(
                "BENCHMARK_CLOSE",
                f"market-spy-{day}",
                f"{day}T21:00:00Z",
                symbol="SPY",
                close=spy,
                session_date=day,
            )
            self.append(
                "QUOTE",
                f"market-aapl-{day}",
                f"{day}T21:00:00Z",
                symbol="AAPL",
                close=aapl,
                session_date=day,
            )
        snapshot = build_snapshot(self.root, write=False)
        self.assertEqual(
            snapshot["portfolios"]["paper"]["metrics"]["max_drawdown"],
            "-0.02941176",
        )
        self.assertEqual(
            snapshot["portfolios"]["paper"]["metrics"][
                "performance_effective_date"
            ],
            "2024-01-02",
        )
        self.assertEqual(
            snapshot["portfolios"]["paper"]["metrics"]["performance_scope"],
            "FULL_HISTORY",
        )

    def test_snapshot_validator_rejects_incomplete_performance_metadata(
        self,
    ) -> None:
        snapshot = build_snapshot(self.root, write=False)
        del snapshot["portfolios"]["paper"]["metrics"]["performance_scope"]
        with self.assertRaises(ValidationError):
            validate_snapshot(snapshot)

    def test_sharpe_uses_trading_session_returns_not_weekend_zeros(self) -> None:
        trading_daily: list[dict[str, object]] = []
        calendar_daily: list[dict[str, object]] = []
        cursor = date(2024, 2, 1)
        cumulative = Decimal("0")
        sessions = 0

        while sessions < 20:
            if is_nyse_session(cursor):
                daily_return = (
                    Decimal("0")
                    if sessions == 0
                    else Decimal("0.01")
                    if sessions % 2
                    else Decimal("-0.005")
                )
                cumulative = (
                    Decimal("0")
                    if sessions == 0
                    else (1 + cumulative) * (1 + daily_return) - 1
                )
                point = {
                    "date": cursor.isoformat(),
                    "daily_return": daily_return,
                    "cumulative_return": cumulative,
                    "data_status": "OK",
                }
                trading_daily.append(point)
                calendar_daily.append(point)
                sessions += 1
            else:
                calendar_daily.append(
                    {
                        "date": cursor.isoformat(),
                        "daily_return": Decimal("0"),
                        "cumulative_return": cumulative,
                        "data_status": "OK",
                    }
                )
            cursor += timedelta(days=1)

        replay = SimpleNamespace(
            realized_pnl_total=Decimal("0"),
            win_rate=None,
            closed_episodes=[],
        )
        trading_metrics = _metrics(trading_daily, replay)
        calendar_metrics = _metrics(calendar_daily, replay)

        self.assertIsNotNone(trading_metrics["sharpe_ratio"])
        self.assertEqual(
            calendar_metrics["sharpe_ratio"],
            trading_metrics["sharpe_ratio"],
        )


if __name__ == "__main__":
    unittest.main()
