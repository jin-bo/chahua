"""茶话室前端事件 envelope（设计文档 §3.5）。

所有推到前端（CLI / P2.3 WebSocket / P3 Electron）的事件都套同一 envelope：

    {schema_version, room_id, message_id, guest_name, turn_id, seq, type, status, ts, data}

agentao 的原生 :class:`agentao.transport.AgentEvent` **不带** 这些字段（它只知道自己
那个 Agentao 实例在跑一轮），所以 envelope 是茶话室在外层合成的。事件类型的"消息"
与"轮次"边界也是茶话室定义的，与 agentao 的 ``TURN_BEGIN/END`` 不一一对应（agentao
的轮次是 LLM-iteration 级，§3.5 明确不向前端转发）。

**type 与发生位置**：

- ``room_info``：``server.py`` 在 ws 连上时一次性下发，给前端装 sidebar / @ 补全；
  不属于任何 turn（``turn_id`` 为 ``None``）。换房时（``switch_room`` inbound）
  会重发一次。``data`` 含 ``rooms_available``（其它可切房间列表）+ ``current_room_id``。
- ``room_history``：``server.py`` 在 ``room_info`` 之后下发，回放 transcript.jsonl，
  前端用来在进 Room 时显示历史对话；同样不属任何 turn（``turn_id`` 为 ``None``）。
  换房时也重发。
- ``turn_start`` / ``turn_end``：orchestrator 合成；turn 对应一次 pick（top-1~2 抢话）。
- ``message_start`` / ``message_end``：``TeaGuest.speak()`` 的 ``try / except / finally``
  外层合成 —— 保证 start 必有 end（status 三选一）。
- ``message_delta`` / ``guest_thinking`` / ``tool_start`` / ``tool_complete``：
  ``ChahuaTransport`` 从对应 agentao 事件转译。

**turn_id 可空**：从 P3.2.2 起，连接级事件（``room_info`` 等）``turn_id=None``；
其余事件路径不变，仍由 orchestrator/transport 在 bind 时塞具体值。

**room_id**：P2.2 没有稳定 room id（room.toml 没字段），envelope 里塞 ``room.name``。
P4 加 ``[room].id`` 后换稳定 ID（display name 可变；envelope 路由不该依赖）。
P9 起前端据 envelope ``room_id`` 分流前台 / 后台房间里程碑 —— 在 ``[room].id`` 落地前，
``admin`` 层强制 ``[room].name`` 跨房唯一（建房 / raw 编辑 toml 两入口），让 ``room.name``
当下可安全充当路由 id。

**id mint**：:func:`new_message_id` 与 :func:`new_turn_id` 同根，由茶话室所有 ID 来源
集中在本模块；``room.py`` 也走这个 helper（不再各自 ``secrets.token_hex``）。
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Optional

_log = logging.getLogger(__name__)


# Envelope wire 版本。前端反序列化时若 != 期望值应回退到兼容模式或拒绝（P2.3 起生效）。
SCHEMA_VERSION = 1


class ChahuaEventType(str, Enum):
    """前端事件类型。值是 JSON-friendly 字符串，envelope 落盘 / 上线时直接用 ``.value``。"""

    ROOM_INFO = "room_info"
    ROOM_HISTORY = "room_history"
    TURN_START = "turn_start"
    MESSAGE_START = "message_start"
    MESSAGE_DELTA = "message_delta"
    MESSAGE_END = "message_end"
    TURN_END = "turn_end"
    GUEST_THINKING = "guest_thinking"
    TOOL_START = "tool_start"
    TOOL_COMPLETE = "tool_complete"
    # 服务端 → 前端的一次性短消息（含 level=info/error 与 text）。给 persona 导入这类
    # mutator 用 —— 失败原因要让用户看见，成功也想报 sidecar 文件数。不挂房间 turn，
    # turn_id / message_id / guest_name 都为 None。
    NOTICE = "notice"
    # 服务端确认收到一个文件上传后回吐 —— data 含 `rel`（落盘相对路径，前端
    # 拼下一条 user_message.files）+ `name`（落盘文件名，去过非法字符）+
    # `size`（字节）+ `original`（用户原始文件名，可能含已被洗掉的字符）。
    # 前端拿到后挂入 pendingFiles pill；不挂 turn。
    FILE_UPLOADED = "file_uploaded"
    # 服务端把整个房间 transcript 拼成 markdown 回吐 —— data 含 `filename`（建议的下载
    # 文件名）+ `markdown`（utf-8 字符串）。前端走 Blob + a.download 触发浏览器下载，
    # **不写服务器盘**（导出物只活在用户机器上）。
    ROOM_EXPORT = "room_export"
    # 服务端按前端 ``download_file`` inbound 请求把文件读出后回吐。data 形态：
    # 成功 ``{rel, name, size, content_b64}`` / 失败 ``{rel, error}``。前端走
    # Blob + a.download 触发浏览器下载，不写本地盘。仅接受房间内 ``share/`` 或
    # ``tasks/<id>/artifacts/`` 前缀的 rel —— 其它路径直接拒，防止穿越读盘。
    FILE_DOWNLOAD = "file_download"
    # P18：当前房历史搜索的一次性响应。data 形态 ``{query, results, truncated}``——
    # ``results`` 是 ``[{message_id, seq, speaker_id, ts_ms, snippet}]``（只带 speaker_id、
    # 不解析 display_name，前端按名册映射，与 room_history 同口径）；``truncated`` 表示命中
    # 数超过 limit 被截断。read-only 帧，不挂房间 turn，turn_id / message_id / guest_name 都为 None。
    SEARCH_RESULTS = "search_results"
    # P5.1 任务房间。事件分工（docs/P5-任务房间.md §4.2）：``TASK_INFO`` 是**权威快照**
    # （ws 连上 / 切房 / active 变化 / 任意 task 状态变更后重发整份），其它四个是 **hint**
    # —— 给前端做 toast / 动画 / 高亮增量项，前端任务状态以最近一次 ``TASK_INFO`` 为准。
    # 全部不挂房间 turn，turn_id / message_id / guest_name 都为 None。
    TASK_INFO = "task_info"
    TASK_OPEN = "task_open"
    TASK_UPDATE = "task_update"
    TASK_DECISION_ADDED = "task_decision_added"
    TASK_ARTIFACT_ADDED = "task_artifact_added"
    # P5.2.4：任务关闭（done / abandoned）—— hint 事件，前端用做局部反馈
    # （toast / 卡片样式翻灰），权威状态仍以紧跟其后的 TASK_INFO 为准。
    TASK_CLOSE = "task_close"
    # P5.8：/clear task 清空产物后的 hint。其它 task hint 自带 diff 信息，清空动作
    # 只有 TASK_INFO 全量快照 —— 单独加这个轻量 hint 让前端不必从快照猜 diff。
    # data 形状：``{task_id, title, count, names}``。前端据此渲染系统气泡。
    TASK_ARTIFACTS_CLEARED = "task_artifacts_cleared"
    # P5.3.3：茶客通过 task_propose_* 工具发出的"提议"（开任务 / 记决策）。data 形状：
    # ``{task_id?, proposer, kind: "decision"|"open", payload: {...}}``。**纯 hint** ——
    # 写权限永远在用户（docs §8 不变量 #3）：前端渲染成"采纳 / 忽略"卡片，采纳后薄包装
    # 转译回既有 ADD_DECISION / OPEN_TASK inbound，server handler 零改动。
    # 工具自己经 ``transport.emit_chahua`` 推（speak() bind 期内 sink 有效）；不走 SDK
    # AgentEvent 回调链，不动 transport_bridge._handle。
    TASK_PROPOSAL = "task_proposal"
    # P6.3.A：调试抽屉点击某条历史索引行后，服务端按 turn_id 查 ``debug/turns.jsonl``
    # 行 + 关联 prompt 文件后回吐。data 形状：``found=true`` 路径
    # ``{found, turn, prompts: {<rel>: text}}``（``prompts`` 字段始终存在，最少 ``{}``；
    # 单 key 严格 ``enabled=True && capture_prompts=True && 文件可读`` 三重满足才出现）；
    # ``found=false`` 路径 ``{found: false}``（被 rotation 清 / 协议过期）。envelope
    # 顶层 ``turn_id`` 与实时 envelope 同款；其它顶层字段 turn_id 之外为 None。
    TURN_DETAIL = "turn_detail"
    # P7.1 显式 handoff / delegation（docs/P7-显式 handoff 与 delegation.md §3.3）。
    # 三者都不挂房间 turn 顶层 ``turn_id`` —— 除了 ``HANDOFF_CONSUMED``：它由
    # orchestrator 在 drain loop 内 ``_emit_turn(TURN_START)`` 之后 emit，envelope
    # 顶层 ``turn_id`` 与本轮 turn_start / turn_end 同值（关联取证用）；
    # ``HANDOFF_ENQUEUED`` / ``HANDOFF_CLEARED`` 由 server inbound handler emit，
    # 顶层 ``turn_id`` 为 None（队列层瞬态、不属任一 turn）。
    # data 形状：
    #   - HANDOFF_ENQUEUED ``{queue: [item.to_dict(), ...]}`` 权威快照，前端
    #     ``handoffQueueState`` 整体替换；
    #   - HANDOFF_CONSUMED ``{item: <HandoffItem.to_dict()>}`` 当前消费的项；
    #   - HANDOFF_CLEARED  ``{items_dropped: [item.to_dict(), ...]}``。
    # 不进 TASK_INFO（队列是调度层瞬态，刷新即清）；schema_version 不 bump。
    HANDOFF_ENQUEUED = "handoff_enqueued"
    HANDOFF_CONSUMED = "handoff_consumed"
    HANDOFF_CLEARED = "handoff_cleared"
    # ``/tools`` / ``/skills`` slash 查询的回包（只读 introspection）。data 形状：
    # ``{guest, permission, tools: [...], skills: [...], view}`` —— 茶客 agent 注册
    # 的 tools + 可用 skills + 当前权限模式快照。``view`` 是请求方传来的展示回声
    # （``"tools"`` / ``"skills"``），前端按它裁剪显示哪段，多查询并发时不串台。
    # 一次性查询、不进 transcript、不落盘、不挂房间 turn；schema_version 不 bump。
    GUEST_CAPS_INFO = "guest_caps_info"
    # P8.3 托管任务会话（MTS，docs/P8.3-原生自动推进.md §3.4）。三者都是 **hint 型**
    # 事件——前端渲状态条 / 系统气泡，不进 transcript、不触发 AI（同 handoff_* /
    # task_* 口径）。都不挂房间 turn（turn_id / message_id / guest_name 全 None）。
    # data 形状：
    #   - MANAGED_SESSION_STARTED  ``{task_id, manager_guest, budget}`` MTS 建立后；
    #   - MANAGED_SESSION_ADVANCED ``{manager_guest, remaining_budget}`` 每次
    #     「worker→管理者」回调入队后（UI 倒计时）；
    #   - MANAGED_SESSION_ENDED    ``{reason}`` MTS 结束时恰好一次（**P8.4 起 5 reason**：
    #     budget_exhausted / task_closed / cap_reached / user_stopped / user_cancel；
    #     P8.3 的 ``manager_finished`` 已退役，管理者没派活 → MTS 进入 dormant）。
    # 新增的是事件类型——旧前端遇未知 type WARN 后忽略（向前兼容），不 bump schema_version。
    MANAGED_SESSION_STARTED = "managed_session_started"
    MANAGED_SESSION_ADVANCED = "managed_session_advanced"
    MANAGED_SESSION_ENDED = "managed_session_ended"

    # P9 切房后房间后台续跑（docs/P9-切房后房间后台续跑.md §4 / §5）。hint 型里程碑：
    # 切走后仍 busy 的房间转后台续跑，其 turn / handoff drain 跑完、runtime 自毁时
    # emit 一次。不进 transcript / 不触发 AI。阶段 9.2 后台 router 是 NOOP,本事件被
    # 丢弃（靠切回快照）；阶段 9.3.1 后台白名单放行后才真送达前端更新房间列表徽标。
    # 新增事件类型——旧前端遇未知 type WARN 后忽略（向前兼容），不 bump schema_version。
    ROOM_BACKGROUND_FINISHED = "room_background_finished"

    # P11 后台 Agent run（docs/P11-后台 Agent.md §「envelope」）。4 个 hint 型事件 ——
    # 不进 transcript、不触发 AI；旧前端遇未知 type WARN 后忽略，不 bump schema_version。
    # 都不挂房间 turn（turn_id / message_id / guest_name 全 None；message_end(ok) 才挂
    # 茶客发言气泡——那一帧的 ``data.run_id`` 是关联依据，不是这里的 4 个事件）。
    # data 形状：
    #   - AGENT_RUN_STARTED   ``{run_id, guest_name, task_id?, issued_by,
    #                            source_guest?, instruction_preview?}`` —— inbound /
    #                            工具校验通过后立即 emit；
    #   - AGENT_RUN_FINISHED  同上字段（已写气泡，run_id 给前端关联）；
    #   - AGENT_RUN_CANCELLED 同上字段（用户 / clear_room 取消）；
    #   - AGENT_RUN_ERROR     同上 + ``{error: <reason>}`` —— wrapper 内 speak() 抛非
    #                            CancelledError 异常被吞、msg 为 None 时走。
    # 4 个事件都加入 P9 后台白名单（``room_runtime._BACKGROUND_WHITELIST``），
    # 后台房 bg run 完成时前端「进行中」徽标 / sidebar 指示能即时更新。
    AGENT_RUN_STARTED = "agent_run_started"
    AGENT_RUN_FINISHED = "agent_run_finished"
    AGENT_RUN_CANCELLED = "agent_run_cancelled"
    AGENT_RUN_ERROR = "agent_run_error"

    # P12.6 「已安装」persona 列表 + 检查 / 更新 / 删除的权威全量快照。data 形状
    # ``{personas: [{name, display_name, persona, avatar_data_uri, summary, source_type,
    # source_url, status, local_modified, installed_version, latest_version, detail}, ...]}``。
    # ``status`` 是上游状态枚举、``local_modified`` 是 bool（两维正交）；版本号并列**纯
    # 展示**不参与判定。``list`` / ``check`` / ``update`` / ``delete`` 后均回这一帧（前端
    # 整批覆盖，同 ``room_info.personas_available`` 口径）。按需发，**不**进高频 room_info。
    # 旧前端遇未知 type WARN 后忽略，不 bump schema_version。
    PERSONAS_INSTALLED = "personas_installed"

    # P10.2 非任务房间产物：茶客通过原生 ``write_file('./share/<x>')`` 落盘到房间公共
    # 桌面 ``<room>/share/``，或用户 UI 上传到 share/ —— 与 ``TASK_ARTIFACT_ADDED`` 平行
    # 的 hint。``data`` 形状 ``{rel, name, size, originated_message_id?}`` —— 无
    # ``task_id`` 字段（这是房间级而非任务级产物）。``originated_message_id`` 缺 → 用户
    # 上传 / 跨周期遗留 / 流式失败丢锚，前端走系统气泡兜底。茶客 ``write_file`` 在
    # 同一段 transport_bridge 拦截点压 pending；``ArtifactDetector`` 扫 share/ diff 后
    # consume + 落 ``message_artifacts.jsonl``。``schema_version`` 不 bump。
    ROOM_ARTIFACT_ADDED = "room_artifact_added"


# status 三态。仅 ``message_end`` / ``turn_end`` 有意义；其余事件一律 OK 占位。
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

# NOTICE envelope 的 data.level 取值。前端按 info / error 决定 toast 还是 alert。
NOTICE_LEVEL_INFO = "info"
NOTICE_LEVEL_ERROR = "error"

# TASK_PROPOSAL envelope 的 ``data.kind`` 取值（docs §6.3 / P7.4 §3.1）。前端按 kind
# 分发"采纳"按钮：decision → ADD_DECISION / open → OPEN_TASK / handoff_* → 既有
# HANDOFF_* inbound。Python 端工具子类 ``_kind`` 字面值、events.py 文档、前端
# (P5.3.6 / P7.4.3) 都从这里取，单点散落。
#
# P7.4：handoff propose 复用同一 TASK_PROPOSAL envelope，kind 是**平铺值**（不嵌套
# ``kind:"handoff"`` + ``payload.kind``）—— 字面值与 HANDOFF_* inbound 帧 type 同形，
# 前端 ``proposal_card.js`` 单层 switch 即可分发。
TASK_PROPOSAL_KIND_DECISION = "decision"
TASK_PROPOSAL_KIND_OPEN = "open"
# P8.2：茶客提议改任务状态。采纳后前端按 status 分流——非终结态 → update_task、
# 终结态（done / abandoned）→ close_task（docs/P8 §4）。
TASK_PROPOSAL_KIND_STATUS = "status"
TASK_PROPOSAL_KIND_HANDOFF_DELEGATE = "handoff_delegate"
TASK_PROPOSAL_KIND_HANDOFF_REVIEW = "handoff_review"
TASK_PROPOSAL_KIND_HANDOFF_PANEL = "handoff_panel"


def new_id(prefix: str, n_bytes: int = 10) -> str:
    """``<prefix>_<n_bytes*2 hex>``。茶话室所有 ID（turn / msg / task / dec）共用一个 mint
    口径 —— 改前缀长度只动一处。"""
    return f"{prefix}_{secrets.token_hex(n_bytes)}"


def new_turn_id() -> str:
    """``turn_<10字节 hex>`` —— 与 :func:`new_message_id` 同长度，方便扫读。"""
    return new_id("turn")


def new_message_id() -> str:
    """``msg_<10字节 hex>``。前端 envelope 与 transcript.jsonl 共用同一 ID。"""
    return new_id("msg")


def new_event_id() -> str:
    """``evt_<10字节 hex>``。每行 ``tasks/<id>/events.jsonl`` 一个 stable id —— 给前端
    / 未来 audit tooling 引用某条状态变更用；P5.2.5 envelope 不嵌（hint 事件够用），
    后续如需"撤销 / 跳转到这条变更"再透传。"""
    return new_id("evt")


def now_ms() -> int:
    """毫秒级 Unix 时间戳。所有持久化记录 / envelope 的 ``ts_ms`` 共用此口径。"""
    return int(time.time() * 1000)


# 兼容旧调用方（envelope 字段 default_factory）。新代码用 :func:`now_ms`。
_now_ms = now_ms


@dataclass(frozen=True, slots=True)
class ChahuaEnvelope:
    """前端事件 envelope。一条流式消息共享同一 ``message_id``；一次 pick 共享同一 ``turn_id``。

    ``seq`` 仅在 ``message_end(status=ok)`` 时有意义（其它情况为 ``None``）—— 它指向
    transcript.jsonl 里这条发言的房间序号；失败 / 取消的发言不入 transcript。
    ``guest_name`` 在 ``turn_start`` / ``turn_end`` 这种轮级事件上为 ``None``（轮维度
    跨多位茶客，没单一归属；前端按 ``turn_id`` 聚合）。
    """

    room_id: str
    turn_id: Optional[str]
    guest_name: Optional[str]
    message_id: Optional[str]
    type: ChahuaEventType
    status: str = STATUS_OK
    ts_ms: int = field(default_factory=_now_ms)
    data: Mapping[str, Any] = field(default_factory=dict)
    seq: Optional[int] = None

    def to_dict(self) -> dict:
        """JSON-safe wire 形态（P2.3 WebSocket 推到前端时直接 ``json.dumps``）。"""
        return {
            "schema_version": SCHEMA_VERSION,
            "room_id": self.room_id,
            "turn_id": self.turn_id,
            "guest_name": self.guest_name,
            "message_id": self.message_id,
            "type": self.type.value,
            "status": self.status,
            "ts_ms": self.ts_ms,
            "seq": self.seq,
            "data": dict(self.data),
        }


EnvelopeSink = Callable[[ChahuaEnvelope], None]
"""消费 envelope 的回调。CLI / WebSocket server / 测试 fixture 都实现这个签名。

约束：sink **不应抛异常**。:func:`emit_to_sink` 把 sink 异常吞掉 + WARN（agentao
transport 契约要求 on_event 不抛），但 sink 端应自管错误以保观察性。
"""


def emit_to_sink(sink: EnvelopeSink, env: ChahuaEnvelope) -> None:
    """把 envelope 投到 ``sink``；sink 抛异常一律 WARN + 吞 —— 所有 emit 路径必须走这里
    才能保住"sink 不能把生产者挂掉"的契约。
    """
    try:
        sink(env)
    except Exception:
        _log.exception("envelope sink raised on %s; dropped", env.type.value)


# 一个永远不抛的占位 sink，给"无 UI 消费者"的调用方用（测试 / 程序化驱动）。
NOOP_SINK: EnvelopeSink = lambda _env: None
