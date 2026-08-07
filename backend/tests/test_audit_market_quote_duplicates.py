from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "audit_market_quote_duplicates.py"
)
SPEC = importlib.util.spec_from_file_location(
    "audit_market_quote_duplicates",
    SCRIPT_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load audit_market_quote_duplicates.py")
AUDIT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = AUDIT
SPEC.loader.exec_module(AUDIT)


def quote(
    event_id: str,
    *,
    source: str = "cron-quote",
    symbol: str = "AAPL",
    session_date: str = "2024-01-02",
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "source": source,
        "action": "QUOTE",
        "symbol": symbol,
        "session_date": session_date,
    }


class MarketQuoteDuplicateAuditTests(unittest.TestCase):
    def test_split_overlay_is_an_expected_duplicate(self) -> None:
        report = AUDIT.audit_events(
            [
                quote("market-quote-source"),
                quote(
                    "market-split-basis-correction",
                    source="manual-quote",
                ),
            ]
        )

        self.assertEqual(report["status"], "clean")
        self.assertEqual(report["duplicate_group_count"], 1)
        self.assertEqual(report["split_overlay_group_count"], 1)
        self.assertEqual(report["provider_retry_group_count"], 0)

    def test_random_provider_retries_require_review(self) -> None:
        report = AUDIT.audit_events(
            [
                quote("market-quote-2024-01-02-aapl-a1b2c3d4"),
                quote("market-quote-2024-01-02-aapl-e5f6a7b8"),
            ]
        )

        self.assertEqual(report["status"], "review_required")
        self.assertEqual(report["provider_retry_group_count"], 1)
        self.assertEqual(
            report["provider_retry_samples"][0]["identity"],
            "AAPL",
        )

    def test_split_overlay_does_not_hide_provider_retry(self) -> None:
        report = AUDIT.audit_events(
            [
                quote("market-quote-first"),
                quote("market-quote-retry"),
                quote(
                    "market-split-basis-correction",
                    source="manual-quote",
                ),
            ]
        )

        self.assertEqual(report["split_overlay_group_count"], 1)
        self.assertEqual(report["provider_retry_group_count"], 1)
        self.assertEqual(report["status"], "review_required")

    def test_split_overlay_does_not_hide_other_manual_correction(self) -> None:
        report = AUDIT.audit_events(
            [
                quote("market-quote-source"),
                quote(
                    "market-split-basis-correction",
                    source="manual-quote",
                ),
                quote("market-other-correction", source="manual-quote"),
            ]
        )

        self.assertEqual(report["split_overlay_group_count"], 1)
        self.assertEqual(report["other_duplicate_group_count"], 1)
        self.assertEqual(report["status"], "review_required")


if __name__ == "__main__":
    unittest.main()
