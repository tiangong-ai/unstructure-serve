# MinerU 4 部署与 PDF 回归

本项目使用 MinerU 4 无状态 SDK；MinerU VLM 后端通过 Docker 部署，应用环境不安装 vLLM。API、Celery、two-stage、Office→PDF 和 MinIO 的业务接口保持原有约定。历史评估见 [升级评估](mineru_4_upgrade_evaluation.md)。

## 安装与启动

在项目目录执行：

```bash
uv sync --locked --group dev
# 首次部署可复制模板；已有 .env 时保留凭证，合并新增字段。
cp -n .env.example .env
MINERU_MODEL_SMALL_BACKEND=onnx uv run mineru-kit models download --tier basic --small-backend onnx --source modelscope
MINERU_MODEL_SMALL_BACKEND=onnx uv run mineru-kit models verify --tier basic --small-backend onnx
docker compose -f compose.mineru.yaml up -d --build
docker compose -f compose.mineru.yaml logs -f mineru-vlm
```

确保 `.secrets/secrets.toml` 已按原部署要求准备好。Docker 需要 NVIDIA Container Toolkit，并注册 `nvidia` runtime；Compose 显式选择该 runtime，以兼容本机的 CDI 模式。容器 healthcheck 成功后再启动 API/worker。首次启动容器会下载 VLM 权重，保存在 Docker 命名卷中。

关键应用配置：

```dotenv
MINERU_DEFAULT_TIER=standard
MINERU_DEFAULT_METHOD=auto
MINERU_MODEL_SMALL_BACKEND=onnx
MINERU_MODEL_VLM_SERVER_URL=http://127.0.0.1:30000
MINERU_MODEL_VLM_MODEL=mineru4
MINERU_MODEL_VLM_HTTP_TIMEOUT=600
MINERU_MODEL_VLM_MAX_CONCURRENCY=8
```

`flash/basic/standard/advanced` 是质量档位。HTTP 请求使用显式 `tier` 或固定缺省值 `standard`，并通过现有 backend payload 字段传到 worker。直接调用服务且未指定档位时，优先读取 `MINERU_DEFAULT_TIER`，再兼容旧 `MINERU_DEFAULT_BACKEND`：pipeline→basic，hybrid→standard，vlm→advanced；旧在途任务仍可读取。旧本地引擎名称也使用 Docker 的远程 VLM 连接，不在应用进程加载大模型。

`MINERU_MODEL_VLM_SERVER_URL` 是 MinerU 模型推理服务，不是文档解析 V1 API；也不要与图片描述模型的 `VLLM_BASE_URLS` 混用。单个 URL 优先于旧 URL 列表；多卡轮换时清除单 URL，再设置 `MINERU_VLLM_SERVER_URLS`。连接凭据通过 `.env` 的 `MINERU_MODEL_VLM_API_KEY` 配置；仅支持 Bearer，旧自定义认证头会明确报错。

通过 `MINERU_HOME` 可选择 CPU 模型及配置的持久目录。`MINERU_DEFAULT_LANG` 仅为旧配置兼容项，新 SDK 不接受语言覆盖；hybrid batch/force-pipeline 参数也不再透传。CPU/GPU 选择和 VLM 并发需显式配置。

## 每次请求选择质量档位

`/mineru`、`/mineru_sci`、`/mineru_with_images`、`/mineru/task`、`/mineru_with_images/task`、`/two_stage/task` 均提供可选 `tier` 表单字段，Swagger 显示四档下拉选项。不传固定使用 `standard`；非法值（包括旧的 `pipeline`/`vlm`/`hybrid` 名称）返回 422，不进入解析队列。

| tier | 解析方式 |
| --- | --- |
| `flash` | 读取 PDF 原生文本层，无推理模型；适合预览和索引，扫描件需选 OCR 档位。 |
| `basic` | 小模型处理 OCR、公式和表格；本部署在 CPU 上运行。 |
| `standard` | 默认；小模型结合 Docker VLM，处理复杂版面。 |
| `advanced` | 使用更多 VLM 推理计算，应对高难度文档。 |

档位语义参见 [MinerU 4.0 发布说明](https://github.com/opendatalab/MinerU/releases/tag/mineru-4.0.0-released)。异步任务保存提交时的档位，worker 环境变量变更不会改变已提交的选择。

```bash
curl --fail -X POST 'http://127.0.0.1:7770/mineru?return_txt=true' \
  -H "Authorization: Bearer $FASTAPI_BEARER_TOKEN" \
  -F 'file=@input/p2.pdf' \
  -F 'tier=advanced'
```

Office 主结果继续先转 PDF，再使用所选档位。同步 DOCX 的原生 TXT-only 分支仍按 MinerU 的原生文档约束使用 `flash`；图片描述模型由独立的 `provider`/`model` 控制，`tier` 只控制 MinerU 拆解质量。

## Docker 版本与资源

Dockerfile 按 [MinerU 4 官方部署文档](https://opendatalab.github.io/MinerU/quick_start/docker_deployment/) 选用 `vllm/vllm-openai:v0.21.0`（CUDA 13），固定 MinerU 4.0.0，并保留镜像配套的 vLLM/Torch 2.11.0 组合。镜像中安装 FastAPI 0.141.1、Starlette 1.6.0、instrumentator 8.1.0，以覆盖旧路由监控兼容问题；构建执行 `pip check` 和版本断言。

基础镜像原有 PyGObject 缺少 Pycairo；Dockerfile 按 [Pycairo 安装文档](https://pycairo.readthedocs.io/en/latest/getting_started.html) 补齐 Cairo 开发库、pkg-config 与 Python 包。

应用依赖通过 `uv.lock` 固定本次升级解析结果。OpenAI SDK 采用 2.54.0，是 MinerU `<3` 约束下的最新兼容版本；不能同时声称已安装最新 3.x。vLLM 的镜像版本是按官方基线有意保留的例外，后续升级镜像要连同 Torch/CUDA、模型和 PDF 回归重新验证。

默认容器绑定 GPU 0、宿主 127.0.0.1:30000，显存比例 0.15。通过下列字段调整，值应与空闲资源匹配：

```dotenv
MINERU_DOCKER_GPU_ID=0
MINERU_DOCKER_PORT=30000
MINERU_DOCKER_GPU_MEMORY=0.15
```

与旧服务并行验证的示例：

```bash
MINERU_DOCKER_PORT=31000 MINERU_DOCKER_GPU_MEMORY=0.09 \
  docker compose -p mineru4-upgrade -f compose.mineru.yaml up -d --build
curl --fail http://127.0.0.1:31000/health
curl --fail http://127.0.0.1:31000/v1/models
```

多卡使用不同 Compose project、端口和 GPU ID，例如：

```bash
MINERU_DOCKER_GPU_ID=0 MINERU_DOCKER_PORT=30000 docker compose -p mineru-gpu0 -f compose.mineru.yaml up -d
MINERU_DOCKER_GPU_ID=1 MINERU_DOCKER_PORT=30001 docker compose -p mineru-gpu1 -f compose.mineru.yaml up -d
```

`ecosystem.vllm*.json` 已改为 Docker Compose 包装器，供仍使用 PM2 管理入口的部署兼容使用；不要同时用两个入口管理同一 Compose project。quatro 模板仍是四卡示例，应按实际硬件删减实例。Docker 默认服务不向公网开放推理端口。

API 的 Gunicorn 模板已改用独立 `uvicorn-worker` 包中的 `uvicorn_worker.UvicornWorker`。普通 Celery 和 two-stage 的队列、优先级不变。迁移时让旧任务排空，或使用独立 broker/队列路由；不要让不同运行环境混用未验证的在途任务。

## 基于 input PDF 的 TDD

`tests/test_mineru_input_pdfs.py` 直接读取项目 `input` 目录，也支持 `MINERU_TEST_INPUT_DIR`。这些测试会真正运行模型，默认常规测试跳过；显式启用时，缺少预期 PDF 会失败，不会假装完成验证。

- `p2.pdf`：完整两页，表格关键字段、选中状态和图片资产；验证 flash、basic、standard、advanced 四档。
- 锂化学品论文：完整九页，正文、公式和图像资产。
- fese.pdf：完整 46 页，验证默认整本调用的 is_full_document 和全部源页码，不会截断在前 10 页。
- 全部 11 份 PDF：分别验证首页、第 11 页（存在时）、末页的源页码和资产，包括 1,016 页文件的末页。抽样测试不等于每份长文档已完整解析。

运行常规检查：

```bash
uv run --group dev black .
uv run --group dev ruff check src
uv run --group dev pytest
```

运行实际 PDF 回归（先准备模型和容器）：

```bash
MINERU_RUN_INPUT_PDFS=1 uv run --group dev pytest tests/test_mineru_input_pdfs.py -v \
  --junitxml=output/mineru4_tdd/input-results.xml
```

升级工作区的 `output/mineru4_tdd/` 保存了升级前的 68 项测试结果、p2/论文的 3.4.3 真实解析基线，以及新增测试的失败记录。PDF 文本和派生资产只留在忽略的 output 目录，不复制进测试源码或提交。

2026-09-17 实测结果：常规测试 79 项通过；模型回归 15 项全部通过，分两次执行共用时 132.20 秒。11 份文件共检查 29 个抽样页，加上 p2/论文/fese 整本测试，共覆盖 79 个不同 PDF 页面；其中 fese 的 46 页完整解析用时 35.35 秒，所有源页码及 is_full_document 均通过校验。论文的 8 张图片通过 two-stage 筛选，其中一张完成真实视觉调用及原位合并；这不代表已逐张人工评估全部视觉输出。另已通过同步 API、真实 MinIO 的 PDF/JSON/JPEG/meta 往返、Office→PDF 和 native DOCX 图片顺序冒烟测试。

日志与 JUnit 结果在 `output/mineru4_tdd/`，15 项模型回归的汇总为 `input-results-all.xml`；MinIO 验证使用独立临时容器，测试后已移除，未写入现有业务桶。

兼容层单测还覆盖保存后的图片引用、页号转换、caption 别名、代码/目录/页脚注释映射、bbox 单位、连接轮换和自定义认证错误。同步、Celery、two-stage 的已有阅读顺序与失败传播测试继续保留。

同日请求级 tier 补齐后：常规测试增至 135 项通过（其中 48 项覆盖六个接口的四档选择、缺省、非法值和 OpenAPI）；p2 的四档真实解析全部通过。basic 首轮实测漏掉“公开竞争”前的选中符号，已基于原 PDF 文本层补回：要求同页完整选项组唯一匹配、首标签至少四字、至少两个其他选项仍有符号；歧义、跨页及非完整匹配均不补。对应 TDD 日志为 `tier-routes-red.log` / `tier-routes-green.log`、`tier-checkbox-red.log` / `tier-checkbox-green.log`、`tier-p2-results.xml`，常规结果为 `tier-pytest.log`。这次补充回归针对四档 p2，前述 11 文件覆盖数据来自升级阶段的测试。

本机服务重新加载后，六个接口的在线 Swagger/非法值校验、`/mineru` 的缺省及四档 p2 上传、`/two_stage/task` 的 basic 真实 Celery 任务均通过，见 `output/mineru4_tdd/tier-api-smoke.log`。

## 结果合同与回滚

`parse_doc` 保存 4.0 的 MiddleJson/资产，再渲染 Content List V1 并归一化为项目旧字段，最后执行 PDF checkbox 回填。每个任务仍返回 `(content_list, artifact_dir, None)`，并显式写出旧命名的 `_content_list.json` 供诊断使用。缺失图片会导致失败，避免视觉阶段静默跳过。

业务 MinIO `parsed.json` 继续采用现有响应结构；不要用原生 MiddleJson 覆盖它。原生 DOCX 仍只用于同步图片接口的 TXT 增强，默认 JSON 与 PDF 资产继续来自 Office→PDF。

升级前保留旧运行环境、锁文件和模型服务地址；灰度使用独立队列和 MinIO prefix。回滚时同步恢复 API/worker 的代码、环境和模型端点，保留已完成资产及需要收尾的在途任务。容器命名卷可复用，不要因回滚而删除模型缓存。

## 本机切换记录（2026-09-17）

原项目 `/home/david/projects/TianGong-AI-Unstructure-Serve` 使用合并后的 `main` 分支运行（升级开发分支为 `codex/mineru4`），API 端口仍为 7770。Docker 后端使用已验证的 127.0.0.1:31000、GPU 0、显存比例 0.09；本机 `.env` 设置 `COMPOSE_PROJECT_NAME=mineru4-upgrade`，因此在原项目目录运行文中的 Compose 命令即可管理该容器。新装机器可采用前文默认端口 30000。

旧 MinerU 原生 PM2 服务已删除；API 与四个 two-stage worker 已重启并保存进程配置。部署后真实同步解析和 Celery dispatch→parse→merge→状态查询均通过 p2 验证，日志见 `output/mineru4_tdd/deployment-smoke.log`。应用侧 ONNX 模型缓存位于 `/home/david/.local/share/tiangong-mineru4`，Docker 模型及编译缓存位于命名卷。

回滚快照位于 `output/mineru4_tdd/rollback-20260917/`，包含旧源码归档、配置、PM2 快照与旧 `.venv`。恢复时需将旧虚拟环境移回原 `.venv` 路径，再同步恢复代码、配置和原模型服务；不要直接从改名后的旧环境启动带绝对 shebang 的脚本。
