import { dateOnly, filterByRange, numeric } from "./utils.js";

function storage() {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function storageKey(config, suffix) {
  return `${config.storagePrefix || "portfolio-tracker"}:${suffix}`;
}

function readStoredJson(key) {
  try {
    const raw = storage()?.getItem(key);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function writeStoredJson(key, value) {
  try {
    storage()?.setItem(key, JSON.stringify(value));
  } catch {
    // Storage can be unavailable in private browsing; network fetch still works.
  }
}

function removeStored(key) {
  try {
    storage()?.removeItem(key);
  } catch {
    // Best-effort lease cleanup; an expired lease is ignored automatically.
  }
}

async function fetchJson(url, { githubRaw = false } = {}) {
  const requestUrl = new URL(url, window.location.href);
  requestUrl.searchParams.set("_", Date.now().toString());
  const response = await fetch(requestUrl, {
    cache: "no-store",
    headers: {
      Accept: githubRaw
        ? "application/vnd.github.raw+json"
        : "application/json",
    },
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function validateSnapshot(snapshot) {
  const fail = () => {
    throw new Error("快照格式不正確");
  };
  const object = (value) =>
    value !== null && typeof value === "object" && !Array.isArray(value);
  const hasOwn = (value, key) =>
    Object.prototype.hasOwnProperty.call(value, key);
  const decimalString = (value) =>
    typeof value === "string" &&
    value !== "" &&
    /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value) &&
    Number.isFinite(Number(value));
  const decimalOrNull = (value) => value === null || decimalString(value);
  const hasDecimal = (value, key) =>
    hasOwn(value, key) && decimalOrNull(value[key]);
  const utcTimestampOrNull = (value) =>
    value === null ||
    (typeof value === "string" &&
      value.endsWith("Z") &&
      Number.isFinite(Date.parse(value)));
  const dateString = (value) => {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
      return false;
    }
    const parsed = Date.parse(`${value}T00:00:00Z`);
    return (
      Number.isFinite(parsed) &&
      new Date(parsed).toISOString().slice(0, 10) === value
    );
  };
  const symbol = (value) =>
    typeof value === "string" && /^[A-Z]{1,5}(?:\.[A-Z])?$/.test(value);
  const validatePortfolio = (portfolio) => {
    if (
      !object(portfolio) ||
      !["OK", "INSUFFICIENT_DATA", "NO_DATA"].includes(
        portfolio.data_status,
      ) ||
      !Array.isArray(portfolio.holdings) ||
      !Array.isArray(portfolio.recent_trades) ||
      !Array.isArray(portfolio.daily) ||
      !object(portfolio.metrics) ||
      (hasOwn(portfolio, "cash") && !decimalOrNull(portfolio.cash)) ||
      (hasOwn(portfolio, "initial_cash") &&
        !decimalOrNull(portfolio.initial_cash)) ||
      (hasOwn(portfolio, "realized_pnl_per_trade") &&
        !Array.isArray(portfolio.realized_pnl_per_trade))
    ) {
      fail();
    }
    for (const holding of portfolio.holdings) {
      if (
        !object(holding) ||
        !symbol(holding.symbol) ||
        !decimalString(holding.shares) ||
        !decimalString(holding.avg_cost) ||
        !decimalString(holding.cost_basis) ||
        !hasDecimal(holding, "current_price") ||
        !hasDecimal(holding, "market_value") ||
        !hasDecimal(holding, "unrealized_pnl") ||
        !hasDecimal(holding, "unrealized_pnl_pct") ||
        !utcTimestampOrNull(holding.market_price_as_of)
      ) {
        fail();
      }
    }
    for (const trade of portfolio.recent_trades) {
      if (
        !object(trade) ||
        !["BUY", "SELL", "CASH_FLOW"].includes(trade.action) ||
        typeof trade.event_id !== "string" ||
        !utcTimestampOrNull(trade.occurred_at) ||
        !utcTimestampOrNull(trade.created_at) ||
        typeof trade.source !== "string" ||
        !Number.isInteger(trade.ledger_seq) ||
        trade.ledger_seq < 1
      ) {
        fail();
      }
      if (
        ["BUY", "SELL"].includes(trade.action) &&
        (!symbol(trade.symbol) ||
          !decimalString(trade.shares) ||
          !decimalString(trade.price) ||
          !decimalString(trade.fee) ||
          !hasDecimal(trade, "pnl") ||
          !hasDecimal(trade, "pnl_pct"))
      ) {
        fail();
      }
      if (
        trade.action === "CASH_FLOW" &&
        (trade.symbol !== "USD" ||
          !decimalString(trade.amount) ||
          !hasDecimal(trade, "pnl") ||
          !hasDecimal(trade, "pnl_pct"))
      ) {
        fail();
      }
    }
    for (const realized of portfolio.realized_pnl_per_trade || []) {
      if (
        !object(realized) ||
        typeof realized.event_id !== "string" ||
        !symbol(realized.symbol) ||
        !utcTimestampOrNull(realized.occurred_at) ||
        !decimalString(realized.shares) ||
        !decimalString(realized.price) ||
        !decimalString(realized.fee) ||
        !decimalString(realized.pnl) ||
        !decimalString(realized.pnl_pct) ||
        !decimalString(realized.cumulative_pnl) ||
        !Array.isArray(realized.matches)
      ) {
        fail();
      }
      for (const match of realized.matches) {
        if (
          !object(match) ||
          typeof match.buy_event_id !== "string" ||
          ![
            "shares",
            "buy_price",
            "sell_price",
            "buy_fee",
            "sell_fee",
            "cost",
            "proceeds",
            "pnl",
          ].every((key) => decimalString(match[key]))
        ) {
          fail();
        }
      }
    }
    for (const point of portfolio.daily) {
      if (
        !object(point) ||
        !dateString(point.date) ||
        !["OK", "INSUFFICIENT_DATA", "INSUFFICIENT_MARKET_DATA"].includes(
          point.data_status,
        ) ||
        !hasDecimal(point, "nav") ||
        !hasDecimal(point, "cash") ||
        !hasDecimal(point, "external_flow") ||
        !hasDecimal(point, "daily_return") ||
        !hasDecimal(point, "cumulative_return") ||
        !hasDecimal(point, "segment_return") ||
        !hasDecimal(point, "pnl") ||
        !Array.isArray(point.missing_symbols) ||
        !point.missing_symbols.every(symbol) ||
        !(
          point.segment_id === null ||
          (Number.isInteger(point.segment_id) && point.segment_id > 0)
        )
      ) {
        fail();
      }
    }
    if (
      !["OK", "INSUFFICIENT_DATA", "NO_DATA", "FALLBACK"].includes(
        portfolio.metrics.data_status,
      ) ||
      !hasDecimal(portfolio.metrics, "total_return") ||
      !hasDecimal(portfolio.metrics, "realized_pnl") ||
      !hasDecimal(portfolio.metrics, "win_rate") ||
      !hasDecimal(portfolio.metrics, "max_drawdown") ||
      !hasDecimal(portfolio.metrics, "sharpe_ratio") ||
      !Number.isInteger(portfolio.metrics.closed_episodes) ||
      portfolio.metrics.closed_episodes < 0
    ) {
      fail();
    }
  };

  if (
    !object(snapshot) ||
    snapshot.schema_version !== 3 ||
    !Number.isInteger(snapshot.revision) ||
    snapshot.revision < 0 ||
    !utcTimestampOrNull(snapshot.generated_at) ||
    snapshot.generated_at === null ||
    !hasOwn(snapshot, "data_as_of") ||
    !utcTimestampOrNull(snapshot.data_as_of) ||
    !hasOwn(snapshot, "prices_as_of") ||
    !utcTimestampOrNull(snapshot.prices_as_of) ||
    snapshot.currency !== "USD" ||
    !object(snapshot.source_head) ||
    !object(snapshot.portfolios) ||
    !object(snapshot.portfolios.paper) ||
    !object(snapshot.portfolios.live) ||
    !object(snapshot.benchmark) ||
    snapshot.benchmark.symbol !== "SPY" ||
    !Array.isArray(snapshot.benchmark.daily) ||
    !Array.isArray(snapshot.warnings) ||
    !snapshot.warnings.every((warning) => typeof warning === "string")
  ) {
    fail();
  }
  for (const name of ["paper", "live", "market"]) {
    const head = snapshot.source_head[name];
    if (
      !object(head) ||
      !Number.isInteger(head.count) ||
      head.count < 0 ||
      typeof head.hash !== "string" ||
      !/^[a-f0-9]{64}$/.test(head.hash) ||
      !(
        head.last_event_id === null ||
        typeof head.last_event_id === "string"
      )
    ) {
      fail();
    }
  }
  validatePortfolio(snapshot.portfolios.paper);
  validatePortfolio(snapshot.portfolios.live);
  for (const point of snapshot.benchmark.daily) {
    if (
      !object(point) ||
      !dateString(point.date) ||
      !["OK", "INSUFFICIENT_MARKET_DATA"].includes(point.data_status) ||
      !hasDecimal(point, "close") ||
      !hasDecimal(point, "daily_return") ||
      !hasDecimal(point, "cumulative_return") ||
      !hasDecimal(point, "segment_return") ||
      !(
        point.segment_id === null ||
        (Number.isInteger(point.segment_id) && point.segment_id > 0)
      )
    ) {
      fail();
    }
  }
  return snapshot;
}

export function currentPortfolioNav(portfolio) {
  if (!portfolio || portfolio.data_status === "NO_DATA") return null;
  if (portfolio.data_status === "FALLBACK") {
    return numeric(portfolio.estimated_nav);
  }
  const daily = Array.isArray(portfolio.daily) ? portfolio.daily : [];
  if (!daily.length) return null;
  const latest = daily.at(-1);
  if (!["OK", "INSUFFICIENT_DATA"].includes(latest?.data_status)) return null;
  return numeric(latest.nav);
}

export function currentPortfolioTotalPnl(portfolio) {
  const nav = currentPortfolioNav(portfolio);
  const initial = numeric(portfolio?.initial_cash);
  if (nav === null || initial === null) return null;
  const flows = (portfolio.daily || []).reduce(
    (sum, point) => sum + (numeric(point.external_flow) || 0),
    0,
  );
  return nav - initial - flows;
}

function cachedSnapshot(config) {
  const cached = readStoredJson(storageKey(config, "last-good-snapshot"));
  if (!cached || typeof cached.cachedAt !== "number") return null;
  try {
    return {
      cachedAt: cached.cachedAt,
      snapshot: validateSnapshot(cached.snapshot),
    };
  } catch {
    return null;
  }
}

function saveSnapshot(config, snapshot, now) {
  writeStoredJson(storageKey(config, "last-good-snapshot"), {
    cachedAt: now,
    snapshot,
  });
}

function consumeFetchBudget(config, now) {
  const key = storageKey(config, "fetch-budget");
  const hour = 60 * 60 * 1000;
  const maximum = Number(config.maxFetchesPerHour) || 60;
  let budget = readStoredJson(key);
  if (
    !budget ||
    typeof budget.windowStartedAt !== "number" ||
    now - budget.windowStartedAt >= hour ||
    now < budget.windowStartedAt
  ) {
    budget = { windowStartedAt: now, count: 0 };
  }
  if (budget.count >= maximum) {
    return {
      allowed: false,
      retryAfterMs: Math.max(0, budget.windowStartedAt + hour - now),
    };
  }
  budget.count += 1;
  writeStoredJson(key, budget);
  return { allowed: true, retryAfterMs: 0 };
}

function acquireFetchLease(config) {
  const local = storage();
  if (!local) return { key: null, token: null };
  const key = storageKey(config, "fetch-lease");
  const now = Date.now();
  const duration = Number(config.fetchLeaseMs) || 4000;
  const existing = readStoredJson(key);
  if (
    existing &&
    typeof existing.expiresAt === "number" &&
    existing.expiresAt > now
  ) {
    return null;
  }
  const token = `${now}-${Math.random().toString(36).slice(2)}`;
  writeStoredJson(key, { token, expiresAt: now + duration });
  const confirmed = readStoredJson(key);
  return confirmed?.token === token ? { key, token } : null;
}

function releaseFetchLease(lease) {
  if (!lease?.key) return;
  const current = readStoredJson(lease.key);
  if (current?.token === lease.token) removeStored(lease.key);
}

async function waitForSharedSnapshot(config, cachedAt) {
  const deadline = Date.now() + (Number(config.fetchLeaseMs) || 4000) + 250;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 100));
    const candidate = cachedSnapshot(config);
    if (candidate && candidate.cachedAt > cachedAt) return candidate;
    const lease = readStoredJson(storageKey(config, "fetch-lease"));
    if (!lease || Number(lease.expiresAt) <= Date.now()) break;
  }
  return null;
}

function staleSnapshot(snapshot, message, source) {
  return {
    ...snapshot,
    source,
    warnings: [...new Set([...(snapshot.warnings || []), message])],
  };
}

function freshness(snapshot, config, now) {
  const generated = Date.parse(snapshot.generated_at);
  const threshold = (Number(config.staleAfterMinutes) || 15) * 60 * 1000;
  if (!Number.isFinite(generated) || now - generated <= threshold) {
    return snapshot;
  }
  return staleSnapshot(
    snapshot,
    `公開快照可能已過期（最後生成：${snapshot.generated_at || "未知"}）`,
    snapshot.source,
  );
}

async function fetchSnapshot(config) {
  const urls = config.snapshotUrls || [config.snapshotUrl];
  const errors = [];
  for (const url of urls.filter(Boolean)) {
    try {
      return validateSnapshot(
        await fetchJson(url, { githubRaw: url.includes("api.github.com/") }),
      );
    } catch (error) {
      errors.push(`${url}: ${error.message}`);
    }
  }
  throw new Error(errors.join("; ") || "沒有設定快照網址");
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

export function buildCommonComparison(
  paper,
  live,
  benchmark,
  { range = "ALL" } = {},
) {
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
          point.data_status === "OK" && portfolioReturnValue(point) !== null,
      )
      .map((point) => [dateOnly(point.date), portfolioReturnValue(point)]),
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
  latest = filterByRange(
    latest.map((date) => ({ date })),
    range,
  ).map((point) => point.date);

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

export async function loadDashboardData(
  config,
  { force = false, now = Date.now() } = {},
) {
  const cached = cachedSnapshot(config);
  const ttl = Number(config.cacheTtlMs) || 2 * 60 * 1000;
  if (!force && cached && now - cached.cachedAt < ttl) {
    return freshness(
      { ...cached.snapshot, source: "cache" },
      config,
      now,
    );
  }

  const lease = acquireFetchLease(config);
  if (!lease) {
    if (cached) {
      return staleSnapshot(
        cached.snapshot,
        "另一個瀏覽器分頁正在更新；現正使用 last-good cache",
        "stale-cache",
      );
    }
    const shared = await waitForSharedSnapshot(config, -1);
    if (shared) {
      return freshness(
        { ...shared.snapshot, source: "cache" },
        config,
        now,
      );
    }
    const fallback = await loadFallback(config);
    return staleSnapshot(fallback, "另一個瀏覽器分頁更新逾時", "fallback");
  }

  try {
    const budget = consumeFetchBudget(config, now);
    if (!budget.allowed) {
      const minutes = Math.max(1, Math.ceil(budget.retryAfterMs / 60000));
      if (cached) {
        return staleSnapshot(
          cached.snapshot,
          `已達共享更新上限；約 ${minutes} 分鐘後自動恢復`,
          "stale-cache",
        );
      }
      const fallback = await loadFallback(config);
      return staleSnapshot(
        fallback,
        `已達共享更新上限；約 ${minutes} 分鐘後自動恢復`,
        "fallback",
      );
    }

    try {
      const snapshot = await fetchSnapshot(config);
      saveSnapshot(config, snapshot, now);
      return freshness({ ...snapshot, source: "snapshot" }, config, now);
    } catch (snapshotError) {
      if (cached) {
        return staleSnapshot(
          cached.snapshot,
          `無法取得最新快照；現正使用 last-good cache（${snapshotError.message}）`,
          "stale-cache",
        );
      }
      try {
        return await loadFallback(config);
      } catch (fallbackError) {
        throw new Error(
          `無法載入投資組合數據：${snapshotError.message}; ${fallbackError.message}`,
        );
      }
    }
  } finally {
    releaseFetchLease(lease);
  }
}
