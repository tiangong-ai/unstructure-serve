# TianGong AI Unstructure Serve 代理说明

仓库为 `tiangong-ai/unstructure-serve`。当前运行基线是 MinerU 4.0.2 + CPU ONNX 小模型 + Docker vLLM，应用依赖由 `uv.lock` 固定，部署使用 Python 3.13.15。API 与 worker 仍在应用环境运行，`.venv` 使用基础 mineru + CPU ONNX，不安装 Torch/vLLM；四档不依赖 all/full extra。当前部署验收见部署记录的 Python 3.13 / 4.0.2 章节。

## 文档与修改约定

- **每次修改代码、配置或说明，都同步更新本文件涉及的规则或入口。**
- 新增 [AI 接入指南](docs/ai-integration.md) 与 [调优指南](docs/performance-tuning.md)。调优文档正常纳入 Git，但仅供仓库开发运维使用，不通过服务路由、静态目录或 llms.txt 暴露。接口变更同步更新 AI 指南；硬件/调度规则变更同步更新调优指南。
- [依赖与 Python 审计](docs/dependency-audit-2026-09-18.md)及[逐包清单](docs/dependency-inventory-2026-09-18.csv)为开发维护记录，不通过文档服务提供。审计候选不等于线上版本：MinerU 4.0.2 基础包在 ONNX+Docker 下四档已做隔离验证，all/full 不是四档开关；生产依赖仍以 uv.lock 与部署记录为准。Python 3.14 的默认 forkserver 与实际 semaphore 退出警告须专项处理，不能仅凭常规测试通过迁移。
- 当前操作以 [README](README.md)、[部署与回归](docs/mineru_4_upgrade_usage.md)、[普通异步任务](docs/mineru_with_images_task_usage.md)、[two-stage](docs/two_stage_task_usage.md) 为准。
- [历史文档](docs/history/README.md)保留原版本的样本、评估和测试结果，不作为当前安装步骤。[多卡计划](docs/multi_gpu_vllm_scaling_todolist.md)明确区分已实现与待验证能力。
- `.env`、`.secrets/`、输入文件、模型、结果、日志和回滚环境保持私有。公共配置骨架为 `deploy/secrets.example.toml`；不要在 PM2 模板写凭证或实际视觉服务地址。
- README 保持功能和入口简明；部署细节集中到部署说明，队列/字段细节集中到对应任务文档。例子中的默认值必须区分代码缺省、模板值和本机覆盖。
- README 顶部提供可复制给 AI 的初始化启动、故障恢复、安全停止指令，覆盖 model/app/ordinary 三个部署组及依赖检查。停止须先停提交来源并等已提交任务及阶段队列收敛；共享 Redis/独立图片模型先识别归属，不随本项目停机。CLI 中断不取消服务端任务，恢复保留原记录；不以 /health 或 /ready 单项成功代替完整验收。

- 根目录文档只保留 README.md 与 AGENTS.md；专题说明位于 docs，PM2 模板位于 deploy/pm2，Compose/Docker 位于 deploy/mineru-vllm。统一运维入口为 deploy/manage.sh，start 跳过已 online/launching 的进程，更新配置用 restart；PM2 cjs 解析绝对项目路径；直接调用 JSON 模板必须在仓库根目录。Gunicorn 参数集中 deploy/gunicorn.conf.py，缺省 4 worker、5000+0..500 请求回收，PM2 退出窗口 1900 秒。

## 主要入口

| 文件 / 目录 | 职责 |
| --- | --- |
| `src/main.py` | FastAPI 路由、Bearer 鉴权、日志和 shutdown；退出时等待 scheduler 收敛 |
| `src/routers/guides_router.py` | 仅提供 `/llms.txt` 索引和 `/guides/ai-integration.md` 原始 Markdown，继承全局 Bearer；不挂载 docs 或仓库目录 |
| `src/routers/` | 同步/异步解析、Markdown、健康检查与队列状态 |
| `src/scripts/batch_parse.py` / `batch_runner.py` | 三个异步入口的统一批量 CLI、流式上传、滚动任务与 JSON 续跑；旧 two-stage 脚本复用调度核心 |
| `src/services/mineru_service_full.py` | MinerU 4 SDK 兼容层、保存资产、归一化 Content List V1 |
| `src/services/gpu_scheduler.py` | 排队、进程隔离、hard timeout、任务进程组收尾 |
| `src/services/mineru_with_images_service.py` | 图片描述并发及同步 DOCX TXT 增强 |
| `src/services/mineru_task_runner.py` / `tasks/mineru_tasks.py` | 普通 Celery 任务，复用 scheduler 与响应合同 |
| `src/services/two_stage_pipeline.py` | 独立 Celery app 的 parse/dispatch/vision/merge |
| `src/services/vision_service.py` / `vision_service_openai_compatible.py` | provider/model 兜底与 OpenAI-compatible 客户端池 |
| `src/services/vision_prompts.py` | 视觉提示词；原生 DOCX 图片使用严格 OCR |
| `src/services/pdf_text_layer_reconcile.py` | 按同页 PDF 文本层修正 checkbox 状态 |
| `src/utils/file_conversion.py` / `mineru_support.py` | Office 转 PDF 及本服务扩展名边界 |
| `src/utils/text_output.py` | 共享 Unicode 清理、TXT 拼接和视觉标记清理；不重复 UTF-8 编解码 |

## 解析合同

- 六个 POST 入口均有 `tier` 表单枚举：`flash/basic/standard/advanced`，不传固定 `advanced`，非法值 422。枚举位于 `src/utils/mineru_backend.py`；任务通过现有 `backend` / `backend_value` 字段保存提交时的选择。
- `/mineru`、`/mineru_sci`、`/mineru_with_images` 和两个普通 `/task` 的 `chunk_type`、`return_txt` 是 **query 参数**；仅 `/two_stage/task` 将它们定义为 form。文档、curl 和脚本示例必须按真实 OpenAPI 编写。
- 直接服务调用未指定档位时读取 `MINERU_DEFAULT_TIER`，再兼容旧 backend；两者均未配置时使用 `advanced`。旧映射：pipeline→basic、hybrid→standard、vlm→advanced。旧名称只影响档位，大模型推理仍走 Docker；不要重新引入本机引擎。
- PDF、受支持图片和 Office 转 PDF 清单才是服务输入边界；Markdown/TXT 拒绝。不要因上游新增格式而自动扩大接口范围。
- `parse_doc()` 使用无状态 `mineru.parser.parse`，默认 PDF `page_range=all`。零基 start/end 转成一基范围，保留源页号；返回 `(content_list, artifact_dir, None)`。先保存 MiddleJson/图片，再渲染 V1 并补齐旧字段；图片引用缺失必须失败。
- `img_caption/img_footnote`、chart/code/index/page_footnote 映射和 bbox 单位需保持下游兼容；同时写旧命名 `_content_list.json` 供诊断。异常继续冒泡，不返回空值伪装成功。
- Office 主结果始终先经 LibreOffice 转 PDF，再使用所选 tier；每次转换使用独立 profile 并在超时后收尾。
- 仅同步 `/mineru_with_images` 的 `.docx + return_txt=true` 使用额外原生 DOCX flash 分支生成 txt，result/页码 仍来自 PDF。该分支图片按原文顺序插入严格可见内容 OCR，不附加 caption/footnote 或根据上下文推断实体。普通任务和 two-stage 不启用该分支。
- `chunk_type=true` 保留 title/header/footer 和原阅读顺序，视觉增强的图片块标 image，忽略 page_number 块；普通正文/表格可能没有 type。txt 按同序拼接，标题段后两个换行，普通段后一个换行。普通响应省略 null 字段、任务失败查询返回 500；two-stage 保留 null、任务失败查询返回 200 状态体，调用方仍须检查 state。
- checkbox 修正在已有 checkbox 符号时触发 `pdftotext -bbox`，按源页和选项匹配；缺工具、超时或抽取失败则跳过。补回缺失首符号仅允许同页唯一完整选项组、至少两个其他符号仍在、首标签至少四字；歧义、短标签、额外文字或跨页不得补。环境开关与超时见部署配置。

## 视觉与资产

- 默认视觉 provider 为 vLLM；OpenAI/Gemini 实现仍可显式配置。未知 provider/model 在同步图片接口及普通图片任务中宽松接收，由服务兜底；two-stage 则在路由层校验枚举并可返回 422。
- vLLM 必须有 `VLLM_BASE_URL(S)` 才可用，API key 可选。此地址是独立图片描述模型，与 `MINERU_MODEL_VLM_SERVER_URL` 不同。
- OpenAI/vLLM 复用客户端池；多个视觉 endpoint 会顺序尝试。不要把视觉故障切换能力误写成 MinerU 解析端点的能力；MinerU 多 URL 池只有进程内轮换；三卡部署的单 URL 由容器内 vLLM 做请求负载均衡。
- 视觉请求默认 `enable_thinking=false`，采样参数由 `VLLM_VISION_*` 覆盖。同步图片采用单线程池滚动补位，由 `VISION_BATCH_SIZE` 控制每请求在途上限（代码/模板 3），不是所有 API 进程共享限额，也不控制 Celery vision threads/32；上下文在请求前固定，不将生成描述回灌为后续上下文。视觉异常使请求/任务失败，不使用 base_text 降级。OpenAI-compatible 空响应或非 stop 结束必须失败，不能接受被截断内容。Qwen3.5 部署采样模板为 temperature/top_p/top_k/presence_penalty=0.7/0.8/20/1.5，通用代码默认仍为 1/1/40/2。
- 默认图片提示词保留图中数字、单位、标签和关系，合并同类数据，避免重复 caption、无关引言和推断数值；不压缩图片或按字数硬截断。清理仅处理开头完整 thinking 段和确定的中英文套话，保留正文及不确定性。原生 DOCX 严格 OCR 不启用新增套话清理，避免误删原图文字。自定义 prompt 继续优先。
- two-stage 图片筛选按相对面积、分辨率、体积、长宽比、每页数量及哈希去重，合并保持原位；清理视觉输出中的 Page/ChunkType 标记和固定说明前缀。

- 六个解析上传入口统一通过 `src/utils/upload_io.py` 在线程池内按 1 MiB 分块持久化；Office 和 broker 提交不直接阻塞事件循环。同步解析用 shield/wrap_future 等待，HTTP 超时后源文件延迟到实际任务结束再清理。Pydantic 响应直接序列化 JSON，保留 null/pretty 合同。

## 队列与进程

- 普通 app `src.services.celery_app` 消费 `queue_urgent,queue_normal`；普通 worker 不消费 two-stage 的解析/视觉/default merge 队列，防止不同 Celery app 抢到未注册任务。
- two-stage 部署显式配置 normal 队列 `queue_parse_gpu/queue_vision/queue_dispatch/default`；四类 urgent 为 `queue_parse_urgent/queue_vision_urgent/queue_dispatch_urgent/queue_merge_urgent`。API 与每个 worker 必须配置一致，不能只改 worker 的 `-Q`。
- 未配置时代码的 parse 回退到普通队列，dispatch/merge 回退到 default；`.env.example` 显式列出与 PM2 匹配的队列，详细规则见 two-stage 文档。
- Redis 优先级消费按 `-Q` 的 urgent→normal 顺序。dispatch 使用 `self.replace` 启动 chord；不要在 Celery task 内阻塞调用 `result.get()`。四个阶段都要有消费者和可用的 result backend。
- API 与 worker 共享 broker/backend/任务目录；跨容器时目录绝对路径一致。`PENDING` 也可能是未知或过期 ID，`queue_status` ready/unacked 不等于最终结果。
- scheduler 每个历史 GPU_IDS 池缺省有 3 个派发进程（MINERU_SCHEDULER_WORKERS），避免 HTTP 连接亲和造成单池串行；实际解析总数仍由共享槽位限制。scheduler 在独立子进程中解析，Linux 使用 parent-death signal 和任务进程组；仅在 hard timeout、父进程退出或结果返回后清理该任务组。不能按名称/运行时长全局误杀解析进程。
- 隔离任务在成功/失败后显式关闭本任务已加载的 DocVortex 渲染池，避免 Python 等待嵌套进程导致固定退出延迟；保留原 hard timeout 与进程组收尾作为兜底，不通过缩短等待或提前返回来跳过清理。
- 普通模板 threads/16，可用 solo/1 保守运行；two-stage parse 为三个独立 solo/1 worker（名称 parse、parse-2、parse-3），均消费 urgent/normal，prefetch=1；其余线程池。每个解析 worker 的 VLM 并发为 8，PM2 停止窗口 1900 秒；并非全局并发上限。避免 daemonic prefork；保持临时文件 finally 清理和正常 shutdown 等待。
- API 和 parse PM2 模板均显式设置 processing window=64，减少长文档渲染内存；这是内部窗口大小，保留整本解析和跨页后处理，不是页数上限。不要为了多卡吞吐先把 PDF 拆成独立单页任务。
- CPU ONNX 模板每个模型会话的 intra/inter 线程数为 16/1，防止高核数机器上自动线程池过度竞争；这是本机混合 PDF 测量后的配置，不是整个进程的线程上限。VLM 并发保持 8；4 线程及 VLM 16 均有对照证据，不凭单个短文档结果扩大并发。

- `parse_capacity.py` 通过 Linux flock 在同一主机的 API 子进程/普通任务/two-stage 间共享解析槽，缺省 `MINERU_PARSE_SLOTS=3`、等待上限 1800 秒；目录由 `MINERU_PARSE_SLOT_DIR` 指定，缺省系统临时目录下 `tiangong_mineru_parse_slots`。所有参与进程必须使用相同目录/槽数；不是跨主机分布式锁。不删除正在使用的锁文件；fork 的渲染子进程关闭继承租约，进程退出自动释放。scheduler hard timeout 包括等待槽位时间。

## 配置与运维

- 进程环境优先于 `.env`，再回退到 `.secrets/secrets.toml`。PM2 `env` 属于进程环境，不会被 `load_dotenv()` 覆盖；Python 加载 `.env` 不会替调用方 shell 导出变量。
- 配置模块仍要求 TOML 的 FASTAPI/OPENAI/GOOGLE/VLLM 段存在；复制 `deploy/secrets.example.toml` 初始化。公开模板不得包含实际凭证；部分字段空串会回退到 TOML，不代表清除原配置。
- 三卡部署入口为 `deploy/pm2/ecosystem.vllm.parallele.config.json` → `deploy/mineru-vllm/serve.sh parallel`，合并基础 Compose 与 `deploy/mineru-vllm/compose.mineru.parallel.yaml`。单容器绑定 GPU 0/1/2，vLLM DP=3、TP=1，通过单地址内部负载均衡；不设置 external/hybrid LB。PM2 前台托管 Compose，停止超时 70 秒覆盖容器 60 秒退出窗口。单卡基础 Compose 与其他独立容器模板是可选拓扑，不同时管理同一 project。应用 `GPU_IDS` 不控制 Docker GPU。复用已有缓存卷时通过 `MINERU_DOCKER_*_VOLUME` 指定并启用 `MINERU_DOCKER_VOLUMES_EXTERNAL=true`，不删除原卷。
- Docker 基线为 vLLM 0.21.0 配套 Torch/CUDA，模型上下文 8192；应用使用独立 `uv.lock`。升级镜像时重新检查依赖与 PDF，不在应用中补装 vLLM。
- Docker Snap 的开机 CDI 扫描可能早于 UVM 设备创建；基础 Compose 显式映射 `/dev/nvidia-uvm` 和 `/dev/nvidia-uvm-tools`，启动器最多等待约 120 秒。`nvidia-smi` 正常不代表 CUDA 可用，应验证容器内实际张量计算；不要为修复本服务重启共享 Docker 或卸载 GPU 驱动。
- PM2 API 为 `unstructured-gunicorn`，Gunicorn timeout/graceful-timeout 1900 秒。科研入口另有自己的 HTTP 等待窗口；具体超时以模板/运行环境为准。
- 远程 AI 使用实际部署 `/openapi.json` 与 `/guides/ai-integration.md`，`/llms.txt` 仅为文档索引，不是 MCP。两个新增文档路由继承业务鉴权；FastAPI 自动 `/openapi.json`、`/docs`、`/redoc` 不自动继承业务 Depends 保护，需私有时由网关额外限制。索引链接保留 root_path 前缀，不链接调优文档。
- `/health` 仅检查 API 存活；`/ready` 并行检查 MinerU VLM 端点的 `/health`，不可用返回 503，不检查 Redis/独立视觉模型，也不执行实际推理。结合 `/gpu/status` 和 `/two_stage/queue_status` 检查任务状态，启用鉴权时带 Bearer。日志默认 INFO，httpx/httpcore 降到 WARNING，视觉提示词仅 DEBUG；不输出密钥或完整 PM2 环境。
- 维护先定位当前服务树和 active/reserved 任务，按具体任务清理。不要在日常说明中使用全局 PM2 删除、Redis flushdb 或无差别清空共享任务目录。

- 批量 CLI 日志缺省写入 output/logs，可用 TWO_STAGE_LOG_FILE 覆盖；tests 仅保留真正测试，已移除会在 pytest 收集时重置全局日志并一次性批量提交的旧 test_celery.py 脚本（历史可从 Git 查询）。

## 开发与验证

修改后运行：

```bash
uv run --group dev black .
uv run --group dev ruff check src
uv run --group dev pytest
```

- Black 必须排除任意层级 `.venv` 及根目录 output/input/pdfs/pickle，防止修改依赖备份。Ruff 当前显式使用 E4/E7/E9/F；保持异常处理粒度合理，不扩大吞异常范围。
- `test_guides_router.py` 验证只读 AI 指南与带 root_path 的索引，确保调优资料不被服务提供。视觉代码默认值测试必须隔离本机 `VLLM_VISION_*` 环境覆盖。
- 常规测试使用外部依赖/调度替身；`test_mineru_tier_routes.py` 验证六入口参数，`test_mineru4_adapter.py` 验证 SDK/资产，其他测试覆盖阅读顺序、DOCX、视觉和进程生命周期。
- 真实模型回归：`MINERU_RUN_INPUT_PDFS=1 uv run --group dev pytest tests/test_mineru_input_pdfs.py -v`。读取 input 的 11 份 PDF；p2 缺省及四档整本，论文和 fese 整本，其余抽样首页/第 11 页/末页。没有样本应明确失败，不用替身冒充实测。
- 视觉真实回归：`MINERU_RUN_VISION_PDFS=1 uv run --group dev pytest tests/test_vision_input_pdf.py -v` 从 input 论文第五页真实解析图像并请求已配置多模态模型，检查图中关键数值及单位；需同时具备 MinerU 与图片模型服务，不用替身。
- 三卡部署测试验证 Compose 的 GPU/DP 参数与 PM2 前台生命周期；`MINERU_RUN_DP_PDFS=1 uv run --group dev pytest tests/test_mineru_data_parallel.py -v` 使用 input 的 p2 和九页论文，并检查三个 engine 的成功推理计数均增加。测试数量与部署证据统一记录在部署说明。
- `src/scripts/benchmark_mineru.py` 对真实 PDF 做已预热 SDK 进程压测，记录批量完成、单任务服务和排队耗时；不含 Celery/独立视觉阶段。输出目录必须新建，校验整本页号、图片及 p2 关键表格/checkbox；样本与结果保持私有。脚本退出前显式收尾各进程的 DocVortex 渲染池，避免嵌套 multiprocessing 等待退出。
- `src/scripts/two_stage_enqueue.py` 的生产调用须显式 `TWO_STAGE_BASE=http://127.0.0.1:7770`，脚本缺省仍是开发端口 8770，且不传 tier（使用 advanced）。优先级演示 `enqueue_input.py` 会重复提交；不要作为生产批处理入口。
- 400–1000 页批量必须引用 AI 指南第 5.3 节与调优指南第 12 节：整本单文件验收后从 1→2→3 个在途试起，默认 6/800 秒不作千页容量承诺。当前长样本仅抽页验证；two-stage 直接 parse_doc 不走 scheduler hard timeout，late ack 仍需核对 Redis 默认一小时 visibility timeout，不能以延长客户端等待宣称长任务已经可用。调优配置细节只保存在仓库指南，不通过文档服务提供。
- 批量脚本采用滚动在途窗口（`TWO_STAGE_MAX_IN_FLIGHT`，默认 6），输出目录 `.tasks` 原子保存任务 ID/文件摘要/请求参数，重启续查已有任务。查询故障或本地等待超时不重投；只有服务端确认 FAILURE/REVOKED 才有界重试。提交响应丢失时保留 SUBMITTING 并明确停止，不能假定服务器未接收。单输出目录由文件锁限制一个 CLI 写入进程。脚本认证优先环境/.env，再回退本地 TOML 的 FASTAPI.BEARER_TOKEN，不记录令牌。
- 新批次优先 `uv run python -m src.scripts.batch_parse`，`--mode parse/images/two-stage` 覆盖全部三个异步 API；缺省 advanced、在途 2、等待 21600 秒、尝试 1 次、chunk_type=true、return_txt=false。上传/查询/连接超时独立可配置，HTTPX 流式 multipart；网络阶段超时不是服务端任务期限。普通模式 query 与 two-stage form 自动区分，普通任务 HTTP 500 的 FAILURE/REVOKED 作为终态处理。详见 [统一批量说明](docs/batch-processing.md)。
- 新批量输出 `results/<相对路径及扩展名>.json`，`.batch.json` 记录批次身份，`.tasks` 记录输入/请求/结果摘要和任务 ID；完成后仍校验输入，输入或请求改变、已记录文件被筛除时拒绝混用。结果缺失/损坏只重取原 ID；SUBMITTING 不重投；改变等待预算允许续跑，改变工作流或档位需新目录。旧脚本保留 pickle/旧记录合同，不与新脚本混用输出目录。
- 新 CLI `--resume-only` 只收取已有任务，禁止补交及失败重试，未提交文件计入 deferred；要求已有批次目录。计划停机先停止原客户端再按原命令加此选项收尾，恢复后去掉选项继续提交；它不取消服务端任务，也不绕过 SUBMITTING 核查或输入一致性检查。
- `MINERU_RUN_BATCH_PDFS=1` 启用 `tests/test_batch_input_pdfs.py`，使用 input/p2 和九页论文整本验证三模式、续跑；常规边界由 `test_batch_parse.py` 覆盖。本客户端更新不改变千页长任务服务端验收边界。

- `MINERU_RUN_API_PDFS=1` 启用 `test_live_api_pdfs.py`，真实访问部署 API、普通 Celery、two-stage 和基于 p2 的 Office 转换。它不会替换为路由替身；维护窗口执行并保存 task_id，HTTP 查询超时不重投。

## 本机状态

2026-09-18：API 7770，三卡 Docker project `mineru-vlm-parallel`、端口 30000、GPU 0/1/2、每卡显存比例 0.15，由 PM2 `mineru-vlm-docker-parallel` 管理。API 和四类 two-stage worker 已切换地址并在线；parse 为三个独立 solo/1，其余各一个，共六个 worker。缺省档位为 advanced；ONNX 16/1、VLM 并发 8、窗口 64。standard/advanced 同步请求及真实 Celery 任务已验收。旧 31000 单卡容器已移除，缓存卷保留；旧 MinerU 本机 vLLM 启动项已移除，另一仓库的 embedding 服务独立运行。PM2 stop 已验证容器正常退出，并完成重新启动验收及 pm2 save。驱动升级重启后的 UVM 映射缺失已修复，新增 `/ready`；同步隔离渲染池收尾与批量脚本滚动/续跑已优化，见[第二轮记录](docs/mineru_4_upgrade_usage.md#队列与单文件优化2026-09-18第二轮)；并发与线程对照见[优化记录](docs/mineru_4_upgrade_usage.md#重启修复与并发优化2026-09-18)。回滚与验收日志见[部署记录](docs/mineru_4_upgrade_usage.md#本机部署与回滚记录2026-09-17)。


2026-09-18 第二轮：三个 parse worker（solo/1，prefetch=1）、ONNX 16/1、VLM 并发 8 和 64 页窗口保持；修复同步隔离任务渲染池退出等待。批量脚本采用 6 个在途任务并保存可续跑日志。图片请求继续关闭 thinking，本机和公开 Qwen3.5 模板采样 0.7/0.8/20/1.5；同步窗口仍 3、two-stage vision threads/32。API 和六个 two-stage worker 已重载，九页论文与 6 张图的真实任务通过，PM2 已保存。性能证据和限制见[第二轮记录](docs/mineru_4_upgrade_usage.md#队列与单文件优化2026-09-18第二轮)及[图片描述优化](docs/mineru_4_upgrade_usage.md#图片描述优化)。

2026-09-18 文档入口：两份完整指南位于 `docs/ai-integration.md`、`docs/performance-tuning.md`，均提交 Git；只有前者经 `/guides/ai-integration.md` 与 `/llms.txt` 提供给调用者。API 已重载、鉴权与调优文档不暴露已验收，177 项常规测试通过。没有新增公网域名或 MCP 服务。


2026-09-18 Python 3.13 / 4.0.2：应用已切换 Python 3.13.15、MinerU 4.0.2、DocVortex 0.4.12；基础 mineru/ONNX，无应用 Torch/vLLM。Docker 镜像也升级 MinerU 4.0.2，保留 vLLM 0.21.0 + Torch 2.11.0/CUDA 13。API 4 worker，每 scheduler 池派发 3，共享解析容量 3；普通 worker 新增上线，只消费 urgent/normal，避免抢 two-stage merge 的 default。API、七个 Celery worker 与三卡模型均在线并已 pm2 save。198 项常规测试通过；20 项真实模型与 5 项当时的 HTTP/Celery/Office/存储验收通过（存储功能现已移除）。重建容器暴露的 Docker Snap CDI 过期 EGL 挂载已在备份后最小修复，三卡 CUDA 实测通过，未重启共享 Docker，其他 PM2 进程 PID 未变化。详细证据、限制与回滚见部署记录；临时/历史日志归入 output。

- MinIO 功能已按用户要求移除：不注册存储路由，不接受存储任务参数，不返回 minio_assets，应用依赖不含 minio；不要恢复旧接口、上传 helper 或客户端存储选项。结果由调用方通过 HTTP 获取并自行保存，批量 CLI 默认本地 JSON。现存共享存储服务/数据不属于本项目清理范围；历史验收仅作记录。`test_removed_storage_contract.py` 检查公开 schema 和响应不含旧合同。
