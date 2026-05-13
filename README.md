# 茶话室（chahua）

多 Agent 群聊「茶话室」桌面 App —— 用户和多个由 [agentao](../agentao) 驱动的 AI「茶客」
在同一个聊天室里对话。完整设计见 [`docs/DESIGN.md`](docs/DESIGN.md)。

当前进度：**P3.2.1 打字机 + 打分徽章**。
底层（P0–P2.3）已稳：三茶客 + 意愿打分调度 + 事件 envelope + transcript/summary/cursor 续聊 + USER.md 角色。
桌面壳能做的（P3.1+P3.2.1）：拉 sidecar / 建窗 / 流式打字机 / turn_start 顶部打分横条 / speaker 后挂 score 徽章 / cancelled & error 红字封口 / 回车发言。
@ 补全、茶客侧栏、停止按钮、ws 重连、打包 .dmg 在 P3.2.2 / P3.2.3 / P3.3。

## 跑起来

### CLI（最快验证 LLM 凭据 / room.toml）

```bash
# 1. 装依赖（uv 会按 pyproject.toml 把 ../agentao 编辑模式装上）
uv sync

# 2. 配 LLM 凭据（任何 OpenAI-兼容 API 都行）
#    查找顺序：shell export > 项目根 .env > ~/.env
cp .env.example .env
$EDITOR .env   # 填 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL
# 或者已经在 ~/.env 里配过的话，这步跳过即可

# 3. 编辑 USER.md，设你自己的「显示名」和偏好

# 4. 进茶话室（默认房间 rooms/p1-test）
uv run chahua
```

REPL 内：

- `老金> 你好`（提示符的"老金"来自 USER.md `## 显示名`）→ 茶客流式接话
- `/info` 看每位茶客权限（read-only / workspace-write / full-access）
- `/quit`、空行、Ctrl-D 退出

### Electron 桌面壳（P3.1）

CLI 跑通后，桌面壳是同一套 sidecar（`chahua-server`）+ ws 渲染：

```bash
cd app
npm install        # 装 Electron（~150MB，一次）
npm run dev        # main 进程自动拉起 chahua-server sidecar，建窗连 ws
```

P3.1 范围：拉 sidecar / 建窗 / 极简消息流（一条 message_end 一行）/ 输入框发 user_message。
打字机流式、@ 补全、茶客侧栏、停止按钮、打包 .dmg 都在 P3.2 / P3.3。

## 数据位置（本地明文）

所有数据都在用户机器上，没有云端，没有加密。详见 `docs/DESIGN.md` §3.7。

- `rooms/<room>/transcript.jsonl` —— 房间公开记录（append-only）
- `rooms/<room>/summary.jsonl` —— onboarding 摘要历史
- `rooms/<room>/cursor.json` —— 每位茶客的喂养游标
- `rooms/<room>/guests/<name>/.agentao/` —— 茶客的私有 memory.db / sessions
- `rooms/<room>/guests/<name>/agentao.log` —— agentao 日志
- `USER.md` —— 你的角色卡，每轮 reload
- `.env` —— LLM 凭据（已 `.gitignore`）

## 接下来

- **P3.2 消息 UI**：打字机流式（message_delta）、turn_start 打分徽章、茶客侧栏、@ 补全、error/cancelled 渲染。
- **P3.3 cancel + 打包**：停止按钮（ws `cancel` 帧）、electron-builder 打 macOS .dmg、isolation 徽章先位。
- **P4 打磨 + ACP**：逐茶客 provider/model、isolation=global、`[scoring]`/`[summary]` 分派、异构茶客（接入非 agentao 的 ACP-speaking agent）。
