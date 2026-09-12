import { numeric, filterByRange, rangeStartDate } from './utils.js';
import { portfolioRangeEndDate } from './data.js';

export function contributionModel(portfolio, range) {
  const history = portfolio.analytics?.version === 1 ? portfolio.analytics.daily : null;
  const end = portfolioRangeEndDate(portfolio);
  if (!history?.length || !end) return null;
  const selected = filterByRange(history, range, row => row.date, end);
  if (!selected.length || selected.at(-1).date !== end) return null;
  const start = rangeStartDate(range, end);
  const baseline = start ? history.filter(row => row.date < start).at(-1) : null;
  const before = new Map((baseline?.instruments || []).map(row => [row.instrument_id, row]));
  const after = new Map(selected.at(-1).instruments.map(row => [row.instrument_id, row]));
  const rows = [...new Set([...before.keys(), ...after.keys()])].map(id => {
    const a = after.get(id), b = before.get(id);
    const difference = field => {
      const right = a ? numeric(a[field]) : 0;
      const left = b ? numeric(b[field]) : 0;
      return right === null || left === null ? null : right - left;
    };
    const realized = difference('realized'), unrealized = difference('unrealized');
    const income = difference('income'), fees = difference('fees');
    return { id, symbol: (a || b).symbol, realized, unrealized, income, fees,
      total: [realized, unrealized, income].includes(null) ? null : realized + unrealized + income };
  }).sort((a,b) => (b.total ?? -Infinity) - (a.total ?? -Infinity));
  return { start: selected[0].date, end, baseline: baseline?.date || null, rows,
    total: rows.some(row => row.total === null) ? null : rows.reduce((sum,row) => sum + row.total, 0) };
}

export function monthlyModel(portfolio, benchmark, range) {
  if (portfolio.data_status === 'FALLBACK') return [];
  const selected = filterByRange(portfolio.daily || [], range, row => row.date, portfolioRangeEndDate(portfolio));
  const benchmarkMap = new Map((benchmark?.daily || []).map(row => [row.date, row]));
  const groups = new Map();
  for (const row of selected) {
    const key = row.date.slice(0,7);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  const compound = rows => {
    if (!rows.length || rows.some(row => !row || row.data_status !== 'OK' || numeric(row.daily_return) === null)) return null;
    if (new Set(rows.map(row => row.segment_id)).size !== 1) return null;
    return rows.reduce((value,row) => value * (1 + numeric(row.daily_return)), 1) - 1;
  };
  return [...groups].map(([month, rows]) => {
    const result = compound(rows);
    const aligned = rows.map(row => benchmarkMap.get(row.date));
    // The opening valuation is the portfolio's base, not a measured day return.
    // Rebase SPY on that same date instead of including its prior-day movement.
    const openingBase = rows[0].date === portfolio.daily[0]?.date && numeric(rows[0].daily_return) === 0;
    if (openingBase && aligned[0]?.data_status === 'OK') {
      aligned[0] = { ...aligned[0], daily_return: '0' };
    }
    const spy = compound(aligned);
    return { month, start: rows[0].date, end: rows.at(-1).date, result, spy,
      excess: result === null || spy === null ? null : result - spy };
  }).reverse();
}

export function instrumentModel(portfolio, id) {
  const trades = (portfolio.recent_trades || []).filter(row => (row.instrument_id || row.symbol) === id)
    .sort((a,b) => (a.occurred_at || a.date).localeCompare(b.occurred_at || b.date));
  const holding = (portfolio.holdings || []).find(row => (row.instrument_id || row.symbol) === id);
  const latest = portfolio.analytics?.daily?.at(-1)?.instruments.find(row => row.instrument_id === id);
  return { id, symbol: holding?.symbol || trades[0]?.symbol || latest?.symbol || id,
    holding, latest, trades,
    lots: portfolio.analytics?.version === 1 ? portfolio.analytics.open_lots.filter(row => row.instrument_id === id) : null };
}
