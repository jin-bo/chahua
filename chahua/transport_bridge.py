"""ChahuaTransport —— agentao SDK 事件到茶话室前端 envelope 的桥（设计文档 §3.5）。

每个 :class:`chahua.guest.TeaGuest` 持一个 :class:`ChahuaTransport`。终身绑定
``(room_id, guest_name)``；每次 ``speak()`` 前调 :meth:`set_envelope` 设
``(turn_id, message_id)``，调用后 :meth:`clear_envelope` 重置 —— 防止下条消息的
流式 chunk 错挂到上条 message_id 上。

**职责切分**：

- ``message_start`` / ``message_end`` 由 :class:`TeaGuest.speak` 外层合成，不在这里。
  这里只负责"agentao 事件 → 茶话室事件"的纯转译（无副作用、不闭包）。
- ``turn_start`` / ``turn_end`` 由 orchestrator 合成。
- ``ERROR`` 转 ``guest_thinking`` 风格的提示事件，但**消息状态以 message_end.status
  为准**（§3.5.2 明确不依赖 ERROR 事件本身决定流的边界）。

**partial_text 累积**：每条 ``LLM_TEXT`` chunk 在这里追加到 :attr:`partial_text`，供
:class:`TeaGuest.speak` 在异常/取消路径上读取（落 ``message_end.data.partial_text``，
不入 transcript，§3.5.2）。:meth:`set_envelope` 调用时清零。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

from agentao.transport import AgentEvent, EventType, SdkTransport

from .artifact_detector import FileStamp, _list_share_rels
from .debug_recorder import NOOP_RECORDER, TurnRecorder, classify_tool_source
from .events import (
    NOOP_SINK,
    STATUS_OK,
    ChahuaEnvelope,
    ChahuaEventType,
    EnvelopeSink,
    emit_to_sink,
)
from .message_artifacts import MessageArtifactRegistry
from .task_tools import TASK_WRITE_ARTIFACT_TOOL_NAME
from .tasks_store import TasksStore, _validate_artifact_name

_log = logging.getLogger(__name__)


def _normalize_share_rel(file_path: Any) -> Optional[str]:
    """把茶客 ``write_file(file_path=...)`` 的输入规整成 ``share/<x>`` rel；不是 share/
    路径 / 含 traversal 字符 / 含 dot-prefix 段则返 ``None``（P10.2 / P10.3 修）。

    规则与 ``server_inbound_io._download_file`` 的 share/ 段校验对齐，且**与
    :func:`chahua.artifact_detector._list_share_rels` 段级策略保持对称** —— 后者拒任一段
    ``startswith('.')`` 的路径；如果这里不拒，会出现"normalize 接受写入 → pending 落表 →
    detector 扫盘永远跳过此 rel"的死锁式泄露（P10.3 review §3）。

    - 仅接受 ``./share/<...>`` 或 ``share/<...>``（``\\`` / 绝对路径 / 含 ``..`` /
      空段 / 单 ``"."`` 直接拒）。
    - 循环剥前缀 ``./`` —— 允许 ``././share/foo`` 这种 LLM 异形输入（agentao 内部
      normpath 后照常落到 ``share/foo``）。
    - 段以 ``.`` 开头的一律拒（``.hidden`` / ``.cache`` / 与 detector 跳过同口径）。
    - 多级子目录合法（``share/sub/foo.png`` → 整段返回）。
    - 仅接受 str；其它类型 / 空串返 ``None``。

    与 ``download_file`` 不同：本函数仅判断 ``rel`` 形态，**不**做盘上 resolve 检查
    （tool_start 时盘上还没落、resolve 也意义不大）。落盘是否真的落到 share/ 之内由
    agentao ``PathPolicy.contain_file`` 兜底；本函数错放 pending 也只是 detect 阶段
    consume 不到、退化成系统气泡兜底，不会导致写盘到 share/ 之外。
    """
    if not isinstance(file_path, str) or not file_path:
        return None
    # 反斜杠 / 绝对路径直接拒。Windows 上茶客 cwd 也用 POSIX 风（agentao 内部统一），
    # 这里多一道防御。
    if "\\" in file_path or file_path.startswith("/"):
        return None
    rel = file_path
    # 循环剥 './' —— LLM 可能输出 ``././share/foo``、``./././share/x``，agentao 内部
    # normpath 后等同 ``share/foo``；我们也接受，避免 pending 链路在异形输入上断掉。
    while rel.startswith("./"):
        rel = rel[2:]
        if not rel:
            return None
    segments = rel.split("/")
    if len(segments) < 2 or segments[0] != "share":
        return None
    for seg in segments:
        if seg in ("", ".", ".."):
            return None
        # dot-prefix 段同步拒（与 _list_share_rels 对称）。``share/.cache/foo.png`` 会被
        # detector 跳过整族，这里 normalize 也拒，避免 pending 泄露。
        if seg.startswith("."):
            return None
    return rel


# P8.3：托管会话内拦截管理者 propose 的 hook 签名。收一条 ``TASK_PROPOSAL`` envelope
# + 当前 bind 的 sink，返回 ``True`` 表示「已处理，别再下发前端」（拦截）、``False``
# 表示「照常 emit」。Orchestrator 注入 :meth:`Orchestrator._intercept_task_proposal`。
TaskProposalHook = Callable[["ChahuaEnvelope", EnvelopeSink], bool]


# P10.4：args 静态推不出落盘路径的工具，靠 TOOL_START / TOOL_COMPLETE 前后
# diff ``share/`` + active task ``artifacts/`` 把新增 / 真重写文件回填进
# :class:`MessageArtifactRegistry.pending`。``run_shell_command`` 的命令是裸字符串
# （``cp`` / ``mv`` / ``curl`` 等都能写盘）；MCP 工具是黑盒、协议不强制声明产物。
# 与 ``task_write_artifact`` / ``write_file`` / ``replace`` 三个"args 已知"的写盘工具
# 互斥——这三个走 :meth:`_maybe_record_artifact_path` 直接落 pending，diff 路径只接
# 这里不掌握的。两条路径理论上可能撞同 rel：若同一条消息内两条工具都写过同一文件，
# pending 是 ``rel → message_id`` 字典、后写覆盖前写、message_id 一致、幂等无害。
_SHELL_TOOL_NAME = "run_shell_command"
_MCP_TOOL_PREFIX = "mcp__"


def _should_diff_for_attribution(tool_name: str) -> bool:
    """是否对该工具走 TOOL_START / TOOL_COMPLETE 前后 diff 把新文件回填进
    pending。仅接 shell + MCP——其它工具要么是 read-only（diff 永远空、纯浪费
    readdir），要么已经走 :meth:`_maybe_record_artifact_path` args 直拆。
    """
    if tool_name == _SHELL_TOOL_NAME:
        return True
    if tool_name.startswith(_MCP_TOOL_PREFIX):
        return True
    return False


class ChahuaTransport(SdkTransport):
    """SDK 事件 → 茶话室 envelope 的转译器。"""

    def __init__(
        self,
        *,
        room_id: str,
        guest_name: str,
        message_artifacts: Optional[MessageArtifactRegistry] = None,
        share_dir: Optional[Path] = None,
        tasks_store: Optional[TasksStore] = None,
    ) -> None:
        # SdkTransport.on_event 在构造时绑死；我们把 _handle 注册上去。
        super().__init__(on_event=self._handle)
        self._sink: EnvelopeSink = NOOP_SINK
        self._room_id = room_id
        self._guest_name = guest_name
        self._turn_id: Optional[str] = None
        self._message_id: Optional[str] = None
        # 每次 :meth:`bind` 时从 :class:`TeaGuest.speak` 接管的 active task；用 envelope
        # ``data.task_id`` 透出去，让前端把流式 chunk 与任务面板挂钩。
        self._task_id: Optional[str] = None
        self._partial: list[str] = []
        # P6.1：bind() 时由 TeaGuest 传入；workflow tool_start/complete 事件下钩到这里。
        # 未 bind 时为 NOOP_RECORDER；与 sink 同生命周期。
        self._recorder: TurnRecorder = NOOP_RECORDER
        # P8.3：托管会话内拦截管理者 propose 的 hook。``None`` = 缺省（非托管房间 /
        # 测试），``emit_chahua`` 行为与今天完全一致。session 装配时由 Orchestrator
        # 注入；与 transport 终身绑定（不随 bind / clear 变）。
        self._task_proposal_hook: Optional[TaskProposalHook] = None
        # 房间级 message ↔ artifact 注册表（"气泡后挂图片 / 下载链"）。``task_write_artifact``
        # 工具命中时把 ``(task_id, name) → message_id`` 写进 ``pending``，等
        # ``ArtifactDetector`` 取出落盘 + emit envelope。``None`` = 老调用方（测试）/
        # 未启用任务功能，行为同 P9 前。
        self._message_artifacts = message_artifacts
        # P10.4：shell / MCP 工具走 TOOL_START / TOOL_COMPLETE 前后 diff 把新文件
        # 回填 pending —— share_dir = ``<room>/share/`` 实路径（**不**传 guest cwd 的
        # 软链，``_list_share_rels`` 用 ``os.walk(followlinks=False)``）；tasks_store
        # 走 ``list_artifacts(active_task_id)`` 取任务 artifacts/ 当前指纹。两者任一
        # 缺 / 注册表 None / 工具不在白名单 → diff 路径完全 no-op，行为同 P10.3。
        self._share_dir = share_dir
        self._tasks_store = tasks_store
        # call_id → {"before", "message_id", "task_id"}：tool_start 这一刻冻结的快照
        # 与归属 message/task。**冻结 message/task 的原因（P10.4 review §6 修）**：晚到
        # 的 TOOL_COMPLETE 即便 bind 已退出（self._message_id is None），仍能用 entry
        # 里捕获的 mid 完成 record_pending，不丢归属。同一茶客的 agentao 工具调用在同
        # 事件循环内串行，但 call_id 不一定唯一（agentao 自己保证）；以 call_id 当 key。
        self._tool_call_snapshots: dict[
            str, dict[str, Any]
        ] = {}
        # call_id → set[(rel, mid)]：``_maybe_record_artifact_path`` 在 TOOL_START
        # 落下的 pending (rel, mid) 对。失败 / 取消 TOOL_COMPLETE（status != "ok"）时
        # 回滚 —— P10.4 review §8：``task_write_artifact`` 在 TOOL_START 写 pending、但
        # 实际写盘可能失败 / 被覆盖；若是**全新 rel**（baseline 没有 → P10.3 stamp 守卫
        # 不触发 rewrite 拦截 → consume_pending 会把失败 message_id 永久落 jsonl）。
        # 回滚时只 pop 当时记的 mid 还在的条目，避免误删 race 中后来者写的同 rel 新 pending。
        self._tool_call_pre_pending: dict[str, set[tuple[str, str]]] = {}
        # 同一次 bind 内"滚动 baseline"：首个 qualifying TOOL_START 时扫一次 share/ +
        # active task artifacts/，存为 baseline；后续每个 TOOL_COMPLETE 用 after 覆盖。
        # 下一个 TOOL_START 直接复用 baseline 当 before —— 把"每个工具调用扫两次"压成
        # "首次扫一次 + 每个 COMPLETE 扫一次"（P10.4 review §1 perf 修）。tool 间
        # 用户 ``_upload_file`` 介入会让 baseline 看见新文件、被算成下一个工具的"新增"
        # 并 record_pending；但 detector 的 share GC sweep（``_detect_share_artifacts``
        # 末尾）见 ``current == prev`` 不 emit、把这条 pending 当 stale 清掉，不污染
        # 历史归属。
        self._diff_baseline: Optional[dict[str, FileStamp]] = None

    def set_task_proposal_hook(self, hook: Optional[TaskProposalHook]) -> None:
        """注入 / 清除 ``TASK_PROPOSAL`` 拦截 hook（P8.3，docs §5.1）。

        session 装配期一次性注入；与 transport 同生命周期，不随每次 ``bind`` 变。
        """
        self._task_proposal_hook = hook

    # ── per-speak 生命周期 ─────────────────────────────────────────────────

    @contextmanager
    def bind(
        self,
        *,
        sink: EnvelopeSink,
        turn_id: str,
        message_id: str,
        task_id: Optional[str] = None,
        recorder: TurnRecorder = NOOP_RECORDER,
    ) -> Iterator["ChahuaTransport"]:
        """绑定一次 speak() 的 (sink, turn_id, message_id [, task_id, recorder])；
        退出时复位。

        ``with self._transport.bind(...): self._transport.emit_chahua(...)`` ——
        把"开 envelope / 任何路径都得复位"用 Python 语法收紧，避免 set/clear 配对
        在 speak() 的多 except 分支里手抖漏掉。partial_text 缓冲在进入 with 时清。

        ``task_id``：本轮 message_* envelope 的 ``data.task_id``。``None`` = 房间级闲聊，
        data 里不写这个键（envelope schema 不变，老前端无感）。

        ``recorder``：P6.1 起 ``TeaGuest.speak`` 透传；TOOL_START / TOOL_COMPLETE 帧
        转译时同步 ``record_tool_start`` / ``record_tool_complete``。未 bind 期间为
        ``NOOP_RECORDER`` —— 防 agentao 在 speak 外异步 emit 误流入。
        """
        self._sink = sink
        self._turn_id = turn_id
        self._message_id = message_id
        self._task_id = task_id
        self._recorder = recorder
        self._partial.clear()
        try:
            yield self
        finally:
            # 复位顺序无关；都置成"无活动 envelope"状态。partial_text 不清，让
            # 调用方在 finally 里还能读（下次 bind 时清零）。
            self._sink = NOOP_SINK
            self._turn_id = None
            self._message_id = None
            self._task_id = None
            self._recorder = NOOP_RECORDER
            # P10.4 review §6 修：``_tool_call_snapshots`` / ``_tool_call_pre_pending``
            # **不**在 bind 退出时清。每个 entry 已捕获了当时的 message_id / task_id，
            # 即便 ``with bind()`` 已退出（``self._message_id`` 变 None），晚到的
            # TOOL_COMPLETE 仍能用 entry 里的 mid 完成 record_pending / rollback、
            # 不丢归属。同一 call_id 不会跨 bind 复用（agentao 全局唯一），所以没有
            # "下次 bind 撞 stale 条目"的风险——TOOL_COMPLETE 一到 pop、自然清。
            # baseline 仍清：``_diff_baseline`` 是同 bind 内多个 shell/MCP 调用共享的
            # 滚动快照，下次 bind 是新的对话上下文，应重新扫盘。
            self._diff_baseline = None

    @property
    def partial_text(self) -> str:
        """已收到的 chunk 拼接结果。speak() 在 error/cancel 路径上用。"""
        return "".join(self._partial)

    @property
    def guest_name(self) -> str:
        """终身绑定的茶客名（构造时设、不可变）。"""
        return self._guest_name

    @property
    def current_task_id(self) -> Optional[str]:
        """当前 :meth:`bind` 上下文的 task_id 快照；未 bind 时 ``None``。

        P5.3.4 task_tools 在 ``tool.execute()`` 内读：这时正跑在 LLM 的
        ``agent.arun()`` 里、``TeaGuest.speak()`` 已 bind 上下文 → 这里读到的是
        进 speak 时被 snapshot 的归属，与 message_* envelope 同源。"""
        return self._task_id

    def inflight_snapshot(self) -> Optional[dict]:
        """当前 bind 中的 in-flight 消息快照 —— 未 bind（无活动 ``speak()``）→ ``None``。

        P9 切回一个 turn 在后台续跑的房间时，``emit_room_snapshot`` 据此补发那条
        进行中消息的 ``message_start`` + ``message_delta(partial_text)``，让切回前
        已流出的内容立即在聊天区 / 调试面板成形，不必干等 ``message_end``。
        ``partial_text`` 是已 emit 过 delta 的 chunk 拼接 —— 与后续真实 delta 严格
        续接（单事件循环线程，快照在 turn 恢复前同步发完）。
        """
        if self._message_id is None:
            return None
        return {
            "turn_id": self._turn_id,
            "message_id": self._message_id,
            "guest_name": self._guest_name,
            "task_id": self._task_id,
            "partial_text": self.partial_text,
        }

    # ── 给上层 emit 用（message_start / message_end 走这里） ───────────────

    def emit_chahua(
        self,
        type: ChahuaEventType,
        data: Optional[Mapping[str, Any]] = None,
        *,
        status: str = STATUS_OK,
        seq: Optional[int] = None,
    ) -> None:
        """合成 envelope 走 sink。turn_id / message_id 取当前 :meth:`bind` 的值；
        没绑就丢 + WARN（防 agentao 在 speak() 外异步 emit 误流入）。

        bind 时若传了 ``task_id``，自动塞到 ``data.task_id`` —— envelope schema_version
        不变，前端 reducer 按 ``data.task_id`` 把流式 chunk 挂到任务面板。``None`` 时不写键，
        老前端不感知。
        """
        if self._turn_id is None:
            _log.warning(
                "ChahuaTransport.emit_chahua(%s) called without active envelope; dropped",
                type.value,
            )
            return
        # 无任务时跳过 dict 拷贝 —— message_delta 每个 chunk 都过这里，常见场景是房间级闲聊。
        if self._task_id is None:
            merged: Mapping[str, Any] = data or {}
        else:
            merged = {**(data or {}), "task_id": self._task_id}
        env = ChahuaEnvelope(
            room_id=self._room_id,
            turn_id=self._turn_id,
            guest_name=self._guest_name,
            message_id=self._message_id,
            type=type,
            status=status,
            seq=seq,
            data=merged,
        )
        # P8.3：托管会话内管理者的 handoff_delegate / handoff_panel 提议被 hook 直接
        # 入队、拦下不下发前端（不渲采纳卡，docs §5.1）。hook 返回 True = 已拦截。
        # hook 缺省 None / 非 TASK_PROPOSAL 时这段零成本。hook 抛错不阻断 emit ——
        # 退化成照常下发（与 emit_to_sink「sink 不能挂掉生产者」同口径）。
        if type is ChahuaEventType.TASK_PROPOSAL and self._task_proposal_hook is not None:
            try:
                if self._task_proposal_hook(env, self._sink):
                    return
            except Exception:
                _log.exception("task_proposal_hook raised; falling back to emit")
        emit_to_sink(self._sink, env)

    def _maybe_record_artifact_path(
        self, tool_name: str, args: Any, call_id: Optional[str] = None,
    ) -> None:
        """从已知写盘工具的 args 派生 artifact 相对路径，喂给 recorder + message_artifacts
        注册表（docs/P6 §6 + docs/P10.2）。

        派生表：

        - ``task_write_artifact(name, content)`` → rel = ``tasks/<task_id>/artifacts/<name>``
          （需 task_id 已绑定）。喂 recorder + 注册表。
        - ``write_file(file_path="./share/<x>"|"share/<x>")`` → rel = ``share/<x>``
          （P10.2 房间公共桌面）。**只**喂注册表，不喂 recorder —— debug 取证按任务为单位
          组织，房间产物不进 turn 的 artifact 列。

        落到 ``share/`` 外的 ``write_file``（茶客 cwd 内其它路径）直接忽略 —— 不算公共
        产物，也不该挂气泡。

        前置条件：调用方已确保 ``self._message_id is not None``（与 ``record_tool_start``
        合并守卫，避免重复 nullable 判定）。
        """
        if not isinstance(args, dict):
            return
        if tool_name == TASK_WRITE_ARTIFACT_TOOL_NAME:
            if self._task_id is None:
                return
            name = args.get("name")
            if not isinstance(name, str) or not name:
                return
            # 与写盘路径同口径：name 合法（无 ``/`` / ``\\`` / ``..`` / 前缀 ``.``）才记
            # —— 否则 ``TasksStore.write_artifact`` 会拒、根本不会落盘，调试日志记一条
            # "幽灵 artifact" 会误导用户去查根本不存在的文件。前端 ``deriveArtifactPath``
            # 同算法跟进。
            if _validate_artifact_name(name) is not None:
                return
            rel = f"tasks/{self._task_id}/artifacts/{name}"
            self._recorder.record_artifact_path(
                message_id=self._message_id,  # type: ignore[arg-type]
                path=rel,
            )
            # 同一段写盘工具拦截点，把 rel → message_id 压进注册表。
            # 等 ``ArtifactDetector.detect`` 在 pick 周期末扫盘后 ``consume_pending``
            # 取出落盘 + emit ``originated_message_id``。注册表 None = 测试 / 未装配，
            # debug 路径独立，不受影响。
            if self._message_artifacts is not None:
                self._message_artifacts.record_pending(
                    rel=rel,
                    name=name,
                    message_id=self._message_id,  # type: ignore[arg-type]
                )
                # P10.4 review §8：追踪 pending rel，让 TOOL_COMPLETE 看到失败 status
                # 时能 rollback —— 不变量 P10.3 stamp 守卫只覆盖已 baseline 的 rel，
                # 全新 rel 失败时仍会被 consume_pending 误绑死 message_id。
                self._track_pre_pending(call_id, rel)
            return
        # P10.2 / P10.3：原生 ``write_file`` 与 ``replace`` 都能落盘到 ``./share/``
        # —— 房间公共桌面。两者 args 都用 ``file_path`` 字段（agentao file_ops.py
        # WriteFileTool / EditTool 同 schema）。原生写 / 替换都触发 pending，确保
        # 「在场气泡挂图片 / 下载链」覆盖到所有 agentao 落盘工具。
        # P10.4 起 ``run_shell_command`` / ``mcp__*`` 走另一条「TOOL_START / COMPLETE
        # 前后 diff」路径（:meth:`_consume_tool_diff`），args 推不出落盘路径也能挂
        # 气泡——本函数仍只处理 args 可静态推断的写盘工具。
        if tool_name in ("write_file", "replace"):
            if self._message_artifacts is None:
                return
            file_path = args.get("file_path")
            share_rel = _normalize_share_rel(file_path)
            if share_rel is None:
                return
            # share/ 内每段也走 name 校验（拒 ``.DS_Store`` / ``..`` / 前缀点等），与
            # ``download_file`` 段校验对齐 —— 避免茶客 ``write_file('./share/x/../y')``
            # 在注册表里留鬼影。``_normalize_share_rel`` 已拦 ``..`` / 空段 / dot-prefix；
            # 这里再做 basename name 校验作为最后一道。
            name = share_rel.rsplit("/", 1)[-1]
            if _validate_artifact_name(name) is not None:
                return
            self._message_artifacts.record_pending(
                rel=share_rel,
                name=name,
                message_id=self._message_id,  # type: ignore[arg-type]
            )
            # P10.4 review §8：同 task_write_artifact 路径 —— TOOL_COMPLETE 失败时
            # rollback 这条 pending（write_file / replace 失败时旧文件可能仍在或没有）。
            self._track_pre_pending(call_id, share_rel)

    def _track_pre_pending(
        self, call_id: Optional[str], rel: str,
    ) -> None:
        """P10.4 review §8 helper：把 TOOL_START 时刚落下的 (rel, mid) 追加到
        ``_tool_call_pre_pending[call_id]``，配对 TOOL_COMPLETE 的失败回滚。

        ``call_id`` 缺（不该发生，agentao 始终带）/ ``self._message_id`` 缺
        → 不追踪，回滚自然降级到老 P10.3 行为（stamp 守卫 + GC sweep）。
        """
        if not isinstance(call_id, str) or not call_id:
            return
        if self._message_id is None:
            return
        self._tool_call_pre_pending.setdefault(call_id, set()).add(
            (rel, self._message_id),
        )

    def _rollback_pre_pending(self, call_id: str) -> None:
        """P10.4 review §8：TOOL_COMPLETE 收到 ``status != "ok"`` 时，把 TOOL_START
        预落的 (rel, mid) 全部回滚。

        **严格 mid 校验**：pending 是 ``rel → mid`` 覆盖语义；TOOL_START → COMPLETE
        之间另一消息 race 写过同 rel 时 mid 已不同 —— 此时不能盲 pop（会误删后来
        合法新主的 pending）。只 pop ``pending[rel] == 当时记录的 mid`` 的条目。
        """
        if self._message_artifacts is None:
            return
        items = self._tool_call_pre_pending.pop(call_id, None)
        if not items:
            return
        for rel, mid in items:
            if self._message_artifacts.pending.get(rel) == mid:
                self._message_artifacts.pending.pop(rel, None)

    # ── P10.4：shell / MCP 工具 args 不可静态推断的"前后 diff" 路径 ─────────

    def _snapshot_writable_rels(
        self, *, task_id: Optional[str] = None,
    ) -> dict[str, FileStamp]:
        """给 shell / MCP 工具用的"diff 基线" —— 房间 ``share/`` 实路径 + 给定
        ``task_id`` 的 ``artifacts/`` 合并后的 ``rel → (mtime, size)`` 快照。

        - ``share_dir`` / ``tasks_store`` 任一缺 → 对应那段不进 snapshot；两段都缺
          返空 dict，调用方 diff 后自然 no-op。
        - ``task_id`` 缺省 ``None`` → 走当前 bind 的 ``self._task_id``；TOOL_COMPLETE
          路径显式传入捕获的快照 task_id（P10.4 review §6 修，让晚到的 TOOL_COMPLETE
          即便 bind 已退出仍能扫到对的任务）。
        - ``list_artifacts`` 失败 WARN 不抛——任务路径异常不该把整条工具事件链阻断。
        - share/ 与任务 artifacts/ 的 mtime 精度不同（前者 ns、后者 ms），但 diff 比较
          只发生在同 rel key 之内，跨路径无影响。
        """
        effective_task = task_id if task_id is not None else self._task_id
        out: dict[str, FileStamp] = {}
        if self._share_dir is not None:
            out.update(_list_share_rels(self._share_dir))
        if self._tasks_store is not None and effective_task is not None:
            try:
                for art in self._tasks_store.list_artifacts(effective_task):
                    rel = art.get("rel")
                    if not isinstance(rel, str) or not rel:
                        continue
                    out[rel] = (
                        int(art.get("mtime_ms", 0)),
                        int(art.get("size", 0)),
                    )
            except Exception:
                _log.warning(
                    "tool diff: snapshot task artifacts failed for task=%s",
                    effective_task, exc_info=True,
                )
        return out

    def _consume_tool_diff(self, call_id: str) -> None:
        """TOOL_COMPLETE 时调：拿出 TOOL_START 时存的快照，再扫一遍当前盘上状态，
        新增 / 指纹变化的 rel 全部 ``record_pending(rel, name, message_id)``。

        **P10.4 review §1 perf 修**：当 ``self._diff_baseline`` 已有滚动 baseline 时，
        TOOL_START 这一刻不再额外扫盘，直接复用 baseline 当 ``before``——把
        "每个工具调用扫两次"压成 "首次扫一次 + 每个 COMPLETE 扫一次"。COMPLETE 这段
        始终扫一次（``after``），diff 后用 after 覆盖 baseline。

        **P10.4 review §6 修**：snapshot entry 里捕获了 ``message_id`` / ``task_id``，
        即便 ``with bind()`` 已退出（_message_id is None），仍能用捕获值完成
        record_pending、不丢归属。

        守卫：

        - call_id 没在 ``_tool_call_snapshots`` 里（TOOL_START 时未压入 / 已 rollback）
          → 不抛、直接返回。
        - ``_message_artifacts`` 缺 → 没法 record_pending，直接返回。
        - rel 的 basename 跑 ``_validate_artifact_name`` 校验（与
          ``_normalize_share_rel`` 的 basename 校验同口径）—— 不合法 basename 进 pending
          也是 stale，提前拦。
        """
        entry = self._tool_call_snapshots.pop(call_id, None)
        if entry is None:
            return
        if self._message_artifacts is None:
            return
        before: dict[str, FileStamp] = entry["before"]
        captured_mid: str = entry["message_id"]
        captured_task: Optional[str] = entry.get("task_id")
        after = self._snapshot_writable_rels(task_id=captured_task)
        # 更新滚动 baseline，让下一个 qualifying TOOL_START 复用、跳过扫盘。
        # 仅 share/ 段（不含 tasks，task baseline 与当前 task_id 强耦合、不复用）
        # —— 简化：直接用整个 after 当 baseline，下次 TOOL_START 复用整段即可
        # （task 段 diff 也跑同口径）。
        #
        # **P10.4 review §1.2 修**：只在仍处于 bind 内才写 baseline。晚到的
        # TOOL_COMPLETE（``self._message_id is None``，bind 已退出）若仍覆盖
        # baseline，下次 bind 的首个 shell/MCP TOOL_START 会跳过扫盘、把这次
        # COMPLETE 到下次 START 之间凭空出现的文件错算成下条消息的"新增"并
        # 错挂归属。两次 bind 之间的盘状态不连续，必须重扫。
        if self._message_id is not None:
            self._diff_baseline = after
        for rel, stamp in after.items():
            if before.get(rel) == stamp:
                continue
            name = rel.rsplit("/", 1)[-1]
            if _validate_artifact_name(name) is not None:
                continue
            self._message_artifacts.record_pending(
                rel=rel,
                name=name,
                message_id=captured_mid,
            )

    # ── agentao 事件回调 ───────────────────────────────────────────────────

    def _handle(self, event: AgentEvent) -> None:
        """SDK on_event 入口。按类型转译到对应 envelope；其余事件丢弃。"""
        et = event.type
        data = event.data

        if et is EventType.LLM_TEXT:
            chunk = data.get("chunk", "")
            if not chunk:
                return
            self._partial.append(chunk)
            self.emit_chahua(ChahuaEventType.MESSAGE_DELTA, {"chunk": chunk})
            return

        if et is EventType.THINKING:
            text = data.get("text", "")
            if not text:
                return
            self.emit_chahua(ChahuaEventType.GUEST_THINKING, {"text": text})
            return

        if et is EventType.TOOL_START:
            # 字段直接转 —— agentao 用的 key 与设计文档 §3.5.1 一致（见 events.py 注释）。
            tool_name = data.get("tool") or ""
            call_id = data.get("call_id")
            args = data.get("args")
            self.emit_chahua(
                ChahuaEventType.TOOL_START,
                {"tool": tool_name, "args": args, "call_id": call_id},
            )
            # P6.1：MCP 来源走 tool 名启发式（不变量"_classify_tool_source 仅
            # best-effort"），不为识别 MCP 改 agentao event 形态。
            if self._message_id is not None:
                source, mcp_server = classify_tool_source(tool_name)
                self._recorder.record_tool_start(
                    message_id=self._message_id,
                    call_id=call_id,
                    tool=tool_name,
                    args=args,
                    source=source,
                    mcp_server=mcp_server,
                )
                # P6.1 artifact 派生表显式枚举（docs §不变量"仅从特定写盘工具派生"）：
                # ``task_write_artifact(name, content)`` → ``tasks/<task_id>/artifacts/<name>``。
                # ``task_id`` 缺 / ``name`` 缺时跳过（不入 task）。后续加新写盘工具按
                # tool name 扩派生表 —— 不挂 ArtifactDetector。
                self._maybe_record_artifact_path(tool_name, args, call_id=call_id)
                # P10.4：shell / MCP 类工具 args 推不出落盘路径，TOOL_START 这一刻
                # 冻结一份 share/ + active task artifacts/ 指纹快照与归属
                # (message_id / task_id)，等 TOOL_COMPLETE 再 diff 回填 pending。
                # 滚动 baseline（``self._diff_baseline``）非空时复用、跳过扫盘 ——
                # 单次 bind 内多个 shell/MCP 调用之间的"share/ 已知态"是连续的，
                # 用上次 COMPLETE 的 after 当本次的 before 等价。
                if (
                    self._message_artifacts is not None
                    and self._message_id is not None
                    and isinstance(call_id, str)
                    and call_id
                    and _should_diff_for_attribution(tool_name)
                ):
                    if self._diff_baseline is None:
                        self._diff_baseline = self._snapshot_writable_rels()
                    self._tool_call_snapshots[call_id] = {
                        "before": self._diff_baseline,
                        "message_id": self._message_id,
                        "task_id": self._task_id,
                    }
            return

        if et is EventType.TOOL_COMPLETE:
            call_id = data.get("call_id")
            self.emit_chahua(
                ChahuaEventType.TOOL_COMPLETE,
                {
                    "tool": data.get("tool"),
                    "call_id": call_id,
                    "status": data.get("status"),
                    "duration_ms": data.get("duration_ms"),
                    "error": data.get("error"),
                },
            )
            if self._message_id is not None:
                self._recorder.record_tool_complete(
                    message_id=self._message_id,
                    call_id=call_id,
                    status=data.get("status"),
                    duration_ms=data.get("duration_ms"),
                    error=data.get("error"),
                )
            # P10.4：shell / MCP 工具走 diff 路径回填 pending —— 与 TOOL_START 那段
            # 配对。snapshot 命不中（白名单外 / call_id 缺）→ no-op。
            # **status 失败时分流**（P10.4 review §8）：rollback ``_maybe_record_artifact_path``
            # 在 TOOL_START 预落的 pending —— 全新 rel 的失败写盘 P10.3 stamp 守卫拦不住，
            # 若不主动 rollback、consume_pending 会把失败 message_id 永久落 jsonl。
            # 成功（status="ok"）的工具走正常 diff 路径，pre-pending 集合扔掉即可。
            if isinstance(call_id, str) and call_id:
                status = data.get("status")
                if status != "ok":
                    self._rollback_pre_pending(call_id)
                else:
                    # 成功路径：扔掉 pre-pending 追踪（pending 由正常 consume 路径走）。
                    self._tool_call_pre_pending.pop(call_id, None)
                self._consume_tool_diff(call_id)
            return

        if et is EventType.ERROR:
            # 转为 guest_thinking 风格的提示事件，前端可选显示；**不**作为消息状态的根据
            # （§3.5.2：message_end.status 才是边界 truth）。
            msg = data.get("message") or data.get("detail") or "(无 detail)"
            self.emit_chahua(
                ChahuaEventType.GUEST_THINKING,
                {"text": f"[运行时错误：{msg}]"},
            )
            return

        # 其余事件（TURN_BEGIN/END、AGENT_START/END、MEMORY_*、SKILL_*、PERMISSION_*、
        # LLM_CALL_*、TOOL_OUTPUT/RESULT、ASK_USER_*、BACKGROUND_*、PLUGIN_HOOK_FIRED、
        # MODEL_CHANGED、CONTEXT_COMPRESSED、SESSION_SUMMARY_WRITTEN、TURN_START、
        # TOOL_CONFIRMATION、READONLY_MODE_CHANGED 等）—— 茶话室前端不关心，丢弃。
