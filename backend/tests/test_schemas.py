from __future__ import annotations

import unittest
from datetime import UTC, datetime

from seed_demo import market_events, portfolio_events
from portfolio_tracker.errors import ValidationError
from portfolio_tracker.schemas import (
    default_live_trade_identity,
    normalize_event,
    validate_event,
    validate_intake_instrument_identity,
    validate_market_event_intake,
)

from .helpers import candidate


class SchemaTests(unittest.TestCase):
    def test_live_identity_defaults_distinguish_known_etfs(self) -> None:
        self.assertEqual(
            default_live_trade_identity("VOO"),
            {
                "instrument_id": "ETF:VOO",
                "instrument_type": "ETF",
                "quote_symbol": "VOO",
            },
        )
        self.assertEqual(
            default_live_trade_identity("AAPL"),
            {
                "instrument_id": "EQUITY:AAPL",
                "instrument_type": "EQUITY",
                "quote_symbol": "AAPL",
            },
        )

    def test_intake_rejects_listed_security_as_private(self) -> None:
        value = candidate(
            "BUY",
            portfolio="live",
            event_id="live-buy-cbrs-private",
            occurred_at="2026-05-18T15:00:00Z",
            symbol="CBRS",
            instrument_id="PRIVATE:CEREBRAS",
            instrument_type="PRIVATE",
            quote_symbol=None,
            shares="1",
            price="200",
            fee="0",
        )
        validate_event(value, allow_future=True)
        with self.assertRaisesRegex(
            ValidationError,
            "CBRS must use EQUITY:CBRS",
        ):
            validate_intake_instrument_identity(value)

    def test_intake_rejects_retired_when_issued_symbol(self) -> None:
        before = candidate(
            "BUY",
            portfolio="live",
            event_id="live-buy-skhyv-before",
            occurred_at="2026-07-10T15:00:00Z",
            symbol="SKHYV",
            instrument_id="EQUITY:SKHYV",
            instrument_type="EQUITY",
            quote_symbol="SKHYV",
            shares="1",
            price="170",
            fee="0",
        )
        validate_intake_instrument_identity(before)
        after = {
            **before,
            "event_id": "live-buy-skhyv-after",
            "occurred_at": "2026-07-13T15:00:00Z",
            "created_at": "2026-07-13T15:00:00Z",
        }
        with self.assertRaisesRegex(
            ValidationError,
            "SKHYV is retired; use SKHY",
        ):
            validate_intake_instrument_identity(after)

    def test_intake_requires_option_quotes_by_instrument_id(self) -> None:
        value = candidate(
            "BUY",
            portfolio="live",
            event_id="live-buy-amd-option",
            occurred_at="2026-07-01T15:00:00Z",
            symbol="AMD",
            instrument_id="OPTION:AMD:2026-12-18:C:200",
            instrument_type="OPTION",
            quote_symbol="AMD",
            contract_multiplier="100",
            shares="1",
            price="10",
            fee="0",
        )
        validate_event(value, allow_future=True)
        with self.assertRaisesRegex(
            ValidationError,
            "OPTION quote_symbol must be omitted",
        ):
            validate_intake_instrument_identity(value)

    def test_intake_rejects_instrument_prefix_mismatch(self) -> None:
        value = candidate(
            "BUY",
            portfolio="live",
            event_id="live-buy-voo-wrong-prefix",
            occurred_at="2026-07-01T15:00:00Z",
            symbol="VOO",
            instrument_id="EQUITY:VOO",
            instrument_type="ETF",
            quote_symbol="VOO",
            shares="1",
            price="600",
            fee="0",
        )
        with self.assertRaisesRegex(
            ValidationError,
            "prefix must match",
        ):
            validate_intake_instrument_identity(value)

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

    def test_unknown_master_fields_are_rejected(self) -> None:
        value = candidate(
            "BUY",
            portfolio="paper",
            event_id="paper-buy-secret-field",
            occurred_at="2024-01-02T15:00:00Z",
            symbol="AAPL",
            shares="1",
            price="100",
            fee="0",
            account_number="must-not-enter-ledger",
        )
        with self.assertRaisesRegex(ValidationError, "unknown fields for BUY"):
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

    def test_trade_settlement_adjustment_is_small_and_normalized(self) -> None:
        value = candidate(
            "SELL",
            portfolio="live",
            event_id="live-sell-rounding",
            occurred_at="2024-01-02T15:00:00Z",
            symbol="ONDS",
            shares="150",
            price="11.0001",
            fee="0.03",
            settlement_adjustment=("-0.005"),
        )
        validate_event(value)
        self.assertEqual(
            normalize_event(value)["settlement_adjustment"],
            "-0.005",
        )

        invalid = dict(value, settlement_adjustment="-0.010001")
        with self.assertRaisesRegex(
            ValidationError,
            "settlement_adjustment must be between",
        ):
            validate_event(invalid)

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

    def test_automated_actions_require_their_canonical_source(self) -> None:
        cases = (
            (
                candidate(
                    "PORTFOLIO_OPEN",
                    portfolio="paper",
                    event_id="paper-open-wrong-source",
                    occurred_at="2024-01-02T15:00:00Z",
                    initial_cash="1000",
                ),
                "manual-import",
                "PORTFOLIO_OPEN source must be bootstrap",
            ),
            (
                candidate(
                    "QUOTE",
                    portfolio="market",
                    event_id="market-quote-wrong-source",
                    occurred_at="2024-01-02T21:00:00Z",
                    symbol="AAPL",
                    close="100",
                    session_date="2024-01-02",
                ),
                "manual-import",
                "QUOTE source must be cron-quote or manual-quote",
            ),
            (
                candidate(
                    "BENCHMARK_CLOSE",
                    portfolio="market",
                    event_id="market-benchmark-wrong-source",
                    occurred_at="2024-01-02T21:00:00Z",
                    symbol="SPY",
                    close="470",
                    session_date="2024-01-02",
                ),
                "cron-quote",
                "BENCHMARK_CLOSE source must be cron-benchmark",
            ),
        )
        for value, invalid_source, message in cases:
            with self.subTest(action=value["action"]):
                value["source"] = invalid_source
                with self.assertRaisesRegex(ValidationError, message):
                    validate_event(value)

    def test_option_trade_accepts_stable_instrument_identity(self) -> None:
        value = candidate(
            "BUY",
            portfolio="live",
            event_id="live-option-amd",
            occurred_at="2024-01-02T15:00:00Z",
            symbol="AMD",
            instrument_id="OPTION:AMD:2024-03-15:C:165",
            instrument_type="OPTION",
            instrument_name="AMD 2024-03-15 CALL 165",
            quote_symbol="AMD",
            contract_multiplier="100",
            shares="1",
            price="15.4",
            fee="0.02",
        )
        validate_event(value)
        missing_identity = dict(value)
        del missing_identity["instrument_id"]
        with self.assertRaisesRegex(ValidationError, "stable instrument_id"):
            validate_event(missing_identity)

    def test_income_amount_must_be_net_of_withholding(self) -> None:
        valid = candidate(
            "INCOME_EXPENSE",
            portfolio="live",
            event_id="live-dividend-voo",
            occurred_at="2024-01-02T15:00:00Z",
            symbol="VOO",
            instrument_id="ETF:VOO",
            amount="4.58",
            gross_amount="6.54",
            withholding_tax="1.96",
            income_type="DIVIDEND",
        )
        validate_event(valid)
        invalid = dict(valid, amount="6.54")
        with self.assertRaisesRegex(ValidationError, "gross_amount minus"):
            validate_event(invalid)

    def test_split_requires_real_ratio_and_instrument(self) -> None:
        valid = candidate(
            "SPLIT",
            portfolio="live",
            event_id="live-split-tsla",
            occurred_at="2024-01-02T15:00:00Z",
            symbol="TSLA",
            instrument_id="EQUITY:TSLA",
            numerator="3",
            denominator="1",
        )
        validate_event(valid)
        invalid = dict(valid, numerator="1")
        with self.assertRaisesRegex(ValidationError, "must change"):
            validate_event(invalid)

    def test_private_manual_quote_uses_instrument_id(self) -> None:
        value = candidate(
            "QUOTE",
            portfolio="market",
            event_id="market-acme-manual",
            occurred_at="2024-01-02T21:00:00Z",
            source="manual-quote",
            symbol="ACME",
            instrument_id="PRIVATE:ACME",
            close="210",
            session_date="2024-01-02",
        )
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

    def test_market_session_date_must_be_a_real_calendar_date(self) -> None:
        value = candidate(
            "QUOTE",
            portfolio="market",
            event_id="market-quote-invalid-date",
            occurred_at="2024-02-28T21:00:00Z",
            symbol="AAPL",
            close="100",
            session_date="2024-02-31",
        )
        with self.assertRaisesRegex(ValidationError, "real calendar date"):
            validate_event(value)

    def test_market_intake_rejects_non_session_and_future_session(self) -> None:
        value = candidate(
            "QUOTE",
            portfolio="market",
            event_id="market-quote-session-gate",
            occurred_at="2024-01-05T21:00:00Z",
            symbol="AAPL",
            close="100",
            session_date="2024-01-06",
        )
        for session_date in ("2024-01-06", "2024-01-07", "2024-01-15"):
            with self.subTest(session_date=session_date):
                with self.assertRaisesRegex(
                    ValidationError,
                    "NYSE trading session",
                ):
                    validate_market_event_intake(
                        {**value, "session_date": session_date},
                        now=datetime(2024, 1, 16, 21, 0, tzinfo=UTC),
                    )

        with self.assertRaisesRegex(
            ValidationError,
            "latest completed NYSE session",
        ):
            validate_market_event_intake(
                {**value, "session_date": "2024-01-08"},
                now=datetime(2024, 1, 5, 21, 0, tzinfo=UTC),
            )

    def test_market_intake_accepts_a_completed_historical_session(self) -> None:
        value = candidate(
            "BENCHMARK_CLOSE",
            portfolio="market",
            event_id="market-benchmark-completed-session",
            occurred_at="2024-01-02T21:00:00Z",
            symbol="SPY",
            close="470",
            session_date="2024-01-02",
        )

        validate_market_event_intake(
            value,
            now=datetime(2024, 1, 8, 21, 0, tzinfo=UTC),
        )

    def test_demo_seed_events_follow_master_schema(self) -> None:
        for value in portfolio_events() + market_events():
            with self.subTest(event_id=value["event_id"]):
                validate_event(value)


if __name__ == "__main__":
    unittest.main()
