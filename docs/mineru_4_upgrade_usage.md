# 部署、维护与恢复

应用使用 Python 3.13.15、MinerU 4.0.2 和 CPU ONNX；大模型由 Docker vLLM 提供。应用依赖以 uv.lock 为准，不在应用环境安装 vLLM。接口选择见 [README](../README.md#如何调用)，验证方法见[验证指南](validation.md)。

所有 shell 命令在仓库根目录执行，文件示例使用相对路径。跨进程或容器的共享目录由部署配置决定，必须指向同一位置；本文不预设机器目录。

## 首次安装

准备 Linux、NVIDIA 驱动及 Container Toolkit、Docker Compose 2.24.4+、PM2 和 uv。Compose 需要已注册的 nvidia runtime，三卡模板要求 GPU 0/1/2 可用。Python 由 uv 管理，不替换操作系统 Python。

```bash
sudo apt install -y libmagic-dev poppler-utils libreoffice pandoc graphicsmagick
uv python install 3.13.15
uv sync --locked --group dev
mkdir -p .secrets
cp -n deploy/secrets.example.toml .secrets/secrets.toml
cp -n .env.example .env
```

根据实际部署填写 .env 与私有 TOML。即使凭证主要来自环境变量，仍需保留 TOML 中 FASTAPI、OPENAI、GOOGLE、VLLM 必需段落。已有部署合并配置，保留凭证、模型缓存和结果。

Celery 需要 Redis；图片增强另需独立多模态服务。本仓库的 PM2 配置不负责创建这两个依赖，先确认其可用地址、认证和归属。已有服务直接复用；新建依赖按各自运维方式管理，不因本项目重启而重建共享实例。

下载和验证 CPU 小模型：

```bash
uv run mineru-kit models download --tier basic --small-backend onnx --source modelscope
uv run mineru-kit models verify --tier basic --small-backend onnx
```

如需覆盖模型目录，在部署配置中设置 MINERU_HOME。Docker 模型及下载缓存使用命名卷，与应用小模型目录分开。

## 配置规则

应用配置优先级为 **进程环境 > .env > TOML 回退值**。PM2 env 属于进程环境，load_dotenv 不会覆盖它；部分空字符串会回退到 TOML，不表示清除原配置。Compose 启动器读取仓库 .env，但 Python 读取 .env 不会替 shell 导出变量。

| 配置 | 代码缺省或部署模板 | 作用 |
| --- | --- | --- |
| MINERU_DEFAULT_TIER | advanced | 直接 SDK 服务调用的兜底；HTTP 缺省固定 advanced |
| MINERU_DEFAULT_METHOD | auto | SDK ocr_mode，可选 auto/txt/ocr |
| MINERU_MODEL_SMALL_BACKEND | 模板 onnx | 应用 CPU 小模型 |
| MINERU_INTRA_OP_NUM_THREADS / MINERU_INTER_OP_NUM_THREADS | .env.example、API 和 parse 模板 16/1 | 每个 ONNX 模型会话线程数 |
| MINERU_MODEL_VLM_SERVER_URL | 模板 http://127.0.0.1:30000 | MinerU 模型端点，不是业务 API 或独立图片模型 |
| MINERU_MODEL_VLM_MODEL | 模板 mineru4 | 容器公开模型名 |
| MINERU_MODEL_VLM_API_KEY | 可选 | 解析模型认证，需与服务端一致 |
| MINERU_MODEL_VLM_HTTP_TIMEOUT / MINERU_MODEL_VLM_MAX_CONCURRENCY | 模板 600 秒 / 8 | 每个解析进程的模型请求预算 |
| MINERU_PROCESSING_WINDOW_SIZE | 模板 64 页 | 内部窗口，不是文件页数上限 |
| MINERU_PARSE_SLOTS | 代码及模板 3 | 本机 API、普通任务与 two-stage 的共享实际解析上限 |
| MINERU_PARSE_SLOT_DIR | 未设置时使用系统临时目录中的 tiangong_mineru_parse_slots | 参与进程必须使用同一锁目录 |
| MINERU_PARSE_SLOT_WAIT_SECONDS | 代码及模板 1800 秒 | 等待共享槽位的上限；计入 scheduler hard timeout |
| MINERU_SCHEDULER_WORKERS / GPU_IDS | 模板 3 / 0 | 每个应用调度池的派发进程数 / 池标识；不控制 Docker GPU |
| CELERY_BROKER_URL / CELERY_RESULT_BACKEND | 模板为本机 Redis DB 0 | API 与 worker 必须一致 |
| CELERY_VISIBILITY_TIMEOUT / CELERY_RESULT_EXPIRES | 模板 21600 / 86400 秒 | 两个 app 共用的 Redis 消息确认期限 / 结果保留时间；不是任务执行期限 |
| MINERU_TASK_STORAGE_DIR | 未设置时使用系统临时目录中的 tiangong_mineru_tasks | 旧任务临时工作区 |
| MINERU_JOB_STORE_DIR | 未设置时使用仓库 output/jobs | 新任务的持久输入、检查点和结果，所有 API/worker 共用 |
| MINERU_VISION_WAVE_SIZE | 代码及模板 32 | two-stage 每波派发的缺失图片数 |
| VLLM_BASE_URLS / VISION_* | 按部署填写 | 独立图片描述模型；采样与并发见调优指南 |

HTTP 的 tier 支持 flash/basic/standard/advanced。直接调用兼容的旧 backend 仅用于映射档位，不恢复本机大模型引擎。完整依赖维护方法见[依赖指南](dependencies.md)。

### 超时区别

- scheduler 的全局 hard timeout 代码缺省为 600 秒；.env.example 将普通/图片解析设为 1800 秒。API PM2 env 也显式设为 1800 秒；普通 worker 读取自身环境及 .env，不能从 API 配置推定其有效值。
- 科研 API 模板的 HTTP 等待为 110 秒、子进程 hard timeout 为 300 秒；Office 转换模板为 600 秒。
- Gunicorn timeout/graceful_timeout 缺省为 1900 秒，API PM2 停止窗口为 1900 秒。worker PM2 停止窗口也为 1900 秒，模型 PM2 为 70 秒、容器为 60 秒。
- 持久 two-stage 的转换/parse 通过隔离子进程执行，MINERU_TWO_STAGE_HARD_TIMEOUT_SECONDS 缺省回退任务预算 1800 秒；普通/图片任务分别读取对应 hard timeout。该预算包含排队槽位和结果传输，不含所有后续图片请求；客户端等待和 PM2 停止窗口均不能替代执行期限。千页任务还需检查消息确认和结果保存期限，见[长任务准入](performance-tuning.md#12-4001000-页整本批量的专项准入)。

## 启动与维护

### 持久任务恢复与磁盘保留

新异步任务将原始输入、整本解析检查点、单图结果及最终业务 JSON 保存在 `MINERU_JOB_STORE_DIR`，缺省仓库 `output/jobs`。部署应使用可靠持久磁盘，API 和全部 worker 解析到同一路径；备份时包含该目录。旧任务仍使用原临时工作区和 Redis 结果，不自动迁移。

恢复依赖完成阶段的原子文件及本机文件锁。解析在完成前被中断时，仍需重做整本解析；已完成解析但图片失败时，可以复用解析及已完成图片。模型、默认 prompt、采样或其他执行配置变化会拒绝复用不匹配的检查点；应恢复原配置再续算，或明确创建新任务。同名模型替换权重时更新 `MINERU_EXECUTION_PROFILE_REVISION`，客户端无法自行识别服务端权重更换。

```bash
# 仅显示身份、状态和阶段，不打印原文、请求参数或错误正文
uv run python -m src.scripts.manage_jobs list

# 已保存任务但 broker 发布结果未确认：沿用原代次重发
uv run python -m src.scripts.manage_jobs recover "$TASK_ID"

# 修复失败原因并确认没有活跃阶段后：增加代次，复用完成阶段
uv run python -m src.scripts.manage_jobs resume "$TASK_ID"

# 默认仅预览；核对保留策略与已下载结果后再 --apply
uv run python -m src.scripts.manage_jobs gc --retention-days 7
uv run python -m src.scripts.manage_jobs gc --retention-days 7 --apply
```

recover 不会重新提交已标记 published 的任务；需要显式恢复时用 resume。resume 遇到活跃阶段、已成功或已过期任务会拒绝，不是取消接口。broker 仍不可用时保留已知任务身份，恢复连接后再处理。

清理仅处理超过保留期的 SUCCESS/FAILURE，执行中或下载中的共享租约会阻止删除；保留身份墓碑，避免旧幂等键意外创建新任务。默认未配置自动定时清理；需要周期执行时由运维按容量设置，不能无差别删除共享目录。失败且需要续算的任务应在保留期内恢复。Redis 的 `CELERY_RESULT_EXPIRES` 控制阶段引用/chord 与旧任务结果，不控制新任务的磁盘保留期。

统一入口为 deploy/manage.sh。start 跳过已 online/launching 的组件；修改代码或配置后使用 restart。配置从 deploy/pm2 的模板读取，日志进入 output/logs。

| 组名 | 包含组件 |
| --- | --- |
| model | 三卡 MinerU 模型，PM2 名 mineru-vlm-docker-parallel |
| api | Gunicorn，PM2 名 unstructured-gunicorn |
| workers | 三个 parse，加 vision/dispatch/merge，共六个 two-stage worker |
| ordinary | 普通 Celery worker，PM2 名 celery-worker |
| app | api 加 workers，不含 model 或 ordinary |

启动顺序：

```bash
./deploy/manage.sh start model
# 首次权重下载与编译可能较久，等待此检查成功后继续
curl --fail http://127.0.0.1:30000/health
./deploy/manage.sh start workers
./deploy/manage.sh start ordinary
./deploy/manage.sh start api
./deploy/manage.sh status
pm2 save
```

model 的 PM2 online 不代表模型已就绪。脚本的 status 显示该用户的全部 PM2 进程；logs 仅跟踪所选组的第一个进程，检查其他 worker 时用其完整 PM2 名称。

```bash
./deploy/manage.sh logs model
pm2 logs celery-two-stage-vision --lines 100
uv run celery -A src.services.celery_app inspect active_queues --timeout=5
uv run celery -A src.services.two_stage_pipeline inspect active_queues --timeout=5
```

普通 worker 为 threads/16；two-stage parse 为三个 solo/1，vision 为 threads/32，dispatch/merge 各 threads/4，均 prefetch=1。普通和 two-stage 使用不同任务注册表，队列必须按[普通任务](mineru_with_images_task_usage.md)及[two-stage](two_stage_task_usage.md#队列与配置)对应。不要让普通 worker 消费 two-stage merge 的 default 队列。

### 检查就绪

以下 curl 要求 shell 已有 FASTAPI_BEARER_TOKEN；无鉴权部署可省略该头。

```bash
curl --fail -H "Authorization: Bearer $FASTAPI_BEARER_TOKEN" http://127.0.0.1:7770/health
curl --fail -H "Authorization: Bearer $FASTAPI_BEARER_TOKEN" http://127.0.0.1:7770/ready
curl --fail -H "Authorization: Bearer $FASTAPI_BEARER_TOKEN" http://127.0.0.1:7770/two_stage/queue_status
```

health 只检查 API 存活；ready 并行检查配置的 MinerU VLM 端点健康，不检查 Redis、独立视觉模型，也不实际推理。还需核对队列消费者，并用允许测试的 PDF 检查真实 SUCCESS 与内容，见[验证指南](validation.md)。

### 恢复与停止

恢复时用 start 补起缺失组件；有配置修改时，等待受影响任务收敛后 restart 对应组。首次设置开机恢复执行 pm2 startup，按其提示配置系统服务，再 pm2 save。pm2 resurrect 会恢复该用户保存的全部项目，只恢复本项目时使用上述分组入口。

计划停机先停止新增提交。统一客户端可停止后按原命令加 --resume-only 收取已有任务，保持原输入和输出目录；该选项不取消服务端任务。等待相关队列 ready/unacked 和 worker active/reserved/scheduled 收敛，并保存结果后执行：

```bash
./deploy/manage.sh stop api
./deploy/manage.sh stop ordinary
./deploy/manage.sh stop workers
./deploy/manage.sh stop model
pm2 save
```

仅停止已启动的组。共享 Redis、独立模型及其他项目按各自归属管理。不要全局删除 PM2、清空 Redis、删除共享任务目录或模型卷。未知提交、长时间无进展任务应按 ID 定位，不能通过无差别清理恢复。

## Docker 与多卡

三卡由一份基础 Compose 加一份 parallel 覆盖文件定义，project 为 mineru-vlm-parallel。一个容器绑定 GPU 0/1/2，DP=3、TP=1，每卡完整模型副本，通过单地址分配请求。应用无需设置三个 URL；单次模型生成不会自动分成三卡计算。

| 项目 | 配置位置与模板值 |
| --- | --- |
| 基础镜像 | Dockerfile 的 VLLM_IMAGE 参数，vllm/vllm-openai:v0.21.0 |
| 应用模型镜像 | tiangong/mineru-vlm:4.0.2-vllm0.21.0 |
| 上下文 / 并发序列 | Compose command 的 max-model-len=8192、max-num-seqs=16 |
| 对外端口 / 每卡显存比例 | 三卡 PM2 env 的 MINERU_DOCKER_PORT=30000、MINERU_DOCKER_GPU_MEMORY=0.15 |
| GPU 绑定 / DP / TP | compose.mineru.parallel.yaml 中显式设置，扩卡时一起修改 |
| 模型卷 / 下载缓存卷 | 默认 mineru-vlm-models / mineru-vlm-cache，可用 MINERU_DOCKER_MODEL_VOLUME / MINERU_DOCKER_CACHE_VOLUME 覆盖 |

复用已有命名卷时确认两卷存在，再设置 MINERU_DOCKER_VOLUMES_EXTERNAL=true。不要删除缓存卷以解决普通启动问题。端口默认仅绑定 loopback；跨机器访问需另外配置可达地址与认证。

```bash
docker compose --env-file .env -p mineru-vlm-parallel -f deploy/mineru-vllm/compose.mineru.yaml -f deploy/mineru-vllm/compose.mineru.parallel.yaml ps
curl --fail http://127.0.0.1:30000/metrics
docker compose --env-file .env -p mineru-vlm-parallel -f deploy/mineru-vllm/compose.mineru.yaml -f deploy/mineru-vllm/compose.mineru.parallel.yaml exec -T mineru-vlm python3 -c 'import torch; assert torch.cuda.device_count() == 3; print([torch.ones(1, device=f"cuda:{i}").item() for i in range(3)])'
```

向解析服务提交 PDF 后，检查 metrics 的 vllm:request_success_total 中 engine 0/1/2 的增量；短文档或 flash/basic 任务不一定产生足够请求，不能要求每份文档均分。性能规划见[调优指南](performance-tuning.md)。

### GPU 重启故障排查

nvidia-smi 正常不保证容器内 CUDA 可用。检查宿主 UVM 设备节点、Compose 中 nvidia-uvm 与 nvidia-uvm-tools 映射、Container Toolkit/CDI 配置及容器日志。启动器会等待设备就绪，约 120 秒后仍缺失则失败。

驱动升级后，CDI 可能遗漏设备或引用过期的驱动库挂载。先备份并核对实际配置，再按安装方式刷新 CDI；不要盲删缺失挂载、重启共享 Docker 或卸载驱动。修复后执行实际 CUDA 运算和 PDF 验收。

### 可选拓扑与监控

单卡使用 deploy/pm2/ecosystem.vllm.config.json；该模板不在 manage.sh 的 model 组中，需要在仓库根目录直接管理，并同步应用模型地址。三卡与单卡二选一，不能让两套启动器争用同一端口或 GPU。

独立端点模板和多 API 模板是可选示例，不自动扩展模型容量。MINERU_VLLM_SERVER_URLS 只在解析进程内轮换，缺少跨进程调度和故障切换保证，不等同于三卡内部 DP。

两份 Flower 模板分别对应普通和 two-stage app，均默认端口 5555；需要监控时单独选择启动，同时运行时先改端口。它们不包含在 app 组中。

## 升级与回滚

1. 记录部署提交、uv.lock、Python 版本、镜像标识和有效配置，备份到私有 output 子目录；保留模型卷和业务结果。
2. 在隔离分支或 worktree 中按[依赖维护](dependencies.md)完成兼容检查和[回归验证](validation.md)。
3. 停止新增提交，收取结果并核对所有相关任务与队列。接口载荷变化时，必须在旧任务处理完后共同切换 API 与 worker。
4. 停妥受影响组件再同步环境；不要覆盖或移动运行中的虚拟环境。虚拟环境可执行文件可能记录创建位置，回滚应恢复原环境位置，或按原 Python 与锁文件重新创建。
5. 模型变更重建对应 Compose project；应用变更补起 API 与 worker。检查实际推理、鉴权、文档入口及任务生命周期，通过后 pm2 save。
6. 失败时同步恢复代码、依赖、配置和必要的模型版本；只恢复其中一项可能造成不兼容。

已完成升级的提交与操作过程由 Git 历史追溯。当前文档不维护机器私有目录、历史进程状态或逐轮增长的测试数量。
