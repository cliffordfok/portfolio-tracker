from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from portfolio_tracker.ledger import LedgerStore
from portfolio_tracker.replay import replay_portfolio


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "repair_spcx_listing.py"
)
SPEC = importlib.util.spec_from_file_location(
    "repair_spcx_listing",
    SCRIPT_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load repair_spcx_listing.py")
MIGRATION = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MIGRATION
SPEC.loader.exec_module(MIGRATION)


class SpcxListingMigrationTests(unittest.TestCase):
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

    def seed(self, root: Path, *, conflicting_quote: bool = False) -> None:
        live = [
            self.event(
                "live-open",
                "live",
                "2021-09-27T00:00:00Z",
                "PORTFOLIO_OPEN",
                initial_cash="100000",
                currency="USD",
            ),
            self.event(
                "live-buy-spcx-private",
                "live",
                "2026-07-10T14:00:00Z",
                "BUY",
                symbol="SPCX",
                instrument_id="PRIVATE:SPACEX",
                instrument_type="PRIVATE",
                instrument_name="Space Exploration Technologies Corp.",
                quote_symbol=None,
                shares="30",
                price="170",
                fee="0",
            ),
            self.event(
                "live-buy-mu",
                "live",
                "2026-07-10T14:01:00Z",
                "BUY",
                symbol="MU",
                instrument_id="EQUITY:MU",
                instrument_type="EQUITY",
                quote_symbol="MU",
                shares="1",
                price="900",
                fee="0",
            ),
            self.event(
                "live-buy-nvda",
                "live",
                "2026-07-10T14:02:00Z",
                "BUY",
                symbol="NVDA",
                instrument_id="EQUITY:NVDA",
                instrument_type="EQUITY",
                quote_symbol="NVDA",
                shares="1",
                price="200",
                fee="0",
            ),
            self.event(
                "live-buy-voo",
                "live",
                "2026-07-10T14:03:00Z",
                "BUY",
                symbol="VOO",
                instrument_id="ETF:VOO",
                instrument_type="ETF",
                quote_symbol="VOO",
                shares="1",
                price="600",
                fee="0",
            ),
            self.event(
                "live-buy-skhyv",
                "live",
                "2026-07-10T14:04:00Z",
                "BUY",
                symbol="SKHYV",
                instrument_id="EQUITY:SKHYV",
                instrument_type="EQUITY",
                quote_symbol="SKHYV",
                shares="1",
                price="170",
                fee="0",
            ),
        ]
        market = [
            self.event(
                "market-mu-2026-07-24",
                "market",
                "2026-07-24T20:00:00Z",
                "QUOTE",
                symbol="MU",
                close="920.95",
                session_date="2026-07-24",
            ),
            self.event(
                "market-nvda-2026-07-24",
                "market",
                "2026-07-24T20:00:01Z",
                "QUOTE",
                symbol="NVDA",
                close="206.84",
                session_date="2026-07-24",
            ),
            self.event(
                "market-spy-2026-07-24",
                "market",
                "2026-07-24T20:00:02Z",
                "BENCHMARK_CLOSE",
                symbol="SPY",
                close="738.93",
                session_date="2026-07-24",
            ),
        ]
        if conflicting_quote:
            market.append(
                self.event(
                    "market-voo-conflict-2026-07-22",
                    "market",
                    "2026-07-22T20:00:00Z",
                    "QUOTE",
                    symbol="VOO",
                    close="999",
                    session_date="2026-07-22",
                )
            )
        LedgerStore(root).append_many(live + market)

    def test_check_only_has_zero_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.seed(root)
            before_live = (root / "ledger" / "live.jsonl").read_bytes()
            before_market = (root / "ledger" / "market.jsonl").read_bytes()

            result = MIGRATION.execute(root=root, apply=False)

            self.assertEqual(result["status"], "valid")
            self.assertEqual(result["pending"], 17)
            self.assertEqual(result["quote_events"], 15)
            self.assertEqual(
                (root / "ledger" / "live.jsonl").read_bytes(),
                before_live,
            )
            self.assertEqual(
                (root / "ledger" / "market.jsonl").read_bytes(),
                before_market,
            )
            self.assertFalse((root / "backups").exists())
            self.assertFalse((root / "snapshots").exists())

    def test_apply_preserves_business_invariants_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.seed(root)
            store = LedgerStore(root)
            before = replay_portfolio(store.read("live"), portfolio="live")

            first = MIGRATION.execute(root=root, apply=True)
            after = replay_portfolio(store.read("live"), portfolio="live")
            live_lines = (root / "ledger" / "live.jsonl").read_text().splitlines()
            market_lines = (
                root / "ledger" / "market.jsonl"
            ).read_text().splitlines()
            backup_count = len(list((root / "backups").iterdir()))
            second = MIGRATION.execute(root=root, apply=True)

            self.assertEqual(first["status"], "corrected")
            self.assertEqual(first["appended"], 17)
            self.assertEqual(first["snapshot_status"], "rebuilt")
            self.assertEqual(first["acceptance"]["instrument_id"], "EQUITY:SPCX")
            self.assertEqual(first["acceptance"]["quote_status"], "OK")
            self.assertEqual(first["acceptance"]["current_price"], "115.07")
            self.assertEqual(
                first["acceptance"]["performance_effective_date"],
                "2026-07-22",
            )
            self.assertEqual(before.cash, after.cash)
            self.assertEqual(before.buy_outflow, after.buy_outflow)
            self.assertEqual(before.realized_pnl_total, after.realized_pnl_total)
            self.assertEqual(len(before.trade_history), len(after.trade_history))
            holdings = {
                holding["instrument_id"]: holding
                for holding in after.holdings
            }
            self.assertNotIn("PRIVATE:SPACEX", holdings)
            self.assertEqual(holdings["EQUITY:SPCX"]["shares"], 30)
            self.assertEqual(holdings["EQUITY:SPCX"]["avg_cost"], 170)
            self.assertEqual(len(live_lines), 8)
            self.assertEqual(len(market_lines), 18)
            self.assertEqual(second["status"], "current")
            self.assertEqual(second["pending"], 0)
            self.assertEqual(
                len(list((root / "backups").iterdir())),
                backup_count,
            )
            self.assertEqual(
                len((root / "ledger" / "live.jsonl").read_text().splitlines()),
                len(live_lines),
            )
            self.assertEqual(
                len(
                    (root / "ledger" / "market.jsonl")
                    .read_text()
                    .splitlines()
                ),
                len(market_lines),
            )
            publish = json.loads(
                (root / "state" / "publish.pending").read_text()
            )
            self.assertEqual(
                publish["requested_by"],
                "spcx-public-listing-migration",
            )

    def test_conflicting_existing_quote_fails_before_backup_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.seed(root, conflicting_quote=True)
            before_live = (root / "ledger" / "live.jsonl").read_bytes()
            before_market = (root / "ledger" / "market.jsonl").read_bytes()

            with self.assertRaisesRegex(
                MIGRATION.SpcxMigrationError,
                "conflicting market close for VOO on 2026-07-22",
            ):
                MIGRATION.execute(root=root, apply=True)

            self.assertEqual(
                (root / "ledger" / "live.jsonl").read_bytes(),
                before_live,
            )
            self.assertEqual(
                (root / "ledger" / "market.jsonl").read_bytes(),
                before_market,
            )
            self.assertFalse((root / "backups").exists())

    def test_partial_correction_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.seed(root)
            store = LedgerStore(root)
            target = next(
                event
                for event in store.read("live")
                if event["event_id"] == "live-buy-spcx-private"
            )
            replacement = MIGRATION._correction_events(target)[0]
            store.append(replacement)
            before_live = (root / "ledger" / "live.jsonl").read_bytes()

            with self.assertRaisesRegex(
                MIGRATION.SpcxMigrationError,
                "only partially recorded",
            ):
                MIGRATION.execute(root=root, apply=True)

            self.assertEqual(
                (root / "ledger" / "live.jsonl").read_bytes(),
                before_live,
            )
            self.assertFalse((root / "backups").exists())


if __name__ == "__main__":
    unittest.main()
