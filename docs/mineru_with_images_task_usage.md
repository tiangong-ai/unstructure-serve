---
lastReviewedAt: 2026-10-31
lastReviewedCommit: 47251623248cb14e45ee3cfe87fbb6647c4e5612
docType: runbook
scope: repo
status: current
authoritative: true
owner: unstructure-serve
language: zh-CN
whenToUse: "When submitting, operating or recovering ordinary asynchronous parsing jobs."
whenToUpdate: "When ordinary queue routing, request fields, task states or recovery behavior change."
checkPaths:
  - src/routers/mineru_task_router.py
  - src/routers/mineru_with_images_task_router.py
  - src/services/tasks/mineru_tasks.py
  - src/services/durable_pipeline.py
---

# 普通 MinerU 异步任务

> 新批次使用[统一批量客户端](batch-processing.md)：`--mode parse` 为普通纯解析，`--mode images` 为普通图片增强，`--mode two-stage` 为分阶段图片增强。默认在途 2，JSON 落盘并保存任务记录；旧脚本仅保留兼容续跑。

新批次以识别质量为优先，默认及推荐调用使用 `advanced`；`standard` 只作为代表性业务样本质量验收通过后的可选档位，四档均保留。调用示例和验收要求见 [AI 接入指南](ai-integration.md#31-推荐档位与调用方式)。改变已有批次档位时使用新输出目录，旧任务按原参数续查。

本文件覆盖 `POST /mineru/task` 和 `POST /mineru_with_images/task`；后者额外进行图片视觉识别。两者先由 `src.services.shared_parse_app` 执行公共解析，再由普通 Celery app `src.services.celery_app` 执行 `mineru.finish_durable`，组装结果或增强图片。通过同路径的 `GET .../{task_id}` 查询结果。

两个 POST 均支持 `Idempotency-Key`。新任务保存原始输入、完成的整本解析和单图结果；发布结果不明仍返回可恢复 ID，不删除文件。可用统一 `/tasks/{id}/status`、`/result`、`/resume` 做轻量查询、下载与显式阶段恢复，合同见 [AI 指南 §5.4](ai-integration.md#54-提交幂等轻量查询与阶段恢复)。普通失败查询仍为 HTTP 500 状态体，成功结果继续省略 null。

环境、模型和 Docker 准备见[部署说明](mineru_4_upgrade_usage.md)。使用本文件接口时需启动公共解析和普通后续 worker。

恢复阶段要求执行 profile 一致，其中包括 SDK/DocVortex 版本和 ONNX 设备。4.1.4 应用检查点不能直接在 4.2.3 环境 resume，即使仍使用 CPU 也不能强行复用旧 SDK 的阶段结果；需要续算旧任务时由运维恢复原代码、锁文件和配置，或明确创建新任务。客户端保留已有 ID，不通过重复上传或编辑检查点绕过这个边界。

## Worker 与队列

| priority | 公共解析 `queue_parse_shared` | 普通后续 |
| --- | --- | --- |
| `urgent`（不区分大小写） | 消息优先级 0 | `CELERY_TASK_URGENT_QUEUE`（默认 `queue_urgent`） |
| 其他值或不传 | 消息优先级 9 | `CELERY_TASK_MINERU_QUEUE`（默认 `queue_normal`） |

三个异步入口的 urgent 都在同节点公共解析 worker 空闲领取时优先，已运行的任务继续执行。普通后续仅消费自己的两个队列；新任务缺少有效的解析检查点时失败，由恢复流程重新经过公共解析。普通 worker 仍注册旧 `mineru.parse` 和 `mineru.parse_images` 以处理旧消息。同节点的 API 与 worker 共用 broker、result backend，以及同路径的 `MINERU_JOB_STORE_DIR`；旧任务另需原 `MINERU_TASK_STORAGE_DIR`。

在仓库根目录启动：

```bash
./deploy/manage.sh start app
./deploy/manage.sh start ordinary
uv run celery -A src.services.shared_parse_app:celery_app inspect active_queues --timeout=5
uv run celery -A src.services.celery_app inspect active_queues --timeout=5
pm2 logs celery-worker --lines 100
```

模板使用 `-P threads -c 16 --prefetch-multiplier=1`。手动单任务启动方式：

```bash
uv run celery -A src.services.celery_app worker \
  -l info -Q queue_urgent,queue_normal \
  -P solo -c 1 --prefetch-multiplier=1
```

普通 worker 的两种启动方式选其一。公共解析另由 app/app4 启动，使用 solo 并创建隔离解析子进程。若启用监控，使用 `deploy/pm2/ecosystem.celery.flower.json`；默认 5555，与 two-stage Flower 同时运行需改端口。

## 请求参数

**`chunk_type` 和 `return_txt` 是查询参数，不是表单字段。** 其余字段的位置以以下表格和 `/docs` 为准。

| 字段 | 位置 | 默认值 / 含义 |
| --- | --- | --- |
| `file` | multipart form | 必填；PDF、受支持图片或 Office 转 PDF 格式 |
| `tier` | form | `advanced`；可选 `flash/basic/standard/advanced`，非法值 422，入队后保留 |
| `priority` | form | `normal`；不区分大小写的 `urgent` 在公共解析队列中优先领取 |
| `chunk_type` | query | `false`；保留标题、页眉、页脚类型；图片接口标注 image，保持阅读顺序 |
| `return_txt` | query | `false`；返回拼接纯文本 `txt` |
| `pretty` | query | `false`；格式化 JSON |
| `provider` / `model` / `prompt` | form，仅图片接口 | 可选图片描述设置；通常使用服务配置 |

- 一直 PENDING：按 task_id 检查发布及阶段状态，再核对公共解析 worker 是否订阅 `queue_parse_shared`，普通后续 worker 是否订阅 `queue_urgent,queue_normal`，以及 API 和 worker 的 Redis DB 与任务目录是否一致。
- 任务 FAILURE：查看返回 `error`，按解析或后续阶段查看 `celery-two-stage-parse` 等公共解析日志或 `celery-worker` 日志。重点检查 Docker MinerU 端点 `MINERU_MODEL_VLM_SERVER_URL`、独立视觉端点 `VLLM_BASE_URLS`、CPU 模型文件、LibreOffice、共享任务目录和任务超时。若备选耗尽后仅无可用内容，该图片 text 会包含 `[图片内容无法识别]`，任务继续；仍有连接、截断、请求或资产故障则须排查，修复后用原 ID 的 `/tasks/{id}/resume` 复用已完成检查点。
- 入队前 422：检查 tier 和请求字段类型；普通图片接口不会因为未知 provider/model 直接 422。
- 调整 worker 前查看 `inspect active`、`inspect reserved` 和 `inspect active_queues`；上传工作区需等任务完成后按具体任务清理。不要把整个共享临时目录当作普通任务的独占目录。

旧 `src/scripts/enqueue_input.py` 是优先级演示，会把每个文件分别投到 normal/urgent 两次，且默认指向开发端口 8770；不要将它当作本机生产批处理入口。需要批处理和结果落盘时使用 [统一批量客户端](batch-processing.md) 的 parse/images 模式。
