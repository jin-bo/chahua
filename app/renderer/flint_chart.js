// P10.2 flint 图表渲染 —— **DOM 半边**。设计见 docs/P10.2-flint 数据图表渲染.md。
//
// 把 ```flint 围栏块（marked fenced_code emit 的 pre>code.language-flint，后端零改、块体为转义 JSON
// 文本）在渲染进程内**编译成 ECharts option 并渲染成 SVG 图表**就地替换那个 <pre>。
// 纯逻辑（白名单 / 资源闸 / 出口断言 / 收窄 / 色板）住 `flint_core.mjs`，本文件只管
// **运行时懒加载 + 量宽 + 视口门 + 实例生命周期 + 事务式替换 + 徽标**（决策P10.2-12 的拆分）。
//
// 两条茶话室独有的机制（观澜没有，见 §3.4 / §3.5）：
// ① **按气泡宽重编译**：气泡 max-width:75% 比观澜的 wiki 栏窄约 40%，照抄「取第一个 _width ≤ 栏宽
//    的档」会把绘图区压到 81 px。故渲染前先给气泡加 .has-flint（92%）**再量宽**，且收窄档必须同时
//    满足「落进栏」与「绘图区 ≥ 160 px」——都不满足就用自然尺寸 + 横向滚动（常规分支不是异常分支）。
// ② **视口门 + 两道回收**：flint + ECharts 是茶话室**第一个有状态渲染器**（mermaid / KaTeX / hljs
//    都是一次性 DOM 变换）。而 renderer.js::renderSidebar 每帧 room_info 都 replaceChildren() 整列表
//    重建 + 全量重放 transcript——不管就是每条 room_info 泄漏 N 个 ECharts 实例 + 全量重编译。

import { FLINT_MAX_CHARS, CHAHUA_FLINT_THEME, flintError, compileFlintOption } from "./flint_core.mjs";

// ── 双运行时懒加载（决策P10.2-1）────────────────────────────────────────────────────
//
// 两个包都是**真·自包含浏览器 ESM**（无 bare import / 无 CJS / 无 process），与 mermaid.esm.mjs /
// katex.mjs / cdn-assets/es/highlight.min.js 同属一类——本渲染进程是 sandbox:true 的纯 Chromium
// ESM 上下文，**无 CJS 互操作**，P10.1 正是在 highlight.js 的 CJS shim 上栽过一次。
// **升级钉版时必须重跑 §3.2 那张自包含性表**（tests/test_frontend_flint.py 里那条用例即是）。
//
// 失败清 Promise 允许重试 —— 但**光清 Promise 不够**（沿决策P4.20-26）：按 HTML 的 module map 语义，
// 一个失败的动态 import() 会把该 URL 的失败结果留在文档存活期内，重发同一 URL 会立刻再 reject、
// 根本不重取（mermaid/KaTeX/hljs 没这问题只因它们至今没真失败过重试过）。故重试换 query。
let _rtPromise = null;
let _rtAttempt = 0;
function loadFlintRuntime() {
  if (_rtPromise) return _rtPromise;
  const bust = _rtAttempt ? `?retry=${_rtAttempt}` : "";
  _rtPromise = Promise.all([
    import(`../node_modules/flint-chart/dist/echarts/index.js${bust}`),
    import(`../node_modules/echarts/dist/echarts.esm.min.js${bust}`),
  ])
    .then(([flint, echarts]) => {
      if (typeof flint.assembleECharts !== "function") throw new Error("no assembleECharts");
      if (typeof echarts.init !== "function") throw new Error("no echarts.init");
      return { assemble: flint.assembleECharts, echarts };
    })
    .catch((e) => {
      _rtPromise = null;
      _rtAttempt++;
      throw e;
    });
  return _rtPromise;
}

// ── 量宽（决策P10.2-4）──────────────────────────────────────────────────────────────
// **顺序是承重的**：`getBoundingClientRect()` 会强制同步 layout，故加 class 必须在量**之前**；
// 反过来做（渲完靠 CSS :has(.flint-rendered) 放宽）会按旧的 75% 定尺寸、白白浪费 17% 宽度。
// 非气泡宿主（若将来有）没有 .bubble 祖先 → closest 返回 null，静默跳过。
function widenBubbleFor(pre) {
  const bubble = pre.closest && pre.closest(".bubble");
  if (bubble) bubble.classList.add("has-flint"); // .bubble.has-flint { max-width:92%; flex-grow:1 }
}

// 图块失败 / 被 LRU 退回源码后把放宽撤掉（评审修：`.has-flint` 原先只加不删，一条渲染失败的
// 消息会永久保持 92% 宽——它装的只是一坨红徽标的 JSON，旁边所有气泡都是 75%）。
// 判据是「这个气泡里还有没有活着的图」：同一条消息里两个块一成一败时不该撤。
function refreshBubbleWidth(node) {
  const bubble = node && node.closest && node.closest(".bubble");
  if (bubble && !bubble.querySelector("figure.flint-rendered")) bubble.classList.remove("has-flint");
}

// `.flint-rendered` 自己的 chrome（padding 8×2 + border 1×2，见 chat.css）。
// **必须从可用宽里扣掉**（评审修）：量的是待替换 `<pre>` 的边框盒，而换上去的 `<figure>` 内容盒
// 比它窄这么多 —— 不扣的话「落进栏」这条判定恒偏松 18px，实测 585px 窗口下 `_width=471` 被判
// 「落进 avail=475」，渲出来 `clientWidth=473 / scrollWidth=487`，右边缘的末个刻度标签被裁掉。
// 数字与 chat.css 同步：`app/scripts/flint-smoke/` 有一条「figure 不横向溢出」断言钉住它。
const FLINT_FIGURE_CHROME = 18;

// 量待替换 `<pre>` 的边框盒宽（它是块级元素，铺满气泡内容区），再扣掉 figure chrome。
// 不量 `.bubble` 或父容器 `clientWidth`：那含 padding（气泡是 8px 12px），会高估约 24px。
// **前提是气泡真的被撑开了** —— `.bubble` 是 `#messages li.msg`（`display:flex`）里的 flex item
// 且没有 `flex-grow`，天然是 shrink-to-fit：一条只含 ```flint 块的消息，气泡宽度会被
// `<pre>` 里最长那行 JSON 决定（约 350–400px），哪怕栏里空着 750px（评审实测）。
// 故 `.bubble.has-flint` 除 `max-width:92%` 外还带 `flex-grow:1`，让它先长满可用宽再量。
const columnWidth = (pre) =>
  pre ? Math.max(0, Math.floor(pre.getBoundingClientRect().width) - FLINT_FIGURE_CHROME) : 0;

// ── 视口门 + 实例登记簿（决策P10.2-6/-7）──────────────────────────────────────────────
//
// **懒创建**（决策P10.2-6 下半）：写成顶层 `const _io = new IntersectionObserver(...)` 会让本模块
// 在非浏览器环境 import 即抛。纯逻辑已拆去 flint_core.mjs（那边结构上就没有这个 API），这里再补
// 一道懒创建，使「先 import 后用」在任何宿主下都安全。
// **root 必须是真正的滚动容器 `#messages`**（评审修）：`rootMargin` 按规范只扩「隐式 root（视口）」
// 的矩形，中间滚动容器的裁剪不带这个 margin —— root 留 null 时那 200px 提前量对聊天列表**完全无效**，
// 每个块都会在用户眼皮底下从 `<pre>` 换成更高的 `<figure>`，下方内容跟着跳。
// `#messages` 不在时（非聊天宿主 / 冒烟页）退回 null：root 必须是 target 的祖先，猜错等于永不相交。
let _io = null;
function io() {
  if (!_io) {
    const root = typeof document !== "undefined" ? document.getElementById("messages") : null;
    _io = new IntersectionObserver(onIntersect, { root: root || null, rootMargin: "200px 0px" });
  }
  return _io;
}

const _pending = new Set(); // 已 observe、尚未渲染的 <pre>（卸载时要 unobserve）
const _charts = new Set(); // { chart, fig, seq }
const FLINT_MAX_LIVE = 12; // 软上限：全部可见时不淘汰
let _seq = 0;
const touch = (fig) => {
  if (fig.__flintRec) fig.__flintRec.seq = ++_seq;
};

function onIntersect(entries) {
  for (const e of entries) {
    const el = e.target;
    if (el.matches && el.matches("figure.flint-rendered")) {
      el.__flintVisible = e.isIntersecting; // 已渲好的图：只更新可见性，供 LRU 判断
      if (e.isIntersecting) touch(el);
    } else if (e.isIntersecting) {
      io().unobserve(el);
      _pending.delete(el);
      renderOneFlintBlock(el, /* wasVisible */ true); // 待渲的 <pre>：这才开始编译
    }
  }
}

function observeFlintBlock(pre) {
  pre.dataset.flintObserved = "1";
  _pending.add(pre);
  io().observe(pre);
}

// 单点释放：dispose + 解除 observe + 出簿。**必须 unobserve**，否则 observer 一直持有脱树 figure。
function dropChart(rec) {
  try {
    rec.chart.dispose();
  } catch {
    /* 已 dispose */
  }
  if (_io) _io.unobserve(rec.fig);
  _charts.delete(rec);
}

// 第一道回收：谁脱树清谁。**放在 enhanceFlint 的早退之前**——切到无图内容也要回收。
function sweepFlintCharts() {
  for (const rec of [..._charts]) {
    let dom = null;
    try {
      dom = rec.chart.getDom && rec.chart.getDom();
    } catch {
      /* 已 dispose → 当作脱树 */
    }
    if (!dom || !dom.isConnected) dropChart(rec);
  }
  for (const pre of [..._pending])
    if (!pre.isConnected) {
      if (_io) _io.unobserve(pre);
      _pending.delete(pre);
    }
}

// 第二道回收：**显式卸载钩子**，renderer.js::renderSidebar 里紧挨 clearPendingArtifactPreviews() 调。
// 与 sweep 的区别：sweep 是「谁脱树清谁」、要靠 enhanceFlint 被调用；而**用户气泡走 makeUserRow →
// textContent、根本不经 enhanceContent** ⇒ 切到一个空房间 / 只有用户消息的房间，sweep 一次都不会跑，
// 上个房间的实例全留着。这个是无条件全清，一定执行。
export function resetFlintCharts() {
  for (const rec of [..._charts]) dropChart(rec);
  if (_io) for (const pre of _pending) _io.unobserve(pre);
  _pending.clear();
}

// 回退成源码 <pre>（LRU 用）：源码存在 fig.dataset.flintSrc，textContent 赋值天然防注入。
function rebuildPreFrom(src) {
  const pre = document.createElement("pre");
  const code = document.createElement("code");
  code.className = "language-flint";
  code.textContent = src || "";
  pre.appendChild(code);
  return pre;
}

// 软上限 LRU（决策P10.2-7）：把**最久未见且当前不可见**的一张回退成源码，并重新交给 observer
// —— 用户滚回去时它会重渲。**回退而非「拒渲」**：拒渲会让「滚过一段长历史」永久毒化后面所有图，
// 而回退是自愈的。
//
// **新图必须先标可见再淘汰**：新 figure 是从一个**刚相交的** <pre> 渲出来的，但 observer 对它的
// 首个回调是**异步**的 —— 在那之前 __flintVisible 是 undefined。若其余 12 张恰好都可见，新的这张
// 就成了唯一「不可见」候选，被当场退回源码 → 再次相交 → 再渲 → 循环。故 renderOneFlintBlock 在
// 登记时用 wasVisible 直接把它标成可见，且**淘汰在登记之后**才跑。
function evictIfNeeded() {
  if (_charts.size <= FLINT_MAX_LIVE) return;
  let victim = null;
  for (const rec of _charts)
    if (!rec.fig.__flintVisible && (!victim || rec.seq < victim.seq)) victim = rec;
  if (!victim) return; // 全部可见 → 不淘汰（上限是软的）
  const pre = rebuildPreFrom(victim.fig.dataset.flintSrc);
  victim.fig.replaceWith(pre);
  dropChart(victim);
  refreshBubbleWidth(pre); // 气泡里若已无活图 → 撤掉 92% 放宽（滚回去重渲时会重新加）
  observeFlintBlock(pre); // 重新入门，滚回去会自愈
}

// ── 失败徽标（沿决策P4.20-20）────────────────────────────────────────────────────────
// **三档文案必须可分辨**，因为修法完全不同：第二档要减**行数**，第三档要减**类别数**，
// 删行对第三档一点用没有。文案走 data-flint-error + CSS ::before（同 .mermaid-error precedent），
// 无 innerHTML、无 i18n。幂等：重复调只是覆盖同一个属性。
function markFlintFailed(pre, kind) {
  if (!pre || !pre.isConnected) return;
  pre.classList.add("flint-error");
  pre.dataset.flintError =
    kind === "tooLarge"
      ? "数据过大，未渲染（已保留源码）"
      : kind === "truncated"
        ? "数据超出图表容量、已被截断，未渲染（已保留源码）"
        : "图表渲染失败（已保留源码）";
  refreshBubbleWidth(pre); // 失败的块不该让气泡永久停在 92%
}

// ── 单块渲染（事务式替换，沿决策P4.20-15）──────────────────────────────────────────
// **`replaceWith` 是整条链的最后一步**：全链在 <pre> 仍挂在树上时完成，渲染期抛错当场 dispose
// 临时实例。反过来做会漏一条失败路径：setOption 抛错时 <pre> 已脱树，既贴不上徽标也回不去源码，
// 气泡里留一个空 figure。**永不空白、零未消毒注入。**
async function renderOneFlintBlock(pre, wasVisible) {
  if (!pre || !pre.isConnected) return;
  const code = pre.querySelector("code.language-flint");
  if (!code) return;
  let rt;
  try {
    rt = await loadFlintRuntime();
  } catch {
    // 运行时加载失败 → 源码留存 + 徽标。**本块不再重试**：它此刻已出 observer、出 `_pending`，
    // 而没有任何注入点会对一条已渲染的消息重跑 `enhanceContent`（`renderGuestText` /
    // `endStreamingMessage` 各只重建自己的 textEl）。`loadFlintRuntime` 里那套换 query 的重试
    // 是给**后续**块用的——它们各自还会进视口。本块要等下一次整份 transcript 重放（切房 /
    // room_info）才有机会。初稿在这里 `delete pre.dataset.flintObserved` 并注释成「能真重试」，
    // 那条路不可达（评审指出）；重新 observe 又会因为它仍在视口里立刻自旋，故老实留徽标。
    markFlintFailed(pre, "renderFail");
    return;
  }
  if (!pre.isConnected) return; // await 期间被重绘替换 / 移除
  try {
    const src = code.textContent; // JSON 文本（浏览器已反转义），**非 HTML**
    if (src.length > FLINT_MAX_CHARS) throw flintError("tooLarge", "too-big");
    widenBubbleFor(pre); // 先放宽气泡……
    const avail = columnWidth(pre); // ……再量宽（顺序承重，决策P10.2-4）
    // 编译链整条住在 flint_core.mjs，与 node 层用例共用同一份（决策P10.2-12 / 评审修）：
    // 入口白名单 → 按气泡宽重编译 → 形态守门 → 尺寸闸 → 截断闸 → 剥私有键 → 出口断言 →
    // 茶席换色 → formatter 压文本。尺寸这件事交还给 flint（决策P4.20-19）。
    const { opt, width: w, height: h } = compileFlintOption(rt.assemble, JSON.parse(src), avail);
    const fig = document.createElement("figure");
    fig.className = "flint-rendered";
    fig.dataset.flintSrc = src; // LRU 回退时据此重建 <pre>
    // init 允许**离树**元素的前提是显式给 width/height —— 这里正好给了（已过尺寸闸）。
    // 主题走**第二参**，不做 {...opt, ...THEME} 顶层展开（那会盖掉 flint 算好的 xAxis/yAxis）。
    const chart = rt.echarts.init(fig, CHAHUA_FLINT_THEME, { renderer: "svg", width: w, height: h });
    try {
      // renderMode:'richText' → tooltip 作为图元绘制、**不走 HTML 字符串拼接**（ECharts 历史 XSS
      // 多发于 HTML tooltip + 自定义 formatter，本相位把这条路整条关掉，决策P4.20-4）。
      // 喂进去的 formatter 已被 plainTextFormatters 压成纯文本，故 richText 不会画出字面标签。
      chart.setOption({ ...opt, tooltip: { ...(opt.tooltip || {}), renderMode: "richText" } });
    } catch (e) {
      chart.dispose(); // 临时实例不泄漏，源码仍原样挂在树上
      throw e;
    }
    // 补 viewBox（沿决策P4.20-17）：ECharts **浏览器端**的 SVG 渲染器只写死 width/height 属性、
    // **不写 viewBox**（只有 SSR 的 renderToSVGString 会写）。补上它，窄气泡下 CSS 至少能等比收住
    // 溢出部分；真正让图落进气泡的是上面的 assembleFitted，这一行只是兜底。
    const svg = fig.querySelector("svg");
    if (svg && !svg.getAttribute("viewBox")) svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    pre.replaceWith(fig); // 只有全程成功才动 DOM
    const rec = { chart, fig, seq: ++_seq };
    fig.__flintRec = rec;
    fig.__flintVisible = wasVisible; // **登记即标可见**（否则会被自己淘汰成循环）
    _charts.add(rec);
    io().observe(fig); // 继续跟踪可见性，供 LRU
    evictIfNeeded(); // 淘汰在登记之后
  } catch (e) {
    // 分档见 flintError：tooLarge（资源闸）/ truncated（flint 丢了数据）/ renderFail（其余一切）。
    // 三者都保留 <pre> 源码（决策P4.20-10：永不空白、零未消毒注入），只是文案不同。
    markFlintFailed(pre, (e && e.flintKind) || "renderFail");
  }
}

// ── 编排器入口 ─────────────────────────────────────────────────────────────────────
// 经 chat_view.js::enhanceContent 在三处注入点调用（renderGuestText / endStreamingMessage ×2）。
// **不要在流式 delta 期间调**：半截 JSON 必然 JSON.parse 失败刷徽标。
// 幂等可重入：渲成 <figure> 后原 code 已离树；已 observe 过的 <pre> 带 data-flint-observed 跳过。
export function enhanceFlint(container) {
  if (!container) return;
  sweepFlintCharts(); // 决策P10.2-7：先清扫脱树实例（放在早退**之前**——切到无图内容也要回收）
  const blocks = container.querySelectorAll("pre > code.language-flint");
  if (!blocks.length) return; // 早退：无图块零加载这 1.65 MB（懒，决策P10.2-1）
  for (const code of blocks) {
    const pre = code.parentElement;
    if (!pre || pre.dataset.flintObserved === "1") continue;
    observeFlintBlock(pre); // 视口门：只有真滚到眼前的块才付编译代价
  }
}

// 开发态探针（§7 验收 4）：`echarts.getInstanceByDom(dom)` 只能按单个 DOM 查、不能枚举，更看不见
// 已脱树的泄漏实例。DevTools Console 里 `__flintStats()` 看活实例数——切到空房间应当归 0。
if (typeof window !== "undefined")
  window.__flintStats = () => ({ live: _charts.size, pending: _pending.size });
