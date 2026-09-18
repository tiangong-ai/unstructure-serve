# 两段式 MinerU + 视觉任务

入口为 `POST /two_stage/task`、`GET /two_stage/task/{task_id}` 和 `GET /two_stage/queue_status`，使用 Celery app `src.services.two_stage_pipeline`。当前主机部署这套任务系统；它与[普通异步任务](mineru_with_images_task_usage.md)分别使用自己的任务和队列。

处理顺序：保存文件 → MinerU parse → dispatch 分发图片 → vision 并行识别 → merge 按原阅读顺序回填并清理工作区。模型和环境准备见[部署说明](mineru_4_upgrade_usage.md)。

## 队列与配置

`.env.example` 和 PM2 模板约定的队列如下：

| 阶段 | normal | urgent | PM2 进程 / 池 |
| --- | --- | --- | --- |
| parse | `queue_parse_gpu` | `queue_parse_urgent` | `celery-two-stage-parse`、`-2`、`-3` / 各 solo 1 |
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
pm2 start deploy/pm2/ecosystem.two_stage.celery.json
uv run celery -A src.services.two_stage_pipeline inspect active_queues --timeout=5
pm2 status
```

图片任务默认提示词优先提取图中事实，避免重复 caption；固定前缀清理不代替内容校验。空响应或截断的 OpenAI-compatible 输出会导致失败。采样和实测见[图片描述优化](mineru_4_upgrade_usage.md#图片描述优化)。`VISION_BATCH_SIZE` 只影响同步/普通图片任务，two-stage 的图片并发由 vision worker 的 `-c 32` 控制。

六个 worker（parse 三个，其余阶段各一个）均需在线；模板的 `-Q` 顺序为 urgent 在前。三个 parse 使用不同 Celery 节点名，共同消费同一队列，每个最多执行一份文档，prefetch=1。每个解析进程的 VLM 请求并发为 8，由单地址后端分配至三张卡；worker 与 GPU 没有一一绑定关系。parse 池不能使用 daemonic prefork，因为 SDK 还需要创建渲染子进程。dispatch 使用 `self.replace` 触发 chord，不在任务内同步等待 `result.get()`；Redis result backend 必须可用。

parse 的 PM2 停止窗口为 1900 秒，让在途任务完成；这不是任务 hard timeout，维护前仍需等待 active/reserved 清空。API 和 parse 模板的 SDK 窗口均为 64 页，整本结果仍统一后处理。并发基准和配置选择见[部署记录](mineru_4_upgrade_usage.md#重启修复与并发优化2026-09-18)。

需要监控时启动 `deploy/pm2/ecosystem.two_stage.flower.json`，默认 5555；普通 Celery Flower 使用另一个 app，同机同时开两个需改端口。

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
| `file` | 必填；支持类型见 [README](../README.md#解析接口)，Office 先转 PDF |
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

**400–1000 页 PDF 不直接套用默认 6 个在途/800 秒等待。** 先按 [AI 指南的大文件流程](ai-integration.md#53-多份-4001000-页-pdf-的投递流程)做整本单文件验收，核对服务端长任务确认期限与资源，再小窗口投递。延长客户端等待不会改变 Redis 的消息重投或服务端执行期限。

`src/scripts/two_stage_enqueue.py` 读取目录中的 PDF，默认维持最多 6 个在途任务，有任务完成就立即补位，成功结果原子保存为 `<stem>.pkl`，跳过已有同名输出。队列可以持续保持三个解析 worker 有活可做，不需要一次上传全部文件。

任务 ID、文件 SHA-256 和请求参数写入输出目录 `.tasks/`。重启脚本时续查已有任务；待完成文件或参数变化时要求换输出目录。单输出目录有文件锁，避免多个脚本重复写入。已有 `.pkl` 仍按文件名跳过，更换输入内容或解析参数应使用新输出目录。

只有服务端确认 FAILURE/REVOKED 才最多尝试 3 次。查询网络错误继续查询相同 ID；本地等待超时保留 ID 并停止，不取消或重新上传。POST 响应丢失会保留 SUBMITTING，明确提示提交结果未知，需要核查服务端后再处理；这不是服务端幂等保证，不能直接删除状态并重投。

```bash
TWO_STAGE_BASE=http://127.0.0.1:7770 \
TWO_STAGE_INPUT_DIR=/path/to/pdfs \
TWO_STAGE_OUTPUT_DIR=/path/to/pickle \
TWO_STAGE_PRIORITY=normal TWO_STAGE_MAX_IN_FLIGHT=6 \
TWO_STAGE_CHUNK_TYPE=true TWO_STAGE_RETURN_TXT=true \
uv run python src/scripts/two_stage_enqueue.py
```

| 环境变量 | 脚本缺省值 / 作用 |
| --- | --- |
| `TWO_STAGE_BASE` | `http://localhost:8770`；兼容 `MINERU_TASK_BASE`。当前生产 API 需显式设为 7770 |
| `FASTAPI_BEARER_TOKEN` | 先读进程环境 / `.env`，为空时回退本地 `.secrets/secrets.toml` 的 `FASTAPI.BEARER_TOKEN`；两者都没有则报错 |
| `TWO_STAGE_INPUT_DIR` / `TWO_STAGE_OUTPUT_DIR` | `pdfs` / `pickle`；兼容 `ESG_INPUT_DIR` / `ESG_OUTPUT_DIR` |
| `TWO_STAGE_PRIORITY` | `normal` |
| `TWO_STAGE_CHUNK_TYPE` / `TWO_STAGE_RETURN_TXT` | `false` / `false` |
| `TWO_STAGE_MAX_IN_FLIGHT` | `6`；本脚本的在途窗口，不是服务器全局限流；恢复时已提交任务可能超过新设上限 |
| `TWO_STAGE_POLL_INTERVAL` / `TWO_STAGE_POLL_TIMEOUT` | `1` 秒 / `800` 秒；每次提交/恢复后起算，包含 PENDING；本地超时保留 ID，下次运行重新给等待窗口 |
| `VISION_PROVIDER` / `VISION_MODEL` / `VISION_PROMPT` | 可选，原样提交给 API 校验 |

脚本尚无 tier 环境开关，提交不带 tier，因此使用 HTTP 默认 `advanced`；选择其他档位请直接调用接口。转换结果可用 `uv run python src/scripts/read_pickle.py pickle/example.pkl --field result`，命令不带路径时选择 `pickle` 下最新文件。

## 排查

- 长期 PENDING：检查队列映射、四类阶段的六个 worker、Redis DB 和共享工作区；普通 worker 不负责 two-stage 队列。
- parse 后无结果：查看 `celery-two-stage-dispatch`、`celery-two-stage-vision`、`celery-two-stage-merge` 日志及 result backend；不能仅启动 parse/vision。
- vision 积压：检查独立 `VLLM_BASE_URLS` 服务吞吐与错误，再调整 vision 并发；增加应用并发不会扩大模型容量。
- 422：检查 tier、priority、provider/model 枚举及字段类型。
- FAILURE：查看返回 error，检查 `MINERU_MODEL_VLM_SERVER_URL`、CPU 模型、生成的 MiddleJson/图片、LibreOffice 和 hard timeout。
- 停机或清理前使用 `inspect active` / `inspect reserved` 核对当前任务；只处理确认结束的具体任务目录，避免删除普通任务共用的工作区。
