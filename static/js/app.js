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
  // FastAPI 的 422 响应体是 {detail:[...]}，没有 message 字段 —— 上面那个分支
  // 接不住它，data 会原样返回 undefined.items，调用方在 data.items.length
  // 上抛 "Cannot read properties of undefined"，页面永远停在转圈。
  // 这里把 422 也归一成 Error，让失败在调用点就断掉并有明确文案。
  if (!res.ok) {
    const detail = Array.isArray(data?.detail)
      ? data.detail.map(d => `${(d.loc || []).join(".")}: ${d.msg}`).join("; ")
      : (data?.detail ? JSON.stringify(data.detail) : "");
    const msg = `请求失败 HTTP ${res.status}${detail ? " —— " + detail : ""}`;
    toast(msg, "err");
    throw new Error(msg);
  }
  return data;
}

/**
 * 构造查询串，**丢掉空值参数**。
 *
 * 为什么必须丢：FastAPI 对 `int | None` 这类带类型的可选参数，收到空串
 * 不会当成「未提供」，而是在类型转换阶段直接判 422
 * （`PageSize`/`is_enabled=` → "Input should be a valid integer"）。
 * 而 `<select>` 的「全部」选项 value 恰好就是空串 —— 于是「不加筛选」
 * 反而成了唯一必错的请求。这个坑在 /rules 上真实发生过（整页加载不出来），
 * 所以在公共 helper 里统一兜住，页面不必各自记得判空。
 *
 * 保留 `false` 与 `0`：它们是合法取值，只有 ""/null/undefined 才算未填。
 */
function qs(params) {
  const p = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v === "" || v === null || v === undefined) return;
    p.set(k, v);
  });
  return p.toString();
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

/* ---------------- 图标 ----------------
   路径表由 base.html 注入（window.ICON_PATHS），与 templates/_icons.html 的
   PATHS 是同一份数据 —— 服务端 {{ ui.icon() }} 和这里拼出来的 SVG 完全一致。

   JS 里为什么要自己拼而不用 sprite 的 <use>：统计卡、空状态、表格行都是
   innerHTML 动态生成的，用 <use href="#i-x"> 也行，但拼内联路径少一次
   shadow DOM 查找，也让打印/导出时图标不依赖 sprite 是否还在 DOM 里。 */
function svgIcon(name, size = 16, cls = "") {
  const body = (window.ICON_PATHS || {})[name];
  // 图标名拼错时别静默渲染成空白 —— 控制台点名，界面留空但不报错
  if (!body) { console.warn(`[svgIcon] 未知图标名: ${name}`); return ""; }
  return `<svg class="icon ${cls}" width="${size}" height="${size}" viewBox="0 0 24 24"`
    + ` fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round"`
    + ` stroke-linejoin="round" aria-hidden="true" focusable="false">${body}</svg>`;
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

/* ---------------- 返回上一页 ----------------
   左上角的左箭头就是「返回」，位置在页面标题旁，符合用户对后退键的预期
   （原先这个位置放的是折叠侧边栏，点下去不跳转而是收起菜单，很反直觉）。

   历史栈为空时（比如从书签直接打开、或在新标签页打开）不能干等着没反应，
   否则用户会以为按钮坏了 —— 退到仪表盘兜底。 */
(function () {
  const btn = document.getElementById("btnBack");
  if (!btn) return;
  btn.addEventListener("click", () => {
    if (window.history.length > 1) window.history.back();
    else location.href = "/";
  });
})();

/* ---------------- 侧边栏折叠 ----------------
   状态存 localStorage（隐私模式下静默降级为不持久化）。
   折叠后内容区变宽，需要通知 ECharts 重算尺寸 —— 否则图表会被拉伸变形。 */
(function () {
  const shell = document.getElementById("appShell");
  const btn = document.getElementById("btnCollapse");
  if (!shell || !btn) return;

  const KEY = "risk:sidebar:collapsed";

  // 优先级：URL 参数 > localStorage。
  // 支持 URL 参数不光是为了调试 —— 它让「侧边栏收起」成为一个可分享的链接状态。
  const qs = new URLSearchParams(location.search);
  const forced = qs.get("sidebar");
  if (forced === "collapsed" || forced === "expanded") {
    shell.classList.toggle("collapsed", forced === "collapsed");
  } else {
    try {
      if (localStorage.getItem(KEY) === "1") shell.classList.add("collapsed");
    } catch (e) { /* ignore */ }
  }

  btn.addEventListener("click", () => {
    const collapsed = shell.classList.toggle("collapsed");
    try { localStorage.setItem(KEY, collapsed ? "1" : "0"); } catch (e) { /* ignore */ }
    // 等 CSS 过渡（.2s）走完再 resize，避免图表拿到中间态尺寸
    setTimeout(() => window.dispatchEvent(new Event("resize")), 220);
  });

  // 排查 UI 用：?diag=1 时给侧边栏各元素描边
  //   黄 = 导航项占位，绿 = 图标占位，蓝 = 分组标题占位
  // 配 headless 截图即可看出「元素占了空间但内容不可见」这类问题。
  if (qs.get("diag") === "1") {
    setTimeout(() => {
      document.querySelectorAll(".side-nav-item").forEach(el => {
        el.style.background = "rgba(250, 204, 21, .4)";
        const svg = el.querySelector("svg");
        if (svg) svg.style.background = "rgba(34, 197, 94, .5)";
      });
      document.querySelectorAll(".side-nav-group").forEach(el => {
        el.style.background = "rgba(59, 130, 246, .4)";
      });
    }, 400);
  }
})();
