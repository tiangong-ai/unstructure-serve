# TianGong AI Unstructure Serve

基于 FastAPI 的文档解析服务，提供 MinerU 解析、图片视觉增强、Celery 异步任务、MinIO 资产存储及 Markdown→DOCX。当前使用 **MinerU 4.0.0 + Docker vLLM**，Python 应用依赖由 `uv.lock` 固定。

仓库已迁至 [tiangong-ai/unstructure-serve](https://github.com/tiangong-ai/unstructure-serve)，保留原有历史。组织迁移背景见[迁移公告](https://github.com/tiangong-ai/cli-toolkit/releases/tag/v0.0.63)。

## 文档入口

| 文档 | 内容 |
| --- | --- |
| [部署与回归](mineru_4_upgrade_usage.md) | 安装、配置优先级、Docker/PM2、验证、当前主机与回滚记录 |
| [普通异步任务](mineru_with_images_task_usage.md) | `/mineru/task`、`/mineru_with_images/task`、普通 worker 与 MinIO |
| [两段式任务](two_stage_task_usage.md) | `/two_stage/task`、四类 worker、队列与批量脚本 |
| [多卡后续工作](multi_gpu_vllm_scaling_todolist.md) | 已有能力与尚未实现的调度、容错和压测工作 |
| [代理说明](AGENTS.md) | 代码入口、兼容合同和修改要求 |
| [历史记录](docs/history/README.md) | 3.x DOCX 评估、4.0 升级前评估和旧视觉改动 |

## 开始运行

首次部署先按[部署说明](mineru_4_upgrade_usage.md)安装系统工具，准备 `.env`、`.secrets/secrets.toml`、CPU 模型和 Redis。以下命令均在仓库根目录执行：

```bash
uv python install 3.12
uv sync --locked --group dev
pm2 start ecosystem.vllm.parallele.config.json
pm2 start ecosystem.config.json
pm2 start ecosystem.two_stage.celery.json
pm2 save
```

上述模型模板使用 GPU 0、1、2，在一个 Docker 容器中启动三个 vLLM 副本，通过单一 `30000` 端口内部负载均衡；单卡替代步骤见部署说明。

API 默认端口为 `7770`，Swagger 位于 `/docs`。普通 `/mineru/task` 和 `/mineru_with_images/task` 还需要单独启动 `ecosystem.celery.json`；仅启动 two-stage worker 不会消费普通任务。

开发时可单独启动 API：

```bash
uv run uvicorn src.main:app --host 127.0.0.1 --port 7770
```

## 解析接口

| POST 路径 | 执行方式 | 图片视觉增强 | MinIO |
| --- | --- | --- | --- |
| `/mineru` | 同步 | 否 | 支持 |
| `/mineru_sci` | 同步科研入口 | 否 | 不支持 |
| `/mineru_with_images` | 同步 | 是 | 支持 |
| `/mineru/task` | 普通 Celery | 否 | 支持 |
| `/mineru_with_images/task` | 普通 Celery | 是 | 支持 |
| `/two_stage/task` | parse/dispatch/vision/merge | 是 | 不支持 |

支持 PDF、PNG/JPEG/WebP/BMP/TIFF，以及[Office 转换清单](src/utils/file_conversion.py)中的格式。Office 主结果先经 LibreOffice 转 PDF；Markdown/TXT 不走解析接口。MinerU 上游新增的所有原生格式并未自动向本服务开放。

六个接口均接受 multipart 表单字段 `file` 和 `tier`。`tier` 不传固定使用 `advanced`，与 API 进程环境变量无关；异步任务保留提交时的选择，非法值返回 422。

| tier | 用途 |
| --- | --- |
| `flash` | 读取原生文本层，无推理模型；适合电子 PDF 预览，扫描件应选择 OCR 档位 |
| `basic` | 小模型进行 OCR、公式和表格识别；本部署使用 CPU ONNX |
| `standard` | 小模型结合 Docker VLM |
| `advanced` | 默认，使用更多 VLM 推理计算处理困难文档 |

`chunk_type`、`return_txt` 在前五个接口中是 **URL 查询参数**，仅 `/two_stage/task` 把它们定义为表单字段。`chunk_type=true` 保留标题、页眉、页脚及原阅读顺序；视觉增强流程为图片识别块标注 `image`。普通正文或表格不保证有 `type` 字段。`return_txt=true` 返回拼接纯文本，页码从 1 开始。

以下示例要求 shell 中已有实际的 `FASTAPI_BEARER_TOKEN`；Python 会读取 `.env`，curl 不会自动读取：

```bash
curl --fail-with-body 'http://127.0.0.1:7770/mineru?chunk_type=true&return_txt=true' \
  -H "Authorization: Bearer $FASTAPI_BEARER_TOKEN" \
  -F 'file=@input/p2.pdf' \
  -F 'tier=advanced'
```

更多请求示例见 [test.http](test.http)。图片描述使用独立的 `VISION_*` / `VLLM_BASE_URLS` 配置；`tier` 控制 MinerU 拆解，不选择图片描述模型。同步 DOCX 的原生 TXT-only 分支固定使用 `flash`，主 JSON 结果仍使用 Office→PDF 后的所选档位。

## 检查与测试

```bash
uv run --group dev black .
uv run --group dev ruff check src
uv run --group dev pytest
```

真实模型测试默认跳过。模型和 `input` 样本准备好后，按[PDF 回归说明](mineru_4_upgrade_usage.md#验证与回归)执行；常规测试通过不代表所有长 PDF 已完整解析。

`/health` 检查 API 存活，`/ready` 检查 MinerU 模型是否就绪；任务状态由 `/gpu/status`、`/two_stage/queue_status` 提供。日常维护使用指定项目、容器或 PM2 进程的命令，见[运维说明](mineru_4_upgrade_usage.md#启动与维护)。
