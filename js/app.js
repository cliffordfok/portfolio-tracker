import { renderSeriesChart } from "./charts.js";
import {
  buildCommonComparison,
  currentPortfolioNav,
  currentPortfolioTotalPnl,
  loadDashboardData,
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
  valueClass,
} from "./utils.js";

const state = {
  activeTab: "paper",
  range: "ALL",
  data: null,
  loading: false,
  lastManualRefresh: 0,
};

const config = window.PORTFOLIO_CONFIG;

function metricCard(label, value, detail, className = "") {
  return `<article class="metric-card ${className}">
    <p>${escapeHtml(label)}</p>
    <strong>${value}</strong>
    <span>${detail}</span>
  </article>`;
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
    metricCard("投資組合淨值", formatCurrency(nav), `${portfolio.holdings.length} 個未平倉持倉`),
    metricCard(
      "總損益",
      formatCurrency(pnl, { sign: true }),
      `已實現 ${formatCurrency(portfolio.metrics?.realized_pnl, { sign: true })}`,
      valueClass(pnl),
    ),
    metricCard(
      "總回報",
      formatPercent(totalReturn, { sign: true }),
      portfolio.metrics?.data_status === "OK" ? "完整有效區間" : "資料不完整",
      valueClass(totalReturn),
    ),
    metricCard("可用現金", formatCurrency(cash), `初始資金 ${formatCurrency(portfolio.initial_cash)}`),
    metricCard(
      "勝率",
      formatPercent(winRate),
      `${portfolio.metrics?.closed_episodes ?? 0} 個已完成交易週期`,
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
        <td><span class="symbol">${escapeHtml(holding.symbol)}</span></td>
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
  const trades = state.data.portfolios[name].recent_trades || [];
  return filterByRange(trades, state.range, (trade) =>
    dateOnly(trade.occurred_at || trade.date),
  ).filter((trade) => ["BUY", "SELL"].includes(trade.action));
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
      const common = `
        <td>${formatDate(date)}</td>
        <td><span class="symbol">${escapeHtml(trade.symbol)}</span></td>
        <td>${actionBadge(trade.action)}</td>
        <td class="numeric">${formatNumber(trade.shares, 6)}</td>
        <td class="numeric">${formatCurrency(trade.price)}</td>`;
      if (name === "live") {
        return `<tr>${common}
          <td class="numeric">${formatCurrency(trade.fee)}</td>
          <td class="numeric ${valueClass(trade.pnl)}">${formatCurrency(trade.pnl, { sign: true })}</td>
          <td class="notes">${escapeHtml(note || "—")}</td>
        </tr>`;
      }
      return `<tr>${common}
        <td class="numeric ${valueClass(trade.pnl)}">${formatCurrency(trade.pnl, { sign: true })}</td>
        <td class="notes">${escapeHtml(note || "—")}</td>
      </tr>`;
    })
    .join("");
}

function filteredDaily(name) {
  return filterByRange(state.data.portfolios[name].daily || [], state.range);
}

function renderPortfolioChart(name) {
  const values = filteredDaily(name).map((point) => ({
    date: point.date,
    value: point.pnl,
  }));
  renderSeriesChart(`#${name}-chart`, [
    {
      key: name,
      label: name === "paper" ? "模擬倉" : "真實倉",
      values,
    },
  ]);
}

function comparisonReturn(series) {
  return series.length ? numeric(series.at(-1).value) : null;
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
  document.querySelector("#compare-metrics").innerHTML = `
    <article class="compare-card paper-card">
      <div><span>模擬倉</span><strong class="${valueClass(paperReturn)}">${formatPercent(paperReturn, { sign: true })}</strong></div>
      <dl>
        <div><dt>勝率</dt><dd>${formatPercent(paperMetrics.win_rate)}</dd></div>
        <div><dt>最大回撤</dt><dd>${formatPercent(paperMetrics.max_drawdown)}</dd></div>
      </dl>
    </article>
    <article class="compare-card live-card">
      <div><span>真實倉</span><strong class="${valueClass(liveReturn)}">${formatPercent(liveReturn, { sign: true })}</strong></div>
      <dl>
        <div><dt>勝率</dt><dd>${formatPercent(liveMetrics.win_rate)}</dd></div>
        <div><dt>最大回撤</dt><dd>${formatPercent(liveMetrics.max_drawdown)}</dd></div>
      </dl>
    </article>
    <article class="compare-card benchmark-card">
      <div><span>SPY 基準</span><strong class="${valueClass(spyReturn)}">${formatPercent(spyReturn, { sign: true })}</strong></div>
      <dl>
        <div><dt>區間</dt><dd>${state.range}</dd></div>
        <div><dt>有效數據</dt><dd>${comparison.benchmark.length} 日</dd></div>
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
  document.querySelector("#data-as-of").textContent = formatDate(
    state.data.prices_as_of || state.data.data_as_of,
    true,
  );
  document.querySelector("#snapshot-generated-at").textContent =
    `快照生成 ${formatDate(state.data.generated_at, true)}`;
  document.querySelector("#snapshot-revision").textContent =
    `Revision ${state.data.revision}`;
  const status = document.querySelector("#data-status-label");
  status.textContent =
    state.data.source === "snapshot"
      ? "公開快照已同步"
      : state.data.source === "cache"
        ? "快照快取有效"
        : state.data.source === "stale-cache"
          ? "正使用上次有效快照"
          : "虛構示範資料（非實際倉位）";
  status.closest(".market-status").classList.toggle(
    "is-warning",
    !["snapshot", "cache"].includes(state.data.source),
  );
}

function renderActiveTab() {
  if (!state.data) return;
  if (state.activeTab === "compare") {
    renderCompare();
    return;
  }
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

async function refreshData({ quiet = false, force = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  const overlay = document.querySelector("#loading-overlay");
  if (!quiet) overlay.classList.add("is-visible");
  try {
    state.data = await loadDashboardData(config, { force });
    renderAll();
  } catch (error) {
    document.querySelector("#notice-region").innerHTML = `<div class="notice is-error">
      <span aria-hidden="true">×</span><p>${escapeHtml(error.message)}</p>
    </div>`;
    document.querySelector("#data-status-label").textContent = "數據載入失敗";
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

document.addEventListener("DOMContentLoaded", () => {
  bindEvents();
  activateTab("paper", { refresh: false });
  refreshData();
});
