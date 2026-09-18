# 依赖与 Python 版本审计（2026-09-18）

这是升级前的审计快照；后续已执行的 Python 3.13/MinerU 4.0.2 部署见[部署验收](mineru_4_upgrade_usage.md#python-313-与-mineru-402-部署验收2026-09-18)。

本报告是开发维护资料，正常提交 Git，不通过 FastAPI 文档路由或 llms.txt 提供。核对源包括当前 pyproject/uv.lock、实际应用和 Docker 环境、PyPI 稳定版本元数据、上游源码与发行说明。

**结论：不应为了四档安装 MinerU `[all]`。当前应用可评估改用基础包；MinerU/DocVortex 和一批兼容更新已在隔离环境验证。Python 3.13 是可进一步灰度的候选，3.14 暂不切换。** 本轮没有替换线上 `.venv`、Python、容器镜像或运行依赖，现有 MinerU 4.0.0 / Python 3.12 部署仍有效。

## 1. 四档与安装 extras 是两回事

依据 [MinerU 4.0.2 的 pyproject.toml](https://github.com/opendatalab/MinerU/blob/mineru-4.0.2-released/pyproject.toml)：

| 安装方式 | 增加的内容 | 本项目如何选择 |
| --- | --- | --- |
| `mineru` | 基础解析、CPU ONNX 小模型，以及默认 llama.cpp 相关包等 | 本机 ONNX + 远端 Docker VLM 应用侧的精简候选 |
| `mineru[torch]` | Torch、torchvision、transformers、accelerate、safetensors | 使用 Torch 小模型时需要；现有应用是可运行的较大依赖集合，不表示只能用部分档位 |
| `mineru[full]` | 在 torch extra 上增加 Linux vLLM / Windows LMDeploy 等 | 需要在同一环境运行这些推理引擎时评估 |
| `mineru[all]` | 当前是 `mineru[full]` 的别名 | 不应为了 flash/basic/standard/advanced 直接使用 |

四档是解析质量/计算路径；small_backend 和 VLM engine/server_url 是推理实现选择。基础包在现有 ONNX 模型、配置和远端 VLM 可用时，可以提供四档；不是说不准备模型/服务就能离线完成所有推理。[MinerU 4.0 发布说明](https://github.com/opendatalab/MinerU/releases/tag/mineru-4.0.0-released)对此作了独立配置说明。

真实验证：创建没有 torch 的 Python 3.12 隔离环境，安装 MinerU 4.0.2 基础包，连接原有 Docker 模型服务。`input/p2.pdf` 的默认及四档整本回归 **5 项通过**，包括两页、表格和 checkbox。再安装应用依赖（删除显式 torch 和 mineru 的 torch extra）后，常规 **177 项通过**。

该精简环境有 147 个发行包，线上当前 177 个包含本项目 editable 注册；排除本项目注册差异后，约减少 **29 个依赖包**，主要是 Torch/torchvision/transformers/accelerate、CUDA/NVIDIA、Triton 和相关依赖。这是依赖数量，不是已测的解析加速比例。

## 2. 当前版本与更新范围

完整逐包清单：[dependency-inventory-2026-09-18.csv](dependency-inventory-2026-09-18.csv)。范围为应用环境 177 个已安装发行包（含本项目），按当前 Python 3.12 可用稳定版本检查，32 个显示有新版。候选列来自保持当前依赖声明不变的隔离 `uv lock --upgrade`，不是手工逐包强制安装；清单中的 latest 不意味着可兼容升级。

### 2.1 可由现有约束解析出的 11 项更新

| 包 | 当前 | 候选 | 处理意见 |
| --- | --- | --- | --- |
| mineru | 4.0.0 | 4.0.2 | 优先纳入下一次维护；基础包精简方案一起验证 |
| docvortex | 0.4.9 | 0.4.12 | 随 MinerU 更新，4.0.2 要求至少 0.4.12 |
| protobuf | 7.36.1 | 7.36.2 | 兼容补丁候选 |
| huggingface-hub | 1.31.0 | 1.32.0 | 候选；验证模型缓存/下载与访问配置 |
| billiard | 4.2.4 | 4.3.0 | 候选；保留 Celery/进程生命周期回归 |
| pandas | 3.0.5 | 3.0.6 | 传递依赖补丁候选 |
| idna | 3.19 | 3.20 | 传递依赖候选 |
| platformdirs | 4.11.9 | 4.11.10 | 传递依赖补丁候选 |
| wcwidth | 0.8.3 | 0.8.4 | 传递依赖补丁候选 |
| cuda-bindings | 13.4.1 | 13.4.2 | 仅在保留当前应用 Torch 栈时有意义；精简方案移除 |
| cuda-pathfinder | 1.8.1 | 1.8.2 | 同上 |

MinerU 最新稳定 4.0.2 已核对 [PyPI](https://pypi.org/project/mineru/4.0.2/) 与[发布记录](https://github.com/opendatalab/MinerU/releases/tag/mineru-4.0.2-released)。其主要更新涉及 HTML 预览、日志和 DocVortex；本服务未开放 HTML 输入，因此不能把这些更新宣传成 PDF 速度提升，也不借升级扩大输入边界。

保留当前 Torch extra 的完整候选环境，常规 **177 项通过**。最初临时 worktree 位于 `/tmp` 时，Docker Snap 的路径隔离导致 Compose 测试读不到文件；改为用候选解释器执行原仓库测试后全部通过，没有把这个基础设施失败归因于新库。

### 2.2 有新版，但必须保留上游约束

| 包 | 当前 / PyPI 最新 | 当前限制 |
| --- | --- | --- |
| openai | 2.54.0 / 3.15.0 | MinerU 4.0.2 明确要求 `<3`；不是仅删掉本项目上限就能升级 |
| redis（Python 客户端） | 6.4.0 / 8.1.0 | Kombu 的 Redis extra 要求 `<6.5`；与 Redis 服务端版本不是同一概念 |
| pydantic-core | 2.46.5 / 2.49.0 | Pydantic 2.13.5 精确要求 `==2.46.5` |
| mpmath | 1.3.0 / 1.4.1 | SymPy 要求 `<1.4`；精简 Torch 后可随依赖图移除 |
| tomlkit | 0.14.0 / 0.15.1 | Gradio 要求 `<0.15` |
| websockets | 16.1.1 / 17.1 | google-genai 要求 `<17` |
| cuda-toolkit 与多项 nvidia-* | 多个新版，详见清单 | Torch 2.14.0/CUDA 配套约束，不独立逐项追最新版 |

版本约束已从已安装 distribution metadata 和候选解析结果核对；不要使用 `--no-deps`、手工覆盖 site-packages 或忽略 pip check 来绕过。

### 2.3 主要直接依赖当前无须为版本号而更新

FastAPI 0.141.1、Starlette 1.6.0、Uvicorn 0.53.0、Gunicorn 26.2.0、Pydantic 2.13.5、Celery 5.6.3、google-genai 2.24.0、Torch 2.14.0、torchvision 0.29.0、ONNX Runtime 1.30.0，以及当前 Pillow、MinIO、python-docx、Black/Ruff/Pytest 等，在本轮检查中没有对应的可用稳定更新。具体锁定值见清单；这不替代后续安全公告跟踪。

## 3. Docker 模型环境应单独管理

只读检查的实际容器：MinerU 4.0.0、DocVortex 0.4.9、vLLM 0.21.0、Torch 2.11.0+cu130、torchvision 0.26.0+cu130、OpenAI 2.54.0。它与应用 Torch 2.14.0 是不同环境，不能要求二者 Torch 版本相同。

- 下一次镜像维护可先只升级 MinerU/DocVortex，保留 vLLM 镜像提供的 Torch/CUDA 组合，执行 pip check、实际 CUDA 运算、三 engine 推理和 PDF 回归。
- PyPI vLLM 最新为 0.29.0，但 MinerU 4.0.2 当前要求 `<0.29.0`；满足此范围的最新稳定版为 0.28.0。**0.28.0 只是声明兼容候选，不是本项目已经验证的新镜像基线。**
- vLLM 跨多个版本升级需要单独核对 DP/TP、CUDA/驱动、模型架构、服务参数、指标和图像处理；不要借应用 `uv lock --upgrade` 顺便替换线上容器。
- 容器目前 `mineru[torch]` 与已存在的 vLLM/Torch 配套使用；没有必要安装 all 让解析器重新选择另一套本机引擎。是否进一步精简容器依赖应单独 build 验证，本轮未重建模型镜像。

官方来源：[MinerU extras/依赖定义](https://github.com/opendatalab/MinerU/blob/mineru-4.0.2-released/pyproject.toml)、[vLLM 发行包](https://pypi.org/project/vllm/)。

## 4. 依赖声明还可怎样整理

1. 应用 CPU/HTTP 方案可将 `mineru[torch]` 改成 `mineru`，并去掉显式 torch。真实四档已过初步验证；正式上线仍要覆盖 Office、图片增强、MinIO 和正常重启。
2. 当前代码直接使用 httpx、requests、pypdfium2、docvortex、kombu/redis，multipart 接口依赖 python-multipart；这些目前部分依赖上游间接带入。下一次整理应显式声明直接使用的依赖，并保留上游兼容范围，避免某次上游删依赖后本服务缺包。
3. `toml` 目前用于加载配置；Python 3.12 已有 tomllib，可在保持现有配置/异常合同的前提下替换，不必另装新的 TOML 库。`tomlkit` 是 Gradio 的独立传递依赖，不能混为一谈。
4. Flower 属于运维监控，可评估独立 ops extra/group，但要同时更新 PM2 启动文档；不是本轮直接删除它。
5. 系统 uv 当前 0.9.2，而最新为 0.12.16。旧 uv 的内置 Python 下载目录明显落后；本轮用隔离的 `uvx --from uv==0.12.16 uv ...` 准备候选解释器，未替换系统 uv。uv 工具升级影响安装/锁定能力，不直接提升正在运行的 API 推理速度。

## 5. Python 升级的收益和风险

### 5.1 当前运行版本不是“未打补丁的原始 3.12.3”

当前解释器显示 Python 3.12.3，Ubuntu 包实际为 `3.12.3-1ubuntu0.17`，构建日期为 2026-08-31。发行版会回移补丁，不能仅凭 `python -V` 断言存在安全漏洞。上游当前 3.12.14、3.13.15、3.14.7；是否迁移由包来源、补丁支持期和回归决定，不覆盖 `/usr/bin/python3`。

支持状态与版本来源：[Python 版本状态](https://devguide.python.org/versions/)、[Python 下载](https://www.python.org/downloads/)、[3.13.15](https://www.python.org/downloads/release/python-31315/)、[3.14.7](https://www.python.org/downloads/release/python-3147/)。

### 5.2 本项目的隔离验证

| 环境 | 验证结果 |
| --- | --- |
| Python 3.12.3 + 11 项兼容更新，保留 Torch | 常规 177 通过 |
| Python 3.12.3 + MinerU 4.0.2 CPU 精简依赖 | 常规 177 通过；p2 缺省/四档 5 项真实 PDF 通过 |
| Python 3.13.15 + 同类 CPU 精简依赖 | 常规 177 通过；p2 缺省/四档与九页论文 6 项真实 PDF 通过；真实 scheduler 嵌套进程请求成功 |
| Python 3.14.7 + 同类 CPU 精简依赖 | 常规 177 通过；真实 scheduler p2 请求成功，但退出出现 semaphore 清理警告，需要进一步处理 |

表中常规测试均为 177 通过、20 跳过；真实模型用例默认需环境开关单独运行，通过数量已另列。常规测试仍有 TestClient/httpx、AnyIO 及多线程进程 fork 的弃用警告；3.14 另有 google-genai 相关弃用提示，不能将通过理解为零警告。

所有真实解析都访问现有 Docker 模型服务，没有用替身冒充推理。这里没有完成新容器、千页整本、长时间压力、故障恢复或新版 Celery worker 的完整上线验收；常规测试也不等于没有版本迁移风险。

### 5.3 性能实测

用同一份真实解析资产、相同 `_merge_content` 逻辑，预热后七轮，每轮 300 次，取每次拼接耗时中位数。没有启用 cProfile，因此不能与此前包含剖析开销的拼接数字直接比较。

| 真实资产 | Python 3.12.3 | Python 3.13.15 | Python 3.14.7 |
| --- | ---: | ---: | ---: |
| 九页论文，159 块 | 0.403 ms | 0.364 ms | 0.387 ms |
| fese，877 块 | 1.664 ms | 1.473 ms | 1.796 ms |

3.13 在这项拼接微基准约快 10%–11%，但节省的是零点几毫秒，不能推导出几十秒/分钟的模型解析同幅度加速。3.14 在 fese 项反而更慢；这只是共享主机与不同解释器构建的测量，3.12 使用 GCC、候选使用 Clang，不是纯粹只改变语言版本的因果实验。

解析主耗时在 ONNX/原生代码、PDF 渲染和远端模型等待，应用 Python 升级不会更换 Docker 中的 Python、Torch 或推理内核。不能承诺整体显著加速。

### 5.4 为什么暂不直接切换 3.14

Python 3.14 将 Linux 默认 multiprocessing/ProcessPoolExecutor 启动方式由 fork 改为 forkserver；本项目 scheduler 目前使用默认上下文，这会改变嵌套进程、继承状态和资源跟踪行为。[Python 3.14 官方变更](https://docs.python.org/3.14/whatsnew/3.14.html#multiprocessing)

本轮两次实际 scheduler 请求均成功，但 3.14 在两次退出时均出现 resource_tracker 的 10 个 semaphore 清理警告；单看 177 项测试全部通过会遗漏这一点。迁移前需明确进程上下文并验证 hard timeout、父进程退出、渲染池回收、重复请求和 PM2 停机，不应直接把默认方式改回 fork 就宣称完成修复。

普通 CPython 升级也不会自动开启 free-threaded 或 JIT；这些是单独的运行/构建选择，与原生扩展兼容和线程安全有关，本轮没有采用或验证。更高版本可能改善特定 bug，也可能增加兼容工作，稳定性必须由实际运行回归支持。

## 6. 建议的实施顺序

1. **先做应用依赖维护**：以 MinerU 4.0.2 / DocVortex 0.4.12 为候选，采用兼容补丁并评估 CPU 精简方案，补全直接依赖声明；保留现有部署 Python 以便隔离变量。
2. **独立做 Docker 维护**：先保持 vLLM 0.21.0 / 配套 Torch，验证 MinerU/DocVortex 更新；跨版本 vLLM 升级另开完整 GPU 回归。
3. **Python 3.13 灰度**：用独立 `.venv`、真实 API/Celery/Office/MinIO/图片任务和故障场景验证，测端到端与内存，再决定是否迁移；不以微基准为唯一理由。
4. **Python 3.14 暂缓**：先解决或解释真实进程生命周期警告，再做长期回归。

依赖和 Python 升级不会自动解决长任务 Redis 一小时消息确认期限等配置问题。400–1000 页批量仍须按[调优指南](performance-tuning.md)完成专项准入，不能因这次候选测试通过就放开容量承诺。

私有复现材料：`output/dependency-audit/`，含 PyPI 元数据、全部包清单、候选锁文件、隔离环境测试日志、真实 PDF 资产和 Python 拼接/进程实验。线上锁文件与环境未替换，候选锁文件不是已部署配置。
