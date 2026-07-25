const RANGE_DAYS = Object.freeze({
  "1M": 31,
  "3M": 93,
  "6M": 186,
  "1Y": 366,
  ALL: Infinity,
});

export function numeric(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function formatCurrency(value, options = {}) {
  const parsed = numeric(value);
  if (parsed === null) return "—";
  const { sign = false, compact = false } = options;
  const formatted = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: compact ? "compact" : "standard",
    minimumFractionDigits: compact ? 1 : 2,
    maximumFractionDigits: compact ? 1 : 2,
  }).format(Math.abs(parsed));
  if (!sign || parsed === 0) return parsed < 0 ? `-${formatted}` : formatted;
  return `${parsed > 0 ? "+" : "-"}${formatted}`;
}

export function formatPercent(value, options = {}) {
  const parsed = numeric(value);
  if (parsed === null) return "—";
  const { sign = false, digits = 2 } = options;
  const formatted = new Intl.NumberFormat("zh-HK", {
    style: "percent",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    signDisplay: sign ? "exceptZero" : "auto",
  }).format(parsed);
  return formatted;
}

export function formatNumber(value, digits = 2) {
  const parsed = numeric(value);
  if (parsed === null) return "—";
  return new Intl.NumberFormat("en-US", {
    maximumFractionDigits: digits,
  }).format(parsed);
}

export function formatDate(value, includeTime = false) {
  if (!value) return "—";
  const date = /^\d{4}-\d{2}-\d{2}$/.test(value)
    ? new Date(`${value}T00:00:00Z`)
    : new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-HK", {
    year: "numeric",
    month: "short",
    day: "numeric",
    ...(includeTime
      ? { hour: "2-digit", minute: "2-digit", hour12: false }
      : {}),
    timeZone: "Asia/Hong_Kong",
  }).format(date);
}

export function valueClass(value) {
  const parsed = numeric(value);
  if (parsed === null || parsed === 0) return "is-neutral";
  return parsed > 0 ? "is-positive" : "is-negative";
}

export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

export function maxDate(items, accessor = (item) => item.date) {
  const timestamps = items
    .map((item) => new Date(`${accessor(item).slice(0, 10)}T00:00:00Z`).getTime())
    .filter(Number.isFinite);
  return timestamps.length ? new Date(Math.max(...timestamps)) : null;
}

export function filterByRange(items, range, accessor = (item) => item.date) {
  if (range === "ALL" || !RANGE_DAYS[range] || !items.length) return [...items];
  const latest = maxDate(items, accessor);
  if (!latest) return [...items];
  const cutoff = new Date(latest);
  cutoff.setUTCDate(cutoff.getUTCDate() - RANGE_DAYS[range]);
  return items.filter((item) => {
    const timestamp = new Date(`${accessor(item).slice(0, 10)}T00:00:00Z`);
    return timestamp >= cutoff;
  });
}

function csvEscape(value) {
  const normalized = String(value ?? "").replace(/\s+/g, " ").trim();
  return /[",\n]/.test(normalized)
    ? `"${normalized.replaceAll('"', '""')}"`
    : normalized;
}

export function exportTableToCsv(table, filename) {
  const rows = [...table.querySelectorAll("tr")]
    .filter((row) => !row.classList.contains("empty-row"))
    .map((row) =>
      [...row.querySelectorAll("th, td")]
        .map((cell) => csvEscape(cell.textContent))
        .join(","),
    );
  if (!rows.length) return false;
  const blob = new Blob([`\uFEFF${rows.join("\n")}`], {
    type: "text/csv;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  return true;
}

export function emptyRow(columnCount, message = "暫時未有數據") {
  return `<tr class="empty-row"><td colspan="${columnCount}">
    <div class="empty-state"><span aria-hidden="true">◇</span>${escapeHtml(message)}</div>
  </td></tr>`;
}

export function dateOnly(value) {
  return String(value ?? "").slice(0, 10);
}
