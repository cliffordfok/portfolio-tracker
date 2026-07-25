from __future__ import annotations

import unittest

from portfolio_tracker.errors import ValidationError
from portfolio_tracker.schemas import validate_event

from .helpers import candidate


class SchemaTests(unittest.TestCase):
    def test_brk_dot_b_is_accepted(self) -> None:
        value = candidate(
            "BUY",
            portfolio="paper",
            event_id="paper-buy-brkb",
            occurred_at="2024-01-02T15:00:00Z",
            symbol="BRK.B",
            shares="1",
            price="350",
            fee="0",
        )
        validate_event(value)

    def test_symbol_outside_frozen_contract_is_rejected(self) -> None:
        value = candidate(
            "BUY",
            portfolio="paper",
            event_id="paper-buy-invalid",
            occurred_at="2024-01-02T15:00:00Z",
            symbol="AAPL1",
            shares="1",
            price="100",
            fee="0",
        )
        with self.assertRaisesRegex(ValidationError, "symbol must match"):
            validate_event(value)

    def test_timestamp_requires_z_suffix(self) -> None:
        value = candidate(
            "CASH_FLOW",
            portfolio="paper",
            event_id="paper-cash-offset",
            occurred_at="2024-01-02T15:00:00+00:00",
            amount="1",
        )
        with self.assertRaisesRegex(ValidationError, "ISO8601 UTC"):
            validate_event(value)

    def test_master_event_cannot_store_derived_pnl(self) -> None:
        value = candidate(
            "SELL",
            portfolio="paper",
            event_id="paper-sell-derived",
            occurred_at="2024-01-02T15:00:00Z",
            symbol="AAPL",
            shares="1",
            price="100",
            fee="0",
            pnl="12.34",
        )
        with self.assertRaisesRegex(ValidationError, "derived fields"):
            validate_event(value)

    def test_portfolio_open_requires_usd_currency(self) -> None:
        value = candidate(
            "PORTFOLIO_OPEN",
            portfolio="paper",
            event_id="paper-open-eur",
            occurred_at="2024-01-01T14:00:00Z",
            initial_cash="1000",
            currency="EUR",
        )
        with self.assertRaisesRegex(ValidationError, "currency must be USD"):
            validate_event(value)

    def test_shares_over_eight_decimal_places_are_rejected(self) -> None:
        value = candidate(
            "BUY",
            portfolio="paper",
            event_id="paper-buy-precision",
            occurred_at="2024-01-02T15:00:00Z",
            symbol="AAPL",
            shares="0.123456789",
            price="100",
            fee="0",
        )
        with self.assertRaisesRegex(ValueError, "at most 8 decimal places"):
            validate_event(value)


if __name__ == "__main__":
    unittest.main()
