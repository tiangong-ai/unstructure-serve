# 统一批量解析客户端

入口：`uv run python -m src.scripts.batch_parse`。三个异步 API 共用一个滚动提交、轮询和结果保存实现；质量缺省 `advanced`，可选 `flash/basic/standard`。客户端只需要能访问 API，不需要连接服务端 Redis 或 GPU。

## 选择工作流

| `--mode` | POST / GET 前缀 | 场景 |
| --- | --- | --- |
| `parse`（缺省） | `/mineru/task` | 正文、表格等解析，不额外调用图片描述模型 |
| `images` | `/mineru_with_images/task` | 普通队列图片增强 |
| `two-stage` | `/two_stage/task` | 图片增强分阶段执行 |

同步科研 `/mineru_sci` 没有异步任务合同，不提供伪装成队列的批量模式。`parse` 的 advanced 本身仍会使用 MinerU VLM；“不带图片识别”指省去独立图片描述阶段。

## 直接使用

在仓库根目录执行；生产同机缺省地址是 `http://127.0.0.1:7770`，远程用 `--base-url https://your-service/prefix`，保留反向代理路径前缀。认证优先 `FASTAPI_BEARER_TOKEN` 环境变量，其次仓库 `.env`，最后仓库 `.secrets/secrets.toml` 的 `FASTAPI.BEARER_TOKEN`。不通过命令行传令牌。无鉴权的测试 API 才显式使用 `--no-auth`。

```bash
# 预览文件数和总字节数，不上传、不创建输出目录、不验证服务容量
uv run python -m src.scripts.batch_parse \
  --input-dir /path/to/pdfs --output-dir /path/to/results-parse --dry-run

# 纯解析；一次成功落盘后补下一份
uv run python -m src.scripts.batch_parse \
  --mode parse --input-dir /path/to/pdfs --output-dir /path/to/results-parse \
  --max-in-flight 2

# 分阶段图片增强
uv run python -m src.scripts.batch_parse \
  --mode two-stage --input-dir /path/to/pdfs --output-dir /path/to/results-images \
  --max-in-flight 2

# 普通队列图片增强，可显式指定 provider/model/prompt
uv run python -m src.scripts.batch_parse \
  --mode images --input-dir /path/to/pdfs --output-dir /path/to/results-ordinary-images
```

默认只扫描目录第一层 PDF；`--recursive` 扫描子目录，`--extensions pdf,docx,pptx` 选择格式，`--extensions all` 采用服务支持的 PDF、图片及 Office 格式清单，不包含 TXT/Markdown。目录和同名不同扩展名分别保存，不会相互覆盖。扫描结果为空直接失败。输入目录是本批次的稳定清单，运行/续跑过程中不要移动、删除或修改已经提交的文件。

输出为 `results/<输入相对路径及扩展名>.json`，例如 `results/部门A/年报.pdf.json`；保存的是业务对象，包括 `result` 列表和可选 `txt`，不是外层任务状态。`.tasks/<输入相对路径>.json` 保存 SHA-256、请求、task_id、尝试次数、历史任务 ID 和结果摘要；`.batch.json` 保存批次身份。文件原子替换并同步到磁盘，凭证不写入这些记录。

`--chunk-type` 缺省开启，关闭用 `--no-chunk-type`；`--return-txt` 按需增加全文。默认不重复返回一份 TXT。两个普通入口由客户端把这两个字段放入 query，two-stage 放入 form；调用方不用切换写法。

## 并发、超时和重试

| 选项 | 新客户端缺省 | 含义 |
| --- | ---: | --- |
| `--max-in-flight` | 2 | 已提交但尚未取回成功结果的文件数；不代表 GPU 数或图片请求数 |
| `--poll-interval` | 5 秒 | 一轮查询之间的间隔 |
| `--poll-timeout` | 21600 秒 | 每份提交/恢复后的本地等待预算，含排队 |
| `--upload-timeout` | 600 秒 | HTTP 上传/等待提交响应的读写阶段超时 |
| `--query-timeout` | 60 秒 | 单次查询的 HTTP 读写阶段超时 |
| `--connect-timeout` | 10 秒 | 建立连接的超时 |
| `--max-attempts` | 1 | 含首次提交；缺省不自动重跑昂贵的失败文档 |

HTTP 超时是网络阶段的无进展超时，不是硬性整次请求墙钟截止时间。一次查询可能使本地总等待预算略微超出。PDF 上传采用 HTTPX 流式 multipart，不在客户端一次拼接整份文件；服务端请求体、Office 转换、模型解析和完整结果 JSON 仍有自己的内存需求。

`--priority urgent` 可发紧急任务；日常批量保持 normal。不要为了提速将整个目录改为 urgent。`--max-attempts 2` 只在确认 FAILURE/REVOKED 后允许再提交一次；普通 API 的失败虽然是 HTTP 500，仍会作为明确终态处理。服务端配置错误应先修复再提高尝试上限。

## 中断与续跑

同一输出目录只允许一个客户端写入。停止 CLI 不取消已经提交的服务端任务；运行原命令即可恢复，成功结果经摘要检查后跳过，缺失/损坏的结果会按原 task_id 再取回。恢复时降低并发不会取消已有任务，会先查询所有旧任务，再按新的上限补位。

- 断网、429、临时 5xx：继续查询同一 ID，不重新上传。401/403 等永久 HTTP 错误停止并保留记录。
- 本地总等待超时：退出非零，保留 ID；可增加 `--poll-timeout` 后用相同目录续跑。
- 输入内容、工作流、tier、请求字段改变：拒绝混用旧结果，要求新输出目录。已记录文件从扫描清单中消失也会拒绝续跑，避免漏掉仍在服务端运行的任务。
- POST 响应丢失、无法确认 task_id：保留 `SUBMITTING`，停止新增提交。先由运维核查服务端是否接受并找回任务 ID；不能删除记录后盲目重投。当前没有服务端提交幂等键，客户端不能推断“没收到响应就是没提交”。
- `PENDING` 也可能是结果过期或未知 ID，不应自动视为失败重投。及时保存每份成功结果；不要等整批结束后才下载。

新客户端和旧 `two_stage_enqueue.py` 的输出目录、记录格式不同，不混用同一个目录。旧脚本仍支持原环境变量、`.pkl` 输出及旧记录续查；新批次优先使用统一客户端。两者共享滚动调度核心，但保留各自的默认值与请求身份。

## 200 份千页 PDF

客户端可以管理这份文件清单，但这不等于服务端已经通过 20 万页容量验收。整本一个任务，先单份 `--max-in-flight 1` 测量，再试 2、3 份；200 份在客户端等待，不一次上传全部文件。不要切成单页任务来追求 GPU 利用率。

批量前仍须完成服务端长任务执行期限、消息确认/重复投递、结果 TTL、磁盘/RAM、图片阶段积压的专项验收。新客户端六小时等待不会改变这些设置。当前 64 页是内部渲染窗口，失败后没有从第 N 页继续的恢复合同。详细边界见 [AI 指南 §5.3](ai-integration.md#53-多份-4001000-页-pdf-的投递流程)和开发环境的[调优指南 §12](performance-tuning.md)。

## 验证

常规测试：`uv run --group dev pytest tests/test_batch_parse.py tests/test_two_stage_enqueue.py`。

真实回归：`MINERU_RUN_BATCH_PDFS=1 uv run --group dev pytest tests/test_batch_input_pdfs.py -v`。对 input 的 p2 和九页论文整本测试三种工作流、结果落盘及续跑。该组测试访问实际运行服务，维护窗口执行，输出保留私有；不代表千页整本验收。
