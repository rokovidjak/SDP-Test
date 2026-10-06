/* ECharts wrappers: activity timeline + author ownership donut. */
(function () {
  "use strict";

  const AXIS = "#8b98a9";
  const GRID = "#273141";
  const PALETTE = ["#4f8cff", "#7cc4ff", "#3fb950", "#d29922", "#f85149",
                   "#a371f7", "#39c5cf", "#db61a2", "#e3b341", "#56d4dd"];

  let timelineChart = null;
  let ownershipChart = null;

  function el(id) { return document.getElementById(id); }

  function ensureTimeline() {
    if (!timelineChart && el("timeline")) timelineChart = echarts.init(el("timeline"));
    return timelineChart;
  }

  function ensureOwnership() {
    if (!ownershipChart && el("ownership")) ownershipChart = echarts.init(el("ownership"));
    return ownershipChart;
  }

  function setTimeline(series) {
    const chart = ensureTimeline();
    if (!chart) return;
    const added = series.map((p) => [p.ts * 1000, p.added]);
    const removed = series.map((p) => [p.ts * 1000, -p.removed]);
    chart.setOption({
      animationDuration: 300,
      grid: { left: 62, right: 16, top: 28, bottom: 50 },
      tooltip: {
        trigger: "axis",
        backgroundColor: "#1b232e",
        borderColor: GRID,
        textStyle: { color: "#e6edf3", fontSize: 12 },
        formatter: function (params) {
          if (!params || !params.length) return "";
          const ts = params[0].value[0];
          const lines = [new Date(ts).toLocaleString("en-GB", {
            year: "numeric", month: "short", day: "2-digit",
            hour: "2-digit", minute: "2-digit",
          })];
          for (const p of params) {
            const v = p.value[1];
            lines.push(`${p.marker} ${p.seriesName}: <b>${v >= 0 ? "+" : "-"}${Math.abs(v).toLocaleString("en-US")}</b>`);
          }
          return lines.join("<br>");
        },
      },
      legend: { top: 2, right: 10, textStyle: { color: AXIS }, data: ["Added", "Removed"] },
      xAxis: {
        type: "time",
        axisLine: { lineStyle: { color: GRID } },
        axisLabel: { color: AXIS, formatter: { year: "{yyyy}", month: "{MMM} {yyyy}", day: "{dd} {MMM}", hour: "{HH}:{mm}", minute: "{HH}:{mm}" } },
        splitLine: { show: false },
      },
      yAxis: {
        type: "value",
        axisLabel: { color: AXIS, formatter: (v) => Math.abs(v).toLocaleString("en-US") },
        splitLine: { lineStyle: { color: "rgba(39,49,65,.6)" } },
      },
      dataZoom: [
        { type: "inside", xAxisIndex: 0 },
        {
          type: "slider", xAxisIndex: 0, height: 20, bottom: 8,
          borderColor: GRID, backgroundColor: "rgba(27,35,46,.6)",
          fillerColor: "rgba(79,140,255,.18)",
          handleStyle: { color: "#4f8cff" },
          textStyle: { color: AXIS },
        },
      ],
      series: [
        {
          name: "Added", type: "bar", data: added, stack: "a",
          itemStyle: { color: "#3fb950" }, barMaxWidth: 14,
        },
        {
          name: "Removed", type: "bar", data: removed, stack: "a",
          itemStyle: { color: "#f85149" }, barMaxWidth: 14,
        },
      ],
    }, true);
  }

  /* Current visible x-range of the timeline, in unix seconds, or null. */
  function visibleRange() {
    if (!timelineChart) return null;
    const opt = timelineChart.getOption();
    if (!opt || !opt.dataZoom || !opt.dataZoom.length) return null;
    const z = opt.dataZoom[0];
    if (z.start === 0 && z.end === 100 && z.startValue == null) return null;
    let start = z.startValue;
    let end = z.endValue;
    const data = (opt.series && opt.series[0] && opt.series[0].data) || [];
    if (start == null || end == null || typeof start !== "number") {
      if (!data.length) return null;
      const lo = data[0][0];
      const hi = data[data.length - 1][0];
      start = lo + (hi - lo) * ((z.start || 0) / 100);
      end = lo + (hi - lo) * ((z.end == null ? 100 : z.end) / 100);
    }
    if (!(start < end)) return null;
    return { since: Math.floor(start / 1000), until: Math.ceil(end / 1000) };
  }

  function setOwnership(rows, onSelect) {
    const chart = ensureOwnership();
    if (!chart) return;
    const data = rows.map((r) => ({
      name: r.name,
      value: r.churn,
      authorId: r.author_id,
      ownership: r.ownership,
    }));
    chart.setOption({
      animationDuration: 300,
      color: PALETTE,
      tooltip: {
        backgroundColor: "#1b232e",
        borderColor: GRID,
        textStyle: { color: "#e6edf3", fontSize: 12 },
        formatter: (p) => `${p.marker} <b>${p.name}</b><br>` +
          `churn ${Number(p.value).toLocaleString("en-US")}<br>` +
          `ownership ${(p.data.ownership * 100).toFixed(1)}%`,
      },
      legend: {
        bottom: 0, type: "scroll", textStyle: { color: AXIS, fontSize: 11 },
        pageTextStyle: { color: AXIS },
      },
      series: [{
        type: "pie",
        radius: ["48%", "76%"],
        center: ["50%", "44%"],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: "#151b23", borderWidth: 2 },
        label: { show: false },
        emphasis: { label: { show: true, color: "#e6edf3", fontSize: 12, fontWeight: 600 } },
        data,
      }],
    }, true);
    chart.off("click");
    chart.on("click", (p) => {
      if (onSelect && p && p.data && p.data.authorId != null) onSelect(p.data.authorId);
    });
  }

  function resize() {
    if (timelineChart) timelineChart.resize();
    if (ownershipChart) ownershipChart.resize();
  }
  window.addEventListener("resize", resize);

  window.Charts = { setTimeline, visibleRange, setOwnership, resize };
})();
