# MinerU 4 部署与回归

当前发布基线为 MinerU 4.0.0：应用调用无状态 SDK `mineru.parser.parse`，小模型使用 CPU ONNX，VLM 通过 Docker 提供。应用 `.venv` 不安装 vLLM。质量档位和请求参数见 [README](README.md#解析接口)，升级前分析保存在[历史评估](docs/history/mineru_4_upgrade_evaluation.md)。

## 首次安装

所有命令在仓库根目录执行。系统需要 Python 3.12、uv、Docker Compose 2.24.4+、PM2，以及支持本机 GPU 的 NVIDIA 驱动和 Container Toolkit。Docker 需已注册 `nvidia` runtime；Compose 显式指定该 runtime，以兼容本机 CDI 模式。

```bash
sudo apt update
sudo apt install -y libmagic-dev poppler-utils libreoffice pandoc graphicsmagick
uv python install 3.12
uv sync --locked --group dev
mkdir -p .secrets
cp -n deploy/secrets.example.toml .secrets/secrets.toml
cp -n .env.example .env
```

编辑 `.env`，填写真实鉴权令牌、模型地址和运行参数。已有部署应合并新增字段，保留原凭证。配置模块仍会读取 `.secrets/secrets.toml` 中的必需段落，即使凭证全部来自环境变量，也需要该文件；模板只有空值。

模型首次准备：

```bash
MINERU_MODEL_SMALL_BACKEND=onnx uv run mineru-kit models download --tier basic --small-backend onnx --source modelscope
MINERU_MODEL_SMALL_BACKEND=onnx uv run mineru-kit models verify --tier basic --small-backend onnx
pm2 start ecosystem.vllm.parallele.config.json
pm2 logs mineru-vlm-docker-parallel --lines 100
```

以上默认三卡部署要求 GPU 0、1、2 可用，单卡替代见下文。容器首次启动会下载 VLM 权重并编译，等待 healthy 后再接入请求。CPU 模型可通过 `MINERU_HOME` 指定持久目录；容器模型及下载缓存保存在命名卷中。

Celery 需要 Redis。已有实例直接复用；仅在未部署时创建：

```bash
docker run -d --name redis --restart unless-stopped -p 127.0.0.1:6379:6379 redis:8
```

## 配置规则

Python 配置优先级为 **进程环境 > `.env` > `.secrets/secrets.toml` 的回退值**。`load_dotenv()` 不覆盖已有环境；PM2 的 `env` 块因此优先于 `.env`。部分字符串字段的空值仍回退到 TOML，不能用空字符串假定已清除旧配置。修改配置后要重新加载对应 API/worker。Docker Compose 单独读取 `.env` 做变量替换，Python 加载 `.env` 不会把变量导出到调用它的 shell；curl 示例要求 shell 中已有 `FASTAPI_BEARER_TOKEN`。

| 配置 | 模板值 / 作用 |
| --- | --- |
| `MINERU_DEFAULT_TIER` | `advanced`，仅直接服务调用缺省时兜底；HTTP 不传 tier 固定 advanced |
| `MINERU_DEFAULT_METHOD` | `auto`，映射 SDK `ocr_mode`，可选 `auto/txt/ocr` |
| `MINERU_MODEL_SMALL_BACKEND` | `onnx`，CPU 小模型 |
| `MINERU_INTRA_OP_NUM_THREADS` / `MINERU_INTER_OP_NUM_THREADS` | 模板 `16` / `1`，每个 ONNX 模型会话的线程数；SDK 未配置时自动选择，API/parse PM2 显式覆盖 |
| `MINERU_MODEL_VLM_SERVER_URL` | `http://127.0.0.1:30000`，MinerU 模型推理地址 |
| `MINERU_MODEL_VLM_MODEL` | `mineru4`，容器公开的模型名 |
| `MINERU_MODEL_VLM_API_KEY` | 可选 Bearer 认证；本机容器默认不启用认证 |
| `MINERU_MODEL_VLM_HTTP_TIMEOUT` / `MINERU_MODEL_VLM_MAX_CONCURRENCY` | `600` 秒 / `8`，单次模型请求超时 / 每个解析进程的请求并发 |
| `MINERU_PROCESSING_WINDOW_SIZE` | SDK 缺省、`.env.example`、API/parse PM2 模板均为 `64` 页；是渲染/解析窗口，不限制整本页数 |
| `MINERU_DOCKER_GPU_ID` / `MINERU_DOCKER_PORT` / `MINERU_DOCKER_GPU_MEMORY` | GPU ID 仅单卡模板使用；三卡固定 0/1/2。三卡 PM2 env 覆盖端口/每卡显存比例为 `30000` / `0.15`，修改时同步应用 URL |
| `MINERU_DOCKER_MODEL_VOLUME` / `MINERU_DOCKER_CACHE_VOLUME` | 三卡模型/下载缓存卷名，默认 `mineru-vlm-models` / `mineru-vlm-cache`；可复用原部署缓存；已有卷设 `MINERU_DOCKER_VOLUMES_EXTERNAL=true`，避免归入新 project 生命周期 |
| `GPU_IDS` | 模板 `0`，现有应用调度器槽位；不决定 Docker 使用哪张卡 |
| `VISION_*` / `VLLM_BASE_URLS` | 独立图片描述服务，按部署填写，不能指向 MinerU 解析 V1 API |
| `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | API 与所有 worker 使用相同地址；模板为本机 Redis DB 0 |
| `CELERY_TASK_*_QUEUE` | 模板显式设置普通与 two-stage 队列；[队列映射](two_stage_task_usage.md#队列与配置)必须与 worker 的 `-Q` 一致 |
| `MINERU_TASK_STORAGE_DIR` | 默认系统临时目录下的 `tiangong_mineru_tasks`；跨容器时共享相同绝对路径 |

Office 转换模板超时为 600 秒。普通/图片解析 hard timeout 为 1800 秒；科研入口另有 110 秒 HTTP 等待窗口和 300 秒子进程 hard timeout。API 的 Gunicorn timeout/graceful-timeout 均为 1900 秒。具体模板见 `.env.example`、`ecosystem.config.json`，不要把这些数值理解为所有任务统一的超时。

旧 backend 仅用于直接调用和在途 payload 兼容：`pipeline`→`basic`、`hybrid-*`→`standard`、`vlm-*`→`advanced`；对应大模型推理仍使用 Docker。新请求只使用 `tier`。旧 `MINERU_DEFAULT_LANG`、hybrid batch/force-pipeline 参数不再传给上游。

## 启动与维护

先确认共享配置、Redis 和模型服务就绪，再启动所需任务系统：

```bash
pm2 start ecosystem.config.json
pm2 start ecosystem.two_stage.celery.json
pm2 save
```

| 文件 | 用途 |
| --- | --- |
| `ecosystem.vllm.parallele.config.json` | 三卡模型入口，PM2 进程 `mineru-vlm-docker-parallel`，单容器 DP=3 / TP=1 |
| `deploy/mineru-vllm/serve.sh` | 前台执行 Compose，`parallel` 合并两份 YAML，project 固定为 `mineru-vlm-parallel` |
| `compose.mineru.yaml` / `compose.mineru.parallel.yaml` | 单卡基础 / 三卡覆盖；三卡命令必须带两份文件 |
| `ecosystem.config.json` | API `unstructured-gunicorn`，端口 7770 |
| `ecosystem.two_stage.celery.json` | 当前部署使用的六个 worker：parse 三个，vision/dispatch/merge 各一个 |
| `ecosystem.celery.json` | 普通任务 worker，调用两个普通 `/task` 接口时另行启动 |
| `ecosystem.two_stage.flower.json` / `ecosystem.celery.flower.json` | 对应 Celery app 的可选监控；都默认 5555，同时使用时须更改端口 |
| `ecosystem.vllm.config.json` / `ecosystem.vllm.quatro.json` | 可选单卡 / 四个独立端点模板，不属于三卡内部 DP 部署 |
| `ecosystem.quatro.json` | 多 API 实例示例，不等于自动扩容 Docker 推理服务 |

API 使用 `uvicorn_worker.UvicornWorker`。普通 worker 模板为 threads/16，保守手动启动可用 solo/1；two-stage parse 为三个独立 solo/1，其余为线程池，详情见各自任务说明。GPU 调度器会再创建子进程，不使用 Celery prefork 执行这类解析。

以下为常用检查，启用鉴权时传入实际令牌：

```bash
pm2 status
docker compose -p mineru-vlm-parallel -f compose.mineru.yaml -f compose.mineru.parallel.yaml ps
curl --fail http://127.0.0.1:30000/health
curl --fail -H "Authorization: Bearer $FASTAPI_BEARER_TOKEN" http://127.0.0.1:7770/ready
curl --fail -H "Authorization: Bearer $FASTAPI_BEARER_TOKEN" http://127.0.0.1:7770/gpu/status
uv run celery -A src.services.two_stage_pipeline inspect active_queues --timeout=5
pm2 logs unstructured-gunicorn --lines 100
```

`/health` 仅返回 API 存活状态；`/ready` 并行探测全部配置的 MinerU VLM `/health`，不可用时返回 503，单端点超时 3 秒。它沿用解析端点及认证配置，不探测 Redis、MinIO 或独立图片描述服务，也不能代替真实 PDF 回归。

三卡服务和应用 URL 均使用 `30000`；启动脚本的 project 参数优先于 `.env` 中旧的 `COMPOSE_PROJECT_NAME`。更新服务时先确认队列及 active/reserved 任务，等待当前解析收敛，再对指定进程执行 `pm2 reload ecosystem.config.json --update-env` 或重启对应 worker。改动 `.env` 时检查 PM2 `env` 是否覆盖同名字段。

模型维护命令：

```bash
pm2 logs mineru-vlm-docker-parallel --lines 100
pm2 restart ecosystem.vllm.parallele.config.json --update-env
pm2 stop mineru-vlm-docker-parallel
# 需要恢复时执行，再保存 PM2 状态
pm2 start ecosystem.vllm.parallele.config.json
pm2 save
```

模型重启或停止前先排空解析任务。PM2 管理前台 Compose，stop/restart 会传递至容器；容器退出窗口 60 秒，PM2 强制退出窗口 70 秒。PM2 的 online 只代表启动命令存活，须另查容器 healthy 或模型 `/health`。API 停机用 `pm2 stop ecosystem.config.json`，worker 用对应模板。运行清理应定位本项目具体任务或工作目录；共享 Redis、PM2 和 GPU 上还有其他服务，不提供全局清空或按端口强杀作为日常维护步骤。结果过期不等于任务目录可以无条件删除。

## Docker 与多卡

镜像基于 `vllm/vllm-openai:v0.21.0`，安装 MinerU 4.0.0，保留基础镜像配套的 Torch 2.11.0/CUDA 13。Dockerfile 补齐 Pycairo/Cairo 依赖并执行 `pip check`。FastAPI/Starlette/instrumentator 组合已验证；应用 OpenAI SDK 固定在 MinerU `<3` 约束内的 2.54.0。应用升级依据 `uv.lock`，镜像升级需重新验证其完整依赖组合。

模型为 `MinerU2.5-Pro-2605-1.2B`，容器公开名 `mineru4`，最大上下文长度按模型配置设置为 8192。端口默认仅绑定宿主 `127.0.0.1`；跨机器部署需另行配置可达地址和认证。

当前三卡方案恢复旧部署的 **内部数据并行**：GPU 0/1/2 各一份完整模型，`--data-parallel-size 3 --tensor-parallel-size 1`，单个 API 入口由 vLLM 根据副本队列分配推理请求。它不是把同一模型切成三片，也无需配置三个应用 URL。详见 [vLLM 0.21 内部负载均衡](https://docs.vllm.ai/en/v0.21.0/serving/data_parallel_deployment/#internal-load-balancing)。

```bash
pm2 start ecosystem.vllm.parallele.config.json
# 排查时查看最终三卡配置（不要只使用基础 YAML）
docker compose -p mineru-vlm-parallel -f compose.mineru.yaml -f compose.mineru.parallel.yaml config
curl --fail http://127.0.0.1:30000/metrics
```

`/metrics` 中的 `vllm:request_success_total` 应有 `engine="0"`、`"1"`、`"2"`。用 PDF 发起推理后检查三者增量，不能仅凭 nvidia-smi 有显存占用判断负载均衡生效。每卡显存比例是各自总显存的比例，部署前检查其他模型占用；应用的 `GPU_IDS=0` 是解析调度槽位，不限制容器只用 GPU 0。小文档或 flash/basic 请求未必产生足够 VLM 请求，三卡不保证每份文档平均分配或吞吐正好三倍。

主机升级驱动/重启后，如果 `nvidia-smi` 正常但 CUDA 报错，检查容器是否有 `/dev/nvidia-uvm` 和 `/dev/nvidia-uvm-tools`。Docker Snap 的启动期 CDI 扫描可能早于这些节点生成。基础 Compose 显式映射两设备，PM2 启动脚本最多等待约 120 秒；缺失时明确失败，不从应用脚本加载内核模块或重启共享 Docker。确认主机设备存在后，仅重启本项目模型进程。实际 CUDA 验证可用：

```bash
docker exec mineru-vlm-parallel-mineru-vlm-1 python -c 'import torch; assert torch.cuda.device_count() == 3; print([torch.ones(1, device=f"cuda:{i}").item() for i in range(3)])'
```

只有一张卡时，选择单卡模板并将应用 URL 与其端口保持一致，不要同时启动三卡模板：

```bash
pm2 start ecosystem.vllm.config.json
```

其他拓扑可使用每卡独立 Compose project 和不同端口。应用的 `MINERU_VLLM_SERVER_URLS` 列表仅进程内轮换（单 URL 优先），没有跨任务负载均衡、熔断或故障重试保证；这与当前 vLLM 内部 DP 不同。多节点容错、吞吐和队列调优见[后续工作](multi_gpu_vllm_scaling_todolist.md)。

## 验证与回归

```bash
uv sync --locked --group dev --check
uv run --group dev black .
uv run --group dev ruff check src
uv run --group dev pytest
```

`tests/test_mineru_input_pdfs.py` 直接读取 `input`，也可用 `MINERU_TEST_INPUT_DIR` 替换。模型测试默认跳过；显式启用时缺文件会失败：

```bash
mkdir -p output/mineru4_tdd
MINERU_RUN_INPUT_PDFS=1 uv run --group dev pytest tests/test_mineru_input_pdfs.py -v \
  --junitxml=output/mineru4_tdd/input-results.xml
```

三卡真实模型回归使用 input 中的 p2 和九页论文，验证完整页号、图片资产、checkbox 和三个推理副本的计数增量：

```bash
MINERU_RUN_DP_PDFS=1 MINERU_TEST_VLM_URL=http://127.0.0.1:30000 \
  uv run --group dev pytest tests/test_mineru_data_parallel.py -v \
  --basetemp=output/mineru4_tdd/three-gpu/pdf-check
```

`--basetemp` 会清理指定目录，复测时换新目录保留原证据。

2026-09-17 升级验收记录：

- 常规测试 138 项通过（新增三卡配置与 PM2 启动回归），覆盖六入口 tier 默认/枚举/透传、SDK 资产与字段、MinIO、Office、视觉失败传播及调度生命周期。
- 升级阶段 11 份 PDF 的 15 项实测通过：29 个抽样页，加 p2/九页论文/46 页 fese 整本，共覆盖 79 个不同源页面；不代表 11 份长文档全部整本验收。
- tier 补充阶段 p2 四档均通过，形成 17 项可选模型测试；新增三卡测试与 p2 缺省档位回归后共 19 项。basic 丢失的首个 checkbox 在同页唯一完整选项组匹配时回填，并有歧义、跨页、短标签等拒绝条件测试。
- 三卡实测：input 的两页 p2 与九页论文以 advanced 整本通过，engine 0/1/2 成功推理增量分别为 5/6/7；完整页号、图片文件和 p2 checkbox 均通过断言。这是负载分配验收，尚未做同负载吞吐基准。
- 在线六入口的 schema/非法值、同步四档、真实 two-stage basic、MinIO PDF/JSON/JPEG/meta、Office→PDF、原生 DOCX 顺序通过验证；论文的一张图片完成真实视觉请求，未逐图人工评估全部视觉输出。

证据仅保存在忽略的 `output/mineru4_tdd/`：`input-results-all.xml`、`tier-p2-results.xml`、`tier-pytest.log`、`tier-api-smoke.log`、`main-deployment-p2.json`。三卡证据位于 `three-gpu/`，包括 `red.log`、`pdf-green.xml`、`pdf-green/test_input_pdfs_reach_all_thre0/replica-requests.json` 及 PM2 生命周期日志。原始文档及解析资产不纳入 Git。

## 本机部署与回滚记录（2026-09-17）

升级已合并到 `main`（`e2de2fe`），运行目录为 `/home/david/projects/TianGong-AI-Unstructure-Serve`。API 7770；当前模型由 PM2 `mineru-vlm-docker-parallel` 管理，Compose project `mineru-vlm-parallel`，端口 30000、GPU 0/1/2、DP=3 / TP=1、每卡显存比例 0.15。CPU 模型在 `/home/david/.local/share/tiangong-mineru4`；外部卷 `mineru4-upgrade_mineru-models` 和 `mineru4-upgrade_mineru-download-cache` 继续复用。

API 与四类 two-stage worker 已切换新地址并重启，缺省档位现为 advanced。standard/advanced 同步解析和真实 two-stage Celery p2 任务已验收。PM2 stop 实测使容器正常退出（exit 0），重新启动后健康检查和线上请求通过，已执行 `pm2 save`。原 31000 单卡容器已停止并移除，模型缓存保留；旧 MinerU 本机 vLLM 启动项已移除。另一仓库的 `vllm-qwen3-embedding-8b` 保持原进程运行。

兼容层仍返回 `(content_list, artifact_dir, None)`，PDF 默认整本，保留源页号和可访问图片；业务 MinIO `parsed.json` 使用服务响应结构，不用原生 MiddleJson 替换。Office 主 JSON/MinIO 资产来自 PDF，原生 DOCX 仅用于同步 TXT 增强。

旧源码、配置、PM2 快照和旧 `.venv` 保存在 `output/mineru4_tdd/rollback-20260917/`。回滚需排空或隔离在途任务，并同步恢复代码、应用环境及模型服务地址；旧虚拟环境须放回原 `.venv` 路径，避免绝对 shebang 失效。保留已完成资产与模型缓存。

三卡修复前的私有 `.env`、PM2 快照和本次验证日志保存在 `output/mineru4_tdd/three-gpu/`。仅回退三卡部署时可继续使用当前 MinerU 4 应用与单卡 Docker 模板，同步更改模型 URL；不需要恢复 3.x 虚拟环境。

缺省档位调整为 advanced：六入口的 OpenAPI 与任务参数、服务无配置兜底、`.env.example` 和本机 `.env` 同步更新；138 项常规测试、p2 缺省档位真实 SDK 回归，以及重载后的六入口 schema / 同步 / Celery 默认解析均通过；回归证据位于 `output/mineru4_tdd/default-advanced/`。显式指定的其他档位及旧 backend 映射继续保留。


## 重启修复与并发优化（2026-09-18）

驱动升级至 615.71.09 后，主机 GPU 正常，但 Docker Snap 在启动时生成 CDI 清单早于 UVM 设备创建，导致容器缺少 UVM、PyTorch 初始化 CUDA 失败。已在 Compose 显式映射两个 UVM 设备，并在 PM2 启动脚本等待设备就绪。使用新建容器在三卡上执行实际 CUDA 张量计算通过，模型服务恢复；本轮没有再次重启整台主机，也没有重启共享 Docker 或修改独立 embedding 服务。

新增 `/ready`，避免 API 进程仍存活时把模型故障当作解析服务就绪。部署模板使用三个独立 parse worker，每个 solo/1、VLM 并发 8，ONNX intra/inter 线程为 16/1；API 使用相同 ONNX 预算，SDK 窗口由 API 原模板 512 统一为 64。每份文档仍整本解析及后处理，不拆成独立单页任务；三个 worker 共同使用一个 VLM 地址，由后端分配推理请求。

以下是同一三卡后端上的配置对照，**不是单卡与三卡对照**。SDK 测试每个进程先解析一次 p2，再计时；不包括进程启动、预热、Celery 和独立图片描述。共享主机上每组只跑一轮，结果不构成容量或 P99 保证。

| 30 份两页 p2，ONNX 自动线程 | 整批秒数 | 单份服务 P50 / P95（秒） |
| --- | ---: | ---: |
| 1 进程 × VLM 8 | 69.98 | 2.30 / 2.50 |
| 3 进程 × VLM 8 | 31.07 | 3.01 / 3.67 |
| 3 进程 × VLM 16 | 31.10 | 2.96 / 3.76 |
| 6 进程 × VLM 8 | 23.32 | 4.25 / 6.24 |

三个进程明显减少排队和批量完成时间；六个进程吞吐更高，但单份服务耗时更长。本机采用三个进程作为交互延迟与吞吐的折中，不宣称是所有负载的最优值。同步 API 仍有自己的调度器，以上设置不是 API 与 Celery 共享的全局限流器。

混合样本按 p2（2 页）、论文（9 页）、fese（46 页）顺序重复两次，共六份、114 页；固定三个进程 × VLM 8：

| ONNX intra / inter 线程 | 整批秒数 | 单任务服务 P95（秒） |
| --- | ---: | ---: |
| 自动 / 自动 | 101.70 | 56.27 |
| 4 / 1 | 113.17 | 62.79 |
| **16 / 1（采用）** | **73.53** | **42.96** |

另有单进程按相同顺序解析三份样本的 VLM 并发对照：8→16 时整批 82.05→69.66 秒，fese 49.70→39.13 秒，论文 30.02→28.28 秒。提高请求并发可能缩短长文档的模型阶段，但对短文档无明显收益；暂不同时扩大三进程的 VLM 并发。单页也只有在包含可并行区域时才可能受益，单次模型生成不会被 DP 自动分到三卡计算。

所有 SDK 批次均断言整本页号、非空结果、图片文件，以及 p2 表格和 checkbox。复杂报告不同运行的块数有变化，不能将这些检查解释为逐字/逐表质量完全相同；改变采样、精度或进一步提高并发前仍需内容级评估。跨页语义保持依赖整本后处理，不能靠把页面独立分发再简单拼接来替代。

复测示例（输出目录必须不存在，CPU 自动对照可显式把两个线程变量设为 `0`）：

```bash
MINERU_INTRA_OP_NUM_THREADS=16 MINERU_INTER_OP_NUM_THREADS=1 \
  uv run python -m src.scripts.benchmark_mineru \
  --workers 3 --concurrency 8 --jobs 30 \
  --output output/mineru4_tdd/benchmark-new-run
```

真实 HTTP → Celery → 结果查询对照：30 份 p2 均省略 tier（使用 advanced），开启 chunk_type/return_txt，每批先完成一次预热。原单 parse worker 完成整批 **70.87 秒**，新配置 **27.48 秒**（约 2.58 倍吞吐）；从提交到结果的 P95 为 **68.56→27.42 秒**。两批共 60 个任务全部成功，验证两页和关键内容；p2 无需独立图片描述，因此不代表视觉服务的吞吐。

最终常规测试 143 项通过、19 项可选集成默认跳过；本轮显式运行 p2 默认及四档、九页论文三卡回归共 6 项通过，三个 engine 成功请求增量为 6/6/6。混合压测额外覆盖 fese 全 46 页。新 API、三个 parse worker 已重载，`/ready` 返回 200，队列排空后保存 PM2 状态。完整硬件重启复验未执行。

私有证据目录为 `output/mineru4_tdd/reboot-optimization/`，含故障日志、配置失败回归、各组 `report.json` 和完整解析资产。模型、原始 PDF、配置凭证和解析全文均未加入 Git。


## 队列与单文件优化（2026-09-18，第二轮）

测量后优先处理固定开销和提交方式，继续使用 advanced，保持整本解析、页码、图片及跨页后处理。

- 46 页 fese 的业务拼接用 cProfile 对同一份真实解析资产重复 200 次，均值约 7.86 毫秒。合并 Unicode 清理函数、去掉重复 UTF-8 编解码，并使用运行时 Mapping 类型后约 5.97 毫秒。这不是模型解析的主要耗时，不能把这项微优化解释为整份 PDF 加速 24%。
- 单文件隔离进程原来在结果生成后仍等待嵌套 PDF 渲染池退出，约 5.8 秒后才由 watchdog 强制清理。现在成功和失败路径均显式关闭本任务已加载的渲染池；保留原 hard timeout、父进程退出信号和进程组兜底。没有把返回提前到资源清理之前。
- 批量脚本改为默认 6 个任务的滚动在途窗口，任务 ID 和结果原子保存。状态查询故障继续查原 ID，本地超时支持续跑；不再因为轮询超时就重投。POST 结果未知时明确停止并保留记录。用法和恢复边界见[批量脚本](two_stage_task_usage.md#批量脚本)。

同一批 30 份 p2，通过实际批量脚本调用线上 three-worker two-stage；每轮均成功，包含上传、每秒轮询、JSON/PKL 与任务日志写盘：

| 在途窗口 | 整批完成时间 | 峰值未完成任务 |
| --- | ---: | ---: |
| 30（一次全部入队） | 26.61 秒 | 30 |
| **6（采用）** | **26.44 秒** | **6** |
| 3 | 30.33 秒 | 3 |

6 和 30 在本次单轮测量中性能相近，6 减少已上传文件积压；3 的余量不足，受轮询/补位空档影响。窗口大小应随 worker 数、文件大小和视觉阶段压力调整，它只约束当前客户端，不能替代服务端的全局准入或磁盘配额。解析 worker 保留 solo/1、late ack 和 prefetch=1，避免提前领取过多长任务；相关机制见 [Celery 官方优化说明](https://docs.celeryq.dev/en/stable/userguide/optimizing.html#prefetch-limits)。

单文件端到端对照，均使用 p2、advanced、chunk_type/return_txt，串行执行 6 份：

| 调用方式 | 总耗时 | 每份平均 |
| --- | ---: | ---: |
| 原同步 `/mineru` | 58.48 秒 | 9.75 秒 |
| **修复后同步 `/mineru`** | **25.74 秒** | **4.29 秒** |
| 已预热 two-stage，每次仅一个在途 | 14.79 秒 | 2.47 秒 |

同步减少约 56% 的端到端时间，同时保留每任务进程隔离。队列单任务测试每 0.1 秒轮询；生产脚本默认每秒轮询，实际观察延迟还会增加至多约一轮查询的等待。队列优势还包括复用 CPU 模型和连接；不是队列本身让一次模型生成更快。

另外尝试三个解析进程下的混合六份 PDF：ONNX 8/1 + VLM 8 用时 89.88 秒，慢于已采用的 16/1 + VLM 8（前轮 73.53 秒）；16/1 + VLM 16 为 69.75 秒，单轮仅改善约 5%，尚不足以确认稳定收益，线上继续使用 VLM 8。未降低质量档位、压缩图片或修改模型精度。

对低频单文件，同步接口仍适合需要直接响应、纯解析或 MinIO 的调用；对批量文件，使用队列可以复用已预热解析进程。two-stage 包含额外视觉描述，不能与 `/mineru` 的功能直接等同。本轮 p2 没有实际视觉任务，因此队列速度不是图片描述服务的容量测试。

回归：172 项常规测试通过，20 项模型测试默认跳过；显式运行的 p2 缺省/四档与九页论文三卡回归共 6 项通过，三个 engine 请求增量 7/6/5。实际 CLI 在提交 p2 后中断，再以相同输出目录续跑，沿用同一任务 ID、attempts=1；再次运行跳过已有结果。

本轮私有证据位于 `output/mineru4_tdd/queue-optimization/`。剖析的多线程/异步累计 CPU 时间不可简单相加；性能结论使用外部墙钟耗时。上述为共享主机单轮对照，不提供全负载容量保证。


### 图片描述优化

图片模型与 MinerU 的三卡解析模型是两个独立服务。实测图片请求原本已经携带 `chat_template_kwargs.enable_thinking=false`，六张图响应中 reasoning_content 均为空；本轮保留关闭设置。公开 `.env.example` 与本机覆盖采用 Qwen3.5 非 thinking 采样：temperature=0.7、top_p=0.8、top_k=20、presence_penalty=1.5、min_p=0、repetition_penalty=1，依据 [Qwen 官方模型卡](https://huggingface.co/Qwen/Qwen3.5-397B-A17B/blob/main/README.md)。通用代码缺省仍为 1/1/40/2，避免假定所有显式配置的模型都适用 Qwen3.5 参数；进程环境仍优先于 `.env`。

默认提示词聚焦数字、单位、标签和关系，要求同类数据合表、单位集中说明，避免重复 caption、装饰性描述、通用结论和未标注数值的推算。自定义 prompt 和原生 DOCX 严格 OCR 保持各自规则。正则只移除开头完整 `<think>...</think>`、确定的中英文套话及内部 Page/ChunkType 标记，不按长度截断，不删除含数字的正文或“不清晰”等限定。严格 OCR 跳过新增套话/think 清理，保留原图可能印有的文字。OpenAI-compatible 空响应、非正常结束（含 length）和仅套话的结果会失败，防止把 caption 当成成功兜底。

同步图片接口及普通图片任务由每批新建线程池、等待整批结束，改为单线程池有空位即补下一张；结果仍按原图位置回填，上下文在请求前固定。`VISION_BATCH_SIZE` 是每请求并发窗口，代码、模板和当前主机仍为 3；多 API 进程的窗口会叠加。two-stage 继续使用独立 vision threads/32，不能通过这个变量调整，也不与解析的 VLM 并发 8 混为一谈。

对同一论文的六张已保存原图和同一上下文做真实模型调用：

| 提示词 / 参数 | 在途图片 | 六图耗时 | 输出 token | 输出字符 |
| --- | ---: | ---: | ---: | ---: |
| 原提示词 / 原采样 | 6 | 20.64 秒 | 2753 | 10173 |
| 新提示词 / Qwen3.5 非 thinking 采样 | 6 | 17.87 秒 | 2303 | 6720 |
| 新提示词 / Qwen3.5 非 thinking 采样 | 3 | 16.43 秒 | 2014 | 6125 |

六并发对照约减少 16% 输出 token、34% 字符；耗时仅是共享远端服务的单轮测量。3 与 6 并发并未呈现稳定的翻倍收益，因此保留同步窗口 3 和 Celery 32，不盲目加大请求数。仅换第一版提示词曾使 token 增至 2904，也没有采用。最终输出仍可能有重复说明或不准确的图形推断，不能把提示词当作事实校验器；本轮关键数字回归与人工抽查不等于所有图表的完整准确率评估。

新增真实回归从 input 论文第五页解析图像，再请求多模态模型，检查瀑布图各面板的起止值、52% 与 CO₂ 单位；不会使用 caption 中的数字替代模型输出。运行：

```bash
MINERU_RUN_VISION_PDFS=1 uv run --group dev pytest tests/test_vision_input_pdf.py -v
```

API 与六个 two-stage worker 已重载并完成整本线上验收：同一九页论文、6 个真实图片任务，解析后 vision/dispatch/merge 18.30→16.75 秒，整份 49.16→48.19 秒；解析阶段仍约 31 秒，不能将图片阶段收益等同于整份文件收益。`/health`、`/ready`、`/gpu/status`、`/two_stage/queue_status` 均为 200，PM2 状态已保存。

证据与各版输出保存在同一私有目录 `output/mineru4_tdd/queue-optimization/`，模型输出未提交 Git。

## AI 接入文档入口（2026-09-18）

面向调用者的完整指南为 [AI 接入指南](docs/ai-integration.md)，通过 `GET /guides/ai-integration.md` 返回 UTF-8 Markdown；`GET /llms.txt` 提供指向该指南与部署 `/openapi.json` 的小型索引，支持 root_path 前缀。两条新增路由继承现有业务 Bearer 鉴权，不挂载文件目录。FastAPI 自动 `/docs`、`/redoc`、`/openapi.json` 不因此增加鉴权，需要限制时在网关另行配置。

[调优指南](docs/performance-tuning.md) 正常纳入 Git，作为开发运维文档维护，但没有服务路由，也不出现在 llms.txt 中；没有新增 Git 忽略规则。服务不提供 MCP，远程域名/TLS/网络可达性仍由部署方配置。

本次 177 项常规测试通过、20 项模型测试默认跳过；只变更说明与文档只读入口，未重跑模型性能基准。API 已重载，PM2 状态已保存；两个文档入口带凭证 200、无凭证 401，调优文件路径 404，`/health` 与 `/ready` 为 200。轮询示例另验证了成功、普通任务 HTTP 500 失败、two-stage HTTP 200 失败及代理路径前缀。
