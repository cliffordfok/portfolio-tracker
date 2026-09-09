import { renderSeriesChart } from "./charts.js";
import {
  buildRealizedActivityPnlSeries,
  buildCommonComparison,
  currentPortfolioNav,
  currentPortfolioTotalPnl,
  loadDashboardData,
  portfolioRangeEndDate,
} from "./data.js";
import {
  dateOnly,
  emptyRow,
  escapeHtml,
  exportTableToCsv,
  filterByRange,
  formatCurrency,
  formatDate,
  formatNumber,
  formatPercent,
  numeric,
  rangeStartDate,
  valueClass,
} from "./utils.js";

const state = {
  activeTab: "paper",
  range: "ALL",
  data: null,
  loading: false,
  lastManualRefresh: 0,
};

const config = globalThis.window?.PORTFOLIO_CONFIG || {};

function metricCard(label, value, detail, className = "") {
  return `<article class="metric-card ${className}">
    <p>${escapeHtml(label)}</p>
    <strong>${value}</strong>
    <span>${detail}</span>
  </article>`;
}

function snapshotPerformancePeriod(portfolio) {
  const metrics = portfolio?.metrics;
  const latest = portfolio?.daily?.at(-1);
  const start = dateOnly(metrics?.performance_effective_date);
  const end = latest?.data_status === "OK" ? dateOnly(latest.date) : "";
  return start && end ? { start, end } : null;
}

export function snapshotPerformanceDetail(portfolio) {
  const period = snapshotPerformancePeriod(portfolio);
  if (!period) return "快照績效期間未提供 · 不隨篩選";
  const scope =
    portfolio.metrics.performance_scope === "LATEST_COMPLETE_SEGMENT"
      ? "最新完整估值區間"
      : portfolio.metrics.performance_scope === "FULL_HISTORY"
        ? "完整績效歷史"
        : "快照績效區間";
  return `${scope} ${formatDate(period.start)} 至 ${formatDate(period.end)} · 不隨篩選`;
}

export function winRateDetail(metrics) {
  const episodes = Number.isInteger(metrics?.closed_episodes)
    ? metrics.closed_episodes
    : null;
  const count = episodes === null ? "—" : episodes;
  return `${count} 個全帳本已完成交易週期 · 統計期間未提供 · 不隨篩選`;
}

function renderPortfolioMetrics(name) {
  const portfolio = state.data.portfolios[name];
  const nav = currentPortfolioNav(portfolio);
  const pnl = currentPortfolioTotalPnl(portfolio);
  const totalReturn = numeric(portfolio.metrics?.total_return);
  const cash = numeric(portfolio.cash);
  const winRate = numeric(portfolio.metrics?.win_rate);
  const container = document.querySelector(`#${name}-metrics`);
  container.innerHTML = [
    metricCard(
      "投資組合淨值",
      formatCurrency(nav),
      `${portfolio.holdings.length} 個未平倉持倉 · 快照值，不隨篩選`,
    ),
    metricCard(
      "總損益",
      formatCurrency(pnl, { sign: true }),
      `已實現 ${formatCurrency(portfolio.metrics?.realized_pnl, { sign: true })} · 收入／支出 ${formatCurrency(portfolio.metrics?.income_expense, { sign: true })} · 快照值，不隨篩選`,
      valueClass(pnl),
    ),
    metricCard(
      "總回報",
      formatPercent(totalReturn, { sign: true }),
      snapshotPerformanceDetail(portfolio),
      valueClass(totalReturn),
    ),
    metricCard(
      "可用現金",
      formatCurrency(cash),
      `初始資金 ${formatCurrency(portfolio.initial_cash)} · 快照值，不隨篩選`,
    ),
    metricCard(
      "勝率",
      formatPercent(winRate),
      winRateDetail(portfolio.metrics),
    ),
  ].join("");
}

function renderHoldings(name) {
  const body = document.querySelector(`#${name}-holdings tbody`);
  const holdings = state.data.portfolios[name].holdings || [];
  if (!holdings.length) {
    body.innerHTML = emptyRow(6, "目前未有持倉");
    return;
  }
  body.innerHTML = holdings
    .map(
      (holding) => `<tr>
        <td>
          <span class="symbol">${escapeHtml(holding.symbol)}</span>
          <small>${escapeHtml(holding.instrument_name || holding.instrument_type || "")}</small>
          ${holding.quote_status === "MANUAL" ? "<small>人工報價</small>" : ""}
          ${holding.quote_status === "MISSING" ? "<small>待補報價</small>" : ""}
        </td>
        <td class="numeric">${formatNumber(holding.shares, 6)}</td>
        <td class="numeric">${formatCurrency(holding.avg_cost)}</td>
        <td class="numeric">${formatCurrency(holding.current_price)}</td>
        <td class="numeric">${formatCurrency(holding.market_value)}</td>
        <td class="numeric ${valueClass(holding.unrealized_pnl)}">
          <strong>${formatCurrency(holding.unrealized_pnl, { sign: true })}</strong>
          <small>${formatPercent(holding.unrealized_pnl_pct, { sign: true })}</small>
        </td>
      </tr>`,
    )
    .join("");
}

function visibleTrades(name) {
  const portfolio = state.data.portfolios[name];
  const trades = portfolio.recent_trades || [];
  const endDate = portfolioRangeEndDate(portfolio);
  return filterByRange(
    trades,
    state.range,
    (trade) => dateOnly(trade.occurred_at || trade.date),
    endDate,
  ).filter((trade) =>
    ["BUY", "SELL", "CASH_FLOW", "INCOME_EXPENSE", "SPLIT"].includes(
      trade.action,
    ),
  );
}

function actionBadge(action) {
  return `<span class="action-badge action-${action.toLowerCase()}">${action}</span>`;
}

function renderTrades(name) {
  const body = document.querySelector(`#${name}-trades tbody`);
  const trades = visibleTrades(name);
  const columns = name === "live" ? 8 : 7;
  if (!trades.length) {
    body.innerHTML = emptyRow(columns, "所選區間未有交易");
    return;
  }
  body.innerHTML = trades
    .map((trade) => {
      const date = dateOnly(trade.occurred_at || trade.date);
      const note = [trade.strategy, trade.reason, trade.note]
        .filter(Boolean)
        .join(" · ");
      const symbol = trade.symbol || "USD";
      const shares =
        trade.action === "SPLIT"
          ? `${formatNumber(trade.numerator)}:${formatNumber(trade.denominator)}`
          : ["BUY", "SELL"].includes(trade.action)
            ? formatNumber(trade.shares, 6)
            : "—";
      const price = ["BUY", "SELL"].includes(trade.action)
        ? formatCurrency(trade.price)
        : trade.action === "INCOME_EXPENSE"
          ? formatCurrency(trade.amount, { sign: true })
          : "—";
      const activityNote = [trade.income_type, note].filter(Boolean).join(" · ");
      const common = `
        <td>${formatDate(date)}</td>
        <td><span class="symbol">${escapeHtml(symbol)}</span></td>
        <td>${actionBadge(trade.action)}</td>
        <td class="numeric">${shares}</td>
        <td class="numeric">${price}</td>`;
      if (name === "live") {
        return `<tr>${common}
          <td class="numeric">${formatCurrency(trade.fee ?? trade.withholding_tax)}</td>
          <td class="numeric ${valueClass(trade.pnl)}">${formatCurrency(trade.pnl, { sign: true })}</td>
          <td class="notes">${escapeHtml(activityNote || "—")}</td>
        </tr>`;
      }
      return `<tr>${common}
        <td class="numeric ${valueClass(trade.pnl)}">${formatCurrency(trade.pnl, { sign: true })}</td>
        <td class="notes">${escapeHtml(activityNote || "—")}</td>
      </tr>`;
    })
    .join("");
}

export function buildPortfolioChartModel(portfolio, name, range) {
  const portfolioLabel = name === "paper" ? "模擬倉" : "真實倉";
  const endDate = portfolioRangeEndDate(portfolio);
  const dailyValues = filterByRange(
    portfolio.daily || [],
    range,
    (point) => point.date,
    endDate,
  ).map((point) => ({
    date: point.date,
    value: point.pnl,
  }));
  if (dailyValues.filter((point) => numeric(point.value) !== null).length >= 2) {
    return {
      mode: "daily",
      title: "累計總損益",
      description: "包含已實現、未實現損益及收入／支出；不包括外部資金流。",
      legend: `${portfolioLabel}總損益`,
      ariaLabel: `${portfolioLabel}累計總損益圖，包含已實現、未實現損益及收入／支出`,
      values: dailyValues,
    };
  }

  const fallbackValues = filterByRange(
    buildRealizedActivityPnlSeries(portfolio.recent_trades || []),
    range,
    (point) => point.date,
    endDate,
  );
  if (fallbackValues.length) {
    return {
      mode: "realized-activity",
      title: "累計已實現損益及收入／支出",
      description: "後備模式：只包括已實現交易損益及收入／支出，不含未實現損益。",
      legend: `${portfolioLabel}已實現損益及收入／支出`,
      ariaLabel: `${portfolioLabel}累計已實現損益及收入／支出圖，不含未實現損益`,
      values: fallbackValues,
    };
  }

  return {
    mode: "empty",
    title: "累計損益",
    description: "所選區間未有可用損益資料。",
    legend: "未有可用資料",
    ariaLabel: `${portfolioLabel}損益圖，所選區間未有可用資料`,
    values: [],
  };
}

function renderPortfolioChart(name) {
  const model = buildPortfolioChartModel(
    state.data.portfolios[name],
    name,
    state.range,
  );
  document.querySelector(`#${name}-chart-title`).textContent = model.title;
  document.querySelector(`#${name}-chart-description`).textContent =
    model.description;
  document.querySelector(`#${name}-chart-legend`).textContent = model.legend;
  document.querySelector(`#${name}-chart`).setAttribute(
    "aria-label",
    model.ariaLabel,
  );
  renderSeriesChart(`#${name}-chart`, [
    {
      key: name,
      label: model.legend,
      values: model.values,
    },
  ]);
}

function comparisonReturn(series) {
  return series.length ? numeric(series.at(-1).value) : null;
}

function portfolioHistoryBounds(portfolio) {
  const dates = [
    ...(portfolio.daily || []).map((point) => dateOnly(point.date)),
    ...(portfolio.recent_trades || []).map((trade) =>
      dateOnly(trade.occurred_at || trade.date),
    ),
    ...(portfolio.holdings || []).map((holding) =>
      dateOnly(holding.market_price_as_of),
    ),
  ]
    .filter((date) => /^\d{4}-\d{2}-\d{2}$/.test(date))
    .sort();
  return dates.length ? { start: dates[0], end: dates.at(-1) } : null;
}

function setDisplayedRange(
  start,
  end,
  { suffix = "", emptyText = "期間未提供" } = {},
) {
  const element = document.querySelector("#active-range-dates");
  if (!start || !end) {
    element.textContent = emptyText;
    return;
  }
  element.textContent = `${formatDate(start)} 至 ${formatDate(end)}${
    suffix ? ` · ${suffix}` : ""
  }`;
}

function renderPortfolioRange(name) {
  const portfolio = state.data.portfolios[name];
  if (state.range === "ALL") {
    const bounds = portfolioHistoryBounds(portfolio);
    setDisplayedRange(bounds?.start, bounds?.end, {
      suffix: "完整歷史",
      emptyText: "ALL · 完整歷史",
    });
    return;
  }
  const end = portfolioRangeEndDate(portfolio);
  setDisplayedRange(rangeStartDate(state.range, end), end);
}

function comparisonPeriodText(comparison) {
  if (!comparison.range_start_date || !comparison.range_end_date) {
    return "期間未提供";
  }
  return `${formatDate(comparison.range_start_date)} 至 ${formatDate(
    comparison.range_end_date,
  )}`;
}

function compareSnapshotMetric(value, portfolio, kind) {
  const period = snapshotPerformancePeriod(portfolio);
  const valueText = formatPercent(value);
  if (kind === "win-rate") {
    return `${valueText}<small>${escapeHtml(winRateDetail(portfolio.metrics))}</small>`;
  }
  const detail = period
    ? `快照績效 ${formatDate(period.start)} 至 ${formatDate(period.end)} · 不隨篩選`
    : "快照績效期間未提供 · 不隨篩選";
  return `${valueText}<small>${escapeHtml(detail)}</small>`;
}

function renderCompare() {
  const paper = state.data.portfolios.paper.daily || [];
  const live = state.data.portfolios.live.daily || [];
  const benchmark = state.data.benchmark.daily || [];
  const comparison = buildCommonComparison(paper, live, benchmark, {
    range: state.range,
  });
  const paperMetrics = state.data.portfolios.paper.metrics;
  const liveMetrics = state.data.portfolios.live.metrics;
  const paperReturn = comparisonReturn(comparison.paper);
  const liveReturn = comparisonReturn(comparison.live);
  const spyReturn = comparisonReturn(comparison.benchmark);
  const commonPeriod = comparisonPeriodText(comparison);
  setDisplayedRange(
    comparison.range_start_date,
    comparison.range_end_date,
    {
      suffix: "最新共同完整區間",
      emptyText: "沒有共同有效區間",
    },
  );
  document.querySelector("#compare-metrics").innerHTML = `
    <article class="compare-card paper-card">
      <div><span>模擬倉 · 所選共同區間回報</span><strong class="${valueClass(paperReturn)}">${formatPercent(paperReturn, { sign: true })}</strong></div>
      <dl>
        <div><dt>回報期間</dt><dd>${commonPeriod}</dd></div>
        <div><dt>最大回撤（快照）</dt><dd>${compareSnapshotMetric(paperMetrics.max_drawdown, state.data.portfolios.paper, "drawdown")}</dd></div>
        <div><dt>勝率（快照）</dt><dd>${compareSnapshotMetric(paperMetrics.win_rate, state.data.portfolios.paper, "win-rate")}</dd></div>
      </dl>
    </article>
    <article class="compare-card live-card">
      <div><span>真實倉 · 所選共同區間回報</span><strong class="${valueClass(liveReturn)}">${formatPercent(liveReturn, { sign: true })}</strong></div>
      <dl>
        <div><dt>回報期間</dt><dd>${commonPeriod}</dd></div>
        <div><dt>最大回撤（快照）</dt><dd>${compareSnapshotMetric(liveMetrics.max_drawdown, state.data.portfolios.live, "drawdown")}</dd></div>
        <div><dt>勝率（快照）</dt><dd>${compareSnapshotMetric(liveMetrics.win_rate, state.data.portfolios.live, "win-rate")}</dd></div>
      </dl>
    </article>
    <article class="compare-card benchmark-card">
      <div><span>SPY · 所選共同區間回報</span><strong class="${valueClass(spyReturn)}">${formatPercent(spyReturn, { sign: true })}</strong></div>
      <dl>
        <div><dt>回報期間</dt><dd>${commonPeriod}</dd></div>
        <div><dt>有效數據</dt><dd>${comparison.benchmark.length} 日</dd></div>
        <div><dt>篩選</dt><dd>${state.range}</dd></div>
      </dl>
    </article>`;

  renderSeriesChart(
    "#compare-chart",
    [
      {
        key: "paper",
        label: "模擬倉",
        values: comparison.paper,
      },
      {
        key: "live",
        label: "真實倉",
        values: comparison.live,
      },
      {
        key: "benchmark",
        label: "SPY",
        values: comparison.benchmark,
      },
    ],
    { valueType: "percent", height: 380 },
  );
}

function renderNotices() {
  const region = document.querySelector("#notice-region");
  const warnings = state.data.warnings || [];
  if (!warnings.length) {
    region.replaceChildren();
    return;
  }
  region.innerHTML = `<div class="notice">
    <span aria-hidden="true">!</span>
    <p>${warnings.map(escapeHtml).join(" · ")}</p>
  </div>`;
}

function renderMeta() {
  const statusView = dataStatusView(state.data);
  document.querySelector("#data-source").textContent = statusView.sourceLabel;
  document.querySelector("#data-as-of").textContent = formatDate(
    state.data.load_status?.prices_as_of ?? state.data.prices_as_of,
    true,
  );
  document.querySelector("#data-source-acquired-label").textContent =
    statusView.sourceAcquiredLabel;
  document.querySelector("#data-source-acquired-at").textContent = formatDate(
    state.data.load_status?.source_acquired_at,
    true,
  );
  document.querySelector("#data-accessed-at").textContent = formatDate(
    state.data.load_status?.accessed_at,
    true,
  );
  document.querySelector("#snapshot-generated-at").textContent =
    `快照生成 ${formatDate(
      state.data.load_status?.snapshot_generated_at ?? state.data.generated_at,
      true,
    )}`;
  document.querySelector("#snapshot-revision").textContent =
    `Revision ${state.data.revision}`;
  const status = document.querySelector("#data-status-label");
  status.textContent = statusView.label;
  const container = status.closest(".market-status");
  container.classList.toggle("is-warning", statusView.warning);
  container.title = statusView.title;
}

export function dataStatusView(data) {
  const loadStatus = data?.load_status || {};
  if (loadStatus.freshness === "demo" || loadStatus.source === "fallback") {
    return {
      label: "虛構示範資料（非實際倉位）",
      sourceLabel: "虛構示範資料",
      sourceAcquiredLabel: "示範載入",
      warning: true,
      title: "來源：內置示範資料；不代表實際投資組合",
    };
  }
  if (loadStatus.freshness === "stale") {
    return {
      label: "快照已過期",
      sourceLabel:
        loadStatus.source === "cache" ? "last-good cache" : "公開快照",
      sourceAcquiredLabel:
        loadStatus.source === "cache" ? "原始下載" : "網絡取得",
      warning: true,
      title: `來源：${loadStatus.source === "cache" ? "last-good cache" : "公開快照"}；新鮮度：已過期`,
    };
  }
  if (loadStatus.source === "cache" || data?.source === "cache") {
    return {
      label: "快照快取有效",
      sourceLabel: "last-good cache",
      sourceAcquiredLabel: "原始下載",
      warning: false,
      title: "來源：last-good cache；新鮮度：有效",
    };
  }
  return {
    label: "公開快照已同步",
    sourceLabel: "公開快照",
    sourceAcquiredLabel: "網絡取得",
    warning: false,
    title: "來源：公開快照；新鮮度：有效",
  };
}

function renderActiveTab() {
  if (!state.data) return;
  if (state.activeTab === "compare") {
    renderCompare();
    return;
  }
  renderPortfolioRange(state.activeTab);
  renderPortfolioMetrics(state.activeTab);
  renderHoldings(state.activeTab);
  renderTrades(state.activeTab);
  renderPortfolioChart(state.activeTab);
}

function renderAll() {
  renderMeta();
  renderNotices();
  renderActiveTab();
}

export async function refreshData({
  quiet = false,
  force = false,
  loader = loadDashboardData,
  loadConfig = config,
  renderer = renderAll,
} = {}) {
  if (state.loading) return;
  state.loading = true;
  const overlay = document.querySelector("#loading-overlay");
  if (!quiet) overlay.classList.add("is-visible");
  try {
    state.data = await loader(loadConfig, { force });
    renderer();
  } catch (error) {
    document.querySelector("#notice-region").innerHTML = `<div class="notice is-error">
      <span aria-hidden="true">×</span><p>${escapeHtml(error.message)}</p>
    </div>`;
    const status = document.querySelector("#data-status-label");
    status.textContent = "數據載入失敗";
    status.closest(".market-status").classList.add("is-warning");
  } finally {
    state.loading = false;
    overlay.classList.remove("is-visible");
  }
}

function manualRefresh() {
  const button = document.querySelector("#refresh-button");
  const now = Date.now();
  const cooldown = Number(config.refreshCooldownMs) || 30000;
  if (now - state.lastManualRefresh < cooldown) return;
  state.lastManualRefresh = now;
  button.disabled = true;
  button.setAttribute("aria-disabled", "true");
  window.setTimeout(() => {
    button.disabled = false;
    button.removeAttribute("aria-disabled");
  }, cooldown);
  refreshData({ force: true });
}

function activateTab(tabName, { refresh = true } = {}) {
  state.activeTab = tabName;
  document.querySelectorAll("[data-tab]").forEach((button) => {
    const active = button.dataset.tab === tabName;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll("[data-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.panel !== tabName;
  });
  if (refresh) refreshData({ quiet: true });
  else renderActiveTab();
}

function bindEvents() {
  const tabs = [...document.querySelectorAll("[data-tab]")];
  tabs.forEach((button, index) => {
    button.addEventListener("click", () => activateTab(button.dataset.tab));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      const direction = event.key === "ArrowRight" ? 1 : -1;
      const next = tabs[(index + direction + tabs.length) % tabs.length];
      next.focus();
      activateTab(next.dataset.tab);
    });
  });

  document.querySelectorAll("[data-range]").forEach((button) => {
    button.addEventListener("click", () => {
      state.range = button.dataset.range;
      document.querySelectorAll("[data-range]").forEach((item) => {
        const active = item === button;
        item.classList.toggle("is-active", active);
        item.setAttribute("aria-pressed", String(active));
      });
      renderActiveTab();
    });
  });

  document.querySelector("#refresh-button").addEventListener("click", manualRefresh);
  document.querySelectorAll(".export-button").forEach((button) => {
    button.addEventListener("click", () => {
      const table = document.querySelector(`#${button.dataset.table}`);
      exportTableToCsv(
        table,
        `${button.dataset.table}-${new Date().toISOString().slice(0, 10)}.csv`,
      );
    });
  });

  let resizeTimer;
  window.addEventListener("resize", () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(renderActiveTab, 180);
  });
}

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", () => {
    bindEvents();
    activateTab("paper", { refresh: false });
    refreshData();
  });
}

export { state as appState };
