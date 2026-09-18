# TianGong AI Unstructure Serve 代理说明

仓库为 `tiangong-ai/unstructure-serve`。当前运行基线是 MinerU 4.0.2 + CPU ONNX 小模型 + Docker vLLM，应用依赖由 `uv.lock` 固定，部署使用 Python 3.13.15。API 与 worker 仍在应用环境运行，`.venv` 使用基础 mineru + CPU ONNX，不安装 Torch/vLLM；四档不依赖 all/full extra。验证范围与命令见 [验证指南](docs/validation.md)。

## 文档与修改约定

- **每次修改代码、配置或说明，都同步更新本文件涉及的规则或入口。**
- 当前文档入口：[README](README.md)、[部署与恢复](docs/mineru_4_upgrade_usage.md)、[AI 接入](docs/ai-integration.md)、[批量客户端](docs/batch-processing.md)、[普通任务](docs/mineru_with_images_task_usage.md)、[two-stage](docs/two_stage_task_usage.md)。
- 开发维护资料：[依赖](docs/dependencies.md)、[架构](docs/architecture.md)、[调优](docs/performance-tuning.md)、[验证](docs/validation.md)。均纳入 Git，但只有 AI 接入指南通过指定文档路由提供；不能挂载整个 docs 或在 llms.txt 中链接开发资料。
- 文档与示例仅使用相对文件路径；shell 命令以仓库根目录为工作目录。HTTP 路由和服务 URL 不是文件路径，保留实际写法。共享目录在部署时解析并保持所有进程一致，不在说明中写机器目录。
- 已完成的升级评估、过期依赖清单、逐轮操作流水账由 Git 历史追溯，不追加到当前指南。保留有明确条件、复现方法和边界的测量依据；不要把测试数量或私有机器状态重复写入每份说明。
- `.env`、`.secrets/`、输入文件、模型、结果、日志和回滚环境保持私有。公共配置骨架为 `deploy/secrets.example.toml`；不要在 PM2 模板写凭证或实际视觉服务地址。
- README 保持功能和入口简明；部署细节集中到部署说明，队列/字段细节集中到对应任务文档。例子中的默认值必须区分代码缺省、模板值和本机覆盖。
- README 顶部提供以当前工作区为定位方式、可复制给 AI 的初始化启动、故障恢复、安全停止指令，覆盖 model/app/ordinary 三个部署组及依赖检查。停止须先停提交来源并等已提交任务及阶段队列收敛；共享 Redis/独立图片模型先识别归属，不随本项目停机。CLI 中断不取消服务端任务，恢复保留原记录；不以 /health 或 /ready 单项成功代替完整验收。

- 根目录文档只保留 README.md 与 AGENTS.md；专题说明位于 docs，PM2 模板位于 deploy/pm2，Compose/Docker 位于 deploy/mineru-vllm。统一运维入口为 deploy/manage.sh，start 跳过已 online/launching 的进程，更新配置用 restart；PM2 cjs 根据配置文件位置解析项目目录；直接调用 JSON 模板必须在仓库根目录。Gunicorn 参数集中 deploy/gunicorn.conf.py，缺省 4 worker、5000+0..500 请求回收，PM2 退出窗口 1900 秒。

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
| `src/services/job_store.py` | 持久任务身份、原始输入、阶段锁、原子结果和过期墓碑 |
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
- `parse_doc()` 使用无状态 `mineru.parser.parse`，默认 PDF `page_range=all`。零基 start/end 转成一基范围，保留源页号；返回 `(content_list, artifact_dir, None)`。先保存 MiddleJson/图片，再渲染 V1 并补齐旧字段；图片引用缺失必须失败。默认通过公开 ParseResult 导出，不保存原始 model_output.json；仅 dump_debug_intermediate=True 导出该诊断，不能用丢弃 writer 输出的方式绕过前置深拷贝/序列化成本。
- `img_caption/img_footnote`、chart/code/index/page_footnote 映射和 bbox 单位需保持下游兼容；同时写旧命名 `_content_list.json` 供诊断。异常继续冒泡，不返回空值伪装成功。
- Office 主结果始终先经 LibreOffice 转 PDF，再使用所选 tier；每次转换使用独立 profile 并在超时后收尾。
- 仅同步 `/mineru_with_images` 的 `.docx + return_txt=true` 使用额外原生 DOCX flash 分支生成 txt，result/页码仍来自 PDF。该分支图片按原文顺序插入严格可见内容 OCR，不附加 caption/footnote 或根据上下文推断实体。普通任务和 two-stage 不启用该分支。
- `chunk_type=true` 保留 title/header/footer 和原阅读顺序，视觉增强的图片块标 image，忽略 page_number 块；普通正文/表格可能没有 type。txt 按同序拼接，标题段后两个换行，普通段后一个换行。普通响应省略 null 字段、任务失败查询返回 500；two-stage 保留 null、任务失败查询返回 200 状态体，调用方仍须检查 state。
- checkbox 修正在已有 checkbox 符号时触发 `pdftotext -bbox`，按源页和选项匹配；缺工具、超时或抽取失败则跳过。补回缺失首符号仅允许同页唯一完整选项组、至少两个其他符号仍在、首标签至少四字；歧义、短标签、额外文字或跨页不得补。环境开关与超时见部署配置。

## 视觉与资产

- 默认视觉 provider 为 vLLM；OpenAI/Gemini 实现仍可显式配置。未知 provider/model 在同步图片接口及普通图片任务中宽松接收，由服务兜底；two-stage 则在路由层校验枚举并可返回 422。
- vLLM 必须有 `VLLM_BASE_URL(S)` 才可用，API key 可选。此地址是独立图片描述模型，与 `MINERU_MODEL_VLM_SERVER_URL` 不同。
- OpenAI/vLLM 复用客户端池；多个视觉 endpoint 会顺序尝试。不要把视觉故障切换能力误写成 MinerU 解析端点的能力；MinerU 多 URL 池只有进程内轮换；三卡部署的单 URL 由容器内 vLLM 做请求负载均衡。
- 视觉请求默认 `enable_thinking=false`，采样参数由 `VLLM_VISION_*` 覆盖。同步图片采用单线程池滚动补位，由 `VISION_BATCH_SIZE` 控制每请求在途上限（代码/模板 3），不是所有 API 进程共享限额，也不控制 Celery vision threads/32；上下文在请求前固定，不将生成描述回灌为后续上下文。视觉异常使请求/任务失败，不使用 base_text 降级。OpenAI-compatible 空响应或非 stop 结束必须失败，不能接受被截断内容。Qwen3.5 部署采样模板为 temperature/top_p/top_k/presence_penalty=0.2/0.8/20/0，通用代码默认仍为 1/1/40/2。
- 默认 OpenAI-compatible 提示词放在 system，文档上下文作为 user 数据；自定义 prompt 保持优先。图表只提取印出的值，不根据柱高/坐标估算；流程图保留中间步骤。增强时以独立视觉结果替换 SDK 生成的图示正文，仍保留印刷标题/脚注；纯解析和被筛除图片保持 SDK 内容。
- vLLM 视觉客户端默认单次读写阶段超时 180 秒、SDK 重试 0 次，分别由 VLLM_VISION_TIMEOUT_SECONDS/MAX_RETRIES 控制，故障继续尝试下一端点；不是整份任务的墙钟截止时间。
- 默认图片提示词保留图中数字、单位、标签和关系，合并同类数据，避免重复 caption、无关引言和推断数值；不压缩图片或按字数硬截断。清理仅处理开头完整 thinking 段和确定的中英文套话，保留正文及不确定性。原生 DOCX 严格 OCR 不启用新增套话清理，避免误删原图文字。自定义 prompt 继续优先。
- two-stage 保留相对面积、分辨率、体积、长宽比和每页数量筛选。所有图片增强入口仅在单文档内复用相同图片字节及相同语义上下文的请求，重复位置仍逐一回填。默认 key 只忽略生成的页位置标记，自定义 prompt/严格 OCR 保留位置；不同标题/上下文不得合并。缺资产或缺视觉结果必须失败；合并保持原位；清理视觉输出中的 Page/ChunkType 标记和固定说明前缀。

- 六个解析上传入口统一通过 `src/utils/upload_io.py` 在线程池内按 1 MiB 分块持久化；Office 和 broker 提交不直接阻塞事件循环。同步解析用 shield/wrap_future 等待，HTTP 超时后源文件延迟到实际任务结束再清理。Pydantic 响应直接序列化 JSON，保留 null/pretty 合同。

## 队列与进程

- 持久任务存储通过 `MINERU_JOB_STORE_DIR` 指定，缺省仓库 `output/jobs`；所有 API/worker 必须共享同一可靠本地文件系统。任务身份绑定原始文件摘要、文件名、模式及参数；同幂等键不能替换内容。原子文件提交结果，元数据落盘失败不能抹掉已提交结果；清理须取得独占生命周期锁并保留过期墓碑，不能删除仍在执行的任务或锁文件。该存储不是跨主机分布式协调。

- 普通 app `src.services.celery_app` 消费 `queue_urgent,queue_normal`；普通 worker 不消费 two-stage 的解析/视觉/default merge 队列，防止不同 Celery app 抢到未注册任务。
- two-stage 部署显式配置 normal 队列 `queue_parse_gpu/queue_vision/queue_dispatch/default`；四类 urgent 为 `queue_parse_urgent/queue_vision_urgent/queue_dispatch_urgent/queue_merge_urgent`。API 与每个 worker 必须配置一致，不能只改 worker 的 `-Q`。
- 未配置时代码的 parse 回退到普通队列，dispatch/merge 回退到 default；`.env.example` 显式列出与 PM2 匹配的队列，详细规则见 two-stage 文档。
- 两个 Celery app 共用 `celery_runtime.py`：Redis 的 broker/backend/app visibility_timeout 一致读取 `CELERY_VISIBILITY_TIMEOUT`（代码 3600 秒、模板 21600 秒），`CELERY_RESULT_EXPIRES` 同时控制两类结果（代码/模板 86400 秒，TOML/环境可覆盖）。普通 worker 同样使用 priority 队列顺序。所有共享 broker 的 worker 必须一同配置；延长确认期限会延迟崩溃后重投，并不提供幂等或断点恢复。
- Redis 优先级消费按 `-Q` 的 urgent→normal 顺序。dispatch 使用 `self.replace` 启动 chord；不要在 Celery task 内阻塞调用 `result.get()`。四个阶段都要有消费者和可用的 result backend。
- API 与 worker 共享 broker/backend/任务目录；跨容器时目录绝对路径一致。`PENDING` 也可能是未知或过期 ID，`queue_status` ready/unacked 不等于最终结果。
- scheduler 每个历史 GPU_IDS 池缺省有 3 个派发进程（MINERU_SCHEDULER_WORKERS），避免 HTTP 连接亲和造成单池串行；实际解析总数仍由共享槽位限制。scheduler 在独立子进程中解析，Linux 使用 parent-death signal 和任务进程组；仅在 hard timeout、父进程退出或结果返回后清理该任务组。不能按名称/运行时长全局误杀解析进程。
- 隔离任务在成功/失败后显式关闭本任务已加载的 DocVortex 渲染池，避免 Python 等待嵌套进程导致固定退出延迟；保留原 hard timeout 与进程组收尾作为兜底，不通过缩短等待或提前返回来跳过清理。
- `run_isolated_call` 使用 Pipe 接收线程配合子进程存活检查，子进程无结果退出时报告退出码，不等到整段 hard timeout；超时包含执行及结果传输，进程组清理可额外消耗退出窗口。大结果传输也必须持续监督，不退回先 join 再读取的死锁模式。
- 普通模板 threads/16，可用 solo/1 保守运行；two-stage parse 为三个独立 solo/1 worker（名称 parse、parse-2、parse-3），均消费 urgent/normal，prefetch=1；其余线程池。每个解析 worker 的 VLM 并发为 8，PM2 停止窗口 1900 秒；并非全局并发上限。避免 daemonic prefork；保持临时文件 finally 清理和正常 shutdown 等待。
- API 和 parse PM2 模板均显式设置 processing window=64，减少长文档渲染内存；这是内部窗口大小，保留整本解析和跨页后处理，不是页数上限。不要为了多卡吞吐先把 PDF 拆成独立单页任务。
- CPU ONNX 模板每个模型会话的 intra/inter 线程数为 16/1，防止高核数机器上自动线程池过度竞争；这是本机混合 PDF 测量后的配置，不是整个进程的线程上限。VLM 并发保持 8；4 线程及 VLM 16 均有对照证据，不凭单个短文档结果扩大并发。

- `parse_capacity.py` 通过 Linux flock 在同一主机的 API 子进程/普通任务/two-stage 间共享解析槽，缺省 `MINERU_PARSE_SLOTS=3`、等待上限 1800 秒；目录由 `MINERU_PARSE_SLOT_DIR` 指定，缺省系统临时目录下 `tiangong_mineru_parse_slots`。所有参与进程必须使用相同目录/槽数；不是跨主机分布式锁。不删除正在使用的锁文件；fork 的渲染子进程关闭继承租约，进程退出自动释放。scheduler hard timeout 包括等待槽位时间。

## 配置与运维

- 进程环境优先于 `.env`，再回退到 `.secrets/secrets.toml`。PM2 `env` 属于进程环境，不会被 `load_dotenv()` 覆盖；Python 加载 `.env` 不会替调用方 shell 导出变量。
- 配置模块仍要求 TOML 的 FASTAPI/OPENAI/GOOGLE/VLLM 段存在；复制 `deploy/secrets.example.toml` 初始化。公开模板不得包含实际凭证；部分字段空串会回退到 TOML，不代表清除原配置。
- 三卡部署入口为 `deploy/pm2/ecosystem.vllm.parallele.config.json` → `deploy/mineru-vllm/serve.sh parallel`，合并基础 Compose 与 `deploy/mineru-vllm/compose.mineru.parallel.yaml`。单容器绑定 GPU 0/1/2，vLLM DP=3、TP=1，通过单地址内部负载均衡；不设置 external/hybrid LB。PM2 前台托管 Compose，停止超时 70 秒覆盖容器 60 秒退出窗口。单卡基础 Compose 与其他独立容器模板是可选拓扑，不同时管理同一 project。应用 `GPU_IDS` 不控制 Docker GPU。复用已有缓存卷时通过 `MINERU_DOCKER_*_VOLUME` 指定并启用 `MINERU_DOCKER_VOLUMES_EXTERNAL=true`，不删除原卷。
- Docker 基线为 vLLM 0.21.0 配套 Torch/CUDA，模型上下文 8192；应用使用独立 `uv.lock`。升级镜像时重新检查依赖与 PDF，不在应用中补装 vLLM。
- Docker Snap 的开机 CDI 扫描可能早于 UVM 设备创建；基础 Compose 显式映射 `nvidia-uvm` 和 `nvidia-uvm-tools` 设备节点，启动器最多等待约 120 秒。`nvidia-smi` 正常不代表 CUDA 可用，应验证容器内实际张量计算；不要为修复本服务重启共享 Docker 或卸载 GPU 驱动。
- PM2 API 为 `unstructured-gunicorn`，Gunicorn timeout/graceful-timeout 1900 秒。科研入口另有自己的 HTTP 等待窗口；具体超时以模板/运行环境为准。
- 远程 AI 使用实际部署 `/openapi.json` 与 `/guides/ai-integration.md`，`/llms.txt` 仅为文档索引，不是 MCP。两个文档路由继承业务鉴权；FastAPI 自动 `/openapi.json`、`/docs`、`/redoc` 不自动继承业务 Depends 保护，需私有时由网关额外限制。索引链接保留 root_path 前缀，不链接调优文档。
- `/health` 仅检查 API 存活；`/ready` 并行检查 MinerU VLM 端点的 `/health`，不可用返回 503，不检查 Redis/独立视觉模型，也不执行实际推理。通过 `/two_stage/queue_status` 及 Celery inspect 检查队列，按 task_id 查询任务状态，启用鉴权时带 Bearer。日志默认 INFO，httpx/httpcore 降到 WARNING，视觉提示词仅在显式 VISION_LOG_PROMPTS=true 时写 DEBUG，默认不输出上下文；Loguru 日志使用 `{}` 占位符，vLLM 重试日志记录尝试序号和异常类型，不附带上游响应正文；不输出密钥或完整 PM2 环境。
- 维护先定位当前服务树和 active/reserved 任务，按具体任务清理。不要在日常说明中使用全局 PM2 删除、Redis flushdb 或无差别清空共享任务目录。

- 统一批量 CLI 将进度日志写入 stderr、汇总 JSON 写入 stdout；需要文件日志时由调用方重定向。只有兼容 two-stage 脚本默认写入 output/logs，接受 TWO_STAGE_LOG_FILE。不要将兼容脚本的日志位置、6 个在途或 800 秒等待写成统一客户端默认值。

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
- `src/scripts/benchmark_vision.py` 对私有图片/上下文/正则检查清单进行真实多端点重复测量，保存图像摘要、原始响应、质量检查和 token/耗时；正则通过不等于完整语义正确，仍需人工对图核验。
- `src/scripts/build_pdf_case.py` 构造私有扩页 PDF，保存逐页来源与摘要，禁止覆盖；合成重复页与原生长文档分别记录，不把缓存命中收益外推到新内容。
- 真实模型回归：`MINERU_RUN_INPUT_PDFS=1 uv run --group dev pytest tests/test_mineru_input_pdfs.py -v`。按测试中的固定 PDF_NAMES 清单读取 input，新增文件不自动进入回归；p2 缺省及四档整本，论文和 fese 整本，其余抽样首页/第 11 页/末页。没有样本应明确失败，不用替身冒充实测。
- 视觉真实回归：`MINERU_RUN_VISION_PDFS=1 uv run --group dev pytest tests/test_vision_input_pdf.py -v` 从 input 论文第五页真实解析图像并请求已配置多模态模型，检查图中关键数值及单位；需同时具备 MinerU 与图片模型服务，不用替身。
- 三卡部署测试验证 Compose 的 GPU/DP 参数与 PM2 前台生命周期；`MINERU_RUN_DP_PDFS=1 uv run --group dev pytest tests/test_mineru_data_parallel.py -v` 使用 input 的 p2 和九页论文，并检查三个 engine 的成功推理计数均增加。验收须说明模型拓扑、样本范围和证据位置。
- `src/scripts/benchmark_mineru.py` 对真实 PDF 做已预热 SDK 进程压测，记录批量完成、单任务服务和排队耗时；不含 Celery/独立视觉阶段。输出目录必须新建，校验整本页号、图片及 p2 关键表格/checkbox；样本与结果保持私有。脚本退出前显式收尾各进程的 DocVortex 渲染池，避免嵌套 multiprocessing 等待退出。
- `src/scripts/two_stage_enqueue.py` 的生产调用须显式 `TWO_STAGE_BASE=http://127.0.0.1:7770`，脚本缺省仍是开发端口 8770，且不传 tier（使用 advanced）。优先级演示 `enqueue_input.py` 会重复提交；不要作为生产批处理入口。
- 400–1000 页批量必须引用 AI 指南第 5.3 节与调优指南第 12 节：整本单文件验收后从 1→2→3 个在途试起，统一 CLI 默认在途 2、等待 21600 秒，兼容脚本为 6/800 秒；两者均不作千页容量承诺。已完成合成 400/1000 页及原生 1016 页的整本 SDK 实测，另完成 400 页普通解析、90 页 two-stage 和 111 页普通图片任务的 HTTP/Celery 验收，包含客户端超时续查；不同业务样本仍需容量验收；two-stage 直接 parse_doc 不走 scheduler hard timeout，late ack 仍需核对实际 visibility timeout 及所有共享 broker 的消费者，不能以延长客户端等待宣称长任务已经可用。调优配置细节只保存在仓库指南，不通过文档服务提供。
- 兼容 two-stage 脚本采用滚动在途窗口（`TWO_STAGE_MAX_IN_FLIGHT`，默认 6），输出目录 `.tasks` 原子保存任务 ID/文件摘要/请求参数，重启续查已有任务。查询故障或本地等待超时不重投；只有服务端确认 FAILURE/REVOKED 才有界重试。提交响应丢失时保留 SUBMITTING 并明确停止，不能假定服务器未接收。单输出目录由文件锁限制一个 CLI 写入进程。脚本认证优先环境/.env，再回退本地 TOML 的 FASTAPI.BEARER_TOKEN，不记录令牌。
- 新批次优先 `uv run python -m src.scripts.batch_parse`，`--mode parse/images/two-stage` 覆盖全部三个异步 API；缺省 advanced、在途 2、等待 21600 秒、尝试 1 次、chunk_type=true、return_txt=false。上传/查询/连接超时独立可配置，HTTPX 流式 multipart；网络阶段超时不是服务端任务期限。普通模式 query 与 two-stage form 自动区分，普通任务 HTTP 500 的 FAILURE/REVOKED 作为终态处理。详见 [统一批量说明](docs/batch-processing.md)。
- 新 CLI 上传/查询线程上限分别为 `--upload-concurrency=2` / `--query-concurrency=4`，在途窗口包含上传中的文件；仅主线程写 journal。上传从经原摘要校验的匿名快照读取，故障收尾仍保存其他已发请求返回的 ID；identity version 1 保持，改变并发预算允许续跑。幂等键由批次、输入相对身份和尝试次数导出，不含凭证；兼容 requests 脚本保留串行网络行为。
- 新批量输出 `results/<相对路径及扩展名>.json`，`.batch.json` 记录批次身份，`.tasks` 记录输入/请求/结果摘要和任务 ID；完成后仍校验输入，输入或请求改变、已记录文件被筛除时拒绝混用。结果缺失/损坏只重取原 ID；SUBMITTING 不重投；改变等待预算允许续跑，改变工作流或档位需新目录。旧脚本保留 pickle/旧记录合同，不与新脚本混用输出目录。
- 新 CLI `--resume-only` 只收取已有任务，禁止补交及失败重试，未提交文件计入 deferred；要求已有批次目录。计划停机先停止原客户端再按原命令加此选项收尾，恢复后去掉选项继续提交；它不取消服务端任务，也不绕过 SUBMITTING 核查或输入一致性检查。
- `MINERU_RUN_BATCH_PDFS=1` 启用 `tests/test_batch_input_pdfs.py`，使用 input/p2 和九页论文整本验证三模式、续跑；常规边界由 `test_batch_parse.py` 覆盖。本客户端更新不改变千页长任务服务端验收边界。

- `MINERU_RUN_API_PDFS=1` 启用 `test_live_api_pdfs.py`，真实访问部署 API、普通 Celery、two-stage 和基于 p2 的 Office 转换。它不会替换为路由替身；维护窗口执行并保存 task_id，HTTP 查询超时不重投。

## 功能边界

业务结果通过 HTTP 返回，批量客户端保存本地 JSON；不提供远端对象存储接口或资产上传选项。对象存储合同移除的回归见 test_removed_api_contracts.py，不恢复已删除实现。不提供 GPU 调度状态 HTTP 接口；解析池的待处理计数仅用于内部任务分配。工作区中的共享服务、输入和结果不属于接口清理范围。
