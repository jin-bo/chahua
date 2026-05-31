"""persona 包**文件采集**层（P12.6）—— 本地目录递归读 + GitHub Contents API 递归下载。

从 :mod:`chahua.persona_import` 抽出的纯采集原语：把一个 persona 目录（本地或 GitHub）
读成 ``[(相对路径, bytes), ...]``，受 ``_MAX_*`` 限额约束。不写盘、不碰 provenance ——
落盘 / 原子替换在 :mod:`chahua._persona_fs`，编排 / 校验在 ``persona_import``。

GitHub 低层 HTTP（单资源取字节 / JSON）在 :mod:`chahua.persona_github`；本模块只做
"递归遍历 + 限额累加"。``_collect_local_files`` / ``_fetch_github_dir`` 被 ``persona_import``
re-import 回去（``persona_import._collect_local_files`` 仍可 monkeypatch / 调用）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .persona_github import _gh_get_contents, _gh_get_file_bytes
from .persona_provenance import SOURCE_FILENAME, PersonaImportError

_log = logging.getLogger(__name__)


# ── 限额 ──────────────────────────────────────────────────────────────────


# 单文件 / 总包尺寸 + 文件数 / 目录深度上限。导入一个 persona 不该拉整个仓库；
# 上限拍在足够留一些 skill 文档的体量，超过明显是手误（指错了根）。
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_BYTES = 8 * 1024 * 1024
_MAX_FILES = 200
_MAX_DEPTH = 6

# 写盘前丢掉的文件 / 目录 —— 没人想把 .git 或编辑器缓存导进来。
# ``.chahua-source.json``（provenance）在此 —— 三处采集（_walk_local / GitHub walker /
# installed-dir 哈希遍历经 _collect_local_files）都按 name 跳过它：它是 chahua 内部元
# 数据，不该跟着包被「再导出/再导入」漂走，且更新时由 importer 先于替换重算永远新鲜。
_SKIP_NAMES: frozenset[str] = frozenset(
    {".git", ".github", ".vscode", "__pycache__", "node_modules", ".DS_Store",
     SOURCE_FILENAME}
)


# ── 限额累加器 ─────────────────────────────────────────────────────────────


@dataclass
class _PackBudget:
    """单次 import 的累计状态 —— 文件数 / 总字节。本地 + GitHub walker 共用。

    每加一份文件就 :meth:`add` 一次，超限抛 ``PersonaImportError``。
    """

    files: list[tuple[str, bytes]] = field(default_factory=list)
    total_bytes: int = 0

    def add(self, rel: str, data: bytes) -> None:
        size = len(data)
        if size > _MAX_FILE_BYTES:
            raise PersonaImportError(
                f"文件 {rel} 太大（{size} bytes，单文件上限 {_MAX_FILE_BYTES}）"
            )
        if len(self.files) >= _MAX_FILES:
            raise PersonaImportError(
                f"包含的文件数超过 {_MAX_FILES}，请检查源是否指错了根。"
            )
        if self.total_bytes + size > _MAX_TOTAL_BYTES:
            raise PersonaImportError(
                f"包体总大小超过 {_MAX_TOTAL_BYTES} bytes，请精简 persona 目录。"
            )
        self.total_bytes += size
        self.files.append((rel, data))


# ── 本地文件采集 ───────────────────────────────────────────────────────────


def _collect_local_files(src: Path) -> list[tuple[str, bytes]]:
    """递归读 src，返回 ``[(相对路径, bytes), ...]``。受 ``_MAX_*`` 约束。"""
    budget = _PackBudget()
    _walk_local(src, src, depth=0, budget=budget)
    if not budget.files:
        raise PersonaImportError(f"源目录为空：{src}")
    return budget.files


def _walk_local(root: Path, current: Path, *, depth: int, budget: _PackBudget) -> None:
    """递归把 ``current`` 下的文件加进 ``budget``；``rel`` 全部相对最初的 ``root``。"""
    if depth > _MAX_DEPTH:
        raise PersonaImportError(f"目录嵌套超过 {_MAX_DEPTH} 层，请检查源路径。")
    for child in sorted(current.iterdir()):
        if child.name in _SKIP_NAMES:
            continue
        # symlink 拒收：避免环路 / 跨出源目录；导入要的是确定来源。
        if child.is_symlink():
            _log.info("skip symlink %s", child)
            continue
        if child.is_dir():
            _walk_local(root, child, depth=depth + 1, budget=budget)
        elif child.is_file():
            rel = str(child.relative_to(root))
            budget.add(rel, child.read_bytes())


# ── GitHub 拉取（递归下载整目录；低层 HTTP 在 persona_github）────────────────


def _fetch_github_dir(
    owner: str, repo: str, branch: Optional[str], path: str
) -> list[tuple[str, bytes]]:
    """递归下载 GitHub 仓库 ``path`` 下的所有文件，返回 ``[(相对路径, bytes), ...]``。

    Contents API 对 >1MB 的文件不直接返 base64 content（``content`` 为空、要走 raw URL）。
    我们的 _MAX_FILE_BYTES 是 2MB，常态命中 inline content；超时退到 raw.githubusercontent.com。
    """
    budget = _PackBudget()
    _fetch_github_recursive(
        owner, repo, branch, path, prefix="", depth=0, budget=budget
    )
    return budget.files


def _fetch_github_recursive(
    owner: str,
    repo: str,
    branch: Optional[str],
    api_path: str,
    *,
    prefix: str,
    depth: int,
    budget: _PackBudget,
) -> None:
    if depth > _MAX_DEPTH:
        raise PersonaImportError(f"GitHub 目录嵌套超过 {_MAX_DEPTH} 层。")
    entries = _gh_get_contents(owner, repo, branch, api_path)
    if isinstance(entries, dict):
        # path 指向单个文件而非目录 —— Contents API 在 path 是文件时返单 dict。
        raise PersonaImportError(
            f"GitHub 路径指向单文件而非目录：{api_path or '<repo root>'}"
        )
    for entry in entries:
        name = entry.get("name")
        if not isinstance(name, str) or name in _SKIP_NAMES:
            continue
        etype = entry.get("type")
        rel = f"{prefix}/{name}" if prefix else name
        if etype == "dir":
            sub_api = f"{api_path}/{name}" if api_path else name
            _fetch_github_recursive(
                owner, repo, branch, sub_api,
                prefix=rel, depth=depth + 1, budget=budget,
            )
            continue
        if etype != "file":
            _log.info("skip non-file entry %s (type=%s)", rel, etype)
            continue
        # 提前看 entry.size 拒大文件，省一次下载往返；最终 budget.add 再校验实际 bytes。
        size = entry.get("size", 0)
        if isinstance(size, int) and size > _MAX_FILE_BYTES:
            raise PersonaImportError(
                f"文件 {rel} 太大（{size} bytes，单文件上限 {_MAX_FILE_BYTES}）"
            )
        data = _gh_get_file_bytes(entry, owner=owner, repo=repo, branch=branch)
        budget.add(rel, data)
