from __future__ import annotations

import unittest

from portfolio_tracker.errors import BusinessInvariantError
from portfolio_tracker.resolver import resolve_effective_events

from .helpers import event


class ResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.open = event(1, "PORTFOLIO_OPEN", initial_cash="10000")
        self.buy = event(
            2,
            "BUY",
            symbol="AAPL",
            shares="10",
            price="100",
            fee="1",
            note="first",
        )

    def test_multiple_amends_are_per_field_last_write_wins(self) -> None:
        events = [
            self.open,
            self.buy,
            event(
                3,
                "AMEND",
                amend_target="paper-2",
                changes={
                    "fee": "2",
                    "settlement_adjustment": "0.004",
                    "note": "corrected",
                },
            ),
            event(
                4,
                "AMEND",
                amend_target="paper-2",
                changes={"note": "final"},
            ),
        ]
        effective = resolve_effective_events(events)
        resolved_buy = effective[1]
        self.assertEqual(resolved_buy["fee"], "2")
        self.assertEqual(
            resolved_buy["settlement_adjustment"],
            "0.004",
        )
        self.assertEqual(resolved_buy["note"], "final")

    def test_void_after_amend_removes_original_event(self) -> None:
        events = [
            self.open,
            self.buy,
            event(3, "AMEND", amend_target="paper-2", changes={"fee": "2"}),
            event(4, "VOID", void_target="paper-2"),
        ]
        effective = resolve_effective_events(events)
        self.assertEqual([item["action"] for item in effective], ["PORTFOLIO_OPEN"])

    def test_void_of_amend_voids_underlying_economic_event(self) -> None:
        amend = event(3, "AMEND", amend_target="paper-2", changes={"fee": "2"})
        effective = resolve_effective_events(
            [
                self.open,
                self.buy,
                amend,
                event(4, "VOID", void_target="paper-3"),
            ]
        )
        self.assertEqual([item["action"] for item in effective], ["PORTFOLIO_OPEN"])

    def test_void_of_void_is_rejected(self) -> None:
        with self.assertRaisesRegex(BusinessInvariantError, "another VOID"):
            resolve_effective_events(
                [
                    self.open,
                    self.buy,
                    event(3, "VOID", void_target="paper-2"),
                    event(4, "VOID", void_target="paper-3"),
                ]
            )

    def test_amend_after_void_is_rejected(self) -> None:
        with self.assertRaisesRegex(BusinessInvariantError, "AMEND after VOID"):
            resolve_effective_events(
                [
                    self.open,
                    self.buy,
                    event(3, "VOID", void_target="paper-2"),
                    event(4, "AMEND", amend_target="paper-2", changes={"fee": "2"}),
                ]
            )

    def test_duplicate_void_is_rejected(self) -> None:
        with self.assertRaisesRegex(BusinessInvariantError, "duplicate VOID"):
            resolve_effective_events(
                [
                    self.open,
                    self.buy,
                    event(3, "VOID", void_target="paper-2"),
                    event(4, "VOID", void_target="paper-2"),
                ]
            )

    def test_correction_replay_matches_clean_ledger(self) -> None:
        corrected = resolve_effective_events(
            [
                self.open,
                self.buy,
                event(3, "AMEND", amend_target="paper-2", changes={"fee": "2"}),
            ]
        )
        clean_buy = dict(self.buy)
        clean_buy["fee"] = "2"
        clean = resolve_effective_events([self.open, clean_buy])
        self.assertEqual(corrected, clean)


if __name__ == "__main__":
    unittest.main()
