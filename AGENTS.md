# TianGong AI Unstructure Serve 代理说明

仓库已迁入 `tiangong-ai/unstructure-serve`；迁移与升级公告见 README。仓库迁移公告本身不修改解析运行时或依赖，后续 MinerU 4 升级另行记录如下。

2026-09-17 已完成 MinerU 4.0 升级，设计背景见 [升级评估](mineru_4_upgrade_evaluation.md)，当前部署和测试入口见 [部署与回归说明](mineru_4_upgrade_usage.md)。应用改用无状态 SDK + Docker VLM，依赖在 `uv.lock` 固定；真实 PDF TDD 使用 `input`，结果与基线保存在忽略的 `output/mineru4_tdd/`。

## 项目概览
- 这是一个基于 FastAPI 的非结构化文档解析服务，负责统一封装 MinerU 文档解析、Markdown 转档、MinIO 对象存储以及视觉问答能力。
- 主入口在 `src/main.py`，通过依赖注入决定是否开启 Bearer Token 鉴权，并集中挂载各类路由（健康检查、GPU 调度、MinerU 解析、Markdown 转 DOCX、MinIO 上传下载等）。
- GPU 解析任务由自研调度器 `src/services/gpu_scheduler.py` 进行统一排队、超时控制和多进程执行，保障 MinerU 解析稳定性。
- 视觉模型封装在 `src/services/vision_service.py`，按环境变量动态选择 OpenAI、Gemini 或 vLLM 服务，并对模型列表、默认模型及凭证做运行时校验；OpenAI 与 vLLM 通过 `src/services/vision_service_openai_compatible.py` 复用同一套 OpenAI-compatible 客户端池，提示词生成集中在 `src/services/vision_prompts.py`。
- `src/main.py` 初始化根日志记录器为 INFO，并将 `httpx`/`httpcore` 日志级别降至 WARNING，避免打印请求详情。

## 目录速览
- `src/routers/`：各业务路由。`mineru_router.py`/`mineru_sci_router.py`/`mineru_with_images_router.py` 针对不同解析流程，`mineru_task_router.py`/`mineru_with_images_task_router.py` 分别提供 MinerU 普通版与图像版的 Celery 入队与状态查询，`markdown_router.py` 负责 Markdown→DOCX，`minio_router.py` 负责对象存储操作，`gpu_router.py` 暴露调度状态，`health_router.py` 提供健康检查；`mineru_minio_utils.py` 复用 MinerU 解析的 MinIO 前后处理逻辑。
- `src/services/`：MinerU、Markdown、MinIO、视觉与 GPU 调度实现。`mineru_service_full.py` 通过 MinerU 4 `mineru.parser.parse` 获取 ParseResult，先保存图片和 MiddleJson，再导出/归一化 Content List V1，继续返回 `(content_list, artifact_dir, None)`；PDF 仍执行 `pdf_text_layer_reconcile.py` checkbox 回填。Celery 单例和任务入口保持不变。
- `src/utils/`：工具函数，例如统一 JSON 响应包装、Markdown 预处理、Office→PDF 转换、MinerU 支持文件扩展名查询、纯文本导出等。
- `src/models/`：Pydantic 数据模型，描述 API 的入参与返回结构（如 `ResponseWithPageNum`（含可选 `txt`/`minio_assets` 字段）等）。
- 根目录还包含 `README.md`（环境配置与运维命令，已按 MinerU 4 SDK、tier 配置和 Docker VLM 部署更新）、`mineru_with_images_task_usage.md`（面向同事/运维的 `/mineru_with_images/task` 异步接口使用说明，明确普通 Celery 队列 `queue_normal`/`queue_urgent` 与 two-stage `queue_parse_gpu` 的区别）、`two_stage_task_usage.md`（面向同事/运维的 `/two_stage/task` 使用说明，覆盖 parse/vision/dispatch/merge worker、队列状态和批量脚本）、多个 `ecosystem*.json`（pm2 启动模板）以及 `pyproject.toml`/`uv.lock`（依赖声明）。`mineru_3_docx_native_evaluation.md` 记录了 2026-03-29 对 MinerU 3.x 原生 DOCX 拆解的专项评估：当前结论是正文抽取效果更好，但无法等价覆盖现有 `page_number`、`chunk_type`、MinIO PDF 资产和视觉链路语义，因此暂不切换默认 Office 路径。另新增 `multi_gpu_vllm_scaling_todolist.md`，用于记录“多卡下优先采用 `vlm-http-client + 每卡单独 server + 主服务编排`、`vlm-vllm-async-engine` 仅作为可选快车道”的详细实施待办。

## 核心功能
- **MinerU 文档解析**（`src/routers/mineru_router.py` 等）
  - 支持 MinerU 原生扩展名、Office 与图片类格式，利用 `maybe_convert_to_pdf` 先行格式统一，再调用 GPU 调度器执行 MinerU 管线；Markdown、TXT 等纯文本类文件不再进入 MinerU 解析接口，应由调用端本地直接读取。
  - 可选通过 `return_txt` 返回纯文本串（标题段落追加 `\n\n`、普通段落 `\n`）及内容类型标签，结果统一映射到 `TextElementWithPageNum` 模型。
  - MinerU 4 的六个上传解析入口（`/mineru`、`/mineru_sci`、`/mineru_with_images`、两个普通 `/task` 及 `/two_stage/task`）统一接受 `tier` 表单枚举：flash/basic/standard/advanced，不传固定使用 standard；Swagger 显示下拉选项，非法值返回 422。`MinerUTier` 定义在 `src/utils/mineru_backend.py`，请求档位通过现有 backend payload 字段固定到任务中。直接调用服务且未指定档位时，仍读取 `MINERU_DEFAULT_TIER`，再兼容旧 backend：pipeline→basic、hybrid-*→standard、vlm-*→advanced。`MINERU_DEFAULT_METHOD` 对应 ocr_mode。所有 MinerU 大模型推理均连接 Docker VLM。
  - `src/services/mineru_service_full.py` 使用官方无状态 SDK，整本 PDF 默认 page_range=all；分页将零基 start/end 转成一基范围，并保留源页码。保存后的图片必须真实存在，否则抛错。兼容层补齐 img_caption/img_footnote，转换 chart/code/index/page_footnote，并让 bbox 与页面尺寸使用相同单位。Office 默认仍先经 LibreOffice 转 PDF。
  - `src/services/pdf_text_layer_reconcile.py` 在 `parse_doc()` 回读 `content_list` 后执行窄范围后处理：仅当 MinerU 输出中已出现 `☐/☑/□/■` 时，才调用 `pdftotext -bbox` 读取原 PDF 文本层，按页和表格行匹配 checkbox/radio 选项，并把 MinerU 表格 HTML 中误判的选中/未选中状态回填。该逻辑默认开启，可用 `MINERU_TEXT_LAYER_CHECKBOX_RECONCILE=false` 关闭；`pdftotext` 缺失、超时或抽取失败时会跳过，不影响主解析。
  - MinerU 3.x 原生 DOCX 路线已做过专项评估，样本与 synthetic case 说明见 `mineru_3_docx_native_evaluation.md`。当前判断是：原生 DOCX 更适合正文抽取，但不能稳定覆盖现有 `page_number`、`chunk_type.title/list`、`save_to_minio` 与逐页 JPEG 语义，因此默认 Office 路径继续保留 `Office -> PDF -> vllm`。
  - 当调用端传入 `chunk_type=true` 时，解析结果除了保留标题（`type="title"`）外，还会额外返回页眉与页脚片段（`type="header"`/`"footer"`），图像识别块标记为 `type="image"`；所有块保持 MinerU `content_list` 的原始阅读顺序，`page_number` 类型仍被忽略，且 `return_txt=true` 时的纯文本输出会按同样顺序拼接。
  - `/mineru` 与 `/mineru_with_images` 均支持 `save_to_minio` 与 `minio_*` 表单字段，成功时会在 `mineru/<文件名>`（可自定义 `minio_prefix`）下写入源 PDF、解析 JSON 与逐页 JPEG，并通过响应体的 `minio_assets` 摘要返回上传结果；当 `chunk_type=true` 时，写入 MinIO 的 `parsed.json` 会保留 `type` 字段（header/footer/title/image）以便下游消费。两者唯一差异是 `/mineru_with_images` 会额外调用视觉大模型（`pipeline="images"`）为图像生成描述。
  - `/mineru_with_images` 的同步接口对 `.docx` 做了一个受限增强：当 `return_txt=true` 时，`result` 仍沿用原有 `DOCX -> PDF -> vllm` 路径，保持 JSON 结构、页码和现有下游兼容；但 `txt` 会额外基于 MinerU 4 原生 DOCX flash 拆解重新生成，用文档流顺序中的前后文本块作为图片上下文，将视觉识别内容插回正文位置。该 native DOCX txt-only 分支的图片识别现已收紧为“严格 OCR / 可见内容抽取”模式：不再把 DOCX 图片 caption/footnote 直接并入输出，也不允许根据上下文做人物/网站/项目推断。此前 MinerU 3.x 下 `公众号.docx` 的实测回归表明，`Context before/after`、`string`、`ResearchGate`、`GitHub organization page` 等明显解释型污染已被压掉，但复杂信息图中仍可能残留少量解释性串联文本。该模式只影响同步 `POST /mineru_with_images` 的 `.docx + return_txt=true` 组合，不影响默认 Office 路径、Celery 任务或 MinIO 资产合同。
  - `/mineru_with_images` 与 `/mineru_with_images/task` 的 `provider`/`model` 表单覆盖已改为“宽松接收 + 服务层兜底”：路由不再因未知 provider/model 直接返回 422，而是把原始字符串透传给 `vision_service`。其中未知 provider 会被忽略；未知 model 会连同 provider 一起视为未设置，并回退到 `.env` 中的 `VISION_PROVIDER` / `VISION_MODEL`（若 `.env` 未显式设置，则继续沿用现有 provider 默认模型选择逻辑）。
  - 额外可选字段 `minio_meta` 会在 `save_to_minio=true` 时把传入字符串写入 `meta.txt`（与 `source.pdf` 同目录），返回的 `minio_assets.meta_object` 会指向该文件，便于下游查阅附加元信息；若 `save_to_minio=false`，后端会安全地忽略该字段，避免调用端因默认值冲突而报错。
  - `mineru_minio_utils.build_minio_prefix()` 支持保留 Unicode/中文字符及常见中文标点，但所有空格（含全角空格）都会被统一替换为 `_`，其余不可打印字符也会折叠为 `_` 并清理多余分隔符。对应校验见 `tests/test_mineru_minio_utils.py`。
- **MinerU 异步队列**（`src/routers/mineru_task_router.py`/`mineru_with_images_task_router.py` + `src/services/tasks/mineru_tasks.py`）
  - 基于 Celery+Redis 提供 `/mineru/task` 与 `/mineru/task/{task_id}`（纯文本解析）以及 `/mineru_with_images/task` 与 `/mineru_with_images/task/{task_id}`（图像感知版）状态查询，返回 `task_id` 及 Celery `state`（PENDING/STARTED/SUCCESS/FAILURE 等）。
  - 路由校验与同步接口一致：仅接受 `mineru_supported_extensions` 与 Office 转 PDF 扩展名，并显式排除 Markdown、TXT 等纯文本类扩展名。上传文件会落地到 `MINERU_TASK_STORAGE_DIR`（默认系统临时目录的 `tiangong_mineru_tasks` 子目录），Celery 任务结束后自动清理。
  - `priority` 表单字段控制队列：`urgent` 走 `queue_urgent`，其他值走 `queue_normal`（可通过环境覆盖）。
  - 任务执行仍复用 `gpu_scheduler` 和 `mineru_task_runner.run_mineru_local_job`：Office 自动转 PDF，解析结果过滤页眉页脚规则与同步接口保持一致，支持 MinIO 上传与 `minio_meta` 写入；图像版 Celery 任务（`mineru.parse_images`）会额外透传 `vision_provider`/`vision_model`/`vision_prompt` 到 `parse_with_images`。
  - 对外使用和运维启动步骤见根目录 `mineru_with_images_task_usage.md`；该文档强调 `/mineru_with_images/task` 需要 `src.services.celery_app` worker 监听 `queue_urgent,queue_normal,default`，不是 two-stage 的 `queue_parse_gpu`。
- **MinIO 对象操作**（`src/routers/minio_router.py`）
  - 封装上传/下载所需的 endpoint 解析、bucket 校验与对象名规范化，所有异常以 HTTP 错误返回。
  - `/minio/upload` 接收标准的 `UploadFile` 表单字段；`/minio/upload/base64` 提供 Base64 版入口（字段 `file_base64`，可选 `content_type_override`），两者共用内置工具完成对象存储写入并在内容为空时返回 400。
  - `build_storage_collection_name` 会在 MinIO 操作中对 `collection_name`/`user_id` 做统一合法化，沿用之前 `KB_<USER>_<COLLECTION>` 的存储前缀避免路径混乱。
  - 通用配置结构 `MinioConfig` 写在 `src/services/minio_storage.py`。
- **Markdown 工具链**（`src/routers/markdown_router.py` & `src/services/markdown_service.py`）
  - 允许上传 Markdown 文本和可选的 reference DOCX 模板，将内容转换为 DOCX 并按需清理文档样式（依赖 Pandoc 与 python-docx）。
- **GPU 调度与监控**（`src/services/gpu_scheduler.py`）
  - 按 GPU ID 创建 `ProcessPoolExecutor`，每个任务在独立子进程执行，并设有硬超时以防解析卡死。
  - 解析子进程会在 Linux 下设置 parent-death signal，并把每个 MinerU 任务放入独立进程组；只有任务超过 MinerU hard timeout、父进程退出或结果已返回后的收尾阶段才会清理该任务进程组，避免按运行时长误杀大文件解析。`src.main` 的 shutdown 钩子会先调用 `scheduler.shutdown(wait=True)`，让正常 PM2/Gunicorn 重启尽量等待解析任务按自身超时收敛。
  - `/gpu/status` 路由可以查询每块 GPU 的排队任务数及运行情况。
- **视觉问答/解析**（`src/services/vision_service.py`）
  - 统一调度 OpenAI、Gemini、vLLM 视觉大模型；当前默认部署配置（`.env` / `.env.example` / `ecosystem.config.json` / `ecosystem.quatro.json`）已收口到 vLLM：`VISION_PROVIDER_CHOICES=vllm`、`VISION_PROVIDER=vllm`，现有调用默认不会再回退到 OpenAI / Gemini。OpenAI 与 vLLM 通过 `vision_service_openai_compatible.py` 共用 OpenAI-compatible 客户端池；vLLM 必须配置 `VLLM_BASE_URLS`/`VLLM_BASE_URL` 才视为可用，`VLLM_API_KEY` 仅作为可选认证头。
  - 提示词构建集中在 `vision_prompts.py`，默认文案已明确要求模型直接输出核心洞察，禁止使用“根据您提供的上下文信息”“以下是”等前置客套语。
  - 当 vLLM 仅提供 base_url 而未配置密钥时，会使用占位 key（`not-required`）落到相同的 OpenAI-compatible 请求路径；当配置了多个 `VLLM_BASE_URLS` 时，每次请求会按轮换顺序依次尝试所有 endpoint，全部失败才抛错。
  - vLLM 视觉请求会通过 OpenAI-compatible `extra_body.chat_template_kwargs.enable_thinking` 显式控制推理模式：默认 `false`（更偏向低延迟），可通过环境变量 `VLLM_ENABLE_THINKING=true` 开启。
  - vLLM 视觉请求默认带采样参数：`temperature=1.0`、`top_p=1.0`、`presence_penalty=2.0`，以及 `extra_body.top_k=40`、`extra_body.min_p=0.0`、`extra_body.repetition_penalty=1.0`；可通过 `VLLM_VISION_*` 环境变量覆盖。
  - `/mineru_with_images` 的图像描述按 `VISION_BATCH_SIZE` 分批并发调用视觉服务（默认 3、下限 1），上下文在调用前统一基于文本/列表/表格/图像 caption 计算（受 `VISION_CONTEXT_WINDOW` 控制），不会再把已生成的视觉描述写回上下文；图片无需连续也可并行，识别结果最终按原文顺序回填。若视觉调用异常，服务不再退回 caption/footnote 降级文本，而是直接抛错，让同步接口返回 500、Celery 任务失败。
- **两段式 MinerU+视觉并行（新增示例服务）**
  - 新增 `src/services/two_stage_pipeline.py` 定义独立 Celery 应用与任务：`two_stage.parse`（仅 MinerU 解析，GPU 队列）、`two_stage.vision`（单图视觉请求，视觉队列）、`two_stage.merge`（汇总）、`two_stage.dispatch`（fan-out+合并 orchestrator）。队列名可由 `CELERY_TASK_PARSE_QUEUE`/`CELERY_TASK_VISION_QUEUE`/`CELERY_TASK_DISPATCH_QUEUE`/`CELERY_TASK_MERGE_QUEUE` 控制，默认沿用 `CELERY_TASK_MINERU_QUEUE` / `default` / `queue_vision`。工作空间默认 `MINERU_TASK_STORAGE_DIR`，解析完成后在 merge 清理。
  - 两段式 Celery 在 Redis broker 下设置 `broker_transport_options.queue_order_strategy=priority`，多队列 worker 会按 `-Q` 顺序优先消费（例如 `queue_parse_urgent` 优先于 `queue_parse_gpu`）。
  - `two_stage.dispatch` 通过任务替换（`self.replace`）触发 chord/merge，避免在 Celery task 内同步 `result.get()` 导致的阻塞/报错。
  - 新增 `src/routers/two_stage_router.py` 暴露 `/two_stage/task`、`/two_stage/task/{task_id}` 与 `/two_stage/queue_status`，已在 `src/main.py` 默认挂载。支持 PDF 及 Office（API 侧先用 `maybe_convert_to_pdf` 转 PDF），`chunk_type`/`return_txt`/`provider`/`model`/`prompt` 可选；`queue_status` 仅在 Redis broker 下返回 normal/urgent 队列 ready 与 unacked 计数，用于上游背压与运维观察。
  - `/two_stage/task` 新增 `priority` 表单字段（Swagger 枚举 normal/urgent）；`urgent` 时会把解析/视觉/调度/汇总任务路由到 `queue_*_urgent` 队列，其余值走 normal 队列。
  - 使用 normal 队列时，API 进程需将 `CELERY_TASK_PARSE_QUEUE`/`CELERY_TASK_VISION_QUEUE`/`CELERY_TASK_DISPATCH_QUEUE`/`CELERY_TASK_MERGE_QUEUE` 设置为与 worker 监听一致（解析队列默认沿用 `CELERY_TASK_MINERU_QUEUE`= `queue_normal`），避免投递到无人消费的队列。
  - Worker 示例（可按需调整并发）：解析队列 `celery -A src.services.two_stage_pipeline worker -Q queue_parse_gpu -P threads -c 1 -l info`；视觉队列 `celery -A src.services.two_stage_pipeline worker -Q queue_vision -P threads -c 32 -l info`；调度队列 `celery -A src.services.two_stage_pipeline worker -Q queue_dispatch -P threads -c 4 -l info`；汇总队列（处理 merge）`celery -A src.services.two_stage_pipeline worker -Q default -P threads -c 4 -l info`。调度与汇总拆分可避免 dispatch 阻塞 merge 导致 chord 一直处于 active 状态。`submit_two_stage_job` 帮助方法可直接在代码中调用。
  - `queue_*_urgent` 队列名可通过 `CELERY_TASK_*_URGENT_QUEUE` 覆盖，确保与 worker 的 `-Q` 参数一致。
  - 该示例用于解耦 MinerU 解析与视觉阶段，避免 GPU 和视觉卡互相空转；已通过 `main.py` 暴露路由，可直接在现有服务中访问 `/two_stage/*`。
  - 两段式解析中如 SDK 无结果或图片资产缺失，`parse_doc` 会抛出 `RuntimeError`，并将异常继续冒泡，`two_stage.parse` 会捕获并带上源文件路径返回给 Flower，避免再出现 “cannot unpack non-iterable NoneType object” 之类的报错。视觉阶段与 `/mineru_with_images` 对齐：`provider`/`model`/`prompt`（空字符串会清空为 None）会透传给 `vision_completion`，可通过请求参数或 `VISION_PROVIDER`/`VISION_MODEL` 环境变量指定模型；路由层使用 `VisionProvider`/`VisionModel` 进行校验，Swagger 会给出枚举提示。视觉调用若抛异常，`two_stage.vision` 现在会直接失败，不再回写 `base_text` 兜底。
  - 视觉合并规则：若原图有 caption/footnote 则合并为 `<原文本>\n<视觉输出>`（不再添加 “Image Description:” 前缀）；无原始文本时直接用视觉输出，`chunk_type=true` 时图片块标记为 `type="image"`，标题/页眉/页脚仍按原规则打标，视觉输出会清理 `[Page N]`/`[ChunkType=...]` 标记以保证纯文本干净。视觉调用前会过滤图片：面积占比过小（默认 <1%，有 caption 放宽到 0.5%）、极端长宽比（>10:1）、无 caption 且体积过小（默认 <10KB）、固有分辨率过小（最短边 <96px 或像素面积过小）直接跳过；同页超过 5 张也会限流，并按文件哈希去重，减少 logo/边框等噪声送入视觉模型。
  - 对外使用和运维启动步骤见根目录 `two_stage_task_usage.md`；该文档覆盖四类 worker 的 PM2/手动启动、`/two_stage/queue_status`、批量脚本 `src/scripts/two_stage_enqueue.py` 与常见故障排查。
- PM2 模板：`ecosystem.config.json` 仅作为 API 入口模板，当前收敛为 `gunicorn -w 4 --timeout 1900 --graceful-timeout 1900 --keep-alive 30 --max-requests 500 --max-requests-jitter 50`，确保同步 MinerU 解析的大文件请求优先由 `MINERU_*_HARD_TIMEOUT_SECONDS` 控制，不会被 Gunicorn 300/60 秒窗口提前误杀；大吞吐解析仍应走 Celery/two-stage 队列，避免 HTTP worker 长时间占用。`ecosystem.vllm.config.json`、`ecosystem.vllm.parallele.config.json`、`ecosystem.vllm.quatro.json` 均配置 PM2 `max_restarts`、`min_uptime`、`exp_backoff_restart_delay`、`kill_timeout`，防止 vLLM 启动失败时形成重启风暴。`ecosystem.two_stage.celery.json`（parse/vision/dispatch/merge worker 监听 urgent+normal 队列，按 `-Q` 顺序优先消费：`queue_parse_urgent,queue_parse_gpu`；`queue_vision_urgent,queue_vision`；`queue_dispatch_urgent,queue_dispatch`；`queue_merge_urgent,default`）；`ecosystem.two_stage.flower.json`（两段式 Flower，默认 5555 端口，继承两段式队列环境）。

## 配置与敏感信息
- 所有默认配置来自 `.secrets/secrets.toml`，通过 `src/config/config.py` 读取；文件顶部会先 `load_dotenv()`，确保 `.env` 环境变量优先级更高（容器/CI 可直接覆盖）。敏感字段包括 FASTAPI Bearer Token、OpenAI/Gemini/VLLM API Key 等。
  - 运行/调试方式：优先在 `.env` 中放敏感值与运行时模型选择；`ecosystem.config.json` 仅用于非敏感覆盖（如超时参数），避免在 PM2 配置中写入密钥或 vLLM base_url。PM2 启动时先加载 `.env`，再应用 `env` 块覆盖同名字段。
- 关键环境变量：
  - `FASTAPI_AUTH` / `FASTAPI_BEARER_TOKEN` / `FASTAPI_MIDDLEWARE_SECRECT_KEY`：是否开启 Bearer 鉴权及令牌值、中间件密钥。
  - `MINERU_*`：`MINERU_DEFAULT_TIER=standard`、`MINERU_DEFAULT_METHOD=auto`、`MINERU_MODEL_SMALL_BACKEND=onnx` 为当前模板默认值；小模型运行于 CPU，`MINERU_MODEL_VLM_SERVER_URL` / `MINERU_MODEL_VLM_MODEL` 指向 Docker 推理服务。单 URL 优先于旧 URL 列表；多卡使用 `MINERU_VLLM_SERVER_URLS`。`MINERU_MODEL_VLM_HTTP_TIMEOUT` / `MINERU_MODEL_VLM_MAX_CONCURRENCY` 默认配置为 600/8；`MINERU_HOME` 指定模型和配置目录。旧 lang 参数不再传给上游。
    - 旧 `MINERU_HYBRID_BATCH_RATIO` / `MINERU_HYBRID_FORCE_PIPELINE_ENABLE` 不再透传；使用 `MINERU_MODEL_SMALL_BACKEND` 明确小模型资源，使用 VLM 的 timeout/concurrency 配置控制模型请求。
    - `MINERU_MODEL_VLM_API_KEY` 配置 Docker VLM 的 Bearer key；旧 `MINERU_VLLM_API_KEY` / `MINERU_VLLM_AUTH_HEADER` 仅兼容 Bearer，非 Bearer 认证明确报错。连接配置每任务独立构造，避免全局配置竞争。
    - `MINERU_OFFICE_CONVERT_TIMEOUT_SECONDS`：LibreOffice Office→PDF 转换超时时间（默认 180s），超时会终止转换并返回 500。
    - `MINERU_TEXT_LAYER_CHECKBOX_RECONCILE` / `MINERU_TEXT_LAYER_TIMEOUT_SECONDS`：控制 PDF 文本层 checkbox/radio 状态回填（默认开启）及 `pdftotext -bbox` 超时时间（默认 30s）。该功能只在 MinerU 输出中已有 checkbox 符号时触发，用于修正长文本 PDF 个别页面的选中态漏判。
    - `OPENAI_API_KEY` / `GENIMI_API_KEY`：备用视觉/生成模型凭证，代码仍支持，但默认 `.env` / `.env.example` 已不再把它们加入视觉 provider 白名单。
    - `VISION_PROVIDER` / `VISION_PROVIDER_CHOICES` / `VISION_MODELS_*`：视觉 provider 选择与模型白名单；当前默认配置为 `VISION_PROVIDER=vllm`、`VISION_PROVIDER_CHOICES=vllm`。
  - `VISION_BATCH_SIZE`：`/mineru_with_images` 视觉描述的批处理并发度（默认 3，最小 1），调整以配合模型限流。
  - `VLLM_BASE_URL` / `VLLM_BASE_URLS` / `VLLM_API_KEY`：指定 vLLM 视觉服务地址/凭证；必须提供 `VLLM_BASE_URL` 或 `VLLM_BASE_URLS` 才会启用 vLLM 视觉 provider，`VLLM_API_KEY` 仅作为可选认证头。配置多个 URL 时，每次请求会按轮换顺序逐个尝试直到成功或全部失败。
  - `VLLM_ENABLE_THINKING`：控制 vLLM 多模态请求 `chat_template_kwargs.enable_thinking`（默认 false；设置为 `true/1/yes/on` 可开启思维链式推理，通常会增加响应时延）。
  - `VLLM_VISION_TEMPERATURE` / `VLLM_VISION_TOP_P` / `VLLM_VISION_PRESENCE_PENALTY`：控制 vLLM 多模态请求的采样参数（默认 `1.0` / `1.0` / `2.0`）。
  - `VLLM_VISION_TOP_K` / `VLLM_VISION_MIN_P` / `VLLM_VISION_REPETITION_PENALTY`：控制 vLLM 多模态请求 `extra_body` 参数（默认 `40` / `0.0` / `1.0`）。
    - `MINIO_*`：MinIO 凭证与目标桶。
    - `CUDA_VISIBLE_DEVICES`：运行时显卡绑定。
    - 旧 `MINERU_HYBRID_BATCH_RATIO` / `MINERU_HYBRID_FORCE_PIPELINE_ENABLE` 不再透传；使用 `MINERU_MODEL_SMALL_BACKEND` 明确小模型资源，使用 VLM 的 timeout/concurrency 配置控制模型请求。
  - `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND`：Celery broker/结果存储（默认均指向 `redis://localhost:6379/0`）；`CELERY_TASK_DEFAULT_QUEUE`（默认 `default`）、`CELERY_TASK_MINERU_QUEUE`（默认 `queue_normal`）、`CELERY_TASK_URGENT_QUEUE`（默认 `queue_urgent`）控制队列名，`CELERY_RESULT_EXPIRES` 控制结果过期时间（秒）。
  - 两段式队列：`CELERY_TASK_PARSE_QUEUE`/`CELERY_TASK_VISION_QUEUE`/`CELERY_TASK_DISPATCH_QUEUE`/`CELERY_TASK_MERGE_QUEUE` 控制 normal 队列；对应 urgent 队列可用 `CELERY_TASK_PARSE_URGENT_QUEUE`/`CELERY_TASK_VISION_URGENT_QUEUE`/`CELERY_TASK_DISPATCH_URGENT_QUEUE`/`CELERY_TASK_MERGE_URGENT_QUEUE` 覆盖（默认 `queue_parse_urgent`/`queue_vision_urgent`/`queue_dispatch_urgent`/`queue_merge_urgent`）。
  - `MINERU_TASK_STORAGE_DIR`：MinerU Celery 任务的本地落地目录，默认 `tempfile.gettempdir()/tiangong_mineru_tasks`，需保证 worker 与 API 主进程均可读写。
- 本仓库默认将 `.secrets/` 视为外部私有目录，确保部署前准备好相应文件。

## 环境准备与运行
- 推荐使用 [uv](https://docs.astral.sh/uv/) 管理 Python 3.12 及依赖：`uv python install 3.12` → `uv sync --locked`；`.python-version` 将默认解释器固定为已验证的 3.12 系列。
- `pyproject.toml` 与 `uv.lock` 将应用环境升级到 MinerU 4、FastAPI 0.141.1、Starlette 1.6.0、Transformers 5.17.0 等；OpenAI SDK 受 MinerU `<3` 限制，锁定最新兼容 2.54.0。后端按官方 Docker 基线保留 vLLM 0.21.0，镜像中安装 instrumentator 8.1.0 以支持新 Starlette。应用不安装 vLLM；Gunicorn 改用独立 `uvicorn-worker` 包。
- 系统依赖需通过 `apt` 安装：`libmagic-dev`、`poppler-utils`、`libreoffice`、`pandoc`、`graphicsmagick` 等，以支持 Office 转 PDF、文档解析及图片处理。
- 首次运行需下载 MinerU 模型：
  ```bash
  MINERU_MODEL_SMALL_BACKEND=onnx uv run mineru-kit models download --tier basic --small-backend onnx --source modelscope
  MINERU_MODEL_SMALL_BACKEND=onnx uv run mineru-kit models verify --tier basic --small-backend onnx
  ```
- 启动方式：
  ```bash
  MINERU_MODEL_SOURCE=modelscope uvicorn src.main:app --host 0.0.0.0 --port 7770
  ```
  也可按 README 中示例在多 GPU 上启动多个实例，或使用 `pm2 start ecosystem.config.json` 等文件管理进程。
- Celery/Flower：
  ```bash
  # 启动 worker（监听 urgent + normal + default，确保 src.services.tasks 被发现）
  # 注意：GPU 调度内部再起子进程，Celery worker 需使用非 daemonic 池，推荐 -P solo -c 1
  CELERY_BROKER_URL=redis://localhost:6379/0 uv run celery -A src.services.celery_app worker -l info -Q queue_urgent,queue_normal,default -P solo -c 1
  # 监控
  CELERY_BROKER_URL=redis://localhost:6379/0 uv run celery -A src.services.celery_app flower --address=0.0.0.0 --port=5555
  ```
  - PM2 模板：`ecosystem.celery.json` 以 `.venv/bin/python` 作为 interpreter 执行 `.venv/bin/celery`，避免 PM2 默认用 Node 解释脚本导致 SyntaxError。
  - Flower PM2 模板：`ecosystem.celery.flower.json` 同样用 `.venv/bin/python` 解释 `.venv/bin/celery`，默认 `--address=0.0.0.0 --port=5555`，环境中写死 broker/result backend 为本地 Redis。
- 附属服务容器：
  - Kroki（图表渲染）：`docker run -d -p 7999:8000 --restart unless-stopped yuzutech/kroki`
  - Quickchart：`docker run -d -p 7998:3400 --restart unless-stopped ianw/quickchart`
  - MinIO：参考 README 使用 `quay.io/minio/minio` 镜像。
  - MinerU VLM：`docker compose -f compose.mineru.yaml up -d --build`；`MINERU_DOCKER_GPU_ID` / `MINERU_DOCKER_PORT` / `MINERU_DOCKER_GPU_MEMORY` 控制 GPU、端口和显存比例。`ecosystem.vllm*.json` 兼容入口也已改为 Docker Compose 包装器。

## 开发与质量保障：
- 每次修改代码后，确保运行以下代码格式与质量检查的命令，确保程序质量：
  ```bash
  uv run --group dev black .
  uv run --group dev ruff check src
  uv run --group dev pytest
  ```
- 新增的 `tests/` Pytest 测试工程覆盖配置环境变量覆盖逻辑、Markdown→DOCX/文件转换工具、MinIO 封装、Pydantic 模型以及 `/health`、`/gpu/status` 等轻量路由；`tests/conftest.py` 会注入轻量替身（GPU 调度器、MinIO/pypdfium2 stub），无需真实外部依赖即可运行。视觉相关新增 `tests/test_mineru_with_images_service.py`（验证视觉调用失败会直接抛错，以及 `.docx + return_txt=true` 的 native DOCX txt-only 路径会按文档顺序插入图片识别内容，且图片提示词收紧为严格 OCR / 可见内容抽取模式）、`tests/test_mineru_with_images_router.py`（验证同步 `/mineru_with_images` 对 `.docx + return_txt=true` 会把原始 DOCX 路径透传给调度层的 txt-only native 分支、未知 provider/model 不再返回 422、拒绝 Markdown 上传，且 `chunk_type=true` 不会把所有页眉移到结果开头）、`tests/test_mineru_reading_order.py`（验证 `/mineru`、`/mineru_sci` 和普通 Celery runner 在 `chunk_type=true` 时保持 MinerU 原始阅读顺序）、`tests/test_mineru_with_images_task_router.py`（验证图像版 Celery 入队接口在未知 provider/model 下仍可成功入队，并拒绝 Markdown 上传）、`tests/test_vision_service.py`（验证未知 model 会回退到 `.env` 中的 `VISION_MODEL`）、扩展 `tests/test_vision_service_openai_compatible.py`（验证 vLLM 需要 base URL 才视为可用、多个 endpoint 会顺序尝试）、`tests/test_pdf_text_layer_reconcile.py`（验证 PDF 文本层 checkbox/radio 回填、重复选项不串改及环境变量关闭）以及 `tests/test_two_stage_pipeline_parse.py`（验证 MinerU 兼容层会回读 `_content_list.json`、缺失时抛错、`two_stage.vision` 失败时不再回写 `base_text`，以及 two-stage 合并保持 MinerU 原始阅读顺序）。两段式相关：`tests/test_two_stage_router.py` 覆盖 `/two_stage/task` 的无扩展名错误、成功入队和 `/two_stage/queue_status` Redis ready/unacked 统计（通过 monkeypatch stub 掉 Celery/Redis），批量送入两段式 Celery 的脚本移到 `src/scripts/two_stage_enqueue.py`（每批 5000 个提交；默认读取 `pdfs` 目录提交 `/two_stage/task`，轮询完成后将响应中的 result 持久化到 `pickle/<stem>.pkl`，失败/超时会在脚本内自动重试至多 3 次，超限后记录并继续其余文件；输入/输出目录用 `TWO_STAGE_INPUT_DIR`/`TWO_STAGE_OUTPUT_DIR` 覆盖，兼容 `ESG_INPUT_DIR`/`ESG_OUTPUT_DIR`，轮询间隔/超时用 `TWO_STAGE_POLL_INTERVAL`/`TWO_STAGE_POLL_TIMEOUT` 覆盖，优先级用 `TWO_STAGE_PRIORITY`（normal/urgent）控制，超时计时从 Celery 状态变为 `STARTED` 后开始）。
- `src/scripts/read_pickle.py` 可将 pickle 文件转存为 JSON，默认输出到同名 `.json` 文件；可用 `--field result` 仅导出解析结果，`-o` 自定义输出路径。未传入参数时会自动选择 `./pickle` 目录下最新的 `.pkl` 进行转换，便于直接查看两段式任务落地的 pickle 内容。
- 代码中针对 Ruff 规则（F401/BLE001/E722 等）已统一清理未使用依赖，并将异常捕获限定在预期类型；后续新增 try/except 块时请保持同等粒度。
- 图像增强流程的 `_log_vision_prompt` 使用 `logger.debug` 输出前后文，默认不会污染 info 级日志，如需调试可上调日志级别。
- 建议通过 `/health` 做存活探测，`/gpu/status` 监控排队，MinIO 接口应配合真实服务验证。
- 解析路径经常涉及临时文件，注意及时释放；`gpu_scheduler` 已在 finally 中做清理，但新增逻辑时需保持一致。
- 运维清理解析残留进程时，不要按运行时长或进程名全局误杀；应先确认当前 PM2/Gunicorn master 子树，只清理已经脱离当前服务树且匹配本项目解析命令行的 orphan 进程。
- VS Code 调试配置（`.vscode/launch.json` 中的 `UnstructureServe`）已关闭 `.venv/**`、`input/**`、`output/**`、`pdfs/**` 等大目录的 reload 监听，以加快首启扫描。

## 常见运维提示
- 若端口被占用，可参考 README 中的 `lsof` + `kill` 脚本快速清理 7770/8770-8772。
- 解析失败常见原因：
  - Pandoc 未安装或 PATH 配置错误。
  - MinIO 连接参数缺失或证书配置不当。
  - GPU 资源不足导致 MinerU 调度超时。
- Office → PDF 转换：每次调用都会为 LibreOffice 创建独立 profile 目录，避免 `.config/libreoffice` 上的锁文件互相影响；若转换超过超时时间会强制中止并清理遗留 `soffice` 进程。

## 协作约定
- **重要：以后每次修改，都要同步修改 `AGENTS.md`，确保本文档与代码状态一致。**
- 引入新依赖、环境变量、路由或调整解析流程时，请在此文档补充背景、关键入口与测试方式，方便后续代理与开发者快速接手。

## MinerU 4 升级专项验证
- Docker Compose 显式指定 `runtime: nvidia`；本机 Container Toolkit 为 CDI 模式，仅设置 GPU reservation 会触发 NVIDIA runtime hook 错误。
- Dockerfile 补齐基础镜像 PyGObject 所需的 Pycairo 1.29.1/Cairo 开发依赖，OpenAI SDK 与应用统一固定为 2.54.0，并在构建时执行 `pip check`。
- 请求级 tier 由 FastAPI 枚举校验；API 的 standard 缺省值不受进程环境档位影响，直接服务调用保留环境变量兜底。`tests/test_mineru_tier_routes.py` 覆盖六个入口的四档透传、缺省值、非法值拒绝及 OpenAPI 枚举，队列替身隔离上传工作区；`input/p2.pdf` 真实模型回归扩展至四档。
- basic 档的 p2 实测暴露“公开竞争”前选中符号完全丢失；`pdf_text_layer_reconcile.py` 增补受限回填：同页文本层选项组唯一匹配、整组标签及顺序精确相同、单元格还保留至少两个 checkbox、首标签至少四字时，才补回首个符号。歧义组、跨页、短标签和带额外文字的单元格不补；回归覆盖这些拒绝条件，原有状态校正仍保留。
- 请求级 tier 更新后，常规测试 135 项通过（真实模型测试默认跳过 17 项）；单独启用 p2 四档回归全部通过。测试日志保存在 `output/mineru4_tdd/tier-*.log`，真实结果为 `tier-p2-results.xml`。
- MinerU2.5-Pro-2605-1.2B 原生上下文长度为 8192；Compose 按模型配置限制 `--max-model-len`，不强行外推上下文。
- 2026-09-17 隔离环境实测：79 项常规测试通过；11 份 input PDF 的 15 项模型回归全部通过（29 个抽样页及 p2/九页论文/46 页 fese 整本，累计覆盖 79 个不同 PDF 页面）。论文图片还验证 two-stage 图片筛选和一次真实视觉调用；同步 API、MinIO PDF/JSON/JPEG/meta、Office→PDF 及 native DOCX 顺序通过冒烟验证。
- `tests/test_mineru4_adapter.py` 覆盖新 SDK 的结果保存、图片引用、字段映射、页范围、bbox 单位、Docker endpoint 轮换与认证错误；图片和 chart 内嵌的识别内容会保留到旧 caption 字段，避免普通解析链路丢失可见文本。
- `tests/test_mineru_input_pdfs.py` 读取 `input` 的 11 份 PDF；p2/论文跑整本，其余文件抽样首页、第 11 页及末页；额外对 fese.pdf 整本 46 页验证 is_full_document 与全部源页码，覆盖默认整本调用的前 10 页截断风险。使用 `MINERU_RUN_INPUT_PDFS=1 uv run --group dev pytest tests/test_mineru_input_pdfs.py -v` 启用真实模型回归；默认常规测试跳过，缺文件时显式失败。
- Black 显式排除任意层级的 `.venv` 以及根目录 output/input/pdfs/pickle，防止格式化依赖备份和解析产物。
- Ruff 0.16 扩大默认规则集，仓库显式保留原 E4/E7/E9/F 规则；Black 目标固定 Python 3.12，避免跨目标语法变更。所有代码修改后仍运行上面的 Black/Ruff/Pytest 命令。

## 本机升级落地记录（2026-09-17）
- 本机运行目录使用合并后的 `main` 分支，运行 MinerU 4.0.0，应用依赖已按 `uv.lock` 安装；升级开发分支为 `codex/mineru4`。API 保持 7770；`.env` 中的 MinerU URL 指向 Docker 的 127.0.0.1:31000。
- Compose project 为 `mineru4-upgrade`（通过本机 `.env` 的 `COMPOSE_PROJECT_NAME` 设置）；GPU 0，显存比例 0.09，容器模型与编译缓存保存在命名卷。应用 CPU 模型目录为 `/home/david/.local/share/tiangong-mineru4`。
- 原 MinerU 原生 PM2 进程已删除；API 和四类 two-stage worker 已切换新版依赖并重启，PM2 进程列表已保存。部署后同步 `/mineru` 与真实 Celery `/two_stage/task` 均通过 p2 回归。
- 请求级 tier 更新已重新加载 API 和 two-stage parse worker：六个在线接口的 Swagger 缺省/非法值校验通过，`/mineru` 使用 p2 验证缺省及四个显式档位，two-stage 的 basic 任务完成真实 dispatch/parse/merge。结果与日志为 `output/mineru4_tdd/tier-api-*`；Docker VLM 配置保持原部署。
- 原代码、配置、PM2 快照和旧 `.venv` 保留在忽略且权限受限的 `output/mineru4_tdd/rollback-20260917/`，未删除旧模型缓存。
- 发布前核对确认原 MinerU 本机 vLLM 后端及其 PM2 启动项已移除，应用 `.venv` 未安装 vLLM；Docker 容器使用 `unless-stopped` 重启策略。本机另一个仓库的 `vllm-qwen3-embedding-8b` 是独立 embedding 服务，不属于本项目解析后端。
