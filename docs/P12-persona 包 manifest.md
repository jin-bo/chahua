# P12：persona 包 manifest —— 立载体，载体上字段按需逐步加

> persona 从「目录里散件约定」升级为带 `persona.toml` manifest 的安装单元。**本步只立 manifest 这个载体 + 解决当前两个真痛点的最小字段集**：花名册要的能力摘要 + `<Name>.toml.permission` 写了不生效。其它字段全部按真实问题出现的节奏后置。

## 路线图（说明为什么 P12 字段这么少）

| 步 | 字段加入 | 解决什么真痛点 |
|---|---|---|
| **P12（now）** | `schema_version` / `display_name` / `summary` / `[defaults.guest]` | ① `<Name>.toml.permission` 写了不生效；② 花名册要的能力摘要（替 LLM 自动摘要）|
| P12.1 | （无新字段，迁移内置 5 个 persona 到 dir-form + manifest）| 用第一手案例验证 schema 撑不撑得住，撑不住趁早 bump |
| P12.2 | `[requires].mcp_servers` / `skills` | 「装这个 persona 缺哪个 MCP / skill」装前校验 + 引导安装 |
| P12.3 | `[[example_tasks]]` | 装完一键起任务房 —— persona = 即开即用的 workflow |
| P12.4 | `version` / `authors` / `homepage` / `license` / registry index.json | 跨用户分发、`chahua persona search` |

每一步的 ship criteria：上一步在真实社区包里跑出 1-2 个具体痛点，再加字段。schema 自我演进跟着真实使用走，不跟着今天想象走。

**示例任务、版本号刻意推到 P12.3 / P12.4**：
- 示例任务依赖 P5 任务房间 propose-accept 稳定 + manifest 里到底写字符串模板还是引用本地 markdown 文件 —— 这个设计点不试一次定不下来。
- `version` 单独存在没意义，得跟 registry / update / persona-side migration 一起设计。今天写一个 version 字段，明天发现这些问题，要么静默改语义、要么 bump `schema_version`，都不优雅。

## Summary

现状（`chahua/admin_persona.py:42-68` / `chahua/persona_import.py`）：

- persona 包 = 目录约定。`<Name>/<Name>.md` 必，`<Name>.png` / `<Name>.toml` / `mcp.json` / `skills/` 全部可选 sidecar。
- 唯一被消费的 sidecar toml 字段是 `[guest].name`（picker 显示名）。`permission`、`sex` 写了不生效 —— 加入房间时 `DEFAULT_MODE` 覆盖（`chahua/config.py:595`）。
- 没有 `schema_version` 锚点；capability 自述只能靠 `persona_summary.py` 的 LLM 自动摘要。

P12 立的契约：

- **dir-form persona 可带 `persona.toml`**（顶层固定文件名），声明 `schema_version` / `display_name` / `summary` / `[defaults.guest]`。
- **严格白名单**（mirror `config.py` `_ALLOWED_*_KEYS` 套路），未知键 `PersonaManifestError`。
- **manifest 可选**。flat-form / 无 manifest 的 dir-form 走老路径继续工作，行为零回归。
- 顺手把当前「写了不生效」的 `[guest].permission` / `isolation` 通过 `[defaults.guest]` 接通。

**不动**：`<Name>.md` body 仍是 SOUL/system prompt 唯一来源；MCP 信任门 / persona_summary 缓存 / persona_import 整体 wiring 不变。

## 承重不变量

- **`persona.toml` 顶层 `schema_version` 必填且当前唯一合法值 = 1**。缺 / != 1 → `PersonaManifestError`。**Why**：第三方/社区包跨版本兼容的唯一锚点。未来加字段不 bump，破坏性变更才 bump。
- **严格白名单：未知键 → `PersonaManifestError`**。顶层 / `[defaults]` / `[defaults.guest]` 三级都校验。段本身全可选、段内字段也全可选，但出现未声明键即拒。与 `room.toml` 同口径。**Why**：typo 别静默吞。**不**预留「未来口袋」—— P12.2 真要加 `[requires]`，到时白名单加一行就行。
- **`[defaults.guest]` 字段子集严格 ⊂ `chahua.config._ALLOWED_GUEST_KEYS` 且**不**含 `name` / `persona` / LLM 四件套（`model` / `base_url` / `api_key_env` / `temperature`）**。**Why**：persona 包是可分发资产；带 LLM 配置等于把作者本地 env 变量名泄漏给装它的人。`name` / `persona` 由 picker 自动填，不在 manifest 里。
- **`[defaults.guest]` 只在「把 persona 加进房间」那一刻 inflate 进 `[[guest]]`；inflate 后房间 `room.toml` 与 manifest 解绑**。用户更新 manifest 后**不**会自动重 inflate；要让新默认值生效需用户从房间删除该茶客再重新加入。**Why**：persona 包是「模板」，房间 toml 是「实例」，模板更新不该回写到实例（npm `package.json` 同款语义）。
- **picker 三级 fallback：`persona.toml`.display_name → 老 `<Name>.toml`.`[guest].name` → 文件名 stem**。`persona.toml` 与 `<Name>.toml` 同时存在时取前者，不 WARN（迁移期共存常态）。**Why**：现有 5 个内置 persona + 已导入社区包零迁移成本。
- **permission / isolation 默认值合一只发生在 admin 层；frontend → inbound → admin 全链路用 `None` 表示「用户未显式选」**。frontend 不送 `permission` 字段（或送 `null`）、inbound 不再 `data.get("permission") or DEFAULT_MODE`、admin 在 manifest fallback 之后才 coalesce 到 `DEFAULT_MODE`。**Why**：现状（`modals.js:40,83` / `server_inbound_admin.py:367` / `admin_room.py:215` / `admin_guest.py:45`）四层都 eager 默认，manifest defaults 永远拿不到机会 —— 这是 P12 想解决的「写了不生效」核心 bug 在新链路上的等价物。
- **消费路径（`add_guest` / `create_room` / `persona_import`）遇坏 manifest 必须 raise + 转 NOTICE / 拒导；不静默 fallback**。唯一例外是 `discover_personas`（可见性优先 → WARN + stem 兜底）。**Why**：用户 typo 了 `permisson`，如果 discover 容错+消费容错两次，他得到的还是 `DEFAULT_MODE` —— 跟 P12 之前的「写了不生效」完全等价。容错只在「让 persona 不至于消失在 picker 里」这一档；点了添加就必须真合规。

## 文件格式（`persona.toml`）

```toml
schema_version = 1                 # 必填，当前唯一合法值

display_name = "伊冯"               # 可选；缺则用目录名（FS 主键即 persona 显示名）
summary = "一句话能力描述，进花名册 + inflate 到 [[guest]].summary"   # 可选

[defaults.guest]                   # 可选段，加入房间时 inflate 到 [[guest]]
permission = "workspace-write"     # 可选；必须 ∈ chahua.permissions.VALID_MODES
isolation = "room"                 # 可选；必须 ∈ {"room", "global"}
```

4 个字段、2 个可选段。无 `name` —— 目录名是 FS 主键；无 `version` / `authors` —— 留 P12.4；无 `[capabilities]` —— 作者把 keyword / 示例直接写在 `summary` markdown 里。

## 模块改动

### 新增 `chahua/persona_manifest.py`（~120 行）

**单一职责**：从 `persona_dir/persona.toml` path → `PersonaManifest` frozen dataclass。不读 md / 不读头像 / 不扫目录。

```python
class PersonaManifestError(ValueError): ...

@dataclass(frozen=True)
class PersonaManifest:
    schema_version: int                  # 当前仅 == 1
    display_name: Optional[str]
    summary: Optional[str]
    defaults_guest_permission: Optional[str]   # 已校验在 VALID_MODES
    defaults_guest_isolation: Optional[str]    # 已校验

def load_persona_manifest(persona_dir: Path) -> Optional[PersonaManifest]:
    """读 persona_dir/persona.toml。文件不在 → None；解析失败 / 字段非法 → PersonaManifestError。

    **容错策略**：discover_personas 是唯一允许 try/except + WARN + None 的调用方
    （可见性优先）。add_guest / create_room / persona_import 等消费路径**必须**让异常
    原样冒出 —— 由 inbound handler 转 NOTICE 给用户（承重不变量第 7 条）。
    """

def parse_persona_manifest_bytes(blob: bytes) -> PersonaManifest:
    """从已读到内存的 toml bytes 校验 + 构造 PersonaManifest。

    persona_import 路径用 —— 在 _write_files 落盘前对采集到的 files 列表里的
    persona.toml 字节做 dry-run 校验，避免坏 manifest 留下半成品目录（下次重导会撞
    "目录已存在"）。失败 → PersonaManifestError。

    内部实现：tomllib.loads(blob.decode("utf-8")) → 共享 load_persona_manifest 的校验
    路径（同一私有 _parse_dict helper），保证 file-based 与 bytes-based 入口语义完全
    一致。注：tomllib.loads 只接受 str；TOML 规范固定 UTF-8 编码，显式 decode 不引入
    歧义。也可用 tomllib.load(io.BytesIO(blob))，多一个 import 不划算。

    **所有失败统一包装成 PersonaManifestError**，包括 blob 不是合法 UTF-8 时的
    UnicodeDecodeError（用 try/except UnicodeDecodeError as e: raise
    PersonaManifestError("persona.toml 非 UTF-8 编码") from e 包一层）。这样 persona_import
    单 catch PersonaManifestError 就覆盖所有 manifest 解析失败路径。
    """
```

字段拍平，不嵌套 `CapabilityBlock` / `DefaultsGuestBlock`。

白名单常量（mirror `config.py:69-90`）：

```python
_ALLOWED_TOP_KEYS = frozenset({"schema_version", "display_name", "summary", "defaults"})
_ALLOWED_DEFAULTS_KEYS = frozenset({"guest"})
_ALLOWED_DEFAULTS_GUEST_KEYS = frozenset({"permission", "isolation"})
```

### 改 `chahua/admin_persona.py`

新增 helper（仅 dir-form 路径调用，flat-form 跳过）：

```python
def _read_persona_manifest_or_legacy(md_path: Path) -> tuple[Optional[str], Optional[str]]:
    """返回 (display_name, summary)。manifest 优先；缺时退到老 <stem>.toml 的 [guest].name；都缺返 (None, None)。"""
```

`discover_personas` 返回新增一个字段：

```python
{
    "persona": "chahua/personas/Yvonne/Yvonne.md",
    "name": "伊冯",                      # display_name 优先；既有语义
    "avatar_data_uri": "data:image/png;...",
    "summary": "一句话能力描述",          # 新增；缺 → None
}
```

老前端忽略未知键 `summary` 不爆。

### 改 `app/renderer/persona.js`（picker 渲 summary）

`renderPersonaPicker` 在 `.persona-name` 之后追加一行 `.persona-summary`，文本来自 `p.summary`，缺省 / 空字符串 → 不渲染节点。CSS 加一条对应样式（小一号字号 / 副色），与 `.persona-hint` 视觉接近但语义不同。

### 改 `app/renderer/modals.js`（停止 eager 默认 permission）

`modals.js:36-44`（ADD_GUEST 路径）+ `modals.js:80-84`（CREATE_ROOM 路径）当前都硬塞 `permission: DEFAULT_PERMISSION`。改成**不送** `permission` 字段（保持 inbound 白名单 `permission` 可选 `null`）。**Why**：让 manifest defaults 有机会生效，承重不变量第 6 条。

### 改 `chahua/server_inbound_admin.py`（inbound 保留 None + 坏 manifest 转 NOTICE）

**两件事**：

**①** ADD_GUEST inbound（`server_inbound_admin.py:367`）当前 `permission = data.get("permission") or DEFAULT_MODE`。改成：

```python
permission = data.get("permission")          # None 表示「未显式选」
if permission is not None and not isinstance(permission, str):
    _log.warning(...); return
# 不在这里 coalesce —— 留给 admin 层做 manifest fallback
self._add_guest(persona=persona, name=name, permission=permission, sink=sink)
```

同时 `_add_guest` 透传 `permission: Optional[str]` 到 `admin_guest.add_guest`。`create_room` 入站同口径（guests 数组每项 `permission` 字段不再 `or DEFAULT_MODE`）。

**②** `_add_guest` / `_create_room` 当前是 `except Exception: _log.exception + _emit_room_snapshot`（`server_inbound_admin.py:72-77,283-289`），用户看不到 manifest 错原因。改成在 catch-all 之前显式拦 `PersonaManifestError`（这两条路径不会遇到 `PersonaImportError`，那是 persona 导入 inbound 自己的事）：

```python
try:
    admin.add_guest(paths=..., room_dir=..., persona=..., name=..., permission=...)
except PersonaManifestError as e:
    _log.warning("add_guest: %s", e)
    self.server._emit_notice(sink, level=NOTICE_LEVEL_ERROR, text=str(e))
    self.server._emit_room_snapshot(sink)
    return
except Exception:
    _log.exception("add_guest: persona=%r name=%r 失败", persona, name)
    self.server._emit_room_snapshot(sink)
    return
```

复用现有 `self.server._emit_notice(...)` API（`chahua/server.py:778`）。`_create_room` 同口径。前端 NOTICE error 已有展示通路，无需新 UI。

### 改 `chahua/admin_guest.py`（加房间时 inflate `[defaults.guest]` + 顶层 summary）

`add_guest` 签名最小修改：仅把 `permission: str = DEFAULT_MODE` 改为 `Optional[str] = None`，**不**新增 `isolation` / `summary` 入参。P12 阶段没 UI / 协议路径暴露 isolation / summary 显式覆盖；提前扩 API 只会增加面、又得加 None 透传链路。这两个字段**只**从 manifest inflate，inflate 完直接写 snapshot。等真有 UI 覆盖时再加 `Optional[str] = None` 入参（API 扩展兼容）。

```python
def add_guest(
    *,
    paths: Paths,
    room_dir: Path,
    persona: str,
    name: Optional[str] = None,
    permission: Optional[str] = None,    # None = manifest 默认 or DEFAULT_MODE
) -> RoomConfig
```

逻辑：

1. **persona md 走双根 resolve**：`md_abs = paths.find_in_data_then_app(persona)`（`chahua/_paths.py:84`）。**不**用 `Path(persona).parent` —— 会漏 user_data → app 双根语义。
2. **`md_abs is None` 分支**：persona 文件根本不在 —— 跳过 manifest 解析（manifest = None），按现有路径继续写 snapshot，由后续 `load_room_config` 抛 `RoomConfigError`（"persona 不存在"）。**不**在这里报错 —— "persona 文件不存在" 是 `load_room_config` 的语义，本函数不抢。
3. `md_abs` 非 None → `persona_dir = md_abs.parent`；调 `load_persona_manifest(persona_dir)`。`PersonaManifestError` **向上抛**（不容错，inbound 转 NOTICE）。`persona.toml` 文件不在 → manifest = None。
4. 字段 coalesce —— **判定一律用 `is not None`，禁止 `or`**（`""` / `0` 之类显式坏值应在校验层失败，不被 `or` 吞掉；这是承重不变量第 6 条的延伸）：
   - `permission`：`permission if permission is not None else (manifest.defaults_guest_permission if manifest else None)`，再 `if None else DEFAULT_MODE`。三级。
   - `isolation`：`manifest.defaults_guest_isolation if manifest else None`；None → 不写（既有默认在 `RoomConfig` 里）。两级，纯内部。
   - `summary`：`manifest.summary if manifest else None`；None → 不写（既有 LLM 缓存兜底）。两级，纯内部。
5. 写 snapshot dict；`admin_toml.py` renderer 已支持 `summary` / `isolation` 字段，无需改。

**inflate 范围严格 == manifest 字段范围**：`permission` / `isolation` / 顶层 `summary` 三件，都是 manifest 里用户可消费的全部字段。命名上 `summary` 在 manifest 顶层而不在 `[defaults.guest]` 内 —— picker 也要读 summary（不该钻进 `[defaults.guest]` 才能写），但 inflate 行为一视同仁。

### 改 `chahua/admin_room.py`（`create_room` 同口径 inflate）

`admin_room.py:209-216` 当前 normalize 循环用 `g.get("permission") or DEFAULT_MODE`。改成对每位 guest：

1. **persona 走双根 resolve**：`md_abs = paths.find_in_data_then_app(g["persona"])`。
2. **`md_abs is None`** → 跳过 manifest（不读），按 add_guest 同口径 coalesce 写 `normalized_guests`（即 `permission if g.get("permission") is not None else DEFAULT_MODE`，**不**用 `or DEFAULT_MODE`）—— "persona 不存在" 由后续 `load_room_config` 报。**Why**：`or` 会把 `""` 这类显式坏值静默吞成默认值，正是 P12 要消灭的 eager-default 反模式（承重不变量第 6 条）。
3. `md_abs` 非 None → `persona_dir = md_abs.parent`；调 `load_persona_manifest`。`PersonaManifestError` **向上抛**。
4. coalesce 与 add_guest 完全同口径：permission 三级（全用 `is not None` 判定）、isolation / summary 两级（仅 manifest）。
5. 写入 `normalized_guests`。

**事务性**：所有 guest 的 manifest 必须先全部解析成功，才进入 `mkdir(parents=True, exist_ok=False)`。任何一个 manifest 坏 → 抛 → 房间目录不被创建（用户能改完 manifest 后重试，没有半成品房间）。

`create_room` 与 `add_guest` 是 inflate 的两条同口径调用点 —— 不抽公共 helper（两处上下文不同，强行抽容易把房间级 / 茶客级耦合），但**字段 coalesce 顺序必须一致**。

### `chahua/persona_import.py` 调整

`import_from_folder` / `import_from_github` 不要求源仓库有 `persona.toml`，但**发现**有就走严格校验。`<Name>.toml` 继续支持，**不**自动转换为 `persona.toml`。

**校验时机：必须在 `_write_files` 之前**。现有流程是 `_collect_files` → `_validate_target` → `_write_files(target_dir, files)`（`persona_import.py:81-84,103`），`_write_files` 内 `mkdir(exist_ok=False)` 一旦 target 占用即抛 "目录已存在"（`persona_import.py:387-390`）。如果先落盘再校验：① 坏 manifest 留下半成品 target_dir；② 用户改完源仓库重导，撞 "已存在" 错；③ 与 `_write_files` 失败 `rmtree` 回滚不对称（rmtree 只在写入失败时跑）。

实现：在 `_write_files` 前插一步 in-memory 校验：

```python
# 已在内存的 files: list[tuple[str, bytes]]
for rel_path, data in files:
    if rel_path == "persona.toml":
        try:
            parse_persona_manifest_bytes(data)
        except PersonaManifestError as e:
            raise PersonaImportError(f"persona.toml 不合法：{e}") from e
        break
# 校验通过后才 _write_files
```

无 `persona.toml` 入口 → 跳过校验，老路径不变。

## 测试

新增 `tests/test_persona_manifest.py`：

- 合法 manifest 完整解析、字段类型对齐。
- `schema_version` 缺 / != 1 → `PersonaManifestError`。
- 未知顶层键 / 段内键 → `PersonaManifestError`，错误消息含键名。
- `[defaults.guest].permission` 非 `VALID_MODES` 值 → `PersonaManifestError`。
- `[defaults.guest].isolation` 非 `{"room", "global"}` → `PersonaManifestError`。
- 坏 toml（无效语法）→ `PersonaManifestError`。
- `display_name` / `summary` / `[defaults.guest]` 缺 → 字段 None，不抛。
- `parse_persona_manifest_bytes` 合法 UTF-8 bytes → 与 `load_persona_manifest` 同一字段集。
- `parse_persona_manifest_bytes(b"\xff")` → `PersonaManifestError`（非 `UnicodeDecodeError`，锁住「所有失败统一包装」契约）。

`tests/test_admin_persona_manifest_integration.py`：

- `discover_personas` 同时存在 `persona.toml` + 老 `<Name>.toml` 时，display name 取 manifest，`summary` 字段出现。
- 无 manifest 的 dir-form / flat-form persona discover 行为零变化。
- manifest 坏 toml → persona 仍在 picker 里可见（走 stem 兜底），WARN 一次。

`tests/test_admin_guest_inflate.py`：

- manifest `[defaults.guest].permission = "workspace-write"` + 入参 `permission=None` → `[[guest]].permission == "workspace-write"`。
- manifest 同上 + 入参 `permission="ask"`（picker 显式覆盖）→ `[[guest]].permission == "ask"`。
- 无 manifest + 入参 `permission=None` → 回归 `DEFAULT_MODE`。
- manifest `[defaults.guest].isolation = "global"` → 写入 `[[guest]].isolation`（manifest-only 路径，无入参覆盖测试）。
- manifest `summary = "..."` → 写入 `[[guest]].summary`（manifest-only，落进 `resolve_guest_summary` 「手写」档）。
- **坏 manifest（schema_version 错 / 未知键 / permission 非法）→ `add_guest` 抛 `PersonaManifestError`，不静默 fallback**。
- persona md 走双根 resolve —— 同名 md 在 user_data + app 都存在时取 user_data，对应 manifest 也从 user_data 读。

`tests/test_admin_room_inflate.py`（新增）：

- `create_room` 多位 guest，部分持 manifest、部分不持 —— 各自字段独立 inflate。
- 入参 guest dict `{"permission": null}` + manifest 有 default → 用 manifest。
- 入参 guest dict `{"permission": "ask"}` + manifest 有 default → 入参胜。
- 坏 manifest → `create_room` 抛 `PersonaManifestError`；**房间目录不被创建**（事务性 —— `mkdir` 之前先解析所有 guest 的 manifest）。
- 多 guest 时第二位 manifest 坏 → 第一位的处理也不留半成品（同上事务性）。

`tests/test_inbound_admin_permission_none.py`（新增，~50 行）：

- ADD_GUEST inbound 未带 `permission` 键 / 带 `null` → admin 层收到 `None`（mock `_add_guest` 验入参）。
- ADD_GUEST inbound 带 `permission="ask"` → 透传 `"ask"`。
- ADD_GUEST inbound 带 `permission=123`（非 str）→ WARN + ignore。
- CREATE_ROOM 同口径。

`tests/test_inbound_admin_manifest_notice.py`（新增，~40 行）：

- 坏 manifest 经 ADD_GUEST inbound 触发 → 验 sink 收到 `notice` envelope（level=error）+ 文本含 manifest 错原因 + `room_info` snapshot 也补发（UI 复位）。
- CREATE_ROOM 同口径。

`tests/test_persona_import_manifest.py`（新增）：

- 源仓库有合法 `persona.toml` → 导入成功，文件落 user_data。
- 源仓库有坏 `persona.toml`（schema_version 错）→ `PersonaImportError`；**target_dir 必须不存在**（验 in-memory pre-validation 生效，未走到 `_write_files`）；用户改完源仓库重导能成功（不撞 "目录已存在"）。
- 源仓库无 `persona.toml` → 导入成功，行为零变化。

picker UI 改动（`persona.js`）走**手工验收**，无 e2e 自动化。

## 迁移

| 现状 | P12 后行为 |
|---|---|
| flat-form built-in `chahua/personas/范总.md` | 无 manifest 入口；picker 走 stem 兜底；行为零回归。P12.1 批量改 dir-form + manifest。|
| dir-form 社区包持 `<Name>.toml` | 既有 `[guest].name` picker 仍生效；自愿迁移到 `persona.toml`（30 秒手写）。|
| dir-form 包持 `persona.toml` | 走新解析；display / summary / defaults 全用 manifest。|
| `<Name>.toml` 与 `persona.toml` 同时存在 | 取 `persona.toml`，`<Name>.toml` 静默忽略。|

## 非目标（明确不做）

按路线图分流，本步**不**做：

- 任何「未来口袋」字段预留（`[requires]` / `[[example_tasks]]` / `[trust]` / `version` / `authors` / `homepage` / `license`）—— 真到 P12.2~P12.4 那天加。
- `[capabilities]` 结构化字段 —— 作者写在 `summary` markdown 里就够。
- `[defaults.guest].summary` 间接路径 —— 顶层 `summary` 单一来源。
- `chahua persona migrate <dir>` CLI —— 自愿手写迁移成本可接受。
- manifest 解析 `@lru_cache` 预优化 —— 无性能数据支持。
- 内置 personas flat → dir-form 迁移 —— P12.1。

## 风险与缓解

- **作者写老 `<Name>.toml [guest].permission` 期望生效** —— manifest 改造文档明示「`<Name>.toml` 只读 `[guest].name`，要 pin permission 写 `persona.toml`」。
- **`[defaults.guest].permission` 让内置 persona 拿到比当前更大权限** —— 不影响：built-ins 是 flat-form 无 manifest 入口。P12.1 迁移时显式审计每个 built-in 的 permission。
- **manifest 更新不重 inflate 让作者困惑** —— 不变量第 4 条已显式声明；后续 picker UI 在 manifest summary 旁加一行小字「修改对已加入的茶客不生效，需重新加入」。

## 验收

- `examples/personas/Yvonne/persona.toml` 写出来。
- 通过 ADD_GUEST 路径加入示例房间：picker 显示「伊冯」+ summary 在名字下显示一行 + 加入后 `[[guest]].permission == "workspace-write"`。
- 通过 CREATE_ROOM 路径新建房间选中 Yvonne：同样 inflate 生效。
- `examples/personas/Maya/Maya.toml` 不动 —— 回归测试，picker 显示名走老路径、permission 走 `DEFAULT_MODE`。
- 故意把 Yvonne 的 manifest 改坏（`permisson = "..."` typo）：picker 仍可见（discover 容错）；点添加 → 前端收到 NOTICE 描述坏字段，茶客**不**被添加。
- 全量 `uv run pytest` 通过；新增 manifest 测覆盖关键场景（合法解析 / 各类校验拒 / discover 集成 / add_guest inflate / create_room inflate / inbound None 透传 / 坏 manifest 消费时 raise）。
- CLAUDE.md「关键不变量」段加「persona manifest」子段，本 plan 中 7 条不变量同步过去。
