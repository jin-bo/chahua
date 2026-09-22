# P19：Agentao 0.4.18 → 0.5.3 升级计划

核验日期：2026-09-21。状态：已合入 main（PR #54，2026-09-22）；阶段 A–D 在 macOS 上全部执行，含真实 LLM 与桌面 GUI 验收（§6 / §6.1）。仅 Windows 原生构建与 HTTP/SSE MCP 未验。

目标是让 CLI、WebSocket sidecar 和桌面安装包统一使用 Agentao 0.5.3，保持现有房间、权限、调度及持久化契约。建议直接以 0.5.3 为交付版本；0.4.27 仅作为上游推荐的弃用诊断中间站。

## 1. 已核实的基线

| 项目 | 现状 | 计划 |
|---|---|---|
| 项目依赖 | `pyproject.toml` 为 `agentao>=0.4.18` | 调整为 `agentao>=0.5.3,<0.6` |
| 开发锁文件 | `uv.lock` 锁定 0.4.18 | 本次精确解析到 0.5.3，提交完整锁文件 |
| 最新稳定版 | PyPI 0.5.3，2026-09-19 发布，未撤回 | 实施时再次确认版本；后续发布不自动改变本计划目标 |
| Python | ChaHua ≥3.11；Agentao 0.5.3 ≥3.10 | 无需因这次升级提升最低 Python 版本 |
| 桌面安装来源 | `app/scripts/build-python-bundle.js` 安装同级 `../agentao`，未使用 `uv.lock` | 正式打包使用锁定发布依赖 |
| bundle 缓存 | Python 可执行文件存在即跳过，除非 `FORCE=1` | 首次强制重建，后续依据构建指纹失效 |
| 本地上游源码 | 已进入 0.5.4 开发周期 | 不作为 0.5.3 发布包的替代品 |

发布版本依据：[PyPI 0.5.3](https://pypi.org/project/agentao/0.5.3/)、[PyPI 发布元数据](https://pypi.org/pypi/agentao/json)。迁移依据：[官方迁移指南](https://github.com/jin-bo/agentao/blob/v0.5.3/docs/migration/0.4.x-to-0.5.0.md)、[上游变更记录](https://github.com/jin-bo/agentao/blob/v0.5.3/CHANGELOG.md)。迁移文档及源码同时对照了本机 `../agentao`；该 checkout 超前于发布版，实施时须以 0.5.3 制品复核接口。

## 2. 兼容性判断与重点

以下“未命中”是静态检查结果，不代表运行验证通过。

| 变化或耦合点 | ChaHua 现状 | 升级处理与验收 |
|---|---|---|
| 0.5.0 删除 `agentao.harness`、`agentao.session` 及旧 callback 参数 | 当前生产代码使用 `SdkTransport`，未发现这些旧入口 | 扫描生产代码及测试；在 0.4.27 上检查弃用告警 |
| 构造函数第六个起参数改为 keyword-only；session API 必须显式 `project_root` | `TeaGuest` 构造全部使用关键字；当前生产 import 未使用上述 session API | 复核所有调用点及测试替身，避免仅依赖告警扫描 |
| `Agentao.arun()`、取消与关闭 | `guest.py` 依赖文本返回、`CancellationToken`、`close()` | 真实 Agentao 实例验证成功/异常/取消，`message_start/end` 成对且 ID 一致 |
| MCP SDK 与客户端演进 | `mcp_thread.py` 注入自定义 manager；0.5.3 允许 `mcp>=1.26,<3` | 核对 manager 消费接口、分页、连接状态和结果形状；测试 stdio、HTTP/SSE、失败和退出，保留同 owner-task 开关连接的约束 |
| 上游已内化 MCP 线程模型（见 §2.1） | `ThreadedMcpClientManager` 是鸭子类型替身，在自己的 owner task 里调 `client.connect()` | 升级后变成 owner-task 套 owner-task；阶段 A 显式决策保留或退役，阶段 B 默认保留并补回归 |
| 0.4.20 权限 BREAKING：坏的 `permissions.json` 中止 session；规则严格校验、未知 `action` 被拒 | `guest.py` 用 `PermissionEngine(rules=[], loaded_sources=[])`，不读策略文件、不传规则 | 已查、静态未命中；实测只需确认空规则构造在 0.5.3 仍合法 |
| 0.4.20 改了 read-only 拒绝文案（追加“不要重试”） | 生产代码与测试未断言该字面量（已 grep） | 无需改动；P17 验收仍以调试抽屉工具调用 `ok` 为准，不比对拒绝文案 |
| 权限与工具执行 | `permissions.py` 同步 engine 与 runner；工具继承 `Tool` / `AsyncToolBase` | 验证只读拦截、提案只 emit、artifact 专用写入及异步 bg 工具；审计新增内置工具和权限枚举 |
| transport/debug/artifact | 消费 `LLM_TEXT/THINKING/TOOL_START/TOOL_COMPLETE/ERROR` | 对照 0.5.3 实际 payload，验证成功、失败、取消、工具产物归属；新增事件不能破坏现有 envelope |
| 新原生协议与 LLM 返回类型 | `build_client()` 未传 `api_format`；oneshot 读取 `choices[0].message.content` | 首阶段维持现有协议，验证 chat/stream/工具轮次；不根据 URL 或模型名推断协议 |
| 依赖增量 | 新增核心 `anthropic` SDK，Windows 带条件性 tree-sitter 依赖 | 审查锁文件的直接/传递依赖变化，验证 macOS、Windows bundle 及体积变化 |
| skills、记忆和上下文 | 能力投影访问 `tools` / `skill_manager`，现有房间保有 `.agentao` 数据 | 对真实实例验证 introspection、技能发现、长上下文、清历史与旧数据读取 |

0.5.1–0.5.3 的发布说明将这些版本描述为增量改进；跨越 0.5.0 的删除项仍必须单独验证。0.4.19 起还有 MCP 协商、分页和执行行为修复，0.4.20 有两条标注 BREAKING 的权限变更（上表已列），因此不能只检查 0.5.0 的 import 是否报错。

已对照 `v0.5.3` tag 静态核对：ChaHua 的 17 处 agentao import 路径全部存在；`TeaGuest` 传给 `Agentao(...)` 的 6 个 kwarg（含 `mcp_manager=`）仍在；`set_mode`、`set_readonly_mode`、`clear_history`、`close`、`tools.register`、`skill_manager` 仍在。尚未核对、留给阶段 A/B 实测：`arun(images=)` 与 `CancellationToken` 的签名、`LLMClient` oneshot 返回形状。

### 2.1 `mcp_thread.py` 的前提已被上游改掉

`mcp_thread.py` 存在的理由是 0.4.18 的 `McpClientManager` 在调用方的 `run_until_complete` 里跑 loop，放进 ws 事件循环会炸。0.5.3 上游已改：

- `McpClientManager` 自带常驻 loop 线程，经 `run_coroutine_threadsafe` 提交（上游 #241）。
- `McpClient` 新增 `_own_connection` / `_stop_owner`，连接的 owner-task 由 client 内部管理。

后果：`ThreadedMcpClientManager._own_client` 在自己的 owner task 里调 `client.connect()`，升级后是 owner-task 套 owner-task。接口面是兼容的——上游消费的 `connect_all`、`get_all_tools`、`get_client`、`call_tool`、`disconnect_all`、`get_server_status`、`clients` shim 全覆盖；`Agentao.close()` 调无参 `disconnect_all()`，上游新增的 `timeout` 参数不影响。风险在行为而非签名，且 shim 是鸭子类型，不会跟着上游演进。

处理：本次升级默认保留 shim（改动面最小），用回归用例钉住双层 owner 下的行为；退役 shim、直接用上游 manager 列入 §5。保留或退役任一改变了 P17「owner-task：同一 task 进出 exit stack」的表述，CLAUDE.md §P17 与 `docs/P17-只读长期记忆-GuanLan-MCP.md` 两处同步。

## 3. 分阶段实施

### A. 基线与差异清单

1. 在独立分支及临时测试房间执行，记录 ChaHua commit、解释器和实际安装依赖版本。
2. 用现有锁文件执行 `uv sync --locked`、`uv run --locked pytest`，记录基线失败，避免把既有问题归因于升级。
3. 在不触碰项目环境与 `uv.lock` 的临时环境里装 Agentao 0.4.27 跑弃用诊断，用仓库外的独立 venv：`uv venv <tmp>/venv-0427` → `uv pip install --python <tmp>/venv-0427 -e . agentao==0.4.27 pytest pytest-asyncio` → `<tmp>/venv-0427/bin/python -m pytest -W error::DeprecationWarning`；禁止为此执行 `uv lock` / `uv add` / `uv sync`。第三方既有告警单独归因，不能整体屏蔽弃用告警。
4. 对照 0.5.3 制品核验上表 API 和工具清单；记录必改项、无需改动项和待实测项。
5. 对 §2.1 做显式决策并记录理由：保留 `mcp_thread.py`（默认）还是退役。

交付：可复查的基线结果与兼容性清单；中间诊断不得改写项目发布锁文件。

### B. 依赖更新与最小适配

1. 修改约束为 `agentao>=0.5.3,<0.6`。
2. 执行 `uv lock --upgrade-package agentao==0.5.3`，审查解析结果，仅接纳升级所需的依赖变更。
3. 执行 `uv sync --locked`，确认安装版本确为 0.5.3。
4. 按实际差异修改 `guest.py`、`mcp_thread.py`、`permissions.py`、`transport_bridge.py`、工具注册或 LLM 适配；没有证据的接口不重写。
5. 对发现的兼容缺口补契约测试。至少保留一层“真实 Agentao + 模拟 LLM/本地 MCP”的集成验证，避免 mock 替身掩盖接口变化。
6. 保留 shim 时，在 `test_mcp_thread.py` 补双层 owner 回归，至少三个场景：`disconnect_all` 不挂死且无 cancel-scope 错、连接失败路径状态与错误信息正确、在运行中的 ws 事件循环内调用不炸。

边界：本阶段不改调度策略、MTS 预算、房间协议、数据格式或默认 wire。不引入 Agentao 原生调度来替换 ChaHua handoff/bg run。

### C. 开发与发布依赖统一

1. 正式 bundle 从 `uv.lock` 导出目标运行依赖并按锁定版本安装；ChaHua 自身以构建 wheel 安装，禁止第二次不受约束地解析其依赖。
2. 正式构建不再依赖 `../agentao` 是否存在。若保留本地联调入口，要求显式启用并记录源码 commit、dirty 状态，不能混入正式发布。
3. bundle 指纹覆盖锁文件、ChaHua 构建输入、Python 请求版本、OS/架构及构建脚本；只在完整构建与检查成功后写入完成标记，失败产物不能命中缓存。
4. 首次执行 `cd app && FORCE=1 npm run build:python`，在 bundle 内读取 `importlib.metadata.version('agentao')`，并执行依赖一致性与 sidecar 启动检查。
5. macOS 与 Windows 分别原生构建验收；保持 app_root/user_data_root 双根、ready 信号、stdin EOF 和强制结束进程树逻辑。

交付：开发和桌面包实际版本一致的记录，以及脱离同级源码仍可构建的结果。

### D. 回归与发布

| 验证层 | 必测内容 | 通过标准 |
|---|---|---|
| 全量自动化 | `uv run --locked pytest`（前端 `flint_core.test.mjs` 由 `tests/test_frontend_flint.py` 挂在 pytest 上，`app/package.json` 无独立 `test` 脚本）；沙箱渲染进程真加载另跑 `cd app && npm run smoke:flint` | 无新增失败；基线问题有明确归因 |
| 核心契约 | guest/transport、MCP、权限、task tools、skills、LLM 配置/凭证注入、图像输入 | 保持现有不变量；密钥不进日志、toml 或 envelope |
| 编排回归 | @/broadcast、manual 零打分、handoff/panel、MTS 待机/收尾、bg run、取消/切房/断线 | 无漏结束、重复调度、busy 残留或后台泄漏 |
| CLI/sidecar 实测 | 默认 LLM、显式 section、流式文本、读工具、artifact、附图、长对话压缩 | 真实请求成功，工具及产物关联正确，异常能恢复 |
| 桌面实测 | 全新用户目录与旧数据副本、历史回放、清历史、MCP 连接、退出重启 | 数据可读、无遗留进程，bundle 版本为 0.5.3 |

优先复用 `test_mcp_thread.py`、`test_guest_caps.py`、`test_guest_skills.py`、`test_guest_task_tools.py`、`test_transport_recorder.py`、`test_llm_spec.py`、`test_set_llm_credentials.py`、图像/任务/后台相关现有用例。真实服务验证使用专用测试房间与现有授权凭据；缺少平台或凭据的项目应标为待验收，不能宣称已通过。

更新最低依赖说明（`README.md` 安装步骤与 `CLAUDE.md`「常用命令」里的 `agentao≥0.4.18` 两处）、发布说明和构建说明。`CLAUDE.md` §P17 的「agentao ≥0.4.14」是特性起始版本，不改。只有实际改变承重契约时，才同步更新对应设计文档与 `docs/INVARIANTS.md`。

## 4. 发布与回滚门槛

- 建议拆成三个可审查提交：依赖与适配、构建来源与缓存、验证记录与文档；正式发布必须三者一起到位。
- 升级前保留旧安装包和依赖锁；用房间及 `.agentao` 数据副本做兼容试跑，检查是否发生上游存储迁移。
- 若出现只读越权、历史不可读、MCP 退出挂死、消息生命周期失配，停止发布并修复。
- 回滚时成组恢复升级前代码/构建脚本/`pyproject.toml`/`uv.lock`，按旧锁重装并强制重建，不能只降低依赖声明。
- 如果新版本已改变试跑数据格式，旧版使用升级前数据副本；恢复正式用户数据前保留升级后的新增内容，避免覆盖丢失。

## 5. 后续可选工作

退役 `mcp_thread.py`、直接使用上游带线程的 `McpClientManager`：前提是上游 manager 在 ws 事件循环内的连接、并发调用、关闭行为经实测等价，并同步改写 P17 owner-task 不变量与回归测试。

原生 Anthropic Messages、OpenAI Responses、usage 展示单独立项。接入协议时需完成 `LLMSpec → 配置白名单 → session/build_client → admin/toml → UI/凭证入口` 的一致设计，兼顾 section fallback 与不泄露密钥；不能只在某个构造函数增加参数。基础升级验收不依赖这些新功能。

完成定义：0.5.3 锁定、适配与全量回归通过、CLI/sidecar/桌面包一致、目标平台实测有记录、旧数据及回滚路径可用。

## 6. 执行记录（2026-09-21，macOS arm64，分支 `chore/p19-agentao-0.5.3`，基于 `de7bfaf`）

| 步骤 | 结果 |
|---|---|
| A2 基线（锁定 0.4.18） | `uv run --locked pytest`：1576 passed |
| A3 弃用诊断（仓库外独立 venv，agentao 0.4.27） | `pytest -W error::DeprecationWarning`：1576 passed，无弃用命中；未触碰 `uv.lock` |
| A4 接口核对（0.5.3 已装制品） | `arun(user_message, max_iterations, cancellation_token, images)`、`CancellationToken`、`LLMClient(...)`（全关键字，`max_tokens` 默认 65536 与 0.4.18 相同）、`chat()` 返回形状、`Tool` / `AsyncToolBase` 抽象面、`ServerStatus` 枚举——均与 ChaHua 用法一致 |
| A5 shim 决策 | **保留 `mcp_thread.py`**：改动面最小；真 client 探测下双层 owner 行为正常 |
| B 依赖 | `agentao>=0.5.3,<0.6`；锁文件仅 agentao 0.4.18→0.5.3，新增 `anthropic` 1.7.0、`docstring-parser` 0.18.0、`tree-sitter` 0.26.0 / `tree-sitter-powershell` 0.26.4（后两者仅 win32），其余包未动 |
| B 适配 | **生产代码零功能改动**（仅 `mcp_thread.py` 模块说明更新） |
| B 新增契约测试 | `tests/test_agentao_contract.py`（真 `Agentao` + 本地假 OpenAI SSE 端点：流式往返与 message 成对 / 工具轮 TOOL_START·COMPLETE 与结果回灌 / read-only 拦 `write_file`）；`tests/test_mcp_thread.py` +3 条真 `McpClient` + 本地 stdio server（ws 循环内连接调用 / 关停无残留线程无 cancel-scope 错 / 连接失败路径）。新用例在 0.4.27 与 0.5.3 下均通过 |
| B 全量回归（0.5.3） | `uv run --locked pytest -W error::DeprecationWarning`：1582 passed |
| C 构建脚本 | 按 `uv export --locked` + `--require-hashes` 装依赖，chahua 走 wheel `--no-deps`；不再依赖 `../agentao`；`bundle-manifest.json` 构建指纹；`CHAHUA_AGENTAO_SOURCE` 显式联调入口 |
| C bundle 验收（macOS） | `FORCE=1` 重建成功；bundle 内 `agentao` 0.5.3 = 锁定版本；`pip check` 无冲突；体积 158–159M；缓存三态验证：无变化→跳过 / 改脚本→重建 / 改 chahua 源码→重建 |
| C bundle sidecar 启动 | 用 bundle 内 python + `app/templates` seed 的用户目录：0.6s 打出 ready 信号；stdin EOF 后 exit 0，无遗留进程 |

### 6.1 真实 LLM 与桌面 GUI 验收（2026-09-22，merge 后，macOS arm64）

LLM：Gemini `gemini-2.5-flash`（OpenAI 兼容端点，env 默认 LLM，三段 `source=default`）。房间 `p3-黄河路`（模板 seed 的用户目录），探针走 WebSocket，与桌面壳同一路径。

| 场景 | python | 结果 |
|---|---|---|
| `@宝总` 提问 | dev venv | 宝总确定性路由回答 → 范总打分过阈接话；2 start / 2 end 成对；13.7s；sidecar EOF 后 exit 0 |
| `@汪小姐` 用 `read_file` 读 `share/今日菜单.txt` | **bundle 内 python** | `tool_start=1`，回复含文件真实内容；6.6s；exit 0 |
| Electron dev 壳（`CHAHUA_USER_DATA` 指向上述目录） | dev（`uv run chahua-server`） | 0.1–0.5s 连上 sidecar；历史回放 8→9 条气泡（含上两轮）；`@范总` 发消息 → 流式回复入气泡；两次启动第二次回放第一次的回复；关窗无残留 sidecar / Electron 进程；transcript 落盘 |
| `npm run smoke:flint` | — | 21/21 PASS |

覆盖到的计划 D 表项：CLI/sidecar 实测行的默认 LLM、流式文本、读工具；桌面实测行的全新用户目录（模板 seed）、历史回放、退出重启。清历史走 `clear_room` 单测（含 `agent.clear_history()`），未在 GUI 点。

**未执行 / 待验收**（不得视为已通过）：

- **Windows 原生构建与验收**：本机 macOS，无 Windows 主机 / wine；`build-python-bundle.js` 用 `uv python install` 拉 `process.platform` 原生 python，Windows bundle 只能在 Windows 上产。
- HTTP/SSE 传输的 MCP（stdio 已实测；GuanLan 需真实 server）；MCP 连接的 GUI 信任门。
- 显式 LLM section、附图（P13）、长对话压缩、`electron-builder` 出 dmg、旧 `.agentao` 数据副本试跑、`CHAHUA_AGENTAO_SOURCE` 联调路径实跑。

**探针踩到的两点（非回归，记下省下次时间）**：`chahua-server --port 0` 的 ready 行打的是请求值 `:0` 不是实际端口，探针须自己挑空闲端口；本机 shell 有代理 env，`websockets.connect` 连 127.0.0.1 也走代理（HTTP 503），需 `proxy=None`——与 `test_agentao_contract.py` 设 `NO_PROXY` 同源。

**顺带观察（既有现象，非本次回归，未处理）**：sidecar 收到 stdin EOF 后优雅退出耗时约 1.7–4.8s（0.4.27 与 0.5.3 同量级），经常超过 `sidecar.js` 的 2s grace，实际落到 force-kill 兜底。
