# MinerU 4 部署与回归

当前发布基线为 MinerU 4.0.0：应用调用无状态 SDK `mineru.parser.parse`，小模型使用 CPU ONNX，VLM 通过 Docker 提供。应用 `.venv` 不安装 vLLM。质量档位和请求参数见 [README](README.md#解析接口)，升级前分析保存在[历史评估](docs/history/mineru_4_upgrade_evaluation.md)。

## 首次安装

所有命令在仓库根目录执行。系统需要 Python 3.12、uv、Docker Compose、PM2，以及支持本机 GPU 的 NVIDIA 驱动和 Container Toolkit。Docker 需已注册 `nvidia` runtime；Compose 显式指定该 runtime，以兼容本机 CDI 模式。

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
docker compose -f compose.mineru.yaml up -d --build
docker compose -f compose.mineru.yaml logs -f mineru-vlm
```

容器首次启动会下载 VLM 权重并编译，等待 healthy 后再接入请求。CPU 模型可通过 `MINERU_HOME` 指定持久目录；容器模型及下载缓存保存在命名卷中。

Celery 需要 Redis。已有实例直接复用；仅在未部署时创建：

```bash
docker run -d --name redis --restart unless-stopped -p 127.0.0.1:6379:6379 redis:8
```

## 配置规则

Python 配置优先级为 **进程环境 > `.env` > `.secrets/secrets.toml` 的回退值**。`load_dotenv()` 不覆盖已有环境；PM2 的 `env` 块因此优先于 `.env`。部分字符串字段的空值仍回退到 TOML，不能用空字符串假定已清除旧配置。修改配置后要重新加载对应 API/worker。Docker Compose 单独读取 `.env` 做变量替换，Python 加载 `.env` 不会把变量导出到调用它的 shell；curl 示例要求 shell 中已有 `FASTAPI_BEARER_TOKEN`。

| 配置 | 模板值 / 作用 |
| --- | --- |
| `MINERU_DEFAULT_TIER` | `standard`，仅直接服务调用缺省时兜底；HTTP 不传 tier 固定 standard |
| `MINERU_DEFAULT_METHOD` | `auto`，映射 SDK `ocr_mode`，可选 `auto/txt/ocr` |
| `MINERU_MODEL_SMALL_BACKEND` | `onnx`，CPU 小模型 |
| `MINERU_MODEL_VLM_SERVER_URL` | `http://127.0.0.1:30000`，MinerU 模型推理地址 |
| `MINERU_MODEL_VLM_MODEL` | `mineru4`，容器公开的模型名 |
| `MINERU_MODEL_VLM_API_KEY` | 可选 Bearer 认证；本机容器默认不启用认证 |
| `MINERU_MODEL_VLM_HTTP_TIMEOUT` / `MINERU_MODEL_VLM_MAX_CONCURRENCY` | `600` 秒 / `8`，单次模型请求与并发 |
| `MINERU_DOCKER_GPU_ID` / `MINERU_DOCKER_PORT` / `MINERU_DOCKER_GPU_MEMORY` | `0` / `30000` / `0.15`；端口须与应用 URL 一致，显存比例按空闲资源调整 |
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
| `compose.mineru.yaml` | 默认 MinerU VLM 生命周期入口，重启策略 `unless-stopped` |
| `ecosystem.config.json` | API `unstructured-gunicorn`，端口 7770 |
| `ecosystem.two_stage.celery.json` | 当前部署使用的 parse/vision/dispatch/merge 四个 worker |
| `ecosystem.celery.json` | 普通任务 worker，调用两个普通 `/task` 接口时另行启动 |
| `ecosystem.two_stage.flower.json` / `ecosystem.celery.flower.json` | 对应 Celery app 的可选监控；都默认 5555，同时使用时须更改端口 |
| `ecosystem.vllm*.json` | 保留的 PM2→Docker Compose 包装模板；每卡独立 project，不要与另一入口重复管理同一容器 |
| `ecosystem.quatro.json` | 多 API 实例示例，不等于自动扩容 Docker 推理服务 |

API 使用 `uvicorn_worker.UvicornWorker`。普通 worker 模板为 threads/16，保守手动启动可用 solo/1；two-stage parse 为 solo，其余为线程池，详情见各自任务说明。GPU 调度器会再创建子进程，不使用 Celery prefork 执行这类解析。

以下为常用检查，启用鉴权时传入实际令牌：

```bash
pm2 status
docker compose -f compose.mineru.yaml ps
curl --fail http://127.0.0.1:30000/health
curl --fail -H "Authorization: Bearer $FASTAPI_BEARER_TOKEN" http://127.0.0.1:7770/health
curl --fail -H "Authorization: Bearer $FASTAPI_BEARER_TOKEN" http://127.0.0.1:7770/gpu/status
uv run celery -A src.services.two_stage_pipeline inspect active_queues --timeout=5
pm2 logs unstructured-gunicorn --lines 100
```

上例模型端口使用模板值；本机已部署实例使用下方记录的 `31000`。更新服务时先确认队列及 active/reserved 任务，等待当前解析收敛，再对指定进程执行 `pm2 reload ecosystem.config.json --update-env` 或重启对应 worker。改动 `.env` 时检查 PM2 `env` 是否覆盖同名字段。

停机分别使用 `pm2 stop ecosystem.config.json`、对应 worker 模板以及 `docker compose -f compose.mineru.yaml stop`。运行清理应定位本项目具体任务或工作目录；共享 Redis、PM2 和 GPU 上还有其他服务，不提供全局清空或按端口强杀作为日常维护步骤。结果过期不等于任务目录可以无条件删除。

## Docker 与多卡

镜像基于 `vllm/vllm-openai:v0.21.0`，安装 MinerU 4.0.0，保留基础镜像配套的 Torch 2.11.0/CUDA 13。Dockerfile 补齐 Pycairo/Cairo 依赖并执行 `pip check`。FastAPI/Starlette/instrumentator 组合已验证；应用 OpenAI SDK 固定在 MinerU `<3` 约束内的 2.54.0。应用升级依据 `uv.lock`，镜像升级需重新验证其完整依赖组合。

模型为 `MinerU2.5-Pro-2605-1.2B`，容器公开名 `mineru4`，最大上下文长度按模型配置设置为 8192。端口默认仅绑定宿主 `127.0.0.1`；跨机器部署需另行配置可达地址和认证。

多卡使用不同 project、端口和 GPU ID：

```bash
MINERU_DOCKER_GPU_ID=0 MINERU_DOCKER_PORT=30000 docker compose -p mineru-gpu0 -f compose.mineru.yaml up -d --build
MINERU_DOCKER_GPU_ID=1 MINERU_DOCKER_PORT=30001 docker compose -p mineru-gpu1 -f compose.mineru.yaml up -d --build
```

应用端删除单 URL 配置，再设置 `MINERU_VLLM_SERVER_URLS`（逗号分隔或 JSON 数组）。单 URL 优先。现有选择仅为进程内轮换，没有跨任务负载均衡、端点熔断或故障重试承诺，后续工作见[多卡计划](multi_gpu_vllm_scaling_todolist.md)。

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

2026-09-17 升级验收记录：

- 常规测试 135 项通过，覆盖六入口 tier 默认/枚举/透传、SDK 资产与字段、MinIO、Office、视觉失败传播及调度生命周期。
- 升级阶段 11 份 PDF 的 15 项实测通过：29 个抽样页，加 p2/九页论文/46 页 fese 整本，共覆盖 79 个不同源页面；不代表 11 份长文档全部整本验收。
- tier 补充阶段 p2 四档均通过，共形成当前 17 项可选模型测试。basic 丢失的首个 checkbox 在同页唯一完整选项组匹配时回填，并有歧义、跨页、短标签等拒绝条件测试。
- 在线六入口的 schema/非法值、同步四档、真实 two-stage basic、MinIO PDF/JSON/JPEG/meta、Office→PDF、原生 DOCX 顺序通过验证；论文的一张图片完成真实视觉请求，未逐图人工评估全部视觉输出。

证据仅保存在忽略的 `output/mineru4_tdd/`：`input-results-all.xml`、`tier-p2-results.xml`、`tier-pytest.log`、`tier-api-smoke.log`、`main-deployment-p2.json`。原始文档及解析资产不纳入 Git。

## 本机部署与回滚记录（2026-09-17）

升级已合并到 `main`（`e2de2fe`），运行目录为 `/home/david/projects/TianGong-AI-Unstructure-Serve`。API 7770；Compose project `mineru4-upgrade` 由本机 `.env` 指定，模型端口 31000、GPU 0、显存比例 0.09。CPU 模型在 `/home/david/.local/share/tiangong-mineru4`，Docker 缓存使用命名卷。

API 与 四类 two-stage worker 已启动，旧 MinerU 本机 vLLM 进程和 PM2 启动项已移除，PM2 已保存。另一个仓库的 `vllm-qwen3-embedding-8b` 属于独立 embedding 服务。私有 `.env` 和已运行服务不因文档模板整理而自动改变。

兼容层仍返回 `(content_list, artifact_dir, None)`，PDF 默认整本，保留源页号和可访问图片；业务 MinIO `parsed.json` 使用服务响应结构，不用原生 MiddleJson 替换。Office 主 JSON/MinIO 资产来自 PDF，原生 DOCX 仅用于同步 TXT 增强。

旧源码、配置、PM2 快照和旧 `.venv` 保存在 `output/mineru4_tdd/rollback-20260917/`。回滚需排空或隔离在途任务，并同步恢复代码、应用环境及模型服务地址；旧虚拟环境须放回原 `.venv` 路径，避免绝对 shebang 失效。保留已完成资产与模型缓存。
