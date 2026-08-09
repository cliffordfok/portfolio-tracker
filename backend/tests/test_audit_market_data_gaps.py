from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "audit_market_data_gaps.py"
)
SPEC = importlib.util.spec_from_file_location(
    "audit_market_data_gaps", SCRIPT_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load audit_market_data_gaps.py")
AUDIT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = AUDIT
SPEC.loader.exec_module(AUDIT)


def manifest(*gaps: tuple[str, str, str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "decisions": [
            {
                "decision_id": "accepted-test-gaps",
                "status": "accepted",
                "accepted_on": "2026-08-09",
                "portfolio": "live",
                "decision": "do_not_backfill",
                "reason": "test decision",
                "expected_snapshot": {
                    "performance_effective_date": "2025-09-17",
                    "performance_scope": "LATEST_COMPLETE_SEGMENT",
                },
                "gaps": [
                    {
                        "instrument_id": instrument_id,
                        "symbol": symbol,
                        "session_date": session,
                    }
                    for instrument_id, symbol, session in gaps
                ],
            }
        ],
    }


def snapshot(
    *gaps: tuple[str, str], effective_date: str = "2025-09-17"
) -> dict[str, object]:
    return {
        "revision": 10,
        "benchmark": {"symbol": "SPY", "daily": []},
        "portfolios": {
            "paper": {"daily": [], "metrics": {}},
            "live": {
                "daily": [
                    {
                        "date": session,
                        "data_status": "INSUFFICIENT_MARKET_DATA",
                        "missing_symbols": [symbol],
                    }
                    for symbol, session in gaps
                ],
                "metrics": {
                    "performance_effective_date": effective_date,
                    "performance_scope": "LATEST_COMPLETE_SEGMENT",
                },
            },
        },
    }


class MarketDataGapAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.accepted = (
            "OPTION:AAPL:2025-10-10:P:230",
            "AAPL",
            "2025-09-11",
        )

    def test_exact_known_gap_is_accepted(self) -> None:
        report = AUDIT.audit_snapshot(
            snapshot(("AAPL", "2025-09-11")), manifest(self.accepted)
        )

        self.assertEqual(report["status"], "accepted_with_known_gaps")
        self.assertEqual(report["accepted_present_count"], 1)
        self.assertEqual(report["unreviewed_gap_count"], 0)
        self.assertEqual(report["resolved_acceptance_count"], 0)

    def test_new_gap_requires_review(self) -> None:
        report = AUDIT.audit_snapshot(
            snapshot(
                ("AAPL", "2025-09-11"),
                ("MSFT", "2025-09-12"),
            ),
            manifest(self.accepted),
        )

        self.assertEqual(report["status"], "review_required")
        self.assertEqual(report["unreviewed_gap_count"], 1)
        self.assertEqual(report["unreviewed_gaps"][0]["symbol"], "MSFT")

    def test_resolved_gap_marks_acceptance_stale(self) -> None:
        report = AUDIT.audit_snapshot(snapshot(), manifest(self.accepted))

        self.assertEqual(report["status"], "stale_acceptance")
        self.assertEqual(report["resolved_acceptance_count"], 1)

    def test_non_session_carry_forward_is_ignored(self) -> None:
        report = AUDIT.audit_snapshot(
            snapshot(
                ("AAPL", "2025-09-11"),
                ("AAPL", "2025-09-13"),
            ),
            manifest(self.accepted),
        )

        self.assertEqual(report["status"], "accepted_with_known_gaps")
        self.assertEqual(report["actual_gap_count"], 1)

    def test_expected_metric_drift_requires_review(self) -> None:
        report = AUDIT.audit_snapshot(
            snapshot(
                ("AAPL", "2025-09-11"),
                effective_date="2026-01-02",
            ),
            manifest(self.accepted),
        )

        self.assertEqual(report["status"], "review_required")
        self.assertEqual(report["metric_mismatch_count"], 1)

    def test_duplicate_accepted_key_is_invalid(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate accepted"):
            AUDIT.parse_acceptance_manifest(
                manifest(self.accepted, self.accepted)
            )


if __name__ == "__main__":
    unittest.main()
