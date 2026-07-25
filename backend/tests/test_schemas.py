from __future__ import annotations

import unittest
from datetime import UTC, datetime

from seed_demo import market_events, portfolio_events
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

    def test_future_event_beyond_clock_skew_is_rejected(self) -> None:
        value = candidate(
            "CASH_FLOW",
            portfolio="paper",
            event_id="paper-cash-future",
            occurred_at="2024-01-02T15:10:01Z",
            amount="1",
        )
        with self.assertRaisesRegex(ValidationError, "cannot be in the future"):
            validate_event(
                value,
                now=datetime(2024, 1, 2, 15, 0, tzinfo=UTC),
            )

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

    def test_trade_fee_is_required(self) -> None:
        value = candidate(
            "BUY",
            portfolio="paper",
            event_id="paper-buy-no-fee",
            occurred_at="2024-01-02T15:00:00Z",
            symbol="AAPL",
            shares="1",
            price="100",
        )
        with self.assertRaisesRegex(ValidationError, "fee"):
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

    def test_cash_flow_requires_usd_symbol(self) -> None:
        value = candidate(
            "CASH_FLOW",
            portfolio="paper",
            event_id="paper-cash-hkd",
            occurred_at="2024-01-02T15:00:00Z",
            symbol="HKD",
            amount="100",
        )
        with self.assertRaisesRegex(ValidationError, "symbol must be USD"):
            validate_event(value)

    def test_source_must_use_the_frozen_audit_vocabulary(self) -> None:
        value = candidate(
            "PORTFOLIO_OPEN",
            portfolio="paper",
            event_id="paper-open-bad-source",
            occurred_at="2024-01-02T15:00:00Z",
            initial_cash="1000",
        )
        value["source"] = "unknown-agent"
        with self.assertRaisesRegex(ValidationError, "source must be one of"):
            validate_event(value)

    def test_corrections_require_a_non_empty_audit_reason(self) -> None:
        amend = candidate(
            "AMEND",
            portfolio="paper",
            event_id="paper-amend-no-reason",
            occurred_at="2024-01-02T15:00:00Z",
            amend_target="paper-buy-1",
            changes={"fee": "1"},
            amend_reason=" ",
        )
        with self.assertRaisesRegex(ValidationError, "amend_reason"):
            validate_event(amend)

        void = candidate(
            "VOID",
            portfolio="paper",
            event_id="paper-void-no-reason",
            occurred_at="2024-01-02T15:00:00Z",
            void_target="paper-buy-1",
            void_reason="",
        )
        with self.assertRaisesRegex(ValidationError, "void_reason"):
            validate_event(void)

    def test_benchmark_close_requires_spy_symbol(self) -> None:
        value = candidate(
            "BENCHMARK_CLOSE",
            portfolio="market",
            event_id="market-benchmark-aapl",
            occurred_at="2024-01-02T21:00:00Z",
            symbol="AAPL",
            close="100",
            session_date="2024-01-02",
        )
        with self.assertRaisesRegex(ValidationError, "symbol must be SPY"):
            validate_event(value)

    def test_demo_seed_events_follow_master_schema(self) -> None:
        for value in portfolio_events() + market_events():
            with self.subTest(event_id=value["event_id"]):
                validate_event(value)


if __name__ == "__main__":
    unittest.main()
