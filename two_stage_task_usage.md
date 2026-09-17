# /two_stage/task 使用说明

> MinerU 4 升级后，解析 worker 连接 Docker VLM 后端。先按 [部署与回归说明](mineru_4_upgrade_usage.md) 准备模型、`.env` 和容器；本文件中的 Celery 队列划分保持不变。

本文档面向能登录本机/容器的同事，说明如何启动并使用两段式 MinerU + 视觉异步接口：

```text
POST /two_stage/task
GET  /two_stage/task/{task_id}
GET  /two_stage/queue_status
```

这套接口使用独立 Celery 应用 `src.services.two_stage_pipeline`，把一次文档解析拆成四类任务：

```text
two_stage.parse     解析文档，只跑 MinerU，不调用视觉模型
two_stage.dispatch  根据解析结果拆分图片任务，并触发 chord
two_stage.vision    单张图片调用视觉模型，可高并发
two_stage.merge     汇总视觉结果，回填到解析内容并清理工作目录
```

它和 `/mineru_with_images/task` 不是同一套队列：

```text
/two_stage/task             -> queue_parse_gpu / queue_vision / queue_dispatch / default
/mineru_with_images/task    -> queue_normal / queue_urgent
```

## 运行链路

```text
调用方
  -> FastAPI /two_stage/task
  -> 保存上传文件到 MINERU_TASK_STORAGE_DIR
  -> parse worker: MinerU 解析，产出 content_list 和图片任务
  -> dispatch worker: fan-out 到多个 vision task
  -> vision worker: 并发调用视觉模型
  -> merge worker: 合并结果，清理临时目录
  -> 调用方轮询 /two_stage/task/{task_id}
```

关键点：

- API 进程和所有 two-stage worker 必须使用同一个 `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND`。
- API 会把上传文件路径写入任务参数；如果 API 和 worker 在不同容器，必须挂载同一个 `MINERU_TASK_STORAGE_DIR`，并保证容器内路径一致。
- `priority=urgent` 会把 parse/vision/dispatch/merge 全部路由到 urgent 队列。
- `two_stage.dispatch` 使用 Celery `chord`，因此必须配置可用的 result backend，推荐与 broker 同一个 Redis。
- dispatch 和 merge 必须由不同 worker 或至少不同监听队列承接；当前 PM2 模板已经拆开，避免 dispatch 阻塞 merge。
- 当前 `/two_stage/task` 不支持 `save_to_minio` 相关表单字段，结果中的 `minio_assets` 固定为空。

## 队列映射

当前推荐队列如下：

| 阶段 | normal 队列 | urgent 队列 | 推荐 worker |
| --- | --- | --- | --- |
| parse | `queue_parse_gpu` | `queue_parse_urgent` | `celery-two-stage-parse` |
| vision | `queue_vision` | `queue_vision_urgent` | `celery-two-stage-vision` |
| dispatch | `queue_dispatch` | `queue_dispatch_urgent` | `celery-two-stage-dispatch` |
| merge | `default` | `queue_merge_urgent` | `celery-two-stage-merge` |

对应环境变量：

| 环境变量 | 默认/推荐值 | 说明 |
| --- | --- | --- |
| `CELERY_TASK_PARSE_QUEUE` | `queue_parse_gpu` | normal parse 队列 |
| `CELERY_TASK_PARSE_URGENT_QUEUE` | `queue_parse_urgent` | urgent parse 队列 |
| `CELERY_TASK_VISION_QUEUE` | `queue_vision` | normal vision 队列 |
| `CELERY_TASK_VISION_URGENT_QUEUE` | `queue_vision_urgent` | urgent vision 队列 |
| `CELERY_TASK_DISPATCH_QUEUE` | `queue_dispatch` | normal dispatch 队列 |
| `CELERY_TASK_DISPATCH_URGENT_QUEUE` | `queue_dispatch_urgent` | urgent dispatch 队列 |
| `CELERY_TASK_MERGE_QUEUE` | `default` | normal merge 队列 |
| `CELERY_TASK_MERGE_URGENT_QUEUE` | `queue_merge_urgent` | urgent merge 队列 |

API 进程和 worker 进程都要解析到同一组队列。可以用下面命令在当前环境里确认：

```bash
uv run python - <<'PY'
from src.services.two_stage_pipeline import resolve_two_stage_queues
print("normal =", resolve_two_stage_queues("normal"))
print("urgent =", resolve_two_stage_queues("urgent"))
PY
```

期望类似：

```text
normal = {'parse': 'queue_parse_gpu', 'vision': 'queue_vision', 'dispatch': 'queue_dispatch', 'merge': 'default'}
urgent = {'parse': 'queue_parse_urgent', 'vision': 'queue_vision_urgent', 'dispatch': 'queue_dispatch_urgent', 'merge': 'queue_merge_urgent'}
```

## 服务端启动

以下命令都在仓库根目录执行：

```bash
cd /home/david/projects/TianGong-AI-Unstructure-Serve
```

### 1. Redis

确认 Redis 可用：

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

如果 API/worker 在容器内，`localhost` 指的是容器自身；这种部署要把 `CELERY_BROKER_URL` 和 `CELERY_RESULT_BACKEND` 改成实际 Redis 地址。

### 2. API 服务

two-stage 路由已经挂在 `src.main`，随主 API 启动：

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

### 3. two-stage worker

PM2 模板启动全部 four-stage worker：

```bash
pm2 start ecosystem.two_stage.celery.json
```

该模板会启动四个进程：

```text
celery-two-stage-parse     -Q queue_parse_urgent,queue_parse_gpu
celery-two-stage-vision    -Q queue_vision_urgent,queue_vision
celery-two-stage-dispatch  -Q queue_dispatch_urgent,queue_dispatch
celery-two-stage-merge     -Q queue_merge_urgent,default
```

也可以手动启动：

```bash
CELERY_BROKER_URL=redis://localhost:6379/0 \
CELERY_RESULT_BACKEND=redis://localhost:6379/0 \
CELERY_TASK_PARSE_QUEUE=queue_parse_gpu \
uv run celery -A src.services.two_stage_pipeline worker \
  -n parse@%h -l info -Q queue_parse_urgent,queue_parse_gpu \
  -P solo --prefetch-multiplier=1
```

```bash
CELERY_BROKER_URL=redis://localhost:6379/0 \
CELERY_RESULT_BACKEND=redis://localhost:6379/0 \
CELERY_TASK_VISION_QUEUE=queue_vision \
uv run celery -A src.services.two_stage_pipeline worker \
  -n vision@%h -l info -Q queue_vision_urgent,queue_vision \
  -P threads -c 32 --prefetch-multiplier=1
```

```bash
CELERY_BROKER_URL=redis://localhost:6379/0 \
CELERY_RESULT_BACKEND=redis://localhost:6379/0 \
CELERY_TASK_DISPATCH_QUEUE=queue_dispatch \
uv run celery -A src.services.two_stage_pipeline worker \
  -n dispatch@%h -l info -Q queue_dispatch_urgent,queue_dispatch \
  -P threads -c 4 --prefetch-multiplier=1
```

```bash
CELERY_BROKER_URL=redis://localhost:6379/0 \
CELERY_RESULT_BACKEND=redis://localhost:6379/0 \
CELERY_TASK_MERGE_QUEUE=default \
uv run celery -A src.services.two_stage_pipeline worker \
  -n merge@%h -l info -Q queue_merge_urgent,default \
  -P threads -c 4 --prefetch-multiplier=1
```

并发建议：

- `parse` 通常是 GPU/MinerU 重任务，保守使用 `-P solo`，一次处理一个解析任务。
- `vision` 可以根据视觉服务吞吐调大或调小，模板是 `-P threads -c 32`。
- `dispatch` 和 `merge` 是轻任务，但需要保持在线；不要只启动 parse/vision。

### 4. Flower 监控

可选：

```bash
pm2 start ecosystem.two_stage.flower.json
```

或手动启动：

```bash
CELERY_BROKER_URL=redis://localhost:6379/0 \
CELERY_RESULT_BACKEND=redis://localhost:6379/0 \
uv run celery -A src.services.two_stage_pipeline flower --address=0.0.0.0 --port=5555
```

## 启动后检查

确认 worker 在线且监听正确队列：

```bash
uv run celery -A src.services.two_stage_pipeline inspect active_queues --timeout=5
```

期望能看到四类 worker：

```text
parse@...     queue_parse_urgent, queue_parse_gpu
vision@...    queue_vision_urgent, queue_vision
dispatch@...  queue_dispatch_urgent, queue_dispatch
merge@...     queue_merge_urgent, default
```

查看 two-stage 队列状态：

```bash
curl -sS http://127.0.0.1:7770/two_stage/queue_status
```

返回示例：

```json
{
  "broker": "redis",
  "queues": {
    "queue_parse_gpu": 0,
    "queue_vision": 0,
    "queue_dispatch": 0,
    "default": 0,
    "queue_parse_urgent": 0,
    "queue_vision_urgent": 0,
    "queue_dispatch_urgent": 0,
    "queue_merge_urgent": 0
  },
  "unacked": {
    "queue_parse_gpu": 0,
    "queue_vision": 0,
    "queue_dispatch": 0,
    "default": 0,
    "queue_parse_urgent": 0,
    "queue_vision_urgent": 0,
    "queue_dispatch_urgent": 0,
    "queue_merge_urgent": 0
  }
}
```

直接查 Redis：

```bash
redis-cli -n 0 llen queue_parse_urgent
redis-cli -n 0 llen queue_parse_gpu
redis-cli -n 0 llen queue_vision_urgent
redis-cli -n 0 llen queue_vision
redis-cli -n 0 llen queue_dispatch_urgent
redis-cli -n 0 llen queue_dispatch
redis-cli -n 0 llen queue_merge_urgent
redis-cli -n 0 llen default
redis-cli -n 0 hlen unacked
```

查看 PM2 日志：

```bash
pm2 logs celery-two-stage-parse --lines 100
pm2 logs celery-two-stage-vision --lines 100
pm2 logs celery-two-stage-dispatch --lines 100
pm2 logs celery-two-stage-merge --lines 100
pm2 logs unstructured-gunicorn --lines 100
```

## 调用接口

下面示例假设 API 地址为 `http://127.0.0.1:7770`。

无鉴权时：

```bash
API_BASE=http://127.0.0.1:7770

curl -sS -X POST "$API_BASE/two_stage/task" \
  -F "file=@/path/to/report.pdf" \
  -F "return_txt=true" \
  -F "chunk_type=true" \
  -F "priority=normal"
```

有 Bearer 鉴权时：

```bash
API_BASE=http://127.0.0.1:7770
TOKEN="<FASTAPI_BEARER_TOKEN>"

curl -sS -X POST "$API_BASE/two_stage/task" \
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

curl -sS "$API_BASE/two_stage/task/$TASK_ID"
```

可能状态：

```text
PENDING  已投递但还没开始，或 task_id 不在当前 result backend 中
STARTED  某个阶段已经开始
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
        "text": "文本或图片视觉识别结果",
        "page_number": 1,
        "type": "image"
      }
    ],
    "txt": "可选纯文本输出",
    "minio_assets": null
  }
}
```

## 常用表单字段

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `file` | 必填 | 上传 PDF、Office 或 MinerU 支持的文件类型；Markdown/TXT 不走 MinerU。 |
| `tier` | `standard` | MinerU 拆解质量：`flash`、`basic`、`standard`、`advanced`；不传固定 standard，非法值返回 422。入队后保留所选档位。 |
| `priority` | `normal` | `urgent` 进入四个 urgent 队列，其他值进入 normal 队列。 |
| `return_txt` | `false` | 是否返回拼接后的纯文本 `txt`。 |
| `chunk_type` | `false` | 是否保留 `type` 字段，例如 `title`、`header`、`footer`、`image`。 |
| `provider` | 空 | 可选视觉 provider；路由层会按枚举校验，通常不传。 |
| `model` | 空 | 可选视觉模型；路由层会按枚举校验，通常不传。 |
| `prompt` | 空 | 可选视觉提示词覆盖。 |

## 批量提交脚本

仓库内置批量脚本：

```bash
src/scripts/two_stage_enqueue.py
```

默认行为：

- 从 `pdfs/` 目录读取 PDF。
- 提交到 `TWO_STAGE_BASE`，未设置时默认 `http://localhost:8770`。
- 轮询 `/two_stage/task/{task_id}`。
- 成功后把响应中的 `result` 写入 `pickle/<stem>.pkl`。
- 单文件失败或超时会自动重试，最多 3 次。

示例：

```bash
FASTAPI_BEARER_TOKEN="<FASTAPI_BEARER_TOKEN>" \
TWO_STAGE_BASE="http://127.0.0.1:7770" \
TWO_STAGE_INPUT_DIR="/path/to/pdfs" \
TWO_STAGE_OUTPUT_DIR="/path/to/pickle" \
TWO_STAGE_PRIORITY="normal" \
TWO_STAGE_RETURN_TXT=true \
TWO_STAGE_CHUNK_TYPE=true \
uv run python src/scripts/two_stage_enqueue.py
```

可用环境变量：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TWO_STAGE_BASE` | `http://localhost:8770` | API 地址；也兼容 `MINERU_TASK_BASE`。 |
| `FASTAPI_BEARER_TOKEN` | 必填 | 脚本要求存在该变量，用于接口鉴权。 |
| `TWO_STAGE_INPUT_DIR` | `pdfs` | 输入 PDF 目录；也兼容 `ESG_INPUT_DIR`。 |
| `TWO_STAGE_OUTPUT_DIR` | `pickle` | 输出 pickle 目录；也兼容 `ESG_OUTPUT_DIR`。 |
| `TWO_STAGE_PRIORITY` | `normal` | `normal` 或 `urgent`。 |
| `TWO_STAGE_RETURN_TXT` | `false` | 是否提交 `return_txt=true`。 |
| `TWO_STAGE_CHUNK_TYPE` | `false` | 是否提交 `chunk_type=true`。 |
| `TWO_STAGE_POLL_INTERVAL` | `3` | 轮询间隔秒数。 |
| `TWO_STAGE_POLL_TIMEOUT` | `800` | 单任务进入 `STARTED` 后的超时秒数。 |
| `VISION_PROVIDER` | 空 | 可选透传到接口。 |
| `VISION_MODEL` | 空 | 可选透传到接口。 |
| `VISION_PROMPT` | 空 | 可选透传到接口。 |

## 常见问题

### 任务一直 PENDING

按顺序检查：

```bash
redis-cli -n 0 ping
curl -sS http://127.0.0.1:7770/two_stage/queue_status
uv run celery -A src.services.two_stage_pipeline inspect active_queues --timeout=5
pm2 status
```

常见原因：

- 没启动 `ecosystem.two_stage.celery.json`。
- 只启动了普通 `ecosystem.celery.json`，它监听 `queue_normal/queue_urgent`，不能消费 two-stage 任务。
- API 和 worker 使用了不同的 Redis 地址或 Redis DB。
- API 解析出的队列名和 worker 监听的队列名不一致。
- API 和 worker 不共享 `MINERU_TASK_STORAGE_DIR`，worker 拿到的文件路径不存在。
- 结果已超过 `CELERY_RESULT_EXPIRES`，查询时看起来像未知任务。

### parse 完成后一直没有最终结果

重点检查 dispatch/vision/merge 三类 worker：

```bash
uv run celery -A src.services.two_stage_pipeline inspect active_queues --timeout=5
pm2 logs celery-two-stage-dispatch --lines 200
pm2 logs celery-two-stage-vision --lines 200
pm2 logs celery-two-stage-merge --lines 200
```

常见原因：

- `celery-two-stage-dispatch` 未启动，parse 后没人 fan-out。
- `celery-two-stage-vision` 未启动，图片任务堆在 `queue_vision`。
- `celery-two-stage-merge` 未启动，chord 完成后没人合并。
- `CELERY_RESULT_BACKEND` 不可用，Celery chord 无法汇总。
- dispatch 和 merge 被放到同一个阻塞 worker，导致 chord merge 长时间不执行。

### vision 队列积压过多

先看队列：

```bash
redis-cli -n 0 llen queue_vision
redis-cli -n 0 llen queue_vision_urgent
```

处理方式：

- 适当提高 vision worker 并发，例如调整 `ecosystem.two_stage.celery.json` 里的 `-c 32`。
- 如果视觉服务或 vLLM 已经满载，降低 `-c`，避免请求大量失败。
- 检查 `VLLM_BASE_URLS` / `VLLM_BASE_URL` 是否可用。

### 任务 FAILURE

查询接口会返回 `error`。再看相关 worker 日志：

```bash
pm2 logs celery-two-stage-parse --lines 200
pm2 logs celery-two-stage-vision --lines 200
pm2 logs celery-two-stage-merge --lines 200
```

常见原因：

- MinerU 解析失败，或没有生成 `_content_list.json`。
- `MINERU_DEFAULT_BACKEND` / `MINERU_VLLM_SERVER_URLS` 配置错误。
- 视觉服务不可达，或 `provider` / `model` 不在枚举内。
- Office 转 PDF 失败，通常和 LibreOffice 或文件格式有关。
- GPU 显存不足或任务超过 MinerU hard timeout。

### 清理队列和临时文件

只在确认没有正在处理的重要任务时执行。

清空 two-stage 队列：

```bash
uv run celery -A src.services.two_stage_pipeline purge -f \
  -Q queue_parse_urgent,queue_parse_gpu,queue_vision_urgent,queue_vision,queue_dispatch_urgent,queue_dispatch,queue_merge_urgent,default
```

清理临时任务目录：

```bash
rm -rf /tmp/tiangong_mineru_tasks/*
```

如果部署中修改了 `MINERU_TASK_STORAGE_DIR`，请清理对应目录。

## 和 /mineru_with_images/task 的区别

| 接口 | Celery app | 工作方式 | normal 队列 | urgent 队列 |
| --- | --- | --- | --- | --- |
| `/two_stage/task` | `src.services.two_stage_pipeline` | MinerU parse 和图片 vision 拆成多阶段任务 | `queue_parse_gpu`、`queue_vision`、`queue_dispatch`、`default` | `queue_parse_urgent`、`queue_vision_urgent`、`queue_dispatch_urgent`、`queue_merge_urgent` |
| `/mineru_with_images/task` | `src.services.celery_app` | 单个 `mineru.parse_images` 任务内完成解析和视觉 | `queue_normal` | `queue_urgent` |

大吞吐、图片多、希望解析和视觉并行时优先用 `/two_stage/task`。只需要简单异步图像解析时，可以用 `/mineru_with_images/task`。
