// P10.2 flint 图表渲染 —— **纯逻辑半边**。设计见 docs/P10.2-flint 数据图表渲染.md。
//
// **本文件零浏览器 API**（无 document / 无 IntersectionObserver / 无 ECharts）——决策P10.2-12：
// `app/package.json` 没有 `"type":"module"` 且**不能加**（`main` 是 Electron 主进程的 CommonJS），
// 于是 `renderer/*.js` 在 Node 眼里是 CJS，`.js` 直接 import 在 Node 18 上是 SyntaxError，
// 而本机 Node 22.7+ 的语法探测会猜成 ESM 假绿。故纯逻辑住 `.mjs`（Node ≥12 与浏览器都无歧义），
// 由 `flint_core.test.mjs` + `tests/test_frontend_flint.py` 接进 `uv run pytest`（本仓唯一常跑的门）。
// **连带收益**：决策P10.2-6 的「observer 懒创建」在这里从约定升级成结构保证——纯模块引都引不到。
//
// 安全姿态（沿观澜决策P4.20-4/-5）：**入口白名单 + flint 生成式编译 + 键域出口断言 + svg/richText +
// 钉版 + 失败保留源码**。三点与前三类渲染器不同：①块内容只经 JSON.parse、**JSON 里没有函数**，
// 用户造不出 ECharts 的回调；②只放行五个顶层键、`data` 只留 `values`（**拒 data.url**，渲染器永不
// 发起网络请求）；③出口断言**按键域不按值域**——合法数据里就可能有 URL 串，扫值域会把正常图整张否掉。
// **不承诺产物绝对可信**；任何失败一律退回 <pre> 源码 + 徽标（DOM 那半边在 flint_chart.js）。
//
// 资源姿态（沿决策P4.20-6）：块源 64 K **字符**、data.values 1000 行、画布尺寸有限且不过大。
// 超限不渲染、保留源码 +「数据过大」徽标，**不静默截断**。同一条红线也适用于 **flint 自己的截断**
// （`_warnings` 里的 `code:"overflow"`，决策P4.20-18）。

// ── 资源闸常量（沿决策P4.20-6）────────────────────────────────────────────────────────
export const FLINT_MAX_CHARS = 64 * 1024; // 围栏块源上限，**字符数非字节**（src.length 是 UTF-16 码元；
                                          // CJK 一字符 3 字节，叫 BYTES 会让人以为闸在 64 KB、实则放行约 192 KB）
export const FLINT_MAX_ROWS = 1000;       // data.values 行上限（代价按图元数走：散点 1000 行 ≈ 1050 个 SVG 节点）
// 画布**上界**（决策P4.20-20）：闸子只防「大到打死浏览器」。**故意没有下界**——一张小图不消耗资源，
// 而 flint 按语义定尺寸时小图很常见（两周的 Calendar Heatmap 只有百来像素宽）。曾经的 minW/minH
// 把这类合法小图判成「数据过大」，还叫作者去删数据——那只会让画布更小、永远修不好。
export const FLINT_SIZE = { maxW: 1600, maxH: 1200 };

// ── 按气泡宽重编译（决策P10.2-4/-5，茶话室核心新机制一）────────────────────────────────
// 逐档收窄的比例：flint 的 canvasSize 是**布局提示**不是硬钳位（请求 400 得 454、请求 200 得 253），
// 故取第一个真正落进气泡的档；全不中就用自然尺寸 + 横向滚动。
export const FLINT_FIT_RATIOS = [1, 0.75, 0.55, 0.4];
// 绘图区下限（px）：低于此就不是一张能读的图。观澜没有这一条——它的 wiki 栏 377 px 起步够用；
// 茶话室气泡 375 px 时，带 color 图例的线图会出现「_width=321 ≤ 375 成立、绘图区只剩 81 px」的档。
export const FLINT_MIN_PLOT_W = 160;

// ── 安全闸常量（沿决策P4.20-4）──────────────────────────────────────────────────────
export const FLINT_TOP_KEYS = ["data", "semantic_types", "chart_spec", "field_display_names", "options"];
// 注：`field_display_names` 留在白名单里只为**向前兼容**——flint 0.4.1 的 dist 里**根本没有这个键**
// （grep 计数为 0，assembleECharts 只读 chart_spec/semantic_types/data.values/options），写了等于没写。
// 作者侧 skill 已据此改口，不再教人用它（沿决策P4.20-21）。
//
// flint 的 `graphic` 元素类型白名单（评审修）。原先只放行 `type:"text"`，**误杀 flint 自己的产物**：
// ordinal / nominal 的 size 通道会让它手搭一个图例
// `{type:"group", children:[{type:"text"…},{type:"circle"…}]}`（dist/echarts/index.js:3842），
// 于是一张完全合法的散点图永远渲不出来、作者还无从修。
// 白名单而非黑名单：`image` 是唯一能把外部资源拉进渲染进程的 graphic 类型，其余都是矢量绘制图元
// （`style.image` 那条路另有 `k === "image"` 的键域断言兜着）。**新增类型须显式加进来**，
// 这样上游若哪天真开始产 `type:"image"`，仍是拒绝而不是放行。
export const FLINT_GRAPHIC_TYPES = new Set(["text", "group", "circle", "rect", "line"]);

// flint 自己**丢数据**的 `_warnings.code` 清单（评审修）。原先只认 `overflow`，漏掉了
// `too-many-groups-pyramid`（severity 同为 `"warning"`，实测三组 Pyramid 只画前两组）。
// **不能改成按 severity 拦**：`invalid-option-value` 也是 `severity:"warning"`，但它只是
// chartProperty 回落默认值、不丢任何数据，拦它会给出「数据被截断」这个错误的修法指引。
// 升级钉版时 `flint_core.test.mjs` 有一条用例断言 dist 里的 code 全集仍是已知那四个
// —— 上游新增 code 会让它变红，逼一次人工分诊（是不是又一个丢数据的 code）。
export const FLINT_TRUNCATION_CODES = new Set(["overflow", "too-many-groups-pyramid"]);

// 危险**键**清单：值一律被 ECharts 当文本画进 <text>，是**键**才决定它是不是链接/图片/HTML。
// **不含 `target`**（决策P4.20-16）：ECharts 的 `target` 只在与 `link` 配对时才有意义（`'blank'`/`'self'`），
// 而 `link` 已被无条件拒——单独的 `target` 是惰性的。`Sankey Diagram`/`Network Graph` 的
// `series.links[].target` 是**节点名**，禁它等于禁掉这两个图型。
// **不含 `renderItem`**：它另走「必须是函数」的规则（见 assertSafeOption）。
export const FLINT_BAD_KEYS = new Set(["link", "toolbox", "dataView"]);

// 色板槽位**承载语义**的图型（沿决策P4.20-22）：这些模板把颜色当含义用，逐槽换色会把含义改掉。
// 名单由对 dist 穷举「非色板数组里的裸 hex 语义常量」得出，`flint-chart@0.4.1` 全包**只有一处**：
// waterfall 模板的 `{ startEnd:"#5470c6", increase:"#91cc75", decrease:"#ee6666" }`。
// 按值替换会把「涨」的绿换成茶褐、「跌」的红换成苔绿——把「跌」画成绿的比配色不统一坏得多。
// **升级钉版时必须重跑那次 grep。**
export const FLINT_SEMANTIC_COLOR_CHARTS = new Set(["Waterfall Chart"]);

// 只在**颜色键**上按精确串替换：既保住 flint 算好的「谁配哪一档」的配色分派，又不可能碰到数据文本
// （类别名/标签走的是别的键）。非色板里的颜色（渐变、连续色映射、语义色）原样不动。
export const FLINT_COLOR_KEYS = new Set(["color", "borderColor", "shadowColor", "backgroundColor", "areaColor"]);

// ── 茶席主题（决策P10.2-8，结构沿决策P4.20-13/-23）────────────────────────────────────
// 经 `echarts.init(dom, THEME, opts)` **第二参**传入——ECharts 主题 schema 用的是**按轴类型分档**的
// categoryAxis/valueAxis/timeAxis/logAxis/singleAxis，与 flint 产物的 xAxis/yAxis 天然不撞键，
// 故不必自己深合并、也不会盖掉 flint 算好的轴字段与刻度。**不**做 `{...opt, ...THEME}` 顶层展开。
// **五档必须齐**（决策P4.20-23）：只给前两档时，flint 对 temporal x 产出的 `xAxis.type:"time"` 会走
// theme.timeAxis 这一空档、退回 ECharts 原厂灰黑，于是同一张图两条轴不是一套色（用一张类别轴柱图验不出来）。
// 取值对齐 chat.css 既有暖米调（注释标源）。
const CHAHUA_FLINT_AXIS = {
  axisLine: { lineStyle: { color: "#d0c8b4" } },  // 同 blockquote 左边框：轴线
  axisTick: { lineStyle: { color: "#d0c8b4" } },
  axisLabel: { color: "#666" },                   // 同 .bubble-header 弱文本：刻度文字
  splitLine: { lineStyle: { color: "#ece7db" } }, // 比米底再淡一档：网格
};
export const CHAHUA_FLINT_THEME = {
  backgroundColor: "transparent",
  // 图内文字：与 base.css 的 body 同一套字体栈（ECharts SVG 渲染器把它写成 `style="font-family:…"`
  // 的**独立属性**而非 `font:` 简写，故多字体栈安全；不用 "inherit"——那在 font 简写里是非法值）。
  textStyle: { color: "#333", fontFamily: '-apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif' },
  title: { textStyle: { color: "#444" } },
  legend: { textStyle: { color: "#555" } },
  categoryAxis: CHAHUA_FLINT_AXIS,
  valueAxis: CHAHUA_FLINT_AXIS,
  timeAxis: CHAHUA_FLINT_AXIS,   // flint 对 temporal x 产出 xAxis.type:"time"，走的是这一档
  logAxis: CHAHUA_FLINT_AXIS,
  singleAxis: CHAHUA_FLINT_AXIS, // Streamgraph
};

// 序列色板：茶席暖调，相邻两档刻意拉开明度以便区分。**20 档**（沿决策P4.20-24）：flint 的类别数 > 10
// 时会从 cat10 切到 **cat20**，只备 10 档会让第 11 个序列之后原样留着 ECharts 原厂色——一半茶席、
// 一半原厂，正是要避免的那种混搭。第 9/11/20 档刻意取自既有 hljs token 色，与代码块同色温。
export const CHAHUA_FLINT_PALETTE = [
  "#7a5c3e", // 茶褐
  "#4f8a7b", // 竹露青
  "#b5654a", // 陶红
  "#3f6079", // 靛蓝
  "#c9a227", // 秋香
  "#6b7f4e", // 苔绿
  "#9c6a8f", // 紫苏
  "#2f6f6a", // 深潭青
  "#d59c6b", // 杏橙
  "#5a5f7a", // 石青灰
  "#a8b88a", // 竹叶
  "#8c4f4f", // 赭石
  "#4a7fa5", // 湖蓝
  "#b98a5e", // 焦糖
  "#6f8f8a", // 雾青
  "#a3763f", // 琥珀
  "#567a4a", // 松绿
  "#96637a", // 檀紫
  "#7f8fa6", // 远山灰蓝
  "#cc8b76", // 陶土
];

// ── 失败分档（沿决策P4.20-20）────────────────────────────────────────────────────────
// 三种终态各自一句人话：tooLarge（资源闸）/ truncated（flint 丢了数据）/ renderFail（其余一切）。
// 用显式的 `flintKind` 而非 `instanceof RangeError`——后者曾把「画布非有限数」和「画布过小」
// 一并说成「数据过大」，把作者引向删数据这条修不好的路。
export function flintError(kind, message) {
  const e = new Error(message);
  e.flintKind = kind;
  return e;
}

// ── 入口白名单（沿决策P4.20-4/-5）：只放行五个顶层键；拒 data.url，只认内联 data.values ──
//
// **本函数不校验行与单元格的形状**（决策P10.2-11）：skill 的自校验清单要求「对象数组 + 标量单元格」，
// 那是**作者建议不是渲染端闸子**——单元格值走「JSON → 生成式编译 → ECharts 建 SVG DOM」，
// 全程无字符串→DOM 解析，一个嵌套对象与一个字符串在威胁模型上没有区别。
// **已知缺口明记**：嵌套对象 / 嵌套数组单元格不抛错、产出非空但错误的 series、**绕过空 series 守门**
// → 一张静默说谎的图。可选加固是在下面那圈列名校验里顺手抽查被 encodings 引用列的值类型
// （非 string|number|boolean|null|undefined 即 throw，复用既有 catch → 徽标）；遍历本就在做，
// 增量近乎为零。**开这一行时须同步改 §3.3.1 / 决策P10.2-11 与 flint_core.test.mjs 里那条现状断言。**
export function sanitizeFlintInput(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("bad-input");
  const inp = {};
  for (const k of FLINT_TOP_KEYS) if (k in raw) inp[k] = raw[k];
  const values = inp.data && inp.data.values;
  if (!Array.isArray(values) || !values.length) throw new Error("no-inline-values"); // data.url / 远端引用一律拒
  if (values.length > FLINT_MAX_ROWS) throw flintError("tooLarge", "too-many-rows");
  inp.data = { values }; // data 只留 values，其余键（url/format/…）丢弃
  // 列名存在性（沿决策P4.20-14 上半）：flint 对不存在的列**不抛错**，静默产出空 series → 画出一张空图。
  // 列集取**全表并集**而非 values[0]：结构化库出来的行常常列不齐（NULL 列被整行省略），
  // 只看首行会把合法图误判成 unknown-field。行数已 ≤ 1000，全表并集是廉价的。
  const cols = new Set();
  for (const row of values) if (row && typeof row === "object") for (const k of Object.keys(row)) cols.add(k);
  for (const enc of Object.values((inp.chart_spec || {}).encodings || {})) {
    const f = typeof enc === "string" ? enc : enc && enc.field;
    if (f && !cols.has(f)) throw new Error("unknown-field");
  }
  return inp;
}

// ── 画布尺寸闸（沿决策P4.20-6/-20）：只有上界 + 有限性 ────────────────────────────────
// flint 对 baseSize **不做任何钳制**（`{width:1e8}` 直通 `_width=100000123`、`{width:-5,height:NaN}`
// 直通 NaN），故这两条必须自己守：NaN 会让 echarts.init 拿到脏尺寸，1e8 会当场打死渲染进程。
// **没有下界**：小画布不消耗资源，拒它只会误伤按语义定尺寸的小图（见 FLINT_SIZE 注释）。
export function checkedSize(w, h) {
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) throw new Error("bad-size"); // → renderFail
  if (w > FLINT_SIZE.maxW || h > FLINT_SIZE.maxH) throw flintError("tooLarge", "size-too-large");
  return [w, h];
}

// ── flint 自己的截断也不许静默（沿决策P4.20-18）──────────────────────────────────────
// flint 的 filterOverflow 在类别/分面数超预算时会**整批丢掉**取值，并记进 `ecOption._warnings`
// （`code:"overflow"`）。它只对 x/y 轴画出可见的「...N items omitted」占位；**color / column / row
// 三个通道是无标记地丢**——12 个地区的分面图只画 6 个，读者看到的是一张「看起来完整」的图。
// 那正是决策P4.20-6 拒绝静默截断的同一件事，只不过截断发生在 flint 里而不是我们这儿。
// 故：产物剥 `_` 私有键**之前**先读它。其余 severity:"info" 的提示（如选项标签被归一）不丢数据，放行。
export function assertNoSilentTruncation(warnings) {
  if (!Array.isArray(warnings)) return;
  const dropped = warnings.find((w) => w && FLINT_TRUNCATION_CODES.has(w.code));
  if (dropped) throw flintError("truncated", "flint-truncated:" + dropped.code);
}

// ── 出口断言（沿决策P4.20-4）：**按键域**查已知危险键，不扫值域 ──────────────────────
// 为何不扫值域：合法数据里就可能有 URL 串（把模型卡 / 论文链接当类别名很常见），这类串会原样进入产物
// → 一条 /https?:/ 会把一张完全正常的图整张否掉。**危险的从来是键不是值。**
// 这是为**版本漂移**留的守门（钉版之外的运行时铰链），不是在补现存漏洞。
// **不要把它扩写成通用 ECharts option 消毒器**：那是 MathJax 式的脆弱黑名单，决策P10.1-5 已否过一次。
export function assertSafeOption(opt) {
  (function walk(node) {
    if (Array.isArray(node)) return node.forEach(walk);
    if (!node || typeof node !== "object") return;
    for (const [k, v] of Object.entries(node)) {
      if (FLINT_BAD_KEYS.has(k)) throw new Error("bad-key:" + k); // link/toolbox/dataView
      if (k === "image") throw new Error("image-ref"); // graphic/背景的图片引用
      // renderItem 必须是**函数**（决策P4.20-16）：`Waterfall Chart` 走 `series.type:'custom'`、
      // 由 flint **自己**造一个 renderItem 函数——那是已钉版的依赖代码，不是用户可控物
      // （JSON 里造不出函数，flint 也不 eval 字符串）。反过来，一个**字符串** renderItem 只可能来自
      // 版本漂移或注入，一律拒。同理下面的 formatter 只查字符串形态、不碰 flint 自造的函数。
      if (k === "renderItem" && typeof v !== "function") throw new Error("bad-renderItem");
      if (k === "symbol" && typeof v === "string" && v.startsWith("image://")) throw new Error("image-symbol");
      if (k === "formatter" && typeof v === "string" && /[<>]/.test(v)) throw new Error("html-formatter");
      walk(v);
    }
  })(opt);
  // graphic 元素类型白名单，**递归进 children**（评审修：flint 的 ordinal size 图例是
  // `{type:"group", children:[…]}`，只放行 `text` 会把一张合法散点图整张否掉）。
  (function walkGraphic(list) {
    for (const g of list || []) {
      if (!g || !FLINT_GRAPHIC_TYPES.has(g.type)) throw new Error("bad-graphic-type");
      if (g.children) walkGraphic(g.children);
    }
  })(opt.graphic);
  // 空图守门（沿决策P4.20-14 下半）：列名全对、也不抛错，某些图型仍产出空 series——
  // Treemap / Sunburst Chart / Tree 只给 detail+size 时 series[0].data === undefined。
  const series = [].concat(opt.series || []);
  if (!series.length || !series.some((s) => s && (Array.isArray(s.data) ? s.data.length : s.data != null)))
    throw new Error("empty-series");
}

// ── 逐槽换色（沿决策P4.20-24）────────────────────────────────────────────────────────
// 映射表**从 `opt.color` 自身现造**，不硬编码 flint 的色板常量——这样 cat10 / cat20 / 平行坐标那套
// 变体（第 10 槽与 cat10 不同）全都自动覆盖，也不会随上游升级漂移。
// flint 把 ECharts 默认 cat10 硬编码进产物（顶层 `color` 数组 + 每个 `series[].itemStyle.color`），
// 产物永远盖过主题 ⇒ 主题里写 `color` 完全无效，故序列色板只能走这条按位替换。
export function applyChahuaPalette(opt, chartType) {
  if (FLINT_SEMANTIC_COLOR_CHARTS.has(chartType)) return; // 语义色图型整张跳过（决策P4.20-22）
  const source = Array.isArray(opt.color) ? opt.color : null;
  if (!source || !source.length) return;
  const map = new Map();
  source.forEach((c, i) => {
    if (typeof c === "string") map.set(c.toLowerCase(), CHAHUA_FLINT_PALETTE[i % CHAHUA_FLINT_PALETTE.length]);
  });
  (function walk(node, inColorKey) {
    if (Array.isArray(node)) {
      for (let i = 0; i < node.length; i++) {
        const v = node[i];
        if (inColorKey && typeof v === "string" && map.has(v.toLowerCase())) node[i] = map.get(v.toLowerCase());
        else walk(v, inColorKey);
      }
      return;
    }
    if (!node || typeof node !== "object") return;
    for (const [k, v] of Object.entries(node)) {
      const isColorKey = FLINT_COLOR_KEYS.has(k);
      if (isColorKey && typeof v === "string" && map.has(v.toLowerCase())) node[k] = map.get(v.toLowerCase());
      else walk(v, isColorKey);
    }
  })(opt, false);
}

// ── tooltip / 标签的 formatter 压成纯文本（沿决策P4.20-25）──────────────────────────
// 为何必须做：flint 给几乎每张图装的是一个**函数** formatter，返回的是 HTML 串（`<br/>` 分行，
// 部分模板还带 `<b>`/`<span style=…>`）。而本相位把 tooltip 钉在 `renderMode:'richText'`——
// richText **不解析 HTML**，于是标签被原样画成文字：hover 一张最普通的柱图会看到
// 「模型: model-a<br/>评测得分: 72.4」这一行字面量。richText 这条安全铰链不能撤（决策P4.20-4），
// 所以改的是**喂给它的东西**。这一步纯属呈现修正，**不放宽任何安全性质**——产物仍然只作为文本绘制。
// **两处与观澜有意不同**（本相位评审实测后收紧，观澜那份同样中招、已知会）：
// ① 标签正则从 `/<[^>]*>/g` 收成 `/<\/?[a-zA-Z][^<>]*>/g`。前者的 `[^>]` **不排除换行**，而这条链上
//    的字符串里已经有我们自己插进去的 `\n`——于是一个数据值里的 `<` 与后一个数据值里的 `>` 之间
//    的一切被整段吞掉。实测类别名 `"<10ms"` + 分组值 `">=P95"` 的延迟图，整条 tooltip 只剩 `"=P95"`
//    （类别、指标名、数值全没了，无报错无徽标）。收紧后要求 `<` 后必须是字母或 `/`，
//    `<b>`/`</span>`/`<br/>`/`<span style=…>` 照剥，`<10ms` 不再被当标签。
// ② **不再解 HTML 实体**。flint 的 formatter 是把原始单元格值**裸插**进 HTML 串的
//    （dist 里 `&lt;`/`&amp;` 计数为 0、无任何 escape 函数），故产物里出现的 `&amp;` / `&lt;`
//    只可能是**数据本身就长这样**——解它等于改写用户数据。
export function htmlToPlainText(s) {
  return String(s)
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/(?:p|div|li|tr)>/gi, "\n")
    .replace(/<\/?[a-zA-Z][^<>]*>/g, "");
}

export function plainTextFormatters(node) {
  if (Array.isArray(node)) return node.forEach(plainTextFormatters);
  if (!node || typeof node !== "object") return;
  for (const [k, v] of Object.entries(node)) {
    if (k === "formatter" && typeof v === "function") {
      node[k] = function (...args) {
        const out = v.apply(this, args);
        return typeof out === "string" ? htmlToPlainText(out) : out; // 非字符串（富文本对象等）原样放行
      };
      continue;
    }
    plainTextFormatters(v);
  }
}

// ── ★ 按气泡宽重编译（决策P10.2-4/-5/-5b，茶话室核心新机制一）──────────────────────
//
// **不能**编译完再改画布尺寸：flint 把 `legend.left`/`graphic.left`/`grid` 按它自己算出的 `_width`
// 烤成**绝对像素**（一张带 color 通道的折线图 _width=618、legend.left=464），事后把画布改成 375
// 会让整个图例画在视口之外、而 grid.right 仍占着位——图看着还在，读者却没法分辨哪条线是哪个模型。
// 正解是把尺寸这件事**交还给 flint**：拿 `chart_spec.canvasSize` 逐档收窄重编译。

// 绘图区宽度。**`grid` 有三种形态，只认对象会静默失守**（决策P10.2-5b ①）：
//   ① 对象   —— 普通笛卡尔图（`{left,right,…}`）
//   ② 数组   —— 用了 column/row 分面时**每个分面一个 grid**（`[{left,top,width,height},…]`）
//   ③ 不存在 —— 非笛卡尔图型（Pie/Treemap/Sunburst/Gauge…），整张画布就是绘图区
// 数组形态里 `g.left`/`g.right` 是 undefined → `||0` 会让公式退化成 `plotWidth === _width`，
// 于是**下限对所有分面图恒过**：双列分面柱图 avail=375 时 `_width=365`（≤375 ✓）、公式算出 365
// （≥160 ✓）当场采纳，而**真实的两个分面各只有 146 px**。
// 分面图取**最窄的那个分面**——读者是逐个分面读的，最窄的那个决定这张图能不能读。
export function plotWidth(o) {
  const g = o.grid;
  if (Array.isArray(g)) {
    const ws = g.map((x) => (x && Number.isFinite(x.width) ? x.width : o._width));
    return ws.length ? Math.min(...ws) : o._width;
  }
  if (g && typeof g === "object") {
    // `containLabel:true` 时 left/right 是**含轴标签在内**的外边界，ECharts 会把标签宽度再从
    // 这条带里挖走 —— 于是 `_width - left - right` 是个**高估值**，160px 下限对这类图恒过
    // （评审实测：长任务名的 Gantt 收到 316px 时公式算 212 ✓ 采纳，而真正的条形区只剩约 55px）。
    // 标签宽度要等 ECharts 真排版才知道，我们在编译期无从得知，故**认输**：返回 0 让下限必不通过，
    // 收窄一律放弃、走自然尺寸 + 横向滚动。这正是决策P10.2-5 写死的偏好——「宁可横向滚动，
    // 也不要一张绘图区被压成条的图」。代价：标签很短、本可收窄的 Gantt / Bullet 也会横向滚动。
    // flint 0.4.1 里只有 `Gantt Chart` / `Bullet Chart` 两个模板设这个标志。
    if (g.containLabel === true) return 0;
    return o._width - (g.left || 0) - (g.right || 0);
  }
  return o._width; // 非笛卡尔：整张画布即绘图区
}

export const hasOverflow = (o) =>
  Array.isArray(o._warnings) && o._warnings.some((w) => w && w.code === "overflow");

// 取第一个**同时**满足「落进气泡」与「绘图区 ≥ FLINT_MIN_PLOT_W」的档；都不满足就**放弃收窄**、
// 用自然尺寸，由 `.flint-rendered{overflow-x:auto}` 横向滚动兜底（决策P10.2-5）。
// 硬地板是客观存在的（`Pie Chart` 恒 ≥ 280 px、带图例线图恒有 +171 px 开销），故「放弃收窄 + 横向滚动」
// 不是异常分支而是**常规分支**。绘图区随 `_width` 单调收缩，故第一个满足两条的就是最优档、不必回溯。
export function assembleFitted(assemble, input, avail) {
  const base = assemble(input);
  if (!avail || !base || typeof base !== "object" || !Number.isFinite(base._width) || base._width <= avail)
    return base; // 天然就落在气泡里
  const baseOverflow = hasOverflow(base); // base 本来就截断的，不赖收窄
  const ratio = base._height / base._width;
  const tryAt = (width, height) => {
    try {
      const cand = assemble({
        ...input,
        chart_spec: { ...(input.chart_spec || {}), canvasSize: { width, height: Math.max(1, Math.round(height)) } },
      });
      return cand && Number.isFinite(cand._width) ? cand : null;
    } catch {
      return null; // 收窄这条路走不通就用自然尺寸，别把一张本来能画的图弄丢
    }
  };
  for (const r of FLINT_FIT_RATIOS) {
    const width = Math.round(avail * r);
    if (width < 1) break;
    let cand = tryAt(width, width * ratio); // 先按 base 的宽高比试
    if (!cand) break;
    // 收窄**只准让图变小，不准让图变少**（决策P10.2-5b ②）：base 没截断而候选截断了，说明是这一档
    // 把数据挤掉的——采纳它会让一张本来完好的图挂上「数据被截断」徽标，把作者引向删类别这条错路。
    if (!baseOverflow && hasOverflow(cand)) {
      // **但先分清是宽度挤的还是高度挤的**（评审修）：锁死宽高比会让高度跟着一起缩，而对
      // 「类别数决定纵向长度」的图（60 个类别的横向柱图）**光是高度变矮就足以让 flint 丢行**。
      // 实测 base 524×517 无截断，第一档 `{375, 370}` 得 430×427 且 OVF —— 于是 r=1 就 break、
      // 整张图退回自然尺寸横向滚动；而 `{281, base._height}` 得 336×577，落进气泡、绘图区 212、
      // 零数据丢失，是个满足全部验收条件却被静默跳过的档。故同一档再试一次「保住原高度」。
      cand = tryAt(width, base._height);
      if (!cand || hasOverflow(cand)) break; // 真是宽度挤的 → 再窄只会更糟，收工用 base
    }
    // 绘图区随 _width 单调收缩，故第一个同时满足两条的就是最优档。
    if (cand._width <= avail && plotWidth(cand) >= FLINT_MIN_PLOT_W) return cand;
  }
  return base; // 宁可横向滚动，也不要一张绘图区被压成条 / 被挤掉数据的图
}

// 整条**纯逻辑**编译链：`<pre>` 里的 JSON 文本 → 可直接喂 `echarts.setOption` 的 option。
// **`flint_chart.js` 与 `flint_core.test.mjs` 共用这一个**（评审修）——两边各自手抄一遍的话，
// 删掉产品里的形态守门、或把 `assertSafeOption` 挪到剥 `_` 之前，35 条用例仍会全绿：
// 测的是一份已经和产品对不上的副本。
// 抛错一律带（或不带）`flintKind`，由调用方分档成三种徽标文案。
export function compileFlintOption(assemble, raw, avail) {
  const input = sanitizeFlintInput(raw);
  const opt = assembleFitted(assemble, input, avail);
  // 产物形态守门（沿决策P4.20-16）：某些「图型 × 编码」组合下 flint **不装配成 ECharts option**，
  // 而是静默回吐它自己的 Vega-Lite 形中间态（`{mark, encoding}`，无 `series`/`_width`）——
  // 实测 `Sunburst Chart` 缺 `color` 通道时即如此。这一条把它落到**诚实的**「渲染失败」文案上，
  // 而不是让尺寸闸把它误报成「数据过大」（那会把作者引向删数据这条错路）。
  if (!opt || typeof opt !== "object" || !("series" in opt)) throw new Error("not-echarts-option");
  const [width, height] = checkedSize(opt._width, opt._height);
  assertNoSilentTruncation(opt._warnings); // 决策P4.20-18：读完 `_warnings` 再剥 `_` 私有键
  for (const k of Object.keys(opt)) if (k.startsWith("_")) delete opt[k];
  assertSafeOption(opt);
  applyChahuaPalette(opt, (input.chart_spec || {}).chartType); // 茶席逐槽换色（决策P10.2-8）
  plainTextFormatters(opt); // flint 的 HTML tooltip 压成纯文本（决策P4.20-25）
  return { opt, width, height };
}
