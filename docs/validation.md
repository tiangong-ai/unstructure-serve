---
docType: runbook
scope: repo
status: current
authoritative: true
owner: unstructure-serve
language: zh-CN
whenToUse: "When developing or validating this repository independently or in a workspace."
whenToUpdate: "When governance commands, entrypoints or validation requirements change."
checkPaths:
  - .docpact/config.yaml
  - .github/workflows/docpact.yml
lastReviewedAt: 2026-09-19
lastReviewedCommit: 80c8c24ed5087b0f8052d6ddb3fd5fe10a8da90f
---

# 开发验证与验收范围

所有命令在仓库根目录执行。本文件供开发运维使用，不经服务文档路由提供。原始 PDF、凭证、任务记录和测试输出保存在私有目录；公共文档只保留验证方法、适用条件和结论边界。

## 常规检查

```bash
uv sync --locked --group dev --check
uv run --group dev black .
uv run --group dev ruff check src
uv run --group dev pytest
```

`uv sync --check` 可能仅因 README/项目元数据变化而提示重建本项目的 editable 安装；先检查具体变更项，不把它直接当作第三方库漂移。只检查代码、且现有依赖已满足时，可用 `uv run --no-sync --group dev ...` 运行检查，避免同步运行环境；依赖变更仍须在隔离环境验证。

常规测试用替身隔离外部依赖，覆盖路由参数、SDK 适配、资产完整性、阅读顺序、视觉失败、上传清理、共享解析槽和批量恢复；不代表模型解析质量或生产容量已验证。

持久任务的针对性测试包括 `test_durable_jobs.py`、`test_durable_pipeline.py`、`test_job_api.py` 和 `test_manage_jobs.py`：覆盖发布结果不明、元数据落盘失败、旧代消息、逐阶段复用、文件校验、下载租约与保留期清理。共享视觉容量包含真实 fork/spawn/进程死亡，以及本地 HTTP fixture；只有显式模型回归才算实际推理。

故障回归须保留故障前后的输入摘要、任务 ID/generation、parse manifest 和已完成图片的 SHA256/mtime。实测九页论文在指定单图请求前注入一次失败，恢复后原解析及三张已完成图片未改变，六张最终图片各实际调用模型一次。另对真实 SDK 子进程定点 SIGKILL，检查快速失败、同 ID 恢复及任务进程组收尾。注入故障与上游实际超时分开报告，不全局停止共享模型或清空队列。

持久流水线更新后，生产 API 与七个 worker 已完成真实 p2 的三类幂等任务、轻量下载及旧响应合同，以及同步 p2、普通任务、含图论文和 Office 转换验收；之后生产队列及 active/reserved/scheduled 为空。三卡 Docker 模型与独立 embedding 进程未重启，PM2 已保存。该验收不代表低清图片语义或长期满载测试通过，具体质量与长文档边界见调优指南。

Black 排除任意层级的 `.venv` 和根目录的 input/output/pdfs/pickle，避免格式化模型环境或结果。Ruff 规则以 `pyproject.toml` 为准。

## 真实 PDF 回归

这些测试会调用已配置服务，先确认没有繁忙的生产任务，再按改动范围启用。缺少必需样本或模型时不能用替身冒充通过。

| 测试文件 | 启用变量 | 覆盖范围 |
| --- | --- | --- |
| `test_mineru_input_pdfs.py` | `MINERU_RUN_INPUT_PDFS=1` | 固定样本清单的首页、第 11 页或末页；p2 缺省及四档整本；九页论文与 46 页 fese 整本 |
| `test_vision_input_pdf.py` | `MINERU_RUN_VISION_PDFS=1` | 论文第五页的实际图片，验证描述中的关键数值和单位 |
| `test_durable_input_pdfs.py` | `MINERU_RUN_DURABLE_PDFS=1` | input/p2 三类真实异步任务、重复幂等键、轻量状态、文件下载和旧结果合同 |
| `test_mineru_data_parallel.py` | `MINERU_RUN_DP_PDFS=1` | p2、九页论文整本及三个 engine 的成功请求增量 |
| `test_live_api_pdfs.py` | `MINERU_RUN_API_PDFS=1` | 部署 API 的同步/普通任务/two-stage，以及从 p2 文本构造的 DOCX 转换 |
| `test_batch_input_pdfs.py` | `MINERU_RUN_BATCH_PDFS=1` | 三个批量模式的 p2/九页论文整本、JSON 结果及续跑 |

例如验证实际 API 与批量客户端：

```bash
mkdir -p output/validation/run-01
MINERU_RUN_API_PDFS=1 MINERU_RUN_BATCH_PDFS=1 \
  uv run --group dev pytest tests/test_live_api_pdfs.py tests/test_batch_input_pdfs.py -v \
  --basetemp=output/validation/run-01/pytest
```

`--basetemp` 会清理指定目录，每轮使用新目录。按测试代码配置 `MINERU_TEST_API_URL`、`MINERU_TEST_INPUT_DIR` 或 `MINERU_TEST_VLM_URL`；不同测试支持的覆盖变量不同，不把其中一个变量当作所有测试的全局设置。输入回归的文件清单由测试中的 `PDF_NAMES` 固定，往 input 添加文件不会自动扩大回归范围。

任务成功后检查源页号、首尾有效内容、关键表格/数字、checkbox、阅读顺序与图片文件。空白页可能没有业务块，不能只看最大 page_number 判断整本完整性。

## 运行设施验收

| 检查 | 可以确认什么 | 不能据此确认什么 |
| --- | --- | --- |
| PM2 online | 启动命令或进程存活 | 模型已加载、队列有人正确消费 |
| API `/health` | API 可响应 | 模型或队列就绪 |
| API `/ready` | 配置的 MinerU 端点健康 | Redis、独立视觉模型或实际推理成功 |
| `inspect active_queues` | worker 消费的队列 | 已提交任务最终成功 |
| `/two_stage/queue_status` | ready/unacked 数量 | 任务 ID 的最终状态或全部调度任务已清空 |
| 实际 PDF 与 engine 计数 | 该样本的结果与请求分配 | 长期稳定性或千页容量 |

批量客户端还需检查：POST 结果未知时不重投，GET 失败后续查同一 ID，成功结果落盘，重复运行不重复提交，`--resume-only` 不补交或重试文件。任务停止与恢复流程见[批量指南](batch-processing.md#中断与续跑)。

## 性能证据与限制

性能报告至少记录代码/依赖/镜像版本、文件摘要、页数与图片数、有效参数、预热方式、重复次数、错误率和资源峰值。保留报告到私有 output 子目录，在版本提交中说明报告位置；不要把执行过程中不断增长的测试数量写入每份说明。

可复现的 API 派发对照保留在[调优指南](performance-tuning.md#13-api-与解析容量分离)。其短样本结果不能外推为 Python 升级收益，也不能作为单卡/三卡的完整吞吐对照。

已完成合成 400/1000 页与原生 1016 页整本 SDK 实测，条件和结果见[调优指南](performance-tuning.md#14-长文档与图片回归测量)。固定 input pytest 清单仍按上述范围执行，不能把 SDK 测量等同于所有入口或 20 万页批量容量承诺。正式处理前按[大文档流程](ai-integration.md#53-多份-4001000-页-pdf-的投递流程)先做整本单文件，再测试 2、3 个在途文件及故障恢复。

## 长文档样本构造

`src/scripts/build_pdf_case.py` 将输入 PDF 按顺序循环复制到指定页数，不修改原件、不拆分服务任务。输出目录必须新建；case.pdf 与 manifest.json 保存文件摘要和逐页来源，仅留在私有 output。

```bash
uv run python -m src.scripts.build_pdf_case --sources input/p2.pdf input/fese.pdf \
  --pages 400 --output output/long-cases/mixed-400
```

复制扩页用于检查页号、重复图片、内存和任务生命周期，不等同于同规模不同内容文档的性能；须同时测试原生长文档。质量断言应从实际源页建立，不能只要求返回 SUCCESS。

## 私有图片提示词对照

从真实 PDF 的解析资产选图，逐张核对后编写 JSON 数组清单。图片路径相对仓库根目录，`required`/`forbidden` 为 Python 正则；同一清单对所有候选使用同一规则，保留失败结果。示例仅示意结构，实际数字必须来自原图：

```json
[{"name":"figure-a","image":"output/cases/figure.jpg","context":"原文标题及相邻正文","required":["52\\s*%"],"forbidden":["Image Description:"]}]
```

```bash
uv run python -m src.scripts.benchmark_vision \
  --cases output/cases/vision.json --output output/vision-benchmark \
  --concurrency 3 --repetitions 3
```

`--prompt-file` 替换默认提示词用于对照；采样由 `VLLM_VISION_*` 环境覆盖。输出目录必须新建，保存清单/图像摘要、逐请求原始响应、检查结果、token 和耗时；出现空/截断响应或检查失败时退出非零。每张图遍历每个配置端点及每个 seed，随机化执行顺序；记录分端点耗时与容量等待，不启用故障切换掩盖单端点问题。该工具直接调用图片模型，不测完整 PDF/Celery。人工复核数字归属、流程关系与遗漏，不能只凭正则通过采用更短的提示词。上下文、响应和图像均保持私有。

## 文档治理检查

独立仓库使用固定 docpact 0.1.9；无需父 workspace 或运行服务。

```bash
cargo install docpact --version 0.1.9 --locked
docpact route --root . --paths src/routers/job_router.py --format json
docpact validate-config --root . --strict
docpact lint --root . --staged --mode enforce
```

`--staged` 包含新文件。也可用明确 `--base <sha> --head <sha>` 检查提交；实际复核后用 `docpact review mark --root . --path <文档>` 记录证据。配置按 API、处理/持久化、批量客户端、部署/依赖和验证分组复用现有 docs，不引入第二套架构说明。CI 对 PR 的 base/head 运行同样的强制检查，push 只检查配置有效性。治理检查不能替代上面的 Python 或真实服务验收。
