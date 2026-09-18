# TianGong AI Unstructure Serve

## 直接复制给 AI：启动、恢复、停止整套设施

在能访问部署机器的 AI 中打开本仓库作为工作区，再复制以下任一段。AI 会读取本地配置使用凭证，无需把密钥粘贴到对话中。整套设施包括本项目的 API、普通 worker、六个 two-stage worker 和三卡 MinerU 模型，以及配置中依赖的 Redis、图片模型；共享或其他项目管理的依赖须先识别归属。

**首次初始化并启动：**

```text
请初始化并启动当前工作区中 TianGong AI Unstructure Serve 的整套文档解析设施。
先阅读仓库 AGENTS.md、README.md 和 docs/mineru_4_upgrade_usage.md，核对现有进程、端口、GPU、模型缓存和私有配置。已有配置和缓存应复用，缺失配置按示例初始化；不要覆盖凭证或修改系统 Python。缺少必须由我提供的凭证/地址时，明确列出缺项。
按 uv.lock 使用 Python 3.13.15，应用安装 CPU ONNX 依赖，MinerU 大模型只运行在 Docker。按文档准备和验证小模型，检查 Redis、独立图片模型；若依赖由其他项目管理，按其已有方式使用，不重复创建。
通过 deploy/manage.sh start model 启动模型，等待实际健康；再执行 start app 和 start ordinary，启动 API、六个 two-stage worker 与普通 worker。已有在线进程不要重复重启。
验证 API /health、/ready、普通及 two-stage 队列消费者、图片模型可用性，并用 input/p2.pdf 和含图论文做小规模真实验收。完成后 pm2 save；检查开机恢复是否已配置，缺失则按 pm2 startup 的提示配置。
最后列出本项目进程、端口、检查结果、尚缺的配置和可用的调用方式。不要输出密钥，不操作无关服务，不用全局 PM2 删除或 Redis 清空命令。
```

**重启机器后恢复，或排查服务不可用：**

```text
请恢复当前工作区中 TianGong AI Unstructure Serve 的整套文档解析设施。
先读 AGENTS.md、README.md 和部署说明，检查本项目 PM2/容器状态、端口、日志、GPU/CUDA、Redis、独立图片模型，定位故障。先检查在途任务及队列，再决定哪些故障组件需要重启；健康组件保持运行。
复用现有 .venv、uv.lock、私有配置和缓存。通过 deploy/manage.sh start model 补起模型并验证健康，再 start app、start ordinary 补起应用；配置更新需要 restart 时，仅重启受影响组件并先等其任务收敛。不要直接 pm2 resurrect 恢复该用户的所有项目，也不要重启共享 Docker/Redis 或改动其他项目。
如驱动升级后 GPU 可见但推理失败，按部署说明检查 UVM/CDI 和容器内 CUDA，不以 nvidia-smi 正常作为修复完成的依据。
核对 API、三个解析 worker、视觉/调度/合并 worker、普通 worker 和三卡模型，并做小规模真实 PDF 验收。检查批量输出目录中的任务记录及持久任务清单（uv run python -m src.scripts.manage_jobs list），发布不明确的任务按部署说明 recover；失败任务修复原因并确认没有活跃阶段后才按原 ID resume。恢复客户端时使用原命令、原输出目录续查，不重复提交已有 task_id。
完成后 pm2 save，说明原因、修复内容、验证结果和仍未恢复的依赖，不输出密钥。
```

**安全停止整套设施：**

```text
请安全停止当前工作区中 TianGong AI Unstructure Serve 的文档解析设施。
先读 AGENTS.md 和 README.md，确认本项目进程及依赖归属。先停止新增提交，保留各批量客户端的输入、输出目录和任务记录；统一客户端可停止后用原命令加 --resume-only 重新运行，只查询并保存已提交任务，不补交或重试文件。其他提交来源也停止新增请求，保持 API/worker/模型可用直到已有任务结果已收取。
检查普通和 two-stage 的 ready、active、reserved、scheduled/unacked 任务，等待本批任务与各阶段队列收敛；不要把停止 CLI 当成取消服务端任务，不强杀仍在处理的千页文档。如有无法收敛的任务，说明具体任务和原因，不盲目清理。
任务收敛后，依次执行 deploy/manage.sh stop api、stop ordinary、stop workers、stop model，确认本项目进程已停止、MinerU 容器已退出，再 pm2 save，使停止状态在重启后保持。
Redis 和独立图片模型如为共享或其他项目管理的服务，保持运行；仅在确认专用于本项目且停止不会影响其他服务时，按其已有启动方式停止。不删除容器卷、模型缓存、结果、任务记录或私有配置，不使用 pm2 delete all、Redis flush 或全局进程清理。
最后报告已停止的组件、保留的共享依赖，以及下次恢复应执行的步骤。
```

把 PDF、扫描图片和 Office 文档拆成带页码的结构化文本，可额外识别图表。支持单文件同步调用，以及带提交幂等、阶段恢复和结果下载的异步任务。

默认图片描述模型为 **Qwen3.8-Flash-Next-NVFP4**，通过私有 `VLLM_BASE_URLS` 配置独立多模态端点；与 MinerU 文档解析模型分开管理。

当前技术栈：**Python 3.13.15 · MinerU 4.0.2 · CPU ONNX · Docker vLLM 0.21.0 · FastAPI/Celery**。应用依赖由 `uv.lock` 固定，应用环境不安装 Torch/vLLM。质量默认 `advanced`，也支持 `flash/basic/standard`。

## 先理解需要运行什么

| 程序 | 用途 | 何时需要 |
| --- | --- | --- |
| Docker MinerU VLM | 解析模型；默认 GPU 0/1/2，DP=3、单地址 30000 | standard/advanced |
| Gunicorn API | 上传、提交、查询，默认 7770 | 所有服务调用 |
| Redis | Celery 队列与结果状态 | 异步任务 |
| 普通 Celery worker | 纯解析或完整图片增强任务 | 两个普通 `/task` 接口 |
| two-stage 六个 worker | 三个解析 worker，加图片调度、识别、合并 | 图片增强批处理 |
| 独立图片模型服务 | 为提取出来的图片生成描述 | 图片增强；与 MinerU VLM 是两个服务 |

不带图片识别的批量任务优先 `/mineru/task`；带图片识别的批量优先 `/two_stage/task`。**队列名称和消费者必须配套，不能只启动 API。**

## 首次初始化

以下命令在仓库根目录执行。准备 Linux、支持 GPU 的 NVIDIA 驱动/Container Toolkit、Docker Compose 2.24.4+、PM2，以及较新的 uv（建议 0.12.16+）。系统组件安装及单卡方案见[部署说明](docs/mineru_4_upgrade_usage.md#首次安装)。不要替换操作系统自带的 Python。

```bash
sudo apt install -y libmagic-dev poppler-utils libreoffice pandoc graphicsmagick
uv python install 3.13.15
uv sync --locked --group dev
mkdir -p .secrets
cp -n deploy/secrets.example.toml .secrets/secrets.toml
cp -n .env.example .env
```

编辑 `.env` 和 `.secrets/secrets.toml`，至少确认：

- API 鉴权与令牌；即使主要使用环境变量，也保留 TOML 必需段落。
- MinerU 模型地址，默认 `http://127.0.0.1:30000`；小模型使用 ONNX。
- Redis broker/backend 与任务目录，API 和 worker 保持一致。
- 图片增强另填 `VLLM_BASE_URLS`、模型名和凭证。
- 三卡模板要求 GPU 0/1/2 可用；CPU/显存不足时按[调优指南](docs/performance-tuning.md)降低容量。

准备 CPU 模型：

```bash
uv run mineru-kit models download --tier basic --small-backend onnx --source modelscope
uv run mineru-kit models verify --tier basic --small-backend onnx
```

启动异步 worker 前，先准备可用的 Redis 并填写 broker/backend；可以复用已有服务。Redis 和独立图片模型的管理边界见[部署说明](docs/mineru_4_upgrade_usage.md#首次安装)。

启动模型并等待健康检查成功，再启动应用：

```bash
./deploy/manage.sh start model
./deploy/manage.sh logs model
# 首次下载/编译可能较久；以下成功后继续
curl --fail http://127.0.0.1:30000/health
./deploy/manage.sh start app
./deploy/manage.sh start ordinary
pm2 save
```

`app` 包含 API 和六个 two-stage worker；`ordinary` 启动普通 worker。以下脚本命令均从仓库根目录执行；重复 start 会跳过已在线进程，修改配置请使用 restart。PM2 模板位于 `deploy/pm2`，模型 YAML 位于 `deploy/mineru-vllm`，Gunicorn 参数位于 `deploy/gunicorn.conf.py`。可选 Flower 不会自动启动。

## 重启后恢复与日常维护

已配置 PM2 开机服务并保存进程列表时会自动恢复。首次配置时执行 `pm2 startup`，按其提示安装系统服务，然后 `pm2 save`。需要手动恢复保存列表时使用 `pm2 resurrect`，它会恢复该用户保存的全部项目。

只恢复本项目时：

```bash
./deploy/manage.sh start model
curl --fail http://127.0.0.1:30000/health
./deploy/manage.sh start app
./deploy/manage.sh start ordinary
./deploy/manage.sh status
```

修改 `.env`/应用代码后，确认 active/reserved 任务已收敛，再执行相应 `restart api`、`restart workers` 或 `restart ordinary`；模型维护使用 `restart model`。停止对应组件用 `stop`。**不要用全局 `pm2 delete all`、Redis flush 或清空共享任务目录。** 更新 Python/依赖时先按[部署与回滚](docs/mineru_4_upgrade_usage.md)保留旧环境并停妥本项目进程，不能直接覆盖正在运行的 `.venv`。

`/health` 只确认 API 存活；`/ready` 检查 MinerU 模型端点；还需核对队列消费者、独立图片服务和一次真实 PDF。若驱动升级后 GPU 可见但无法推理，按[Docker 故障排查](docs/mineru_4_upgrade_usage.md#gpu-重启故障排查)检查 UVM 设备。

## 如何调用

启用鉴权时带 Bearer。下面假定 shell 已有 `FASTAPI_BEARER_TOKEN`；curl 不会自动读取 `.env`。

```bash
curl --fail-with-body 'http://127.0.0.1:7770/mineru?chunk_type=true&return_txt=true' \
  -H "Authorization: Bearer $FASTAPI_BEARER_TOKEN" \
  -F 'file=@input/p2.pdf' -F 'tier=advanced'
```

| POST 接口 | 执行方式 | 图片增强 |
| --- | --- | --- |
| `/mineru` | 同步 | 否 |
| `/mineru_sci` | 同步科研入口 | 否 |
| `/mineru_with_images` | 同步 | 是 |
| `/mineru/task` | 普通队列 | 否 |
| `/mineru_with_images/task` | 普通队列 | 是 |
| `/two_stage/task` | 分阶段队列 | 是 |

六个接口均上传 `file`，可选表单 `tier`；`flash` 适合电子 PDF 文本层预览，`basic` 使用 OCR 小模型，`standard/advanced` 调用 Docker VLM。`chunk_type`、`return_txt` 在前五个接口中是 URL 查询参数，仅 two-stage 使用表单。返回页码从 1 开始，`return_txt=true` 附加按阅读顺序拼接的文本。

支持 PDF、PNG/JPEG/WebP/BMP/TIFF 和[Office 转换清单](src/utils/file_conversion.py)，不接受 Markdown/TXT 作为解析输入。异步提交后保存 task_id，持续查询对应 `/task/{task_id}`；查询超时不能视为任务失败并盲目重投。

大量 400–1000 页 PDF 应整本入队，先单文件验证，再逐步增加在途数量；不能把默认并发或抽页回归当作千页容量保证。三个异步入口统一使用[可续跑批量客户端](docs/batch-processing.md)：

```bash
# 不额外描述图片；需要图片增强改 --mode two-stage，并使用新的输出目录
uv run python -m src.scripts.batch_parse \
  --mode parse --input-dir input/batch --output-dir output/batch-parse \
  --max-in-flight 2
```

示例假定已将待处理文件放入 `input/batch`；也可替换为自己的输入目录。`--mode images` 为普通队列图片增强。默认 advanced、输出 JSON；可调档位、上传/轮询超时，支持子目录和 Office/图片格式，原输出目录续跑不会盲目重投。千页整本首次试验用一个文件、在途 1，并先完成服务端长任务配置验收。完整选择规则见 [AI 接入指南](docs/ai-integration.md)。

## 文档与开发

| 文档 | 内容 |
| --- | --- |
| [部署与恢复](docs/mineru_4_upgrade_usage.md) | 配置、模型、队列、恢复与回滚 |
| [AI 接入指南](docs/ai-integration.md) | 端点选择、上传字段、轮询、失败处理、大文档批量 |
| [普通任务](docs/mineru_with_images_task_usage.md) / [two-stage](docs/two_stage_task_usage.md) | 各自队列、字段及批量示例 |
| [统一批量客户端](docs/batch-processing.md) | 三种异步模式、文件筛选、JSON 结果、超时及续跑 |
| [架构说明](docs/architecture.md) | 模块职责、容量与生命周期、部署目录 |
| [调优指南](docs/performance-tuning.md) | CPU/GPU/模型变化后的测量和参数选择 |
| [依赖维护](docs/dependencies.md) | 安装边界、兼容升级与 Python 迁移 |
| [验证指南](docs/validation.md) | 常规检查、真实 PDF 回归和验收范围 |
| [HTTP 示例](examples/test.http) | VS Code REST Client 调用样例 |

远程 AI 可读取 `/llms.txt`、`/guides/ai-integration.md` 和 `/openapi.json`；前两个继承业务鉴权。调优、架构和部署文档仅在仓库维护，不经服务公开。Swagger `/docs` 与 OpenAPI 如需私有，由网关额外保护。

```bash
# 开发 API（不要与生产争用 7770）
uv run uvicorn src.main:app --host 127.0.0.1 --port 8770
# 常规检查
uv run --group dev black .
uv run --group dev ruff check src
uv run --group dev pytest
# 真实 input PDF 回归；需要可用模型
MINERU_RUN_INPUT_PDFS=1 uv run --group dev pytest tests/test_mineru_input_pdfs.py -v
```
