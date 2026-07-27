import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  buildRealizedActivityPnlSeries,
  buildCommonComparison,
  calculateFallbackPortfolio,
  currentPortfolioNav,
  currentPortfolioTotalPnl,
  loadDashboardData,
  normalizeBenchmark,
} from "../js/data.js";
import {
  csvEscape,
  exportTableToCsv,
  filterByRange,
  escapeHtml,
  formatCurrency,
  formatPercent,
  numeric,
} from "../js/utils.js";

test("realized activity chart falls back to FIFO and income P&L", () => {
  assert.deepEqual(
    buildRealizedActivityPnlSeries([
      {
        ledger_seq: 4,
        occurred_at: "2026-01-04T14:00:00Z",
        action: "INCOME_EXPENSE",
        pnl: "7.5",
      },
      {
        ledger_seq: 3,
        occurred_at: "2026-01-03T15:00:00Z",
        action: "CASH_FLOW",
        pnl: null,
      },
      {
        ledger_seq: 2,
        occurred_at: "2026-01-02T15:00:00Z",
        action: "SELL",
        pnl: "-2",
      },
      {
        ledger_seq: 1,
        occurred_at: "2026-01-01T15:00:00Z",
        action: "BUY",
        pnl: null,
      },
    ]),
    [
      { date: "2026-01-02", value: -2 },
      { date: "2026-01-04", value: 5.5 },
    ],
  );
});

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
  assert.equal(result.metrics.performance_effective_date, null);
  assert.equal(result.metrics.performance_scope, null);
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
  assert.equal(result.performance_effective_date, "2026-01-03");
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

test("CSV table export downloads visible rows and excludes empty placeholders", async () => {
  const makeRow = (values, { empty = false } = {}) => ({
    classList: { contains: (name) => name === "empty-row" && empty },
    querySelectorAll: () => values.map((textContent) => ({ textContent })),
  });
  const table = {
    querySelectorAll: () => [
      makeRow(["Symbol", "Note"]),
      makeRow(["AAPL", "=SUM(1,1)"]),
      makeRow(["No data yet"], { empty: true }),
    ],
  };
  let capturedBlob;
  let clicked = false;
  let removed = false;
  let revokedUrl;
  const originalDocument = globalThis.document;
  const originalCreateObjectUrl = URL.createObjectURL;
  const originalRevokeObjectUrl = URL.revokeObjectURL;
  globalThis.document = {
    body: { append() {} },
    createElement: () => ({
      click() {
        clicked = true;
      },
      remove() {
        removed = true;
      },
    }),
  };
  URL.createObjectURL = (blob) => {
    capturedBlob = blob;
    return "blob:portfolio-csv";
  };
  URL.revokeObjectURL = (url) => {
    revokedUrl = url;
  };

  try {
    assert.equal(exportTableToCsv(table, "portfolio.csv"), true);
    const bytes = new Uint8Array(await capturedBlob.arrayBuffer());
    assert.deepEqual([...bytes.slice(0, 3)], [0xef, 0xbb, 0xbf]);
    assert.equal(
      await capturedBlob.text(),
      'Symbol,Note\nAAPL,"\'=SUM(1,1)"',
    );
    assert.equal(clicked, true);
    assert.equal(removed, true);
    assert.equal(revokedUrl, "blob:portfolio-csv");
  } finally {
    if (originalDocument === undefined) delete globalThis.document;
    else globalThis.document = originalDocument;
    URL.createObjectURL = originalCreateObjectUrl;
    URL.revokeObjectURL = originalRevokeObjectUrl;
  }
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
    'data-range="1M" aria-pressed="false"',
    'data-range="ALL" aria-pressed="true"',
    "d3@7.9.0",
    'integrity="sha384-',
    'crossorigin="anonymous"',
  ]) {
    assert.ok(html.includes(required), `missing ${required}`);
  }
  const app = await readFile(new URL("../js/app.js", import.meta.url), "utf8");
  assert.match(
    app,
    /item\.setAttribute\("aria-pressed", String\(active\)\)/,
    "range buttons must expose their active state to assistive technology",
  );
  assert.match(app, /最新完整估值區間/);
  assert.match(html, /最新共同完整估值區間/);
  const rawDataUrl =
    "https://raw.githubusercontent.com/cliffordfok/portfolio-tracker/portfolio-data/portfolio-snapshot.json";
  const contentsApiUrl =
    "https://api.github.com/repos/cliffordfok/portfolio-tracker/contents/portfolio-snapshot.json?ref=portfolio-data";
  assert.ok(config.includes(rawDataUrl));
  assert.ok(config.includes(contentsApiUrl));
  assert.ok(
    config.indexOf(rawDataUrl) < config.indexOf(contentsApiUrl),
    "public raw snapshot must be tried before the rate-limited Contents API",
  );
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

test("fallback FIFO honors exact broker settlement adjustments", () => {
  const result = calculateFallbackPortfolio(
    [
      {
        date: "2026-01-01",
        symbol: "ONDS",
        action: "BUY",
        shares: 150,
        price: 10,
        fee: 0,
      },
      {
        date: "2026-01-02",
        symbol: "ONDS",
        action: "SELL",
        shares: 150,
        price: 11.0001,
        fee: 0.03,
        settlement_adjustment: -0.005,
      },
    ],
    5000,
  );
  assert.ok(Math.abs(result.cash - 5149.98) < 1e-9);
  assert.ok(Math.abs(result.metrics.realized_pnl - 149.98) < 1e-9);
});

test("Hermes contract uses the real Docker paths and never reads credentials", async () => {
  const contract = await readFile(
    new URL("../.hermes.md", import.meta.url),
    "utf8",
  );
  for (const required of [
    "/data/portfolio-tracker",
    "/data/portfolio",
    "telegram-trade",
    "live-telegram-TELEGRAM_UPDATE_ID",
    "Live opening is approved as USD `0` at `2021-09-27T00:00:00Z`",
    "scripts/import_live_staging.py",
    "Only `portfolio_cron.py publish|maintain`",
    "`bootstrap-publish` action may do the same only when an operator explicitly",
    "portfolio_cron.py doctor-paper-active",
    "canonical `NO_DATA`",
    "Do not read `/data/.hermes/.env`",
    "`/data/portfolio/secrets/github-token`",
  ]) {
    assert.ok(contract.includes(required), `missing Hermes rule: ${required}`);
  }
  assert.doesNotMatch(contract, /Project code: `\/opt\/portfolio-tracker`/);
  assert.doesNotMatch(contract, /Private runtime: `\/var\/lib\/portfolio-tracker`/);
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
      performance_effective_date: null,
      performance_scope: null,
      total_return: null,
      realized_pnl: "0",
      income_expense: "0",
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
    schema_version: 4,
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

test("schema 3 public snapshot is upgraded in memory during rollout", async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  globalThis.window = {
    location: { href: "https://cliffordfok.github.io/portfolio-tracker/" },
    localStorage: new MemoryStorage(),
  };
  const payload = validSnapshot(3);
  payload.schema_version = 3;
  delete payload.portfolios.paper.metrics.income_expense;
  delete payload.portfolios.paper.metrics.performance_effective_date;
  delete payload.portfolios.paper.metrics.performance_scope;
  payload.portfolios.paper.data_status = "INSUFFICIENT_DATA";
  payload.portfolios.paper.metrics.data_status = "INSUFFICIENT_DATA";
  payload.portfolios.paper.holdings = [
    {
      symbol: "AAPL",
      shares: "2",
      avg_cost: "100",
      cost_basis: "200",
      current_price: null,
      market_price_as_of: null,
      market_value: null,
      unrealized_pnl: null,
      unrealized_pnl_pct: null,
    },
  ];
  globalThis.fetch = async () => ({
    ok: true,
    json: async () => payload,
  });
  try {
    const result = await loadDashboardData(
      {
        snapshotUrls: ["https://example.test/snapshot-v3.json"],
        storagePrefix: "schema-v3-rollout-test",
        staleAfterMinutes: 999999,
      },
      { now: 1000 },
    );
    assert.equal(result.source, "snapshot");
    assert.equal(result.schema_version, 4);
    assert.equal(result.portfolios.paper.metrics.income_expense, "0");
    assert.equal(
      result.portfolios.paper.holdings[0].instrument_id,
      "AAPL",
    );
    assert.equal(
      result.portfolios.paper.holdings[0].quote_status,
      "MISSING",
    );
  } finally {
    globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
  }
});

test("snapshot validation accepts an intentional return-base gap", async () => {
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

test("snapshot validation accepts latest-segment performance metadata", async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  globalThis.window = {
    location: { href: "https://cliffordfok.github.io/portfolio-tracker/" },
    localStorage: new MemoryStorage(),
  };
  const payload = validSnapshot(32);
  const paper = payload.portfolios.paper;
  paper.data_status = "OK";
  paper.cash = "1050";
  paper.initial_cash = "1000";
  paper.metrics = {
    ...paper.metrics,
    data_status: "OK",
    performance_effective_date: "2026-01-03",
    performance_scope: "LATEST_COMPLETE_SEGMENT",
    total_return: "0.05",
    max_drawdown: "0",
  };
  paper.daily = [
    {
      date: "2026-01-03",
      nav: "1000",
      cash: "1000",
      external_flow: "0",
      daily_return: null,
      cumulative_return: null,
      segment_id: 2,
      segment_return: "0",
      pnl: "0",
      data_status: "OK",
      missing_symbols: [],
    },
    {
      date: "2026-01-04",
      nav: "1050",
      cash: "1050",
      external_flow: "0",
      daily_return: "0.05",
      cumulative_return: null,
      segment_id: 2,
      segment_return: "0.05",
      pnl: "50",
      data_status: "OK",
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
        storagePrefix: "performance-segment-test",
        staleAfterMinutes: 999999,
      },
      { now: 1000 },
    );
    assert.equal(result.source, "snapshot");
    assert.equal(
      result.portfolios.paper.metrics.performance_effective_date,
      "2026-01-03",
    );
    assert.equal(
      result.portfolios.paper.metrics.performance_scope,
      "LATEST_COMPLETE_SEGMENT",
    );
  } finally {
    globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
  }
});

test("snapshot validation accepts instrument, income, and split fields", async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  globalThis.window = {
    location: { href: "https://cliffordfok.github.io/portfolio-tracker/" },
    localStorage: new MemoryStorage(),
  };
  const payload = validSnapshot(4);
  payload.portfolios.live = {
    ...payload.portfolios.live,
    data_status: "INSUFFICIENT_DATA",
    cash: "810",
    initial_cash: "1000",
    holdings: [
      {
        instrument_id: "PRIVATE:ACME",
        instrument_type: "PRIVATE",
        instrument_name: "Example Private Company",
        quote_symbol: null,
        quote_status: "MISSING",
        symbol: "ACME",
        shares: "4",
        avg_cost: "50",
        cost_basis: "200",
        contract_multiplier: "1",
        current_price: null,
        market_price_as_of: null,
        market_value: null,
        unrealized_pnl: null,
        unrealized_pnl_pct: null,
      },
    ],
    recent_trades: [
      {
        event_id: "live-income-1",
        portfolio: "live",
        occurred_at: "2024-01-02T16:00:00Z",
        created_at: "2024-01-02T16:00:00Z",
        source: "manual-import",
        ledger_seq: 3,
        action: "INCOME_EXPENSE",
        symbol: "ACME",
        amount: "7",
        gross_amount: "10",
        withholding_tax: "3",
        income_type: "DIVIDEND",
        pnl: "7",
        pnl_pct: null,
      },
      {
        event_id: "live-split-1",
        portfolio: "live",
        occurred_at: "2024-01-02T17:00:00Z",
        created_at: "2024-01-02T17:00:00Z",
        source: "manual-import",
        ledger_seq: 4,
        action: "SPLIT",
        symbol: "ACME",
        instrument_id: "PRIVATE:ACME",
        numerator: "2",
        denominator: "1",
        shares_before: "2",
        shares_after: "4",
        pnl: null,
        pnl_pct: null,
      },
      {
        event_id: "live-sell-1",
        portfolio: "live",
        occurred_at: "2024-01-02T18:00:00Z",
        created_at: "2024-01-02T18:00:00Z",
        source: "manual-import",
        ledger_seq: 5,
        action: "SELL",
        symbol: "ONDS",
        shares: "150",
        price: "11.0001",
        fee: "0.03",
        settlement_adjustment: "-0.005",
        pnl: "149.98",
        pnl_pct: "0.09998667",
      },
    ],
    daily: [],
    metrics: {
      ...payload.portfolios.live.metrics,
      data_status: "INSUFFICIENT_DATA",
      income_expense: "7",
    },
  };
  globalThis.fetch = async () => ({
    ok: true,
    json: async () => payload,
  });
  try {
    const result = await loadDashboardData(
      {
        snapshotUrls: ["https://example.test/snapshot-v4.json"],
        storagePrefix: "schema-v4-instruments-test",
        staleAfterMinutes: 999999,
      },
      { now: 1000 },
    );
    assert.equal(
      result.portfolios.live.holdings[0].instrument_id,
      "PRIVATE:ACME",
    );
    assert.equal(result.portfolios.live.recent_trades[0].amount, "7");
    assert.equal(
      result.portfolios.live.recent_trades[2].settlement_adjustment,
      "-0.005",
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
    payload = validSnapshot(14);
    payload.portfolios.paper.metrics.performance_effective_date =
      "2026-01-01";
    delete payload.portfolios.paper.metrics.performance_scope;
    const failedPerformanceMetadata = await loadDashboardData(config, {
      force: true,
      now: 2750,
    });
    assert.equal(failedPerformanceMetadata.source, "stale-cache");
    assert.equal(failedPerformanceMetadata.revision, 11);
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

test("sample JSON is an explicit demo fallback and never a primary snapshot", async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  const localStorage = new MemoryStorage();
  globalThis.window = {
    location: { href: "https://cliffordfok.github.io/portfolio-tracker/" },
    localStorage,
  };
  const requests = [];
  globalThis.fetch = async (requestUrl) => {
    const url = String(requestUrl);
    requests.push(url);
    if (url.includes("remote.test")) {
      return { ok: false, status: 503, statusText: "Unavailable" };
    }
    if (url.includes("paper.json")) {
      return {
        ok: true,
        json: async () => [
          {
            date: "2026-01-01",
            symbol: "AAPL",
            action: "BUY",
            shares: 1,
            price: 100,
            fee: 0,
          },
        ],
      };
    }
    if (url.includes("live.json")) {
      return { ok: true, json: async () => [] };
    }
    if (url.includes("benchmark.json")) {
      return {
        ok: true,
        json: async () => [{ date: "2026-01-01", close: 500 }],
      };
    }
    throw new Error(`unexpected request: ${url}`);
  };
  const config = {
    snapshotUrls: ["https://remote.test/portfolio-snapshot.json"],
    fallbackUrls: {
      paper: "./data/paper.json",
      live: "./data/live.json",
      benchmark: "./data/benchmark.json",
    },
    fallbackInitialCash: { paper: 100000, live: 50000 },
    maxFetchesPerHour: 60,
    storagePrefix: "explicit-demo-fallback-test",
    staleAfterMinutes: 999999,
  };
  try {
    const result = await loadDashboardData(config, { now: 1000 });
    assert.equal(result.source, "fallback");
    assert.equal(result.revision, "fallback");
    assert.ok(
      result.warnings.some(
        (warning) =>
          warning.includes("虛構示範數據") &&
          warning.includes("並非你的實際投資組合"),
      ),
    );
    assert.ok(
      requests.every(
        (url) =>
          !url.includes("portfolio-snapshot.json") ||
          url.includes("remote.test"),
      ),
    );
  } finally {
    globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
  }
});

test("production config never treats the bundled demo snapshot as authoritative", async () => {
  const configSource = await readFile(
    new URL("../js/config.js", import.meta.url),
    "utf8",
  );
  const appSource = await readFile(
    new URL("../js/app.js", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(
    configSource,
    /snapshotUrls:[\s\S]*?\.\/data\/portfolio-snapshot\.json/,
  );
  assert.match(appSource, /虛構示範資料（非實際倉位）/);
});

test("gitignore blocks common private credential artifacts", async () => {
  const ignoreSource = await readFile(
    new URL("../.gitignore", import.meta.url),
    "utf8",
  );
  const rules = new Set(
    ignoreSource
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith("#")),
  );
  for (const rule of [
    ".env",
    "*.token",
    "*.pem",
    "*.key",
    "id_ed25519",
    "id_ed25519.*",
    "id_rsa",
    "id_rsa.*",
    "known_hosts",
  ]) {
    assert.ok(rules.has(rule), `missing credential ignore rule: ${rule}`);
  }
});
