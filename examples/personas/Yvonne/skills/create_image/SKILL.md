---
name: create_image
description: >
  根据用户提示词生成单张/少量图像。可在四种生图模型之间选择：
  ① gpt（OpenAI gpt-image-2）
  ② nanobanana（Google Gemini 3.1 flash image preview）
  ③ seedreamv5（TensorsLab Seedream v5）
  ④ wan（阿里 Wan2.7-image-pro）。
  支持画幅（aspect ratio）、质量、保存格式、出图张数等参数。
  脚本不强制输出位置，Agent 必须根据当前任务上下文选定目标目录
  （工作目录 / 共享目录 workspace/data|reports|docs|... / 任务目录 workspace/board/... / 其它）。
  触发条件：用户提供一段提示词并要求"生成图像 / 出图 / 画一张 / draw / generate image / make a picture"等。
---

# create_image · 提示词→图像 通用生图脚本

按用户提示词直接生成图像。脚本统一封装四种生图后端，由 `--model` 选择，参数尽量对齐；不同模型独有的能力（如 gpt 的 quality 档位、nanobanana 的 image_size 档位）通过专用参数透出。

## 何时使用本 skill

- 用户给出一段提示词，要求生成 **单张或少量** 图像。
- 用户明确指定使用 gpt / nanobanana / seedreamv5 / wan 中的某个模型，或希望对比多个模型的输出。
- 用户要求快速画一张题图、配图、封面、海报草图、参考图等。

**不要使用本 skill 的场景：**
- 需要图生图 / 编辑现有图像 → 当前脚本仅支持文本到图像（text-to-image）。
- 需要视频 / 3D / 音频 → 不适用。

## 四种模型如何选

| 模型 | 调用方式 | 强项 | 弱项 | 所需环境变量 |
|------|----------|------|------|--------------|
| `gpt` | OpenAI 官方 `images.generate` | 提示词理解强、文字渲染好、可控性高 | 原生只有方形/横向/纵向三档 size，非原生比例由本地居中裁剪兜底 | `OPENAI_API_KEY`，可选 `OPENAI_BASE_URL` |
| `nanobanana` | Google `google-genai` SDK，模型 `gemini-3.1-flash-image-preview` | 速度快、原生支持多种比例与 1K/2K/4K 档位 | 创意控制偏弱 | `GEMINI_API_KEY` |
| `seedreamv5` | TensorsLab `/v1/images/seedreamv5`（异步任务 + 轮询） | 国风/写实质感佳、分辨率自由 | 有积分制；提交后需轮询，耗时偏长 | `TENSORLAB_API_KEY` |
| `wan` | 阿里 DashScope SDK，模型 `wan2.7-image-pro` | 中文提示词友好、阿里云生态内 | size 仅有有限固定档位 | `QWEN_API_KEY` |

如果用户未指定模型：
- 默认建议 `gpt`（综合最稳）。
- 用户强调"快速 / 草图 / 多张对比" → `nanobanana`。
- 用户强调"中文写实 / 国风 / 高分辨率" → `seedreamv5` 或 `wan`。
- 用户没装某个 SDK / 没有相应 API Key → 切到下一个能用的模型。

## 必备参数

| 参数 | 说明 | 必填 |
|------|------|------|
| `--model {gpt,nanobanana,seedreamv5,wan}` | 选用的生图后端 | 是 |
| `--prompt "<文本>"` 或 stdin | 提示词；省略时从 stdin 读取（适合长文本） | 是 |
| `--output-dir <path>` | 图像保存目录（Agent 必须显式给出，见下） | 是 |

## 常用可选参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--aspect-ratio` | `16:9` | 画幅比例，常用：`1:1 / 4:3 / 3:4 / 16:9 / 9:16 / 21:9 / 2:3 / 3:2` |
| `--quality {low,medium,high,auto}` | `high` | 仅 `gpt` 使用，对应 gpt-image 的 quality 档位 |
| `--image-size {1K,2K,4K,512}` | `2K` | 仅 `nanobanana` 使用 |
| `--n` | `1` | 出图张数（部分后端可能强制为 1） |
| `--output-format {png,jpeg,webp}` | `png` | 保存格式；`gpt` 后端会透传给 API，其他后端会以该后缀保存 |
| `--filename <base>` | 时间戳 | 文件名前缀；最终文件为 `<base>_<i>.<ext>` |
| `--api-key` | 读环境变量 | 直接传入 API Key，覆盖环境变量 |
| `--dry-run` |  | 打印请求计划，不调用 API |
| `--debug` |  | 打开 DEBUG 日志 |

## 输出目录选择（Agent 的职责）

脚本**不会**给 `--output-dir` 设默认值——Agent 必须根据当前任务语境，从下列候选中**主动**挑一个：

| 语境 | 推荐目录 |
|------|----------|
| 用户在写报告、文档、研究笔记 | `workspace/reports/<topic>/` 或 `workspace/docs/<topic>/assets/` |
| 用户在做 PPT / slides 类工作 | `workspace/docs/slides/<deck-name>/` |
| 用户处于一个共享/通用上下文，没有明确归属 | `workspace/data/images/<topic>/` 或 `workspace/Downloads/images/<date>/` |
| 用户明确给了路径 | 直接用，不要再"修正" |
| 真正的临时/一次性草图 | `workspace/raw/sketches/<date>/` |
| 用户在执行一个特定任务 | `task/images` |
| 用户要共享给外部（客户/同事）的图像 | `share/images/<topic>/` |

铁律：**不要把生成图像写到项目根或源码树**。所有生成物按 [AGENTAO.md §Workspace](../../AGENTAO.md) 的约定，必须落在 `workspace/` 或 `share/` 或 `task/` 下。
如果上下文不够明确，先用一句话告诉用户你打算存到哪里，然后再生成。

## 环境变量

| 模型 | 环境变量 | 来源 |
|------|----------|------|
| `gpt` | `OPENAI_API_KEY`（必需），`OPENAI_BASE_URL`（可选自定义网关） | OpenAI |
| `nanobanana` | `GEMINI_API_KEY` | Google AI Studio |
| `seedreamv5` | `TENSORLAB_API_KEY` | TensorsLab |
| `wan` | `QWEN_API_KEY` | 阿里云 DashScope |

脚本会自动 `load_dotenv()`，因此在项目根的 `.env` 文件里写好即可，不需要每次手动 export。

## 调用约定（始终通过 `uv run`）

```bash
# 最简：单张 16:9 高质量
uv run python skills/create_image/scripts/create_image.py \
  --model gpt \
  --prompt "霓虹雨夜的赛博朋克东京街景，俯视长焦，胶片颗粒" \
  --aspect-ratio 16:9 \
  --quality high \
  --output-dir workspace/reports/cyberpunk-tokyo/

# nanobanana 2K 多张对比
uv run python skills/create_image/scripts/create_image.py \
  --model nanobanana \
  --prompt "极简瑞士平面海报，几何分割，柠檬黄与靛蓝" \
  --aspect-ratio 4:3 \
  --image-size 2K \
  --n 3 \
  --output-dir workspace/docs/slides/swiss-poster/

# seedreamv5（异步任务，会自动轮询直到完成）
uv run python skills/create_image/scripts/create_image.py \
  --model seedreamv5 \
  --prompt "敦煌壁画风格的飞天，矿物颜料，绢本设色" \
  --aspect-ratio 3:4 \
  --output-dir workspace/raw/sketches/dunhuang/

# wan（阿里 Wan2.7-image-pro，中文提示词友好）
uv run python skills/create_image/scripts/create_image.py \
  --model wan \
  --prompt "宋代山水写意，水墨淡彩，远山如黛" \
  --aspect-ratio 16:9 \
  --filename song-shanshui \
  --output-dir workspace/data/images/shanshui/

# 提示词较长时用 stdin
cat my_prompt.txt | uv run python skills/create_image/scripts/create_image.py \
  --model gpt --aspect-ratio 1:1 --output-dir workspace/raw/sketches/today/
```

## 失败时的行为

- 网络/临时错误：自动重试最多 **3 次**，每次间隔 5 秒。
- 内容审核 / API Key 无效 / 配额不足：**不重试**，直接退出并打印结构化错误，便于 LLM 后续调整提示词或换模型。
- 任何错误都以非零退出码返回；成功时退出码 0 并在 stdout 末尾打印每张已保存的绝对路径。

## 与已有 PPT 脚本的关系

本 skill 的 `scripts/create_image.py` 是 `skills/zootopia-ppt/scripts/` 下四个 `image_gen_ppt_*.py` 的"单图通用版"：
- 抽掉了 Markdown 大纲解析、角色注入、按 `##` 切页等 PPT 专属逻辑；
- 保留了各模型的请求构造、画幅映射、本地裁剪兜底、轮询逻辑、错误重试；
- 统一了 CLI 参数，让 Agent 在多模型间切换的成本最小。

如果用户需要按大纲批量生成成套 PPT 图像，请引导到 `pro-ppt` / `zootopia-ppt` skill，不要在本 skill 内重新实现大纲解析。
