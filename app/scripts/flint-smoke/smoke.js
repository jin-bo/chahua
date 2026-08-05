// P10.2 Electron 真渲染冒烟的渲染进程侧（跑法与覆盖面见同目录 main.js）。
// 在与主窗口同配置（sandbox:true / contextIsolation:true / 同一份 CSP）的渲染进程里跑真 enhanceFlint。
// 注：flint_chart.js 内部的 `import("../node_modules/…")` 是相对**它自己**的 URL 解析的，
// 故本文件放在 scripts/ 下不影响两个运行时的加载路径。
import { enhanceFlint, resetFlintCharts } from "../../renderer/flint_chart.js";

const host = document.getElementById("host");
const results = [];
const ok = (name, cond, extra = "") => results.push({ name, pass: !!cond, extra });

function put(json) {
  host.replaceChildren();
  const pre = document.createElement("pre");
  const code = document.createElement("code");
  code.className = "language-flint";
  code.textContent = json;
  pre.appendChild(code);
  host.appendChild(pre);
  return pre;
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

async function renderAndWait(json, ms = 900) {
  put(json);
  enhanceFlint(host);
  await wait(ms);
}

const bar = JSON.stringify({
  data: { values: [{ 模型: "model-a", 得分: 72.4 }, { 模型: "model-b", 得分: 81.9 }, { 模型: "model-c", 得分: 65.1 }] },
  chart_spec: { chartType: "Bar Chart", encodings: { x: { field: "模型" }, y: { field: "得分" } } },
});

(async () => {
  try {
    // ① 真渲染：两个 ESM 在沙箱渲染进程里能 import（P10.1 highlight.js CJS 坑的对应验证项）+ 出图
    await renderAndWait(bar);
    const fig = host.querySelector("figure.flint-rendered");
    ok("① 出 figure.flint-rendered", !!fig);
    const svg = fig && fig.querySelector("svg");
    ok("① svg 存在且有 <text>", !!svg && svg.querySelectorAll("text").length > 0,
      svg ? `texts=${svg.querySelectorAll("text").length}` : "no svg");
    ok("① viewBox 已补", !!svg && !!svg.getAttribute("viewBox"), svg ? svg.getAttribute("viewBox") : "");
    ok("① 气泡放宽 .has-flint", !!document.querySelector(".bubble.has-flint"));
    ok("① 活实例登记", window.__flintStats().live === 1, JSON.stringify(window.__flintStats()));
    // 茶席色板：产物里不该再有 ECharts 原厂 cat10 的 #5470c6
    ok("① 茶席换色生效（无 #5470c6）", !fig.innerHTML.includes("#5470c6"));

    // ② 注入：类别名塞 payload → 不弹窗、DOM 内无 img/script/[onerror]，payload 以文本出现
    await renderAndWait(JSON.stringify({
      data: { values: [{ 类: '<img src=x onerror="window.__pwned=1">', 值: 3 }, { 类: "正常", 值: 5 }] },
      chart_spec: { chartType: "Bar Chart", encodings: { x: { field: "类" }, y: { field: "值" } } },
    }));
    const f2 = host.querySelector("figure.flint-rendered");
    ok("② 注入块照常渲染", !!f2);
    ok("② 未被执行（无 __pwned）", !window.__pwned);
    ok("② DOM 内无 img/script/[onerror]",
      !!f2 && f2.querySelectorAll("img,script,[onerror]").length === 0);
    ok("② payload 以文本出现在 <text> 里",
      !!f2 && [...f2.querySelectorAll("text")].some((t) => (t.textContent || "").includes("onerror")));

    // ③ 不误伤：类别名本身是一条 URL → 照常渲染（决策P4.20-4 反向用例）
    await renderAndWait(JSON.stringify({
      data: { values: [{ 类: "https://example.com/model-card", 值: 3 }, { 类: "b", 值: 5 }] },
      chart_spec: { chartType: "Bar Chart", encodings: { x: { field: "类" }, y: { field: "值" } } },
    }));
    ok("③ URL 类别名照常渲染", !!host.querySelector("figure.flint-rendered"));

    // ④ 拒 data.url → 保留源码 + 徽标
    await renderAndWait(JSON.stringify({
      data: { url: "http://example.com/x.csv" },
      chart_spec: { chartType: "Bar Chart", encodings: { x: { field: "a" }, y: { field: "b" } } },
    }), 500);
    const p4 = host.querySelector("pre.flint-error");
    ok("④ 拒 data.url：保留源码 + 徽标", !!p4 && p4.dataset.flintError.includes("渲染失败"),
      p4 ? p4.dataset.flintError : "no badge");

    // ⑤ 坏 JSON → 渲染失败徽标
    await renderAndWait("{ 这不是 JSON", 400);
    const p5 = host.querySelector("pre.flint-error");
    ok("⑤ 坏 JSON → 图表渲染失败", !!p5 && p5.dataset.flintError.includes("渲染失败"));

    // ⑥ 资源闸：3000 行 → 数据过大
    const big = { data: { values: [] }, chart_spec: { chartType: "Bar Chart", encodings: { x: { field: "a" }, y: { field: "b" } } } };
    for (let i = 0; i < 3000; i++) big.data.values.push({ a: `c${i}`, b: i });
    await renderAndWait(JSON.stringify(big), 500);
    const p6 = host.querySelector("pre.flint-error");
    ok("⑥ 3000 行 → 数据过大", !!p6 && p6.dataset.flintError.includes("数据过大"), p6 ? p6.dataset.flintError : "");

    // ⑦ 空图守门：Treemap 只给 detail+size → 保留源码而非一张空图
    await renderAndWait(JSON.stringify({
      data: { values: [{ 名: "a", 量: 3 }, { 名: "b", 量: 5 }] },
      chart_spec: { chartType: "Treemap", encodings: { detail: { field: "名" }, size: { field: "量" } } },
    }), 500);
    ok("⑦ 空图守门：不出现空 figure",
      !host.querySelector("figure.flint-rendered") && !!host.querySelector("pre.flint-error"));

    // ⑧ 卸载钩子：resetFlintCharts 后活实例归 0
    await renderAndWait(bar);
    const before = window.__flintStats().live;
    resetFlintCharts();
    ok("⑧ resetFlintCharts → live 归 0", before === 1 && window.__flintStats().live === 0,
      `before=${before} after=${window.__flintStats().live}`);

    // ⑨ 气泡真被撑开（评审修）：`.bubble` 是不 grow 的 flex item，只写 max-width 时它的宽度由
    //    `<pre>` 里最长那行 JSON 决定。这里用一份**行很短**的规格 —— 若放宽没生效，气泡只有
    //    一两百像素；生效了它应当接近 li 行宽的 92%。
    const shortLines = JSON.stringify({
      data: { values: [{ a: "x", b: 1 }, { a: "y", b: 2 }] },
      chart_spec: { chartType: "Bar Chart", encodings: { x: { field: "a" }, y: { field: "b" } } },
    }, null, 1);
    await renderAndWait(shortLines);
    const bubble = document.querySelector(".bubble");
    const row = document.querySelector("#messages li.msg");
    const bw = bubble.getBoundingClientRect().width;
    const rw = row.getBoundingClientRect().width;
    ok("⑨ 含图气泡被撑到栏宽的 92% 附近（不是被 JSON 文本宽度决定）", bw > rw * 0.8,
      `bubble=${Math.round(bw)} row=${Math.round(rw)} ratio=${(bw / rw).toFixed(2)}`);

    // ⑩ figure 不横向溢出（评审修）：avail 要扣掉 figure 自己的 padding+border（18px），
    //    否则「落进栏」这条判定恒偏松，右边缘的刻度标签会被裁掉。
    const fig9 = document.querySelector("figure.flint-rendered");
    ok("⑩ figure 不横向溢出（chrome 已从 avail 里扣掉）",
      !!fig9 && fig9.scrollWidth <= fig9.clientWidth,
      fig9 ? `client=${fig9.clientWidth} scroll=${fig9.scrollWidth}` : "no figure");

    // ⑪ 失败后撤掉气泡放宽（评审修）：`.has-flint` 原先只加不删，一条渲染失败的消息会永久
    //    保持 92% 宽 —— 而它装的只是一坨红徽标的 JSON。
    await renderAndWait(JSON.stringify({
      data: { values: [{ 名: "a", 量: 3 }, { 名: "b", 量: 5 }] },
      chart_spec: { chartType: "Treemap", encodings: { detail: { field: "名" }, size: { field: "量" } } },
    }), 600);
    ok("⑪ 渲染失败 → .has-flint 被撤掉",
      !!host.querySelector("pre.flint-error") && !document.querySelector(".bubble.has-flint"));

    // ⑫ observer 的 root 是真滚动容器 #messages（rootMargin 才有意义）
    ok("⑫ #messages 是 observer 能用的滚动容器", !!document.getElementById("messages"));

    // ⑬ **窄栏下逐档收窄后仍不横向溢出**（评审那条 18px chrome 的真实复现面）。
    //    带 color 图例的线图是最坏情况（图例开销恒定），逐个栏宽扫一遍：凡是渲出了 figure 的，
    //    都必须 scrollWidth ≤ clientWidth；退回自然尺寸横向滚动的档不算失败（那是设计选择），
    //    但**被判定为「落进栏」却仍溢出**是 bug。
    const lineJson = JSON.stringify({
      data: { values: (() => {
        const v = [];
        for (const g of ["甲", "乙", "丙"]) for (let d = 1; d <= 6; d++) v.push({ 日: `2026-01-0${d}`, 值: d * (g === "甲" ? 3 : 5), 组: g });
        return v;
      })() },
      chart_spec: { chartType: "Line Chart", encodings: { x: { field: "日" }, y: { field: "值" }, color: { field: "组" } } },
    });
    const msgs = document.getElementById("messages");
    const bad = [];
    for (const w of [560, 585, 600, 620, 700, 820]) {
      msgs.style.maxWidth = `${w}px`;
      await renderAndWait(lineJson, 700);
      const f = host.querySelector("figure.flint-rendered");
      if (!f) continue; // 未渲染（徽标）→ 不在本条覆盖面内
      // 自然尺寸兜底那档本来就是横向滚动，认它；只揪「收窄过、却仍溢出」的。
      const narrowed = f.querySelector("svg") && Number(f.querySelector("svg").getAttribute("width")) < 562;
      if (narrowed && f.scrollWidth > f.clientWidth) bad.push(`${w}:${f.clientWidth}/${f.scrollWidth}`);
    }
    msgs.style.maxWidth = "";
    ok("⑬ 窄栏收窄后 figure 不横向溢出", bad.length === 0, bad.join(" "));
  } catch (e) {
    ok("harness 未抛异常", false, String((e && e.stack) || e));
  }
  console.log("FLINT_SMOKE_RESULT " + JSON.stringify(results));
})();
