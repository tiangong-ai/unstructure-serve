# MinerU 4.0 升级评估

> 实施状态（2026-09-17）：下文保留升级前评估。后续已完成 4.0 SDK 兼容层、依赖升级及 Docker VLM 验证；11 份 input PDF 的 15 项真实回归全部通过。当前部署配置与验收范围见 [部署与回归说明](mineru_4_upgrade_usage.md)。

评估日期：2026-09-17。项目基线：`c09a7ec`，`uv.lock` 与当前虚拟环境均为 MinerU `3.4.3`。上游核对对象为 2026-09-16 发布的 `mineru-4.0.0-released`，提交 `22f5abb681c2314feffb9353fdff919d52dd8369`，并核对了 PyPI 4.0.0 元数据。在线文档可能继续更新，涉及具体依赖和实现时以该发布提交为准。

本次完成了源码、配置、依赖元数据审查，以及 Python 3.12 下两种依赖组合的解析预检；没有安装 4.0 到项目环境，没有运行 4.0 模型或真实文档回归。本文中的方案和工期是工程评估，不是已经完成的迁移。

## 结论与建议

建议启动隔离环境中的升级验证，按“替换解析兼容层、保持现有业务契约、验证后灰度”的顺序推进。4.0 涉及解析 API、模型、配置和结构化输出重构，直接放宽 `mineru<4` 会破坏现有运行入口。[发布说明](https://github.com/opendatalab/MinerU/releases/tag/mineru-4.0.0-released)、[迁移指南](https://opendatalab.github.io/MinerU/reference/migration_4/)

第一阶段建议采用以下路径：

```text
现有 FastAPI / 普通 Celery / two-stage parse
    → parse_doc() 项目兼容层
    → MinerU 4 无状态 Python SDK
    → 本地小模型 + 独立 MinerU VLM 服务
    → 保存资产、导出并归一化 Content List
    → 原有 checkbox 回填、图片视觉、TXT、MinIO 与响应模型
```

这条路径能集中改造 `src/services/mineru_service_full.py`，并继续使用现有排队与业务处理。MinerU 自带 Doclib、Router、V1 API 可以作为后续独立演进方向；当前没有必要同时替换 Celery 编排和对外 API。

潜在收益是新的版面/OCR/公式模型、统一结构化结果及更广的原生文档支持。能否提高本项目文档的准确率、吞吐和稳定性，必须由同一批样本的 3.4.3/4.0 对照实验决定。

## 当前项目与 4.0 的差异

| 位置 | 当前行为 | 迁移处理 |
| --- | --- | --- |
| `pyproject.toml`、`uv.lock` | `mineru[all]>=3.0.1,<4`，实际 3.4.3 | 隔离分支先精确固定 4.0.0，重新生成锁文件；Python 支持范围补上 `<3.15`，部署继续使用 3.12 |
| `src/services/mineru_service_full.py` | 导入 `mineru.cli.common.do_parse/read_fn`，回读 `{stem}_content_list.json` | 4.0 发布树中已无 `mineru/cli/common.py`；换用 `mineru.parser.parse` 与 `ParseResult` |
| `src/utils/mineru_backend.py` | 校验 `pipeline/vlm-*/hybrid-*` | 业务配置改为 tier 与推理连接分开表达；过渡期显式处理旧 backend |
| `src/utils/mineru_support.py` | 从 `mineru.cli.common` 反射扩展名 | 原导入失效后会静默回落到少量格式；改为经过业务验证的白名单/新格式元数据适配 |
| `mineru_with_images_service.py` | 用 `output_dir + img_path` 读取图片 | 先把新结果的图片资产保存到任务目录，再导出 Content List |
| `gpu_scheduler.py`、`mineru_markdown.py` | 消费旧文本、图片和表格字段 | 在兼容层统一字段与新块类型，避免各条链路分别漏内容 |
| 原生 DOCX TXT 分支 | 原 DOCX 传入 `parse_doc(..., backend=backend)` | 明确选择 `tier="flash"`，不传 PDF 页范围；保持严格 OCR 与按文档流插回 |
| `ecosystem.vllm*.json` | `.venv/bin/mineru-vllm-server` | 改为新环境的 `mineru-kit vlm-server --engine vllm ...`，重新核对启动参数 |
| 同步、普通 Celery、two-stage | 复用同一适配层，但执行生命周期不同 | 分别验证冷启动、GPU 绑定、超时和清理，不能只测一个入口 |

4.0 Python SDK 默认解析整个 PDF；Doclib 的 `mineru parse` 默认前 10 页。服务侧使用无状态 SDK，并明确整本文档语义，可避免把面向交互阅读的分页续读行为带进现有批处理服务。[SDK 文档](https://opendatalab.github.io/MinerU/usage/sdk_api/)

## 依赖与部署环境

### 已确认的依赖变化

| 组件 | 当前锁定值 | 4.0 发布包声明 |
| --- | --- | --- |
| Python | 项目要求 `>=3.12` | `>=3.10,<3.15` |
| MinerU | 3.4.3 | 首轮固定 4.0.0 |
| mineru-vl-utils | 1.0.5 | `>=2.0.4,<3` |
| Transformers | 4.57.6 | `torch` extra 要求 `>=5.10.1,<6` |
| vLLM | 0.11.0 | Linux `full` extra 要求 `>=0.19.1,<0.29.0` |
| Torch | 2.8.0 | `torch` extra 声明 `>=2.7,<3`，具体版本受 vLLM 等进一步约束 |
| DocVortex | 当前无该锁定包 | `>=0.4.9,<1` |

注意：发布包仍保留 `all → full` 别名。因此 `all` 本身不是安装失败的原因，但建议显式使用 `torch` 或 `full` 表达环境用途。基础包也包含 Gradio、ONNX、llama.cpp 等依赖，不能把它当成极简 HTTP 客户端。[固定版本依赖声明](https://github.com/opendatalab/MinerU/blob/22f5abb681c2314feffb9353fdff919d52dd8369/pyproject.toml)

建议的环境组合：

- API/parse worker：如果 VLM 独立部署，可使用 `mineru[torch]==4.0.0`；先验证 Torch 小模型的资源占用。若 GPU 主要留给 VLM，可另测基础包 + 显式 ONNX CPU 小模型，并衡量 CPU 吞吐。
- MinerU VLM 服务：独立虚拟环境或镜像，使用 `mineru[full]==4.0.0`，固定经过验证的 vLLM/Torch/CUDA 组合。
- 如果第一轮仍共用一个虚拟环境，则使用 `full` 并锁定整套依赖；它会扩大升级影响面，但不必先完成依赖拆分才能开始 PoC。
- 项目的图片描述服务由 `VISION_*` / `VLLM_BASE_URLS` 控制，与 MinerU 版面解析使用的 VLM 连接分别配置。

### 已完成的解析预检

将项目直接依赖复制到临时 requirements 文件，仅把 MinerU 替换为 `mineru[torch]==4.0.0` 或 `mineru[full]==4.0.0`，分别执行 `uv pip compile ... --python-version 3.12`。两组均退出成功，且保留 `fastapi==0.136.3`、`starlette<0.52`。

| 解析组合 | 本次解析选择的关键版本 |
| --- | --- |
| `torch` | FastAPI 0.136.3、Starlette 0.51.0、Torch 2.14.0、Transformers 5.17.0、DocVortex 0.4.9 |
| `full` | FastAPI 0.136.3、Starlette 0.51.0、vLLM 0.23.0、Torch 2.11.0、Transformers 5.17.0、DocVortex 0.4.9 |

这些是评估日的可解析组合，不是已验证的生产推荐锁定值。此检查不等于 `uv lock` 的完整跨平台解析，也不验证 wheel 安装、GPU/驱动兼容、服务启动或运行时 API。

`full` 的解析结果仍含 `prometheus-fastapi-instrumentator==7.1.0`。因此现有 FastAPI/Starlette 兼容约束应先保留，待新 vLLM 的请求入口实测通过后再单独讨论解除。

## parse_doc 兼容层设计

### 调用与资产保存

保持当前 `(content_list, output_dir, None)` 返回契约。下面是迁移骨架，不是已经验证的生产实现；省略项目配置映射、错误包装和字段归一化：

```python
from pathlib import Path

from mineru.parser import ParseResult, parse
from mineru.parser.writer import FileBasedDataWriter
from mineru.render import RenderFormat, render


def parse_one(source: Path, artifact_dir: Path, *, tier, ocr_mode, vlm_config):
    result = parse(
        source,
        tier=tier,
        ocr_mode=ocr_mode,
        page_range="all" if source.suffix.lower() == ".pdf" else "",
        vlm_config=vlm_config,
    )
    result.save(FileBasedDataWriter(str(artifact_dir)))
    saved = ParseResult.from_json(
        (artifact_dir / "middle_json.json").read_text(encoding="utf-8")
    )
    content_list = render(saved.middle_json, RenderFormat.CONTENT_LIST)
    # 此处归一化业务字段、校验图片落盘，并对 PDF 执行 checkbox 回填。
    return content_list, str(artifact_dir), None
```

关键是从**保存后的结果**导出 Content List。4.0 `save()` 在副本上外置图片，不会把原始 `result.middle_json` 原地改成文件路径；它写入 `middle_json.json`、`structured_content.json`、Markdown 与图片资产，且不自动生成当前约定的 `{stem}_content_list.json`。只调用 `save()` 然后渲染原对象，仍可能得到无法用 `os.path.join()` 读取的图片表示。[ParseResult 保存实现](https://github.com/opendatalab/MinerU/blob/22f5abb681c2314feffb9353fdff919d52dd8369/mineru/parser/base.py)

如果运维工具仍需要旧 content-list 文件名，可由项目适配层显式写出归一化结果。two-stage 必须让 parse 输出的图片一直保留到 vision/merge 完成；不能在 parse 返回前销毁临时目录。

### 必须显式归一化的内容

| 项目契约 | 4.0 风险与处理 |
| --- | --- |
| `page_idx` / `page_number` | 4.0 page_idx 仍从 0 开始；保持源 PDF 页码，只在业务响应处加 1。旧 start/end 页号需转换为新的一基页范围，验证边界和空白页 |
| 标题与阅读顺序 | V1 仍输出 `text + text_level`；按页面和块原顺序处理，验证标题级别、页眉页脚、列表和跨页内容，不重新排序页眉 |
| 图片 caption/footnote | 4.0 V1 使用 `image_caption/image_footnote`；项目图片服务已兼容两种拼写，但 scheduler、Markdown、checkbox 等仍消费 `img_caption/img_footnote`，应统一补齐别名且避免重复拼接 |
| 图片引用 | 确保 `img_path` 对应本任务保存目录内的真实图片；记录缺失资产错误，不能以“视觉任务数为 0”掩盖资源适配失败 |
| 新块类型 | V1 包含 `chart/code/index/page_footnote` 等；当前多个消费者会跳过未知类型。定义业务映射，保留其文本；需要视觉处理的 chart 应显式进入现有图片流程 |
| 图片中的 `content` | 新结果可能自带图片/图表内嵌内容，制定与项目视觉输出的合并规则，防止漏字、重复和 OCR 分支出现解释性文本 |
| bbox 与页面尺寸 | MiddleJson 使用 0–1 坐标，V1 转成 0–1000；不可与 PDF point 尺寸直接计算面积。two-stage 图片过滤须使用一致坐标系，真实宽高比另用页面几何/图片尺寸校正 |
| 表格与 checkbox | 继续生成下游使用的 `table_body` HTML，再执行窄范围 checkbox 回填；验证合并单元格、行匹配、重复选项和跨页表格 |

字段依据为发布版本的 [Content List V1 renderer](https://github.com/opendatalab/MinerU/blob/22f5abb681c2314feffb9353fdff919d52dd8369/mineru/render/_internal/content_list/v1.py) 与 [共用坐标转换](https://github.com/opendatalab/MinerU/blob/22f5abb681c2314feffb9353fdff919d52dd8369/mineru/render/_internal/content_list/common.py)。保留 Content List V1 是减少迁移成本的桥接策略，不能据其名称假定完全兼容。

新增 MiddleJson 可先作为内部诊断产物保存；既有 MinIO `parsed.json` 继续遵循本项目响应契约。不要把历史业务 JSON 当作 4.0 MiddleJson 直接导入。4.0 的历史结果兼容读取有明确边界。[结果契约](https://opendatalab.github.io/MinerU/reference/output_files/)

## 配置、模型与 GPU 生命周期

### 配置映射

| 现有配置 | 建议迁移处理 |
| --- | --- |
| `MINERU_DEFAULT_BACKEND` | 新增项目配置 `MINERU_DEFAULT_TIER`，明确它是项目封装字段。旧字段保留一个过渡期，避免在途 Celery payload 失效 |
| `pipeline` | 以 `basic` 为候选，小模型质量仍需回归 |
| `hybrid-*` | 以 `standard` 为候选，另测 `advanced` |
| `vlm-*` | 对照测试 `standard` 与 `advanced`，不宣称 standard 等价原纯 VLM。发布源码的旧 VLM 别名对应 xhigh effort，可作为 advanced 对照的依据 |
| `MINERU_DEFAULT_METHOD=auto/txt/ocr` | 映射到公开 SDK 的 `ocr_mode` |
| `MINERU_DEFAULT_LANG` | 新公开 `parse()` 没有 lang 参数；核查业务语言样本和新 OCR 能力，不能原样传入或承诺等价覆盖 |
| `MINERU_VLLM_SERVER_URLS` 等 | 保留项目 endpoint 选择能力，每个任务构造独立 `VlmConfig(server_url=...)`；上游配置支持的是单个远程 URL |
| `MINERU_VLLM_API_KEY` | 映射到 `VlmConfig.api_key` 或 `MINERU_MODEL_VLM_API_KEY` |
| `MINERU_VLLM_AUTH_HEADER` | 新公共连接配置只提供 Bearer API key；Bearer 可提取 key，任意认证方案需额外适配或明确报错，不能静默忽略 |
| `MINERU_MODEL_SOURCE` | 继续使用；另配置 4.0 专用 `MINERU_HOME` 或 `MINERU_CONFIG` |
| hybrid batch/force pipeline 变量 | 新配置与公开 SDK 无同名等价入口；逐项复核实际使用，替换为受支持的资源控制或报告不支持 |
| `MINERU_*_HARD_TIMEOUT_SECONDS` | 属于项目生命周期控制，保留；另配 VLM 单次请求 timeout/concurrency |

项目当前 `.env.example` 为 `hybrid-http-client`，而 `parse_doc` 无环境覆盖时的代码默认是 `vlm-http-client`。应记录每种部署的有效值，避免基于单一“默认”推断升级后的解析质量。本次没有读取私有 `.env`。

4.0 在配置模块加载时读取配置；应确保 `.env` 在 MinerU 初始化前加载。多任务不要修改全局 VLM 配置或通过改进程环境变量轮换 URL，使用每次解析独立的 `VlmConfig`。新字段和远程端点优先级见 [模型配置文档](https://opendatalab.github.io/MinerU/usage/model_source/)。

### 模型服务

4.0 发布包的新小模型资源为 `MinerU-4_models_torch/onnx`，vLLM 启动包装器默认选择 `MinerU2.5-Pro-2605-1.2B`。现有模型目录或仍可响应的 3.x VLM 服务不代表完成迁移，须核对实际模型名、版本、客户端协议和输出质量。[模型注册](https://github.com/opendatalab/MinerU/blob/22f5abb681c2314feffb9353fdff919d52dd8369/mineru/model/registry.py)、[vLLM 启动实现](https://github.com/opendatalab/MinerU/blob/22f5abb681c2314feffb9353fdff919d52dd8369/mineru/kit/vlm_server/vllm_server.py)

以下命令仅用于已准备好的 4.0 隔离环境；模型下载/校验环境应选择与部署一致的配置，下载本地 VLM 权重时不要带远程 VLM URL：

```bash
mineru-kit models download --tier standard --small-backend torch --vlm-engine vllm --source modelscope
mineru-kit models verify --tier standard --small-backend torch --vlm-engine vllm
CUDA_VISIBLE_DEVICES=0 mineru-kit vlm-server --engine vllm --port 31000
```

端口只是隔离验证示例。正式迁移同步修改单卡、多卡、PM2 模板和运维文档，保留重启退避、kill timeout；旧 `--data-parallel-size` 和显存比例应在选定 vLLM 版本逐项验证。[CLI 文档](https://opendatalab.github.io/MinerU/usage/cli_tools/)

### 项目特有的资源风险

`standard/advanced` 即使使用远程 VLM，解析侧也需要小模型；`onnx` 走 CPU，`torch` 的 GPU 使用独立于远程 VLM。必须显式配置，避免 `auto` 根据安装环境改变资源分配。[档位与运行时](https://opendatalab.github.io/MinerU/usage/tiers/)

- 同步接口与普通 Celery 经 `gpu_scheduler` 每任务再启动子进程。小模型可能每任务重复加载，需分别测首文件/后续文件耗时及峰值显存，保留既有硬超时和进程组清理机制。
- `two_stage_pipeline.parse_task` 实际直接调用 `parse_doc`，不经上述 scheduler；它可能复用进程内模型，但不能假定享有 scheduler 的硬超时。应检查独立超时、worker 生命周期及 GPU 绑定。
- 当前 two-stage parse PM2 模板未显式绑定 GPU，而 VLM 模板分别绑定 GPU。引入 Torch 小模型后要防止 parse worker 与 vLLM 在同卡争抢显存，并验证多 worker 启动行为。
- 保留“每卡独立 VLM server + 项目编排”作为首轮拓扑；端点轮换要验证跨进程公平性，进程内 cycle 不等于全局负载均衡。
- `VlmConfig.max_concurrency` 默认 100，是模型调用层并发；它与 Celery 并发、图片描述的 `VISION_BATCH_SIZE` 属于不同层，应按实测容量配合设置。

若上述冷启动或资源争抢成为主要瓶颈，第二阶段可以考虑独立 MinerU V1 解析服务，让模型常驻；届时需补充远程任务取消、超时后任务仍在运行、结果下载与两套队列背压的处理。

## Office 与 DOCX 策略

第一阶段保留默认 `Office → LibreOffice PDF → MinerU 4`，从而维持当前 PDF 页码、`source.pdf`、逐页 JPEG 和 MinIO 语义。原生 Office 输出虽然更方便抽取正文，但不能据此推断等价覆盖原分页合同；已有 [3.x DOCX 专项评估](mineru_3_docx_native_evaluation.md) 的约束在迁移时仍需验证。

同步 `/mineru_with_images` 已存在的 `.docx + return_txt=true` 是必须一起迁移的例外：其 `result` 继续走 PDF，`txt` 使用 `parse(original_docx, tier="flash")`，保留图片上下文顺序、严格 OCR 提示词与失败行为。不能让 DOCX 继承 PDF 的 standard tier 或 `page_range="all"`。[原生格式 SDK 用法](https://opendatalab.github.io/MinerU/usage/sdk_api/)

新支持的 EPUB/OFD/HTML/CSV 等格式后续再逐类开放，先定义页码、图片和 MinIO 的接口契约。现有纯文本拒绝策略仍适用。

## 分阶段实施与验收

| 阶段 | 工作与退出条件 |
| --- | --- |
| 1. 基线与隔离环境 | 保存 3.4.3 锁文件、模型/配置和样本结果；建立 4.0 独立环境、模型目录、端口、任务队列与临时存储目录；完成安装、服务健康和真实推理冒烟 |
| 2. 兼容层与配置 | 新 SDK 调用、图片保存、字段/块类型归一化、tier/旧 backend 映射、扩展名和 DOCX TXT 分支；保留外部 API 契约 |
| 3. 自动化与真实回归 | 适配现有测试替身，增加真实 4.0 产物契约测试；完成同步、普通 Celery、two-stage 全链路与 MinIO 测试 |
| 4. 容量与灰度 | 对比 standard/advanced 的质量、耗时与资源；验证小模型资源分配、长文档超时、进程退出和在途任务；小流量切换 |
| 5. 后续优化 | 按收益决定是否引入常驻 V1 解析服务、扩大原生格式支持和重构输出模型 |

建议回归样本至少覆盖：中文/英文文本 PDF、扫描件、多栏论文、合并单元格表格、带 checkbox 表单、含图片 DOCX、PPTX/XLSX、超过 10 页与超过旧 300 秒窗口的大文件，以及空白/损坏页。

验收重点：

1. 完整页数、源页码、阅读顺序和 `chunk_type` 语义；尤其检查第 11 页之后与空白页。
2. 普通版、图片版、科研版、两类 Celery 任务及 two-stage 的结果结构、状态和失败传播保持约定。
3. 图片数、落盘路径、有效视觉任务数、caption 与新增 chart/code/index 内容不静默丢失；bbox 过滤不会误删大图。
4. `return_txt` 顺序与标题换行规则、DOCX OCR 污染回归、视觉调用失败行为。
5. MinIO 的 `source.pdf`、`parsed.json`、逐页 JPEG、`meta.txt` 及中文 prefix 保持合同；灰度使用独立 prefix，避免覆盖基线资产。
6. 同步/普通 Celery 的硬超时和进程组清理，以及 two-stage 自身的任务生命周期分别验证；PM2 重启后的在途任务可收敛。
7. 同样硬件/并发下记录质量错误、P50/P95 耗时、页/秒、CPU/RSS、GPU 显存和失败率。性能容差在基线测量后约定，关键字段/资产丢失应作为阻断项。

代码改造时执行仓库要求的检查：

```bash
uv run --group dev black .
uv run --group dev ruff check src
uv run --group dev pytest
```

现有 `tests/test_two_stage_pipeline_parse.py` 对 `do_parse/read_fn` 的 monkeypatch 要随新入口重写；`test_mineru_support.py`、`test_mineru_backend.py`、阅读顺序、图片服务、DOCX TXT、MinIO、checkbox 和 GPU 生命周期测试均应保留并更新。仅替身单测通过不足以证明新模型兼容，需要真实结果文件作为契约样本。

灰度期间 API 与 worker 必须按版本路由到隔离队列，不能让 3.x 和 4.x worker 无差别消费含不同配置语义的任务。切换前排空旧队列或实现明确的 payload 版本兼容；回滚要同步恢复 API、worker、依赖、模型服务地址与配置，保留运行中的任务和已产出资产。

## 工作量估算

假设有可用测试 GPU、模型下载顺畅且真实回归样本现成，一名熟悉项目的工程师预计：环境/模型验证 1–2 天，兼容层和配置 2–3 天，自动化/真实回归与容量灰度 2–3 天，总体约 5–8 个工作日。它不包含新增格式对外开放、原生 Office 全面替换或 MinerU V1 服务架构迁移；驱动和模型下载问题可能增加时间。

下一步最有价值的交付是隔离环境 PoC：用一个长 PDF 和一个含图 DOCX 跑通 `parse → save → Content List → 现有响应`，同时测清小模型与 VLM 的显存分配，再据结果锁定 tier、依赖组合和实施范围。
