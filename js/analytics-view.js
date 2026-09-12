import { contributionModel, monthlyModel, instrumentModel } from './analytics.js';
import { escapeHtml as e, formatCurrency as money, formatPercent as pct, formatNumber as number, valueClass, exportTableToCsv, numeric } from './utils.js';
const cell = value => `<td class="numeric ${valueClass(value)}">${money(value,{sign:true})}</td>`;
const table = (id,heads,body) => `<div class="table-scroll"><table id="${id}"><thead><tr>${heads.map(x=>`<th scope="col">${x}</th>`).join('')}</tr></thead><tbody>${body}</tbody></table></div><button type="button" data-analytics-export="${id}">匯出 CSV</button>`;
const chosen = {};
export function renderAnalytics(container, portfolio, benchmark, range, name) {
  if (!container) return;
  const model = contributionModel(portfolio,range), months = monthlyModel(portfolio,benchmark,range);
  const ids = new Map();
  for (const row of [...(portfolio.holdings || []), ...(portfolio.recent_trades || [])]) {
    if (row.symbol) ids.set(row.instrument_id || row.symbol,row.symbol);
  }
  const rows = model?.rows.map(r=>`<tr><td><button type="button" data-instrument="${e(r.id)}">${e(r.symbol)}</button><small>${e(r.id)}</small></td>${cell(r.realized)}${cell(r.unrealized)}${cell(r.income)}${cell(r.total)}${cell(r.fees)}</tr>`).join('');
  container.innerHTML = `<article class="analytics-card"><h2>損益貢獻榜</h2><p>所選期間 · USD 金額貢獻。交易費已計入 FIFO 損益及成本，最後一欄只供參考，不再扣減。</p>
  ${model ? `<p>${e(model.start)} 至 ${e(model.end)} · ${model.baseline ? `對比 ${e(model.baseline)} 收市` : '由組合開始累計'} · 合計 ${money(model.total,{sign:true})}</p>${table(`${name}-contributions`,['資產','已實現損益','未實現損益變動','淨收入／支出','總貢獻','交易費（已包含）'],rows || '<tr><td colspan="6">未有資產活動</td></tr>')}` : '<p role="status">此份資料未提供個股歷史估值，暫時無法計算期間貢獻。</p>'}
  <p>— 表示端點估值不足；收入包括已扣稅股息及其他收入／支出。沒有明確資產身份的收入獨立列示。</p></article>
  <article class="analytics-card"><h2>個股詳情</h2><label for="${name}-instrument">選擇資產（包括已平倉）</label><select id="${name}-instrument"><option value="">請選擇</option>${[...ids].map(([id,symbol])=>`<option value="${e(id)}">${e(symbol)} · ${e(id)}</option>`).join('')}</select><div data-instrument-detail><p>選擇資產，查看完整交易歷史及目前未平倉批次。</p></div></article>
  <article class="analytics-card"><h2>月度績效表</h2><p>依所選範圍按月分組，顯示實際涵蓋日期；首尾月份可能未完整。每日 TWR 複利，超額為回報百分點差。缺價或回報缺口顯示 —。</p>${months.length ? table(`${name}-monthly`,['月份','涵蓋日期','組合 TWR','SPY','超額（百分點）'],months.map(r=>`<tr><th scope="row">${e(r.month)}</th><td>${e(r.start)} 至 ${e(r.end)}</td><td class="numeric ${valueClass(r.result)}">${pct(r.result,{sign:true})}</td><td class="numeric">${pct(r.spy,{sign:true})}</td><td class="numeric ${valueClass(r.excess)}">${r.excess === null ? '—' : number(r.excess*100)}</td></tr>`).join('')) : '<p>未有可用每日績效資料。</p>'}</article>`;
  const select = container.querySelector('select');
  const show = id => {
    chosen[name] = id; select.value = ids.has(id) ? id : '';
    const target = container.querySelector('[data-instrument-detail]');
    if (!ids.has(id)) { target.innerHTML = '<p>請選擇資產。</p>'; return; }
    const d = instrumentModel(portfolio,id), l = d.latest;
    const total = l && numeric(l.unrealized) !== null ? numeric(l.realized)+numeric(l.income)+numeric(l.unrealized) : null;
    target.innerHTML = `<h3>${e(d.symbol)}</h3><p>完整歷史及最新快照 · 不隨時間篩選 · ${d.holding ? '持有中' : '目前沒有持倉'}</p><p>累計總損益 ${money(total,{sign:true})} · 已實現 ${money(l?.realized,{sign:true})} · 淨收入／支出 ${money(l?.income,{sign:true})} · 未實現 ${money(l?.unrealized,{sign:true})}</p><h4>未平倉 FIFO 批次</h4><p>成本包括未分攤交易費及交收調整；股數反映拆股。</p>${d.lots === null ? '<p>快照未提供批次資料。</p>' : table(`${name}-lots`,['買入時間（UTC）','餘下股數／合約','合約乘數','餘下成本'],d.lots.map(r=>`<tr><td>${e(r.opened_at)}</td><td class="numeric">${number(r.shares,6)}</td><td class="numeric">${number(r.contract_multiplier)}</td><td class="numeric">${money(r.cost_basis)}</td></tr>`).join('') || '<tr><td colspan="4">沒有未平倉批次</td></tr>')}<h4>買賣及收入歷史</h4>${table(`${name}-instrument-trades`,['時間（UTC）','活動','股數／合約','價格','交易費','損益／淨收入'],d.trades.map(r=>`<tr><td>${e(r.occurred_at || r.date)}</td><td>${e(r.action)}</td><td class="numeric">${number(r.shares,6)}</td><td class="numeric">${money(r.price)}</td><td class="numeric">${money(r.fee)}</td>${cell(r.pnl)}</tr>`).join('') || '<tr><td colspan="6">沒有活動</td></tr>')}`;
  };
  select.addEventListener('change',()=>show(select.value));
  container.querySelectorAll('[data-instrument]').forEach(button=>button.addEventListener('click',()=>{show(button.dataset.instrument); select.focus();}));
  container.onclick = event => { const button = event.target.closest('[data-analytics-export]'); if (button) exportTableToCsv(container.querySelector(`#${button.dataset.analyticsExport}`),`${button.dataset.analyticsExport}.csv`); };
  if (ids.has(chosen[name])) show(chosen[name]);
}
