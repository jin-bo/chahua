# 茶话室（chahua）

多 Agent 群聊「茶话室」桌面 App —— 用户和多个由 [agentao](../agentao) 驱动的 AI「茶客」
在同一个聊天室里对话。完整设计见 [`docs/DESIGN.md`](docs/DESIGN.md)。

当前进度：**P0 骨架**。单茶客（宝总）、CLI、内存 transcript、`USER.md` 角色、流式输出。

## 跑起来

```bash
# 1. 装依赖（uv 会按 pyproject.toml 把 ../agentao 编辑模式装上）
uv sync

# 2. 配 LLM 凭据（任何 OpenAI-兼容 API 都行）
#    查找顺序：shell export > 项目根 .env > ~/.env
cp .env.example .env
$EDITOR .env   # 填 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL
# 或者已经在 ~/.env 里配过的话，这步跳过即可

# 3. 编辑 USER.md，设你自己的「显示名」和偏好

# 4. 进茶话室
uv run chahua
```

REPL 内：

- `老金> 你好`（提示符的"老金"来自 USER.md `## 显示名`）→ 宝总会流式回话
- `/info` 看权限状态（验证 `read-only` 同时设了 `permission_engine` 和 `tool_runner.readonly_mode`）
- `/quit`、空行、Ctrl-D 退出

## 数据位置（本地明文）

所有数据都在用户机器上，没有云端，没有加密。详见 `docs/DESIGN.md` §3.7。

- `rooms/p0-test/transcript`（P0 是内存，未落盘；P2 会写 `transcript.jsonl`）
- `rooms/p0-test/guests/宝总/.agentao/` —— 宝总的私有 memory.db / sessions
- `rooms/p0-test/guests/宝总/agentao.log`
- `USER.md` —— 你的角色卡，每轮 reload
- `.env` —— LLM 凭据（已 `.gitignore`）

## 接下来

- **P1 调度**：意愿打分主循环、`@` 提及确定性路由、刚发言者冷却、阈值衰减、cursor + onboarding 摘要。
- **P2 服务化**：WebSocket server + 前端事件 envelope + 落盘。
- **P3 Electron**：桌面 UI。
- **P4 打磨 + ACP**：异构茶客（接入非 agentao 的 ACP-speaking agent）。
