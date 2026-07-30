from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from portfolio_tracker.ledger import LedgerStore, _append_json_line
from portfolio_tracker.replay import replay_portfolio
from portfolio_tracker.resolver import resolve_effective_events
from portfolio_tracker.schemas import normalize_event


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "repair_legacy_option_identities.py"
)
SPEC = importlib.util.spec_from_file_location(
    "repair_legacy_option_identities",
    SCRIPT_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load repair_legacy_option_identities.py")
MIGRATION = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MIGRATION
SPEC.loader.exec_module(MIGRATION)


class LegacyOptionIdentityMigrationTests(unittest.TestCase):
    def append_partial(
        self,
        root: Path,
        candidate: dict[str, object],
    ) -> None:
        stored = normalize_event(candidate)
        stored["ledger_seq"] = len(LedgerStore(root).read("live")) + 1
        _append_json_line(
            root / "ledger" / "live.jsonl",
            stored,
        )

    def event(
        self,
        event_id: str,
        occurred_at: str,
        action: str,
        **fields: object,
    ) -> dict[str, object]:
        return {
            "event_id": event_id,
            "portfolio": "live",
            "occurred_at": occurred_at,
            "created_at": occurred_at,
            "source": "bootstrap" if action == "PORTFOLIO_OPEN" else "manual-import",
            "action": action,
            **fields,
        }

    def seed(self, root: Path) -> None:
        events = [
            self.event(
                "live-open",
                "2024-01-01T14:00:00Z",
                "PORTFOLIO_OPEN",
                initial_cash="10000",
                currency="USD",
            ),
            self.event(
                "live-option-buy",
                "2024-01-02T15:00:00Z",
                "BUY",
                symbol="NVDA",
                instrument_id="OPTION:NVDA:2024-01-19:C:500",
                instrument_type="OPTION",
                quote_symbol="NVDA",
                contract_multiplier="100",
                shares="1",
                price="5",
                fee="1",
            ),
            self.event(
                "live-option-sell",
                "2024-01-03T15:00:00Z",
                "SELL",
                symbol="NVDA",
                instrument_id="OPTION:NVDA:2024-01-19:C:500",
                instrument_type="OPTION",
                quote_symbol="NVDA",
                contract_multiplier="100",
                shares="1",
                price="6",
                fee="1",
            ),
        ]
        LedgerStore(root).append_many(events)

    def test_check_only_has_zero_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.seed(root)
            ledger = root / "ledger" / "live.jsonl"
            before = ledger.read_bytes()

            result = MIGRATION.execute(root=root, apply=False)

            self.assertEqual(result["status"], "valid")
            self.assertEqual(result["target_events"], 2)
            self.assertEqual(result["pending"], 4)
            self.assertEqual(ledger.read_bytes(), before)
            self.assertFalse((root / "backups").exists())
            self.assertFalse(
                (root / "snapshots" / "portfolio-snapshot.json").exists()
            )

    def test_apply_preserves_financials_and_retry_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.seed(root)
            store = LedgerStore(root)
            before = replay_portfolio(store.read("live"), portfolio="live")

            result = MIGRATION.execute(root=root, apply=True)

            self.assertEqual(result["status"], "corrected")
            self.assertEqual(result["appended"], 4)
            events = store.read("live")
            after = replay_portfolio(events, portfolio="live")
            self.assertEqual(after.cash, before.cash)
            self.assertEqual(after.buy_outflow, before.buy_outflow)
            self.assertEqual(after.sell_inflow, before.sell_inflow)
            self.assertEqual(after.realized_pnl_total, before.realized_pnl_total)
            effective_options = [
                event
                for event in resolve_effective_events(events)
                if event.get("instrument_type") == "OPTION"
            ]
            self.assertEqual(len(effective_options), 2)
            self.assertTrue(
                all("quote_symbol" not in event for event in effective_options)
            )
            self.assertEqual(
                (
                    root / "state" / "publish.pending"
                ).read_text(encoding="utf-8").count(
                    "legacy-option-identity-migration"
                ),
                1,
            )
            backup_count = len(list((root / "backups").iterdir()))
            event_count = len(events)
            snapshot_revision = result["snapshot_revision"]

            retry = MIGRATION.execute(root=root, apply=True)

            self.assertEqual(retry["status"], "current")
            self.assertEqual(retry["pending"], 0)
            self.assertEqual(len(store.read("live")), event_count)
            self.assertEqual(
                len(list((root / "backups").iterdir())),
                backup_count,
            )
            self.assertEqual(retry["snapshot_revision"], snapshot_revision)

    def test_partial_migration_resumes_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.seed(root)
            plan = MIGRATION.inspect_migration(root)
            self.append_partial(root, plan.candidates[0])

            partial = MIGRATION.inspect_migration(root)
            self.assertEqual(partial.pending, 3)
            result = MIGRATION.execute(root=root, apply=True)

            self.assertEqual(result["status"], "corrected")
            self.assertEqual(result["appended"], 3)
            self.assertEqual(
                MIGRATION.inspect_migration(root).pending,
                0,
            )

    def test_conflicting_generated_event_fails_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.seed(root)
            candidate = MIGRATION.inspect_migration(root).candidates[0]
            conflict = dict(candidate, note="conflicting payload")
            self.append_partial(root, conflict)

            with self.assertRaisesRegex(
                MIGRATION.ConflictError,
                "different payload",
            ):
                MIGRATION.inspect_migration(root)

            self.assertFalse((root / "backups").exists())


if __name__ == "__main__":
    unittest.main()
