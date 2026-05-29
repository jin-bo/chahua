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
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ._paths import Paths
from ._persist import write_bytes_atomic, write_json_atomic
from .admin import PERSONAS_REL_DIR, sanitize_fs_name
from .persona_manifest import PersonaManifestError, parse_persona_manifest_bytes

_log = logging.getLogger(__name__)


# ── provenance（P12.6 消费侧安装元数据）──────────────────────────────────────

# chahua 私有 per-persona sidecar，记「这个 persona 从哪来的」。与作者的可分发
# ``persona.toml`` 严格分层（manifest 跟上游走、provenance 跟本地装机走）。属于
# ``.chahua-seeded`` 同款 chahua 私有 dotfile 家族 —— 永不被当 persona 包内容采集。
SOURCE_FILENAME = ".chahua-source.json"
SOURCE_SCHEMA_VERSION = 1
"""provenance 当前唯一合法 schema_version。缺 / != 1 → read_source WARN+None 降级。"""

# 更新状态枚举（P12.6）。「变没变」只由 commit sha / content_hash 定，version 纯展示。
# - unknown：仅「有可用 provenance 但还没检查」（list 初拉）；check 永不返此值。
# - up_to_date：检查过，与上游一致。
# - update_available：检查到上游更新（含降级——上游版本号更低也照样提示）。
# - source_unavailable：无法更新的统一终态（缺/坏 provenance、文件夹源已删、GitHub 404）。
# - error：临时失败（403 rate-limit / 网络错），重试可能恢复。
STATUS_UNKNOWN = "unknown"
STATUS_UP_TO_DATE = "up_to_date"
STATUS_UPDATE_AVAILABLE = "update_available"
STATUS_SOURCE_UNAVAILABLE = "source_unavailable"
STATUS_ERROR = "error"


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


class PersonaImportError(ValueError):
    """import 失败的统一类型。message 直接 emit 给前端，要够具体能告诉用户怎么修。"""


class _GitHubError(PersonaImportError):
    """带 HTTP 状态码的 GitHub 失败。子类化 :class:`PersonaImportError` —— 现有
    ``except PersonaImportError`` 全部照常捕获、友好 message 不变；新增 ``code`` 让
    :func:`check_persona_update` 区分 404（源已删 → source_unavailable）/ 403（rate
    limit → error）/ 其它（error）。``code is None`` = 网络层错误（URLError）。"""

    def __init__(self, message: str, *, code: Optional[int]) -> None:
        super().__init__(message)
        self.code = code


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


@dataclass(frozen=True)
class GithubProvenance:
    """GitHub 来源细节（provenance 子结构）。"""

    owner: str
    repo: str
    ref: Optional[str]
    """用户给的分支（``tree/<branch>/...``）或 None（仓库根 / 未指定 → default branch）。"""
    path: str
    """仓库内子目录（仓库根为 ``""``）。"""
    commit_sha: Optional[str]
    """导入时刻该 path 的最新 commit sha。「变没变」的权威锚点；取数失败时 None。"""

    def to_dict(self) -> dict:
        return {
            "owner": self.owner,
            "repo": self.repo,
            "ref": self.ref,
            "path": self.path,
            "commit_sha": self.commit_sha,
        }


@dataclass(frozen=True)
class PersonaSource:
    """provenance 内存形态（= ``.chahua-source.json`` 的解析结果）。"""

    name: str
    source_type: str
    """``"github"`` | ``"folder"``。"""
    source_url: str
    """原始 URL / 路径，给前端展示用。"""
    content_hash: str
    """导入时所有采集文件（不含 provenance 自身）的规范化哈希。文件夹源 diff + 本地改动检测共用。"""
    version: Optional[str]
    """导入时刻 ``persona.toml`` 的 version（可选）。**纯展示**，不参与判定。"""
    imported_at: str
    updated_at: str
    github: Optional[GithubProvenance] = None
    source_path: Optional[str] = None
    """文件夹来源的源目录绝对路径（github 来源为 None）。"""
    schema_version: int = SOURCE_SCHEMA_VERSION

    def to_dict(self) -> dict:
        d: dict = {
            "schema_version": self.schema_version,
            "name": self.name,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "content_hash": self.content_hash,
            "version": self.version,
            "imported_at": self.imported_at,
            "updated_at": self.updated_at,
        }
        if self.source_type == "github" and self.github is not None:
            d["github"] = self.github.to_dict()
        if self.source_type == "folder" and self.source_path is not None:
            d["source_path"] = self.source_path
        return d


@dataclass(frozen=True)
class UpdateStatus:
    """:func:`check_persona_update` 的结果 —— 两正交维度 + 纯展示版本号 + detail。"""

    name: str
    status: str
    """:data:`STATUS_*` 之一。"""
    local_modified: bool
    """已安装目录当前哈希 ≠ 存档（本地被改过）。与 ``status`` 正交。"""
    installed_version: Optional[str]
    latest_version: Optional[str]
    detail: str

    def to_row_patch(self) -> dict:
        """合并回「已安装」行的字段（list 行整批被 check 结果覆盖这几项）。"""
        return {
            "status": self.status,
            "local_modified": self.local_modified,
            "installed_version": self.installed_version,
            "latest_version": self.latest_version,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class UpdatedPersona:
    """:func:`update_persona` 的结果。"""

    name: str
    persona_rel: str
    version: Optional[str]


def read_source(persona_dir: Path) -> Optional[PersonaSource]:
    """读 ``persona_dir/.chahua-source.json``。缺文件 → None（静默）；坏 JSON /
    schema_version≠1 / 字段不合法 → WARN + None（**不抛**）。

    **容错档**（承重不变量）：provenance 是增强不是承重，坏了只让该 persona「不可更新」，
    不该让它从「已安装」列表 / picker 消失。与 :func:`load_persona_manifest` 的 fail-fast
    口径有意相反。
    """
    path = persona_dir / SOURCE_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        _log.warning("provenance 读失败，按无来源处理：%s（%s）", path, e)
        return None
    src = _source_from_dict(data)
    if src is None:
        _log.warning("provenance 内容不合法，按无来源处理：%s", path)
    return src


def write_source(persona_dir: Path, source: PersonaSource) -> None:
    """原子写 ``persona_dir/.chahua-source.json``。父目录由调用方（_write_files /
    _install_files）已建好。"""
    write_json_atomic(persona_dir / SOURCE_FILENAME, source.to_dict())


def _write_source_best_effort(persona_dir: Path, source: PersonaSource) -> None:
    """import 路径专用：写 provenance 失败 WARN 不抛。

    **Why**：``_write_files`` 已把 persona 文件落盘且不再回滚；此刻让 write_source 的
    IO 异常冒出会被 ``_run_import`` 报「导入失败」，但 persona 其实已导入、且占了目录名
    （重导会撞「已存在」）。provenance 是增强不是承重 —— 写失败只让该 persona 退化成
    「来源未知」，import 本身仍算成功（与承重不变量「provenance 坏只降级」一致）。
    """
    try:
        write_source(persona_dir, source)
    except OSError as e:
        _log.warning(
            "写 provenance 失败（persona 已导入但未纳管，可重新导入补全）：%s（%s）",
            persona_dir / SOURCE_FILENAME, e,
        )


def _source_from_dict(data: object) -> Optional[PersonaSource]:
    """``dict`` → :class:`PersonaSource`，任何字段不合法 → None（调用方降级）。"""
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != SOURCE_SCHEMA_VERSION:
        return None
    name = data.get("name")
    source_type = data.get("source_type")
    content_hash = data.get("content_hash")
    source_url = data.get("source_url")
    version = data.get("version")
    imported_at = data.get("imported_at")
    updated_at = data.get("updated_at")
    if not (isinstance(name, str) and name):
        return None
    if source_type not in ("github", "folder"):
        return None
    if not (isinstance(content_hash, str) and content_hash):
        return None
    if not isinstance(source_url, str):
        return None
    if version is not None and not isinstance(version, str):
        return None
    if not (isinstance(imported_at, str) and isinstance(updated_at, str)):
        return None
    github: Optional[GithubProvenance] = None
    source_path: Optional[str] = None
    if source_type == "github":
        github = _github_prov_from_dict(data.get("github"))
        if github is None:
            return None
    else:  # folder
        source_path = data.get("source_path")
        if not (isinstance(source_path, str) and source_path):
            return None
    return PersonaSource(
        name=name,
        source_type=source_type,
        source_url=source_url,
        content_hash=content_hash,
        version=version,
        imported_at=imported_at,
        updated_at=updated_at,
        github=github,
        source_path=source_path,
    )


def _github_prov_from_dict(gh: object) -> Optional[GithubProvenance]:
    if not isinstance(gh, dict):
        return None
    owner = gh.get("owner")
    repo = gh.get("repo")
    path = gh.get("path")
    ref = gh.get("ref")
    commit_sha = gh.get("commit_sha")
    if not (isinstance(owner, str) and owner):
        return None
    if not (isinstance(repo, str) and repo):
        return None
    if not isinstance(path, str):  # 仓库根为 ""，合法
        return None
    if ref is not None and not isinstance(ref, str):
        return None
    if commit_sha is not None and not isinstance(commit_sha, str):
        return None
    return GithubProvenance(
        owner=owner, repo=repo, ref=ref, path=path, commit_sha=commit_sha
    )


def _now_iso() -> str:
    """UTC ISO8601（秒级 + ``Z``）。provenance 时间戳口径。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _content_hash(files: list[tuple[str, bytes]]) -> str:
    """采集文件的规范化哈希。对 ``sorted(files, key=relpath)`` 逐项喂
    ``relpath + "\\0" + sha256(bytes).hexdigest() + "\\n"``，relpath 用 ``/`` 归一
    （跨平台稳定），前缀 ``"sha256:"``。``.chahua-source.json`` 已在 ``_SKIP_NAMES`` 不入 files。
    """
    h = hashlib.sha256()
    for rel, data in sorted(files, key=lambda t: t[0].replace("\\", "/")):
        rel_norm = rel.replace("\\", "/")
        h.update(rel_norm.encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(data).hexdigest().encode("ascii"))
        h.update(b"\n")
    return "sha256:" + h.hexdigest()


def _version_from_files(files: list[tuple[str, bytes]]) -> Optional[str]:
    """从采集 files 里挑根级 ``persona.toml`` 读 ``version``。缺 manifest / 缺字段 /
    解析失败 → None（**纯展示、best-effort，永不阻断**）。import / update / github-check 复用。
    """
    for rel, data in files:
        if rel.replace("\\", "/") == "persona.toml":
            try:
                return parse_persona_manifest_bytes(data).version
            except PersonaManifestError:
                return None
    return None


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
    # 先转绝对路径再存 provenance：相对 src 会让 check / update 在 sidecar 重启 / 切换
    # 启动方式后按彼时 cwd 解析，源被误判缺失或落到另一个相对目录。
    src = src.resolve()
    if not src.is_dir():
        raise PersonaImportError(f"源目录不存在或不是目录：{src}")
    name = _derive_name(src)
    target_dir = _validate_target(paths, name)

    files = _collect_local_files(src)
    _validate_manifest_pre_write(files)
    _write_files(target_dir, files)
    now = _now_iso()
    _write_source_best_effort(
        target_dir,
        PersonaSource(
            name=name,
            source_type="folder",
            source_url=str(src),
            source_path=str(src),
            content_hash=_content_hash(files),
            version=_version_from_files(files),
            imported_at=now,
            updated_at=now,
        ),
    )
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
    # commit_sha 取数 best-effort —— files 已下载成功（repo/path 存在、未撞 rate limit），
    # 这步通常成功；偶发失败（mid-import rate-limit）不该让整次导入回退，存 None 降级
    # （用户可重新导入补全 provenance）。与「导入行为零变化」承诺一致。
    commit_sha = _safe_latest_commit_sha(owner, repo, branch, path)
    _write_files(target_dir, files)
    now = _now_iso()
    _write_source_best_effort(
        target_dir,
        PersonaSource(
            name=name,
            source_type="github",
            source_url=url,
            content_hash=_content_hash(files),
            version=_version_from_files(files),
            imported_at=now,
            updated_at=now,
            github=GithubProvenance(
                owner=owner, repo=repo, ref=branch, path=path,
                commit_sha=commit_sha,
            ),
        ),
    )
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


def _gh_latest_commit_sha(
    owner: str, repo: str, ref: Optional[str], path: str
) -> Optional[str]:
    """commits API 取 ``path`` 的最新 commit sha（**1 req**，check 的便宜锚点）。

    ``path`` 为空（仓库根 persona）时省略 path 参数 = 该分支最新 commit。HTTP 错误经
    :class:`_GitHubError` 冒出（调用方分流 404/403）；返回 sha 或 None（响应形态意外）。
    """
    params = {"per_page": "1"}
    if path:
        params["path"] = path
    if ref:
        params["sha"] = ref
    url = f"{_GH_API}/repos/{owner}/{repo}/commits?{urllib.parse.urlencode(params)}"
    data = _gh_get_json(url)
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    sha = first.get("sha")
    return sha if isinstance(sha, str) and sha else None


def _safe_latest_commit_sha(
    owner: str, repo: str, ref: Optional[str], path: str
) -> Optional[str]:
    """:func:`_gh_latest_commit_sha` 的 best-effort 包装 —— 任何 GitHub 失败 WARN + None，
    不让 import 时取基线 sha 失败回退整次导入。"""
    try:
        return _gh_latest_commit_sha(owner, repo, ref, path)
    except PersonaImportError as e:
        _log.warning("导入时未取到 commit sha（provenance 降级，可重新导入）：%s", e)
        return None


def _gh_fetch_one_file(
    owner: str, repo: str, ref: Optional[str], path: str
) -> bytes:
    """取单个文件字节（contents API）。check 时单取上游 ``persona.toml`` 读 version 用。

    ``path`` 必须指向文件；指向目录（contents API 返 list）→ :class:`PersonaImportError`。
    HTTP 错误经 :class:`_GitHubError` 冒出。
    """
    entry = _gh_get_contents(owner, repo, ref, path)
    if not isinstance(entry, dict):
        raise PersonaImportError(f"GitHub 路径不是单文件：{path}")
    return _gh_get_file_bytes(entry, owner=owner, repo=repo, branch=ref)


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
        # 把常见状态翻译成用户能看懂的句子；其余原样带状态码。``_GitHubError`` 子类化
        # PersonaImportError —— import 路径照常捕获、check 路径读 .code 分流。
        if e.code == 404:
            raise _GitHubError(
                f"GitHub 404 —— 仓库 / 路径 / 分支不存在：{url}", code=404
            ) from e
        if e.code == 403:
            # rate-limit 是匿名访问最常见的 403；body 通常含 "API rate limit exceeded"。
            raise _GitHubError(
                f"GitHub 403 —— 可能撞到匿名 rate limit（60 req/h），稍后再试：{url}",
                code=403,
            ) from e
        raise _GitHubError(f"GitHub HTTP {e.code}：{url}", code=e.code) from e
    except urllib.error.URLError as e:
        raise _GitHubError(f"无法连接 GitHub：{e.reason}", code=None) from e


# ── 写盘 + 结果 ───────────────────────────────────────────────────────────


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
    任一步失败 restore bak。manifest dry-run 由调用方 :func:`update_persona` 在调本函数
    **之前**做（坏 manifest → 根本不开始 swap，旧版完好）。

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


def recover_interrupted_persona_updates(paths: Paths) -> None:
    """公开崩溃恢复入口：在**所有**消费 persona 的路径起点调一次，把被中断的原子更新
    （只剩 ``.<name>.bak-…``）还原回 ``<name>/``。session 重建 / persona discovery /
    「已安装」页都调 —— 否则 app 重启后引用该 persona 的房间会在用户打开「已安装」页前就
    加载失败。幂等 + 廉价（一次 iterdir）+ 失败永不阻断。"""
    _recover_interrupted_updates(paths.user_data_root / PERSONAS_REL_DIR)


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


# ── 更新 / 检查 / 删除（P12.6）────────────────────────────────────────────


def _user_persona_dir(paths: Paths, name: str) -> Path:
    """解析 ``user_data_root/chahua/personas/<name>`` —— sanitize + 精确匹配 +
    ``relative_to`` 防穿越。返回路径（**可能不存在**，调用方自行处理）。越界 / 落到
    personas 根本身 → :class:`PersonaImportError`。

    update / delete / check 都以 ``list_installed_personas`` 列出的**目录名**为操作键。
    若 sanitize 改写了入参（如 ``"a/b"`` → ``"a-b"``），说明传入的不是规范目录名 ——
    直接拒，而非静默归一化到某个真目录（否则畸形 / 直连 WS 请求可误删别的 persona）。"""
    safe = _sanitize_name(name, source="persona 名")
    if safe != name:
        raise PersonaImportError(
            f"非法 persona 名（须与已安装目录名精确匹配）：{name!r}"
        )
    literal = paths.user_data_root / PERSONAS_REL_DIR / safe
    # 末段是 symlink → 拒。否则 .resolve() 会跟进链接目标，update/delete/check 会作用到
    # 别的 persona（删别名等于删被指向者）。persona 目录从不该是 symlink（导入只写真目录）。
    if literal.is_symlink():
        raise PersonaImportError(
            f"persona「{name}」是符号链接，拒绝对其更新 / 删除 / 检查"
        )
    root = (paths.user_data_root / PERSONAS_REL_DIR).resolve()
    target = literal.resolve()
    try:
        target.relative_to(root)
    except ValueError as e:
        raise PersonaImportError(f"非法 persona 名（路径穿越）：{name!r}") from e
    if target == root:
        raise PersonaImportError(f"非法 persona 名：{name!r}")
    return target


def _collect_from_source(source: PersonaSource) -> list[tuple[str, bytes]]:
    """按 provenance 重新采集 files。folder → 重走源目录；github → 重拉仓库目录。
    folder 源已删 / github 目录空 → :class:`PersonaImportError`（update 失败原因）。"""
    if source.source_type == "folder":
        if not source.source_path:
            raise PersonaImportError("provenance 缺 source_path，无法重新采集")
        src = Path(source.source_path)
        if not src.is_dir():
            raise PersonaImportError(f"源目录不存在或已移走：{src}")
        return _collect_local_files(src)
    gh = source.github
    if gh is None:
        raise PersonaImportError("provenance 缺 github 信息，无法重新采集")
    files = _fetch_github_dir(gh.owner, gh.repo, gh.ref, gh.path)
    if not files:
        raise PersonaImportError("GitHub 目录里没有可导入的文件")
    return files


def _detect_local_modified(persona_dir: Path, source: PersonaSource) -> bool:
    """已安装目录当前哈希 ≠ 存档哈希 → True。读盘失败 → True（无从判断时按已改动处理，
    宁可拒绝无确认覆盖也不静默丢用户改动）。``.chahua-source.json`` 已在 ``_SKIP_NAMES``，
    重算时不入哈希，与存档口径一致。"""
    try:
        files = _collect_local_files(persona_dir)
    except PersonaImportError:
        return True
    return _content_hash(files) != source.content_hash


def _validate_has_persona_md(files: list[tuple[str, bytes]], *, name: str) -> None:
    """更新前确认重采集的 ``files`` 含 ``<name>.md`` —— **必须精确同名**，不接受「恰好一份
    *.md」兜底。**Why**：与 import 不同，update 的目录名 ``<name>`` 固定，房间 room.toml
    早已存下 ``chahua/personas/<name>/<name>.md`` 这个**确切路径**。上游若把 md 改名（哪怕
    仍只有一份 .md），swap 后 ``<name>.md`` 不存在 → 下次 session 重建解析该路径失败、房间
    加载崩。import 能走「单份 *.md」兜底是因为它**按 md stem 反推目录名**（路径自洽）；
    update 不能。删 / 改名 md 一律拒，旧版完好。"""
    rels = [rel for rel, _ in files]
    if f"{name}.md" in rels:
        return
    raise PersonaImportError(
        f"更新源缺少 {name}.md（房间引用的正是该路径，改名 / 删除会让房间加载失败），"
        f"拒绝替换"
    )


def update_persona(
    paths: Paths, name: str, *, force: bool = False
) -> UpdatedPersona:
    """按 provenance 重新采集 → manifest dry-run → 原子替换 → 重写 provenance。

    无 provenance → :class:`PersonaImportError`（不是导入的 persona）。**本地改动检测**：
    已安装哈希 ≠ 存档且 ``not force`` → :class:`PersonaImportError`（强制更新会丢弃改动）；
    ``force=True`` 才走原子替换。manifest 校验在替换**之前** —— 坏 manifest 旧版完好。
    """
    persona_dir = _user_persona_dir(paths, name)
    source = read_source(persona_dir)
    if source is None:
        raise PersonaImportError(
            f"persona「{name}」不是导入的 / 无来源记录，无法更新"
        )
    if not force and _detect_local_modified(persona_dir, source):
        raise PersonaImportError(
            f"persona「{name}」本地已修改，强制更新会丢弃改动（确认后再更新）"
        )
    files = _collect_from_source(source)
    _validate_manifest_pre_write(files)
    _validate_has_persona_md(files, name=persona_dir.name)
    new_version = _version_from_files(files)
    new_commit_sha = source.github.commit_sha if source.github else None
    if source.source_type == "github" and source.github is not None:
        # 内容已重拉成功 → 再取新 sha best-effort；失败保留旧基线（绝不退化成 None ——
        # 否则下次 check 看到 commit_sha=None 误判 source_unavailable、永久禁更新）。
        fetched_sha = _safe_latest_commit_sha(
            source.github.owner, source.github.repo,
            source.github.ref, source.github.path,
        )
        if fetched_sha is not None:
            new_commit_sha = fetched_sha
    new_source = PersonaSource(
        name=source.name,
        source_type=source.source_type,
        source_url=source.source_url,
        content_hash=_content_hash(files),
        version=new_version,
        imported_at=source.imported_at,  # 首次导入时间保留
        updated_at=_now_iso(),
        source_path=source.source_path,
        github=(
            GithubProvenance(
                owner=source.github.owner, repo=source.github.repo,
                ref=source.github.ref, path=source.github.path,
                commit_sha=new_commit_sha,
            )
            if source.github is not None
            else None
        ),
    )
    _replace_dir_atomic(persona_dir, files, source=new_source)
    persona_rel = str(PERSONAS_REL_DIR / persona_dir.name / f"{persona_dir.name}.md")
    return UpdatedPersona(
        name=persona_dir.name, persona_rel=persona_rel, version=new_version
    )


def check_persona_update(paths: Paths, name: str) -> UpdateStatus:
    """检查单个 persona 是否有上游更新。**status 唯一由内容信号定**（commit sha /
    content_hash），version 纯展示。内部吞掉所有「预期失败」（源已删 / 404 / 403 /
    取版本号失败）映射成 status —— 只在真正意外时才抛（handler 兜底成 error 行）。"""
    persona_dir = _user_persona_dir(paths, name)
    source = read_source(persona_dir)
    if source is None:
        return UpdateStatus(
            name=name, status=STATUS_SOURCE_UNAVAILABLE, local_modified=False,
            installed_version=None, latest_version=None, detail="来源未知，不可更新",
        )
    local_modified = _detect_local_modified(persona_dir, source)
    if source.source_type == "folder":
        return _check_folder(name, source, local_modified)
    return _check_github(name, source, local_modified)


def _check_folder(
    name: str, source: PersonaSource, local_modified: bool
) -> UpdateStatus:
    installed_version = source.version
    src = Path(source.source_path) if source.source_path else None
    if src is None or not src.is_dir():
        return UpdateStatus(
            name=name, status=STATUS_SOURCE_UNAVAILABLE, local_modified=local_modified,
            installed_version=installed_version, latest_version=None,
            detail="源目录不存在或已移走",
        )
    try:
        files = _collect_local_files(src)
    except PersonaImportError as e:
        return UpdateStatus(
            name=name, status=STATUS_SOURCE_UNAVAILABLE, local_modified=local_modified,
            installed_version=installed_version, latest_version=None,
            detail=f"源目录读取失败：{e}",
        )
    if _content_hash(files) == source.content_hash:
        return UpdateStatus(
            name=name, status=STATUS_UP_TO_DATE, local_modified=local_modified,
            installed_version=installed_version, latest_version=None, detail="",
        )
    # 变了：update_available。重走时顺手读源 version 填 latest_version（免费、纯展示）。
    return UpdateStatus(
        name=name, status=STATUS_UPDATE_AVAILABLE, local_modified=local_modified,
        installed_version=installed_version, latest_version=_version_from_files(files),
        detail="",
    )


def _check_github(
    name: str, source: PersonaSource, local_modified: bool
) -> UpdateStatus:
    installed_version = source.version
    gh = source.github
    if gh is None:
        return UpdateStatus(
            name=name, status=STATUS_SOURCE_UNAVAILABLE, local_modified=local_modified,
            installed_version=installed_version, latest_version=None,
            detail="来源信息缺失",
        )
    if gh.commit_sha is None:
        return UpdateStatus(
            name=name, status=STATUS_SOURCE_UNAVAILABLE, local_modified=local_modified,
            installed_version=installed_version, latest_version=None,
            detail="导入时未记录基线版本，请删除后重新导入",
        )
    try:
        latest_sha = _gh_latest_commit_sha(gh.owner, gh.repo, gh.ref, gh.path)
    except _GitHubError as e:
        # 404 = 仓库/路径已删（稳定不可更新）；403/网络/其它 = 临时失败（可重试）。
        status = STATUS_SOURCE_UNAVAILABLE if e.code == 404 else STATUS_ERROR
        detail = "仓库或路径不存在" if e.code == 404 else str(e)
        return UpdateStatus(
            name=name, status=status, local_modified=local_modified,
            installed_version=installed_version, latest_version=None, detail=detail,
        )
    except PersonaImportError as e:
        return UpdateStatus(
            name=name, status=STATUS_ERROR, local_modified=local_modified,
            installed_version=installed_version, latest_version=None, detail=str(e),
        )
    if latest_sha is None:
        return UpdateStatus(
            name=name, status=STATUS_ERROR, local_modified=local_modified,
            installed_version=installed_version, latest_version=None,
            detail="无法获取上游版本信息",
        )
    if latest_sha == gh.commit_sha:
        return UpdateStatus(
            name=name, status=STATUS_UP_TO_DATE, local_modified=local_modified,
            installed_version=installed_version, latest_version=None, detail="",
        )
    # 变了：update_available。尽力取上游 version（独立 try/except，失败不波及 status）。
    latest_version, detail = _gh_best_effort_latest_version(gh)
    return UpdateStatus(
        name=name, status=STATUS_UPDATE_AVAILABLE, local_modified=local_modified,
        installed_version=installed_version, latest_version=latest_version,
        detail=detail,
    )


def _gh_best_effort_latest_version(
    gh: GithubProvenance,
) -> tuple[Optional[str], str]:
    """内容已确认变化后，尽力取上游 ``persona.toml`` 的 version。**失败绝不波及 status**
    —— 404 / 网络 / 解析失败 / 缺字段 → ``(None, "上游版本信息不可用")``。返 ``(version, detail)``。"""
    toml_path = f"{gh.path}/persona.toml" if gh.path else "persona.toml"
    try:
        data = _gh_fetch_one_file(gh.owner, gh.repo, gh.ref, toml_path)
        return parse_persona_manifest_bytes(data).version, ""
    except (PersonaImportError, PersonaManifestError) as e:
        _log.info("上游 version 取数失败（不影响 status）：%s", e)
        return None, "上游版本信息不可用"


def delete_persona(paths: Paths, name: str) -> None:
    """删整个 ``user_data_root`` 下的 dir-form persona 目录（destructive）。

    ``sanitize_fs_name`` + ``relative_to`` 双重防穿越；必须 ``is_dir()``。越界 / 非目录 /
    不存在 → :class:`PersonaImportError`。app_root 内置（只读）天然不在 user_data 根内 →
    解析后 ``is_dir()`` 为假 → 拒。
    """
    target = _user_persona_dir(paths, name)
    if not target.is_dir():
        raise PersonaImportError(f"persona「{name}」不存在或不是目录，无法删除")
    shutil.rmtree(target)
