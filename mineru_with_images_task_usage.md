# /mineru_with_images/task 使用说明

> MinerU 4 升级后，解析 worker 连接 Docker VLM 后端。先按 [部署与回归说明](mineru_4_upgrade_usage.md) 准备模型、`.env` 和容器；本文件中的 Celery 队列划分保持不变。

本文档面向能登录本机/容器的同事，说明如何启动并使用mineru_with_images异步接口：

```text
POST /mineru_with_images/task
GET  /mineru_with_images/task/{task_id}
```

这套接口使用普通 MinerU Celery 应用 `src.services.celery_app`，默认队列是：

```text
normal: queue_normal
urgent: queue_urgent
default: default
```

## 运行链路

```text
调用方
  -> FastAPI /mineru_with_images/task
  -> 保存上传文件到 MINERU_TASK_STORAGE_DIR
  -> Redis broker
  -> Celery task mineru.parse_images
  -> worker 本地执行 MinerU + 图片视觉识别
  -> Redis result backend
  -> 调用方轮询 /mineru_with_images/task/{task_id}
```

关键点：

- API 进程和 Celery worker 必须使用同一个 `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND`。
- API 进程会把上传文件路径写入任务参数；如果 API 和 worker 在不同容器，必须挂载同一个 `MINERU_TASK_STORAGE_DIR`，并保证容器内路径一致。
- `priority=urgent` 会进入 `queue_urgent`；其他值或不传会进入 `queue_normal`。
- 视觉服务默认走 `.env` 中的 `VISION_PROVIDER` / `VISION_MODEL` / `VLLM_BASE_URLS` 等配置；调用方通常不需要传 `provider` 和 `model`。

## 服务端启动

以下命令都在仓库根目录执行：

```bash
cd /home/david/projects/TianGong-AI-Unstructure-Serve
```

### 1. Redis

本机已有 Redis 时先确认：

```bash
redis-cli -n 0 ping
```

期望返回：

```text
PONG
```

如果没有 Redis，可临时用 Docker 启动：

```bash
docker run -d --name redis -p 6379:6379 redis:8
```

如果 API/worker 在容器内，`localhost` 指的是容器自身；这种部署要把 `CELERY_BROKER_URL` 改成实际 Redis 地址，例如 `redis://redis:6379/0` 或宿主机可达地址。

### 2. 本地API 服务（可选）

PM2 模板启动：

```bash
pm2 start ecosystem.config.json
```

检查：

```bash
pm2 status
curl -sS http://127.0.0.1:7770/health
```

如果服务开启了 Bearer 鉴权，调用接口时需要带：

```text
Authorization: Bearer <FASTAPI_BEARER_TOKEN>
```

### 3. Celery worker

PM2 模板启动普通 MinerU Celery worker：

```bash
pm2 start ecosystem.celery.json
```

该模板监听：

```text
queue_urgent,queue_normal,default
```

对应配置见 `ecosystem.celery.json`：

```text
CELERY_TASK_MINERU_QUEUE=queue_normal
CELERY_TASK_URGENT_QUEUE=queue_urgent
```

手动启动命令：

```bash
CELERY_BROKER_URL=redis://localhost:6379/0 \
CELERY_RESULT_BACKEND=redis://localhost:6379/0 \
CELERY_TASK_MINERU_QUEUE=queue_normal \
CELERY_TASK_URGENT_QUEUE=queue_urgent \
MINERU_MODEL_SOURCE=modelscope \
uv run celery -A src.services.celery_app worker \
  -l info -Q queue_urgent,queue_normal,default \
  -P solo -c 1 --prefetch-multiplier=1
```

PM2 模板当前使用 `-P threads -c 16`。如果单机 GPU 或视觉服务扛不住并发，优先降低 `-c`，或改用上面的保守 `-P solo -c 1`。

### 4. Flower 监控

可选：

```bash
pm2 start ecosystem.celery.flower.json
```

或手动启动：

```bash
CELERY_BROKER_URL=redis://localhost:6379/0 \
CELERY_RESULT_BACKEND=redis://localhost:6379/0 \
uv run celery -A src.services.celery_app flower --address=0.0.0.0 --port=5555
```

## 启动后检查

确认 worker 在线且监听正确队列：

```bash
uv run celery -A src.services.celery_app inspect active_queues --timeout=5
```

期望能看到 `queue_urgent`、`queue_normal`、`default`。

查看 Redis 队列积压：

```bash
redis-cli -n 0 llen queue_urgent
redis-cli -n 0 llen queue_normal
redis-cli -n 0 hlen unacked
```

查看 PM2 日志：

```bash
pm2 logs celery-worker --lines 100
pm2 logs unstructured-gunicorn --lines 100
```

如果只看到 two-stage worker 监听 `queue_parse_gpu`，说明 `/mineru_with_images/task` 的 worker 没启动；需要启动 `ecosystem.celery.json`。

## 调用接口

下面示例假设 API 地址为 `http://127.0.0.1:7770`。

无鉴权时：

```bash
API_BASE=http://127.0.0.1:7770

curl -sS -X POST "$API_BASE/mineru_with_images/task" \
  -F "file=@/path/to/report.pdf" \
  -F "return_txt=true" \
  -F "chunk_type=true" \
  -F "priority=normal"
```

有 Bearer 鉴权时：

```bash
API_BASE=http://127.0.0.1:7770
TOKEN="<FASTAPI_BEARER_TOKEN>"

curl -sS -X POST "$API_BASE/mineru_with_images/task" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/path/to/report.pdf" \
  -F "return_txt=true" \
  -F "chunk_type=true" \
  -F "priority=normal"
```

返回示例：

```json
{
  "task_id": "9d0f7c9e-1a2b-4c3d-8e9f-000000000000",
  "state": "PENDING"
}
```

轮询状态：

```bash
TASK_ID="9d0f7c9e-1a2b-4c3d-8e9f-000000000000"

curl -sS "$API_BASE/mineru_with_images/task/$TASK_ID?pretty=true"
```

可能状态：

```text
PENDING  已投递但还没开始，或 task_id 不在当前 result backend 中
STARTED  worker 已开始处理
SUCCESS  已完成，响应里会带 result
FAILURE  任务失败，响应里会带 error
REVOKED  任务被撤销
```

成功结果结构：

```json
{
  "task_id": "9d0f7c9e-1a2b-4c3d-8e9f-000000000000",
  "state": "SUCCESS",
  "result": {
    "result": [
      {
        "text": "第一页文本或图片识别结果",
        "page_number": 1,
        "type": "image"
      }
    ],
    "txt": "可选纯文本输出",
    "minio_assets": null
  },
  "error": null
}
```

## 常用表单字段

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `file` | 必填 | 上传 PDF、Office 或 MinerU 支持的文件类型；Markdown/TXT 不走 MinerU。 |
| `tier` | `standard` | MinerU 拆解质量：`flash`、`basic`、`standard`、`advanced`；不传固定 standard，非法值返回 422。入队后保留所选档位。 |
| `priority` | `normal` | `urgent` 进入 `queue_urgent`，其他值进入 `queue_normal`。 |
| `return_txt` | `false` | 是否返回拼接后的纯文本 `txt`。 |
| `chunk_type` | `false` | 是否保留 `type` 字段，例如 `title`、`header`、`footer`、`image`。 |
| `provider` | 空 | 可选视觉 provider；通常不传，使用 `.env` 默认值。 |
| `model` | 空 | 可选视觉模型；通常不传，使用 `.env` 默认值。 |
| `prompt` | 空 | 可选视觉提示词覆盖。 |
| `save_to_minio` | `false` | 是否上传源 PDF、解析 JSON、逐页图片到 MinIO。 |
| `minio_address` | 空 | MinIO 地址，仅 `save_to_minio=true` 时需要。 |
| `minio_access_key` | 空 | MinIO access key。 |
| `minio_secret_key` | 空 | MinIO secret key。 |
| `minio_bucket` | 空 | 目标 bucket。 |
| `minio_prefix` | 自动生成 | MinIO 对象前缀，默认 `mineru/<filename>`。 |
| `minio_meta` | 空 | 可选元信息，会写入同目录 `meta.txt`。 |

MinIO 示例：

```bash
curl -sS -X POST "$API_BASE/mineru_with_images/task" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/path/to/report.pdf" \
  -F "return_txt=true" \
  -F "save_to_minio=true" \
  -F "minio_address=http://127.0.0.1:9000" \
  -F "minio_access_key=<access_key>" \
  -F "minio_secret_key=<secret_key>" \
  -F "minio_bucket=<bucket>" \
  -F "minio_prefix=mineru/demo/report" \
  -F "minio_meta=source=manual-test"
```

## 常见问题

### 任务一直 PENDING

按顺序检查：

```bash
redis-cli -n 0 ping
redis-cli -n 0 llen queue_normal
redis-cli -n 0 llen queue_urgent
uv run celery -A src.services.celery_app inspect active_queues --timeout=5
pm2 status
```

常见原因：

- 没启动 `ecosystem.celery.json`。
- worker 只监听了 `queue_parse_gpu`，这是 two-stage 队列，不能消费本接口任务。
- API 和 worker 使用了不同的 `CELERY_BROKER_URL` 或 Redis DB。
- API 和 worker 不共享 `MINERU_TASK_STORAGE_DIR`，worker 拿到的文件路径不存在。
- 结果已超过 `CELERY_RESULT_EXPIRES`，查询时看起来像未知任务。

### queue_normal 有积压但 worker 不处理

确认 worker 监听队列包含 `queue_normal`：

```bash
uv run celery -A src.services.celery_app inspect active_queues --timeout=5
```

如果没有，重启普通 worker：

```bash
pm2 restart ecosystem.celery.json
```

或手动启动监听：

```bash
uv run celery -A src.services.celery_app worker \
  -l info -Q queue_urgent,queue_normal,default \
  -P solo -c 1 --prefetch-multiplier=1
```

### 任务 FAILURE

查询结果会返回 `error`。再看 worker 日志：

```bash
pm2 logs celery-worker --lines 200
```

常见原因：

- `MINERU_DEFAULT_BACKEND` 配置错误。
- MinerU vLLM server 不可达，例如 `MINERU_VLLM_SERVER_URLS` 指向错误。
- 视觉 vLLM 服务不可达，例如 `VLLM_BASE_URLS` 未配置或服务异常。
- Office 转 PDF 失败，通常和 LibreOffice 或文件格式有关。
- GPU 显存不足或任务超过 MinerU hard timeout。

### 清理队列和临时文件

只在确认没有正在处理的重要任务时执行。

清空普通 MinerU Celery 队列：

```bash
uv run celery -A src.services.celery_app purge -f -Q queue_urgent,queue_normal,default
```

清理临时任务目录：

```bash
rm -rf /tmp/tiangong_mineru_tasks/*
```

如果部署中修改了 `MINERU_TASK_STORAGE_DIR`，请清理对应目录。

## 和 /two_stage/task 的区别

| 接口 | Celery app | 主要 task | normal 队列 | urgent 队列 |
| --- | --- | --- | --- | --- |
| `/mineru_with_images/task` | `src.services.celery_app` | `mineru.parse_images` | `queue_normal` | `queue_urgent` |
| `/two_stage/task` | `src.services.two_stage_pipeline` | `two_stage.parse` 等 | `queue_parse_gpu`、`queue_vision`、`queue_dispatch`、`default` | `queue_parse_urgent`、`queue_vision_urgent`、`queue_dispatch_urgent`、`queue_merge_urgent` |

如果要用 `/mineru_with_images/task`，启动 `ecosystem.celery.json`。如果只启动 `ecosystem.two_stage.celery.json`，本接口的任务不会被消费。
