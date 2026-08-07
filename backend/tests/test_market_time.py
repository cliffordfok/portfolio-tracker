from __future__ import annotations

import unittest
from datetime import UTC, date, datetime

from portfolio_tracker.market_time import (
    is_nyse_session,
    latest_completed_nyse_session,
)


class MarketTimeTests(unittest.TestCase):
    def test_latest_completed_session_requires_aware_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone"):
            latest_completed_nyse_session(datetime(2026, 7, 30, 20, 0))

    def test_nyse_session_calendar_rejects_weekends_and_holidays(self) -> None:
        self.assertFalse(is_nyse_session(date(2024, 1, 6)))
        self.assertFalse(is_nyse_session(date(2024, 1, 7)))
        self.assertFalse(is_nyse_session(date(2024, 1, 15)))
        self.assertTrue(is_nyse_session(date(2024, 1, 16)))

    def test_saturday_new_year_does_not_close_preceding_friday(self) -> None:
        self.assertTrue(is_nyse_session(date(2021, 12, 31)))
        self.assertTrue(is_nyse_session(date(2027, 12, 31)))

    def test_sunday_new_year_is_observed_on_monday(self) -> None:
        self.assertFalse(is_nyse_session(date(2023, 1, 2)))

    def test_regular_session_completes_at_official_close(self) -> None:
        self.assertEqual(
            latest_completed_nyse_session(
                datetime(2026, 7, 30, 19, 59, 59, tzinfo=UTC)
            ),
            date(2026, 7, 29),
        )
        self.assertEqual(
            latest_completed_nyse_session(
                datetime(2026, 7, 30, 20, 0, tzinfo=UTC)
            ),
            date(2026, 7, 30),
        )

    def test_early_close_session_completes_at_thirteen_hundred(self) -> None:
        self.assertEqual(
            latest_completed_nyse_session(
                datetime(2026, 11, 27, 17, 59, 59, tzinfo=UTC)
            ),
            date(2026, 11, 25),
        )
        self.assertEqual(
            latest_completed_nyse_session(
                datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
            ),
            date(2026, 11, 27),
        )


if __name__ == "__main__":
    unittest.main()
