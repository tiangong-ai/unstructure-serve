# 两段式 MinerU + 视觉任务

入口为 `POST /two_stage/task`、`GET /two_stage/task/{task_id}` 和 `GET /two_stage/queue_status`，使用 Celery app `src.services.two_stage_pipeline`。当前主机部署这套任务系统；它与[普通异步任务](mineru_with_images_task_usage.md)分别使用自己的任务和队列。

处理顺序：保存文件 → MinerU parse → dispatch 分发图片 → vision 并行识别 → merge 按原阅读顺序回填并清理工作区。模型和环境准备见[部署说明](mineru_4_upgrade_usage.md)。

## 队列与配置

`.env.example` 和 PM2 模板约定的队列如下：

| 阶段 | normal | urgent | PM2 进程 / 池 |
| --- | --- | --- | --- |
| parse | `queue_parse_gpu` | `queue_parse_urgent` | `celery-two-stage-parse` / solo |
| vision | `queue_vision` | `queue_vision_urgent` | `celery-two-stage-vision` / threads 32 |
| dispatch | `queue_dispatch` | `queue_dispatch_urgent` | `celery-two-stage-dispatch` / threads 4 |
| merge | `default` | `queue_merge_urgent` | `celery-two-stage-merge` / threads 4 |

API 和全部 worker 都需要相同的 broker、result backend、绝对工作区路径及队列设置：

```dotenv
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/0
CELERY_TASK_PARSE_QUEUE=queue_parse_gpu
CELERY_TASK_VISION_QUEUE=queue_vision
CELERY_TASK_DISPATCH_QUEUE=queue_dispatch
CELERY_TASK_MERGE_QUEUE=default
```

对应 urgent 环境变量为 `CELERY_TASK_PARSE_URGENT_QUEUE`、`CELERY_TASK_VISION_URGENT_QUEUE`、`CELERY_TASK_DISPATCH_URGENT_QUEUE`、`CELERY_TASK_MERGE_URGENT_QUEUE`，默认值就是上表的 urgent 列。

上表是**部署约定**。未显式设置时，代码中 parse 回退到 `CELERY_TASK_MINERU_QUEUE`（通常 `queue_normal`），dispatch/merge 回退到 `CELERY_TASK_DEFAULT_QUEUE`（通常 `default`）；它们不会自动使用 `queue_parse_gpu` / `queue_dispatch`。只配置 worker 的 `-Q` 而未配置 API，会把任务投到无人消费的队列。

检查当前进程解析出的配置：

```bash
uv run python - <<'PY'
from src.services.two_stage_pipeline import resolve_two_stage_queues
print("normal:", resolve_two_stage_queues("normal"))
print("urgent:", resolve_two_stage_queues("urgent"))
PY
```

## 启动和监控

```bash
pm2 start ecosystem.two_stage.celery.json
uv run celery -A src.services.two_stage_pipeline inspect active_queues --timeout=5
pm2 status
```

四个 worker 均需在线；模板的 `-Q` 顺序为 urgent 在前。parse 池不能使用 daemonic prefork，因为应用调度器需要再创建子进程。dispatch 使用 `self.replace` 触发 chord，不在任务内同步等待 `result.get()`；Redis result backend 必须可用。

需要监控时启动 `ecosystem.two_stage.flower.json`，默认 5555；普通 Celery Flower 使用另一个 app，同机同时开两个需改端口。

```bash
API_BASE=http://127.0.0.1:7770
curl --fail-with-body "$API_BASE/two_stage/queue_status" \
  -H "Authorization: Bearer $FASTAPI_BEARER_TOKEN"
pm2 logs celery-two-stage-parse --lines 100
```

`queue_status` 在 Redis broker 下返回 `queues`（ready）及 `unacked`（已取走未确认）的逐队列计数，适合上游背压；不能当作每个任务的最终状态。API/worker 跨容器时，共享 `MINERU_TASK_STORAGE_DIR` 且容器内路径一致。

## 请求字段

本接口以下字段**全部为 multipart 表单字段**；与普通接口的 query 开关不同。

| 字段 | 默认值 / 含义 |
| --- | --- |
| `file` | 必填；支持类型见 [README](README.md#解析接口)，Office 先转 PDF |
| `tier` | `advanced`；`flash/basic/standard/advanced`，非法值返回 422，任务保留提交时选择 |
| `chunk_type` | `false`；保留标题、页眉、页脚和图片类型及阅读顺序 |
| `return_txt` | `false`；返回拼接纯文本 |
| `priority` | `normal`；仅 `normal/urgent`，urgent 将四个阶段全部路由到 urgent 队列 |
| `provider` / `model` | 可选，通常使用服务配置；未知值会在此接口返回 422 |
| `prompt` | 可选图片提示词；空白串视为未设置 |

此接口不提供 MinIO 参数，最终 `minio_assets` 为空；也不启用同步 DOCX 的原生 TXT-only 分支。图片会按面积、分辨率、体积、长宽比、同页数量及哈希去重筛选，只有保留的图片进入视觉阶段。任一实际视觉请求失败会使任务失败，不用 caption/base_text 降级。

## 提交和结果

shell 中先设置实际 `FASTAPI_BEARER_TOKEN`；Python 会读取 `.env`，curl 不会自动读取。关闭 API 鉴权时可省略 Authorization。

```bash
API_BASE=http://127.0.0.1:7770
curl --fail-with-body "$API_BASE/two_stage/task" \
  -H "Authorization: Bearer $FASTAPI_BEARER_TOKEN" \
  -F 'file=@input/p2.pdf' -F 'tier=advanced' \
  -F 'chunk_type=true' -F 'return_txt=true' -F 'priority=normal'
```

返回 `task_id` 和初始 `state` 后轮询：

```bash
TASK_ID=example-task-id
curl --fail-with-body "$API_BASE/two_stage/task/$TASK_ID" \
  -H "Authorization: Bearer $FASTAPI_BEARER_TOKEN"
```

`PENDING` 可能是未开始、未知或已过期 ID；`STARTED` 表示某阶段已开始，`SUCCESS` 返回结果，`FAILURE` 返回错误，另可能出现 `REVOKED`。成功结构：

```json
{
  "task_id": "example-task-id",
  "state": "SUCCESS",
  "result": {
    "result": [{"text": "解析文本或图片识别内容", "page_number": 1, "type": "image"}],
    "txt": "可选纯文本输出",
    "minio_assets": null
  }
}
```

成功结果中的未设置字段可为 null，普通正文/表格不保证有非空 type。任务 FAILURE/REVOKED 查询使用 HTTP 200 返回带 error 的状态体；入队或查询基础设施错误仍可返回 4xx/5xx，调用方需同时检查 HTTP 状态与任务 state。

## 批量脚本

`src/scripts/two_stage_enqueue.py` 读取目录中的 PDF，按每批最多 5000 个任务提交并轮询，成功后把返回的 `result` 保存为 `<stem>.pkl`，跳过已有同名输出。失败/运行超时最多尝试 3 次（首次加 2 次重试）；它不取消之前的服务端任务，重试可能与原任务重叠。

```bash
TWO_STAGE_BASE=http://127.0.0.1:7770 \
TWO_STAGE_INPUT_DIR=/path/to/pdfs \
TWO_STAGE_OUTPUT_DIR=/path/to/pickle \
TWO_STAGE_PRIORITY=normal \
TWO_STAGE_CHUNK_TYPE=true TWO_STAGE_RETURN_TXT=true \
uv run python src/scripts/two_stage_enqueue.py
```

| 环境变量 | 脚本缺省值 / 作用 |
| --- | --- |
| `TWO_STAGE_BASE` | `http://localhost:8770`；兼容 `MINERU_TASK_BASE`。当前生产 API 需显式设为 7770 |
| `FASTAPI_BEARER_TOKEN` | 必填，脚本会读取 `.env` |
| `TWO_STAGE_INPUT_DIR` / `TWO_STAGE_OUTPUT_DIR` | `pdfs` / `pickle`；兼容 `ESG_INPUT_DIR` / `ESG_OUTPUT_DIR` |
| `TWO_STAGE_PRIORITY` | `normal` |
| `TWO_STAGE_CHUNK_TYPE` / `TWO_STAGE_RETURN_TXT` | `false` / `false` |
| `TWO_STAGE_POLL_INTERVAL` / `TWO_STAGE_POLL_TIMEOUT` | `3` 秒 / `800` 秒；超时从观察到 STARTED 起算，不包含一直 PENDING 的时间 |
| `VISION_PROVIDER` / `VISION_MODEL` / `VISION_PROMPT` | 可选，原样提交给 API 校验 |

脚本尚无 tier 环境开关，提交不带 tier，因此使用 HTTP 默认 `advanced`；选择其他档位请直接调用接口。转换结果可用 `uv run python src/scripts/read_pickle.py pickle/example.pkl --field result`，命令不带路径时选择 `pickle` 下最新文件。

## 排查

- 长期 PENDING：检查队列映射、四个 worker、Redis DB 和共享工作区；普通 worker 不负责 two-stage 队列。
- parse 后无结果：查看 `celery-two-stage-dispatch`、`celery-two-stage-vision`、`celery-two-stage-merge` 日志及 result backend；不能仅启动 parse/vision。
- vision 积压：检查独立 `VLLM_BASE_URLS` 服务吞吐与错误，再调整 vision 并发；增加应用并发不会扩大模型容量。
- 422：检查 tier、priority、provider/model 枚举及字段类型。
- FAILURE：查看返回 error，检查 `MINERU_MODEL_VLM_SERVER_URL`、CPU 模型、生成的 MiddleJson/图片、LibreOffice 和 hard timeout。
- 停机或清理前使用 `inspect active` / `inspect reserved` 核对当前任务；只处理确认结束的具体任务目录，避免删除普通任务共用的工作区。
