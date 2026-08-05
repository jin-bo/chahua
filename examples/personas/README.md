# examples/personas —— 示例茶客与它们的 sidecar

这里放的是**随仓库走的示例 persona**，用来演示 persona 包的三样东西：人格卡（`<name>.md`）、
包清单（`persona.toml`）、以及两类 **sidecar**——`skills/` 与 `mcp.json`。
用户从这里手工导入（复制到自己的 personas 目录，或在 UI 里「导入 persona」），
**它们不随 app 打包、也不 seed**。

## 目录一览

| persona | sidecar | 说明 |
|---|---|---|
| `Maya` | `skills/task-management`、`skills/flint-chart-author` | 任务推进型茶客：会开任务 / 推进决策，也会把结果画成图 |
| `Yvonne` | `skills/create_image`、`skills/standard-review`、`skills/lab-standard-review`、`mcp.json` | 带图像生成与评审流程的茶客 |
| `孙博士` | `mcp.json` | P17 只读长期记忆示例（接 GuanLan MCP 当自己的记性） |
| `elonmusk` / `唐三藏` / `审稿委员会` | — | 纯人格卡示例 |

## skills 的两个 scope：persona 自带 vs 全局

**这两个 scope 并存、不互斥**（agentao 默认 `SkillManager` 同时扫两处）：

- **persona scope（本目录用的就是这个）**：skill 放在 `<persona>/skills/<name>/SKILL.md`。
  装配房间时 `chahua/persona_assets.py::materialize_skills` 把整个 `skills/` 暴露到该茶客工作区的
  `<workdir>/.agentao/skills`（优先 symlink，故改 `SKILL.md` 立刻生效；Windows 普通用户退回 copytree）。
  **只有挂了这个 persona 的茶客拿得到。** `persona.toml` 不需要写任何东西——
  `_scan_skills` 自动发现 `skills/` 下每个含 `SKILL.md` 的子目录。
- **全局 scope**：把整个 skill 目录拷进 `~/.agentao/skills/<name>/`，**全体茶客都能用**，
  与房间 / persona 无关。

  ```bash
  cp -R examples/personas/Maya/skills/flint-chart-author ~/.agentao/skills/
  ```

选哪个：**只有某个角色该会的**（Maya 的任务管理）留 persona scope；
**跟角色无关、谁都可能用到的能力**（比如画图）适合放全局。

## flint 图表作者 skill（P10.2）

`Maya/skills/flint-chart-author/` 是 P10.2 的**作者侧供给**，与渲染端配套：
桌面壳会把茶客回复里的 ` ```flint ` 围栏块编译渲染成图表（见
[`docs/P10.2-flint 数据图表渲染.md`](../../docs/P10.2-flint%20数据图表渲染.md)）。

**为什么这一片是必须的**：mermaid / LaTeX 模型本就会写，而 **flint 是 2026 年的新语言、
模型不会自发产出合法块**——没有作者侧 skill，渲染器就是「通了电没有灯」。

skill 自校验清单里的**四条阈值与渲染端逐条一致**（顶层五键 / 拒 `data.url` / ≤1000 行 / 画布上界），
改渲染端闸值时要同步改它，否则 skill 会稳定产出被自家闸子拒渲的块。
清单里的「对象数组 + 标量单元格」则是**作者建议不是渲染端闸子**——两者不要混（P10.2 §3.3.1）。

> CLI（`uv run chahua`）不渲染图表，flint 块回字面 JSON；导出的 markdown 里同样是原样源码。
