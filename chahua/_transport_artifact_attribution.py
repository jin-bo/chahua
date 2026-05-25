""":class:`ArtifactAttributor` —— :class:`ChahuaTransport` 的 P10 artifact 归属子域。

设计见 [`docs/P10-消息气泡挂件.md`](../docs/P10-消息气泡挂件.md) §「pending 归属来源」。
把原 ``transport_bridge.py`` 里 5 个 artifact-attribution 方法（``_maybe_record_artifact_path``
/ ``_track_pre_pending`` / ``_rollback_pre_pending`` / ``_snapshot_writable_rels`` /
``_consume_tool_diff``）+ 配套 helper（``_normalize_share_rel`` / ``_should_diff_for_attribution``）+
状态字段（``_tool_call_snapshots`` / ``_tool_call_pre_pending`` / ``_diff_baseline``）搬到本模块。

:class:`ChahuaTransport` 持一个 :class:`ArtifactAttributor` 实例（``self._attribution``）；
``_handle`` 的 TOOL_START / TOOL_COMPLETE 分支调 ``self._attribution.<method>``、``bind``
finally 调 ``clear_baseline()``。``_tool_call_snapshots`` / ``_diff_baseline`` 是 ``test_share_artifacts``
直接 access 的内部字段 —— ``ChahuaTransport`` 保留两个 read-only @property 把它们透出
（``transport._tool_call_snapshots`` / ``transport._diff_baseline``）保测试 / debug 路径
零改动。

承重不变量（CLAUDE.md 聊天界面渲染 §「pending 归属来源」+ docs/P10.4）：

- args 已知 3 工具（``task_write_artifact`` / ``write_file`` / ``replace``）走
  ``maybe_record_artifact_path`` args 直拆 → 直接 ``record_pending(rel, mid)``，
  TOOL_START 时落、TOOL_COMPLETE 失败时 ``rollback_pre_pending`` 走严格 mid 校验。
- args 推不出落盘路径的 2 类工具（``run_shell_command`` / ``mcp__*``）走
  ``snapshot_writable_rels`` 前后 diff —— TOOL_START 冻结一份指纹 + 归属
  ``(message_id, task_id)``，TOOL_COMPLETE 时 diff after − before、新增 / 指纹变化
  的 rel 进 pending。
- 滚动 baseline ``_diff_baseline`` 仅在 ``self.transport._message_id is not None`` 时
  更新（晚到的 TOOL_COMPLETE 不写）—— 跨 bind 盘状态不连续，下次 bind 首个工具
  必须重扫；本 bind 内多个 shell/MCP 共享 baseline 把"扫两次"压成"扫一次 + 每个
  COMPLETE 扫一次"。
- ``_tool_call_snapshots`` / ``_tool_call_pre_pending`` **不**在 bind 退出时清 —— 晚到
  的 TOOL_COMPLETE（``_message_id is None``）仍能用 entry 里冻结的 mid / task_id
  完成 record_pending / rollback。同 call_id 不跨 bind 复用（agentao 全局唯一）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .artifact_detector import FileStamp, _list_share_rels
from .message_artifacts import MessageArtifactRegistry
from .task_tools import TASK_WRITE_ARTIFACT_TOOL_NAME
from .tasks_store import TasksStore, _validate_artifact_name

if TYPE_CHECKING:
    from .transport_bridge import ChahuaTransport

_log = logging.getLogger(__name__)


# P10.4：args 静态推不出落盘路径的工具，靠 TOOL_START / TOOL_COMPLETE 前后
# diff ``share/`` + active task ``artifacts/`` 把新增 / 真重写文件回填进
# :class:`MessageArtifactRegistry.pending`。``run_shell_command`` 的命令是裸字符串
# （``cp`` / ``mv`` / ``curl`` 等都能写盘）；MCP 工具是黑盒、协议不强制声明产物。
# 与 ``task_write_artifact`` / ``write_file`` / ``replace`` 三个"args 已知"的写盘工具
# 互斥——这三个走 :meth:`maybe_record_artifact_path` 直接落 pending，diff 路径只接
# 这里不掌握的。两条路径理论上可能撞同 rel：若同一条消息内两条工具都写过同一文件，
# pending 是 ``rel → message_id`` 字典、后写覆盖前写、message_id 一致、幂等无害。
_SHELL_TOOL_NAME = "run_shell_command"
_MCP_TOOL_PREFIX = "mcp__"


def _should_diff_for_attribution(tool_name: str) -> bool:
    """是否对该工具走 TOOL_START / TOOL_COMPLETE 前后 diff 把新文件回填进
    pending。仅接 shell + MCP——其它工具要么是 read-only（diff 永远空、纯浪费
    readdir），要么已经走 :meth:`maybe_record_artifact_path` args 直拆。
    """
    if tool_name == _SHELL_TOOL_NAME:
        return True
    if tool_name.startswith(_MCP_TOOL_PREFIX):
        return True
    return False


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


class ArtifactAttributor:
    """:class:`ChahuaTransport` 的 P10 artifact 归属 slot。

    持 transport 反向引用（读 ``_message_id`` / ``_task_id`` / ``_recorder``）+ 自有
    状态字段（``_tool_call_snapshots`` / ``_tool_call_pre_pending`` / ``_diff_baseline``）
    + 装配期注入的房间级 dep（``message_artifacts`` / ``share_dir`` / ``tasks_store``）。
    """

    def __init__(
        self,
        *,
        transport: "ChahuaTransport",
        message_artifacts: Optional[MessageArtifactRegistry],
        share_dir: Optional[Path],
        tasks_store: Optional[TasksStore],
    ) -> None:
        self.transport = transport
        # 房间级 message ↔ artifact 注册表。``task_write_artifact`` / args-known 写盘
        # 工具命中时把 ``(rel, message_id)`` 压进 ``pending``，等
        # ``ArtifactDetector.detect`` 取出落盘 + emit envelope。``None`` = 老调用方
        # （测试）/ 未启用任务功能，行为同 P9 前。
        self.message_artifacts = message_artifacts
        # P10.4：shell / MCP 工具走 TOOL_START / TOOL_COMPLETE 前后 diff —— share_dir =
        # ``<room>/share/`` 实路径（**不**传 guest cwd 的软链，``_list_share_rels`` 用
        # ``os.walk(followlinks=False)``）；tasks_store 走 ``list_artifacts(active_task_id)``
        # 取任务 artifacts/ 当前指纹。两者任一缺 / 注册表 None / 工具不在白名单 →
        # diff 路径完全 no-op，行为同 P10.3。
        self.share_dir = share_dir
        self.tasks_store = tasks_store
        # call_id → {"before", "message_id", "task_id"}：tool_start 这一刻冻结的快照
        # 与归属 message/task。**冻结 message/task 的原因（P10.4 review §6 修）**：晚到
        # 的 TOOL_COMPLETE 即便 bind 已退出（self.transport._message_id is None），仍能用
        # entry 里捕获的 mid 完成 record_pending，不丢归属。同一茶客的 agentao 工具调用
        # 在同事件循环内串行，但 call_id 不一定唯一（agentao 自己保证）；以 call_id 当 key。
        self._tool_call_snapshots: dict[str, dict[str, Any]] = {}
        # call_id → set[(rel, mid)]：``maybe_record_artifact_path`` 在 TOOL_START
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

    def clear_baseline(self) -> None:
        """bind 退出时清滚动 baseline —— 跨 bind 盘状态不连续，下次 bind 首个 qualifying
        TOOL_START 必须重扫。``_tool_call_snapshots`` / ``_tool_call_pre_pending``
        **不**在这里清（晚到的 TOOL_COMPLETE 仍需要 entry 里的 mid，见 module docstring）。
        """
        self._diff_baseline = None

    def maybe_record_artifact_path(
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

        前置条件：调用方已确保 ``self.transport._message_id is not None``（与
        ``record_tool_start`` 合并守卫，避免重复 nullable 判定）。
        """
        if not isinstance(args, dict):
            return
        transport = self.transport
        message_id = transport._message_id
        if tool_name == TASK_WRITE_ARTIFACT_TOOL_NAME:
            if transport._task_id is None:
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
            rel = f"tasks/{transport._task_id}/artifacts/{name}"
            transport._recorder.record_artifact_path(
                message_id=message_id,  # type: ignore[arg-type]
                path=rel,
            )
            # 同一段写盘工具拦截点，把 rel → message_id 压进注册表。
            # 等 ``ArtifactDetector.detect`` 在 pick 周期末扫盘后 ``consume_pending``
            # 取出落盘 + emit ``originated_message_id``。注册表 None = 测试 / 未装配，
            # debug 路径独立，不受影响。
            if self.message_artifacts is not None:
                self.message_artifacts.record_pending(
                    rel=rel,
                    name=name,
                    message_id=message_id,  # type: ignore[arg-type]
                )
                # P10.4 review §8：追踪 pending rel，让 TOOL_COMPLETE 看到失败 status
                # 时能 rollback —— 不变量 P10.3 stamp 守卫只覆盖已 baseline 的 rel，
                # 全新 rel 失败时仍会被 consume_pending 误绑死 message_id。
                self.track_pre_pending(call_id, rel)
            return
        # P10.2 / P10.3：原生 ``write_file`` 与 ``replace`` 都能落盘到 ``./share/``
        # —— 房间公共桌面。两者 args 都用 ``file_path`` 字段（agentao file_ops.py
        # WriteFileTool / EditTool 同 schema）。原生写 / 替换都触发 pending，确保
        # 「在场气泡挂图片 / 下载链」覆盖到所有 agentao 落盘工具。
        # P10.4 起 ``run_shell_command`` / ``mcp__*`` 走另一条「TOOL_START / COMPLETE
        # 前后 diff」路径（:meth:`consume_tool_diff`），args 推不出落盘路径也能挂
        # 气泡——本函数仍只处理 args 可静态推断的写盘工具。
        if tool_name in ("write_file", "replace"):
            if self.message_artifacts is None:
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
            self.message_artifacts.record_pending(
                rel=share_rel,
                name=name,
                message_id=message_id,  # type: ignore[arg-type]
            )
            # P10.4 review §8：同 task_write_artifact 路径 —— TOOL_COMPLETE 失败时
            # rollback 这条 pending（write_file / replace 失败时旧文件可能仍在或没有）。
            self.track_pre_pending(call_id, share_rel)

    def track_pre_pending(
        self, call_id: Optional[str], rel: str,
    ) -> None:
        """P10.4 review §8 helper：把 TOOL_START 时刚落下的 (rel, mid) 追加到
        ``_tool_call_pre_pending[call_id]``，配对 TOOL_COMPLETE 的失败回滚。

        ``call_id`` 缺（不该发生，agentao 始终带）/ ``self.transport._message_id`` 缺
        → 不追踪，回滚自然降级到老 P10.3 行为（stamp 守卫 + GC sweep）。
        """
        if not isinstance(call_id, str) or not call_id:
            return
        message_id = self.transport._message_id
        if message_id is None:
            return
        self._tool_call_pre_pending.setdefault(call_id, set()).add(
            (rel, message_id),
        )

    def rollback_pre_pending(self, call_id: str) -> None:
        """P10.4 review §8：TOOL_COMPLETE 收到 ``status != "ok"`` 时，把 TOOL_START
        预落的 (rel, mid) 全部回滚。

        **严格 mid 校验**：pending 是 ``rel → mid`` 覆盖语义；TOOL_START → COMPLETE
        之间另一消息 race 写过同 rel 时 mid 已不同 —— 此时不能盲 pop（会误删后来
        合法新主的 pending）。只 pop ``pending[rel] == 当时记录的 mid`` 的条目。
        """
        if self.message_artifacts is None:
            return
        items = self._tool_call_pre_pending.pop(call_id, None)
        if not items:
            return
        for rel, mid in items:
            if self.message_artifacts.pending.get(rel) == mid:
                self.message_artifacts.pending.pop(rel, None)

    # ── P10.4：shell / MCP 工具 args 不可静态推断的"前后 diff" 路径 ─────────

    def snapshot_writable_rels(
        self, *, task_id: Optional[str] = None,
    ) -> dict[str, FileStamp]:
        """给 shell / MCP 工具用的"diff 基线" —— 房间 ``share/`` 实路径 + 给定
        ``task_id`` 的 ``artifacts/`` 合并后的 ``rel → (mtime, size)`` 快照。

        - ``share_dir`` / ``tasks_store`` 任一缺 → 对应那段不进 snapshot；两段都缺
          返空 dict，调用方 diff 后自然 no-op。
        - ``task_id`` 缺省 ``None`` → 走当前 bind 的 ``self.transport._task_id``；
          TOOL_COMPLETE 路径显式传入捕获的快照 task_id（P10.4 review §6 修，让
          晚到的 TOOL_COMPLETE 即便 bind 已退出仍能扫到对的任务）。
        - ``list_artifacts`` 失败 WARN 不抛——任务路径异常不该把整条工具事件链阻断。
        - share/ 与任务 artifacts/ 的 mtime 精度不同（前者 ns、后者 ms），但 diff 比较
          只发生在同 rel key 之内，跨路径无影响。
        """
        effective_task = task_id if task_id is not None else self.transport._task_id
        out: dict[str, FileStamp] = {}
        if self.share_dir is not None:
            out.update(_list_share_rels(self.share_dir))
        if self.tasks_store is not None and effective_task is not None:
            try:
                for art in self.tasks_store.list_artifacts(effective_task):
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

    def consume_tool_diff(self, call_id: str) -> None:
        """TOOL_COMPLETE 时调：拿出 TOOL_START 时存的快照，再扫一遍当前盘上状态，
        新增 / 指纹变化的 rel 全部 ``record_pending(rel, name, message_id)``。

        **P10.4 review §1 perf 修**：当 ``self._diff_baseline`` 已有滚动 baseline 时，
        TOOL_START 这一刻不再额外扫盘，直接复用 baseline 当 ``before``——把
        "每个工具调用扫两次"压成 "首次扫一次 + 每个 COMPLETE 扫一次"。COMPLETE 这段
        始终扫一次（``after``），diff 后用 after 覆盖 baseline。

        **P10.4 review §6 修**：snapshot entry 里捕获了 ``message_id`` / ``task_id``，
        即便 ``with bind()`` 已退出（``transport._message_id is None``），仍能用捕获值
        完成 record_pending、不丢归属。

        守卫：

        - call_id 没在 ``_tool_call_snapshots`` 里（TOOL_START 时未压入 / 已 rollback）
          → 不抛、直接返回。
        - ``message_artifacts`` 缺 → 没法 record_pending，直接返回。
        - rel 的 basename 跑 ``_validate_artifact_name`` 校验（与
          ``_normalize_share_rel`` 的 basename 校验同口径）—— 不合法 basename 进 pending
          也是 stale，提前拦。
        """
        entry = self._tool_call_snapshots.pop(call_id, None)
        if entry is None:
            return
        if self.message_artifacts is None:
            return
        before: dict[str, FileStamp] = entry["before"]
        captured_mid: str = entry["message_id"]
        captured_task: Optional[str] = entry.get("task_id")
        after = self.snapshot_writable_rels(task_id=captured_task)
        # 更新滚动 baseline，让下一个 qualifying TOOL_START 复用、跳过扫盘。
        # 仅 share/ 段（不含 tasks，task baseline 与当前 task_id 强耦合、不复用）
        # —— 简化：直接用整个 after 当 baseline，下次 TOOL_START 复用整段即可
        # （task 段 diff 也跑同口径）。
        #
        # **P10.4 review §1.2 修**：只在仍处于 bind 内才写 baseline。晚到的
        # TOOL_COMPLETE（``transport._message_id is None``，bind 已退出）若仍覆盖
        # baseline，下次 bind 的首个 shell/MCP TOOL_START 会跳过扫盘、把这次
        # COMPLETE 到下次 START 之间凭空出现的文件错算成下条消息的"新增"并
        # 错挂归属。两次 bind 之间的盘状态不连续，必须重扫。
        if self.transport._message_id is not None:
            self._diff_baseline = after
        for rel, stamp in after.items():
            if before.get(rel) == stamp:
                continue
            name = rel.rsplit("/", 1)[-1]
            if _validate_artifact_name(name) is not None:
                continue
            self.message_artifacts.record_pending(
                rel=rel,
                name=name,
                message_id=captured_mid,
            )

    def maybe_start_diff_snapshot(
        self, *, tool_name: str, call_id: Optional[str],
    ) -> None:
        """TOOL_START 入口：满足白名单 + 注册表挂上 + message_id 在 → 冻结一份
        before 快照与归属（``message_id``、``task_id``）进 ``_tool_call_snapshots``。

        把原 ``_handle`` 里 5 道条件 + dict 写入聚到一个方法。调用方（``ChahuaTransport._handle``）
        只在 ``_message_id is not None`` 分支调，本方法守 4 道（注册表 / call_id /
        白名单 / message_id 二次确认）。``_diff_baseline`` 复用：非空时直接当 before，
        跳过扫盘；空时立刻 ``snapshot_writable_rels`` 填一份。
        """
        transport = self.transport
        if self.message_artifacts is None:
            return
        if transport._message_id is None:
            return
        if not isinstance(call_id, str) or not call_id:
            return
        if not _should_diff_for_attribution(tool_name):
            return
        if self._diff_baseline is None:
            self._diff_baseline = self.snapshot_writable_rels()
        self._tool_call_snapshots[call_id] = {
            "before": self._diff_baseline,
            "message_id": transport._message_id,
            "task_id": transport._task_id,
        }
