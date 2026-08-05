// P10.2 §7 node 层：flint_core.mjs 的纯函数用例 + 双运行时自包含性 + 接线断言。
//
// 跑法：`node --test app/renderer/flint_core.test.mjs`（node:test 自 18.13 起可用，与 app/package.json
// 的 engines 下限 18.17 相容；零 npm 新依赖）。**并由 `tests/test_frontend_flint.py` subprocess 接进
// `uv run pytest`** —— 本仓无 CI，`uv run pytest` 是唯一常跑的门，不接进去的用例等于不存在
// （P10.1 那 12 项 marked 用例就是跑过一次然后消失的先例）。见决策P10.2-12。
//
// **为何是 `.mjs`**：`app/package.json` 没有 `"type":"module"` 且**不能加**（main 是 Electron 主进程的
// CommonJS），于是 `renderer/*.js` 在 Node 眼里是 CJS，`.js` 直接 import 在 Node 18 上是 SyntaxError，
// 而本机 Node 22.7+ 的语法探测会猜成 ESM 假绿——本机绿、下限红，最坏的那种假绿。

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { assembleECharts, ecAllTemplateDefs } from "../node_modules/flint-chart/dist/echarts/index.js";
import {
  FLINT_MAX_ROWS,
  FLINT_MIN_PLOT_W,
  FLINT_TRUNCATION_CODES,
  CHAHUA_FLINT_THEME,
  sanitizeFlintInput,
  checkedSize,
  assertNoSilentTruncation,
  assertSafeOption,
  applyChahuaPalette,
  plainTextFormatters,
  htmlToPlainText,
  plotWidth,
  hasOverflow,
  assembleFitted,
  compileFlintOption,
} from "./flint_core.mjs";

const HERE = fileURLToPath(new URL(".", import.meta.url)); // Node 18 兼容（import.meta.dirname 是 21+）
const readHere = (rel) => readFileSync(HERE + rel, "utf8");

// ── 素材 ──────────────────────────────────────────────────────────────────────────
const barSpec = () => ({
  data: {
    values: [
      { 模型: "model-a", 评测得分: 72.4 },
      { 模型: "model-b", 评测得分: 81.9 },
      { 模型: "model-c", 评测得分: 65.1 },
      { 模型: "model-d", 评测得分: 90.2 },
    ],
  },
  chart_spec: { chartType: "Bar Chart", encodings: { x: { field: "模型" }, y: { field: "评测得分" } } },
});

const lineWithLegend = () => {
  const values = [];
  for (const g of ["甲", "乙", "丙"])
    for (let d = 1; d <= 6; d++) values.push({ 日: `2026-01-0${d}`, 值: d * (g === "甲" ? 3 : g === "乙" ? 5 : 7), 组: g });
  return {
    data: { values },
    chart_spec: {
      chartType: "Line Chart",
      encodings: { x: { field: "日" }, y: { field: "值" }, color: { field: "组" } },
    },
  };
};

const pieSpec = () => ({
  data: { values: [{ 类: "a", 量: 3 }, { 类: "b", 量: 5 }, { 类: "c", 量: 2 }] },
  chart_spec: { chartType: "Pie Chart", encodings: { color: { field: "类" }, size: { field: "量" } } },
});

const facetBar = () => {
  const values = [];
  for (const g of ["甲", "乙"]) for (const c of ["a", "b", "c"]) values.push({ 类: c, 值: (c.charCodeAt(0) % 7) + 1, 组: g });
  return {
    data: { values },
    chart_spec: {
      chartType: "Bar Chart",
      encodings: { x: { field: "类" }, y: { field: "值" }, column: { field: "组" } },
    },
  };
};

// ── ★ 按气泡宽重编译（决策P10.2-4/-5/-5b）────────────────────────────────────────────

test("assembleFitted：560px 气泡 × 自然 ~523 的柱图 → 原样返回 base，不做无谓重编译", () => {
  const input = sanitizeFlintInput(barSpec());
  const base = assembleECharts(input);
  assert.ok(base._width <= 560, `前提破了：自然宽 ${base._width} 应 ≤ 560`);
  const got = assembleFitted(assembleECharts, input, 560);
  assert.equal(got._width, base._width);
});

test("assembleFitted：375px 气泡 × 柱图 → 落进气泡且绘图区 ≥ 160", () => {
  const input = sanitizeFlintInput(barSpec());
  const got = assembleFitted(assembleECharts, input, 375);
  assert.ok(got._width <= 375, `_width=${got._width} 应落进 375`);
  assert.ok(plotWidth(got) >= FLINT_MIN_PLOT_W, `绘图区 ${plotWidth(got)} 应 ≥ ${FLINT_MIN_PLOT_W}`);
});

test("assembleFitted：375px 气泡 × 带 color 图例的线图 → 要么绘图区 ≥ 160，要么退回自然尺寸（绝不采纳 81px 那档）", () => {
  // 观澜那条「取第一个 _width ≤ 栏宽的档」在 377px 起步的 wiki 栏里够用；茶话室气泡里会采纳
  // 「_width=321 ≤ 375 成立、绘图区只剩 81px」这种档——6 个刻度 3 条线挤在 81px 里。
  const input = sanitizeFlintInput(lineWithLegend());
  const base = assembleECharts(input);
  const got = assembleFitted(assembleECharts, input, 375);
  const fellBack = got._width === base._width;
  assert.ok(
    fellBack || (got._width <= 375 && plotWidth(got) >= FLINT_MIN_PLOT_W),
    `既没退回自然尺寸(${base._width})，采纳的档也不合格：_width=${got._width} plot=${plotWidth(got)}`,
  );
});

test("assembleFitted：260px 气泡 × 饼图（硬地板 280）→ 返回自然尺寸，走横向滚动", () => {
  // 硬地板是客观存在的：Pie Chart 不管请求多小都 ≥ 280px。一个 260px 的气泡**永远**装不下一张饼图
  // ——「收不进去」在聊天里是常态不是边角，故「放弃收窄 + 横向滚动」是一等路径。
  const input = sanitizeFlintInput(pieSpec());
  const base = assembleECharts(input);
  const got = assembleFitted(assembleECharts, input, 260);
  assert.equal(got._width, base._width);
  assert.ok(got._width > 260, `前提破了：饼图自然宽 ${got._width} 竟落进了 260`);
});

test("★ plotWidth 认全 grid 三形态：数组取最窄分面 / 对象减左右边距 / 缺失取整张画布", () => {
  // 这条错了闸子恒过，是「看起来在守」的典型（决策P10.2-5b ①）。
  assert.equal(plotWidth({ _width: 500, grid: [{ width: 205 }, { width: 146 }] }), 146);
  assert.equal(plotWidth({ _width: 500, grid: { left: 86, right: 38 } }), 500 - 86 - 38);
  assert.equal(plotWidth({ _width: 400 }), 400);
  // 数组形态但 width 缺失 → 退回 _width（不至于 NaN），空数组同理
  assert.equal(plotWidth({ _width: 400, grid: [{ left: 10 }] }), 400);
  assert.equal(plotWidth({ _width: 400, grid: [] }), 400);
});

test("★ plotWidth：containLabel:true 的 grid 认输返 0（评审修：否则下限恒过）", () => {
  // containLabel 时 left/right 是**含轴标签**的外边界，ECharts 还要从这条带里挖走标签宽度
  // ——实测长任务名的 Gantt 收到 316px 时公式算 212 ✓，而真正的条形区只剩约 55px。
  // 标签宽度编译期不可知，故认输 → 下限必不过 → 放弃收窄走横向滚动（决策P10.2-5 的偏好）。
  assert.equal(plotWidth({ _width: 316, grid: { containLabel: true, left: 66, right: 38 } }), 0);
  assert.equal(plotWidth({ _width: 316, grid: { containLabel: false, left: 66, right: 38 } }), 316 - 66 - 38);
});

test("★ Gantt（containLabel）在窄气泡下退回自然尺寸而非挤扁（评审修）", () => {
  const gantt = { data: { values: [] }, chart_spec: { chartType: "Gantt Chart", encodings: { y: { field: "任务" }, x: { field: "开始" }, x2: { field: "结束" } } } };
  for (let i = 0; i < 8; i++)
    gantt.data.values.push({ 任务: `任务名称很长的第${i}项`, 开始: `2026-01-0${(i % 8) + 1}`, 结束: `2026-01-0${(i % 8) + 2}` });
  const input = sanitizeFlintInput(gantt);
  const base = assembleECharts(input);
  assert.equal(base.grid.containLabel, true, "前提破了：上游不再给 Gantt 设 containLabel");
  assert.equal(assembleFitted(assembleECharts, input, 375)._width, base._width);
});

test("★ 收窄先试锁宽高比、被高度挤到丢数据时改「保住原高度」再试一次（评审修）", () => {
  // 类别数决定纵向长度的图（60 类别横向柱图）：锁死宽高比会让高度一起缩，**光是高度变矮**
  // 就足以让 flint 丢行 —— 于是第一档就 `hasOverflow` 而 break，整张退回自然尺寸横向滚动。
  // 实测 base 524×517 无截断；`{375,370}` → 430×427 OVF；而 `{281, base._height}` → 336×577，
  // 落进气泡、绘图区 212、零数据丢失，是个满足全部验收条件却曾被静默跳过的档。
  const many = { data: { values: [] }, chart_spec: { chartType: "Bar Chart", encodings: { y: { field: "类" }, x: { field: "值" } } } };
  for (let i = 0; i < 60; i++) many.data.values.push({ 类: `类别-${i}`, 值: ((i * 7) % 23) + 1 });
  const input = sanitizeFlintInput(many);
  const base = assembleECharts(input);
  assert.equal(hasOverflow(base), false, "前提破了：自然尺寸本就截断");
  assert.ok(base._width > 375);
  const got = assembleFitted(assembleECharts, input, 375);
  assert.ok(got._width < base._width, `应当收窄成功，实得 _width=${got._width}（= base 说明又退回去了）`);
  assert.ok(got._width <= 375 && plotWidth(got) >= FLINT_MIN_PLOT_W);
  assert.equal(hasOverflow(got), false, "收窄仍不得丢数据");
});

test("★ 分面图 × 375px：不得采纳「整体合规、单个分面挤成百来 px」的档", () => {
  // 初稿只认对象形态的 grid，数组形态下 g.left/g.right 是 undefined、`||0` 让公式退化成
  // plotWidth === _width ⇒ 160px 下限对**所有分面图恒过**。
  const input = sanitizeFlintInput(facetBar());
  const natural = assembleECharts(input);
  assert.ok(Array.isArray(natural.grid), "前提破了：分面图的 grid 应是数组形态");
  const got = assembleFitted(assembleECharts, input, 375);
  assert.ok(
    got._width === natural._width || plotWidth(got) >= FLINT_MIN_PLOT_W,
    `采纳了一个分面只有 ${plotWidth(got)}px 的档（_width=${got._width}）`,
  );
});

test("★ 收窄不得制造 base 没有的 overflow（决策P10.2-5b ②）", () => {
  // 收窄**会**制造 flint 的截断：同一份数据自然尺寸 _warnings=null，收到 ratio 0.4 时 flint 直接
  // 丢掉一个分面并记 {code:"overflow", channel:"column"}。若采纳该档，一张自然尺寸完全正常的图
  // 会被 assertNoSilentTruncation 判成「数据被截断」，徽标还叫作者去减类别——问题是收窄循环造的。
  const input = sanitizeFlintInput(facetBar());
  const base = assembleECharts(input);
  assert.equal(hasOverflow(base), false, "前提破了：自然尺寸本就截断，这条用例失去意义");
  const got = assembleFitted(assembleECharts, input, 375);
  assert.equal(hasOverflow(got), false, "收窄把数据挤掉了，却让作者去改他没错的东西");
});

test("assembleFitted：avail 为 0 / 假值（量不到宽）→ 直接返回自然尺寸，不进收窄循环", () => {
  const input = sanitizeFlintInput(barSpec());
  const base = assembleECharts(input);
  assert.equal(assembleFitted(assembleECharts, input, 0)._width, base._width);
});

// ── 入口白名单 / 资源闸（沿决策P4.20-4/-5/-6）────────────────────────────────────────

test("sanitizeFlintInput：拒 data.url（渲染器永不发起网络请求）", () => {
  assert.throws(() => sanitizeFlintInput({ data: { url: "http://example.com/x.csv" }, chart_spec: {} }), /no-inline-values/);
});

test("sanitizeFlintInput：只放行五个顶层键，其余整键丢弃", () => {
  const out = sanitizeFlintInput({ ...barSpec(), 自定义: { evil: 1 }, options: { a: 1 } });
  assert.deepEqual(Object.keys(out).sort(), ["chart_spec", "data", "options"].sort());
});

test("sanitizeFlintInput：data 只留 values，url/format 等兄弟键一并丢弃", () => {
  const raw = barSpec();
  raw.data.url = "http://example.com/x.csv";
  raw.data.format = "csv";
  const out = sanitizeFlintInput(raw);
  assert.deepEqual(Object.keys(out.data), ["values"]);
});

test("sanitizeFlintInput：1001 行 → tooLarge（≤1000 是真闸子，与 skill 清单逐条一致）", () => {
  const raw = barSpec();
  raw.data.values = Array.from({ length: FLINT_MAX_ROWS + 1 }, (_, i) => ({ 模型: `m${i}`, 评测得分: i }));
  assert.throws(() => sanitizeFlintInput(raw), (e) => e.flintKind === "tooLarge");
});

test("sanitizeFlintInput：列集取**全表并集** —— 首行缺列、后续行有 → 照常通过", () => {
  // 结构化库出来的行常常列不齐（NULL 列被整行省略），只看首行会把合法图误判成 unknown-field。
  const raw = {
    data: { values: [{ 模型: "a" }, { 模型: "b", 评测得分: 81.9 }] },
    chart_spec: { chartType: "Bar Chart", encodings: { x: { field: "模型" }, y: { field: "评测得分" } } },
  };
  assert.doesNotThrow(() => sanitizeFlintInput(raw));
});

test("sanitizeFlintInput：encodings 引用不存在的列 → unknown-field（flint 对此不抛错，会画空图）", () => {
  const raw = barSpec();
  raw.chart_spec.encodings.y = { field: "不存在的列" };
  assert.throws(() => sanitizeFlintInput(raw), /unknown-field/);
});

test("checkedSize：只有上界与有限性，**故意没有下界**", () => {
  assert.deepEqual(checkedSize(400, 320), [400, 320]);
  assert.deepEqual(checkedSize(114, 90), [114, 90]); // 按语义定尺寸的小图（两周的 Calendar Heatmap）
  assert.throws(() => checkedSize(NaN, 100), /bad-size/);
  assert.throws(() => checkedSize(-5, 100), /bad-size/);
  assert.throws(() => checkedSize(1e8, 100), (e) => e.flintKind === "tooLarge");
  assert.throws(() => checkedSize(100, 1201), (e) => e.flintKind === "tooLarge");
});

test("assertNoSilentTruncation：丢数据的 code → truncated；不丢数据的放行", () => {
  assert.doesNotThrow(() => assertNoSilentTruncation(null));
  assert.doesNotThrow(() => assertNoSilentTruncation([{ code: "coerced-option-label", severity: "info" }]));
  // **不丢数据的 warning 必须放行**：`invalid-option-value` 也是 severity:"warning"，但它只是
  // chartProperty 回落默认值 —— 拦它会给出「数据被截断」这个把作者引向删类别的错误指引。
  // 这条同时钉死「不许改成按 severity 拦」。
  assert.doesNotThrow(() => assertNoSilentTruncation([{ code: "invalid-option-value", severity: "warning" }]));
  for (const code of ["overflow", "too-many-groups-pyramid"])
    assert.throws(
      () => assertNoSilentTruncation([{ code, severity: "warning" }]),
      (e) => e.flintKind === "truncated",
      `${code} 应被判为截断`,
    );
});

test("★ 三组 Pyramid 只画前两组 → 必须判截断（评审修：原先只认 overflow，这条 code 直接过闸）", () => {
  const pyr = { data: { values: [] }, chart_spec: { chartType: "Pyramid Chart", encodings: { x: { field: "阶" }, y: { field: "量" }, color: { field: "组" } } } };
  for (const g of ["甲", "乙", "丙"]) for (const s of ["A", "B", "C"]) pyr.data.values.push({ 阶: s, 量: (s.charCodeAt(0) % 5) + 1, 组: g });
  const warned = assembleECharts(sanitizeFlintInput(pyr))._warnings;
  assert.ok(
    Array.isArray(warned) && warned.some((w) => w.code === "too-many-groups-pyramid"),
    "前提破了：上游不再产这个 warning，请重新分诊 FLINT_TRUNCATION_CODES",
  );
  assert.throws(() => runChain(pyr), (e) => e.flintKind === "truncated");
});

test("★ 上游 `_warnings.code` 全集未变（升级钉版时逼一次人工分诊）", () => {
  // 「哪些 code 会丢数据」只能人工判。这条把 dist 里的 code 字面量全集钉住：上游新增任何 code
  // 都会让它变红，提醒去看那条新 code 是不是又一个丢数据的（是就加进 FLINT_TRUNCATION_CODES）。
  const dist = readHere("../node_modules/flint-chart/dist/echarts/index.js");
  const codes = [...new Set([...dist.matchAll(/code:\s*"([^"]+)"/g)].map((m) => m[1]))].sort();
  assert.deepEqual(codes, ["coerced-option-label", "invalid-option-value", "overflow", "too-many-groups-pyramid"]);
  for (const c of FLINT_TRUNCATION_CODES) assert.ok(codes.includes(c), `${c} 在上游已不存在`);
});

// ── 出口断言（按键域不按值域，沿决策P4.20-4/-14/-16）────────────────────────────────

const okSeries = { series: [{ data: [1, 2] }] };

test("assertSafeOption：link / toolbox / dataView / image / image:// 符号 / 带尖括号的字符串 formatter → 拒", () => {
  assert.throws(() => assertSafeOption({ ...okSeries, title: { link: "http://x" } }), /bad-key:link/);
  assert.throws(() => assertSafeOption({ ...okSeries, toolbox: {} }), /bad-key:toolbox/);
  assert.throws(() => assertSafeOption({ ...okSeries, feature: { dataView: {} } }), /bad-key:dataView/);
  assert.throws(() => assertSafeOption({ ...okSeries, graphic: [{ type: "image", image: "x.png" }] }), /image-ref/);
  assert.throws(() => assertSafeOption({ series: [{ data: [1], symbol: "image://x.png" }] }), /image-symbol/);
  assert.throws(() => assertSafeOption({ ...okSeries, tooltip: { formatter: "<img src=x onerror=1>" } }), /html-formatter/);
  assert.throws(() => assertSafeOption({ series: [{ data: [1], renderItem: "evil()" }] }), /bad-renderItem/);
  // graphic 类型白名单另有专门用例（`rect` 现已放行——flint 自己会产），这里只钉 image 那条。
  assert.throws(() => assertSafeOption({ ...okSeries, graphic: [{ type: "image" }] }), /bad-graphic-type/);
});

test("assertSafeOption 反向用例：`target` 不拒、函数 renderItem 不拒（决策P4.20-16）", () => {
  // target 单独出现是惰性的（只在与已被拒的 link 配对时才有意义），而 Sankey/Graph 的
  // series.links[].target 是**节点名**——禁它等于禁掉这两个图型。
  assert.doesNotThrow(() => assertSafeOption({ series: [{ data: [1], links: [{ source: "a", target: "b" }] }] }));
  // Waterfall 走 series.type:'custom'，renderItem 是 flint **自己造的函数**（JSON 里造不出函数）。
  assert.doesNotThrow(() => assertSafeOption({ series: [{ data: [1], renderItem: () => ({}) }] }));
});

test("assertSafeOption：**不扫值域** —— 类别名是一条 URL 的图照常放行（决策P4.20-4 反向用例）", () => {
  // 合法数据里就有 URL 串（把模型卡 / 论文链接当类别名很常见）；扫值域会把正常图整张否掉。
  assert.doesNotThrow(() =>
    assertSafeOption({ xAxis: { data: ["https://example.com/model-card"] }, series: [{ data: [1] }] }),
  );
});

test("★ assertSafeOption：放行 flint 自造的 group 型图例，仍拒 image 型 graphic（评审修）", () => {
  // 原先要求 graphic 元素一律 `type:"text"`，把 flint 对 ordinal size 通道自造的
  // `{type:"group", children:[{type:"text"},{type:"circle"}]}` 图例整张否掉。
  const legend = { type: "group", children: [{ type: "text" }, { type: "circle" }] };
  assert.doesNotThrow(() => assertSafeOption({ ...okSeries, graphic: [legend] }));
  // 白名单仍然是白名单：未知类型、以及唯一能拉外部资源进来的 image 型，一律拒。
  assert.throws(() => assertSafeOption({ ...okSeries, graphic: [{ type: "image" }] }), /bad-graphic-type/);
  assert.throws(() => assertSafeOption({ ...okSeries, graphic: [{ type: "未知" }] }), /bad-graphic-type/);
  // **递归进 children**：藏在 group 里的 image 同样拒。
  assert.throws(
    () => assertSafeOption({ ...okSeries, graphic: [{ type: "group", children: [{ type: "image" }] }] }),
    /bad-graphic-type/,
  );
});

test("★ ordinal size 通道的散点图能整条链跑通（评审修，原先必被 graphic 闸误杀）", () => {
  const opt = runChain({
    data: { values: [
      { 横: 10, 纵: 5, 规模: "小" }, { 横: 20, 纵: 9, 规模: "中" },
      { 横: 30, 纵: 3, 规模: "大" }, { 横: 40, 纵: 7, 规模: "中" },
    ] },
    chart_spec: {
      chartType: "Scatter Plot",
      encodings: { x: { field: "横" }, y: { field: "纵" }, size: { field: "规模", type: "ordinal" } },
    },
  });
  assert.ok(Array.isArray(opt.graphic) && opt.graphic.some((g) => g.type === "group"), "前提破了：flint 没产 group 图例");
});

test("assertSafeOption：空 series 守门（列名全对也可能产出空图）", () => {
  assert.throws(() => assertSafeOption({ series: [] }), /empty-series/);
  assert.throws(() => assertSafeOption({ series: [{ data: undefined }] }), /empty-series/);
  assert.throws(() => assertSafeOption({ series: [{ data: [] }] }), /empty-series/);
});

// ── 茶席主题与色板（决策P10.2-8）──────────────────────────────────────────────────

test("CHAHUA_FLINT_THEME：五档轴必须齐（漏档的轴退回 ECharts 原厂灰黑，同图两轴两套色）", () => {
  for (const k of ["categoryAxis", "valueAxis", "timeAxis", "logAxis", "singleAxis"])
    assert.ok(CHAHUA_FLINT_THEME[k] && CHAHUA_FLINT_THEME[k].axisLine, `主题缺 ${k} 档`);
  assert.equal(CHAHUA_FLINT_THEME.backgroundColor, "transparent");
});

test("applyChahuaPalette：Waterfall Chart 语义色整张跳过（不把「跌」画成绿的）", () => {
  const opt = { color: ["#5470c6", "#91cc75", "#ee6666"], series: [{ itemStyle: { color: "#91cc75" } }] };
  applyChahuaPalette(opt, "Waterfall Chart");
  assert.deepEqual(opt.color, ["#5470c6", "#91cc75", "#ee6666"]);
  assert.equal(opt.series[0].itemStyle.color, "#91cc75");
});

test("applyChahuaPalette：只碰**颜色键** —— 恰好叫 `#5470c6` 的类别名不被改写", () => {
  const opt = {
    color: ["#5470c6", "#91cc75"],
    xAxis: { data: ["#5470c6", "正常类别"] },
    series: [{ itemStyle: { color: "#5470c6" }, data: ["#5470c6"] }],
  };
  applyChahuaPalette(opt, "Bar Chart");
  assert.equal(opt.xAxis.data[0], "#5470c6", "数据文本被当成色板槽位改写了");
  assert.equal(opt.series[0].data[0], "#5470c6", "数据文本被当成色板槽位改写了");
  assert.notEqual(opt.series[0].itemStyle.color, "#5470c6", "颜色键没换成茶席色");
  assert.notEqual(opt.color[0], "#5470c6");
});

test("applyChahuaPalette：映射表从 opt.color 自身现造 —— cat20 也全覆盖，不留一半原厂", () => {
  const source = Array.from({ length: 20 }, (_, i) => `#${(i + 16).toString(16).repeat(3)}`);
  const opt = { color: [...source], series: source.map((c) => ({ itemStyle: { color: c }, data: [1] })) };
  applyChahuaPalette(opt, "Bar Chart");
  for (let i = 0; i < 20; i++)
    assert.notEqual(opt.series[i].itemStyle.color, source[i], `第 ${i + 1} 槽仍是原厂色`);
});

test("plainTextFormatters / htmlToPlainText：把 flint 的 HTML tooltip 压成纯文本（richText 不解析 HTML）", () => {
  assert.equal(htmlToPlainText("模型: a<br/>得分: <b>72.4</b>"), "模型: a\n得分: 72.4");
  assert.equal(htmlToPlainText('<span style="color:#5470c6">甲</span>: 3'), "甲: 3");
  const opt = { tooltip: { formatter: () => "甲<br/>乙" }, series: [{ label: { formatter: () => 42 } }] };
  plainTextFormatters(opt);
  assert.equal(opt.tooltip.formatter(), "甲\n乙");
  assert.equal(opt.series[0].label.formatter(), 42); // 非字符串返回值原样放行
});

test("★ htmlToPlainText：数据里的尖括号不得吞掉整段 tooltip（评审修）", () => {
  // flint 把原始单元格值**裸插**进 HTML 串。旧的 /<[^>]*>/g 里 `[^>]` 不排除换行，于是
  // 一个值里的 `<` 与后一个值里的 `>` 之间的一切被整段删掉——实测只剩 "=P95"。
  assert.equal(
    htmlToPlainText("<10ms<br/>延迟: 5<br/>组: >=P95"),
    "<10ms\n延迟: 5\n组: >=P95",
  );
  assert.equal(htmlToPlainText("a > b <br/> c < d"), "a > b \n c < d");
});

test("★ htmlToPlainText：不解 HTML 实体（flint 从不转义，出现即数据本身，评审修）", () => {
  assert.equal(htmlToPlainText("A&amp;B"), "A&amp;B");
  assert.equal(htmlToPlainText("&lt;tag&gt;"), "&lt;tag&gt;");
  // 顺带钉住上游前提：dist 里没有任何转义动作，故上面那条推理成立。
  const dist = readHere("../node_modules/flint-chart/dist/echarts/index.js");
  assert.equal((dist.match(/&lt;|&amp;/g) || []).length, 0, "上游开始转义了 —— 请重新决定要不要解实体");
});

// ── ★ 畸形单元格的下场钉死（决策P10.2-11：已知缺口，不让它无声开合）────────────────

// **跑的是 flint_chart.js 真正调用的那条链**（决策P10.2-12 / 评审修）：以前这里手抄了一份
// 9 步副本，删掉产品里的形态守门、或把 assertSafeOption 挪到剥 `_` 之前，35 条用例照样全绿。
const runChain = (raw, avail = 0) => compileFlintOption(assembleECharts, raw, avail).opt;
const cellCase = (values) => ({
  data: { values },
  chart_spec: { chartType: "Bar Chart", encodings: { x: { field: "c" }, y: { field: "v" } } },
});

test("畸形单元格：数组行 / null 行 → 抛（安全：徽标 + 保留源码）", () => {
  // 数组行的列集是 {"0","1"} ⇒ 列名校验先拦下；null 行被列集跳过、由 flint 自己抛。
  assert.throws(() => runChain(cellCase([["a", 1], ["b", 2]])), /unknown-field/);
  assert.throws(() => runChain(cellCase([null, { c: "b", v: 2 }])));
});

test("畸形单元格：null 单元格 → 通过（图上一个缺口，语义合理）", () => {
  const opt = runChain(cellCase([{ c: "a", v: null }, { c: "b", v: 2 }]));
  assert.deepEqual(opt.series[0].data, [null, 2]);
});

test("★ 畸形单元格：嵌套对象 / 嵌套数组 → **当前通过**（明写接受的已知缺口，决策P10.2-11）", () => {
  // 本用例**故意断言现状**：这两种不抛错、产出**非空但错误**的 series，正好绕过空 series 守门
  // ⇒ 一张静默说谎的图。它不是安全面（单元格值全程无字符串→DOM 解析），且触发它需要作者把
  // 对象塞进单元格——skill 自校验清单第 4 条已明写禁止，属作者纪律能覆盖的范围。
  // **哪天有人开了 flint_core.mjs 里那行可选加固（抽查被 encodings 引用列的值类型），这条会变红**
  // ——那正是提醒：请同步改 docs/P10.2 §3.3.1、决策P10.2-11 与 app/CLAUDE.md，而不是删掉本用例。
  const objCase = runChain(cellCase([{ c: "a", v: { n: 1 } }, { c: "b", v: { n: 2 } }]));
  assert.ok(objCase.series[0].data.length > 0, "缺口已被堵上 → 请同步更新文档与不变量");
  const arrCase = runChain(cellCase([{ c: "a", v: [1, 2] }, { c: "b", v: [3] }]));
  assert.ok(arrCase.series[0].data.length > 0, "缺口已被堵上 → 请同步更新文档与不变量");
});

// ── ★ 37 图型不误杀（沿决策P4.20-16，上游升级后必跑）──────────────────────────────

test("★ 37 图型 × 各自正确的数据形状 → 全过闸，0 个被误拒", () => {
  // 「该拒的拒得住」靠上面那些打桩用例证；「不该拒的不乱拒」**只能靠把全集跑一遍**。
  const CAT = ["甲", "乙", "丙", "丁"];
  const DATES = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"];
  // 各图型的「必给通道」取自 skill 的 references/flint-spec.md 通道列（由 ecAllTemplateDefs 生成）。
  const REQUIRED = {
    "Bar Chart": ["x", "y"], "Grouped Bar Chart": ["x", "y", "group"], "Stacked Bar Chart": ["x", "y", "color"],
    "Lollipop Chart": ["x", "y"], "Pyramid Chart": ["x", "y"], "Waterfall Chart": ["x", "y"],
    "Bullet Chart": ["y", "x", "goal"], "Gauge Chart": ["size"], "Radar Chart": ["x", "y"], "Rose Chart": ["x", "y"],
    "Line Chart": ["x", "y"], "Area Chart": ["x", "y"], Streamgraph: ["x", "y", "color"],
    "Range Area Chart": ["x", "y", "y2"], "Bump Chart": ["x", "y", "color"], "Slope Chart": ["x", "y", "color"],
    "Candlestick Chart": ["x", "open", "high", "low", "close"], "Calendar Heatmap": ["x", "color"],
    Histogram: ["x"], "Density Plot": ["x"], "ECDF Plot": ["x"], Boxplot: ["x", "y"], "Strip Plot": ["x", "y"],
    "Ranged Dot Plot": ["x", "y"], "Scatter Plot": ["x", "y"], Regression: ["x", "y"],
    "Connected Scatter Plot": ["x", "y", "order"], Heatmap: ["x", "y", "color"], "Parallel Coordinates": ["color", "detail"],
    "Pie Chart": ["size", "color"], "Funnel Chart": ["y", "size"], Treemap: ["color", "size", "detail"],
    "Sunburst Chart": ["color", "size", "detail"], Tree: ["color", "detail"], "Sankey Diagram": ["x", "y", "size"],
    "Network Graph": ["x", "y", "size"], "Gantt Chart": ["y", "x", "x2"],
  };
  const NUM_X = ["Histogram", "Density Plot", "ECDF Plot", "Scatter Plot", "Regression", "Connected Scatter Plot", "Bullet Chart"];
  const DATE_X = ["Calendar Heatmap", "Candlestick Chart", "Line Chart", "Area Chart", "Streamgraph", "Range Area Chart", "Bump Chart", "Slope Chart", "Gantt Chart"];
  const CAT_Y = ["Bullet Chart", "Funnel Chart", "Sankey Diagram", "Network Graph", "Heatmap", "Boxplot", "Strip Plot", "Ranged Dot Plot", "Gantt Chart"];
  const NUMERIC = new Set(["y", "y2", "size", "opacity", "order", "goal", "open", "high", "low", "close"]);
  const cellFor = (ch, chart, i) => {
    if (ch === "x") return NUM_X.includes(chart) ? 10 + i * 7 : DATE_X.includes(chart) ? DATES[i % 4] : CAT[i % 4];
    if (ch === "y") return CAT_Y.includes(chart) ? CAT[(i + 1) % 4] : 5 + i * 3;
    if (ch === "x2") return DATES[(i + 1) % 4];
    if (NUMERIC.has(ch)) return 2 + i * 4;
    return CAT[i % 4]; // color / group / detail
  };

  const failures = [];
  for (const def of ecAllTemplateDefs) {
    const chart = def.chart;
    const chans = REQUIRED[chart] || def.channels.slice(0, 2);
    const values = [];
    for (let i = 0; i < 8; i++) {
      const row = {};
      for (const ch of chans) row[`col_${ch}`] = cellFor(ch, chart, i);
      // Parallel Coordinates 的轴由**数据里的数值列**自动成轴、不经 encodings；只给 color/detail
      // 会让 flint 静默回吐非 ECharts 中间态（这不是误杀，是数据形状不对）。
      if (chart === "Parallel Coordinates") Object.assign(row, { 指标A: 10 + i * 3, 指标B: 50 - i * 2, 指标C: i * i + 1 });
      values.push(row);
    }
    const encodings = {};
    for (const ch of chans) encodings[ch] = { field: `col_${ch}` };
    try {
      runChain({ data: { values }, chart_spec: { chartType: chart, encodings } });
    } catch (e) {
      failures.push(`${chart}: ${e.message}`);
    }
  }
  assert.equal(ecAllTemplateDefs.length, 37, "上游图型数变了 —— 请同步 skill 的图型表与本用例");
  assert.deepEqual(failures, [], `被误拒的图型：\n${failures.join("\n")}`);
});

// ── 双运行时自包含性（§3.2 那张表的机械化；**升级钉版必跑**）──────────────────────

test("★ 双运行时是真·自包含浏览器 ESM（沙箱渲染进程无 CJS 互操作，P10.1 在这里栽过一次）", () => {
  const targets = [
    { name: "flint-chart", rel: "../node_modules/flint-chart/dist/echarts/index.js", expect: "assembleECharts" },
    { name: "echarts", rel: "../node_modules/echarts/dist/echarts.esm.min.js", expect: "init" },
  ];
  for (const t of targets) {
    const src = readHere(t.rel);
    // bare import：`from "pkg"`（**正则须容忍 from 与引号之间的空格**）；相对路径不算。
    const bare = [...src.matchAll(/\bfrom\s*["']([^"']+)["']/g)].map((m) => m[1]).filter((s) => !/^[./]/.test(s));
    assert.deepEqual(bare, [], `${t.name} 有 bare import：${bare}`);
    assert.equal([...src.matchAll(/\bimport\s*\(/g)].length, 0, `${t.name} 有动态 import(`);
    assert.equal([...src.matchAll(/\brequire\s*\(/g)].length, 0, `${t.name} 有 require(`);
    assert.equal([...src.matchAll(/\bmodule\.exports\b/g)].length, 0, `${t.name} 有 module.exports`);
    assert.equal([...src.matchAll(/\bprocess\./g)].length, 0, `${t.name} 用了 process.`);
    assert.equal([...src.matchAll(/\b__dirname\b/g)].length, 0, `${t.name} 用了 __dirname`);
    const names = [...src.matchAll(/\bexport\s*\{([^}]*)\}/g)]
      .flatMap((m) => m[1].split(","))
      .map((s) => s.trim().split(/\s+as\s+/).pop());
    assert.ok(names.includes(t.expect), `${t.name} 缺具名导出 ${t.expect}`);
  }
});

// ── 接线断言（防漏接）───────────────────────────────────────────────────────────────

test("接线：enhanceContent 调 enhanceFlint、highlightCode 跳 language-flint", () => {
  const src = readHere("chat_view.js");
  assert.match(src, /enhanceFlint\(container\)/);
  // **必须匹配真正的守卫表达式，不能只匹配 `language-flint`**（评审修）：本次改动同时往
  // chat_view.js 里加了一句含该串的中文注释，宽松的 /language-flint/ 会被注释满足 ⇒ 把
  // 过滤器那行删掉测试照绿，而那正是「纯 flint 消息不该拉起 124KB hljs」这条懒加载的守卫。
  assert.match(src, /!c\.classList\.contains\("language-flint"\)/);
});

test("接线：flint_chart.js 走 flint_core 的共享编译链，不再手抄一份", () => {
  const src = readHere("flint_chart.js");
  assert.match(src, /compileFlintOption\(/);
  for (const dup of ["assertSafeOption(", "assertNoSilentTruncation(", "applyChahuaPalette("])
    assert.ok(!src.includes(dup), `flint_chart.js 又自己拼链路了：${dup}`);
});

test("接线：renderSidebar 挂了卸载钩子 resetFlintCharts（只有 sweep 的话切空房稳定泄漏）", () => {
  const src = readHere("renderer.js");
  assert.match(src, /resetFlintCharts\(\)/);
});

test("★ 接线：setOption 必须早于 replaceWith（顺序反了会静默留一个空 figure）", () => {
  // 决策P4.20-15：失败封闭靠的是**顺序** —— replaceWith 是整条链的最后一步。
  // 这条错了不报错、只静默空白，review 时最容易放过。
  const src = readHere("flint_chart.js");
  const setOpt = src.indexOf("chart.setOption(");
  const replace = src.indexOf("pre.replaceWith(");
  assert.ok(setOpt > 0 && replace > 0, "找不到 setOption / replaceWith 调用点");
  assert.ok(setOpt < replace, "replaceWith 跑在了 setOption 之前 —— 失败路径会留一个空 figure");
});

test("★ 接线：assertNoSilentTruncation 必须早于剥 `_` 私有键（_warnings 就住在那儿）", () => {
  // 链路已收进 flint_core.mjs::compileFlintOption，故断言落在那里。
  const src = readHere("flint_core.mjs");
  const warn = src.indexOf("assertNoSilentTruncation(opt._warnings)");
  const strip = src.indexOf('k.startsWith("_")');
  assert.ok(warn > 0 && strip > 0);
  assert.ok(warn < strip, "先剥私有键就再也读不到 _warnings，flint 的静默截断会漏过去");
});

test("flint_core.mjs 零浏览器 API（决策P10.2-12 的结构保证）", () => {
  // 本文件顶部那句 `import … from "./flint_core.mjs"` 已经证了一半：顶层若 new 一个浏览器 API，
  // 纯 Node 里 import 当场就炸。这条补另一半 —— **函数体内**新引入的浏览器 API（import 不会炸、
  // 只在跑到时炸），那正是把纯逻辑关回 DOM 半边的开始。
  // 注释里可以谈论这些 API（本文件就在谈），故先剥注释：块注释全剥，行注释只剥「`//` 前是空白或
  // 行首」的那种 —— 这样不会误伤字符串里的 `image://`（`//` 前是 `:`）。
  const src = readHere("flint_core.mjs")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|\s)\/\/.*$/gm, "$1");
  for (const api of ["IntersectionObserver", "document.", "window.", "getBoundingClientRect"])
    assert.ok(!src.includes(api), `flint_core.mjs 引入了浏览器 API：${api} —— 本文件必须能在纯 Node 里 import`);
});
