from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from portfolio_tracker.errors import ValidationError


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "import_live_staging.py"
)
SPEC = importlib.util.spec_from_file_location(
    "import_live_staging",
    SCRIPT_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load import_live_staging.py")
IMPORTER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = IMPORTER
SPEC.loader.exec_module(IMPORTER)


class LiveStagingImporterTests(unittest.TestCase):
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

    def plan(self, source: Path) -> dict[str, object]:
        return {
            "plan_version": 1,
            "source": {
                "filename": source.name,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            },
            "events": [
                self.event(
                    "live-open-20210927",
                    "2021-09-27T00:00:00Z",
                    "PORTFOLIO_OPEN",
                    initial_cash="0",
                    currency="USD",
                ),
                self.event(
                    "live-wire-1",
                    "2021-09-27T13:00:00Z",
                    "CASH_FLOW",
                    symbol="USD",
                    amount="1000",
                ),
                self.event(
                    "live-buy-tsla",
                    "2021-09-30T15:00:00Z",
                    "BUY",
                    symbol="TSLA",
                    instrument_id="EQUITY:TSLA",
                    instrument_type="EQUITY",
                    quote_symbol="TSLA",
                    shares="4",
                    price="100",
                    fee="0",
                ),
                self.event(
                    "live-dividend-tsla",
                    "2021-10-01T14:00:00Z",
                    "INCOME_EXPENSE",
                    symbol="TSLA",
                    instrument_id="EQUITY:TSLA",
                    amount="7",
                    gross_amount="10",
                    withholding_tax="3",
                    income_type="DIVIDEND",
                ),
                self.event(
                    "live-split-tsla",
                    "2022-08-29T15:00:00Z",
                    "SPLIT",
                    symbol="TSLA",
                    instrument_id="EQUITY:TSLA",
                    instrument_type="EQUITY",
                    quote_symbol="TSLA",
                    numerator="3",
                    denominator="1",
                ),
            ],
            "expected": {
                "ending_cash": "607",
                "income_expense_total": "7",
                "realized_pnl": "0",
                "holdings": {"EQUITY:TSLA": "12"},
            },
        }

    def write_plan(self, directory: Path, source: Path) -> Path:
        path = directory / "live-plan.json"
        path.write_text(
            json.dumps(self.plan(source)),
            encoding="utf-8",
        )
        return path

    def test_check_only_validates_without_writing_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.xlsx"
            source.write_bytes(b"fixture")
            plan = self.write_plan(root, source)

            result = IMPORTER.execute(
                root=root / "runtime",
                plan_path=plan,
                source_workbook=source,
                apply=False,
            )

            self.assertEqual(result["status"], "valid")
            self.assertEqual(result["events"], 5)
            self.assertFalse((root / "runtime" / "ledger" / "live.jsonl").exists())

    def test_staging_rejects_post_listing_private_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.xlsx"
            source.write_bytes(b"fixture")
            plan = self.plan(source)
            plan["events"] = [
                plan["events"][0],
                self.event(
                    "live-buy-cbrs-private",
                    "2026-05-18T15:00:00Z",
                    "BUY",
                    symbol="CBRS",
                    instrument_id="PRIVATE:CEREBRAS",
                    instrument_type="PRIVATE",
                    quote_symbol=None,
                    shares="1",
                    price="200",
                    fee="0",
                ),
            ]

            with self.assertRaisesRegex(
                ValidationError,
                "CBRS must use EQUITY:CBRS",
            ):
                IMPORTER.validate_plan(plan)

    def test_apply_is_atomic_and_retry_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.xlsx"
            source.write_bytes(b"fixture")
            plan = self.write_plan(root, source)
            runtime = root / "runtime"

            first = IMPORTER.execute(
                root=runtime,
                plan_path=plan,
                source_workbook=source,
                apply=True,
            )
            second = IMPORTER.execute(
                root=runtime,
                plan_path=plan,
                source_workbook=source,
                apply=True,
            )

            self.assertEqual(first["status"], "imported")
            self.assertEqual(first["appended"], 5)
            self.assertEqual(second["status"], "duplicate")
            self.assertEqual(second["duplicates"], 5)
            lines = (runtime / "ledger" / "live.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 5)
            snapshot = json.loads(
                (runtime / "snapshots" / "portfolio-snapshot.json").read_text()
            )
            self.assertEqual(snapshot["schema_version"], 4)
            self.assertEqual(snapshot["portfolios"]["live"]["cash"], "607")
            self.assertTrue((runtime / "state" / "publish.pending").exists())

    def test_source_hash_mismatch_has_zero_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.xlsx"
            source.write_bytes(b"fixture")
            plan = self.write_plan(root, source)
            source.write_bytes(b"changed")
            runtime = root / "runtime"

            with self.assertRaisesRegex(
                IMPORTER.LiveImportError,
                "SHA-256 mismatch",
            ):
                IMPORTER.execute(
                    root=runtime,
                    plan_path=plan,
                    source_workbook=source,
                    apply=True,
                )

            self.assertFalse((runtime / "ledger" / "live.jsonl").exists())

    def test_expected_invariant_mismatch_has_zero_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.xlsx"
            source.write_bytes(b"fixture")
            payload = self.plan(source)
            payload["expected"]["ending_cash"] = "999"
            plan = root / "live-plan.json"
            plan.write_text(json.dumps(payload), encoding="utf-8")
            runtime = root / "runtime"

            with self.assertRaisesRegex(
                IMPORTER.LiveImportError,
                "business invariants",
            ):
                IMPORTER.execute(
                    root=runtime,
                    plan_path=plan,
                    source_workbook=source,
                    apply=True,
                )

            self.assertFalse((runtime / "ledger" / "live.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
