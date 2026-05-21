# 茶话室（chahua）

多 Agent 群聊「茶话室」桌面 App —— 用户和多个由 [agentao](https://github.com/jin-bo/agentao) 驱动的 AI「茶客」
在同一个聊天室里对话（像微信群，可 @、茶客之间也能接话）。

- 项目介绍：[`docs/INTRODUCTION.md`](docs/INTRODUCTION.md)
- 完整设计：[`docs/DESIGN.md`](docs/DESIGN.md)
- 变更记录：[`CHANGELOG.md`](CHANGELOG.md) / 历次发布说明：[`docs/releases/`](docs/releases/)

## Quick Start

```bash
# 1. python 依赖（uv 按 pyproject.toml 从 PyPI 拉 agentao ≥0.4.6）
uv sync

# 2. LLM 凭据（任何 OpenAI-兼容 API 都行）
cp .env.example .env
$EDITOR .env       # 填 OPENAI_API_KEY / OPENAI_MODEL；也可改 LLM_PROVIDER 走 deepseek / moonshot / siliconflow / openrouter / ollama

# 3a. 桌面壳（默认入 rooms/p3-黄河路：宝总 / 汪小姐 / 范总）
cd app && npm install && npm run dev

# 3b. 或 CLI（默认入 rooms/p1-test）
uv run chahua
```

可选：编辑仓库根 `USER.md` 设你的显示名 / 语气偏好；不填默认显示名"用户"。

## 模式

### CLI（`uv run chahua`）—— 最快验证 LLM 凭据 / room.toml

REPL 内：

- `显示名> 你好`（提示符的显示名来自 `USER.md` `## 显示名`，未填走"用户>"）→ 茶客流式接话
- `/info` 看每位茶客权限（read-only / workspace-write / full-access）
- `/tools <茶客名>` / `/skills <茶客名>` 查某位茶客 agent 注册的工具 / 可用 skills；`/help` 看全部命令
- `/quit`、空行、Ctrl-D 退出

### Electron 桌面壳（`cd app && npm run dev`）

main 进程随机选空闲端口、起 `chahua-server` sidecar、建窗连 ws。关窗 = 退出 sidecar，
transcript / cursor / summary 持久化保留。

首启动从 `app/templates/` seed 默认房 / `.env.example` / `USER.md` 占位到用户数据根：
- dev 模式（`npm run dev`）：用户数据根 = 仓库根，seed 跳过不动 repo 文件
- packaged 模式（`.dmg`）：用户数据根 = `~/Library/Application Support/chahua/`，
  首启动写 `.chahua-seeded` marker 做幂等

可用快捷动作：

- 发言 / 停止当前 turn（同一按钮形变）；`@<茶客名>` 直接路由（含空格的名字也能命中）。
- 显式接力（handoff）：意愿打分之上的确定性指派通道。composer 底栏「指派」按钮弹一个统一面板，勾 1 位茶客 =「交给他发下一句」、勾 2~4 位 = 发起一场平行表态的圆桌（可选指定汇总人）；消息气泡上的「请审…」按钮另派一位茶客审阅该条消息。接力跑完即回到等用户输入、不回落打分；顶部「➡️ 下一句」队列条可整体取消（停当前 + 清后续）。茶客也能在发言里**提议**接力（指派 / 请审 / 圆桌），渲成「采纳 / 忽略」卡片，你点采纳后才生效。
- 多行输入：Enter 发送、Shift+Enter 换行；textarea 自动撑高到 200px 切滚动。
- 附件上传：composer 左侧 `+`，文件落房间共享目录，随下一条消息一起进上下文。
- 任务房间：右上角面板「+ 新任务」开任务 → composer 顶部 chip 显示当前 active 任务；茶客感知 active 任务（onboarding/incremental prompt 都注入 task 块），可写 `./task/<name>` 自动归集为 artifact、可 propose 决策 / 新任务 / 改任务状态（如达成时提议标记完成）待你「采纳」。产物列表点击即下载（带白名单 + symlink 防逃逸）。多任务共存，单时刻最多 1 个 active。
- 调试抽屉：与任务面板互斥占右侧槽位，看每一轮谁被候选 / 分数 / 选谁 / prompt / 用了哪些工具 / 产物路径；切房 / 重启后仍能翻"开窗前"的历史 turn（点击索引行即拉详情）。默认开，盘上落到 `rooms/<id>/debug/`；`max_turns = 500` 按 turn 自动 rotation；`room.toml [debug] enabled = false` 可关。
- 茶客能力查询：composer 里输 `/tools <茶客名>` / `/skills <茶客名>` 查某位茶客 agent 注册的工具 / 可用 skills（只读，结果显示为系统气泡，不进记录）；`/help` 看全部斜杠命令。
- sidebar：切换房间 / 加茶客 / 导入 persona（本地目录或 GitHub）；点茶客头像设权限、勾选信任其 MCP。
- **双击房间名** 弹「更改房间配置（直接编辑 room.toml）/ 清空聊天」；**双击用户头像或显示名** 弹「编辑配置（USER.md）/ 换头像（PNG / JPG / WebP / GIF）」。

## 数据位置（本地明文，无云端，无加密）

详见 `docs/DESIGN.md` §3.7。

- `rooms/<room>/transcript.jsonl` —— 房间公开记录（append-only）
- `rooms/<room>/summary.jsonl` —— onboarding 摘要历史
- `rooms/<room>/cursor.json` —— 每位茶客的喂养游标
- `rooms/<room>/share/` —— 房间共享文件目录；UI 上传落这里，每位茶客的 `<guest_workdir>/share` 软链到它
- `rooms/<room>/guests/<name>/.agentao/` —— 茶客的私有 memory.db / sessions
- `rooms/<room>/guests/<name>/agentao.log` —— agentao 日志
- `USER.md` —— 你的角色卡，每轮 reload
- `.env` —— LLM 凭据（已 `.gitignore`）

packaged 模式下以上路径全部相对 `~/Library/Application Support/chahua/`；
ship 自带的 python 包 + 默认 personas 在 `.app` bundle 内只读，与用户数据分离。

## 接下来

- **macOS 签名 / 公证**：当前 .dmg 仍未签名，用户双击会 Gatekeeper 红屏（ctrl-click → 打开 走一次即可）。脱 Gatekeeper 警告需 Apple Developer ID（$99/年），未排期。
- **P3.3.3 Windows 实跑**：build-python-bundle 跑 win 端 + electron-builder NSIS 出未签名 .exe；接缝（platform 分支 / sidecar resolver / `mklink /J` junction / stdin EOF graceful shutdown / `build.win` 配置）从 v0.1.0 起一直就位，仍剩 Windows 主机 / CI matrix 端真跑一次。
- **ACP 异构茶客**：当前只支持 agentao 驱动的茶客；接第一个非 agentao 的 ACP 茶客（`transport = "acp"`，详见 `docs/DESIGN.md` §3.9）是后续大版本，暂未排期。

## 打包发布

```bash
cd app
npm install                    # 含 electron-builder devDep（首次 ~150MB）
npm run build:python           # 把 python + chahua + agentao 烤进 app/python-bundle/（~130MB；idempotent）
npm run build:mac              # → app/dist/茶话室-<ver>-mac-arm64.dmg（首次约 75s）
```

未签名 .dmg：用户双击会 Gatekeeper 红屏 "无法验证开发者"；ctrl-click → 打开
走一次即可。要进 Mac App Store 或脱 Gatekeeper 警告需 Apple Developer ID（$99/年），
P3.3.2 内未做。Windows .exe 接缝（`build:win`）已配，跑构建需 Windows 主机或 CI matrix。

## License

MIT —— 详见 [`LICENSE`](LICENSE)。
