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

function dateTimestamp(value) {
  const day = dateOnly(value);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return null;
  const timestamp = Date.parse(`${day}T00:00:00Z`);
  return Number.isFinite(timestamp) ? timestamp : null;
}

export function maxDate(items, accessor = (item) => item.date) {
  const timestamps = items
    .map((item) => dateTimestamp(accessor(item)))
    .filter((timestamp) => timestamp !== null);
  return timestamps.length ? new Date(Math.max(...timestamps)) : null;
}

export function rangeStartDate(range, endDate) {
  if (range === "ALL" || !RANGE_DAYS[range]) return null;
  const endTimestamp = dateTimestamp(endDate);
  if (endTimestamp === null) return null;
  const cutoff = new Date(endTimestamp);
  cutoff.setUTCDate(cutoff.getUTCDate() - RANGE_DAYS[range]);
  return cutoff.toISOString().slice(0, 10);
}

export function filterByRange(
  items,
  range,
  accessor = (item) => item.date,
  endDate = null,
) {
  if (!RANGE_DAYS[range] || !items.length) return [...items];
  const explicitEnd = dateTimestamp(endDate);
  // ALL has no lower bound, but an explicit valuation cutoff still applies.
  if (range === "ALL") {
    if (explicitEnd === null) return [...items];
    return items.filter((item) => {
      const timestamp = dateTimestamp(accessor(item));
      return timestamp !== null && timestamp <= explicitEnd;
    });
  }
  const latest =
    explicitEnd === null ? maxDate(items, accessor) : new Date(explicitEnd);
  if (!latest) return [...items];
  const cutoff = Date.parse(
    `${rangeStartDate(range, latest.toISOString())}T00:00:00Z`,
  );
  const end = latest.getTime();
  return items.filter((item) => {
    const timestamp = dateTimestamp(accessor(item));
    return timestamp !== null && timestamp >= cutoff && timestamp <= end;
  });
}

export function csvEscape(value) {
  const normalized = String(value ?? "").replace(/\s+/g, " ").trim();
  const safe = /^[=+\-@]/.test(normalized)
    ? `'${normalized}`
    : normalized;
  return /[",\n]/.test(safe)
    ? `"${safe.replaceAll('"', '""')}"`
    : safe;
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
