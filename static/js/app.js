/* ============================================================
   电商风控系统 —— 通用前端脚本
   ============================================================ */

/** fetch 封装：统一错误码提示，返回解析后的 JSON */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* 空响应体 */ }

  if (!res.ok && data && data.message) {
    toast(`${data.code || "错误"}：${data.message}`, "err");
    throw new Error(data.message);
  }
  return data;
}

/** 全局提示 */
function toast(msg, type = "ok", ms = 3200) {
  const box = document.getElementById("toast");
  if (!box) return;
  const el = document.createElement("div");
  el.className = `toast-item ${type}`;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

/** 决策 → 徽章 class（对齐后端 decision 枚举） */
const DECISION_BADGE = {
  "通过": "badge-pass",
  "标记": "badge-mark",
  "人工审核": "badge-review",
  "二次验证": "badge-challenge",
  "拒绝": "badge-reject",
};
function decisionBadge(d) {
  return `<span class="badge ${DECISION_BADGE[d] || "badge-gray"}">${d || "-"}</span>`;
}

/** 风险等级 → 徽章 class */
const LEVEL_BADGE = { "低": "badge-lv-low", "中": "badge-lv-mid", "高": "badge-lv-high", "极高": "badge-lv-critical" };
function levelBadge(l) {
  return `<span class="badge ${LEVEL_BADGE[l] || "badge-gray"}">${l || "-"}</span>`;
}

/** 名单类型 → 徽章 class */
const LIST_BADGE = { "黑名单": "badge-black", "白名单": "badge-white", "灰名单": "badge-gray" };
function listBadge(t) {
  return `<span class="badge ${LIST_BADGE[t] || "badge-gray"}">${t || "-"}</span>`;
}

/** 千分位 */
function fmt(n) {
  if (n === null || n === undefined) return "-";
  return Number(n).toLocaleString("zh-CN");
}
function fmtMoney(n) {
  if (n === null || n === undefined) return "-";
  return "¥" + Number(n).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

/** ECharts 初始化（自动 resize） */
const _charts = [];
function initChart(id, option) {
  const el = document.getElementById(id);
  if (!el || typeof echarts === "undefined") return null;
  const chart = echarts.init(el);
  chart.setOption(option);
  _charts.push(chart);
  return chart;
}
window.addEventListener("resize", () => _charts.forEach(c => c.resize()));

/** 通用折线图配置 */
function lineOption(categories, series, { yName = "", percent = false } = {}) {
  return {
    tooltip: { trigger: "axis" },
    legend: { bottom: 0, itemHeight: 8, textStyle: { fontSize: 11, color: "#475569" } },
    grid: { left: 8, right: 12, top: 22, bottom: 28, containLabel: true },
    xAxis: {
      type: "category", data: categories, boundaryGap: false,
      axisLine: { lineStyle: { color: "#e2e8f0" } },
      axisLabel: { color: "#94a3b8", fontSize: 11 },
    },
    yAxis: {
      type: "value", name: yName,
      axisLabel: { color: "#94a3b8", fontSize: 11, formatter: percent ? "{value}%" : "{value}" },
      splitLine: { lineStyle: { color: "#f1f5f9" } },
    },
    series: series.map(s => ({
      type: "line", smooth: true, symbolSize: 5, ...s,
    })),
  };
}

/** 通用环形图配置 */
function pieOption(data, centerText, subText) {
  return {
    tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)" },
    legend: { bottom: 0, itemHeight: 8, textStyle: { fontSize: 11, color: "#475569" } },
    series: [{
      type: "pie", radius: ["58%", "78%"], center: ["50%", "42%"],
      avoidLabelOverlap: true, itemStyle: { borderColor: "#fff", borderWidth: 2 },
      label: { show: false },
      emphasis: { label: { show: true, fontSize: 14, fontWeight: "bold" } },
      data,
    }],
    graphic: centerText ? [{
      type: "text", left: "center", top: "34%",
      style: { text: String(centerText), fontSize: 26, fontWeight: 700, fill: "#0f172a", textAlign: "center" },
    }, {
      type: "text", left: "center", top: "48%",
      style: { text: subText || "", fontSize: 11, fill: "#94a3b8", textAlign: "center" },
    }] : [],
  };
}

/** 通用横向条形图配置（规则命中 TOP5） */
function barHOption(categories, values, colors) {
  return {
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    grid: { left: 8, right: 40, top: 8, bottom: 4, containLabel: true },
    xAxis: { type: "value", axisLabel: { color: "#94a3b8", fontSize: 11 }, splitLine: { lineStyle: { color: "#f1f5f9" } } },
    yAxis: {
      type: "category", data: categories,
      axisLabel: { color: "#475569", fontSize: 11.5 },
      axisLine: { lineStyle: { color: "#e2e8f0" } },
    },
    series: [{
      type: "bar", data: values, barWidth: 12,
      itemStyle: { borderRadius: [0, 6, 6, 0], color: p => colors[p.dataIndex % colors.length] },
      label: { show: true, position: "right", color: "#475569", fontSize: 11 },
    }],
  };
}

/** 时间友好显示 */
function timeAgo(iso) {
  if (!iso) return "-";
  const d = new Date(iso.replace(" ", "T"));
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

/** 顶栏全局搜索：按输入内容跳转到对应页面 */
document.addEventListener("DOMContentLoaded", () => {
  const box = document.getElementById("globalSearch");
  if (!box) return;
  box.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const q = box.value.trim();
    if (!q) return;
    if (/^cas/i.test(q)) location.href = `/cases?case_id=${encodeURIComponent(q)}`;
    else if (/^\d+$/.test(q)) location.href = `/cases?user_id=${encodeURIComponent(q)}`;
    else location.href = `/rules?keyword=${encodeURIComponent(q)}`;
  });
});
