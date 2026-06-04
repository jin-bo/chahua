# P15：桌面登录态自动注入 LLM 配置 —— 用户在 desktop 选模型，零重启即时生效

> 桌面 app 集成了 chahua。用户登录后自动拿到**提供商 URL + apikey**，并可在使用中**选择用哪个模型**。最用户友好的形态是：用户在 desktop 选定模型后，**自动设置进 chahua**，无需手改 `.env`、无需重启 sidecar。toC 用户配大模型本就是门槛，登录态自动注入把它抹平。

> **本文档状态：MVP 已实现（server 侧纯 env 路径）。** `set_llm_credentials` inbound + handler 落在 `chahua/server_inbound_settings.py`，回归 `tests/test_set_llm_credentials.py`（12 测）。承重不变量已同步进 `CLAUDE.md`「配置与 LLM 装配」段 + `docs/INVARIANTS.md §P15`。**启动 spawn-env 注入 / 选模型 UI 联动属集成 host 职责**（选项 A），不改 chahua 自身 bootstrap；CLI / 自带壳走既有 `.env` / shell export，天然兼容。

## Summary —— 评审结论先行

原始提议给了两个备选方向：「动态修改配置」或「`.env` reload 机制」。评审后修正如下：

1. **方向对、价值真实**：toC 用户配大模型确实是门槛，desktop 登录态自动注入体验最佳。
2. **「动态修改配置」不是新增能力，而是已存在**：chahua 早有运行期热重建会话的机制 `_replace_session()`（`server.py:745`）—— admin 改配置后**不重启进程即时生效**。P15 只是给它一个新触发入口 + 凭证注入点。
3. **`.env` reload 是为错误前提设计的方案**：`load_env_files()`（`session.py:46`）只在进程启动时加载一次。即便实现 reload，仍要触发 session 重建才生效，白绕一圈，还得处理 dotenv 语义 / 时机 / 并发。**不推荐**。
4. **真问题是两座桥**：启动时 host 把凭证放进 sidecar child env；运行期换模型 →（新 inbound 帧）→ server 更新 `os.environ` + 热重建。

**核心矛盾**：chahua 有硬不变量「**API key 永不进 toml / envelope**」。而 desktop 登录拿到的是**内存里的 raw apikey 明文**。这决定了 key 的接入点**不是配置文件，而是进程环境变量层**。

**为什么纯 env、不写 toml**：chahua 房间默认 LLM 是两层 fallback —— `[room.llm]` toml 段 → `LLMSpec.try_from_env()`（`session.py:300`）。**只要 `room.toml` 不写 `[room.llm]`，`try_from_env()` 就接管**，从 `LLM_PROVIDER` / `<PREFIX>_MODEL` / `<PREFIX>_BASE_URL` / `<PREFIX>_API_KEY` 读全套凭证（env 路径允许裸 model）。于是 P15 全程只动 `os.environ`，**一行 toml 都不写**。这条路径同时消掉了「写 toml」方案的三类复杂度（磁盘回滚 / 结构化重写 / toml 持久 `api_key_env` 引发的启动死锁）。

**启动期的硬约束（首版文档漏掉、Codex 评审纠正）**：凭证必须在 **spawn sidecar 之前**就进 child env。否则启动时 `build_room_session` 装配房间默认会失败 —— 全缺 → `RoomConfigError`（`server_entry.py:83` 能接住、优雅报错）；**有 model 无 key → `build_client` 抛 `SystemExit`（`llm_spec.py:336`），而 `server_entry.py:83` 只接 `RoomConfigError`、不接 `SystemExit` → 进程直接死**。所以「启动后由前端重推凭证」是死路 —— host 必须 spawn 前把**全套 env 原子注入**。

## 推荐方案：启动 spawn-env + 运行期 inbound → `os.environ` → `_replace_session`

```
【启动路径 —— 凭证必须在 spawn 前就位】
host（Electron main）：登录态拿到 (provider, model, base_url, raw_api_key)
   └─ spawn sidecar 时把全套 LLM env 放进 child env（sidecar.js:106 的 env 对象）：
        LLM_PROVIDER=<provider>
        <PREFIX>_MODEL=<bare-model>          # PREFIX = provider.upper().replace("-","_")
        <PREFIX>_BASE_URL=<base_url>         # 缺则 build_client 按 provider 默认兜底
        <PREFIX>_API_KEY=<raw_api_key>
   ▼
sidecar 启动：build_room_session → room.toml 无 [room.llm] → try_from_env() 读上面 env
        → build_client 装配成功（全缺 → RoomConfigError 优雅报错；有 model 无 key → SystemExit 进程死）

【运行期换 key / 换模型 —— 走 inbound】
desktop 用户运行中改模型 / 换 key
   ▼
新 inbound 帧  set_llm_credentials{provider, model, base_url, api_key}   # 严格白名单，未知键 NOTICE error 丢帧
   ▼
server handler（不记录 raw payload）：
   ① 快照将要改写的 env 旧值（供失败回滚）
   ② 纯字符串校验（provider/model 非空、能拼成 <provider>/<model>）失败 → 不动 env + NOTICE error
   ③ 写 os.environ：LLM_PROVIDER / <PREFIX>_{MODEL,BASE_URL,API_KEY}   # key 只活在进程内存 environ，绝不落盘
   ④ _cancel_and_drain_all_foreground + _replace_session(...)   # 复用现成热重建：重跑 build_room_session → try_from_env → build_client → 替换前台 session
   ▼
即时生效，零重启。装配失败（_replace_session 返 False）→ 内存旧 session 保留 + 恢复 env 旧值 + NOTICE
```

为什么是这条路（对照四方案）：

| 方案 | 破坏不变量 | 用户体验 | 评价 |
|---|---|---|---|
| reload `.env` 文件 | 否 | 中 | ❌ 错前提；dotenv 不覆盖已存在 env、reload 时机/并发都要处理；最后仍要 session 重建 |
| 整进程重启 sidecar | 否 | 最差 | ❌ 断 ws、丢前台 runtime、清后台 MTS / bg run —— 正是要避免的 |
| 写 `[room.llm]` toml + handler 自理磁盘回滚 | 否 | 最佳 | ⚠️ 行为等价但多一层：磁盘原子写 + 失败回滚 + `_render_room_toml` + toml 持久 `api_key_env` 引发启动死锁论述 |
| **纯 env：inbound→`os.environ`→`_replace_session`（不写 toml）** | **否** | **最佳** | ✅ 凭证只在内存 environ，无 toml 写盘、无磁盘回滚；fallback 天然接管；手改 `[room.llm]` 自动优先 |

**纯 env 相对「写 toml」白送的简化**：① 没有 toml 写盘 → 没有磁盘回滚、没有「schema 合法但 build 不起来的坏 toml 落盘」中间态；② 启动失败退化成「全新 CLI 没配 .env」这个早被理解的 `RoomConfigError`，不是 chauha 自找的持久化陷阱；③ 用户在设置面板手写 `[room.llm]` 天然优先于 env（`_resolve_room_default_spec` 先查 toml），desktop 注入就是「房间默认兜底」，语义干净。**唯一 trade-off**：model 选择的跨重启持久化完全落在 host —— 但 host 本就必须持久化+重注入 key（见「运维闭环」），多管 model/base_url 是对称的边际增量。

## 启动 bootstrap 决策（必须二选一，不能写软条件）

当前 chahua 自带 Electron 壳的启动序是 `app.whenReady()` → `startSidecar()` → `createWindow(sidecar.wsUrl)`（`app/main/index.js:97/110/116`）—— **sidecar 先于窗口起**。若登录态来自 renderer（窗口里登录），sidecar 早就因缺 LLM env 启动失败了。所以 P15 落地前**必须定死下面二选一**，不能含糊写「若已登录就注入」（那必撞启动死锁）：

- **选项 A（集成 host 形态，本文档默认）**：**host 必须在 spawn chahua sidecar 之前就持有登录态**，spawn 时把全套 LLM env 注入 child env。这与「运维闭环」一节「凭证持久化+重注入全归 host」完全一致 —— host 是凭证的拥有者，sidecar 只是被喂。**P15 不为这条改 chahua 自身 bootstrap**。
- **选项 B（chahua 自带壳要自管登录时）**：必须**改 bootstrap 顺序** —— 先弹一个**不依赖 sidecar 的登录窗口**，登录拿到凭证后再 `startSidecar()`（env 已就绪）。本质是把 `startSidecar` 推迟到登录之后。MVP 不做，仅备记。

MVP 采选项 A。无论 A/B，红线一致：**`startSidecar` 那一刻 child env 必须已齐**。

## 承重不变量（提案，落地时同步进 CLAUDE.md / INVARIANTS）

- **raw apikey 永不落盘、永不进 toml / envelope / 日志**。desktop 推来的明文 key 只写进进程 `os.environ` 的 `<PREFIX>_API_KEY`；**P15 不写任何 toml**。下发给前端的只能是 `api_key_ready` bool / 当前 provider·model，绝不回声 key。这是 P15 的底线，与既有「API key 永不进 toml / envelope」同一精神。
- **P15 handler 禁止记录完整 inbound payload，日志只写 redacted 信息**。P15 inbound 帧**带 raw `api_key`**，若照搬 `log(... data ...)` 风格明文会落进 `sidecar.log`。**承重约束**：handler 只 log `provider` / `model` / `base_url` 的 host，或 redacted key（如 `sk-***`）；**绝不**整帧 / 整 payload 进日志。**Why**：sidecar 日志是最大、最易被忽略的泄漏面，比 transcript / envelope 更隐蔽。
- **`api_key` 字段绝不复用 `_server_helpers.require_str`（及同族 `require_*`）做校验**。这些共享校验器失败时 `_log.warning("... 收到 %r", v)`（`_server_helpers.py:27`）会把**收到的值原样 `%r` 进日志** —— 对 `provider` / `model` / `base_url` 无害，对 raw `api_key` 是直接泄漏。**承重约束**：`api_key` 走 handler **自写的敏感字段校验**，失败只 log 字段名 + 类型（`type(v).__name__`），**绝不 log 值**；`provider` / `model` / `base_url` 可继续用 `require_str`（它们本就不敏感）。**Why**：泄漏点不在「handler 主动打 payload」，而藏在「顺手复用的通用校验器」里 —— 最隐蔽的那类。
- **凭证是启动期硬依赖 —— host 必须 spawn 前把全套 LLM env 原子注入 child env**。`room.toml` 不写 `[room.llm]`，房间默认全靠 `try_from_env()`。启动时：全缺 → `RoomConfigError`（`server_entry.py:83` 接住、优雅报错）；**有 model 无 key → `build_client` 抛 `SystemExit`（`llm_spec.py:336`）→ `server_entry.py:83` 不接 → 进程直接死**。**因此承重约束**：host 必须在 `spawn`（`sidecar.js:106` 的 `env`）前一次性放齐 `LLM_PROVIDER` + `<PREFIX>_{MODEL,BASE_URL,API_KEY}`；**绝不把「启动后由前端重推凭证」写成可行路径** —— 那是死锁。落地前必按「启动 bootstrap 决策」二选一定死（MVP 选 A：host 先登录再 spawn），不写软条件。
- **运行期改 env 必快照旧值、失败回滚 env（不碰盘，因为根本没写盘）**。handler 改 `os.environ` 前先快照将动的几个 key 的旧值（含「原本未设」用 sentinel 记）；`_replace_session` 返 `False` → 逐个恢复（旧值写回 / 原本未设则 `del`）。**Why**：`_replace_session` 失败保留旧内存 session（`server.py:761`），若 env 已被改坏，下次切房 / 重建会拿坏 env 再炸；env 回滚让进程态与存活的旧 session 一致。**这是纯 env 路径里唯一的「回滚」，且全在内存、无磁盘 IO**。
- **重建前必 `_cancel_and_drain_all_foreground()`，不是 `_cancel_and_drain_inflight()`**。所有「准备 mutate / replace 前台 session」的 handler（`update_room_toml` / admin 加删茶客 / 改 LLM / clear_room）都走 `_cancel_and_drain_all_foreground()`（`server.py:862`），P15 照此沿用。**Why**：前台 runtime 里不只有普通 turn —— 还可能有 bg run / dormant MTS 持着旧 session/client 在跑；只 cancel inflight（`_cancel_and_drain_inflight` 是 cancel 按钮专用、有意不动 bg run）会让 bg run wrapper 的 finally 在旧 session close **之后**写脏（P11 C9 修）。这也更简明 —— 照现有设置类 handler 的同一模式做，不自创弱化路径。
- **复用 `_replace_session` 做内存热重建，不新开重建路径、不重启进程**。运行期换模型必经 `_cancel_and_drain_all_foreground()` + `server.py:745 _replace_session()`。**绝不**整进程重启、**绝不**手动逐茶客换 client。**Why**：`_replace_session` 已处理 router ws_sink 刷新、MTS 复位、旧 session 关闭；另起一套必漏边界。
- **P15 只改「房间默认」锚点，谁挂在锚点上谁跟着动**。`_replace_session` 调 `build_room_session` 时 fallback 每次重建重新解析：`scoring_spec = room_config.scoring_llm or room_default_spec` / `summary_spec = ... or scoring_spec` / `guest = gc.llm or room_default_spec`。所以改 env 默认后：**显式写了自己 LLM 段（`[scoring]` / `[summary]` / `[[guest]].llm`）的 section 钉住不变；没写、靠 fallback 的 section 自动跟新默认走**。这正是既有「fallback 走 section 级」不变量的行为，**不是 bug**：最常见 toC 场景（只配房间默认、scoring/summary/茶客全 fallback）→ desktop 切一次模型整房间一起换，正是期望；想把打分钉死在便宜模型的用户**必须显式写 `[scoring]`**，那是用户主动选择，P15 不替他做。
- **model 必须能拼成 `<provider>/<model>`，env 路径用裸 model + `LLM_PROVIDER`**。inbound 帧带显式 `provider` 字段 → handler `canonical_provider(provider)` 归一（`google`→`gemini`）→ `provider_env_prefix` 推前缀 → 写 `LLM_PROVIDER` + `<PREFIX>_MODEL`（裸 model）。`try_from_env` 据 `LLM_PROVIDER` 推前缀读回，**不能漏写 `LLM_PROVIDER`** 否则前缀错配。
- **provider URL 直接进 `<PREFIX>_BASE_URL`，无歧义**。desktop 的提供商 URL = `LLMSpec.base_url`。`build_client`（`llm_spec.py:319`）优先用它，缺才查内置 `_DEFAULT_BASE_URLS`。未知 provider 且 base_url 空 → `build_client` 抛 `SystemExit`，故 host 对非内置 provider 必带 base_url。
- **失败必提醒用户，但「装配失败」只覆盖能在装配期判出的错**。`_replace_session` 返 `False`（缺 key / 未知 provider 且缺 base_url / model 拼不成 `<provider>/<model>` 等）→ 保留旧内存 session + 恢复 env 旧值 + emit NOTICE error。延续既有「设置失败必提醒用户」的修复精神（commit bbf8d76）。**注意边界**：`build_client`（`llm_spec.py:335`）只校验 key **是否非空**，不联网验证 —— **「key 存在但是错的」装配期不暴露，要到首次 LLM 调用才报错**。P15 **不为验证 key 加网络 probe**（过度设计）；坏 key 的反馈走运行期对话报错，不进 P15 的回滚保证。

## 现状链路（P15 前）

```
进程启动一次:  load_env_files(session.py:46)  →  os.environ 灌入 .env（之后不回读磁盘）
build_room_session(session.py:374):
   _resolve_room_default_spec(300)  →  [room.llm] 优先 / 缺则 try_from_env()
       try_from_env: LLM_PROVIDER → prefix；<PREFIX>_MODEL（裸，缺→None）；<PREFIX>_BASE_URL
       两者都缺 → RoomConfigError「二选一」（server_entry.py:83 接住）
   build_client(llm_spec.py:319):
       api_key:  <PREFIX>_API_KEY（try_from_env 的 spec api_key_env=None → 走默认名）
                 ★ 有 spec 但缺 key 值 → raise SystemExit（llm_spec.py:336），不是返回 None
       base_url: spec.base_url  /  内置 _DEFAULT_BASE_URLS（未知 provider 且缺 → SystemExit）
   clients_by_spec 缓存：同 spec 复用一个 client（5 茶客+scoring+summary 常共用）
   TeaGuest(llm_client=...) 持有 client，生命周期绑定

★ server_entry.py:83 的 try/except 只接 RoomConfigError，不接 SystemExit
   → 有 model 无 key 时 build_client 的 SystemExit 直接逃逸、进程退出、server 起不来

运行期改 LLM（已存在！）:
   admin UI → update_room_llm / update_guest_llm → 改写 room.toml
   → _replace_session(server.py:745) → 重跑 build_room_session → 替换前台 session → 零重启即时生效
   （P15 不复用这条「改 toml」入口，只复用末段 _replace_session 热重建）

Electron → sidecar（sidecar.js:106）:
   spawn 时 env: {...process.env, CHAHUA_APP_ROOT, CHAHUA_USER_DATA, ...}
   shell export 的 LLM 凭证全透传 —— P15 在此追加 LLM_PROVIDER + <PREFIX>_{MODEL,BASE_URL,API_KEY}
```

**关键事实**：换模型 / 换 key **必然要重建 session**（client 在 `build_room_session` 时一次性构造并被 TeaGuest 持有，中途不重建）。但**「重建 session」≠「重启进程」** —— `_replace_session` 已实现进程内热重建。P15 要做的只是给它一个新触发入口 + env 注入点。

## P15 改造触点（提案）

| 层 | 文件:位置 | 改动 |
|---|---|---|
| 启动 env | `app/main/sidecar.js:106` | 登录态已就绪时，spawn 时把 `LLM_PROVIDER` + `<PREFIX>_{MODEL,BASE_URL,API_KEY}` 放进 child `env`（**MVP 第 1 条，不是可选项**） |
| 入站帧 | `chahua/server.py` `_INBOUND_ROUTES` + 新 handler | 新增 `set_llm_credentials` 帧；白名单严格 `{provider, model, base_url, api_key}`，**`provider`/`model`/`api_key` 必填非空，`base_url` 可缺或空串**；**未知键 NOTICE error 丢帧**（照任务 inbound `server.py:1268` 口径，敏感帧不静默吞误发字段） |
| handler 装配 | `chahua/server_inbound_settings.py`（`SettingsHandlers`，当前前台房专用） | **不 log raw payload** + **`api_key` 自写校验（不走 `require_str`，失败只 log 名/类型）** → 纯字符串校验 → 快照 env 旧值 → 写 `os.environ` → `_cancel_and_drain_all_foreground` + `_replace_session` → 成功重发 room snapshot / 失败恢复 env 旧值 |
| env 注入 | 新 handler 内 | `canonical_provider`/`provider_env_prefix`（复用 `llm_spec.py`）推前缀，写 `LLM_PROVIDER` + `<PREFIX>_{MODEL,API_KEY}`；**`base_url` 缺或空串 → 不设 / `del` `<PREFIX>_BASE_URL`，让 `_DEFAULT_BASE_URLS` 默认表接管**（未知 provider 且无 base_url 才在 `build_client` 报错） |
| env 回滚 | 新 handler 内 | 改 env 前快照将动 key 的旧值（含 sentinel「原本未设」）；`_replace_session` 返 `False` → 逐个恢复 / `del`（**无磁盘 IO**） |
| 下发 | 复用 room snapshot | 成功后 `_emit_room_snapshot(sink)` 重发 —— `room_info.room_default_llm` 已带 `model`/`api_key_env`/`api_key_ready`/`source`；**不新增专用 ack 帧**，**绝不**回 raw key |
| 前端 | `app/renderer/*` + `app/main/*` | 登录态拿到凭证后：spawn 前注入全套 env（启动）/ 运行中改模型发 `set_llm_credentials`（运行期）；选模型 UI 联动 |

## MVP 范围（收敛三条）

1. **spawn sidecar 时，全套 LLM env（`LLM_PROVIDER` + `<PREFIX>_{MODEL,BASE_URL,API_KEY}`）必须已在 child env 里**（`sidecar.js:106`）—— 保证启动期 `build_client` 读得到、不撞 `SystemExit`。**这不是软条件**：见下「启动 bootstrap 决策」—— 登录态必须先于 spawn 存在，二选一定死，不能写成「若已登录就注入」。
2. **新增当前前台房间专用 inbound：`set_llm_credentials { provider, model, base_url, api_key }`**（`provider`/`model`/`api_key` 必填，`base_url` 可缺/空），只改 `os.environ`，**不支持 `scope`、不写任何 toml**。
3. **handler**：不记录 raw payload（`api_key` 自写校验）→ 纯字符串校验 → 快照 env 旧值 → 写 `os.environ` → `await _cancel_and_drain_all_foreground()` + `_replace_session`；成功 → 重发 room snapshot（不新增 ack 帧）/ 失败 → 恢复 env 旧值 + 保留旧 session + NOTICE error。

**明确从 MVP 删除**（等真实冲突出现再加）：`scope` 字段 / 多房间同步 / desktop 托管标记 / 覆盖茶客显式模型 / **写 toml**（连带磁盘回滚、`_render_room_toml`、`api_key_env` 持久化全部不做）。注意：不写 toml ≠ 不影响 fallback 段 —— `[scoring]`/`[summary]`/茶客若靠 fallback，会随房间默认自动切，见承重不变量第 6 条。

## 集成方使用指南：运行期修改模型 / API key

host 软件在**运行过程中**改 chahua 的模型 / key，**只走一条路：通过已建立的 ws 连接（聊天用的同一条）发一帧 `set_llm_credentials`**。不碰 `.env`、不写 toml、不重启 sidecar、不重连。

```jsonc
// host → sidecar（上行帧）
{
  "type": "set_llm_credentials",
  "provider": "deepseek",                    // 必填；canonical 归一 + 决定 <PREFIX> 前缀 + base_url 兜底
  "model": "deepseek-chat",                  // 必填；裸 model（env 路径允许裸）；写进 DEEPSEEK_MODEL
  "base_url": "https://api.deepseek.com/v1", // 可缺/空串；给则写 DEEPSEEK_BASE_URL，空则删/不设 → _DEFAULT_BASE_URLS 接管
  "api_key": "sk-xxxxxxxx"                   // 必填；明文，只此一程进内存 → DEEPSEEK_API_KEY
}
```

server handler 内部（`server_inbound_settings.py` 新 handler）：① 不 log raw payload + 纯字符串校验（`provider`/`model`/`api_key` 非空、能拼 `<provider>/<model>`；`base_url` 可缺/空；失败 → 不动 env + NOTICE error）→ ② 快照 `LLM_PROVIDER` / `<PREFIX>_{MODEL,BASE_URL,API_KEY}` 旧值 → ③ 写这几个 `os.environ`（key 只入内存 environ，绝不落盘；`base_url` 空则不设/`del`）→ ④ `await _cancel_and_drain_all_foreground()` + `_replace_session` 进程内热重建（`build_room_session` → `try_from_env` 从刚写的 env 读到新凭证 → `build_client`）→ ⑤ 失败恢复 env 旧值 + 保留旧 session + NOTICE error → ⑥ 成功 **重发 room snapshot**（`_emit_room_snapshot(sink)`）—— `room_info.room_default_llm` 已含 `model` / `api_key_env` / `api_key_ready`（从 `os.environ` 实时探测，`server_room_snapshot.py:80`）/ `source`，**不新增专用 ack 下行帧**；重发 snapshot 同时刷新 scoring / summary / 茶客 fallback 的结果。**绝不回声 raw key**。**即时生效、零重启。** 改模型、改 key、两者一起改都是这同一帧（handler 整组重写这几个 env，只改模型也要把当前 key 一并带上）。

> UX 增强（非承重）：若成功后该前台房 `room_config.room_llm is not None`（显式 `[room.llm]`），可追发一条 NOTICE info「本房显式配置优先，desktop 注入只更新了 env 默认，本房未受影响」。它不影响凭证安全 / session 正确性，是体验提醒，**不围绕它扩不变量与测试矩阵**（见「边界」）。

**粒度对照**：

| 场景 | host 发什么 | 生效范围 |
|---|---|---|
| 换模型（同 provider） | `set_llm_credentials` 带新 `model` + 原 key | 房间默认（env）→ **所有靠 fallback 的 section**（未显式配模型的茶客 + 没写 `[scoring]`/`[summary]` 时连打分/摘要一起切）；显式钉了自己 LLM 段的不变 |
| 换 key / 换 provider | 带新 `provider` / `base_url` / `api_key` | 同上 |
| 改**某个茶客**的模型 | **不归 P15** —— 走既有 `update_guest_llm` admin 帧 | 单个 `[[guest]]` |

### ⚠️ 承重运维闭环：运行期改完凭证，host 必须自己持久化

这是集成方最易踩的坑。运行期 `set_llm_credentials` 只更新了**当前进程的 `os.environ`**，chahua **不写任何 toml**。于是：

- **本次进程**：新凭证在 `os.environ` 里，一切正常。
- **下次重启 sidecar**：`build_room_session` 又从 child env 读 `LLM_PROVIDER` + `<PREFIX>_{MODEL,BASE_URL,API_KEY}` —— 这些值由 **host 在 spawn 时注入**。若 host 没把「运行期换的新凭证」同步进自己持久化的登录态，下次 spawn 仍注入旧值（或没注入 → 全缺 `RoomConfigError` / 半缺 `SystemExit`）。

**责任切分（写死）**：chahua 这边**全程不持久化任何 LLM 配置**（不写 toml、不存值）；凭证与模型选择的「跨重启持久化」责任**完全在 host**。所以 host 运行期收到用户改凭证，要做**两件事**，缺一不可：① 发 `set_llm_credentials`（让当前进程即时生效）；② 更新 host 自己持久化的登录态（保证下次 spawn 时 child env 带同一套新值）。

```
启动:   host 持久化登录态 → spawn sidecar 时 child env 注入 LLM_PROVIDER + <PREFIX>_{MODEL,BASE_URL,API_KEY}
        → try_from_env 读到 → build_client 装配成功（全缺 RoomConfigError / 半缺 SystemExit）
运行期: 用户在 host 改模型/key
        → ① host 发 set_llm_credentials（ws）→ server 改 os.environ + _replace_session 热重建 → 即时生效
        → ② host 同步更新自己持久化的登录态（供下次 spawn）   ← 漏掉这步则重启回退
重启:   回到「启动」—— child env 带最新凭证
```

一句话：**运行期改 = 一帧 `set_llm_credentials` 热重建；但 host 必须把新凭证也存到自己那边，否则下次重启会回退。**

## 未来扩展（非 MVP，仅备忘）

- **多房间作用域**：是否提供「全局默认」让所有房间共享 desktop 凭证（P9 多 runtime 下后台房间同步策略）。纯 env 下 env 是进程级的，新切入的房间重建即吃新 env —— 但已在跑的后台 runtime 不变（见「边界」）。
- **provider 自动识别**：从 desktop provider URL / model_id 推断 provider，省掉帧里显式 `provider` 字段。
- **写回 toml 设置面板（可选增强）**：若产品后续要「desktop 注入的 model 回填进设置面板让用户可见可改」，再引入「写 `[room.llm]`」路径（届时承担磁盘回滚 + 启动死锁论述，见方案对照表第 3 行）。MVP 不做。

## 边界与已知交互

- **P9 多 runtime**：`_replace_session` 只替换前台 runtime 的 session。后台续跑房间 / 后台 MTS / bg run 的 session 不受影响（已构造的 TeaGuest 持旧 client）。改 `os.environ` 是进程级的，但已构造的 client 不回读 env —— 下次该房 session 重建才用新凭证，与「更新/删除 persona 不影响在跑房间」同口径。
- **in-flight + bg run / MTS**：运行期换模型前必 `_cancel_and_drain_all_foreground()`（不能在运行的 turn / bg run / dormant MTS 下重建 session），与既有 admin 改 LLM / `update_room_toml` 同语义。**不要用 `_cancel_and_drain_inflight`** —— 它不动 bg run，会让 bg wrapper 在旧 session close 后写脏（见承重不变量）。
- **显式 `[room.llm]` 完全遮蔽 env 注入（UX 增强，非承重）**：用户在设置面板手写 `[room.llm]`（走既有 `update_room_llm` admin 帧）→ `_resolve_room_default_spec` 先查 toml、env 完全忽略。即 desktop 注入是「房间默认兜底」，显式 toml 永远压它，无来回打架、不影响凭证安全与 session 正确性。**静默陷阱**：handler 仍会「成功」（env 确实写了、`_replace_session` 也重建了），用户却可能以为本房已切而实际没变。**建议（不做成承重）**：成功后若 `room_config.room_llm is not None`，追发一条 NOTICE **info**「本房显式 `[room.llm]` 优先，desktop 注入只更新了 env 默认，本房未受影响」。这比引入「desktop 托管标记 / 覆盖模式」简明得多 —— 但它纯属体验提醒，**不围绕它扩不变量与测试矩阵**；核心测试集中在不落盘 / 不泄 key / env 回滚 / snapshot 刷新。
- **CLI 形态**：P15 是 desktop 专属体验；CLI（`uv run chahua`）仍走 `.env` / shell export，本就是同一套 env 入口，天然兼容。
- **凭证安全**：raw key 只在「ws 帧 in 内存 → `os.environ`」+「Electron child env」流转，不落盘、不进 transcript / debug 取证 / envelope / **日志**。debug recorder、`message_artifacts` 等持久层天然不碰它；sidecar 日志由 handler「禁 log raw payload」承重不变量守住。

## 测试计划（复现优先：先写失败用例）

- `set_llm_credentials` 入站白名单严格：**未知键 → NOTICE error 丢帧**（不是 WARN 忽略）、缺必需字段（`provider`/`model`/`api_key`）→ NOTICE error；**`base_url` 缺或空串合法**（不报错，走默认表）。
- `base_url` 可选：给非空 → 写 `<PREFIX>_BASE_URL`；缺 / 空串 → 不设或 `del`，断言已知 provider（如 deepseek）仍能装配成功（`_DEFAULT_BASE_URLS` 接管）。
- 注入后 `os.environ` 含 `LLM_PROVIDER` + `<PREFIX>_{MODEL,BASE_URL,API_KEY}`；`_replace_session` 成功。
- **snapshot 刷新（核心）**：成功后重发 room snapshot，断言 `room_info.room_default_llm` 的 `model` / `provider` 为新值、`api_key_ready=true`（从 `os.environ` 实时探测）；**断言未新增专用 ack 下行帧**（只复用 room snapshot）。
- **绝不写盘**：注入前后 `room.toml` 字节完全不变（断言文件未被改），且全程不含 raw key。
- **env 回滚**：装配失败（缺 key / 坏 model 形态 / 未知 provider 且缺 base_url）→ handler 恢复 env 旧值（含「原本未设 → 注入后失败 → 被 `del` 回未设」）+ 保留旧内存 session + NOTICE error；断言失败后 `os.environ` 与注入前一致。**不测「坏 key 回滚」** —— `build_client` 不验 key 真伪，present-but-wrong 装配期必过、回滚不触发（坏 key 反馈走运行期对话）。
- （可选，非核心）显式 `[room.llm]` 遮蔽提醒：若实现了那条 info NOTICE，可加一条「有 `[room.llm]` → 成功后追发 info」的轻测；**MVP 不强制**，不为它扩矩阵。
- **日志不泄漏**：mock logger，断言 `set_llm_credentials` handler 任何 log 调用的格式化结果**不含 raw api_key 明文** —— 含「`api_key` 类型非法（传 int / null）触发校验失败」这一路（钉死它走自写校验、只 log 类型，不像 `require_str` 那样 `%r` 打值）。
- envelope / debug 落盘断言不含 raw key。
- 粒度（两面都测）：① 注入只改房间默认 env，**显式**配了 `[[guest]].llm` / `[scoring]` / `[summary]` 的 section 装配后仍持旧 spec（钉住）；② **没写**自己 LLM 段的 guest / scoring / summary，注入后重建 → 自动解析到**新** room 默认（fallback 跟随，断言新 spec 一致）。
- **启动依赖回归**：① 全缺（无 `[room.llm]` 且无 model env）→ `build_room_session` 抛 `RoomConfigError`（`server_entry.py:83` 能接，优雅）；② 有 model env 无 key env → `build_client` 抛 `SystemExit`（钉死「半缺即崩、host 必须全套原子注入」，反向证明「启动后重推」不可行）。

## CLAUDE.md / INVARIANTS 同步清单（落地后补）

- CLAUDE.md「配置与 LLM 装配」段：加 P15 desktop 注入不变量（raw key 只进 `os.environ` + child env、**全程不写 toml**、复用 `_replace_session`、粒度只动房间默认 env、启动期 env 硬依赖、失败回滚 env 旧值且无磁盘 IO）。
- `docs/INVARIANTS.md`：加 §P15 完整 rationale（含纯 env 为何优于写 toml、启动 `RoomConfigError`/`SystemExit` 二分、日志泄漏面）。
- `_INBOUND_ROUTES` / WebSocket 线协议段：登记 `set_llm_credentials` 帧（严格白名单、未知键 NOTICE error 丢帧）。
