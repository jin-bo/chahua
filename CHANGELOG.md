# Changelog

格式参考 [Keep a Changelog](https://keepachangelog.com/)；版本号遵循 [SemVer](https://semver.org/)。
完整发布说明见 [`docs/releases/`](docs/releases/)。

## [Unreleased]

### Added
- **双击 popover + raw room.toml 编辑器**（2026-05-15）：sidebar 删「清空聊天 / 编辑配置 / 换头像」三个按钮；双击房间名弹「更改房间配置 / 清空聊天」、双击用户头像/显示名弹「编辑配置 / 换头像」。「更改房间配置」是新功能：textarea 直接编辑当前房间 `room.toml`，服务端 `load_room_config` 校验失败回滚旧字节 + emit notice。新增 wire 帧上行 `update_room_toml`；`room_info` envelope 携带 `room_toml_content` / `room_toml_source` 给 modal prefill。新增 `admin.update_room_toml(room_dir, content, *, paths)`。详见 `docs/DESIGN.md` §3.11。
- **persona MCP & skills 装载 + MCP 信任门**（2026-05-15）：persona 目录可放 sibling `mcp.json`（MCP servers）和 `skills/`（agentao skills 目录）。skills 启动时通过 `_fs.link_dir_idempotent` 软链到茶客 `working_directory`；MCP 因为带来「任意可执行」风险**必须用户在 popover 上显式勾选「信任此 persona 的 MCP」才装载**。信任记录跨房间持久化在 `user_data_root/.chahua/persona-trust.json`。room_info envelope 在每位 guest 上附 `mcp_servers`（command + args 摘要）/ `mcp_trusted` / `skills_available`；新 inbound `set_persona_mcp_trust`。新增模块：`chahua/persona_assets.py` / `chahua/trust.py`。
- **「话题讨论你」打分加档**（2026-05-15）：scoring 阶段检测 transcript 里对**第三方**茶客的提及（非 `@`，是文本里"宝总你怎么看"这种），给被提及的茶客 score 一个额外加成档。让相关茶客更主动接话；@ 直接路由的确定性路径不受影响。
- **persona elonmusk + Yvonne / 唐三藏 精修**（2026-05-15）：新增 `examples/personas/elonmusk/`（含头像 / prompt / sibling toml）；唐三藏 / Yvonne prompt 重写。persona picker 现在读 sibling `<Name>.toml` 里的 `[guest].name` —— 目录叫 `Yvonne` 但显示「伊冯」之类的场景生效。
- **房间共享文件**：每间房 `rooms/<room>/share/` 为共享目录，每位茶客的 `<guest_workdir>/share` 软链到此（Windows 无符号链接权限时降级为 WARN）。Composer 左侧附件按钮支持多文件上传，文件随下一条用户消息以 `<./share/xxx>` 形式进入 transcript 与茶客上下文。单文件 2MB 上限；文件名经 `sanitize_fs_name` 洗过，traversal / 绝对路径双向拒绝；详见 `docs/DESIGN.md` §3.10。
- **多行输入**：composer 由 `<input>` 切 `<textarea>`，Enter 发送、Shift+Enter 换行，textarea 内容增长自适应高度到 200px 切滚动；IME composition / `@` 补全 dropdown 行为不变。
- 新增 wire 帧：上行 `upload_file`（`filename` + `content_b64`），下行 `file_uploaded`（`rel` + `name` + `size` + `original`）；`user_message` 新增可选 `files: [str]` 字段。
- `chahua._fs.link_dir_idempotent`：共享的 symlink 幂等 helper（房间 share / persona skills 共用，单点处理 broken symlink + Windows 无权限边角）。

### Changed
- **scoring / summarizer `_MAX_TOKENS` 抬高**（2026-05-15）：scoring 128 → 1024，summarizer 512 → 2048。Gemini 2.5 Flash 系列把 thinking budget 也算进 `max_tokens`，旧值会让 visible output 在 `{"score":` 处被 length 截断（Finish Reason: length, Completion Tokens: 4）。对 OpenAI / Claude / DeepSeek 等非 thinking 模型不增加实际 token 消耗。
- **popover 重构**（2026-05-15）：抽出 `positionPopoverByAnchor` + `attachPopoverDismissHandlers`，permission popover 与新的 action popover 共用；CSS 改 `.popover` 底座 + `.permission-popover` / `.action-popover` 修饰类，去除 `className = "action-popover permission-popover"` 双类样式借用。
- `_materialize`（persona skills）改用 `_fs.link_dir_idempotent` + 失败 copytree 兜底，去重约 25 行边角处理代码。

### Fixed
- **`@` 含空格的茶客名路由**（2026-05-15）：`@唐 三藏` 这种含空格的名字之前会被 mention 正则切断到第一个空格；修正口径以 guests 名册做最长前缀匹配。

---

首次公开发布。详见 [`docs/releases/v0.1.0.md`](docs/releases/v0.1.0.md)。

### Added
- 多 Agent 群聊核心：意愿打分调度 + 三茶客「黄河路」默认房（宝总 / 汪小姐 / 范总）+ `@` 补全 + `@broadcast`
- agentao 集成：每个茶客独立 `working_directory` / memory / sessions
- CLI 模式 `uv run chahua`（REPL + `/info` + `/quit`）
- Electron 桌面壳：sidecar + WebSocket + 流式打字机 + markdown 渲染（gfm + DOMPurify）+ 气泡复制
- 茶客侧栏：头像 + permission V 标 + 打分徽章
- 房间能力：进 Room 显示历史 / 切换房间 / 清空聊天 / 停止当前 turn / 断线退避重连
- 持久化：`transcript.jsonl` / `summary.jsonl` / `cursor.json` / 茶客私有 `.agentao/memory.db`
- 首启动 seed `app/templates/` → userData（dev 跳过；packaged 写 `.chahua-seeded` marker）
- macOS 打包：`npm run build:python` + `npm run build:mac` 出未签名 `.dmg`（~130MB，内嵌 python + chahua + agentao）
- 跨平台 graceful shutdown：stdin EOF 替 SIGINT
- sidecar 日志落盘到 `~/Library/Logs/chahua/`
- LICENSE (MIT) + `docs/INTRODUCTION.md`

### Changed
- agentao 依赖从本地 path-editable → PyPI `agentao>=0.4.6`
- 聊天布局换气泡（茶客左 / 用户右镜像）；打分徽章从主聊天挪到 sidebar 茶客名右侧
- 缺省茶室切到 `p3-黄河路`；宝总改 full-access
- 拆 `app_root` / `user_data_root`
- 意愿打分 `want_threshold` 0.55 → 0.45

### Fixed
- orchestrator 中途 pick None 后「停止」按钮卡死
