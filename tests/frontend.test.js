import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  buildCommonComparison,
  calculateFallbackPortfolio,
  currentPortfolioNav,
  currentPortfolioTotalPnl,
  loadDashboardData,
  normalizeBenchmark,
} from "../js/data.js";
import {
  csvEscape,
  filterByRange,
  escapeHtml,
  formatCurrency,
  formatPercent,
  numeric,
} from "../js/utils.js";

test("fallback FIFO calculation keeps live and paper state independent", () => {
  const trades = [
    {
      date: "2026-01-01",
      symbol: "AAPL",
      action: "BUY",
      shares: 10,
      price: 100,
      fee: 1,
      current_price: 120,
    },
    {
      date: "2026-01-02",
      symbol: "AAPL",
      action: "SELL",
      shares: 4,
      price: 110,
      fee: 1,
      current_price: 120,
    },
  ];
  const result = calculateFallbackPortfolio(trades, 5000);
  assert.equal(result.holdings[0].shares, 6);
  assert.ok(Math.abs(result.metrics.realized_pnl - 38.6) < 1e-9);
  assert.ok(Math.abs(result.cash - 4438) < 1e-9);
});

test("fallback calculation rejects an oversell instead of fabricating P&L", () => {
  assert.throws(
    () =>
      calculateFallbackPortfolio(
        [
          {
            date: "2026-01-01",
            symbol: "AAPL",
            action: "SELL",
            shares: 1,
            price: 100,
            fee: 0,
          },
        ],
        5000,
      ),
    /oversells AAPL/,
  );
});

test("current portfolio totals never substitute a stale or cash-only NAV", () => {
  const incomplete = {
    data_status: "INSUFFICIENT_DATA",
    initial_cash: "1000",
    cash: "900",
    estimated_nav: "9999",
    holdings: [
      { symbol: "AAPL", market_value: "120", unrealized_pnl: "20" },
      { symbol: "MSFT", market_value: null, unrealized_pnl: null },
    ],
    daily: [
      {
        date: "2026-01-01",
        nav: "1020",
        external_flow: "0",
        data_status: "OK",
      },
      {
        date: "2026-01-02",
        nav: null,
        external_flow: "0",
        data_status: "INSUFFICIENT_MARKET_DATA",
      },
    ],
    metrics: { realized_pnl: "10" },
  };
  assert.equal(currentPortfolioNav(incomplete), null);
  assert.equal(currentPortfolioTotalPnl(incomplete), null);

  const invalidReturnBase = {
    ...incomplete,
    daily: [
      {
        date: "2026-01-02",
        nav: "1025",
        external_flow: "0",
        data_status: "INSUFFICIENT_DATA",
      },
    ],
  };
  assert.equal(currentPortfolioNav(invalidReturnBase), 1025);
  assert.equal(currentPortfolioTotalPnl(invalidReturnBase), 25);

  const recovered = {
    ...incomplete,
    daily: [
      ...incomplete.daily,
      {
        date: "2026-01-03",
        nav: "1050",
        external_flow: "0",
        data_status: "OK",
      },
    ],
  };
  assert.equal(currentPortfolioNav(recovered), 1050);
  assert.equal(currentPortfolioTotalPnl(recovered), 50);

  const fallback = {
    ...incomplete,
    data_status: "FALLBACK",
    estimated_nav: 1015,
    daily: [],
  };
  assert.equal(currentPortfolioNav(fallback), 1015);
  assert.equal(currentPortfolioTotalPnl(fallback), 15);
});

test("benchmark is normalized from its first close", () => {
  const series = normalizeBenchmark([
    { date: "2026-01-01", close: 100 },
    { date: "2026-01-02", close: 110 },
  ]);
  assert.equal(series[0].cumulative_return, 0);
  assert.ok(Math.abs(series[1].cumulative_return - 0.1) < 1e-12);
});

test("compare uses and rebases the latest common contiguous segment", () => {
  const paper = [
    { date: "2026-01-01", cumulative_return: 0, data_status: "OK" },
    {
      date: "2026-01-02",
      cumulative_return: null,
      segment_return: null,
      data_status: "INSUFFICIENT_MARKET_DATA",
    },
    {
      date: "2026-01-03",
      cumulative_return: null,
      segment_return: 0,
      data_status: "OK",
    },
    {
      date: "2026-01-04",
      cumulative_return: null,
      segment_return: 0.05,
      data_status: "OK",
    },
  ];
  const live = [
    { date: "2026-01-01", cumulative_return: 0.01, data_status: "OK" },
    { date: "2026-01-02", cumulative_return: 0.02, data_status: "OK" },
    { date: "2026-01-03", cumulative_return: 0.03, data_status: "OK" },
    { date: "2026-01-04", cumulative_return: 0.04, data_status: "OK" },
  ];
  const benchmark = [
    { date: "2026-01-01", cumulative_return: 0, data_status: "OK" },
    { date: "2026-01-02", cumulative_return: 0.01, data_status: "OK" },
    { date: "2026-01-03", cumulative_return: 0.02, data_status: "OK" },
    { date: "2026-01-04", cumulative_return: 0.03, data_status: "OK" },
  ];
  const result = buildCommonComparison(paper, live, benchmark);
  assert.deepEqual(
    result.paper.map((point) => point.date),
    ["2026-01-03", "2026-01-04"],
  );
  assert.equal(result.paper[0].value, 0);
  assert.ok(Math.abs(result.paper[1].value - 0.05) < 1e-12);
  assert.equal(result.live[0].value, 0);
});

test("compare range is anchored to the latest common date", () => {
  const paper = [
    { date: "2026-05-01", cumulative_return: 0, data_status: "OK" },
    { date: "2026-06-01", cumulative_return: 0.02, data_status: "OK" },
    { date: "2026-06-30", cumulative_return: 0.04, data_status: "OK" },
  ];
  const live = [
    { date: "2026-05-01", cumulative_return: 0, data_status: "OK" },
    { date: "2026-06-01", cumulative_return: 0.01, data_status: "OK" },
    { date: "2026-06-30", cumulative_return: 0.03, data_status: "OK" },
    { date: "2026-07-31", cumulative_return: 0.05, data_status: "OK" },
  ];
  const benchmark = [
    { date: "2026-05-01", cumulative_return: 0, data_status: "OK" },
    { date: "2026-06-01", cumulative_return: 0.01, data_status: "OK" },
    { date: "2026-06-30", cumulative_return: 0.02, data_status: "OK" },
    { date: "2026-07-31", cumulative_return: 0.04, data_status: "OK" },
  ];

  const result = buildCommonComparison(paper, live, benchmark, {
    range: "1M",
  });
  assert.deepEqual(
    result.paper.map((point) => point.date),
    ["2026-06-01", "2026-06-30"],
  );
  assert.deepEqual(
    result.live.map((point) => point.date),
    ["2026-06-01", "2026-06-30"],
  );
  assert.deepEqual(
    result.benchmark.map((point) => point.date),
    ["2026-06-01", "2026-06-30"],
  );
  assert.equal(result.paper[0].value, 0);
  assert.equal(result.live[0].value, 0);
  assert.equal(result.benchmark[0].value, 0);
});

test("global range uses latest dataset date rather than today's date", () => {
  const rows = [
    { date: "2024-01-01" },
    { date: "2024-06-01" },
    { date: "2024-06-20" },
  ];
  assert.deepEqual(
    filterByRange(rows, "1M").map((row) => row.date),
    ["2024-06-01", "2024-06-20"],
  );
});

test("formatters handle null and signed values", () => {
  assert.equal(numeric(null), null);
  assert.equal(formatCurrency(null), "—");
  assert.match(formatCurrency(12, { sign: true }), /^\+\$/);
  assert.match(formatPercent(-0.125), /-12\.50%/);
});

test("untrusted table text is HTML-escaped", () => {
  assert.equal(
    escapeHtml('<img src=x onerror="alert(1)">'),
    "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;",
  );
});

test("CSV export neutralizes spreadsheet formulas", () => {
  for (const prefix of ["=", "+", "-", "@"]) {
    assert.equal(csvEscape(`${prefix}SUM(1,1)`), `"'${prefix}SUM(1,1)"`);
  }
  assert.equal(csvEscape("ordinary note"), "ordinary note");
});

test("static page contains all required tabs, tables, and D3 v7", async () => {
  const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
  const config = await readFile(new URL("../js/config.js", import.meta.url), "utf8");
  for (const required of [
    'data-tab="paper"',
    'data-tab="live"',
    'data-tab="compare"',
    'id="paper-holdings"',
    'id="live-trades"',
    'id="compare-chart"',
    'id="snapshot-generated-at"',
    "d3@7.9.0",
    'integrity="sha384-',
    'crossorigin="anonymous"',
  ]) {
    assert.ok(html.includes(required), `missing ${required}`);
  }
  assert.match(config, /contents\/portfolio-snapshot\.json\?ref=portfolio-data/);
  assert.doesNotMatch(config, /contents\/data\/portfolio-snapshot\.json/);
});

test("systemd path units trigger only while pending markers exist", async () => {
  const unitPaths = [
    "../systemd/portfolio-rebuild.path.example",
    "../systemd/portfolio-publish.path.example",
  ];

  for (const unitPath of unitPaths) {
    const unit = await readFile(new URL(unitPath, import.meta.url), "utf8");
    assert.match(unit, /^PathExists=.*\.pending$/m);
    assert.doesNotMatch(unit, /^PathChanged=/m);
  }
});

test("only the publisher service receives the GitHub token environment", async () => {
  const rebuild = await readFile(
    new URL("../systemd/portfolio-rebuild.service.example", import.meta.url),
    "utf8",
  );
  const backup = await readFile(
    new URL("../systemd/portfolio-backup.service.example", import.meta.url),
    "utf8",
  );
  const publisher = await readFile(
    new URL("../systemd/portfolio-publish.service.example", import.meta.url),
    "utf8",
  );

  for (const unit of [rebuild, backup]) {
    assert.doesNotMatch(unit, /^EnvironmentFile=/m);
    assert.doesNotMatch(unit, /PORTFOLIO_GITHUB_TOKEN/);
    assert.match(unit, /--root \/var\/lib\/portfolio-tracker/);
  }
  assert.match(
    publisher,
    /^EnvironmentFile=\/etc\/portfolio-tracker\/portfolio\.env$/m,
  );
});

class MemoryStorage {
  constructor() {
    this.values = new Map();
  }

  getItem(key) {
    return this.values.has(key) ? this.values.get(key) : null;
  }

  setItem(key, value) {
    this.values.set(key, String(value));
  }

  removeItem(key) {
    this.values.delete(key);
  }
}

function validSnapshot(revision = 1) {
  const portfolio = () => ({
    data_status: "NO_DATA",
    holdings: [],
    recent_trades: [],
    daily: [],
    metrics: {
      data_status: "NO_DATA",
      total_return: null,
      realized_pnl: "0",
      win_rate: null,
      max_drawdown: null,
      sharpe_ratio: null,
      closed_episodes: 0,
    },
  });
  const emptyHead = () => ({
    count: 0,
    last_event_id: null,
    hash: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  });
  return {
    schema_version: 3,
    revision,
    generated_at: "2026-07-25T00:00:00Z",
    data_as_of: null,
    prices_as_of: null,
    currency: "USD",
    source_head: {
      paper: emptyHead(),
      live: emptyHead(),
      market: emptyHead(),
    },
    portfolios: {
      paper: portfolio(),
      live: portfolio(),
    },
    benchmark: { symbol: "SPY", daily: [] },
    warnings: [],
  };
}

test("snapshot validation accepts an invalid return-base gap", async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  globalThis.window = {
    location: { href: "https://cliffordfok.github.io/portfolio-tracker/" },
    localStorage: new MemoryStorage(),
  };
  const payload = validSnapshot(31);
  payload.portfolios.paper.data_status = "INSUFFICIENT_DATA";
  payload.portfolios.paper.metrics.data_status = "INSUFFICIENT_DATA";
  payload.portfolios.paper.daily = [
    {
      date: "2026-01-02",
      nav: "0",
      cash: "0",
      external_flow: "0",
      daily_return: null,
      cumulative_return: null,
      segment_id: null,
      segment_return: null,
      pnl: "0",
      data_status: "INSUFFICIENT_DATA",
      missing_symbols: [],
    },
  ];
  globalThis.fetch = async () => ({
    ok: true,
    json: async () => payload,
  });
  try {
    const result = await loadDashboardData(
      {
        snapshotUrls: ["https://example.test/snapshot.json"],
        storagePrefix: "return-base-gap-test",
        staleAfterMinutes: 999999,
      },
      { now: 1000 },
    );
    assert.equal(result.source, "snapshot");
    assert.equal(
      result.portfolios.paper.daily[0].data_status,
      "INSUFFICIENT_DATA",
    );
  } finally {
    globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
  }
});

test("dashboard cache prevents a second fetch inside the two-minute TTL", async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  const localStorage = new MemoryStorage();
  globalThis.window = {
    location: { href: "https://cliffordfok.github.io/portfolio-tracker/" },
    localStorage,
  };
  let fetches = 0;
  globalThis.fetch = async () => {
    fetches += 1;
    return {
      ok: true,
      json: async () => validSnapshot(),
    };
  };
  const config = {
    snapshotUrls: ["https://example.test/snapshot.json"],
    cacheTtlMs: 120000,
    maxFetchesPerHour: 60,
    storagePrefix: "cache-test",
    staleAfterMinutes: 999999,
  };
  try {
    const first = await loadDashboardData(config, { now: 1000 });
    const second = await loadDashboardData(config, { now: 2000 });
    assert.equal(first.source, "snapshot");
    assert.equal(second.source, "cache");
    assert.equal(fetches, 1);
  } finally {
    globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
  }
});

test("failed forced refresh serves the last-good snapshot with a warning", async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  const localStorage = new MemoryStorage();
  globalThis.window = {
    location: { href: "https://cliffordfok.github.io/portfolio-tracker/" },
    localStorage,
  };
  let fail = false;
  globalThis.fetch = async () => {
    if (fail) throw new Error("offline");
    return {
      ok: true,
      json: async () => validSnapshot(7),
    };
  };
  const config = {
    snapshotUrls: ["https://example.test/snapshot.json"],
    cacheTtlMs: 120000,
    maxFetchesPerHour: 60,
    storagePrefix: "stale-test",
    staleAfterMinutes: 999999,
  };
  try {
    await loadDashboardData(config, { now: 1000 });
    fail = true;
    const result = await loadDashboardData(config, {
      force: true,
      now: 2000,
    });
    assert.equal(result.revision, 7);
    assert.equal(result.source, "stale-cache");
    assert.ok(result.warnings.some((warning) => warning.includes("last-good")));
  } finally {
    globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
  }
});

test("shared hourly budget falls back to cache instead of over-fetching", async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  const localStorage = new MemoryStorage();
  globalThis.window = {
    location: { href: "https://cliffordfok.github.io/portfolio-tracker/" },
    localStorage,
  };
  let fetches = 0;
  globalThis.fetch = async () => {
    fetches += 1;
    return {
      ok: true,
      json: async () => validSnapshot(9),
    };
  };
  const config = {
    snapshotUrls: ["https://example.test/snapshot.json"],
    cacheTtlMs: 1,
    maxFetchesPerHour: 1,
    storagePrefix: "budget-test",
    staleAfterMinutes: 999999,
  };
  try {
    await loadDashboardData(config, { now: 1000 });
    const result = await loadDashboardData(config, {
      force: true,
      now: 2000,
    });
    assert.equal(fetches, 1);
    assert.equal(result.source, "stale-cache");
    assert.ok(result.warnings.some((warning) => warning.includes("共享更新上限")));
  } finally {
    globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
  }
});

test("invalid refreshed schema never replaces the last-good cache", async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  const localStorage = new MemoryStorage();
  globalThis.window = {
    location: { href: "https://cliffordfok.github.io/portfolio-tracker/" },
    localStorage,
  };
  let payload = validSnapshot(11);
  globalThis.fetch = async () => ({
    ok: true,
    json: async () => payload,
  });
  const config = {
    snapshotUrls: ["https://example.test/snapshot.json"],
    cacheTtlMs: 120000,
    maxFetchesPerHour: 60,
    storagePrefix: "invalid-schema-test",
    staleAfterMinutes: 999999,
  };
  try {
    await loadDashboardData(config, { now: 1000 });
    payload = validSnapshot(12);
    payload.portfolios.paper.holdings = "not-an-array";
    const failedRefresh = await loadDashboardData(config, {
      force: true,
      now: 2000,
    });
    assert.equal(failedRefresh.source, "stale-cache");
    assert.equal(failedRefresh.revision, 11);
    payload = validSnapshot(13);
    payload.portfolios.paper.metrics.realized_pnl = true;
    const failedDecimal = await loadDashboardData(config, {
      force: true,
      now: 2500,
    });
    assert.equal(failedDecimal.source, "stale-cache");
    assert.equal(failedDecimal.revision, 11);
    const cached = await loadDashboardData(config, { now: 3000 });
    assert.equal(cached.source, "cache");
    assert.equal(cached.revision, 11);
  } finally {
    globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
  }
});

test("an active cross-tab fetch lease prevents a duplicate network request", async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  const localStorage = new MemoryStorage();
  globalThis.window = {
    location: { href: "https://cliffordfok.github.io/portfolio-tracker/" },
    localStorage,
  };
  let fetches = 0;
  globalThis.fetch = async () => {
    fetches += 1;
    return {
      ok: true,
      json: async () => validSnapshot(21),
    };
  };
  const config = {
    snapshotUrls: ["https://example.test/snapshot.json"],
    cacheTtlMs: 1,
    fetchLeaseMs: 4000,
    maxFetchesPerHour: 60,
    storagePrefix: "lease-test",
    staleAfterMinutes: 999999,
  };
  try {
    await loadDashboardData(config, { now: 1000 });
    localStorage.setItem(
      "lease-test:fetch-lease",
      JSON.stringify({
        token: "other-tab",
        expiresAt: Date.now() + 4000,
      }),
    );
    const result = await loadDashboardData(config, {
      force: true,
      now: 2000,
    });
    assert.equal(fetches, 1);
    assert.equal(result.source, "stale-cache");
    assert.ok(result.warnings.some((warning) => warning.includes("另一個瀏覽器分頁")));
  } finally {
    globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
  }
});
