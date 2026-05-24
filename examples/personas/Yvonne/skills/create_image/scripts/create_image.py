#!/usr/bin/env python3
"""根据提示词生成单张/少量图像的通用脚本。

支持四个生图后端，由 ``--model`` 选择：
  - gpt        : OpenAI 官方 gpt-image-2（原生，禁用 OpenRouter；用 OPENAI_API_KEY）
  - nanobanana : Google Gemini 3.1 flash image preview（用 GEMINI_API_KEY）
  - seedreamv5 : TensorsLab Seedream v5（异步任务+轮询；用 TENSORLAB_API_KEY）
  - wan        : 阿里 DashScope Wan2.7-image-pro（用 QWEN_API_KEY）

参数尽量对齐：
  - --prompt / stdin       : 提示词
  - --aspect-ratio         : 画幅比例（如 16:9 / 1:1 / 9:16）
  - --quality              : 质量（仅 gpt 使用：low/medium/high/auto）
  - --image-size           : 分辨率档位（仅 nanobanana 使用：512/1K/2K/4K）
  - --n                    : 出图张数
  - --output-format        : png/jpeg/webp
  - --output-dir           : 必填；Agent 根据任务上下文给出
  - --filename             : 文件名前缀（默认时间戳）

设计来源：从 ``skills/zootopia-ppt/scripts/image_gen_ppt_{gpt,nb,wan}.py`` 与
``image_gen_ppt.py`` 抽取的"单图通用版"，去掉了 Markdown 大纲解析等 PPT 专属逻辑。
"""

from __future__ import annotations

import argparse
import base64
import logging
import mimetypes
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

if not sys.stdout.isatty():
    sys.stdout.reconfigure(line_buffering=True)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("create_image")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

ASPECT_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
DEFAULT_ASPECT_RATIO = "16:9"

_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


def parse_aspect_ratio(value: str | None) -> str:
    if not value:
        return DEFAULT_ASPECT_RATIO
    s = value.strip()
    for ar in ASPECT_RATIOS:
        if ar in s:
            return ar
    return DEFAULT_ASPECT_RATIO


def aspect_ratio_to_floats(ar: str) -> tuple[int, int]:
    a, _, b = ar.partition(":")
    return int(a), int(b)


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|\s]+', "_", name).strip("_")
    return cleaned or "image"


def default_filename_base() -> str:
    return f"image_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def read_prompt(args_prompt: str | None) -> str:
    if args_prompt:
        return args_prompt.strip()
    if sys.stdin.isatty():
        raise SystemExit("❌ 缺少 --prompt 且 stdin 为空：请通过 --prompt 或管道传入提示词。")
    text = sys.stdin.read().strip()
    if not text:
        raise SystemExit("❌ stdin 提示词为空。")
    return text


def center_crop_to_aspect(
    image_bytes: bytes,
    target_w: int,
    target_h: int,
    ext: str | None,
) -> tuple[bytes, str | None]:
    """If actual aspect differs from target by >2%, center-crop to target ratio.

    Used by gpt-image where only fixed sizes are supported. Silent no-op if
    Pillow is missing.
    """
    try:
        from PIL import Image
        import io
    except ImportError:
        logger.warning("⚠️ 未安装 Pillow，跳过比例裁剪。`uv add pillow` 可启用本地裁剪兜底。")
        return image_bytes, ext

    target_ratio = target_w / target_h
    img = Image.open(io.BytesIO(image_bytes))
    actual_w, actual_h = img.size
    actual_ratio = actual_w / actual_h

    if abs(actual_ratio - target_ratio) / target_ratio < 0.02:
        return image_bytes, ext

    logger.info(
        f"   实际尺寸 {actual_w}×{actual_h} (ratio {actual_ratio:.3f}) 与目标 "
        f"{target_w}:{target_h} (ratio {target_ratio:.3f}) 不符，居中裁剪。"
    )

    if actual_ratio > target_ratio:
        new_w = int(round(actual_h * target_ratio))
        new_h = actual_h
        left = (actual_w - new_w) // 2
        top = 0
    else:
        new_w = actual_w
        new_h = int(round(actual_w / target_ratio))
        left = 0
        top = (actual_h - new_h) // 2

    cropped = img.crop((left, top, left + new_w, top + new_h))
    out_fmt_pil = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}.get(
        (ext or "png").lower(), "PNG"
    )
    if out_fmt_pil == "JPEG" and cropped.mode in ("RGBA", "P"):
        cropped = cropped.convert("RGB")
    buf = io.BytesIO()
    cropped.save(buf, format=out_fmt_pil)
    new_ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}[out_fmt_pil]
    return buf.getvalue(), new_ext


def save_bytes(output_dir: Path, base: str, index: int, ext: str, data: bytes) -> Path:
    ext = (ext or "png").lower().lstrip(".")
    if ext == "jpeg":
        ext = "jpg"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{base}_{index}.{ext}"
    path.write_bytes(data)
    logger.info(f"📥 已保存: {path} ({len(data)} bytes)")
    return path


# ---------------------------------------------------------------------------
# Backend: gpt — OpenAI gpt-image-2 (native, NOT OpenRouter)
# ---------------------------------------------------------------------------

GPT_MODEL = "gpt-image-2"
GPT_QUALITY_OPTIONS = ("low", "medium", "high", "auto")


def _aspect_ratio_to_gpt_size(ar: str) -> str:
    try:
        w, h = aspect_ratio_to_floats(ar)
        ratio = w / h
    except Exception:
        return "1536x864"
    if ratio >= 1.15:
        return "1536x864"
    if ratio <= 0.87:
        return "864x1536"
    return "1024x1024"


def gen_gpt(
    prompt: str,
    aspect_ratio: str,
    output_dir: Path,
    filename_base: str,
    quality: str,
    output_format: str,
    n: int,
    api_key: str,
) -> list[Path]:
    from openai import OpenAI

    size = _aspect_ratio_to_gpt_size(aspect_ratio)
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or None
    client = OpenAI(api_key=api_key, base_url=base_url)

    kwargs: dict = {
        "model": GPT_MODEL,
        "prompt": prompt,
        "n": n,
        "size": size,
        "quality": quality,
    }
    if output_format in ("png", "jpeg", "webp"):
        kwargs["output_format"] = output_format

    logger.info(f"🎨 OpenAI ({GPT_MODEL}) · size={size} 目标比例 {aspect_ratio} · quality={quality} · n={n}")
    t0 = time.monotonic()
    result = client.images.generate(timeout=300.0, **kwargs)
    logger.info(f"✅ 响应耗时 {time.monotonic() - t0:.1f}s")

    usage = getattr(result, "usage", None)
    if usage:
        logger.info(
            f"📊 Token 用量: 输入 {getattr(usage, 'input_tokens', None)}, "
            f"输出 {getattr(usage, 'output_tokens', None)}, "
            f"合计 {getattr(usage, 'total_tokens', None)}"
        )

    images: list[tuple[bytes, str | None]] = []
    for item in getattr(result, "data", None) or []:
        b64 = getattr(item, "b64_json", None)
        if b64:
            images.append((base64.b64decode(b64), output_format or None))
            continue
        url = getattr(item, "url", None)
        if url:
            images.append(_decode_image_url(url))
    if not images:
        raise RuntimeError(f"API 响应中未包含图像数据: {result!r}")

    ar_w, ar_h = aspect_ratio_to_floats(aspect_ratio)
    saved: list[Path] = []
    for i, (img_bytes, ext) in enumerate(images):
        ext = ext or output_format
        img_bytes, ext = center_crop_to_aspect(img_bytes, ar_w, ar_h, ext)
        saved.append(save_bytes(output_dir, filename_base, i, ext or output_format, img_bytes))
    return saved


def _decode_image_url(url: str) -> tuple[bytes, str | None]:
    if url.startswith("data:"):
        header, _, b64 = url.partition(",")
        mime = header[5:].split(";", 1)[0].strip().lower()
        return base64.b64decode(b64), _MIME_EXT.get(mime)
    import urllib.request
    with urllib.request.urlopen(url) as resp:  # nosec - controlled provider URL
        ctype = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        return resp.read(), _MIME_EXT.get(ctype)


# ---------------------------------------------------------------------------
# Backend: nanobanana — Google Gemini 3.1 flash image preview
# ---------------------------------------------------------------------------

NB_MODEL = "gemini-3.1-flash-image-preview"
NB_IMAGE_SIZES = ("512", "1K", "2K", "4K")
NB_RATIOS = ("1:1", "1:4", "1:8", "2:3", "3:2", "3:4", "4:1", "4:3", "4:5", "5:4", "8:1", "9:16", "16:9", "21:9")


def gen_nanobanana(
    prompt: str,
    aspect_ratio: str,
    output_dir: Path,
    filename_base: str,
    image_size: str,
    n: int,
    api_key: str,
) -> list[Path]:
    from google import genai
    from google.genai import types

    if aspect_ratio not in NB_RATIOS:
        logger.warning(f"⚠️ Gemini 不直接支持比例 {aspect_ratio}，回退到 16:9。")
        aspect_ratio = "16:9"
    if image_size not in NB_IMAGE_SIZES:
        logger.warning(f"⚠️ 未知 image_size={image_size}，回退到 2K。")
        image_size = "2K"

    client = genai.Client(api_key=api_key)
    logger.info(f"🎨 Gemini ({NB_MODEL}) · 比例 {aspect_ratio} · 分辨率 {image_size} · n={n}")

    saved: list[Path] = []
    for run_idx in range(n):
        response = client.models.generate_content(
            model=NB_MODEL,
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"],
                image_config=types.ImageConfig(
                    aspect_ratio=aspect_ratio,
                    image_size=image_size,
                ),
                thinking_config=types.ThinkingConfig(
                    include_thoughts=True,
                    thinking_level="MINIMAL",
                ),
            ),
        )

        img_count_this_run = 0
        for part in response.parts:
            if getattr(part, "thought", False):
                if part.text:
                    logger.info(f"💭 模型思维过程:\n{part.text}")
                continue
            if part.text:
                logger.info(f"💬 模型回复文本: {part.text.strip()}")
            if part.inline_data:
                try:
                    image = part.as_image()
                    import io
                    buf = io.BytesIO()
                    image.save(buf, format="PNG")
                    saved.append(save_bytes(output_dir, filename_base, len(saved), "png", buf.getvalue()))
                    img_count_this_run += 1
                except Exception as e:
                    logger.warning(f"解析图像数据失败: {e}")
        if img_count_this_run == 0:
            raise RuntimeError(f"Gemini 第 {run_idx + 1} 次响应中未包含图像数据。")
    return saved


# ---------------------------------------------------------------------------
# Backend: seedreamv5 — TensorsLab (async task + polling)
# ---------------------------------------------------------------------------

SEEDREAM_BASE = "https://api.tensorslab.com"
SEEDREAM_GENERATE = f"{SEEDREAM_BASE}/v1/images/seedreamv5"
SEEDREAM_STATUS = f"{SEEDREAM_BASE}/v1/images/infobytaskid"
SEEDREAM_STATUS_MAP = {1: "排队中", 2: "生成中", 3: "已完成", 4: "失败"}


def _aspect_ratio_to_seedream_resolution(ar: str) -> str:
    try:
        w, h = aspect_ratio_to_floats(ar)
        ratio = w / h
    except Exception:
        return "3840x2160"
    if abs(ratio - 16 / 9) < 0.05:
        return "3840x2160"
    if abs(ratio - 9 / 16) < 0.05:
        return "2160x3840"
    if abs(ratio - 1.0) < 0.05:
        return "2048x2048"
    if abs(ratio - 4 / 3) < 0.05:
        return "2880x2160"
    if abs(ratio - 3 / 4) < 0.05:
        return "2160x2880"
    if ratio >= 1.0:
        long_side = 3840
        return f"{long_side}x{int(round(long_side / ratio))}"
    long_side = 3840
    return f"{int(round(long_side * ratio))}x{long_side}"


def gen_seedreamv5(
    prompt: str,
    aspect_ratio: str,
    output_dir: Path,
    filename_base: str,
    output_format: str,
    n: int,
    api_key: str,
    poll_interval: int = 6,
    timeout: int = 300,
) -> list[Path]:
    import requests

    session = requests.Session()
    session.proxies = {"http": "", "https": ""}
    headers = {"Authorization": f"Bearer {api_key}"}

    resolution = _aspect_ratio_to_seedream_resolution(aspect_ratio)
    logger.info(f"🎨 TensorsLab seedreamv5 · 分辨率 {resolution} · 比例 {aspect_ratio} · n={n}")

    if n != 1:
        logger.info("ℹ️ seedreamv5 每次任务只生成 1 张；将循环提交以达到 n 张。")

    saved: list[Path] = []
    for run_idx in range(n):
        files = [
            ("prompt", (None, prompt)),
            ("resolution", (None, resolution)),
            ("category", (None, "seedreamv5")),
        ]
        resp = session.post(SEEDREAM_GENERATE, headers=headers, files=files, timeout=60)
        resp.raise_for_status()
        result = resp.json()
        code = result.get("code")
        if code == 9000:
            raise RuntimeError("账户积分不足，请前往 https://tensorai.tensorslab.com/ 充值")
        if code != 1000:
            raise RuntimeError(f"API 错误: {result.get('msg', '未知错误')} (code={code})")
        task_id = result["data"]["taskid"]
        logger.info(f"✅ 第 {run_idx + 1}/{n} 任务提交成功，task_id={task_id}")

        start = time.time()
        urls: list[str] = []
        while time.time() - start < timeout:
            status_resp = session.post(
                SEEDREAM_STATUS,
                headers={**headers, "Content-Type": "application/json"},
                json={"taskid": task_id},
                timeout=30,
            )
            status_json = status_resp.json()
            data = status_json.get("data") if status_json.get("code") == 1000 else None
            if not data:
                time.sleep(poll_interval)
                continue
            status = data.get("image_status")
            elapsed = int(time.time() - start)
            logger.info(f"🔄 状态: {SEEDREAM_STATUS_MAP.get(status, '未知')} (已等待 {elapsed}s)")
            if status == 3:
                urls = data.get("url", [])
                break
            if status == 4:
                raise RuntimeError(f"生成任务失败: {data.get('error_message', '未知原因')}")
            time.sleep(poll_interval)
        else:
            raise RuntimeError(f"等待超时（已等待 {timeout}s）")

        if not urls:
            logger.warning("⚠️ 任务完成但未返回图像 URL")
            continue
        for url in urls:
            r = session.get(url, timeout=60)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "").split(";")[0].strip()
            ext = mimetypes.guess_extension(ctype) or Path(urlparse(url).path).suffix or f".{output_format}"
            ext = ext.lstrip(".")
            if ext == "jpe":
                ext = "jpg"
            saved.append(save_bytes(output_dir, filename_base, len(saved), ext, r.content))
    if not saved:
        raise RuntimeError("seedreamv5 未返回任何可下载的图像。")
    return saved


# ---------------------------------------------------------------------------
# Backend: wan — Alibaba DashScope Wan2.7-image-pro
# ---------------------------------------------------------------------------

WAN_MODEL = "wan2.7-image-pro"
WAN_SIZE_MAP = {
    "1:1": "1024*1024",
    "16:9": "1696*960",
    "9:16": "960*1696",
    "3:4": "896*1152",
    "4:3": "1152*896",
}


def gen_wan(
    prompt: str,
    aspect_ratio: str,
    output_dir: Path,
    filename_base: str,
    output_format: str,
    n: int,
    api_key: str,
) -> list[Path]:
    import requests
    import dashscope
    from dashscope.aigc.image_generation import ImageGeneration
    from dashscope.api_entities.dashscope_response import Message

    dashscope.base_http_api_url = "https://dashscope.aliyuncs.com/api/v1"
    selected_size = WAN_SIZE_MAP.get(aspect_ratio, "1024*1024")
    logger.info(f"🎨 DashScope ({WAN_MODEL}) · size {selected_size} (比例 {aspect_ratio}) · n={n}")

    message = Message(role="user", content=[{"text": prompt}])
    rsp = ImageGeneration.call(
        model=WAN_MODEL,
        api_key=api_key,
        messages=[message],
        enable_sequential=False,
        n=n,
        size=selected_size,
    )
    if rsp.status_code != 200:
        raise RuntimeError(f"API 返回错误: {rsp.code} - {rsp.message}")

    saved: list[Path] = []
    output = getattr(rsp, "output", None) or {}
    for choice in output.get("choices") or []:
        message = choice.get("message") or {}
        for item in message.get("content") or []:
            if item.get("type") == "image" and item.get("image"):
                url = item["image"]
                r = requests.get(url, timeout=60)
                r.raise_for_status()
                ext = mimetypes.guess_extension(r.headers.get("content-type", "").split(";")[0].strip()) or f".{output_format}"
                ext = ext.lstrip(".")
                saved.append(save_bytes(output_dir, filename_base, len(saved), ext, r.content))
    if not saved:
        raise RuntimeError(f"wan 未返回有效的图像链接。原始响应: {rsp}")
    return saved


# ---------------------------------------------------------------------------
# Env / API-key resolution
# ---------------------------------------------------------------------------

_API_KEY_ENV = {
    "gpt": "OPENAI_API_KEY",
    "nanobanana": "GEMINI_API_KEY",
    "seedreamv5": "TENSORLAB_API_KEY",
    "wan": "QWEN_API_KEY",
}


def resolve_api_key(model: str, cli_key: str | None) -> str:
    if cli_key:
        return cli_key.strip()
    env_name = _API_KEY_ENV[model]
    key = os.environ.get(env_name, "").strip()
    if not key:
        raise SystemExit(
            f"❌ 未找到 {env_name}。请在 .env 文件中设置，或导出为环境变量，或通过 --api-key 显式传入。"
        )
    return key


# ---------------------------------------------------------------------------
# Retry wrapper & main
# ---------------------------------------------------------------------------

MAX_RETRIES = 3


def _is_transient(err: Exception) -> bool:
    msg = str(err).lower()
    for needle in ("timeout", "timed out", "connection", "rate limit", "429", "502", "503", "504", "temporarily"):
        if needle in msg:
            return True
    return False


def run_with_retry(fn, label: str):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn()
        except SystemExit:
            raise
        except Exception as e:
            remaining = MAX_RETRIES - attempt
            transient = _is_transient(e)
            logger.warning(f"⚠️ {label} 第 {attempt} 次尝试失败: {e}")
            if remaining <= 0 or not transient:
                if not transient and remaining > 0:
                    logger.info("ℹ️ 错误看起来非临时性（如内容审核 / Key 无效），不再重试。")
                raise
            logger.info(f"🔄 将在 5 秒后重试（剩余 {remaining} 次）...")
            time.sleep(5)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="根据提示词生成图像（gpt/nanobanana/seedreamv5/wan）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  create_image.py --model gpt --prompt '霓虹雨夜的东京' --aspect-ratio 16:9 \\\n"
            "                  --quality high --output-dir workspace/reports/tokyo/\n"
            "  echo '极简瑞士平面海报' | create_image.py --model nanobanana \\\n"
            "                  --aspect-ratio 4:3 --image-size 2K --n 3 \\\n"
            "                  --output-dir workspace/docs/slides/swiss/\n"
        ),
    )
    parser.add_argument("--model", "-m", required=True, choices=list(_API_KEY_ENV.keys()),
                        help="选用的生图后端")
    parser.add_argument("--prompt", "-P", default=None,
                        help="提示词；未提供时从 stdin 读取")
    parser.add_argument("--output-dir", "-o", required=True,
                        help="图像保存目录（Agent 根据任务上下文给定，建议在 workspace/ 之下）")
    parser.add_argument("--aspect-ratio", "-a", default=DEFAULT_ASPECT_RATIO,
                        help=f"画幅比例 (常用: {', '.join(ASPECT_RATIOS)})；默认 {DEFAULT_ASPECT_RATIO}")
    parser.add_argument("--quality", "-q", default="high", choices=list(GPT_QUALITY_OPTIONS),
                        help="质量档位 (仅 gpt 使用)")
    parser.add_argument("--image-size", "-s", default="2K", choices=list(NB_IMAGE_SIZES),
                        help="分辨率档位 (仅 nanobanana 使用)")
    parser.add_argument("--n", type=int, default=1,
                        help="出图张数（默认 1；部分后端可能强制为 1）")
    parser.add_argument("--output-format", "-f", default="png", choices=["png", "jpeg", "webp"],
                        help="保存格式（默认 png）")
    parser.add_argument("--filename", default=None,
                        help="文件名前缀；默认 image_YYYYMMDD_HHMMSS")
    parser.add_argument("--api-key", default=None,
                        help="API Key，覆盖对应环境变量")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅打印请求计划，不调用 API")
    parser.add_argument("--debug", action="store_true",
                        help="启用 DEBUG 日志")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    prompt = read_prompt(args.prompt)
    aspect_ratio = parse_aspect_ratio(args.aspect_ratio)
    output_dir = Path(args.output_dir).expanduser().resolve()
    filename_base = safe_filename(args.filename) if args.filename else default_filename_base()

    logger.info(f"🧭 模型      : {args.model}")
    logger.info(f"📂 输出目录  : {output_dir}")
    logger.info(f"📐 画幅比例  : {aspect_ratio}")
    logger.info(f"🔢 张数      : {args.n}")
    logger.info(f"🏷️ 文件名前缀 : {filename_base}")
    logger.info(f"📝 提示词    : {prompt[:160]}{'…' if len(prompt) > 160 else ''}")

    if args.dry_run:
        print("=== create_image dry-run ===")
        print(f"model         : {args.model}")
        print(f"prompt        : {prompt}")
        print(f"aspect_ratio  : {aspect_ratio}")
        print(f"quality       : {args.quality} (仅 gpt 生效)")
        print(f"image_size    : {args.image_size} (仅 nanobanana 生效)")
        print(f"n             : {args.n}")
        print(f"output_format : {args.output_format}")
        print(f"output_dir    : {output_dir}")
        print(f"filename_base : {filename_base}")
        return 0

    api_key = resolve_api_key(args.model, args.api_key)
    output_dir.mkdir(parents=True, exist_ok=True)

    def _run():
        if args.model == "gpt":
            return gen_gpt(
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                output_dir=output_dir,
                filename_base=filename_base,
                quality=args.quality,
                output_format=args.output_format,
                n=args.n,
                api_key=api_key,
            )
        if args.model == "nanobanana":
            return gen_nanobanana(
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                output_dir=output_dir,
                filename_base=filename_base,
                image_size=args.image_size,
                n=args.n,
                api_key=api_key,
            )
        if args.model == "seedreamv5":
            return gen_seedreamv5(
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                output_dir=output_dir,
                filename_base=filename_base,
                output_format=args.output_format,
                n=args.n,
                api_key=api_key,
            )
        if args.model == "wan":
            return gen_wan(
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                output_dir=output_dir,
                filename_base=filename_base,
                output_format=args.output_format,
                n=args.n,
                api_key=api_key,
            )
        raise SystemExit(f"未知模型: {args.model}")

    try:
        saved = run_with_retry(_run, label=f"{args.model} 生图")
    except Exception as e:
        logger.error(f"❌ 生图失败: {e}")
        return 1

    print()
    print(f"🎉 完成！共保存 {len(saved)} 张图像，目录: {output_dir}")
    for path in saved:
        print(str(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
