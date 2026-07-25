from __future__ import annotations

import unittest
from decimal import Decimal

from portfolio_tracker.errors import BusinessInvariantError
from portfolio_tracker.replay import replay_portfolio

from .helpers import event


class ReplayTests(unittest.TestCase):
    def fixture(self) -> list[dict]:
        return [
            event(1, "PORTFOLIO_OPEN", initial_cash="1000"),
            event(2, "BUY", symbol="AAPL", shares="10", price="10", fee="1"),
            event(3, "BUY", symbol="AAPL", shares="5", price="20", fee="0.5"),
            event(4, "SELL", symbol="AAPL", shares="12", price="30", fee="1.2"),
        ]

    def test_fifo_fee_allocation_and_realized_pnl(self) -> None:
        result = replay_portfolio(self.fixture())
        self.assertEqual(result.realized_pnl_total, Decimal("217.600000"))
        self.assertEqual(result.cash, Decimal("1157.300000"))
        self.assertEqual(result.holdings[0]["shares"], Decimal("3.00000000"))
        self.assertEqual(result.holdings[0]["cost_basis"], Decimal("60.300000"))
        self.assertEqual(result.holdings[0]["avg_cost"], Decimal("20.100000"))
        matches = result.realized_pnl_per_trade[0]["matches"]
        self.assertEqual(matches[0]["sell_fee"], Decimal("1.000000"))
        self.assertEqual(matches[1]["sell_fee"], Decimal("0.200000"))
        self.assertEqual(matches[1]["buy_fee"], Decimal("0.200000"))

    def test_cash_conservation(self) -> None:
        result = replay_portfolio(self.fixture())
        expected = (
            result.initial_cash
            + result.cash_flow_total
            - result.buy_outflow
            + result.sell_inflow
        )
        self.assertEqual(result.cash, expected)

    def test_closed_episode_win_rate(self) -> None:
        events = self.fixture() + [
            event(5, "SELL", symbol="AAPL", shares="3", price="15", fee="0")
        ]
        result = replay_portfolio(events)
        self.assertEqual(len(result.closed_episodes), 1)
        self.assertEqual(result.closed_episodes[0]["pnl"], Decimal("202.300000"))
        self.assertEqual(result.win_rate, Decimal("1.00000000"))

    def test_oversell_rejected(self) -> None:
        events = [
            event(1, "PORTFOLIO_OPEN", initial_cash="1000"),
            event(2, "SELL", symbol="AAPL", shares="1", price="10", fee="0"),
        ]
        with self.assertRaisesRegex(BusinessInvariantError, "oversells"):
            replay_portfolio(events)

    def test_negative_cash_rejected(self) -> None:
        events = [
            event(1, "PORTFOLIO_OPEN", initial_cash="10"),
            event(2, "BUY", symbol="AAPL", shares="2", price="10", fee="0"),
        ]
        with self.assertRaisesRegex(BusinessInvariantError, "cash negative"):
            replay_portfolio(events)


if __name__ == "__main__":
    unittest.main()
