# 普通 MinerU 异步任务

> 新批次使用[统一批量客户端](batch-processing.md)：`--mode parse` 为普通纯解析，`--mode images` 为普通图片增强，`--mode two-stage` 为分阶段图片增强。默认在途 2，JSON 落盘并保存任务记录；旧脚本仅保留兼容续跑。

本文件覆盖 `POST /mineru/task` 和 `POST /mineru_with_images/task`；后者额外进行图片视觉识别。两者使用 Celery app `src.services.celery_app`，分别运行 `mineru.parse` / `mineru.parse_images`，通过同路径的 `GET .../{task_id}` 查询结果。

环境、模型和 Docker 准备见[部署说明](mineru_4_upgrade_usage.md)。当前主机已运行普通及 two-stage worker；独立部署本文件接口时需启动普通 worker。

## Worker 与队列

| priority | 队列 | 配置 |
| --- | --- | --- |
| `urgent` | `queue_urgent` | `CELERY_TASK_URGENT_QUEUE` |
| 其他值或不传 | `queue_normal` | `CELERY_TASK_MINERU_QUEUE` |

普通 worker 只监听 urgent/normal，不消费 two-stage merge 使用的 `default`。它与 [two-stage](two_stage_task_usage.md) 的解析、视觉和调度队列不同。API 与 worker 必须共享 broker、result backend，以及同一路径下的 `MINERU_TASK_STORAGE_DIR`。

在仓库根目录启动：

```bash
./deploy/manage.sh start ordinary
uv run celery -A src.services.celery_app inspect active_queues --timeout=5
pm2 logs celery-worker --lines 100
```

模板使用 `-P threads -c 16 --prefetch-multiplier=1`。手动单任务启动方式：

```bash
uv run celery -A src.services.celery_app worker \
  -l info -Q queue_urgent,queue_normal \
  -P solo -c 1 --prefetch-multiplier=1
```

两者选择一种。解析会再创建子进程，不使用 prefork 池。若启用监控，使用 `deploy/pm2/ecosystem.celery.flower.json`；默认 5555，与 two-stage Flower 同时运行需改端口。

## 请求参数

**`chunk_type` 和 `return_txt` 是查询参数，不是表单字段。** 其余字段的位置以以下表格和 `/docs` 为准。

| 字段 | 位置 | 默认值 / 含义 |
| --- | --- | --- |
| `file` | multipart form | 必填；PDF、受支持图片或 Office 转 PDF 格式 |
| `tier` | form | `advanced`；可选 `flash/basic/standard/advanced`，非法值 422，入队后保留 |
| `priority` | form | `normal`；`urgent` 插入 urgent 队列 |
| `chunk_type` | query | `false`；保留标题、页眉、页脚类型；图片接口标注 image，保持阅读顺序 |
| `return_txt` | query | `false`；返回拼接纯文本 `txt` |
| `pretty` | query | `false`；格式化 JSON |
| `provider` / `model` / `prompt` | form，仅图片接口 | 可选图片描述设置；通常使用服务配置 |

- 一直 PENDING：检查 `celery-worker` 是否在线、`active_queues` 是否包含 `queue_urgent,queue_normal`、API/worker 是否使用同一个 Redis DB；仅有 two-stage worker 不会消费这些任务。
- 任务 FAILURE：查看返回 `error` 和 `pm2 logs celery-worker`。重点检查 Docker MinerU 端点 `MINERU_MODEL_VLM_SERVER_URL`、独立视觉端点 `VLLM_BASE_URLS`、CPU 模型文件、LibreOffice、共享任务目录和任务超时。
- 入队前 422：检查 tier 和请求字段类型；普通图片接口不会因为未知 provider/model 直接 422。
- 调整 worker 前查看 `inspect active`、`inspect reserved` 和 `inspect active_queues`；上传工作区需等任务完成后按具体任务清理。不要把整个共享临时目录当作普通任务的独占目录。

旧 `src/scripts/enqueue_input.py` 是优先级演示，会把每个文件分别投到 normal/urgent 两次，且默认指向开发端口 8770；不要将它当作本机生产批处理入口。需要批处理和结果落盘时使用 [统一批量客户端](batch-processing.md) 的 parse/images 模式。
