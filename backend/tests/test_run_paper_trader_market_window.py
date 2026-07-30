from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "run_paper_trader_market_window.py"
)
SPEC = importlib.util.spec_from_file_location("paper_window", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load run_paper_trader_market_window.py")
WINDOW = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = WINDOW
SPEC.loader.exec_module(WINDOW)


class RecordingRunner:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(
        self,
        command: list[str],
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(command), dict(environment)))
        return subprocess.CompletedProcess(command, self.returncode)


class PaperTraderMarketWindowTests(unittest.TestCase):
    def test_deployment_wrapper_routes_through_guard(self) -> None:
        wrapper = (
            SCRIPT_PATH.parent / "run_paper_trader_market_window.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "/data/portfolio-tracker/scripts/"
            "run_paper_trader_market_window.py",
            wrapper,
        )
        self.assertIn(
            "-- /usr/local/bin/python3 /data/scripts/swing_trader.py",
            wrapper,
        )

    def test_summer_candidate_runs_at_1545_edt(self) -> None:
        decision = WINDOW.decide(
            datetime(2026, 7, 30, 19, 45, tzinfo=UTC)
        )
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.local_time.strftime("%H:%M%z"), "15:45-0400")

    def test_winter_candidate_runs_at_1545_est(self) -> None:
        decision = WINDOW.decide(
            datetime(2026, 12, 1, 20, 45, tzinfo=UTC)
        )
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.local_time.strftime("%H:%M%z"), "15:45-0500")

    def test_other_utc_candidate_is_skipped_after_dst_conversion(self) -> None:
        summer = WINDOW.decide(
            datetime(2026, 7, 30, 20, 45, tzinfo=UTC)
        )
        winter = WINDOW.decide(
            datetime(2026, 12, 1, 19, 45, tzinfo=UTC)
        )
        self.assertFalse(summer.eligible)
        self.assertFalse(winter.eligible)
        self.assertEqual(summer.reason, "outside_market_window")
        self.assertEqual(winter.reason, "outside_market_window")

    def test_nyse_holiday_is_skipped(self) -> None:
        decision = WINDOW.decide(
            datetime(2026, 11, 26, 20, 45, tzinfo=UTC)
        )
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, "not_nyse_session")

    def test_early_close_session_is_skipped(self) -> None:
        decision = WINDOW.decide(
            datetime(2026, 11, 27, 20, 45, tzinfo=UTC)
        )
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, "early_close_session")

    def test_eligible_execution_scrubs_github_credentials(self) -> None:
        runner = RecordingRunner()
        result, returncode = WINDOW.execute(
            ["/usr/local/bin/python3", "/data/scripts/swing_trader.py"],
            now_utc=datetime(2026, 7, 30, 19, 45, tzinfo=UTC),
            runner=runner,
            source_environment={
                "PATH": "/usr/bin",
                "GITHUB_TOKEN": "secret",
                "PORTFOLIO_GITHUB_TOKEN": "publisher-secret",
            },
        )
        self.assertEqual(returncode, 0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(runner.calls), 1)
        environment = runner.calls[0][1]
        self.assertEqual(environment["PATH"], "/usr/bin")
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("PORTFOLIO_GITHUB_TOKEN", environment)

    def test_check_only_never_runs_child(self) -> None:
        runner = RecordingRunner()
        result, returncode = WINDOW.execute(
            [],
            now_utc=datetime(2026, 7, 30, 19, 45, tzinfo=UTC),
            check_only=True,
            runner=runner,
        )
        self.assertEqual(returncode, 0)
        self.assertEqual(result["status"], "eligible")
        self.assertEqual(runner.calls, [])

    def test_child_failure_is_propagated(self) -> None:
        runner = RecordingRunner(returncode=23)
        result, returncode = WINDOW.execute(
            ["paper-trader"],
            now_utc=datetime(2026, 7, 30, 19, 45, tzinfo=UTC),
            runner=runner,
        )
        self.assertEqual(returncode, 23)
        self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main()
