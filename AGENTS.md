# TianGong AI Unstructure Serve 代理说明

仓库为 `tiangong-ai/unstructure-serve`。当前运行基线是 MinerU 4.0.0 + CPU ONNX 小模型 + Docker vLLM，应用依赖由 `uv.lock` 固定，部署使用 Python 3.12。API 与 worker 仍在应用环境运行，`.venv` 不安装 vLLM。

## 文档与修改约定

- **每次修改代码、配置或说明，都同步更新本文件涉及的规则或入口。**
- 当前操作以 [README](README.md)、[部署与回归](mineru_4_upgrade_usage.md)、[普通异步任务](mineru_with_images_task_usage.md)、[two-stage](two_stage_task_usage.md) 为准。
- [历史文档](docs/history/README.md)保留原版本的样本、评估和测试结果，不作为当前安装步骤。[多卡计划](multi_gpu_vllm_scaling_todolist.md)明确区分已实现与待验证能力。
- `.env`、`.secrets/`、输入文件、模型、结果、日志和回滚环境保持私有。公共配置骨架为 `deploy/secrets.example.toml`；不要在 PM2 模板写凭证或实际视觉服务地址。
- README 保持功能和入口简明；部署细节集中到部署说明，队列/字段细节集中到对应任务文档。例子中的默认值必须区分代码缺省、模板值和本机覆盖。

## 主要入口

| 文件 / 目录 | 职责 |
| --- | --- |
| `src/main.py` | FastAPI 路由、Bearer 鉴权、日志和 shutdown；退出时等待 scheduler 收敛 |
| `src/routers/` | 同步/异步解析、MinIO、Markdown、健康检查与队列状态 |
| `src/services/mineru_service_full.py` | MinerU 4 SDK 兼容层、保存资产、归一化 Content List V1 |
| `src/services/gpu_scheduler.py` | 排队、进程隔离、hard timeout、任务进程组收尾 |
| `src/services/mineru_with_images_service.py` | 图片描述并发及同步 DOCX TXT 增强 |
| `src/services/mineru_task_runner.py` / `tasks/mineru_tasks.py` | 普通 Celery 任务，复用 scheduler 与 MinIO 合同 |
| `src/services/two_stage_pipeline.py` | 独立 Celery app 的 parse/dispatch/vision/merge |
| `src/services/vision_service.py` / `vision_service_openai_compatible.py` | provider/model 兜底与 OpenAI-compatible 客户端池 |
| `src/services/vision_prompts.py` | 视觉提示词；原生 DOCX 图片使用严格 OCR |
| `src/services/pdf_text_layer_reconcile.py` | 按同页 PDF 文本层修正 checkbox 状态 |
| `src/utils/file_conversion.py` / `mineru_support.py` | Office 转 PDF 及本服务扩展名边界 |
| `src/routers/mineru_minio_utils.py` / `src/services/minio_storage.py` | MinIO 前后处理、PDF/JSON/JPEG/meta 资产 |

## 解析合同

- 六个 POST 入口均有 `tier` 表单枚举：`flash/basic/standard/advanced`，不传固定 `standard`，非法值 422。枚举位于 `src/utils/mineru_backend.py`；任务通过现有 `backend` / `backend_value` 字段保存提交时的选择。
- `/mineru`、`/mineru_sci`、`/mineru_with_images` 和两个普通 `/task` 的 `chunk_type`、`return_txt` 是 **query 参数**；仅 `/two_stage/task` 将它们定义为 form。文档、curl 和脚本示例必须按真实 OpenAPI 编写。
- 直接服务调用未指定档位时读取 `MINERU_DEFAULT_TIER`，再兼容旧 backend：pipeline→basic、hybrid→standard、vlm→advanced。旧名称只影响档位，大模型推理仍走 Docker；不要重新引入本机引擎。
- PDF、受支持图片和 Office 转 PDF 清单才是服务输入边界；Markdown/TXT 拒绝。不要因上游新增格式而自动扩大接口范围。
- `parse_doc()` 使用无状态 `mineru.parser.parse`，默认 PDF `page_range=all`。零基 start/end 转成一基范围，保留源页号；返回 `(content_list, artifact_dir, None)`。先保存 MiddleJson/图片，再渲染 V1 并补齐旧字段；图片引用缺失必须失败。
- `img_caption/img_footnote`、chart/code/index/page_footnote 映射和 bbox 单位需保持下游兼容；同时写旧命名 `_content_list.json` 供诊断。异常继续冒泡，不返回空值伪装成功。
- Office 主结果始终先经 LibreOffice 转 PDF，再使用所选 tier；每次转换使用独立 profile 并在超时后收尾。
- 仅同步 `/mineru_with_images` 的 `.docx + return_txt=true` 使用额外原生 DOCX flash 分支生成 txt，result/页码/MinIO 仍来自 PDF。该分支图片按原文顺序插入严格可见内容 OCR，不附加 caption/footnote 或根据上下文推断实体。普通任务和 two-stage 不启用该分支。
- `chunk_type=true` 保留 title/header/footer 和原阅读顺序，视觉增强的图片块标 image，忽略 page_number 块；普通正文/表格可能没有 type。txt 按同序拼接，标题段后两个换行，普通段后一个换行。普通响应省略 null 字段、任务失败查询返回 500；two-stage 保留 null、任务失败查询返回 200 状态体，调用方仍须检查 state。
- checkbox 修正在已有 checkbox 符号时触发 `pdftotext -bbox`，按源页和选项匹配；缺工具、超时或抽取失败则跳过。补回缺失首符号仅允许同页唯一完整选项组、至少两个其他符号仍在、首标签至少四字；歧义、短标签、额外文字或跨页不得补。环境开关与超时见部署配置。

## 视觉与资产

- 默认视觉 provider 为 vLLM；OpenAI/Gemini 实现仍可显式配置。未知 provider/model 在同步图片接口及普通图片任务中宽松接收，由服务兜底；two-stage 则在路由层校验枚举并可返回 422。
- vLLM 必须有 `VLLM_BASE_URL(S)` 才可用，API key 可选。此地址是独立图片描述模型，与 `MINERU_MODEL_VLM_SERVER_URL` 不同。
- OpenAI/vLLM 复用客户端池；多个视觉 endpoint 会顺序尝试。不要把视觉故障切换能力误写成 MinerU 解析端点的能力；MinerU 目前只有进程内轮换。
- 视觉请求默认 `enable_thinking=false`，采样参数由 `VLLM_VISION_*` 覆盖。同步图片分批并发由 `VISION_BATCH_SIZE` 控制；上下文在请求前固定，不将生成描述回灌为后续上下文。视觉异常使请求/任务失败，不使用 base_text 降级。
- two-stage 图片筛选按相对面积、分辨率、体积、长宽比、每页数量及哈希去重，合并保持原位；清理视觉输出中的 Page/ChunkType 标记和固定说明前缀。
- `/mineru`、`/mineru_with_images` 及两个普通任务支持 MinIO；科研/two-stage 不支持。保存转换后的 source.pdf、服务 parsed.json、逐页 JPEG 和可选 meta.txt。`chunk_type=true` 时 JSON 保留类型，`save_to_minio=false` 时忽略 minio_meta。
- MinIO prefix 保留 Unicode/中文标点，空格和不可打印字符规范化；通用上传还支持 base64，空内容返回 400。不要用原生 MiddleJson 覆盖业务 parsed.json。

## 队列与进程

- 普通 app `src.services.celery_app` 消费 `queue_urgent,queue_normal,default`；普通 worker 不消费 two-stage 的解析/视觉队列。
- two-stage 部署显式配置 normal 队列 `queue_parse_gpu/queue_vision/queue_dispatch/default`；四类 urgent 为 `queue_parse_urgent/queue_vision_urgent/queue_dispatch_urgent/queue_merge_urgent`。API 与每个 worker 必须配置一致，不能只改 worker 的 `-Q`。
- 未配置时代码的 parse 回退到普通队列，dispatch/merge 回退到 default；`.env.example` 显式列出与 PM2 匹配的队列，详细规则见 two-stage 文档。
- Redis 优先级消费按 `-Q` 的 urgent→normal 顺序。dispatch 使用 `self.replace` 启动 chord；不要在 Celery task 内阻塞调用 `result.get()`。四个阶段都要有消费者和可用的 result backend。
- API 与 worker 共享 broker/backend/任务目录；跨容器时目录绝对路径一致。`PENDING` 也可能是未知或过期 ID，`queue_status` ready/unacked 不等于最终结果。
- scheduler 在独立子进程中解析，Linux 使用 parent-death signal 和任务进程组；仅在 hard timeout、父进程退出或结果返回后清理该任务组。不能按名称/运行时长全局误杀解析进程。
- 普通模板 threads/16，可用 solo/1 保守运行；two-stage parse 为 solo，其余线程池。避免 daemonic prefork；保持临时文件 finally 清理和正常 shutdown 等待。

## 配置与运维

- 进程环境优先于 `.env`，再回退到 `.secrets/secrets.toml`。PM2 `env` 属于进程环境，不会被 `load_dotenv()` 覆盖；Python 加载 `.env` 不会替调用方 shell 导出变量。
- 配置模块仍要求 TOML 的 FASTAPI/OPENAI/GOOGLE/VLLM 段存在；复制 `deploy/secrets.example.toml` 初始化。公开模板不得包含实际凭证；部分字段空串会回退到 TOML，不代表清除原配置。
- 默认部署入口为 `compose.mineru.yaml`；`ecosystem.vllm*.json` 是保留的 Compose 包装示例，同一 project 由一个入口管理。应用的 `GPU_IDS` 不控制 Docker GPU。
- Docker 基线为 vLLM 0.21.0 配套 Torch/CUDA，模型上下文 8192；应用使用独立 `uv.lock`。升级镜像时重新检查依赖与 PDF，不在应用中补装 vLLM。
- PM2 API 为 `unstructured-gunicorn`，Gunicorn timeout/graceful-timeout 1900 秒。科研入口另有自己的 HTTP 等待窗口；具体超时以模板/运行环境为准。
- `/health`、`/gpu/status` 和 `/two_stage/queue_status` 用于检查状态，启用鉴权时带 Bearer。日志默认 INFO，httpx/httpcore 降到 WARNING，视觉提示词仅 DEBUG；不输出密钥或完整 PM2 环境。
- 维护先定位当前服务树和 active/reserved 任务，按具体任务清理。不要在日常说明中使用全局 PM2 删除、Redis flushdb 或无差别清空共享任务目录。

## 开发与验证

修改后运行：

```bash
uv run --group dev black .
uv run --group dev ruff check src
uv run --group dev pytest
```

- Black 必须排除任意层级 `.venv` 及根目录 output/input/pdfs/pickle，防止修改依赖备份。Ruff 当前显式使用 E4/E7/E9/F；保持异常处理粒度合理，不扩大吞异常范围。
- 常规测试使用外部依赖/调度替身；`test_mineru_tier_routes.py` 验证六入口参数，`test_mineru4_adapter.py` 验证 SDK/资产，其他测试覆盖阅读顺序、DOCX、视觉、MinIO 和进程生命周期。
- 真实模型回归：`MINERU_RUN_INPUT_PDFS=1 uv run --group dev pytest tests/test_mineru_input_pdfs.py -v`。读取 input 的 11 份 PDF；p2 四档整本，论文和 fese 整本，其余抽样首页/第 11 页/末页。没有样本应明确失败，不用替身冒充实测。
- 2026-09-17 升级验收为 135 项常规测试；历史 PDF 覆盖范围、追加四档结果及本机切换证据统一记录在部署说明，避免多处维护测试数字。
- `src/scripts/two_stage_enqueue.py` 的生产调用须显式 `TWO_STAGE_BASE=http://127.0.0.1:7770`，脚本缺省仍是开发端口 8770，且不传 tier（使用 standard）。优先级演示 `enqueue_input.py` 会重复提交；不要作为生产批处理入口。

## 本机状态

2026-09-17：升级合并到 main，API 7770，Docker project `mineru4-upgrade`、端口 31000、GPU 0、显存比例 0.09；API 和 四类 two-stage worker 在线。旧 MinerU 本机 vLLM 和 PM2 启动项已移除；另一仓库的 embedding 服务独立运行。回滚目录、模型缓存及验收日志见[部署记录](mineru_4_upgrade_usage.md#本机部署与回滚记录2026-09-17)。

本次说明整理归档历史评估，修正普通任务的 query 示例和配置优先级，集中维护队列/模板说明；`deploy/secrets.example.toml` 替代原开发 TOML，私有 `.secrets/` 整体忽略。仅公共模板调整了单槽位、超时和显式队列示例，运行中的私有 `.env` 与业务代码不变。
