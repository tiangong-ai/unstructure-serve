# 普通 MinerU 异步任务

本文件覆盖 `POST /mineru/task` 和 `POST /mineru_with_images/task`；后者额外进行图片视觉识别。两者使用 Celery app `src.services.celery_app`，分别运行 `mineru.parse` / `mineru.parse_images`，通过同路径的 `GET .../{task_id}` 查询结果。

环境、模型和 Docker 准备见[部署说明](mineru_4_upgrade_usage.md)。当前主机默认运行 two-stage worker；使用本文件的接口需另行启动普通 worker。

## Worker 与队列

| priority | 队列 | 配置 |
| --- | --- | --- |
| `urgent` | `queue_urgent` | `CELERY_TASK_URGENT_QUEUE` |
| 其他值或不传 | `queue_normal` | `CELERY_TASK_MINERU_QUEUE` |

普通 worker 模板还监听 `default`。它与 [two-stage](two_stage_task_usage.md) 的解析、视觉和调度队列不同。API 与 worker 必须共享 broker、result backend，以及同一路径下的 `MINERU_TASK_STORAGE_DIR`。

在仓库根目录启动：

```bash
pm2 start ecosystem.celery.json
uv run celery -A src.services.celery_app inspect active_queues --timeout=5
pm2 logs celery-worker --lines 100
```

模板使用 `-P threads -c 16 --prefetch-multiplier=1`。手动单任务启动方式：

```bash
uv run celery -A src.services.celery_app worker \
  -l info -Q queue_urgent,queue_normal,default \
  -P solo -c 1 --prefetch-multiplier=1
```

两者选择一种。解析会再创建子进程，不使用 prefork 池。若启用监控，使用 `ecosystem.celery.flower.json`；默认 5555，与 two-stage Flower 同时运行需改端口。

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
| `save_to_minio` | form | `false`；上传 PDF、JSON、逐页 JPEG |
| `minio_address` / `minio_access_key` / `minio_secret_key` / `minio_bucket` | form | MinIO 连接、凭证和目标桶 |
| `minio_prefix` | form | 默认 `mineru/<文件名>`，可覆盖 |
| `minio_meta` | form | 保存为 `meta.txt`；未启用 MinIO 时忽略 |

图片接口对未知 provider/model 宽松接收，由视觉服务回退到配置的选择；视觉请求异常则任务失败，不用 caption/base_text 掩盖失败。这里的 `tier` 只控制 MinerU 拆解。图片请求采用 `VISION_BATCH_SIZE` 控制的滚动窗口（缺省 3），完成一张即补下一张，结果保持原位；每个文档的窗口会叠加。默认提示词、采样与截断/空响应处理见[图片描述优化](mineru_4_upgrade_usage.md#图片描述优化)。

## 提交和查询

shell 中先设置实际的 `FASTAPI_BEARER_TOKEN`；curl 不会自动读取项目 `.env`。不启用 API 鉴权时可省略 Authorization 头。

```bash
API_BASE=http://127.0.0.1:7770
curl --fail-with-body "$API_BASE/mineru_with_images/task?chunk_type=true&return_txt=true" \
  -H "Authorization: Bearer $FASTAPI_BEARER_TOKEN" \
  -F 'file=@input/p2.pdf' \
  -F 'tier=advanced' \
  -F 'priority=normal'
```

只要 MinerU 文本解析时，将路径改为 `/mineru/task`。响应包含任务 ID，例如：

```json
{"task_id": "example-task-id", "state": "PENDING"}
```

```bash
TASK_ID=example-task-id
curl --fail-with-body "$API_BASE/mineru_with_images/task/$TASK_ID?pretty=true" \
  -H "Authorization: Bearer $FASTAPI_BEARER_TOKEN"
```

状态包括 `PENDING`、`STARTED`、`SUCCESS`、`FAILURE`、`REVOKED`。`PENDING` 也可能表示未知或已过期 ID，不能单靠它判断任务正在排队。

```json
{
  "task_id": "example-task-id",
  "state": "SUCCESS",
  "result": {
    "result": [{"text": "解析文本", "page_number": 1, "type": "title"}],
    "txt": "解析文本\n\n"
  }
}
```

普通接口会省略值为 null 的字段：未启用的 txt/minio_assets、成功时的 error、没有标签的块 type 都可能不存在。`chunk_type=true` 不保证每个正文或表格块有 type。任务 FAILURE/REVOKED 查询返回 HTTP 500，并在 JSON 中保留 task_id/state/error。图片任务不启用同步接口特有的原生 DOCX TXT 分支。

启用 MinIO 时在提交请求增加以下表单字段（变量须由调用方填写）：

```bash
curl --fail-with-body "$API_BASE/mineru/task?chunk_type=true&return_txt=true" \
  -H "Authorization: Bearer $FASTAPI_BEARER_TOKEN" \
  -F 'file=@input/p2.pdf' -F 'tier=advanced' -F 'save_to_minio=true' \
  -F "minio_address=$MINIO_ADDRESS" -F "minio_access_key=$MINIO_ACCESS_KEY" \
  -F "minio_secret_key=$MINIO_SECRET_KEY" -F "minio_bucket=$MINIO_BUCKET" \
  -F 'minio_prefix=mineru/demo/p2' -F 'minio_meta=source=manual-test'
```

MinIO 的 `parsed.json` 保留服务输出；`chunk_type=true` 时保留类型，响应 `minio_assets` 返回对象摘要。Office 输入上传转换后的 `source.pdf`。

## 排查

- 一直 PENDING：检查 `celery-worker` 是否在线、`active_queues` 是否包含 `queue_urgent,queue_normal,default`、API/worker 是否使用同一个 Redis DB；仅有 two-stage worker 不会消费这些任务。
- 任务 FAILURE：查看返回 `error` 和 `pm2 logs celery-worker`。重点检查 Docker MinerU 端点 `MINERU_MODEL_VLM_SERVER_URL`、独立视觉端点 `VLLM_BASE_URLS`、CPU 模型文件、LibreOffice、共享任务目录和任务超时。
- 入队前 422：检查 tier 和请求字段类型；普通图片接口不会因为未知 provider/model 直接 422。
- 调整 worker 前查看 `inspect active`、`inspect reserved` 和 `inspect active_queues`；上传工作区需等任务完成后按具体任务清理。不要把整个共享临时目录当作普通任务的独占目录。

旧 `src/scripts/enqueue_input.py` 是优先级演示，会把每个文件分别投到 normal/urgent 两次，且默认指向开发端口 8770；不要将它当作本机生产批处理入口。需要批处理和结果落盘时使用 [two-stage 批量脚本](two_stage_task_usage.md#批量脚本)。
