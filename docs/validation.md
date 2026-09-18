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

Black 排除任意层级的 `.venv` 和根目录的 input/output/pdfs/pickle，避免格式化模型环境或结果。Ruff 规则以 `pyproject.toml` 为准。

## 真实 PDF 回归

这些测试会调用已配置服务，先确认没有繁忙的生产任务，再按改动范围启用。缺少必需样本或模型时不能用替身冒充通过。

| 测试文件 | 启用变量 | 覆盖范围 |
| --- | --- | --- |
| `test_mineru_input_pdfs.py` | `MINERU_RUN_INPUT_PDFS=1` | 固定样本清单的首页、第 11 页或末页；p2 缺省及四档整本；九页论文与 46 页 fese 整本 |
| `test_vision_input_pdf.py` | `MINERU_RUN_VISION_PDFS=1` | 论文第五页的实际图片，验证描述中的关键数值和单位 |
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

千页样本目前只在固定页抽样用例中覆盖，尚未承诺千页整本并发或 20 万页批量容量。正式处理前按[大文档流程](ai-integration.md#53-多份-4001000-页-pdf-的投递流程)先做整本单文件，再测试 2、3 个在途文件及故障恢复。

## 长文档样本构造

`src/scripts/build_pdf_case.py` 将输入 PDF 按顺序循环复制到指定页数，不修改原件、不拆分服务任务。输出目录必须新建；case.pdf 与 manifest.json 保存文件摘要和逐页来源，仅留在私有 output。

```bash
uv run python -m src.scripts.build_pdf_case --sources input/p2.pdf input/fese.pdf \
  --pages 400 --output output/long-cases/mixed-400
```

复制扩页用于检查页号、重复图片、内存和任务生命周期，不等同于同规模不同内容文档的性能；须同时测试原生长文档。质量断言应从实际源页建立，不能只要求返回 SUCCESS。
