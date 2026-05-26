"""从本地文件夹或 GitHub 导入 persona 包。

一个 persona "包"是一个目录，目录名 = persona 名（如 ``Yvonne``），至少含
``<Name>.md``（SOUL / system prompt）。可选 sidecar：``<Name>.png`` 头像、
``<Name>.toml`` 的 ``[guest]`` 默认（permission 等）、``mcp.json``、``skills/``。

导入时整个目录被复制到 ``user_data_root/chahua/personas/<Name>/``。:func:`chahua.admin.discover_personas`
扩展后会扫到 dir-form persona（``<Name>/<Name>.md``），picker 立刻看到。

**故意拒绝覆盖**：目标 ``<Name>.md``（flat 或 dir form 任一）已存在 → ``PersonaImportError``。
用户改名重导或先删旧的；静默 overwrite 容易把用户改过的 persona 清掉。

GitHub 走 anonymous Contents API（``api.github.com/repos/{o}/{r}/contents/{p}?ref={b}``），
60 req/h 限速一份 persona 通常 <10 files 用不掉多少。token 鉴权 / 私有仓暂不支持。
"""

from __future__ import annotations

import base64
import json
import logging
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ._paths import Paths
from ._persist import write_bytes_atomic
from .admin import PERSONAS_REL_DIR, sanitize_fs_name
from .persona_manifest import PersonaManifestError, parse_persona_manifest_bytes

_log = logging.getLogger(__name__)


# ── 限额 ──────────────────────────────────────────────────────────────────


# 单文件 / 总包尺寸 + 文件数 / 目录深度上限。导入一个 persona 不该拉整个仓库；
# 上限拍在足够留一些 skill 文档的体量，超过明显是手误（指错了根）。
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_BYTES = 8 * 1024 * 1024
_MAX_FILES = 200
_MAX_DEPTH = 6

# 写盘前丢掉的文件 / 目录 —— 没人想把 .git 或编辑器缓存导进来。
_SKIP_NAMES: frozenset[str] = frozenset(
    {".git", ".github", ".vscode", "__pycache__", "node_modules", ".DS_Store"}
)


class PersonaImportError(ValueError):
    """import 失败的统一类型。message 直接 emit 给前端，要够具体能告诉用户怎么修。"""


@dataclass(frozen=True)
class ImportedPersona:
    name: str
    """persona 名（目录名 / .md 文件名 stem）。"""
    persona_rel: str
    """relative path 形如 ``chahua/personas/<Name>/<Name>.md``，可塞 room.toml 的 persona 字段。"""
    has_avatar: bool
    extras: tuple[str, ...]
    """除 ``<Name>.md`` / ``<Name>.png`` 外被一起拷下来的相对路径（mcp.json / skills/.../SKILL.md 等）。
    给前端报告用 —— 让用户知道有哪些 sidecar 文件被保留，目前 runtime 还没消费它们。"""


# ── 公共入口 ──────────────────────────────────────────────────────────────


def import_from_folder(paths: Paths, src: Path) -> ImportedPersona:
    """从本地文件夹导入。``src`` 是 persona 目录（如 ``examples/personas/Yvonne``）。

    流程：定位 ``<Name>.md`` → 校验目标未占 → 拷整目录到
    ``user_data_root/chahua/personas/<Name>/``。

    **manifest 校验时机**：发现 ``persona.toml`` 时**在 ``_write_files`` 之前**对字节
    做 dry-run（:func:`_validate_manifest_pre_write`）。坏 manifest 直接 raise，
    target_dir 不被创建 —— 否则下次重导会撞 "已存在"（plan §"持久化、事件契约"
    第 7 条）。
    """
    if not src.is_dir():
        raise PersonaImportError(f"源目录不存在或不是目录：{src}")
    name = _derive_name(src)
    target_dir = _validate_target(paths, name)

    files = _collect_local_files(src)
    _validate_manifest_pre_write(files)
    _write_files(target_dir, files)
    return _make_result(name, target_dir, files)


def import_from_github(paths: Paths, url: str) -> ImportedPersona:
    """从 GitHub URL 导入（公开仓库 only）。支持两种形态：

    - ``https://github.com/<owner>/<repo>`` —— 整个仓库根作为 persona 目录，name 取 repo 名。
    - ``https://github.com/<owner>/<repo>/tree/<branch>/<path>`` —— 子目录作为 persona 目录。

    manifest 校验时机同 :func:`import_from_folder`：写盘前 dry-run。
    """
    owner, repo, branch, path = _parse_github_url(url)
    name = _derive_name_from_github(repo, path)
    target_dir = _validate_target(paths, name)

    files = _fetch_github_dir(owner, repo, branch, path)
    if not files:
        raise PersonaImportError(
            f"GitHub 目录里没有可导入的文件：{url}"
        )
    _validate_manifest_pre_write(files)
    _write_files(target_dir, files)
    return _make_result(name, target_dir, files)


def _validate_manifest_pre_write(files: list[tuple[str, bytes]]) -> None:
    """如果采集到根级 ``persona.toml``，在落盘之前用 :func:`parse_persona_manifest_bytes`
    做 dry-run；坏 → :class:`PersonaImportError`，让 :func:`_write_files` 永远拿不到
    机会建半成品 target_dir（plan §"承重不变量"第 7 条）。

    无 ``persona.toml`` 时直接 return —— manifest 是可选的（plan §"承重不变量"
    第 5 条），老社区包持 ``<Name>.toml`` legacy sidecar 不受影响。

    **大小写严格小写**：source 有 ``Persona.toml`` / ``PERSONA.TOML`` 等错名 → 拒。
    macOS APFS / Windows NTFS case-insensitive FS 上 ``load_persona_manifest`` 仍能
    找到错名文件，但 Linux ext4 case-sensitive 上看不见 —— 跨平台行为会漂移，导入
    阶段严格拒名是最稳办法。**仅匹配根级**：nested ``skills/foo/persona.toml`` 等
    不被识别为 manifest（runtime 也不读，写盘后是 dead weight）。
    """
    for rel_path, data in files:
        if rel_path == "persona.toml":
            try:
                parse_persona_manifest_bytes(data)
            except PersonaManifestError as e:
                raise PersonaImportError(
                    f"persona.toml 不合法：{e}"
                ) from e
            continue  # 防御：_collect 写法上唯一 rel_path，万一未来漏出多份，全验一遍
        if "/" not in rel_path and rel_path.lower() == "persona.toml":
            raise PersonaImportError(
                f"persona.toml 文件名大小写不对：{rel_path!r}。请改成全小写 persona.toml。"
            )


# ── 名字 / 目标校验 ────────────────────────────────────────────────────────


def _derive_name(src: Path) -> str:
    """persona 名 = 源目录 basename；缺 ``<Name>.md`` 时退化成"目录里唯一一份 .md 文件" stem。"""
    base = src.name
    candidate_md = src / f"{base}.md"
    if candidate_md.is_file():
        return _sanitize_name(base, source=str(src))
    # fallback：兼容用户的目录名与 md 文件名不一致（比如把 Yvonne.md 放进了名为 "persona-yvonne" 的目录）。
    mds = [p for p in src.iterdir() if p.is_file() and p.suffix.lower() == ".md"]
    if len(mds) == 1:
        return _sanitize_name(mds[0].stem, source=str(src))
    raise PersonaImportError(
        f"在 {src} 里找不到 <Name>.md（应有与目录同名的 .md，或目录里恰好一份 *.md）"
    )


def _derive_name_from_github(repo: str, path: str) -> str:
    """GitHub URL 推导 persona 名。子目录优先（``tree/<b>/<dir>`` → ``<dir>``）；仓库根 → repo 名。"""
    base = Path(path).name if path else repo
    return _sanitize_name(base, source=f"github:{repo}/{path or ''}")


def _sanitize_name(raw: str, *, source: str) -> str:
    """走 :func:`chahua.admin.sanitize_fs_name`，错误包成 ``PersonaImportError``。"""
    try:
        return sanitize_fs_name(raw, label=f"persona 名（来自 {source}）")
    except ValueError as e:
        raise PersonaImportError(str(e)) from e


def _validate_target(paths: Paths, name: str) -> Path:
    """目标 dir = ``user_data_root/chahua/personas/<name>/``。同名 flat ``.md`` 已占 → 报错。

    dir 已占由 :func:`_write_files` 的 ``mkdir(exist_ok=False)`` 兜底，省一次 racy stat。
    flat form 不会被 mkdir 自然撞上，所以提前判一下。
    """
    target_dir = paths.user_data_root / PERSONAS_REL_DIR / name
    flat_md = paths.user_data_root / PERSONAS_REL_DIR / f"{name}.md"
    if flat_md.is_file():
        raise PersonaImportError(
            f"persona {name!r} 已存在（flat form：{flat_md}）。请先删除或导入到别的名字。"
        )
    return target_dir


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


# ── GitHub 拉取 ───────────────────────────────────────────────────────────


# `urllib` 默认 User-Agent 是 ``Python-urllib/3.x``；GitHub API 要求显式 UA 否则 403。
_GH_UA = "chahua-persona-importer"
_GH_API = "https://api.github.com"


def _parse_github_url(url: str) -> tuple[str, str, Optional[str], str]:
    """把 GitHub URL 拆成 ``(owner, repo, branch_or_None, path)``。

    branch 为 None 时调用方走 default branch；这里**不**预先 resolve default branch，让
    Contents API 用 ref 缺省（=default branch）省一次往返。
    """
    parsed = urllib.parse.urlparse(url.strip())
    host = (parsed.netloc or "").lower()
    if host not in ("github.com", "www.github.com"):
        raise PersonaImportError(f"不是 github.com 链接：{url}")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise PersonaImportError(f"GitHub URL 缺 owner/repo：{url}")
    owner, repo = parts[0], parts[1]
    # repo 后缀 `.git` 容忍一下（github 网页 URL 不带，但用户从 clone URL 改过来时常带）。
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if len(parts) == 2:
        return owner, repo, None, ""
    kind = parts[2]
    if kind not in ("tree", "blob"):
        raise PersonaImportError(
            f"GitHub URL 第三段必须是 tree/blob：{url}"
        )
    if len(parts) < 4:
        raise PersonaImportError(f"GitHub URL 缺 branch：{url}")
    branch = urllib.parse.unquote(parts[3])
    path = "/".join(urllib.parse.unquote(p) for p in parts[4:])
    return owner, repo, branch, path


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


def _gh_get_contents(
    owner: str, repo: str, branch: Optional[str], path: str
) -> object:
    qs = f"?ref={urllib.parse.quote(branch)}" if branch else ""
    url = f"{_GH_API}/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}{qs}"
    return _gh_get_json(url)


def _gh_get_file_bytes(
    entry: dict, *, owner: str, repo: str, branch: Optional[str]
) -> bytes:
    """优先取 entry 自带的 base64 content；空（>1MB）则走 raw.githubusercontent.com。"""
    encoding = entry.get("encoding")
    content = entry.get("content")
    if encoding == "base64" and isinstance(content, str) and content:
        try:
            return base64.b64decode(content)
        except (ValueError, TypeError) as e:
            raise PersonaImportError(f"GitHub 返回的 base64 解码失败：{e}") from e
    download_url = entry.get("download_url")
    if not isinstance(download_url, str) or not download_url:
        # 兜底自拼 raw URL —— GitHub 偶尔会在 contents API 里漏 download_url（实测罕见，
        # 但 path 含特殊字符时有过报告）。
        ref = branch or "HEAD"
        path = entry.get("path") or entry.get("name")
        if not isinstance(path, str):
            raise PersonaImportError("GitHub 返回缺 path / download_url，无法取文件内容。")
        download_url = (
            f"https://raw.githubusercontent.com/{owner}/{repo}/{urllib.parse.quote(ref)}"
            f"/{urllib.parse.quote(path)}"
        )
    return _gh_get_bytes(download_url)


def _gh_get_json(url: str) -> object:
    raw = _gh_get_bytes(url, accept="application/vnd.github+json")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PersonaImportError(f"GitHub 响应 JSON 解析失败 ({url})：{e}") from e


def _gh_get_bytes(url: str, *, accept: Optional[str] = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _GH_UA})
    if accept:
        req.add_header("Accept", accept)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        # 把常见状态翻译成用户能看懂的句子；其余原样带状态码。
        if e.code == 404:
            raise PersonaImportError(f"GitHub 404 —— 仓库 / 路径 / 分支不存在：{url}") from e
        if e.code == 403:
            # rate-limit 是匿名访问最常见的 403；body 通常含 "API rate limit exceeded"。
            raise PersonaImportError(
                f"GitHub 403 —— 可能撞到匿名 rate limit（60 req/h），稍后再试：{url}"
            ) from e
        raise PersonaImportError(f"GitHub HTTP {e.code}：{url}") from e
    except urllib.error.URLError as e:
        raise PersonaImportError(f"无法连接 GitHub：{e.reason}") from e


# ── 写盘 + 结果 ───────────────────────────────────────────────────────────


def _write_files(target_dir: Path, files: list[tuple[str, bytes]]) -> None:
    """把采集到的文件落到 ``target_dir`` 下。失败 rmtree 回滚（与 create_room 同款）。

    ``mkdir(exist_ok=False)`` 兼当目标占用检查 —— 已存在直接 ``FileExistsError``，外层
    包成 ``PersonaImportError``。每个文件走 :func:`write_bytes_atomic`，写一半被 kill
    不留半截内容。
    """
    try:
        target_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as e:
        raise PersonaImportError(
            f"persona 目录 {target_dir} 已存在。请先删除或导入到别的名字。"
        ) from e
    try:
        for rel, data in files:
            # 防 traversal：``..`` 段拒（即便 _walk_local 上层已过滤）。
            rel_path = Path(rel)
            if rel_path.is_absolute() or any(part == ".." for part in rel_path.parts):
                raise PersonaImportError(f"非法相对路径（含 ..）：{rel}")
            write_bytes_atomic(target_dir / rel_path, data)
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise


def _make_result(
    name: str, target_dir: Path, files: list[tuple[str, bytes]]
) -> ImportedPersona:
    persona_rel = str(PERSONAS_REL_DIR / name / f"{name}.md")
    md_basename = f"{name}.md"
    png_basename = f"{name}.png"
    has_avatar = (target_dir / png_basename).is_file()
    extras = tuple(
        rel for rel, _ in files if rel not in (md_basename, png_basename)
    )
    return ImportedPersona(
        name=name,
        persona_rel=persona_rel,
        has_avatar=has_avatar,
        extras=extras,
    )
