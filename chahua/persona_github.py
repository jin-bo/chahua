"""GitHub Contents API 客户端（persona 导入 / 更新检查用的低层 HTTP 原语）。

匿名访问公开仓库（``api.github.com/repos/{o}/{r}/contents/{p}?ref={b}``），60 req/h
限速一份 persona 通常 <10 files 用不掉多少。token 鉴权 / 私有仓暂不支持。本模块只管
「单个资源 → 字节 / JSON」与 URL 解析；递归下载整目录的 budget/skip 逻辑留在
:mod:`chahua.persona_import`（与本地 ``_walk_local`` 并列）。
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from .persona_provenance import PersonaImportError

# `urllib` 默认 User-Agent 是 ``Python-urllib/3.x``；GitHub API 要求显式 UA 否则 403。
_GH_UA = "chahua-persona-importer"
_GH_API = "https://api.github.com"


class _GitHubError(PersonaImportError):
    """带 HTTP 状态码的 GitHub 失败。子类化 :class:`PersonaImportError` —— 现有
    ``except PersonaImportError`` 全部照常捕获、友好 message 不变；新增 ``code`` 让
    :func:`chahua.persona_import.check_persona_update` 区分 404（源已删 →
    source_unavailable）/ 403（rate limit → error）/ 其它（error）。``code is None`` =
    网络层错误（URLError）。"""

    def __init__(self, message: str, *, code: Optional[int]) -> None:
        super().__init__(message)
        self.code = code


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
