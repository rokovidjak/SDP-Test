/* ECharts wrappers: activity timeline + author ownership donut. */
(function () {
  "use strict";

  const AXIS = "#64748b";
  const GRID = "#e2e8f0";
  const TIP = {
    backgroundColor: "#ffffff",
    borderColor: "#e3e9f0",
    textStyle: { color: "#0f172a", fontSize: 12 },
    extraCssText: "box-shadow:0 18px 44px -18px rgba(15,23,42,.28);border-radius:10px;padding:8px 12px;",
  };
  const PALETTE = ["#0891b2", "#14b8a6", "#6366f1", "#0ea5e9", "#8b5cf6",
                   "#22d3ee", "#f59e0b", "#ec4899", "#10b981", "#94a3b8"];

  let timelineChart = null;
  let ownershipChart = null;
  let treemapChart = null;

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
        ...TIP,
        trigger: "axis",
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
        splitLine: { lineStyle: { color: "rgba(226,232,240,.9)" } },
      },
      dataZoom: [
        { type: "inside", xAxisIndex: 0 },
        {
          type: "slider", xAxisIndex: 0, height: 20, bottom: 8,
          borderColor: GRID, backgroundColor: "rgba(248,250,252,.9)",
          fillerColor: "rgba(6,182,212,.14)",
          handleStyle: { color: "#06b6d4" },
          textStyle: { color: AXIS },
        },
      ],
      series: [
        {
          name: "Added", type: "bar", data: added, stack: "a",
          itemStyle: { color: "#14b8a6" }, barMaxWidth: 14,
        },
        {
          name: "Removed", type: "bar", data: removed, stack: "a",
          itemStyle: { color: "#f43f5e" }, barMaxWidth: 14,
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
        ...TIP,
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
        itemStyle: { borderColor: "#ffffff", borderWidth: 2 },
        label: { show: false },
        emphasis: { label: { show: true, color: "#0f172a", fontSize: 12, fontWeight: 600 } },
        data,
      }],
    }, true);
    chart.off("click");
    chart.on("click", (p) => {
      if (onSelect && p && p.data && p.data.authorId != null) onSelect(p.data.authorId);
    });
  }

  function ensureTreemap() {
    if (!treemapChart && el("treemap")) treemapChart = echarts.init(el("treemap"));
    return treemapChart;
  }

  /* Drillable project map: area = metric, colour = growth (green) vs
     decline (red), click a tile to descend into that child object. */
  function setTreemap(nodes, metric, onSelect) {
    const chart = ensureTreemap();
    if (!chart || !nodes || !nodes.length) return;
    const fmtN = (v) => Number(v || 0).toLocaleString("en-US");
    const signN = (v) => (v > 0 ? "+" : "") + fmtN(v);
    const data = nodes.map((n) => {
      const raw = n[metric] == null ? 0 : n[metric];
      const churn = n.churn || 0;
      const ratio = churn > 0 ? Math.max(-1, Math.min(1, n.growth / churn)) : 0;
      const rgb = ratio < 0 ? "244,63,94" : "16,185,129";
      const alpha = 0.20 + 0.55 * Math.abs(ratio);
      return {
        name: n.name + (n.kind === "dir" ? "/" : ""),
        value: Math.max(Math.abs(raw), 0.001),
        objKind: n.kind,
        objPath: n.path,
        added: n.added,
        removed: n.removed,
        growth: n.growth,
        churn: n.churn,
        modifications: n.modifications,
        itemStyle: { color: `rgba(${rgb},${alpha.toFixed(3)})` },
      };
    });
    chart.setOption({
      animationDuration: 300,
      tooltip: {
        ...TIP,
        formatter: (p) => {
          const d = p.data;
          return `<b>${d.name}</b><br>` +
            `added ${fmtN(d.added)} · removed ${fmtN(d.removed)}<br>` +
            `growth ${signN(d.growth)} · churn ${fmtN(d.churn)}<br>` +
            (d.modifications == null ? "" : `modifications ${fmtN(d.modifications)}<br>`) +
            `<span style="color:#64748b">click to open</span>`;
        },
      },
      series: [{
        type: "treemap",
        roam: false,
        nodeClick: false,
        breadcrumb: { show: false },
        label: {
          show: true,
          color: "#0f172a",
          fontSize: 11,
          formatter: (p) => (p.name.length > 26 ? p.name.slice(0, 25) + "…" : p.name),
        },
        upperLabel: { show: false },
        itemStyle: { borderColor: "#ffffff", borderWidth: 1, gapWidth: 2 },
        emphasis: { itemStyle: { borderColor: "#0891b2", borderWidth: 2 } },
        data,
      }],
    }, true);
    chart.off("click");
    chart.on("click", (p) => {
      if (onSelect && p && p.data && p.data.objPath != null) {
        onSelect({ kind: p.data.objKind, path: p.data.objPath });
      }
    });
  }

  function resize() {
    if (timelineChart) timelineChart.resize();
    if (ownershipChart) ownershipChart.resize();
    if (treemapChart) treemapChart.resize();
  }
  window.addEventListener("resize", resize);

  window.Charts = { setTimeline, visibleRange, setOwnership, setTreemap, resize };
})();
