from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from portfolio_tracker.ledger import LedgerStore
from portfolio_tracker.snapshot import build_snapshot


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "repair_split_adjusted_quotes.py"
)
SPEC = importlib.util.spec_from_file_location(
    "repair_split_adjusted_quotes",
    SCRIPT_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load repair_split_adjusted_quotes.py")
MIGRATION = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MIGRATION
SPEC.loader.exec_module(MIGRATION)


class SplitAdjustedQuoteMigrationTests(unittest.TestCase):
    def event(
        self,
        event_id: str,
        portfolio: str,
        occurred_at: str,
        action: str,
        **fields: object,
    ) -> dict[str, object]:
        source = {
            "PORTFOLIO_OPEN": "bootstrap",
            "QUOTE": "cron-quote",
            "BENCHMARK_CLOSE": "cron-benchmark",
        }.get(action, "manual-import")
        return {
            "event_id": event_id,
            "portfolio": portfolio,
            "occurred_at": occurred_at,
            "created_at": occurred_at,
            "source": source,
            "action": action,
            **fields,
        }

    def seed(self, root: Path) -> None:
        store = LedgerStore(root)
        store.append_many(
            [
                self.event(
                    "live-open",
                    "live",
                    "2024-06-03T14:00:00Z",
                    "PORTFOLIO_OPEN",
                    initial_cash="1000",
                    currency="USD",
                ),
                self.event(
                    "live-buy-nvda",
                    "live",
                    "2024-06-05T15:00:00Z",
                    "BUY",
                    symbol="NVDA",
                    instrument_id="EQUITY:NVDA",
                    instrument_type="EQUITY",
                    quote_symbol="NVDA",
                    shares="1",
                    price="120",
                    fee="0",
                ),
                self.event(
                    "live-sell-nvda",
                    "live",
                    "2024-06-07T15:00:00Z",
                    "SELL",
                    symbol="NVDA",
                    instrument_id="EQUITY:NVDA",
                    instrument_type="EQUITY",
                    quote_symbol="NVDA",
                    shares="1",
                    price="121",
                    fee="0",
                ),
            ]
        )
        market: list[dict[str, object]] = []
        for day, nvda, spy in (
            ("2024-06-05", "12", "530"),
            ("2024-06-06", "12.1", "531"),
            ("2024-06-07", "12.2", "532"),
            ("2024-06-10", "12.3", "533"),
        ):
            market.extend(
                [
                    self.event(
                        f"market-nvda-{day}",
                        "market",
                        f"{day}T20:00:00Z",
                        "QUOTE",
                        symbol="NVDA",
                        close=nvda,
                        session_date=day,
                    ),
                    self.event(
                        f"market-spy-{day}",
                        "market",
                        f"{day}T20:00:01Z",
                        "BENCHMARK_CLOSE",
                        symbol="SPY",
                        close=spy,
                        session_date=day,
                    ),
                ]
            )
        store.append_many(market)

    def plan_payload(self, root: Path) -> dict[str, object]:
        store = LedgerStore(root)
        live = store.read("live")
        market = store.read("market")
        return {
            "schema_version": 1,
            "created_at": "2026-07-29T00:00:00Z",
            "quote_basis": MIGRATION.QUOTE_BASIS,
            "live_event_digest": MIGRATION._source_live_digest(live),
            "market_event_digest": MIGRATION._source_market_digest(market),
            "splits": [
                {
                    "symbol": "NVDA",
                    "instrument_id": "EQUITY:NVDA",
                    "effective_session": "2024-06-10",
                    "numerator": "10",
                    "denominator": "1",
                }
            ],
        }

    def write_plan(self, root: Path, payload: dict[str, object]) -> Path:
        path = root / "split-plan.json"
        path.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
        return path

    def test_check_only_is_hash_bound_and_has_zero_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.seed(root)
            plan_path = self.write_plan(root, self.plan_payload(root))
            live_path = root / "ledger" / "live.jsonl"
            market_path = root / "ledger" / "market.jsonl"
            before_live = live_path.read_bytes()
            before_market = market_path.read_bytes()

            result = MIGRATION.execute(
                root=root,
                plan_path=plan_path,
                apply=False,
            )

            self.assertEqual(result["status"], "valid")
            self.assertEqual(result["split_facts"], 1)
            self.assertEqual(result["corrected_sessions"], 3)
            self.assertEqual(result["pending"], 3)
            self.assertEqual(live_path.read_bytes(), before_live)
            self.assertEqual(market_path.read_bytes(), before_market)
            self.assertFalse((root / "backups").exists())

    def test_apply_corrects_nav_and_retry_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.seed(root)
            plan_path = self.write_plan(root, self.plan_payload(root))
            live_path = root / "ledger" / "live.jsonl"
            live_before = live_path.read_bytes()
            before = build_snapshot(root, write=False)
            before_day = {
                point["date"]: point
                for point in before["portfolios"]["live"]["daily"]
            }
            self.assertEqual(before_day["2024-06-05"]["nav"], "892")

            result = MIGRATION.execute(
                root=root,
                plan_path=plan_path,
                apply=True,
            )

            self.assertEqual(result["status"], "corrected")
            self.assertEqual(result["appended"], 3)
            self.assertEqual(live_path.read_bytes(), live_before)
            snapshot = json.loads(
                (
                    root / "snapshots" / "portfolio-snapshot.json"
                ).read_text(encoding="utf-8")
            )
            daily = {
                point["date"]: point
                for point in snapshot["portfolios"]["live"]["daily"]
            }
            self.assertEqual(daily["2024-06-05"]["nav"], "1000")
            self.assertEqual(daily["2024-06-06"]["nav"], "1001")
            self.assertEqual(daily["2024-06-07"]["nav"], "1001")
            event_count = len(LedgerStore(root).read("market"))
            backup_count = len(list((root / "backups").iterdir()))

            retry = MIGRATION.execute(
                root=root,
                plan_path=plan_path,
                apply=True,
            )

            self.assertEqual(retry["status"], "current")
            self.assertEqual(retry["pending"], 0)
            self.assertEqual(len(LedgerStore(root).read("market")), event_count)
            self.assertEqual(
                len(list((root / "backups").iterdir())),
                backup_count,
            )

    def test_source_digest_mismatch_fails_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.seed(root)
            payload = self.plan_payload(root)
            payload["market_event_digest"] = "0" * 64
            plan_path = self.write_plan(root, payload)
            market_path = root / "ledger" / "market.jsonl"
            before = market_path.read_bytes()

            with self.assertRaisesRegex(
                MIGRATION.SplitQuoteMigrationError,
                "market ledger does not match",
            ):
                MIGRATION.execute(
                    root=root,
                    plan_path=plan_path,
                    apply=True,
                )

            self.assertEqual(market_path.read_bytes(), before)
            self.assertFalse((root / "backups").exists())

    def test_multiple_reverse_splits_compound_to_session_basis(self) -> None:
        source = self.event(
            "market-sqqq-2024-01-02",
            "market",
            "2024-01-02T21:00:00Z",
            "QUOTE",
            symbol="SQQQ",
            close="250",
            session_date="2024-01-02",
        )
        facts = [
            MIGRATION.SplitFact(
                "SQQQ",
                "ETF:SQQQ",
                "2024-02-01",
                MIGRATION.Decimal("1"),
                MIGRATION.Decimal("5"),
            ),
            MIGRATION.SplitFact(
                "SQQQ",
                "ETF:SQQQ",
                "2024-03-01",
                MIGRATION.Decimal("1"),
                MIGRATION.Decimal("5"),
            ),
        ]

        factor = MIGRATION._factor_for_quote(source, facts)
        corrected = MIGRATION._corrected_quote(
            source,
            factor,
            created_at="2026-07-29T00:00:00Z",
        )

        self.assertEqual(factor, MIGRATION.Decimal("0.04"))
        self.assertEqual(corrected["close"], "10.00")

    def test_generate_plan_uses_only_splits_inside_quote_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.seed(root)

            def fetch(symbol: str) -> list[tuple[datetime, int]]:
                self.assertEqual(symbol, "NVDA")
                return [
                    (datetime(2020, 1, 1), 4),
                    (datetime(2024, 6, 10), 10),
                    (datetime(2027, 1, 1), 2),
                ]

            payload = MIGRATION.generate_plan(
                root=root,
                fetch_splits=fetch,
                created_at="2026-07-29T00:00:00Z",
            )

            self.assertEqual(
                payload["splits"],
                [
                    {
                        "symbol": "NVDA",
                        "instrument_id": "EQUITY:NVDA",
                        "effective_session": "2024-06-10",
                        "numerator": "10",
                        "denominator": "1",
                    }
                ],
            )

    def test_yfinance_fetcher_normalizes_listed_share_class(self) -> None:
        requested: list[str] = []
        split_date = datetime(2024, 1, 2)

        class FakeTicker:
            def __init__(self, symbol: str) -> None:
                requested.append(symbol)
                self.splits = {split_date: 50}

        with patch.dict(
            sys.modules,
            {"yfinance": SimpleNamespace(Ticker=FakeTicker)},
        ):
            fetch = MIGRATION._yfinance_fetcher()

        self.assertEqual(list(fetch("BRK.B")), [(split_date, 50)])
        self.assertEqual(requested, ["BRK-B"])

    def test_yfinance_fetcher_fails_closed_on_missing_series(self) -> None:
        class FakeTicker:
            def __init__(self, symbol: str) -> None:
                self.splits = None

        with patch.dict(
            sys.modules,
            {"yfinance": SimpleNamespace(Ticker=FakeTicker)},
        ):
            fetch = MIGRATION._yfinance_fetcher()

        with self.assertRaisesRegex(
            MIGRATION.SplitQuoteMigrationError,
            "returned no split series for BRK.B",
        ):
            list(fetch("BRK.B"))

    def test_provider_float_ratio_is_normalized_to_auditable_fraction(
        self,
    ) -> None:
        self.assertEqual(
            MIGRATION._ratio_parts(0.3333333333333333),
            ("1", "3"),
        )
        self.assertEqual(MIGRATION._ratio_parts(0.2), ("1", "5"))
        self.assertEqual(MIGRATION._ratio_parts(10.0), ("10", "1"))

    def test_provider_ratio_fails_closed_when_not_safely_normalized(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            MIGRATION.SplitQuoteMigrationError,
            "cannot be normalized safely",
        ):
            MIGRATION._ratio_parts("1.234567890123456789")


if __name__ == "__main__":
    unittest.main()
