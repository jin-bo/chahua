"""持久化共享 helper（P2.1，设计文档 §3.7）。

茶话室持久化三个文件：

- ``rooms/<id>/transcript.jsonl`` —— 房间公共发言，append-only。
- ``rooms/<id>/summary.jsonl``    —— 摘要段，append-only。
- ``rooms/<id>/cursor.json``      —— 每茶客 ``last_seen_seq``，整体重写。

P2.1 选择：

- **append-only 用 jsonl** —— 一行一条，崩溃只伤最后一行；解析时**跳过坏行**而不是
  炸掉房间。理由：CLI / WebSocket 异常退出 / SIGKILL 都可能截断最后一行，硬要求
  全文件合法 jsonl 会让用户损失全部历史。
- **整体重写用 tmp+rename** —— ``cursor.json`` 是小文件 (<1KB)，每条用户消息后写一次
  完全可接受；用原子 rename 保证读端永远看到完整 JSON，不会读到中间状态。
- **不做 fsync** —— 进程崩溃丢最后一行，OS 崩才会丢更多。茶话室是单机闲聊 App，
  ``fsync`` 每条消息的 IO 代价不值得。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterator, Mapping

_log = logging.getLogger(__name__)


def append_jsonl(path: Path, record: Mapping[str, object]) -> None:
    """追加一条 JSON 记录（含末尾 ``\\n``）到 ``path``。

    **不**保证父目录存在 —— 调用方在构造期一次性 ``mkdir``，避免每条消息都 stat。
    多进程并发追加（不在 P2.1 模型里）暂不保证原子性。
    """
    line = json.dumps(record, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
        f.write("\n")


def read_jsonl_skip_bad(path: Path) -> Iterator[dict]:
    """逐行读 jsonl，**忽略**解析失败的行（包括最后一行被截断的情况）。

    返回 dict 流，调用方再做字段校验（缺字段时各自决定怎么兜底）。
    文件不存在 → 静默返回空迭代器（首次启动是预期场景）。
    """
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            raw = raw.rstrip("\n")
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                _log.warning("skip malformed jsonl line %s:%d", path, lineno)
                continue
            if isinstance(obj, dict):
                yield obj
            else:
                _log.warning("skip non-dict jsonl line %s:%d", path, lineno)


def _tmp_path(path: Path) -> Path:
    """生成 ``<parent>/.<name>.tmp`` 形式的 tmp 路径（P10.2 修：dot-prefix）。

    旧式 ``<path>.tmp``（如 ``share/foo.png.tmp``）会被 :func:`chahua.artifact_detector
    ._list_share_rels` 当成正常文件扫到、emit ``ROOM_ARTIFACT_ADDED`` 幽灵气泡；
    dot-prefix 命名让该扫描器一致地跳过整族 tmp 残骸（已经存在 ``startswith('.')``
    过滤）。改名仅影响 rename 前的瞬态文件，``os.replace`` 后的最终路径不变。
    """
    return path.parent / f".{path.name}.tmp"


def write_json_atomic(path: Path, data: object) -> None:
    """原子写整个 JSON 文件：写 ``<parent>/.<name>.tmp`` → ``os.replace`` 到 ``<path>``。

    读端永远看到完整 JSON 或上一版完整 JSON，不会读到半写状态。父目录由调用方
    在构造期建好（同 :func:`append_jsonl`）。
    """
    tmp = _tmp_path(path)
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def write_text_atomic(path: Path, text: str) -> None:
    """原子写整段文本（UTF-8）。``room.toml`` / ``USER.md`` 等小文件共用，避免写一半
    被 kill 留下半截内容。父目录不存在时自动创建（这些目标 mutator 入口才有，没有
    "构造期一次性 mkdir"的承诺）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """原子写整段二进制。头像 PNG / share/ 上传走这里 —— 同 :func:`write_text_atomic`，
    内容是字节而非 UTF-8 文本。tmp 名走 dot-prefix（详见 :func:`_tmp_path`）确保
    share/ 扫描期间瞬态残骸不被当成 artifact 气泡误报。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    tmp.write_bytes(data)
    os.replace(tmp, path)


def read_json_or_none(path: Path) -> object | None:
    """读整个 JSON 文件；不存在 / 解析失败 → ``None`` + WARN。

    持久化数据失效不该让房间起不来 —— 失败按"没有"处理，茶客重新走 onboarding。
    """
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log.warning("ignored broken json %s; treating as empty (%s)", path, e)
        return None
