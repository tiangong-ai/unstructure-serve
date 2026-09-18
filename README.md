# TianGong AI Unstructure Serve

把 PDF、扫描图片和 Office 文档拆成带页码的结构化文本，可额外识别图表，也可把 PDF、JSON 和逐页图片保存到 MinIO。支持单文件同步调用和可续查的异步任务。

当前技术栈：**Python 3.13.15 · MinerU 4.0.2 · CPU ONNX · Docker vLLM 0.21.0 · FastAPI/Celery**。应用依赖由 `uv.lock` 固定，应用环境不安装 Torch/vLLM。质量默认 `advanced`，也支持 `flash/basic/standard`。

## 先理解需要运行什么

| 程序 | 用途 | 何时需要 |
| --- | --- | --- |
| Docker MinerU VLM | 解析模型；默认 GPU 0/1/2，DP=3、单地址 30000 | standard/advanced |
| Gunicorn API | 上传、提交、查询，默认 7770 | 所有服务调用 |
| Redis | Celery 队列与结果状态 | 异步任务 |
| 普通 Celery worker | 纯解析或完整图片增强任务，支持 MinIO | 两个普通 `/task` 接口 |
| two-stage 六个 worker | 三个解析 worker，加图片调度、识别、合并 | 图片增强批处理 |
| 独立图片模型服务 | 为提取出来的图片生成描述 | 图片增强；与 MinerU VLM 是两个服务 |

不带图片识别的批量任务优先 `/mineru/task`；带图片识别的批量优先 `/two_stage/task`。需要 MinIO 的图片任务使用 `/mineru_with_images/task`。**队列名称和消费者必须配套，不能只启动 API。**

## 首次初始化

以下命令在仓库根目录执行。准备 Linux、支持 GPU 的 NVIDIA 驱动/Container Toolkit、Docker Compose 2.24.4+、PM2，以及较新的 uv（建议 0.12.16+）。系统组件安装及单卡方案见[部署说明](docs/mineru_4_upgrade_usage.md#首次安装)。不要修改系统 `/usr/bin/python3`。

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

Redis 可以复用已有服务。全新单机没有 Redis 时，按[部署说明](docs/mineru_4_upgrade_usage.md#首次安装)创建；不要重建或清空已有共享 Redis。

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

`app` 包含 API 和六个 two-stage worker；`ordinary` 启动普通 worker。统一脚本可从任意目录用绝对路径调用；重复 start 会跳过已在线进程，修改配置请使用 restart。PM2 模板位于 `deploy/pm2`，模型 YAML 位于 `deploy/mineru-vllm`，Gunicorn 参数位于 `deploy/gunicorn.conf.py`。可选 Flower 不会自动启动。

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

`/health` 只确认 API 存活；`/ready` 检查 MinerU 模型端点；还需核对队列消费者、独立图片服务和一次真实 PDF。若驱动升级后 GPU 可见但无法推理，按[Docker 故障排查](docs/mineru_4_upgrade_usage.md#docker-与多卡)检查 UVM 设备。

## 如何调用

启用鉴权时带 Bearer。下面假定 shell 已有 `FASTAPI_BEARER_TOKEN`；curl 不会自动读取 `.env`。

```bash
curl --fail-with-body 'http://127.0.0.1:7770/mineru?chunk_type=true&return_txt=true' \
  -H "Authorization: Bearer $FASTAPI_BEARER_TOKEN" \
  -F 'file=@input/p2.pdf' -F 'tier=advanced'
```

| POST 接口 | 执行方式 | 图片增强 | MinIO |
| --- | --- | --- | --- |
| `/mineru` | 同步 | 否 | 是 |
| `/mineru_sci` | 同步科研入口 | 否 | 否 |
| `/mineru_with_images` | 同步 | 是 | 是 |
| `/mineru/task` | 普通队列 | 否 | 是 |
| `/mineru_with_images/task` | 普通队列 | 是 | 是 |
| `/two_stage/task` | 分阶段队列 | 是 | 否 |

六个接口均上传 `file`，可选表单 `tier`；`flash` 适合电子 PDF 文本层预览，`basic` 使用 OCR 小模型，`standard/advanced` 调用 Docker VLM。`chunk_type`、`return_txt` 在前五个接口中是 URL 查询参数，仅 two-stage 使用表单。返回页码从 1 开始，`return_txt=true` 附加按阅读顺序拼接的文本。

支持 PDF、PNG/JPEG/WebP/BMP/TIFF 和[Office 转换清单](src/utils/file_conversion.py)，不接受 Markdown/TXT 作为解析输入。异步提交后保存 task_id，持续查询对应 `/task/{task_id}`；查询超时不能视为任务失败并盲目重投。

大量 400–1000 页 PDF 应整本入队，先单文件验证，再逐步增加在途数量；不能把默认并发或抽页回归当作千页容量保证。带图片识别可用[可续跑批量脚本](docs/two_stage_task_usage.md#批量脚本)。完整选择规则和可运行客户端见 [AI 接入指南](docs/ai-integration.md)。

## 文档与开发

| 文档 | 内容 |
| --- | --- |
| [部署与回归](docs/mineru_4_upgrade_usage.md) | 配置、模型、队列、恢复、回滚和实测记录 |
| [AI 接入指南](docs/ai-integration.md) | 端点选择、上传字段、轮询、失败处理、大文档批量 |
| [普通任务](docs/mineru_with_images_task_usage.md) / [two-stage](docs/two_stage_task_usage.md) | 各自队列、字段及批量示例 |
| [架构说明](docs/architecture.md) | 模块职责、容量与生命周期、部署目录 |
| [调优指南](docs/performance-tuning.md) | CPU/GPU/模型变化后的测量和参数选择 |
| [依赖审计](docs/dependency-audit-2026-09-18.md) | 升级前版本约束与隔离验证 |
| [HTTP 示例](examples/test.http) / [历史记录](docs/history/README.md) | 调用样例与旧版本证据 |

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
