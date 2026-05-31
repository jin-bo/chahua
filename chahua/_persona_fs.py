"""persona 包**写盘 / 原子替换 / 崩溃恢复**层（P12.6）。

从 :mod:`chahua.persona_import` 抽出的纯文件系统原语，承重不变量集中在此：

- **create 路径**（import）：:func:`_write_files` 建新目录 + 写 + 失败 rmtree 回滚。
- **update 路径**：:func:`_replace_dir_atomic` 全量替换 + 原子 swap，绝不原地改写 ——
  半个 persona 比旧 persona 危险得多。
- **崩溃恢复**：:func:`_recover_interrupted_updates` 把 swap 中途被打断、只剩在
  ``.<name>.bak-…`` 里的旧版还原回 ``<name>/``，并清其余工作残骸。

采集（读源）在 :mod:`chahua._persona_collect`；provenance 数据模型 / 写入在
:mod:`chahua.persona_provenance`；编排 / manifest 校验在 ``persona_import``。
本层不解析 manifest、不取 provenance 内容（``_replace_dir_atomic`` 只负责把调用方
已构造好的 ``source`` 与文件**同次换入**）。
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
from pathlib import Path

from ._persist import write_bytes_atomic
from .persona_provenance import PersonaImportError, PersonaSource, write_source

_log = logging.getLogger(__name__)


def _validate_rels(files: list[tuple[str, bytes]]) -> None:
    """防 traversal：``..`` 段 / 绝对路径拒（即便 _walk_local 上层已过滤）。"""
    for rel, _ in files:
        rel_path = Path(rel)
        if rel_path.is_absolute() or any(part == ".." for part in rel_path.parts):
            raise PersonaImportError(f"非法相对路径（含 ..）：{rel}")


def _write_files_into(dir_path: Path, files: list[tuple[str, bytes]]) -> None:
    """把 files 写到**已存在**的 ``dir_path``（不建目录、不回滚、不校验 —— 调用方负责）。
    每个文件走 :func:`write_bytes_atomic`，写一半被 kill 不留半截内容。"""
    for rel, data in files:
        write_bytes_atomic(dir_path / Path(rel), data)


def _write_files(target_dir: Path, files: list[tuple[str, bytes]]) -> None:
    """create 路径（import）：建新目录 + 写 + 失败 rmtree 回滚（与 create_room 同款）。

    ``mkdir(exist_ok=False)`` 兼当目标占用检查 —— 已存在直接 ``FileExistsError``，外层
    包成 ``PersonaImportError``。
    """
    _validate_rels(files)
    try:
        target_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as e:
        raise PersonaImportError(
            f"persona 目录 {target_dir} 已存在。请先删除或导入到别的名字。"
        ) from e
    try:
        _write_files_into(target_dir, files)
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise


def _replace_dir_atomic(
    target_dir: Path, files: list[tuple[str, bytes]], *, source: PersonaSource
) -> None:
    """update 路径：原子替换**已存在**的 ``target_dir``，绝不原地改写。

    ``personas/.<Name>.update-<pid>/`` 写新内容 + provenance（**provenance 进 tmp，与
    内容同次换入** —— 不分两步写，避免 swap 成功但 provenance 写失败的中间态）→
    ``rename(target → .<Name>.bak-<pid>)`` → ``rename(tmp → target)`` → 成功删 bak；
    任一步失败 restore bak。manifest dry-run 由调用方 :func:`persona_import.update_persona`
    在调本函数**之前**做（坏 manifest → 根本不开始 swap，旧版完好）。

    **Why**：更新目标已存在，必须保证「替换中途被 kill / rename 失败」时旧版完好无损 ——
    半个 persona 比旧 persona 危险得多。
    """
    _validate_rels(files)
    parent = target_dir.parent
    # tmp/bak 名带 pid + 随机 token：``update_persona`` 经 ``asyncio.to_thread`` 跑，同
    # 进程内两次并发更新同一 persona（双击 / 两帧）会共享 pid —— 仅用 pid 会撞名 + 让开头
    # 的 rmtree 误删对方的工作目录。token 保证每次更新独占自己的 tmp/bak。
    token = f"{os.getpid()}-{secrets.token_hex(3)}"
    tmp_dir = parent / f".{target_dir.name}.update-{token}"
    bak_dir = parent / f".{target_dir.name}.bak-{token}"
    # 1) 写新内容 + provenance 到 tmp。
    try:
        tmp_dir.mkdir(parents=True, exist_ok=False)
        _write_files_into(tmp_dir, files)
        write_source(tmp_dir, source)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    # 2) swap：旧版挪 bak（target 随即不存在）→ 新版就位。失败 restore。
    try:
        os.replace(target_dir, bak_dir)
    except OSError:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    try:
        os.replace(tmp_dir, target_dir)
    except OSError:
        os.replace(bak_dir, target_dir)  # 还原旧版
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    # 3) 成功，删 bak。
    shutil.rmtree(bak_dir, ignore_errors=True)


# .<name>.update-<pid>-<hex6> / .<name>.bak-<pid>-<hex6>（_replace_dir_atomic 的工作目录）。
_UPDATE_WORKDIR_RE = re.compile(
    r"^\.(?P<name>.+)\.(?P<kind>update|bak)-(?P<pid>\d+)-[0-9a-f]{6}$"
)


def _recover_interrupted_updates(personas_root: Path) -> None:
    """崩溃恢复 + 残骸清理。:func:`_replace_dir_atomic` 在 ``rename(target→bak)`` 与
    ``rename(tmp→target)`` 之间被 SIGKILL / 断电打断时，旧版只剩在 ``.<name>.bak-…/``
    （dot-dir，listing 跳过）—— persona 看起来「消失」了。这里把这种孤儿 bak 还原回
    ``<name>/``，并清掉其余工作残骸。

    **只碰 pid ≠ 当前进程的工作目录**：当前进程 pid 的 ``.update-`` / ``.bak-`` 可能是本
    进程正在跑的并发更新（``update_persona`` 走 ``asyncio.to_thread``，bak/tmp 名嵌 pid），
    绝不能抢；异进程 pid 的必是上次运行崩溃 / 收尾失败的残骸，安全处理。pid 复用极罕见且
    最坏只是跳过恢复（无数据损失）。失败永不阻断 —— 仅 WARN。
    """
    if not personas_root.is_dir():
        return
    me = os.getpid()
    for entry in sorted(personas_root.iterdir()):
        m = _UPDATE_WORKDIR_RE.match(entry.name)
        if m is None or not entry.is_dir() or int(m.group("pid")) == me:
            continue
        target = personas_root / m.group("name")
        if m.group("kind") == "bak" and not target.exists():
            # swap 中途崩溃，旧版只剩在 bak → 还原回 target。
            try:
                os.replace(entry, target)
            except OSError as e:
                _log.warning("恢复中断的 persona 更新失败 %s → %s：%s", entry.name, target.name, e)
            continue
        # 其余残骸（target 已就位的 bak / 任意 update tmp）→ 清掉。
        shutil.rmtree(entry, ignore_errors=True)
