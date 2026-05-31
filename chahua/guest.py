"""TeaGuest —— 一个茶客（设计文档 §3.1 / §3.5）。

P2.2 起 :meth:`speak` 不再吃 ``on_chunk`` 回调，改吃 envelope ``sink``。茶客自己持
:class:`chahua.transport_bridge.ChahuaTransport`：

- :meth:`speak` 用 :meth:`ChahuaTransport.bind` 包住整段 ``arun`` 调用，``with``
  退出时清 envelope —— set/clear 配对再也不会在某个 except 分支里漏掉。
- 内层 try 走 ``message_end`` 的三态分支（ok / cancelled / error）。message_id 在
  :meth:`speak` 开头分配一次，前端 envelope 和 transcript 落盘 record 共用同一 ID
  —— 前端能把流式 chunk 与持久化 record 串起来。
- :class:`agentao.cancellation.CancellationToken` 通过参数透传给
  ``agent.arun(cancellation_token=…)``；CancelledError 与 KeyboardInterrupt 都按
  cancelled 路径走（Python 3.11+ asyncio 把 SIGINT 翻成 CancelledError；同步 REPL
  input 拿到的是 KeyboardInterrupt——两条都得保 message_start/end 配对）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from agentao import Agentao
from agentao.cancellation import CancellationToken
from agentao.llm import LLMClient
from agentao.mcp import load_mcp_config
from agentao.mcp.config import expand_env_vars
from agentao.paths import user_root
from agentao.permissions import PermissionEngine

from .agent_run_tools import StartAgentRun, register_agent_run_tools
from .debug_recorder import NOOP_RECORDER, TurnRecorder
from .events import (
    STATUS_CANCELLED,
    STATUS_ERROR,
    STATUS_OK,
    ChahuaEventType,
    EnvelopeSink,
    new_message_id,
)
from .mcp_thread import ThreadedMcpClientManager
from .message_artifacts import MessageArtifactRegistry
from .permissions import apply_permission_mode
from .persona_assets import PersonaAssets
from .handoff_tools import register_handoff_tools
from .image_input import resolve_images
from .room import Message, Room
from .task_tools import register_task_tools
from .tasks_store import TasksStore
from .transport_bridge import ChahuaTransport

_log = logging.getLogger(__name__)


# P12.5：仅用于「引用了变量但宿主未设置」的 WARN 扫描；真正的 substitution 仍走
# agentao.expand_env_vars（与 .agentao/mcp.json 文件加载同一套语义，不漂移）。这里复刻
# agentao mcp/config.py 的 ``$VAR`` / ``${VAR}`` 形态，故意不 import 私有 _ENV_VAR_RE ——
# 私有符号无稳定承诺，几行正则比耦合划算。
_ENV_REF_RE = re.compile(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
# 展开作用于这两类 str→str 映射字段；``args`` 是 list[str]，单独走。
_MCP_EXPAND_DICT_FIELDS = ("env", "headers")


def _expand_one(server_name: str, where: str, value: Any) -> Any:
    """对单个值做 ``$VAR`` / ``${VAR}`` 展开；非 str 原样返回。

    引用了变量名的值才走 agentao expand_env_vars；引用的变量在宿主 env 里**未设置或为
    空串**时 WARN 一次（两者都展开成空串 —— 空 API key 会让 MCP server 运行时静默失败，
    WARN 是必要诊断）。同名变量在一个值里多次出现只 WARN 一次。缺变量本身不报错、不阻断
    （与房间级容错档一致）。
    """
    if not isinstance(value, str):
        return value
    # findall 与 agentao expand_env_vars 同正则；每项 (braced, bare) 仅一组非空。
    refs = _ENV_REF_RE.findall(value)
    if not refs:
        return value
    warned: set[str] = set()
    for braced, bare in refs:
        var = braced or bare
        if var not in warned and not os.environ.get(var):
            warned.add(var)
            _log.warning(
                "MCP server %r 的 %s 引用的环境变量 ${%s} 未设置或为空，将展开为空串",
                server_name, where, var,
            )
    return expand_env_vars(value)


def _expand_mcp_server(server_name: str, cfg: dict) -> dict:
    """对单个 MCP server cfg 的 ``env`` / ``headers`` / ``args`` 做变量插值（P12.5）。

    返回**新 dict**，不原地改 ``cfg`` —— 调用方传进来的 persona / 房间级配置是
    :func:`chahua.persona_assets.discover_assets` 的 raw 快照，room snapshot 投影还要
    用它的字面 ``${VAR}``（绝不下发展开后真值）。
    """
    out = dict(cfg)
    for field in _MCP_EXPAND_DICT_FIELDS:
        val = out.get(field)
        if isinstance(val, dict):
            out[field] = {
                k: _expand_one(server_name, f"{field}.{k}", v) for k, v in val.items()
            }
    args = out.get("args")
    if isinstance(args, list):
        out["args"] = [
            _expand_one(server_name, f"args[{i}]", a) for i, a in enumerate(args)
        ]
    return out


def _merged_mcp_configs(
    working_directory: Path,
    persona_servers: Optional[dict],
    room_level_servers: Optional[dict] = None,
) -> dict:
    """Overlay 三档 MCP 来源 → 单一 dict 喂 :class:`agentao.Agentao`：

    1. agentao 文件加载（``<workdir>/.agentao/mcp.json`` + ``~/.agentao/mcp.json``）。
    2. persona sidecar mcp（trust 门控已在 :func:`chahua.session._build_guests` 处生效 ——
       未受信任时 ``persona_servers`` 是 ``None``）。
    3. 房间级 ``[[guest.extra_mcp_servers]]`` —— 用户手写在自己 room.toml 里，等价用户
       意图自动信任，无 trust 清单。同名时**覆盖** persona / 文件加载层。

    设计 §2.4：trust 门控不对称（persona 来路可能任意可执行；房间级是用户当下意图）；
    覆盖顺序由"哪一层更接近用户当下意图"决定。

    **P12.5 env 插值**：persona / 房间两层的 ``env`` / ``headers`` / ``args`` 走
    :func:`_expand_mcp_server`，让密钥从可分发的 persona 包 / 用户 toml 里只留变量名引用。
    文件加载层（``configs``）**不**在此重复展开 —— ``load_mcp_config`` 已展开过，每层恰好
    展开一次。
    """
    try:
        configs = load_mcp_config(
            project_root=working_directory,
            user_root=user_root(),
        )
    except Exception:
        _log.exception("Failed to load MCP config for %s", working_directory)
        configs = {}
    merged = dict(configs)
    if persona_servers:
        for name, cfg in persona_servers.items():
            cfg = _expand_mcp_server(name, cfg)
            if name in merged:
                _log.info("persona MCP server %r overrides file-loaded config", name)
            merged[name] = {**cfg, "trust": cfg.get("trust", True)}
    if room_level_servers:
        for name, cfg in room_level_servers.items():
            cfg = _expand_mcp_server(name, cfg)
            if name in merged:
                _log.info(
                    "room-level MCP server %r overrides persona / file-loaded config", name
                )
            # 房间级自动信任 —— 用户在自己 room.toml 里手写 == 用户意图。
            merged[name] = {**cfg, "trust": True}
    return merged


class TeaGuest:
    """一个茶客。持 :class:`Room` 引用 —— 写入 transcript / 合成 message_end 都从这里。"""

    def __init__(
        self,
        *,
        name: str,
        persona_md: str,
        llm_client: LLMClient,
        working_directory: Path,
        room: Room,
        tasks_store: TasksStore,
        permission: str = "read-only",
        assets: Optional[PersonaAssets] = None,
        room_level_mcp: Optional[dict[str, dict]] = None,
        recorder: TurnRecorder = NOOP_RECORDER,
        message_artifacts: Optional[MessageArtifactRegistry] = None,
        share_dir: Optional[Path] = None,
    ) -> None:
        self.name = name
        self.room = room
        self.working_directory = Path(working_directory)
        self.working_directory.mkdir(parents=True, exist_ok=True)
        # P13：视觉图像输入懒读落点。``share_dir`` 是 ``<room>/share/`` 实路径（transport
        # 也吃同一份做 artifact diff）—— speak() 调 ``resolve_images(self._share_dir, ...)``
        # 把本轮用户图从 share/ 现读现传 arun(images=)。``None`` = 裸构 / 未装配 → 视觉退化
        # 为无图（resolve_images 兜底返 []，绝不炸 speak）。
        self._share_dir = share_dir
        # P6.1：speak() 在 message_start/end 处调 recorder；NOOP_RECORDER = 测试 /
        # [debug] enabled=false 时不动盘。也透传到 transport.bind() 给工具事件 hook。
        self._recorder = recorder
        # P11.2 C11：``spawn_agent_run(s)`` 工具入口槽位。session 装配期是 None ——
        # ``register_agent_run_tools`` 用闭包 ``lambda: self.start_agent_run`` 注册，
        # 工具实例每次 ``execute`` 现取一次 instance attr。server ``_attach_runtime_state``
        # 装回调进来（切房 re-attach 直接重写本槽位）—— 闭包逐次读 attr，立刻见新值。
        self.start_agent_run: Optional[StartAgentRun] = None

        # transport 终身绑定 (room_id, guest_name)；per-speak 的 (sink, turn_id,
        # message_id) 通过 bind() 临时设。room_id 暂用 room.name —— P4 加 [room].id
        # 后由 cli 显式传，这里换个 helper 即可。
        self._transport = ChahuaTransport(
            room_id=room.name,
            guest_name=name,
            message_artifacts=message_artifacts,
            # P10.4：shell / MCP 工具 args 推不出落盘路径，transport 自己拿 share/
            # 实路径 + tasks_store 做"前后 diff"回填 pending。share_dir 必须是
            # ``<room>/share/`` 实路径，不能传 guest cwd 的软链——``_list_share_rels``
            # 用 ``os.walk(followlinks=False)``，软链会被当成空目录。
            share_dir=share_dir,
            tasks_store=tasks_store,
        )

        permission_engine = PermissionEngine(
            project_root=self.working_directory,
            rules=[],
            loaded_sources=[],
        )

        if assets is not None:
            assets.materialize_skills(self.working_directory)

        mcp_manager = None
        mcp_configs = _merged_mcp_configs(
            self.working_directory,
            assets.mcp_servers if assets is not None else None,
            room_level_servers=room_level_mcp,
        )
        if mcp_configs:
            # chahua constructs TeaGuest inside the websocket event loop.  The
            # default agentao sync MCP manager nests run_until_complete there,
            # so use a background-loop manager and inject it as already-owned.
            # Keep agentao's original file-loaded + persona-extra merge order.
            mcp_manager = ThreadedMcpClientManager(mcp_configs)
            try:
                mcp_manager.connect_all()
            except Exception:
                _log.exception("%s: MCP connection failed", self.name)
                mcp_manager.disconnect_all()
                mcp_manager = None

        self.agent = Agentao(
            working_directory=self.working_directory,
            llm_client=llm_client,
            project_instructions=persona_md,
            transport=self._transport,
            permission_engine=permission_engine,
            mcp_manager=mcp_manager,
        )

        # tool_runner 是 Agentao.__init__ 里装的，read-only 拦截必须在那之后才能套上。
        apply_permission_mode(self.agent, permission)

        # 工具实例读 transport.current_task_id snapshot —— 与 message_* envelope 同源，
        # 避免老任务的 propose 错挂到新 active（docs §6.3 协议层不可能 race）。
        register_task_tools(self.agent, tasks_store=tasks_store, transport=self._transport)
        # P7.4：handoff propose 工具（propose_delegate / review / panel）—— 与 task
        # 工具并列注册。propose_review 要 room 把 reviewee 名解析成 message_id。
        register_handoff_tools(self.agent, transport=self._transport, room=room)
        # P11.2 C11：``spawn_agent_run`` / ``spawn_agent_runs`` —— 茶客主动调度并发
        # 后台 run。getter 闭合 ``self.start_agent_run`` instance attr，让 server
        # 切房 re-attach 后同一 Tool 实例下次 call 自动看见新 runtime（闭合契约）。
        register_agent_run_tools(
            self.agent,
            source_guest=self.name,
            get_start_agent_run=lambda: self.start_agent_run,
        )

    @property
    def permission(self) -> str:
        """当前权限模式（每次从 agent.permission_engine 拉，避免与运行时切换脱节）。"""
        return self.agent.permission_engine.active_mode.value

    def set_task_proposal_hook(self, hook) -> None:
        """转发到 :meth:`ChahuaTransport.set_task_proposal_hook`（P8.3）。

        session 装配期由 Orchestrator 注入 ``_intercept_task_proposal`` —— 托管会话内
        管理者的 delegate / panel 提议经此 hook 直接入队、不渲采纳卡（docs §5.1）。
        """
        self._transport.set_task_proposal_hook(hook)

    def describe_capabilities(self) -> dict:
        """只读快照：本茶客 agent 注册的 tools + 可用 skills + 当前权限模式。

        ``/tools`` / ``/skills`` slash 查询用 —— server ``_inbound_list_guest_caps``
        与 CLI REPL 共调这一个投影。tools / 可用 skills 都是 ``__init__`` 时一次注册 /
        扫描的**静态**集合，post-init 不变；本方法纯读、不动 agent 状态。
        """
        tools = [
            {
                "name": t.name,
                "description": t.description or "",
                "is_read_only": bool(t.is_read_only),
            }
            for t in sorted(self.agent.tools.list_tools(), key=lambda t: t.name)
        ]
        skill_mgr = self.agent.skill_manager
        skills = [
            {
                "name": name,
                "description": skill_mgr.available_skills.get(name, {}).get(
                    "description", ""
                )
                or "",
            }
            for name in sorted(skill_mgr.list_available_skills())
        ]
        return {
            "guest": self.name,
            "permission": self.permission,
            "tools": tools,
            "skills": skills,
        }

    def inflight_snapshot(self) -> Optional[dict]:
        """本茶客当前正在流式输出的消息快照；不在 ``speak()`` 中 → ``None``。

        P9 切回房间重发快照用（见 :meth:`ChahuaTransport.inflight_snapshot`）。
        """
        return self._transport.inflight_snapshot()

    # ── 主入口 ────────────────────────────────────────────────────────────

    async def speak(
        self,
        context_message: str,
        *,
        turn_id: str,
        sink: EnvelopeSink,
        cancellation_token: Optional[CancellationToken] = None,
        task_id: Optional[str] = None,
        record_debug: bool = True,
        images_rel: tuple[str, ...] = (),
    ) -> Optional[Message]:
        """让茶客说一句话。

        返回追加到 transcript 的 :class:`Message`（成功）或 ``None``（异常 / 取消）。
        ``message_start`` / ``message_end`` 由本函数合成 —— 保证一对一：

        - 成功：``message_end(status=ok, data={text}, seq=msg.seq)`` —— transcript
          里的 ``Message.message_id`` 与 envelope ``message_id`` 一致。
        - 取消：``message_end(status=cancelled, data={partial_text})``；不写 transcript；
          重抛让 orchestrator 决定是否中止后续。
        - 失败：``message_end(status=error, data={partial_text, error})``；不写 transcript;
          异常被吞，函数返回 ``None`` —— orchestrator 让链跑下去（与 P1 一致）。

        ``task_id``：orchestrator 在 turn_start 时 snapshot 的 active_task_id。本轮所有
        message_* envelope 的 data 都带这个 tag；transcript 落盘也带。``None`` = 房间级闲聊。

        P6.1：``record_message_start`` 在 envelope MESSAGE_START 之后调（speak_prompt
        从形参拿）；``record_message_end`` 单点在外层 ``finally``，``status`` 悲观初始化
        为 ``STATUS_ERROR``，三条出口路径（return msg / raise / return None）共享 finally。

        P11 C4：``record_debug=False`` 让 bg run wrapper 调用方临时绕开 ``TurnRecorder`` ——
        本次 speak 整段把 ``self._recorder`` 替换为 :data:`NOOP_RECORDER`（bind / record
        全部落空），避免 bg message 串进前台并发的 debug turn。代价：bg run 不进 debug 视图
        （docs/P11 §「TurnRecorder 串台修复」）。``speak`` 是顺序复用、不并发，外层 finally
        无条件复原原 recorder。
        """
        message_id = new_message_id()
        # 悲观初始化：finally 路径永远拿到合法 status；ok 在 return msg 前覆写，
        # cancel 在 except CancelledError 里覆写，error 走默认。
        status: str = STATUS_ERROR
        msg: Optional[Message] = None
        # P11 C4：bg run wrapper 传 ``record_debug=False`` 时整段切到 NOOP_RECORDER。
        # ``self._recorder`` 是实例 attr 而非 closure，bind 与三处 record_* 都读 self.
        original_recorder = self._recorder
        if not record_debug:
            self._recorder = NOOP_RECORDER
        try:
            with self._transport.bind(
                sink=sink, turn_id=turn_id, message_id=message_id, task_id=task_id,
                recorder=self._recorder,
            ):
                # ``capture_prompts=True`` 时把 context_message 搭车 MESSAGE_START.data；
                # 老前端忽略未知字段、``schema_version`` 不 bump。关闭时整字段不写 key
                # （区分"prompt 捕获关了" vs "prompt 是空"，docs §不变量）。
                start_data: dict = {}
                if self._recorder.capture_prompts:
                    start_data["speak_prompt"] = context_message
                self._transport.emit_chahua(ChahuaEventType.MESSAGE_START, start_data)
                self._recorder.record_message_start(
                    message_id=message_id, guest=self.name,
                    speak_prompt=context_message,
                    # P13：rel-only 落 debug 便于回放本轮带图；bytes 不进取证。
                    images_rel=images_rel,
                )
                try:
                    # P13：本轮用户图懒读现传。``resolve_images`` 从 ``share/`` 现读现传
                    # base64，绝不入 transcript / envelope。视觉模型直接看像素；非视觉
                    # 模型由 agentao reactive 回退换成 ``- share/x.png`` 文本引用重试 ——
                    # chahua 不做能力探测，逐茶客 model 各自生效。``images_rel`` 空 →
                    # resolved 为 ``[]``，arun 行为与 P13 前完全一致。
                    resolved = resolve_images(self._share_dir, images_rel)
                    text = await self.agent.arun(
                        context_message,
                        cancellation_token=cancellation_token,
                        images=resolved or None,
                    )
                except (asyncio.CancelledError, KeyboardInterrupt):
                    status = STATUS_CANCELLED
                    self._emit_failure(STATUS_CANCELLED, "cancelled")
                    raise
                except Exception as e:
                    _log.exception("%s.speak() failed", self.name)
                    # status 保持 STATUS_ERROR
                    self._emit_failure(STATUS_ERROR, str(e))
                    return None

                msg = self.room.append(
                    self.name, text, message_id=message_id, task_id=task_id,
                )
                # message_end.data.text 是兜底字段（前端可以靠它做一次性渲染，不用拼 chunk）。
                self._transport.emit_chahua(
                    ChahuaEventType.MESSAGE_END,
                    {"text": text},
                    status=STATUS_OK,
                    seq=msg.seq,
                )
                status = STATUS_OK
                return msg
        finally:
            # finally 在 with 退出后；recorder 与 transport 绑定状态解耦，bind 已复位
            # 时调用更显安全。``record_message_end`` 自带 try/except —— recorder 失败
            # 永远不阻断 speak。
            self._recorder.record_message_end(
                message_id=message_id, status=status,
                seq=msg.seq if msg is not None else None,
            )
            # P11 C4：无条件复原原 recorder —— 即便上面 record_message_end 走 NOOP 路径
            # 也保证 self._recorder 不会被永久落成 NOOP（下次 speak 起来走前台路径正常）。
            self._recorder = original_recorder

    def _emit_failure(self, status: str, error: str) -> None:
        """两条失败分支（cancelled / error）共用的 message_end emit。"""
        self._transport.emit_chahua(
            ChahuaEventType.MESSAGE_END,
            {"partial_text": self._transport.partial_text, "error": error},
            status=status,
        )

    def close(self) -> None:
        """释放 agent 资源。多次调用安全。"""
        self.agent.close()
