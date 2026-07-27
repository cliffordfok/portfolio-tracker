from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from portfolio_tracker.ledger import LedgerStore, _append_json_line
from portfolio_tracker.replay import replay_portfolio


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "repair_instrument_identities.py"
)
SPEC = importlib.util.spec_from_file_location(
    "repair_instrument_identities",
    SCRIPT_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load repair_instrument_identities.py")
MIGRATION = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MIGRATION
SPEC.loader.exec_module(MIGRATION)


class InstrumentIdentityMigrationTests(unittest.TestCase):
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

    def seed(
        self,
        root: Path,
        *,
        omit_last_quote: bool = False,
    ) -> None:
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
                "live-cbrs-buy-1",
                "live",
                "2026-05-18T14:00:00Z",
                "BUY",
                symbol="CBRS",
                instrument_id="PRIVATE:CEREBRAS",
                instrument_type="PRIVATE",
                instrument_name="Cerebras Systems Inc.",
                quote_symbol=None,
                shares="4",
                price="300",
                fee="0",
            ),
            self.event(
                "live-cbrs-buy-2",
                "live",
                "2026-05-18T14:01:00Z",
                "BUY",
                symbol="CBRS",
                instrument_id="PRIVATE:CEREBRAS",
                instrument_type="PRIVATE",
                instrument_name="Cerebras Systems Inc.",
                quote_symbol=None,
                shares="4",
                price="290",
                fee="0",
            ),
            self.event(
                "live-cbrs-buy-3",
                "live",
                "2026-05-22T14:00:00Z",
                "BUY",
                symbol="CBRS",
                instrument_id="PRIVATE:CEREBRAS",
                instrument_type="PRIVATE",
                instrument_name="Cerebras Systems Inc.",
                quote_symbol=None,
                shares="1",
                price="278.60",
                fee="0",
            ),
            self.event(
                "live-cbrs-buy-4",
                "live",
                "2026-05-26T14:00:00Z",
                "BUY",
                symbol="CBRS",
                instrument_id="PRIVATE:CEREBRAS",
                instrument_type="PRIVATE",
                instrument_name="Cerebras Systems Inc.",
                quote_symbol=None,
                shares="1",
                price="260",
                fee="0",
            ),
            self.event(
                "live-cbrs-sell",
                "live",
                "2026-06-02T14:00:00Z",
                "SELL",
                symbol="CBRS",
                instrument_id="PRIVATE:CEREBRAS",
                instrument_type="PRIVATE",
                instrument_name="Cerebras Systems Inc.",
                quote_symbol=None,
                shares="10",
                price="225",
                fee="0.05",
            ),
            self.event(
                "live-skhyv-buy",
                "live",
                "2026-07-10T15:00:00Z",
                "BUY",
                symbol="SKHYV",
                instrument_id="EQUITY:SKHYV",
                instrument_type="EQUITY",
                instrument_name="SK hynix Inc. ADR",
                quote_symbol="SKHYV",
                shares="10",
                price="170",
                fee="0",
            ),
        ]
        closes = {
            "2026-07-22": ("165.27", "747.41"),
            "2026-07-23": ("169.50", "738.18"),
            "2026-07-24": ("154.57", "738.93"),
        }
        market: list[dict[str, object]] = []
        for session_date, (skhy_close, spy_close) in closes.items():
            if not (omit_last_quote and session_date == "2026-07-24"):
                market.append(
                    self.event(
                        f"market-skhyv-{session_date}",
                        "market",
                        f"{session_date}T20:00:00Z",
                        "QUOTE",
                        symbol="SKHYV",
                        close=skhy_close,
                        session_date=session_date,
                    )
                )
            market.append(
                self.event(
                    f"market-spy-{session_date}",
                    "market",
                    f"{session_date}T20:00:01Z",
                    "BENCHMARK_CLOSE",
                    symbol="SPY",
                    close=spy_close,
                    session_date=session_date,
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
            self.assertEqual(result["target_events"], 6)
            self.assertEqual(result["quote_events"], 3)
            self.assertEqual(result["pending"], 15)
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

    def test_apply_preserves_invariants_and_retry_is_idempotent(self) -> None:
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
            self.assertEqual(first["appended"], 15)
            self.assertEqual(first["acceptance"]["cbrs_trade_count"], 5)
            self.assertEqual(first["acceptance"]["skhy_trade_count"], 1)
            self.assertEqual(
                first["acceptance"]["skhy_instrument_id"],
                "EQUITY:SKHY",
            )
            self.assertEqual(first["acceptance"]["skhy_quote_status"], "OK")
            self.assertEqual(
                first["acceptance"]["skhy_current_price"],
                "154.57",
            )
            self.assertEqual(before.cash, after.cash)
            self.assertEqual(before.buy_outflow, after.buy_outflow)
            self.assertEqual(before.sell_inflow, after.sell_inflow)
            self.assertEqual(before.realized_pnl_total, after.realized_pnl_total)
            self.assertEqual(len(before.trade_history), len(after.trade_history))
            holdings = {
                holding["instrument_id"]: holding
                for holding in after.holdings
            }
            self.assertNotIn("PRIVATE:CEREBRAS", holdings)
            self.assertNotIn("EQUITY:SKHYV", holdings)
            self.assertNotIn("EQUITY:CBRS", holdings)
            self.assertEqual(holdings["EQUITY:SKHY"]["shares"], 10)
            self.assertEqual(holdings["EQUITY:SKHY"]["avg_cost"], 170)
            self.assertEqual(len(live_lines), 19)
            self.assertEqual(len(market_lines), 9)
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
                "listed-instrument-identity-migration",
            )

    def test_missing_source_quote_fails_before_backup_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.seed(root, omit_last_quote=True)
            before_live = (root / "ledger" / "live.jsonl").read_bytes()
            before_market = (root / "ledger" / "market.jsonl").read_bytes()

            with self.assertRaisesRegex(
                MIGRATION.IdentityMigrationError,
                "expected one SKHYV quote for 2026-07-24",
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

    def test_partial_correction_is_resumed_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.seed(root)
            store = LedgerStore(root)
            plan = MIGRATION.inspect_migration(root)
            next_sequence = max(
                event["ledger_seq"]
                for event in store.read("live")
            ) + 1
            # Emulate a process crash after the replacement CBRS SELL was
            # fsynced but before its matching VOID or any later batch events.
            # This prefix cannot replay economically by itself because both
            # the original and replacement SELL are temporarily effective.
            for candidate in plan.candidates[:9]:
                stored = MIGRATION.normalize_event(candidate)
                stored["ledger_seq"] = next_sequence
                next_sequence += 1
                _append_json_line(store.path_for("live"), stored)
            partial = MIGRATION.execute(root=root, apply=False)
            result = MIGRATION.execute(root=root, apply=True)
            retry = MIGRATION.execute(root=root, apply=True)

            self.assertEqual(partial["status"], "valid")
            self.assertEqual(partial["pending"], 6)
            self.assertEqual(partial["duplicates"], 9)
            self.assertEqual(result["status"], "corrected")
            self.assertEqual(result["appended"], 6)
            self.assertEqual(result["acceptance"]["cbrs_trade_count"], 5)
            self.assertEqual(result["acceptance"]["skhy_trade_count"], 1)
            self.assertEqual(retry["status"], "current")
            self.assertEqual(retry["pending"], 0)
            self.assertEqual(
                len((root / "ledger" / "live.jsonl").read_text().splitlines()),
                19,
            )
            self.assertEqual(
                len(
                    (root / "ledger" / "market.jsonl")
                    .read_text()
                    .splitlines()
                ),
                9,
            )


if __name__ == "__main__":
    unittest.main()
