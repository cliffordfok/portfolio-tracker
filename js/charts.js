import { formatCurrency, formatDate, formatPercent, numeric } from "./utils.js";

const COLORS = Object.freeze({
  paper: "#42e8b4",
  live: "#65a8ff",
  benchmark: "#b58cff",
  grid: "#263747",
  axis: "#7890a3",
});

function chartFrame(element, height) {
  element.replaceChildren();
  if (!window.d3) {
    element.innerHTML =
      '<div class="chart-empty">圖表程式庫未能載入，表格數據仍可正常使用。</div>';
    return null;
  }
  const width = Math.max(element.clientWidth || 720, 320);
  const margin = { top: 18, right: 24, bottom: 38, left: 68 };
  const svg = window.d3
    .select(element)
    .append("svg")
    .attr("viewBox", `0 0 ${width} ${height}`)
    .attr("role", "img");
  return {
    svg,
    width,
    height,
    margin,
    innerWidth: width - margin.left - margin.right,
    innerHeight: height - margin.top - margin.bottom,
  };
}

function drawAxes(frame, x, y, formatter) {
  const { svg, margin, innerWidth, innerHeight } = frame;
  const plot = svg
    .append("g")
    .attr("transform", `translate(${margin.left},${margin.top})`);

  plot
    .append("g")
    .attr("class", "chart-grid")
    .call(
      window.d3
        .axisLeft(y)
        .ticks(5)
        .tickSize(-innerWidth)
        .tickFormat(""),
    );
  plot
    .append("g")
    .attr("class", "chart-axis")
    .attr("transform", `translate(0,${innerHeight})`)
    .call(
      window.d3
        .axisBottom(x)
        .ticks(Math.min(6, Math.max(2, Math.floor(innerWidth / 130))))
        .tickFormat(window.d3.timeFormat("%b %d")),
    );
  plot
    .append("g")
    .attr("class", "chart-axis")
    .call(window.d3.axisLeft(y).ticks(5).tickFormat(formatter));
  return plot;
}

function showEmpty(element, message = "所選區間未有足夠數據") {
  element.replaceChildren();
  element.innerHTML = `<div class="chart-empty">${message}</div>`;
}

function addTooltip(element, frame, plot, x, y, allDates, series, formatter) {
  const tooltip = document.createElement("div");
  tooltip.className = "chart-tooltip";
  tooltip.hidden = true;
  element.append(tooltip);
  const focus = plot
    .append("line")
    .attr("class", "chart-focus-line")
    .attr("y1", 0)
    .attr("y2", frame.innerHeight)
    .style("opacity", 0);

  plot
    .append("rect")
    .attr("class", "chart-overlay")
    .attr("width", frame.innerWidth)
    .attr("height", frame.innerHeight)
    .on("pointermove", function (event) {
      const [pointerX] = window.d3.pointer(event, this);
      const target = x.invert(pointerX);
      const bisect = window.d3.bisector((value) => value).center;
      const index = bisect(allDates, target);
      const date = allDates[Math.max(0, Math.min(index, allDates.length - 1))];
      const values = series
        .map((item) => {
          const point = item.values.find(
            (entry) => entry.parsedDate.getTime() === date.getTime(),
          );
          return point?.value == null
            ? null
            : `<span><i style="background:${item.color}"></i>${item.label}<b>${formatter(point.value)}</b></span>`;
        })
        .filter(Boolean)
        .join("");
      tooltip.innerHTML = `<strong>${formatDate(date.toISOString())}</strong>${values}`;
      tooltip.hidden = false;
      tooltip.style.left = `${Math.min(pointerX + frame.margin.left + 14, frame.width - 210)}px`;
      tooltip.style.top = `${frame.margin.top + 10}px`;
      focus.attr("x1", x(date)).attr("x2", x(date)).style("opacity", 1);
    })
    .on("pointerleave", () => {
      tooltip.hidden = true;
      focus.style("opacity", 0);
    });
}

export function renderSeriesChart(
  selector,
  inputSeries,
  { valueType = "currency", height = 310 } = {},
) {
  const element = document.querySelector(selector);
  if (!element) return;
  const series = inputSeries
    .map((item) => ({
      ...item,
      color: item.color || COLORS[item.key],
      values: item.values
        .map((entry) => ({
          ...entry,
          parsedDate: new Date(`${entry.date}T00:00:00Z`),
          value: numeric(entry.value),
        }))
        .sort((a, b) => a.parsedDate - b.parsedDate),
    }))
    .filter((item) => item.values.length);

  const validPoints = series.flatMap((item) =>
    item.values.filter((point) => point.value !== null),
  );
  if (!validPoints.length) {
    showEmpty(element);
    return;
  }

  const frame = chartFrame(element, height);
  if (!frame) return;
  const allDates = [
    ...new Map(
      series
        .flatMap((item) => item.values.map((point) => point.parsedDate))
        .map((date) => [date.getTime(), date]),
    ).values(),
  ].sort((a, b) => a - b);
  const xExtent = window.d3.extent(allDates);
  if (xExtent[0].getTime() === xExtent[1].getTime()) {
    xExtent[0] = new Date(xExtent[0].getTime() - 86400000);
    xExtent[1] = new Date(xExtent[1].getTime() + 86400000);
  }
  const yValues = validPoints.map((point) => point.value);
  let yMin = Math.min(...yValues, 0);
  let yMax = Math.max(...yValues, 0);
  const padding = Math.max((yMax - yMin) * 0.16, valueType === "percent" ? 0.005 : 10);
  yMin -= padding;
  yMax += padding;

  const x = window.d3
    .scaleUtc()
    .domain(xExtent)
    .range([0, frame.innerWidth]);
  const y = window.d3
    .scaleLinear()
    .domain([yMin, yMax])
    .nice()
    .range([frame.innerHeight, 0]);
  const formatter =
    valueType === "percent"
      ? (value) => formatPercent(value, { digits: 1 })
      : (value) => formatCurrency(value, { compact: true });
  const plot = drawAxes(frame, x, y, formatter);

  plot
    .append("line")
    .attr("class", "chart-zero-line")
    .attr("x1", 0)
    .attr("x2", frame.innerWidth)
    .attr("y1", y(0))
    .attr("y2", y(0));

  const line = window.d3
    .line()
    .defined((point) => point.value !== null)
    .x((point) => x(point.parsedDate))
    .y((point) => y(point.value))
    .curve(window.d3.curveMonotoneX);

  for (const item of series) {
    plot
      .append("path")
      .datum(item.values)
      .attr("class", "chart-line")
      .attr("stroke", item.color)
      .attr("d", line);
    const last = [...item.values].reverse().find((point) => point.value !== null);
    if (last) {
      plot
        .append("circle")
        .attr("class", "chart-endpoint")
        .attr("cx", x(last.parsedDate))
        .attr("cy", y(last.value))
        .attr("r", 4)
        .attr("fill", item.color);
    }
  }
  frame.svg.attr(
    "aria-label",
    `${series.map((item) => item.label).join("、")}時間序列圖`,
  );
  addTooltip(element, frame, plot, x, y, allDates, series, formatter);
}
