from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "import_paper_log.py"
)
SPEC = importlib.util.spec_from_file_location("import_paper_log", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load import_paper_log.py")
IMPORTER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = IMPORTER
SPEC.loader.exec_module(IMPORTER)


class RecordingRunner:
    def __init__(self, statuses: list[str]) -> None:
        self.statuses = statuses
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        status = self.statuses[len(self.commands) - 1]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"status": status}),
            stderr="",
        )


class PaperLogImporterTests(unittest.TestCase):
    def write_log(self, directory: str, events: list[object]) -> Path:
        path = Path(directory) / "paper.jsonl"
        path.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )
        return path

    def sample(self, **changes: object) -> dict[str, object]:
        event: dict[str, object] = {
            "timestamp": "2026-07-16T14:00:29.976330",
            "action": "BUY",
            "symbol": "aapl",
            "shares": 1,
            "price": 180.5,
            "cost": 180.5,
            "reason": "entry signal",
        }
        event.update(changes)
        return event

    def test_legacy_event_id_is_stable_for_existing_number_types(self) -> None:
        event = self.sample()
        self.assertEqual(
            IMPORTER.legacy_event_id(event),
            "paper-swing-38fb2c164d0dc983c5add5bb1e4182d4",
        )
        float_shares = self.sample(shares=1.0)
        self.assertNotEqual(
            IMPORTER.legacy_event_id(event),
            IMPORTER.legacy_event_id(float_shares),
        )

    def test_sell_partial_maps_to_sell_and_uses_utc(self) -> None:
        trade = IMPORTER.prepare_trade(
            self.sample(action="SELL_PARTIAL"),
            line_number=1,
        )
        self.assertEqual(trade.action, "SELL")
        self.assertEqual(trade.symbol, "AAPL")
        self.assertEqual(trade.occurred_at, "2026-07-16T14:00:29.976330Z")

    def test_complete_preflight_happens_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = self.write_log(
                temp,
                [
                    self.sample(),
                    self.sample(
                        timestamp="2026-07-16T14:01:00",
                        shares="not-a-number",
                    ),
                ],
            )
            runner = RecordingRunner(["appended"])
            with self.assertRaisesRegex(
                IMPORTER.PaperLogImportError,
                "line 2 shares must be numeric",
            ):
                plan = IMPORTER.build_plan(
                    source,
                    cutoff="2026-07-16T00:00:00Z",
                )
                IMPORTER.execute_plan(
                    plan,
                    python="python3",
                    bridge=Path("/repo/hermes_bridge.py"),
                    runtime_root=Path("/runtime"),
                    runner=runner,
                )
            self.assertEqual(runner.commands, [])

    def test_retry_counts_appended_duplicate_and_rebuild_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = self.write_log(
                temp,
                [
                    self.sample(),
                    self.sample(
                        timestamp="2026-07-16T14:01:00",
                        action="SELL_PARTIAL",
                        shares=0.5,
                        price=181.0,
                    ),
                    self.sample(
                        timestamp="2026-07-16T14:02:00",
                        symbol="MSFT",
                        price=400.0,
                    ),
                ],
            )
            plan = IMPORTER.build_plan(
                source,
                cutoff="2026-07-16T00:00:00Z",
            )
            runner = RecordingRunner(
                ["appended", "duplicate", "recorded_but_rebuild_pending"]
            )
            result = IMPORTER.execute_plan(
                plan,
                python="python3",
                bridge=Path("/repo/hermes_bridge.py"),
                runtime_root=Path("/runtime"),
                runner=runner,
            )
            self.assertEqual(result["appended"], 2)
            self.assertEqual(result["duplicates"], 1)
            self.assertEqual(result["rebuild_pending"], 1)
            self.assertEqual(len(runner.commands), 3)
            self.assertIn("--source", runner.commands[0])
            self.assertIn("swing-trader", runner.commands[0])

    def test_cutoff_is_timezone_aware_and_empty_plan_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = self.write_log(
                temp,
                [self.sample(timestamp="2026-07-15T23:59:59.999999")],
            )
            plan = IMPORTER.build_plan(
                source,
                cutoff="2026-07-16T00:00:00Z",
            )
            self.assertEqual(plan.source_events, 1)
            self.assertEqual(plan.skipped_before_cutoff, 1)
            self.assertEqual(plan.trades, ())

    def test_out_of_order_post_cutoff_log_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = self.write_log(
                temp,
                [
                    self.sample(timestamp="2026-07-16T14:01:00"),
                    self.sample(timestamp="2026-07-16T14:00:00"),
                ],
            )
            with self.assertRaisesRegex(
                IMPORTER.PaperLogImportError,
                "timestamps must be ordered",
            ):
                IMPORTER.build_plan(
                    source,
                    cutoff="2026-07-16T00:00:00Z",
                )

    def test_reconciliation_matches_existing_retry_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = self.write_log(temp, [self.sample()])
            plan = IMPORTER.build_plan(
                source,
                cutoff="2026-07-16T00:00:00Z",
            )
            ledger_path = Path(temp) / "ledger" / "paper.jsonl"
            ledger_path.parent.mkdir()
            stored = IMPORTER.expected_ledger_payload(plan.trades[0])
            stored["ledger_seq"] = 2
            ledger_path.write_text(json.dumps(stored) + "\n", encoding="utf-8")

            self.assertEqual(
                IMPORTER.reconcile_ledger(plan, ledger_path),
                {"existing": 1, "missing": 0},
            )

    def test_reconciliation_rejects_unknown_legacy_event_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = self.write_log(temp, [self.sample()])
            plan = IMPORTER.build_plan(
                source,
                cutoff="2026-07-16T00:00:00Z",
            )
            ledger_path = Path(temp) / "paper.jsonl"
            stored = IMPORTER.expected_ledger_payload(plan.trades[0])
            stored["event_id"] = "paper-swing-unknown"
            stored["ledger_seq"] = 2
            ledger_path.write_text(json.dumps(stored) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                IMPORTER.PaperLogImportError,
                "absent from the source plan",
            ):
                IMPORTER.reconcile_ledger(plan, ledger_path)

    def test_reconciliation_rejects_payload_mismatch_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = self.write_log(temp, [self.sample()])
            plan = IMPORTER.build_plan(
                source,
                cutoff="2026-07-16T00:00:00Z",
            )
            ledger_path = Path(temp) / "paper.jsonl"
            stored = IMPORTER.expected_ledger_payload(plan.trades[0])
            stored["price"] = "999"
            stored["ledger_seq"] = 2
            ledger_path.write_text(json.dumps(stored) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                IMPORTER.PaperLogImportError,
                "payload mismatch",
            ):
                IMPORTER.reconcile_ledger(plan, ledger_path)

    def test_bridge_error_is_sanitized(self) -> None:
        trade = IMPORTER.prepare_trade(self.sample(), line_number=1)

        def failing(command: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="credential=must-not-leak",
            )

        with self.assertRaisesRegex(
            IMPORTER.PaperLogImportError,
            r"bridge rejected event paper-swing-.*exit 1",
        ) as raised:
            IMPORTER.execute_plan(
                IMPORTER.ImportPlan(1, 0, (trade,)),
                python="python3",
                bridge=Path("/repo/hermes_bridge.py"),
                runtime_root=Path("/runtime"),
                runner=failing,
            )
        self.assertNotIn("must-not-leak", str(raised.exception))

    def test_check_only_cli_does_not_require_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = self.write_log(temp, [self.sample()])
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "--source-log",
                    str(source),
                    "--bridge",
                    str(Path(temp) / "missing.py"),
                    "--check-only",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["status"], "valid")
            self.assertEqual(result["eligible"], 1)


if __name__ == "__main__":
    unittest.main()
