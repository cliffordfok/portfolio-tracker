import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  buildCommonComparison,
  calculateFallbackPortfolio,
  normalizeBenchmark,
} from "../js/data.js";
import {
  filterByRange,
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

test("static page contains all required tabs, tables, and D3 v7", async () => {
  const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
  for (const required of [
    'data-tab="paper"',
    'data-tab="live"',
    'data-tab="compare"',
    'id="paper-holdings"',
    'id="live-trades"',
    'id="compare-chart"',
    "d3@7",
  ]) {
    assert.ok(html.includes(required), `missing ${required}`);
  }
});
