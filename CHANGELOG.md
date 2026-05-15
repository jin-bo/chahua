# Changelog

格式参考 [Keep a Changelog](https://keepachangelog.com/)；版本号遵循 [SemVer](https://semver.org/)。
完整发布说明见 [`docs/releases/`](docs/releases/)。

## [Unreleased]

### Added
- **房间共享文件**：每间房 `rooms/<room>/share/` 为共享目录，每位茶客的 `<guest_workdir>/share` 软链到此（Windows 无符号链接权限时降级为 WARN）。Composer 左侧附件按钮支持多文件上传，文件随下一条用户消息以 `<./share/xxx>` 形式进入 transcript 与茶客上下文。单文件 2MB 上限；文件名经 `sanitize_fs_name` 洗过，traversal / 绝对路径双向拒绝；详见 `docs/DESIGN.md` §3.10。
- **多行输入**：composer 由 `<input>` 切 `<textarea>`，Enter 发送、Shift+Enter 换行，textarea 内容增长自适应高度到 200px 切滚动；IME composition / `@` 补全 dropdown 行为不变。
- 新增 wire 帧：上行 `upload_file`（`filename` + `content_b64`），下行 `file_uploaded`（`rel` + `name` + `size` + `original`）；`user_message` 新增可选 `files: [str]` 字段。
- `chahua._fs.link_dir_idempotent`：共享的 symlink 幂等 helper（房间 share / persona skills 共用，单点处理 broken symlink + Windows 无权限边角）。

### Changed
- `_materialize`（persona skills）改用 `_fs.link_dir_idempotent` + 失败 copytree 兜底，去重约 25 行边角处理代码。

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
