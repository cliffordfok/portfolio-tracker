from decimal import Decimal as D
import unittest
from portfolio_tracker.replay import replay_portfolio
from portfolio_tracker.snapshot import _instrument_analytics, _daily_series, _validate_analytics
from portfolio_tracker.decimal_utils import json_safe
from portfolio_tracker.errors import ValidationError
from .helpers import event

class AnalyticsTests(unittest.TestCase):
    def fixture(self):
        events = [
            event(1,'PORTFOLIO_OPEN',initial_cash='10000'),
            event(2,'BUY',symbol='AAPL',instrument_id='EQUITY:AAPL',quote_symbol='AAPL',shares='10',price='100',fee='2'),
            event(3,'SELL',symbol='AAPL',instrument_id='EQUITY:AAPL',quote_symbol='AAPL',shares='4',price='120',fee='1'),
            event(4,'INCOME_EXPENSE',symbol='AAPL',instrument_id='EQUITY:AAPL',amount='7',gross_amount='10',withholding_tax='3',income_type='DIVIDEND'),
            event(5,'CASH_FLOW',amount='500'),
        ]
        days = ['2024-01-02','2024-01-03','2024-01-04','2024-01-05']
        quotes = {'AAPL': {day: {'close':D(value),'market_price_as_of':day+'T21:00:00Z','source':'cron-quote'} for day,value in zip(days,['110','120','125','130'])}}
        return events,days,quotes

    def test_attribution_reconciles_with_nav_and_fifo_lots(self):
        events,days,quotes = self.fixture()
        result = replay_portfolio(events)
        a = _instrument_analytics(result,quotes,days,days)
        daily = _daily_series(result,quotes,days,days)
        for row,nav in zip(a['daily'],daily):
            total = sum((i['realized']+i['income']+i['unrealized'] for i in row['instruments']), D(0))
            self.assertEqual(total,nav['pnl'])
        last = a['daily'][-1]['instruments'][0]
        self.assertEqual(last['realized'],D('78.20'))
        self.assertEqual(last['income'],D('7'))
        self.assertEqual(last['unrealized'],D('178.80'))
        self.assertEqual(a['open_lots'][0]['cost_basis'],D('601.20'))
        self.assertEqual(a['open_lots'][0]['shares'],D('6'))
        _validate_analytics(json_safe(a),json_safe(daily))

    def test_missing_price_is_null_and_recovers_without_zero(self):
        events,days,quotes = self.fixture()
        del quotes['AAPL'][days[1]]
        a = _instrument_analytics(replay_portfolio(events),quotes,days,days)
        self.assertIsNone(a['daily'][1]['instruments'][0]['unrealized'])
        self.assertEqual(a['daily'][2]['instruments'][0]['unrealized'],D('148.80'))

    def test_backdated_and_amended_events_keep_canonical_replay(self):
        events,days,quotes = self.fixture()
        events.append(event(6,'AMEND',amend_target='paper-2',changes={'fee':'4'}))
        events.append(event(7,'INCOME_EXPENSE',occurred_at='2024-01-03T16:00:00Z',symbol='USD',amount='2',withholding_tax='0',income_type='INTEREST'))
        result = replay_portfolio(events)
        a = _instrument_analytics(result,quotes,days,days)
        daily = _daily_series(result,quotes,days,days)
        self.assertEqual(sum(i['realized']+i['income']+i['unrealized'] for i in a['daily'][-1]['instruments']),daily[-1]['pnl'])

    def test_analytics_validation_rejects_duplicate_identity(self):
        events,days,quotes = self.fixture()
        result = replay_portfolio(events)
        a = json_safe(_instrument_analytics(result,quotes,days,days))
        a['daily'][0]['instruments'] *= 2
        with self.assertRaises(ValidationError):
            _validate_analytics(a,json_safe(_daily_series(result,quotes,days,days)))

    def test_split_and_full_exit_preserve_cost_and_total(self):
        events, days, quotes = self.fixture()
        events = events[:2] + [
            event(3, 'SPLIT', symbol='AAPL', instrument_id='EQUITY:AAPL', numerator='2', denominator='1'),
            event(4, 'SELL', symbol='AAPL', instrument_id='EQUITY:AAPL', quote_symbol='AAPL', shares='20', price='60', fee='2'),
        ]
        quotes['AAPL'][days[1]]['close'] = D('60')
        result = replay_portfolio(events)
        a = _instrument_analytics(result, quotes, days, days)
        self.assertEqual(a['daily'][1]['instruments'][0]['unrealized'], D('198'))
        self.assertEqual(a['daily'][-1]['instruments'][0]['realized'], D('196'))
        self.assertEqual(a['daily'][-1]['instruments'][0]['unrealized'], D('0'))
        self.assertEqual(a['open_lots'], [])

    def test_option_multiplier_uses_contract_quote(self):
        events, days, _ = self.fixture()
        events = [events[0], event(2, 'BUY', symbol='AAPL', instrument_id='OPTION:AAPL', instrument_type='OPTION', shares='2', price='3', fee='2', contract_multiplier='100')]
        quotes = {'OPTION:AAPL': {day: {'close':D('4'), 'source':'manual-quote', 'market_price_as_of':day+'T21:00:00Z'} for day in days}}
        a = _instrument_analytics(replay_portfolio(events), quotes, days, days)
        self.assertEqual(a['daily'][-1]['instruments'][0]['unrealized'], D('198'))
        self.assertEqual(a['open_lots'][0]['cost_basis'], D('602'))
        self.assertEqual(a['open_lots'][0]['contract_multiplier'], D('100'))
