"""茶客自动归集（P5.4 + P10.2 + P10.3）：每个 pick 周期末尾扫两条路径，diff 上次扫到
的文件指纹后 emit hint envelope：

- **任务路径**（P5.4）：扫 active task 的 ``artifacts/``、emit ``task_artifact_added``
  + 一帧 ``task_info``。closed task 不扫。
- **房间路径**（P10.2）：扫 ``<room>/share/``、emit ``room_artifact_added``。无 task_info
  伴随（share/ 是房间级公共桌面，不归属任何任务面板）。无论是否有 active task 都扫,
  让非任务房间也能在气泡后挂图片 / 下载链。

从 :mod:`chahua.orchestrator` 抽出。Orchestrator 持一个
:class:`ArtifactDetector` 实例，``_run_ai_chain`` 末尾调 :meth:`detect`；
保留 ``_seen_artifacts`` / ``_kick_detect_new_artifacts`` 转发属性维持测试入口稳定。

设计要点：

- 任务侧初始化只 seed 非终结态任务的 artifacts —— closed task 永远不会
  被 :meth:`detect` 读到（前置过滤），seed 进来纯浪费 readdir 还堆 dict。
- 房间侧初始化恒 seed 整份 share/ 当前快照 —— 房间打开（boot / 切回）时把已有
  share/ 文件当"已知集"，让茶客 / 用户**新增**才发气泡。否则旧文件每次切房都重发一遍。
- 用户走 UI ``attach_artifact`` 上传到 active 任务后，调用方须同步调 :meth:`mark_seen`
  把新文件名记进 ``seen`` —— 否则下一轮 :meth:`detect` 会把它当 ``new_names`` 重发
  ``task_artifact_added`` 且无条件标 ``created_by=guest``。
- 用户走 UI ``upload_file`` 落到 share/ 后，调用方（``server_inbound_io._upload_file``）
  须同步调 :meth:`mark_share_seen`。
- **P10.3 review §1/§2 修补**：rewrite-emit 必须有"文件真的变了"证据，不能仅凭
  pending + file-exists 就触发——否则 ``write_file`` / ``task_write_artifact`` /
  ``replace`` 工具调用**失败 / 取消**时，旧文件还在盘上、pending 也没消费，老路径
  会把那条失败消息的 ``message_id`` 误绑到未变文件上去。引入 ``(mtime_ns, size)``
  指纹基线，rewrite 检测须指纹真变化。
- **P10.3 review §1/§2 修补**（其它）：symlink 不下钻、stat 竞态不污染基线、share/
  路径下未落盘的 pending 末尾 GC、share/ 扫描 ``_SHARE_SCAN_MAX_FILES`` 软上限。
"""

from __future__ import annotations

import logging
import os
import stat as _stat
from pathlib import Path
from typing import Optional

from .events import ChahuaEnvelope, ChahuaEventType, EnvelopeSink, emit_to_sink
from .message_artifacts import MessageArtifactRegistry
from .room import Room
from .task import ARTIFACT_CREATED_BY_GUEST
from .tasks_store import (
    CLOSED_STATUSES,
    TasksStore,
    _validate_artifact_name,
    build_task_info_payload,
)

_log = logging.getLogger(__name__)


# share/ 扫描软上限 —— rglob 是同步 IO，跑在 asyncio 事件循环上；用户偶尔会把整个
# 目录树拖进 share/（截图集合 / git checkout 等）。超额时只 WARN + 截断，前端只
# emit 已扫到的前 N 个；用户至少能看见，且 UI 不会因事件循环阻塞而冻结。
_SHARE_SCAN_MAX_FILES = 5000


# 文件指纹 ``(mtime_ns, size)`` —— P10.3 review §1/§2：仅"pending 有条 + 文件存在"
# 不足以判定 rewrite（失败 / 取消的写盘不动 mtime / size，老路径会误发并把失败
# 消息的 message_id 持久化为合法 originated_message_id）。指纹存 mtime_ns 而非
# ms，避免 macOS 上低精度 mtime 让两次连续写抹平差异。size 是第二维守门，规避
# fs 上 mtime 精度极低时的 false-negative。
FileStamp = tuple[int, int]


def _list_share_rels(share_dir: Optional[Path]) -> dict[str, FileStamp]:
    """枚举 ``share_dir`` 下的合法 artifact rel → 指纹 map（P10.2 / P10.3）。

    返回 ``share/<...>`` 形 rel → ``(mtime_ns, size)``。规则：

    - 用 ``os.walk(share_dir, followlinks=False)`` —— **不**跟随目录 symlink，防止
      ``share/link -> /tmp/outside`` 把房间外文件枚举进来（信息泄露 + ``download_file``
      resolve 后会拒、但 envelope 已经把 rel 喊出去）。
    - 单个文件 ``os.path.islink`` 跳过，并用 ``stat(..., follow_symlinks=False)``
      + ``S_ISREG`` 仅接受普通文件，避免符号链接逃出 share/。
    - 任一路径段以 ``.`` 开头 → 跳（``.DS_Store`` / ``.gitkeep`` / 隐藏目录 /
      ``write_bytes_atomic`` 的 ``.<name>.tmp`` 残骸）。
    - basename 跑 ``_validate_artifact_name`` 校验，失败 → 跳（前缀点 / 含非法字符）。
    - 总文件数超 ``_SHARE_SCAN_MAX_FILES`` → WARN + 截断（防阻塞事件循环）。
    - share_dir 不存在 / 不是目录 → 返空 dict（房间没开 share/ 也安全）。
    - readdir / stat 异常吞掉 + WARN —— 不阻断房间运行。
    """
    if share_dir is None:
        return {}
    if not share_dir.exists() or not share_dir.is_dir():
        return {}
    out: dict[str, FileStamp] = {}
    try:
        for dirpath, dirnames, filenames in os.walk(share_dir, followlinks=False):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fname in filenames:
                if fname.startswith("."):
                    continue
                if _validate_artifact_name(fname) is not None:
                    continue
                full = os.path.join(dirpath, fname)
                try:
                    if os.path.islink(full):
                        continue
                    st = os.stat(full, follow_symlinks=False)
                    if not _stat.S_ISREG(st.st_mode):
                        continue
                except OSError:
                    _log.warning(
                        "share scan: per-file stat failed, skip: %s",
                        full, exc_info=True,
                    )
                    continue
                try:
                    rel_path = os.path.relpath(full, share_dir)
                except ValueError:
                    continue
                segments = rel_path.replace("\\", "/").split("/")
                if not segments or any(seg.startswith(".") for seg in segments):
                    continue
                out["share/" + "/".join(segments)] = (st.st_mtime_ns, st.st_size)
                if len(out) >= _SHARE_SCAN_MAX_FILES:
                    _log.warning(
                        "share scan: hit %d-file cap at %s; remaining files won't auto-attach",
                        _SHARE_SCAN_MAX_FILES, share_dir,
                    )
                    return out
    except OSError:
        _log.warning("scan share dir failed: %s", share_dir, exc_info=True)
    return out


def _stamp_of_artifact(art: dict) -> FileStamp:
    """从 ``tasks_store.list_artifacts`` 返回的 dict 取 ``(mtime_ms, size)`` 指纹。
    任务路径用 mtime_ms（list_artifacts 已经 ``int(st.st_mtime * 1000)``），与
    share/ 路径 mtime_ns 不同精度但 same role —— rewrite 比较只看是否变化、不跨
    路径比较，精度差异无影响。
    """
    return (int(art.get("mtime_ms", 0)), int(art.get("size", 0)))


class ArtifactDetector:
    """track 每 task 上次扫到的 artifact 指纹 + 房间级 share/ rel 指纹，emit 新增 /
    重写 hint。"""

    def __init__(
        self,
        *,
        room: Room,
        tasks_store: Optional[TasksStore],
        share_dir: Optional[Path] = None,
        message_artifacts: Optional[MessageArtifactRegistry] = None,
    ) -> None:
        self.room_id = room.name
        self.tasks_store = tasks_store
        self.share_dir = share_dir
        self.message_artifacts = message_artifacts
        # task_id → 上次扫到的文件名 set（**只读视图，给测试 / orchestrator
        # ``_seen_artifacts`` 属性**）。内部 rewrite 判定走下面的 ``_task_stamps``。
        self.seen: dict[str, set[str]] = {}
        # task_id → {name → (mtime_ms, size)} —— P10.3 修：rewrite 判定要看真变化。
        self._task_stamps: dict[str, dict[str, FileStamp]] = {}
        if tasks_store is not None:
            for t in tasks_store.list_tasks():
                if t.status in CLOSED_STATUSES:
                    continue
                arts = tasks_store.list_artifacts(t.id)
                self.seen[t.id] = {a["name"] for a in arts}
                self._task_stamps[t.id] = {
                    a["name"]: _stamp_of_artifact(a) for a in arts
                }
        # share/ rel → (mtime_ns, size) 指纹基线（P10.2 boot seed / P10.3 真变化检测）。
        self._share_stamps: dict[str, FileStamp] = _list_share_rels(self.share_dir)

    # 兼容 P10.2 测试：``share_seen`` 暴露 keys 视图（只读用 ``in`` / 集合相等）。
    @property
    def share_seen(self) -> set[str]:
        """share/ 已见 rel 集合 —— P10.2 公共契约，membership 校验 / 集合比较用。
        内部存储已升级为 ``_share_stamps``（带指纹）；本属性给测试和老调用方一个
        ``rel in det.share_seen`` 形式的稳定接口。"""
        return set(self._share_stamps.keys())

    def forget(self, task_id: str) -> None:
        """重置某任务的 seen 缓存到空集 —— 给"清空产物"路径用。

        如果不重置，``/clear task`` 后下一轮 :meth:`detect` 会扫到 ``current_names={}``、
        ``prev=老集合``，走 ``removed_names`` 分支，与服务端已 emit 的 ``task_info`` 形成
        多余的二次广播（无害但冗余）。集中在这里而不是让调用方戳 ``self.seen[...] = set()``
        —— 避免私属性外泄到 ``server_inbound_task`` / ``cli`` 两个调用点。
        """
        self.seen[task_id] = set()
        self._task_stamps[task_id] = {}

    def mark_seen(self, task_id: str, name: str) -> None:
        """把单个 artifact 名增量记进 ``seen`` —— 给 ``attach_artifact`` 用（P5.8 §5.4）。

        用户经 UI 上传产物到 active 任务后调用方须同步调本方法，否则下一轮
        :meth:`detect` 把该文件当 ``new_names`` 重发 ``task_artifact_added`` 且无条件
        标 ``created_by=guest``。**必须 ``setdefault`` 增量 add**，不能整组覆盖 ——
        那会让该任务已有的 ``seen`` 旧名丢失，下一轮 detect 把它们重新当新产物再发
        一轮气泡。

        P10.3 review §4 修：指纹基线也要同步写。否则后续茶客对同名产物的失败/取消
        ``task_write_artifact`` 调用会让 rewrite 检测把"current stamp vs None"判为
        变化，把用户上传的文件错误归到那条失败消息的 ``originated_message_id``。
        实际 stat 借 tasks_store 路径；失败按 (0,0) 占位，下次自然 detect 会覆写。
        """
        self.seen.setdefault(task_id, set()).add(name)
        stamp: FileStamp = (0, 0)
        if self.tasks_store is not None:
            try:
                p = self.tasks_store.artifacts_dir(task_id) / name
                st = p.stat()
                # 与 _stamp_of_artifact 同口径（mtime_ms, size），保证 rewrite 检测
                # 比较的两端都是同一精度。
                stamp = (int(st.st_mtime * 1000), st.st_size)
            except OSError:
                pass
        self._task_stamps.setdefault(task_id, {})[name] = stamp

    def mark_share_seen(self, rel: str) -> None:
        """把单个 share/ rel 增量记进 ``share_seen`` —— 给 ``_upload_file`` 用（P10.2）。

        指纹同 ``mark_seen``：上传完已知文件在盘上，但这里不强制 stat（调用方已经
        刚 write_bytes_atomic 完，重 stat 一次为了基线值得）。stat 失败则按 (0,0)
        占位——下次 detect 会自然覆写。

        **P10.4 review §2 修**：非 ``share/`` rel 直接拒（WARN）。``_share_stamps``
        只接受 ``_list_share_rels`` 出口形 ``share/<...>``——一旦写入 ``tasks/<...>``
        或其它形态的 key，detector 永远不会从 ``_list_share_rels`` 看到它，
        ``current_rels - prev_rels`` / GC sweep 都不会经手，垃圾在内存里永久驻留。
        """
        if not isinstance(rel, str) or not rel.startswith("share/"):
            _log.warning("mark_share_seen: 拒非 share/ rel: %r", rel)
            return
        stamp: FileStamp = (0, 0)
        if self.share_dir is not None:
            sub = rel[len("share/"):]
            try:
                st = (self.share_dir / sub).stat()
                stamp = (st.st_mtime_ns, st.st_size)
            except OSError:
                pass
        self._share_stamps[rel] = stamp

    def detect(self, sink: EnvelopeSink, active_task_id: Optional[str]) -> None:
        """两条路径串行扫：任务 artifacts/ + 房间公共桌面 share/。

        末尾跑一次 ``_gc_non_active_task_pending`` —— 切 active task 后非活
        task 的 pending 不会再被 ``_detect_task_artifacts`` 经手，会永远滞留。
        统一在 detect 出口 sweep（P10.4 review §5 修）。
        """
        self._detect_task_artifacts(sink, active_task_id)
        self._detect_share_artifacts(sink)
        self._gc_non_active_task_pending(active_task_id)

    def _detect_task_artifacts(
        self, sink: EnvelopeSink, active_task_id: Optional[str]
    ) -> None:
        """扫 active task 的 ``artifacts/``，emit 茶客新写入 / 真重写的产物。"""
        if active_task_id is None or self.tasks_store is None:
            return
        task = self.tasks_store.get_task(active_task_id)
        if task is None or task.status in CLOSED_STATUSES:
            return
        artifacts = self.tasks_store.list_artifacts(active_task_id)
        current_names = {a["name"] for a in artifacts}
        current_stamps = {a["name"]: _stamp_of_artifact(a) for a in artifacts}
        prev_names = self.seen.get(active_task_id, frozenset())
        prev_stamps = self._task_stamps.get(active_task_id, {})
        new_names = current_names - prev_names
        removed_names = prev_names - current_names
        # P10.3 修：rewrite 必须有"文件真变了"证据 —— 仅 pending 命中 + 文件存在
        # 不够（失败 / 取消的 task_write_artifact 留 pending 但不动 mtime/size，老
        # 路径会把失败消息的 message_id 误绑到老文件上）。指纹 ``(mtime_ms, size)``
        # 必须与 baseline 不同才算重写。
        rewrite_names: set[str] = set()
        if self.message_artifacts is not None:
            prefix = f"tasks/{active_task_id}/artifacts/"
            for rel in list(self.message_artifacts.pending.keys()):
                if not rel.startswith(prefix):
                    continue
                pname = rel[len(prefix):]
                if pname not in current_names or pname in new_names:
                    continue
                # 指纹真的变了才算 rewrite。文件未变 = 写盘失败 / 取消 / no-op。
                if current_stamps.get(pname) != prev_stamps.get(pname):
                    rewrite_names.add(pname)
        emit_names = new_names | rewrite_names
        # 基线指纹同步盘上状态——即便没有 emit / removed，用户编辑文件 mtime 也
        # 要刷新基线，否则下次 detect 反复 false rewrite。
        self.seen[active_task_id] = current_names
        self._task_stamps[active_task_id] = current_stamps
        if not emit_names and not removed_names:
            # 即便无 emit 也要执行 pending GC（失败 / no-op 写盘的 stale 条目）。
            self._gc_task_pending(active_task_id, current_names, emit_names)
            return

        for artifact in (a for a in artifacts if a["name"] in emit_names):
            payload: dict = {
                "task_id": active_task_id,
                "name": artifact["name"],
                "size": artifact["size"],
                "rel": artifact["rel"],
                "created_by": ARTIFACT_CREATED_BY_GUEST,
            }
            if self.message_artifacts is not None:
                mid = self.message_artifacts.consume_pending(rel=artifact["rel"])
                if mid is not None:
                    self.message_artifacts.persist(
                        message_id=mid,
                        rel=artifact["rel"],
                        name=artifact["name"],
                        size=artifact["size"],
                    )
                    payload["originated_message_id"] = mid
            self._emit(sink, ChahuaEventType.TASK_ARTIFACT_ADDED, payload)
        if emit_names:
            self._emit(
                sink,
                ChahuaEventType.TASK_INFO,
                build_task_info_payload(self.tasks_store),
            )
        self._gc_task_pending(active_task_id, current_names, emit_names)

    def _gc_non_active_task_pending(
        self, active_task_id: Optional[str],
    ) -> None:
        """P10.4 review §5 修：清非活 task 残留的 pending 条目。

        ``_gc_task_pending`` 只扫 ``tasks/<active_task_id>/artifacts/`` prefix；用户切换
        active task 后旧 task 的 pending 永远走不到那段 GC。逻辑：

        - 该 pending 的 task_id 与盘上任意一个 task 都不对应（task 删 / 被
          重命名失败 / jsonl 损坏）→ stale，pop。
        - 该 task 在但已 closed → 永远不会进 ``_detect_task_artifacts``，pending 在
          那儿也没用，pop。
        - 该 task 在 / 非 closed / 但 != active —— 留着，等用户切回 active 时
          ``_detect_task_artifacts`` 经手判定。

        active 段已由 ``_gc_task_pending`` 各自处理，这里只扫"非活"段。
        ``message_artifacts`` / ``tasks_store`` 任一缺 → no-op。
        """
        if self.message_artifacts is None or self.tasks_store is None:
            return
        # 一次性建索引：tasks_store 当前所有 task 的 id → status，避免 N × get_task 调用。
        try:
            tasks_index = {
                t.id: t.status for t in self.tasks_store.list_tasks()
            }
        except Exception:
            _log.warning("gc non-active pending: list_tasks failed", exc_info=True)
            return
        stale: list[str] = []
        for rel in self.message_artifacts.pending:
            if not rel.startswith("tasks/"):
                continue
            seg = rel.split("/")
            if len(seg) < 4 or seg[2] != "artifacts":
                # 形态异常的 tasks/ rel 也算 stale —— 不可能匹配任何 task。
                stale.append(rel)
                continue
            tid = seg[1]
            if tid == active_task_id:
                # active 段交给 _gc_task_pending 处理，不在这里动。
                continue
            status = tasks_index.get(tid)
            if status is None:
                # task 不存在了（删除 / 损坏 / 从未存在），永远不会再 detect 到。
                stale.append(rel)
            elif status in CLOSED_STATUSES:
                # closed task 永不进 _detect_task_artifacts，pending 没人能消费。
                stale.append(rel)
            # 非 active / 仍 open：留着，等用户切回 active 再走 active 段 GC。
        for rel in stale:
            self.message_artifacts.pending.pop(rel, None)

    def _gc_task_pending(
        self,
        active_task_id: str,
        current_names: set[str],
        emit_names: set[str],
    ) -> None:
        """P10.3 GC：清理失败 / 未变 task_write_artifact 写入留下的 pending —— 文件
        存在但指纹未变（写盘失败 / no-op）→ pending 是 stale，未来同 rel 真重写
        才能用新的 pending 而不绑这条失败 message_id。"""
        if self.message_artifacts is None:
            return
        prefix = f"tasks/{active_task_id}/artifacts/"
        stale = []
        for rel in self.message_artifacts.pending:
            if not rel.startswith(prefix):
                continue
            pname = rel[len(prefix):]
            if pname not in current_names:
                stale.append(rel)
            elif pname not in emit_names:
                # 文件存在但指纹未变（且没被 new/rewrite 选中）→ 失败 / no-op 写。
                stale.append(rel)
        for rel in stale:
            self.message_artifacts.pending.pop(rel, None)

    def _detect_share_artifacts(self, sink: EnvelopeSink) -> None:
        """扫 ``<room>/share/``，emit ``room_artifact_added`` 新增 / 真重写。

        与任务路径有意不同：

        - **不**伴随 ``task_info`` 帧（share/ 不在任务面板上，没"权威快照"概念）。
        - 不发"移除" hint —— share/ 删文件是用户操作，没有"自动归集"语义；老气泡留在
          历史里也无副作用（点下载时 server 端会回错误）。
        - 不区分 created_by —— 茶客 ``write_file`` 与用户上传走同一 envelope，由
          前端按 ``originated_message_id`` 是否命中分流（命中 → 挂气泡末尾；不命中 →
          系统气泡兜底）。
        """
        if self.share_dir is None:
            return
        current = _list_share_rels(self.share_dir)
        current_rels = set(current)
        prev_rels = set(self._share_stamps)
        new_rels = current_rels - prev_rels
        # P10.3 修：rewrite 仅指纹变化才算（失败 write_file / replace 不动指纹）。
        rewrite_rels: set[str] = set()
        if self.message_artifacts is not None:
            for rel in list(self.message_artifacts.pending.keys()):
                if not rel.startswith("share/"):
                    continue
                if rel not in current_rels or rel in new_rels:
                    continue
                if current.get(rel) != self._share_stamps.get(rel):
                    rewrite_rels.add(rel)
        emit_rels = new_rels | rewrite_rels
        # 基线候选：除了"待 emit"的项，其余沿盘上指纹更新（含 unchanged + 用户
        # 编辑 mtime 仍要刷新，不让下次 detect 反复 false rewrite）。
        new_baseline: dict[str, FileStamp] = {
            rel: stamp for rel, stamp in current.items() if rel not in emit_rels
        }
        for rel in sorted(emit_rels):
            name = rel.rsplit("/", 1)[-1]
            stamp = current.get(rel)
            if stamp is None:
                # 在 list_share_rels 与本次循环间被删（极少见 race）—— 不进 baseline、
                # 不 emit；下次再创建仍能 emit。
                _log.warning(
                    "share artifact disappeared mid-detect: %s", rel,
                )
                continue
            new_baseline[rel] = stamp
            mtime_ns, size = stamp
            payload: dict = {
                "rel": rel,
                "name": name,
                "size": size,
            }
            if self.message_artifacts is not None:
                mid = self.message_artifacts.consume_pending(rel=rel)
                if mid is not None:
                    self.message_artifacts.persist(
                        message_id=mid,
                        rel=rel,
                        name=name,
                        size=size,
                    )
                    payload["originated_message_id"] = mid
            self._emit(sink, ChahuaEventType.ROOM_ARTIFACT_ADDED, payload)
        self._share_stamps = new_baseline
        # P10.3 GC：share/ 路径下未落盘 OR 指纹未变（失败/取消/no-op 写入）的 pending
        # 一律丢。保留它们会让未来同名真写入误绑这条 stale message_id。
        if self.message_artifacts is not None:
            stale = []
            for rel in self.message_artifacts.pending:
                if not rel.startswith("share/"):
                    continue
                if rel not in current_rels:
                    stale.append(rel)
                elif rel not in emit_rels:
                    # 文件在但没被 new/rewrite 选中 → 指纹未变 → 写入失败 / no-op。
                    stale.append(rel)
            for rel in stale:
                self.message_artifacts.pending.pop(rel, None)

    def _emit(
        self, sink: EnvelopeSink, event_type: ChahuaEventType, data: dict
    ) -> None:
        emit_to_sink(sink, ChahuaEnvelope(
            room_id=self.room_id,
            turn_id=None, guest_name=None, message_id=None,
            type=event_type, data=data,
        ))
