import { dateOnly, numeric } from "./utils.js";

async function fetchJson(url) {
  const requestUrl = new URL(url, window.location.href);
  requestUrl.searchParams.set("_", Date.now().toString());
  const response = await fetch(requestUrl, {
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function validateSnapshot(snapshot) {
  if (
    !snapshot ||
    Number(snapshot.schema_version) < 3 ||
    !snapshot.portfolios?.paper ||
    !snapshot.portfolios?.live ||
    !Array.isArray(snapshot.benchmark?.daily)
  ) {
    throw new Error("快照格式不正確");
  }
  return snapshot;
}

export function calculateFallbackPortfolio(trades, initialCash) {
  const lots = new Map();
  const latestPrice = new Map();
  const history = [];
  let cash = initialCash;
  let realized = 0;

  const sorted = trades
    .map((trade, index) => ({ ...trade, _index: index }))
    .sort((a, b) => a.date.localeCompare(b.date) || a._index - b._index);

  for (const trade of sorted) {
    const symbol = String(trade.symbol || "").toUpperCase();
    const action = String(trade.action || "").toUpperCase();
    const quantity = numeric(trade.shares) || 0;
    const price = numeric(trade.price) || 0;
    const fee = numeric(trade.fee) || 0;
    latestPrice.set(symbol, numeric(trade.current_price) ?? price);
    if (!lots.has(symbol)) lots.set(symbol, []);

    if (action === "BUY") {
      cash -= quantity * price + fee;
      lots.get(symbol).push({
        remaining: quantity,
        original: quantity,
        price,
        feeRemaining: fee,
      });
      history.push({ ...trade, symbol, action, pnl: null, pnl_pct: null });
      continue;
    }

    let remaining = quantity;
    let tradePnl = 0;
    let matchedCost = 0;
    const symbolLots = lots.get(symbol);
    for (const lot of symbolLots) {
      if (remaining <= 0) break;
      if (lot.remaining <= 0) continue;
      const matched = Math.min(remaining, lot.remaining);
      const finalLotPiece = matched === lot.remaining;
      const buyFee = finalLotPiece
        ? lot.feeRemaining
        : (lot.feeRemaining * matched) / lot.remaining;
      const sellFee = (fee * matched) / quantity;
      const cost = matched * lot.price + buyFee;
      const proceeds = matched * price - sellFee;
      tradePnl += proceeds - cost;
      matchedCost += cost;
      lot.remaining -= matched;
      lot.feeRemaining -= buyFee;
      remaining -= matched;
    }
    if (remaining > 1e-10) {
      throw new Error(`SELL oversells ${symbol} by ${remaining} shares`);
    }
    cash += quantity * price - fee;
    realized += tradePnl;
    history.push({
      ...trade,
      symbol,
      action,
      pnl: tradePnl,
      pnl_pct: matchedCost ? tradePnl / matchedCost : null,
      cumulative_pnl: realized,
    });
  }

  const holdings = [];
  for (const [symbol, symbolLots] of lots) {
    const openLots = symbolLots.filter((lot) => lot.remaining > 1e-10);
    const quantity = openLots.reduce((sum, lot) => sum + lot.remaining, 0);
    if (!quantity) continue;
    const costBasis = openLots.reduce(
      (sum, lot) => sum + lot.remaining * lot.price + lot.feeRemaining,
      0,
    );
    const currentPrice = latestPrice.get(symbol);
    const marketValue = currentPrice == null ? null : quantity * currentPrice;
    holdings.push({
      symbol,
      shares: quantity,
      avg_cost: costBasis / quantity,
      cost_basis: costBasis,
      current_price: currentPrice,
      market_value: marketValue,
      unrealized_pnl: marketValue == null ? null : marketValue - costBasis,
      unrealized_pnl_pct:
        marketValue == null || !costBasis ? null : (marketValue - costBasis) / costBasis,
    });
  }

  const realizedTimeline = history
    .filter((trade) => trade.pnl != null)
    .map((trade) => ({
      date: dateOnly(trade.date),
      pnl: trade.cumulative_pnl,
      cumulative_return: trade.cumulative_pnl / initialCash,
      segment_id: 1,
      data_status: "OK",
    }));

  const marketValue = holdings.reduce(
    (sum, holding) => sum + (holding.market_value || 0),
    0,
  );
  const unrealized = holdings.reduce(
    (sum, holding) => sum + (holding.unrealized_pnl || 0),
    0,
  );
  const totalPnl = realized + unrealized;

  return {
    data_status: "FALLBACK",
    initial_cash: initialCash,
    cash,
    holdings,
    recent_trades: history.reverse(),
    daily: realizedTimeline,
    metrics: {
      data_status: "FALLBACK",
      realized_pnl: realized,
      total_return: totalPnl / initialCash,
      win_rate: null,
      max_drawdown: null,
      sharpe_ratio: null,
      closed_episodes: 0,
    },
    estimated_nav: cash + marketValue,
  };
}

export function normalizeBenchmark(rows) {
  if (!rows.length) return [];
  const first = numeric(rows[0].close);
  return rows.map((row, index) => {
    const close = numeric(row.close);
    const previous = index ? numeric(rows[index - 1].close) : null;
    return {
      date: dateOnly(row.date),
      close,
      daily_return: previous ? close / previous - 1 : null,
      cumulative_return: first ? close / first - 1 : null,
      data_status: "OK",
    };
  });
}

function portfolioReturnValue(point) {
  return numeric(point.cumulative_return) ?? numeric(point.segment_return);
}

export function buildCommonComparison(paper, live, benchmark) {
  const paperMap = new Map(
    paper
      .filter((point) => point.data_status === "OK" && portfolioReturnValue(point) !== null)
      .map((point) => [dateOnly(point.date), portfolioReturnValue(point)]),
  );
  const liveMap = new Map(
    live
      .filter((point) => point.data_status === "OK" && portfolioReturnValue(point) !== null)
      .map((point) => [dateOnly(point.date), portfolioReturnValue(point)]),
  );
  const benchmarkMap = new Map(
    benchmark
      .filter(
        (point) =>
          point.data_status === "OK" && numeric(point.cumulative_return) !== null,
      )
      .map((point) => [dateOnly(point.date), numeric(point.cumulative_return)]),
  );

  let current = [];
  let latest = [];
  for (const point of benchmark) {
    const day = dateOnly(point.date);
    if (paperMap.has(day) && liveMap.has(day) && benchmarkMap.has(day)) {
      current.push(day);
    } else if (current.length) {
      latest = current;
      current = [];
    }
  }
  if (current.length) latest = current;
  if (!latest.length) return { paper: [], live: [], benchmark: [] };

  function rebase(map) {
    const baseline = map.get(latest[0]);
    return latest.map((day) => ({
      date: day,
      value: (1 + map.get(day)) / (1 + baseline) - 1,
    }));
  }

  return {
    paper: rebase(paperMap),
    live: rebase(liveMap),
    benchmark: rebase(benchmarkMap),
  };
}

async function loadFallback(config) {
  const [paper, live, benchmark] = await Promise.all([
    fetchJson(config.fallbackUrls.paper),
    fetchJson(config.fallbackUrls.live),
    fetchJson(config.fallbackUrls.benchmark),
  ]);
  return {
    schema_version: 3,
    revision: "fallback",
    generated_at: new Date().toISOString(),
    data_as_of: [
      ...paper.map((trade) => trade.date),
      ...live.map((trade) => trade.date),
      ...benchmark.map((row) => row.date),
    ].sort().at(-1),
    prices_as_of: benchmark.at(-1)?.date ?? null,
    currency: "USD",
    source: "fallback",
    portfolios: {
      paper: calculateFallbackPortfolio(paper, config.fallbackInitialCash.paper),
      live: calculateFallbackPortfolio(live, config.fallbackInitialCash.live),
    },
    benchmark: { symbol: "SPY", daily: normalizeBenchmark(benchmark) },
    warnings: ["未能讀取生成快照；現正使用瀏覽器 FIFO 後備計算。"],
  };
}

export async function loadDashboardData(config) {
  try {
    const snapshot = validateSnapshot(await fetchJson(config.snapshotUrl));
    return { ...snapshot, source: "snapshot" };
  } catch (snapshotError) {
    try {
      return await loadFallback(config);
    } catch (fallbackError) {
      throw new Error(
        `無法載入投資組合數據：${snapshotError.message}; ${fallbackError.message}`,
      );
    }
  }
}
