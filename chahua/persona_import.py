"""从本地文件夹或 GitHub 导入 persona 包 + 更新 / 检查 / 删除生命周期（P12.6）。

一个 persona "包"是一个目录，目录名 = persona 名（如 ``Yvonne``），至少含
``<Name>.md``（SOUL / system prompt）。可选 sidecar：``<Name>.png`` 头像、
``<Name>.toml`` 的 ``[guest]`` 默认（permission 等）、``mcp.json``、``skills/``。

导入时整个目录被复制到 ``user_data_root/chahua/personas/<Name>/``。:func:`chahua.admin.discover_personas`
扩展后会扫到 dir-form persona（``<Name>/<Name>.md``），picker 立刻看到。

**故意拒绝覆盖**：目标 ``<Name>.md``（flat 或 dir form 任一）已存在 → ``PersonaImportError``。
用户改名重导或先删旧的；静默 overwrite 容易把用户改过的 persona 清掉。

本模块是「import / update / check / delete」编排层；低层原语已分四块抽出：
- :mod:`chahua.persona_provenance` —— provenance 数据模型 / 读写 / 内容哈希 / 状态词表 /
  基类异常 :class:`PersonaImportError`。
- :mod:`chahua.persona_github` —— GitHub Contents API 低层 HTTP（单资源取字节 / JSON）。
- :mod:`chahua._persona_collect` —— 文件采集（本地递归读 / GitHub 递归下载 + ``_MAX_*`` 限额）。
- :mod:`chahua._persona_fs` —— 写盘 / 原子替换 / 崩溃恢复。

为保持公开 import 路径与历史一致（``from chahua.persona_import import X`` /
``persona_import.X`` 仍可用），各块的对外符号在本模块顶部 re-export；测试 monkeypatch
``persona_import._gh_latest_commit_sha`` / ``._collect_local_files`` / ``._collect_from_source``
/ ``._safe_latest_commit_sha`` / ``._gh_fetch_one_file`` 仍生效 —— **它们的 bare-name 调用方
（``_safe_latest_commit_sha`` / ``_check_github`` / ``_check_folder`` / ``_collect_from_source``
/ ``_detect_local_modified`` / ``import_from_*`` / ``update_persona`` …）都留在本模块**，经本
模块命名空间解析 patch。被抽出的纯函数（采集 / 写盘）不 bare-call 任何被 patch 的名字，故
搬走后语义不变。"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ._paths import Paths
from ._persona_collect import _collect_local_files, _fetch_github_dir
from ._persona_fs import (
    _recover_interrupted_updates,
    _replace_dir_atomic,
    _write_files,
)
from .admin import PERSONAS_REL_DIR, sanitize_fs_name
from .persona_github import (
    _GitHubError,
    _gh_fetch_one_file,
    _gh_latest_commit_sha,
    _parse_github_url,
)
from .persona_manifest import PersonaManifestError, parse_persona_manifest_bytes
from .persona_provenance import (
    SOURCE_FILENAME,
    STATUS_ERROR,
    STATUS_SOURCE_UNAVAILABLE,
    STATUS_UNKNOWN,
    STATUS_UP_TO_DATE,
    STATUS_UPDATE_AVAILABLE,
    GithubProvenance,
    PersonaImportError,
    PersonaSource,
    _content_hash,
    _now_iso,
    _version_from_files,
    read_source,
    write_source,
)

_log = logging.getLogger(__name__)

# 对外（含历史）公开符号 —— 含从 persona_provenance / persona_github re-export 的部分，
# 让 ``persona_import.X`` / ``from chahua.persona_import import X`` 兼容历史调用方与测试。
__all__ = [
    # 异常
    "PersonaImportError",
    "_GitHubError",
    # provenance 数据模型 / 常量
    "GithubProvenance",
    "PersonaSource",
    "SOURCE_FILENAME",
    "STATUS_UNKNOWN",
    "STATUS_UP_TO_DATE",
    "STATUS_UPDATE_AVAILABLE",
    "STATUS_SOURCE_UNAVAILABLE",
    "STATUS_ERROR",
    "read_source",
    "write_source",
    # 结果类型
    "ImportedPersona",
    "UpdateStatus",
    "UpdatedPersona",
    # 公开入口
    "import_from_folder",
    "import_from_github",
    "update_persona",
    "check_persona_update",
    "delete_persona",
    "recover_interrupted_persona_updates",
]


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


# ── GitHub 基线 sha（best-effort；低层 HTTP / 递归采集见 persona_github / _persona_collect）──


def _safe_latest_commit_sha(
    owner: str, repo: str, ref: Optional[str], path: str
) -> Optional[str]:
    """:func:`persona_github._gh_latest_commit_sha` 的 best-effort 包装 —— 任何 GitHub
    失败 WARN + None，不让 import 时取基线 sha 失败回退整次导入。"""
    try:
        return _gh_latest_commit_sha(owner, repo, ref, path)
    except PersonaImportError as e:
        _log.warning("导入时未取到 commit sha（provenance 降级，可重新导入）：%s", e)
        return None


# ── 崩溃恢复入口 + 结果（写盘 / 原子替换原语见 _persona_fs）─────────────────────


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
