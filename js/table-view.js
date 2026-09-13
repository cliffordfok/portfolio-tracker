// Keep the full filtered rows outside the document. Paging never changes exports.
const views = new Map();
const exportsByTable = new WeakMap();

export function pageWindow(length, page = 0, size = 10) {
  size = [10, 25, 50].includes(Number(size)) ? Number(size) : 10;
  const pages = Math.max(1, Math.ceil(length / size));
  page = Math.max(0, Math.min(pages - 1, Number.isFinite(page) ? Math.floor(page) : 0));
  return { page, pages, size, start: page * size, end: Math.min(length, (page + 1) * size) };
}

export function contributionSummary(rows, value = row => row.total) {
  const positive = rows.filter(row => value(row) !== null && value(row) > 0)
    .sort((a, b) => value(b) - value(a)).slice(0, 5);
  const negative = rows.filter(row => value(row) !== null && value(row) < 0)
    .sort((a, b) => value(a) - value(b)).slice(0, 5);
  return [...positive, ...negative];
}

export function exportableTable(table) {
  const rows = exportsByTable.get(table);
  if (!rows) return table;
  const copy = table.cloneNode(false);
  const head = table.querySelector("thead");
  if (head) copy.append(head.cloneNode(true));
  const body = document.createElement('tbody');
  rows.forEach(row => body.append(row.cloneNode(true)));
  copy.append(body);
  return copy;
}

export function mountTable(table, { scope = '', mode = 'pages' } = {}) {
  const body = table?.querySelector?.("tbody");
  if (!body) return;
  const rows = [...body.children].filter(row => !row.classList.contains('empty-row') && !row.querySelector('[colspan]'));
  exportsByTable.set(table, rows);
  const key = table.id;
  let state = views.get(key);
  if (!state || state.scope !== scope) {
    state = { scope, page: 0, size: 10, expanded: false, year: 'recent' };
    views.set(key, state);
  }
  if (!rows.length) return;
  // Re-rendering details may reuse the same wrapper; controls belong to this table.
  const controls = document.createElement('div');
  controls.className = 'table-controls';
  controls.setAttribute('role', 'group');
  controls.setAttribute('aria-label', `${table.id} 表格顯示選項`);
  table.parentElement.before(controls);
  const makeButton = (label, action) => {
    const button = document.createElement('button');
    button.type = 'button'; button.textContent = label;
    button.addEventListener('click', action); controls.append(button);
    return button;
  };
  const label = document.createElement('label');
  const select = document.createElement('select');
  label.append(mode === 'monthly' ? '月份範圍 ' : '每頁 ');
  label.append(select); controls.append(label);
  let toggle;
  if (mode === 'contribution') {
    toggle = makeButton('查看全部', () => { state.expanded = !state.expanded; state.page = 0; render(); });
  }
  const options = mode === 'monthly'
    ? [['recent', '最近 12 個月'], ...[...new Set(rows.map(row => row.dataset.month.slice(0, 4)))].sort().reverse().map(year => [year, `${year} 年`])]
    : [[10, '10 筆'], [25, '25 筆'], [50, '50 筆']];
  options.forEach(([value, text]) => { const option = document.createElement('option'); option.value = value; option.textContent = text; select.append(option); });
  if (mode === 'monthly' && !options.some(([value]) => value === state.year)) state.year = 'recent';
  select.value = mode === 'monthly' ? state.year : String(state.size);
  select.addEventListener('change', () => {
    if (mode === 'monthly') state.year = select.value;
    else state.size = Number(select.value);
    state.page = 0; render();
  });
  const previous = makeButton('上一頁', () => { state.page--; render(); });
  const next = makeButton('下一頁', () => { state.page++; render(); });
  const status = document.createElement('span'); status.setAttribute('aria-live', 'polite'); controls.append(status);
  function render() {
    let shown;
    const summary = mode === 'contribution' && !state.expanded;
    label.hidden = summary;
    previous.hidden = next.hidden = summary || mode === 'monthly';
    if (toggle) { toggle.textContent = summary ? '查看全部' : '返回摘要'; toggle.setAttribute('aria-expanded', String(state.expanded)); }
    if (summary) {
      shown = contributionSummary(rows, row => row.dataset.total === '' ? null : Number(row.dataset.total));
      status.textContent = `獲利前 5＋虧損前 5 · 顯示 ${shown.length}／${rows.length} 項；零貢獻及缺估值項目請查看全部`;
    } else if (mode === 'monthly') {
      shown = state.year === 'recent' ? rows.slice(0, 12) : rows.filter(row => row.dataset.month.startsWith(state.year));
      exportsByTable.set(table, state.year === 'recent' ? rows : shown);
      status.textContent = `顯示 ${shown.length}／${rows.length} 個月 · 匯出${state.year === 'recent' ? '全域篩選內全部月份' : state.year + ' 年篩選結果'}`;
    } else {
      const page = pageWindow(rows.length, state.page, state.size); state.page = page.page;
      shown = rows.slice(page.start, page.end);
      previous.disabled = page.page === 0; next.disabled = page.page === page.pages - 1;
      status.textContent = `${page.start + 1}–${page.end}／${rows.length} 筆 · 第 ${page.page + 1}／${page.pages} 頁`;
    }
    body.replaceChildren(...shown);
    if (!shown.length) {
      const row = document.createElement('tr'); row.className = 'empty-row';
      const cell = document.createElement('td'); cell.colSpan = table.querySelector('thead tr')?.children.length || 1;
      cell.textContent = '沒有符合摘要的項目；可查看全部紀錄。'; row.append(cell); body.append(row);
    }
  }
  render();
}
