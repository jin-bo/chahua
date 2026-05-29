"""persona 包来源 / 更新元数据模型（P12.6 consumer-side provenance）。

provenance 是**消费侧安装元数据**（「这个 persona 是谁、从哪、什么时候装的」），住
per-persona sidecar ``.chahua-source.json``，与作者可分发的 ``persona.toml`` 严格分层
（manifest 跟上游走、provenance 跟本地装机走）。本模块是 provenance 数据形状 + 读写 +
内容哈希 + 更新状态词表的单一来源；:mod:`chahua.persona_import` 的 import / update /
check / delete 编排在它之上。

容错档（承重不变量）：provenance 是增强不是承重，坏了只让该 persona「不可更新」，不该
让它从「已安装」列表 / picker 消失 —— 与 :func:`load_persona_manifest` 的 fail-fast 口径
有意相反。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ._persist import write_json_atomic
from .persona_manifest import PersonaManifestError, parse_persona_manifest_bytes

_log = logging.getLogger(__name__)


class PersonaImportError(ValueError):
    """import / update / delete 失败的统一类型。message 直接 emit 给前端，要够具体能告诉用户怎么修。"""


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
