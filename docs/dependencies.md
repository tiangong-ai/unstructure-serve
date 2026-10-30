---
docType: runbook
scope: repo
status: current
authoritative: true
owner: unstructure-serve
language: zh-CN
whenToUse: "When installing, upgrading or checking application and model dependencies."
whenToUpdate: "When Python, package constraints, lockfile or model image dependencies change."
checkPaths:
  - pyproject.toml
  - uv.lock
  - .python-version
  - deploy/mineru-vllm/Dockerfile
lastReviewedAt: 2026-10-12
lastReviewedCommit: f454ed87411bec0a795778b3a521f3baf94d5b0f
---

# 依赖与 Python 维护

本文件供开发维护使用，不通过服务文档路由提供。应用版本以 `.python-version`、`pyproject.toml` 和 `uv.lock` 为准；模型环境以 `deploy/mineru-vllm/Dockerfile` 及实际镜像为准。

## 环境边界

| 环境 | 当前约定 | 验证方式 |
| --- | --- | --- |
| 应用 | Python 3.13.15、MinerU 4.1.2 基础包、DocVortex 0.6.8、mineru-vl-utils 2.0.8、CPU ONNX；不安装 Torch/vLLM | `uv sync --locked --group dev --check`、`uv pip check` |
| Docker 模型 | 模板缺省 vLLM 0.21.0 配套的 Torch/CUDA，新镜像安装 MinerU 4.1.2、DocVortex 0.6.8 与 mineru-vl-utils 2.0.8；GPUStack 示例使用 vLLM 0.28.0 | 构建中的 `pip check`、容器 CUDA 计算及真实 PDF |
| 系统工具 | LibreOffice、Poppler、Pandoc 等 | [部署检查](mineru_4_upgrade_usage.md)与 Office 回归 |

四档质量是解析选项，不是安装 extras。CPU ONNX 加 Docker VLM 的应用使用基础 `mineru`；不要为了 `flash/basic/standard/advanced` 安装包含本地模型引擎的 extra。`uv.lock` 可能含其他操作系统的条件依赖，不能只搜索锁文件中的包名来判断本机是否安装。

本服务明确使用 `MINERU_MODEL_SMALL_BACKEND=onnx`、`MINERU_MODEL_ONNX_DEVICE=cpu`。SDK 4.1 的默认设备为 auto，Linux 基础安装也可能带有 WebGPU provider；已安装 provider 不代表启用了 GPU。适配层在 `.env` 之后补齐 CPU 缺省、再惰性读取 `get_config()`，并拒绝实际配置中的 gpu/auto/torch；进程环境仍优先于 `.env`，不会被静默覆盖。四档、就绪检查和持久任务使用同一策略，大模型继续由独立 vLLM 服务在 GPU 推理。

## 兼容升级流程

1. 在独立 Git 分支或 worktree 中保存修改，记录原部署提交、锁文件、Python 版本、模型镜像及私有配置。
2. 先读目标版本发行说明和依赖约束，再运行解析器求解；兼容最新版不等于忽略上游版本上限。不要以单独 `pip install -U` 替换已锁定环境。
3. 在隔离环境生成候选锁文件，检查升级与移除项，再运行常规测试及[真实 PDF 回归](validation.md)。应用与 Docker 分别检查，不混用依赖。
4. 确认在途任务收敛后，按[升级与回滚](mineru_4_upgrade_usage.md#升级与回滚)切换；保存中间提交，逐次审查暂存改动。

执行 profile 包含 SDK/DocVortex 版本和 ONNX 设备。4.0 到 4.1 的旧检查点不会因两边都使用 CPU 而自动兼容；切换前排空任务，需要续算旧任务时保留并恢复原代码、锁文件及私有配置，不手工补写 manifest 字段或摘要。

以下命令只在隔离的开发环境执行，不直接修改运行服务使用的环境：

```bash
uv lock --upgrade-package mineru --upgrade-package docvortex --upgrade-package mineru-vl-utils
uv sync --locked --group dev
uv pip check
uv run --group dev pytest
```

本次三件套升级先调整 `pyproject.toml` 的约束，再运行上述定向锁更新；仅更新必要包及新引入的传递依赖，审查实际锁文件差异，不使用 `uv lock --upgrade` 扩大到无关依赖。锁文件已经过审查、只需初始化时使用 `uv sync --locked`，不要附加升级选项。根据改动范围重新检查四档、Office、视觉、任务失败传播、进程退出和文档字段。

### 兼容上限

不能用 `pip list --outdated` 的最新版列表直接覆盖锁文件。当前阻止升级的上游约束如下；更新上游时重新核对包元数据：

| 包 | 约束来源 | 当前选择 |
| --- | --- | --- |
| OpenAI SDK | MinerU 4.1.2 要求 `openai>=1.70,<3` | 2.54.0，不能直接换成 3.x |
| Redis Python 客户端 | Kombu 的 Redis extra 要求 `<6.5` | 6.4.0；与 Redis 服务端版本是两回事 |
| pydantic-core | Pydantic 2.14.0 精确依赖 `==2.50.0` | 随 Pydantic 一起升级 |
| tomlkit | Gradio 要求 `<0.15` | 0.14.0 |
| websockets | google-genai 要求 `<17` | 16.1.1 |

MinerU 4.1.2 的 `full` extra 声明 `vllm>=0.19.1,<0.31.0`。模型镜像模板缺省使用 vLLM 0.21.0；0.28.0 可通过私有环境配置覆盖基础镜像和版本校验参数。每种 MinerU/vLLM 镜像组合都须分别验收；更换 vLLM 同时更换 Torch/CUDA 与推理实现，需检查 CUDA、MinerU logits processor、全部 DP 副本和真实 PDF。vLLM 0.28.0 对缺少顶层 `tie_word_embeddings=true` 的旧 MinerU2.5 Pro 2605-1.2B 缓存必须显式设置该值，否则真实生成会退化为重复符号并产生空解析；上游模型仓库已在 2026-10-08 的 config.json 补齐该字段，但 Compose 仍通过 `--hf-overrides` 保证旧缓存正确。vLLM 0.28.0 要求镜像内 FastAPI `>=0.133,<0.137`；可用 `MINERU_DOCKER_FASTAPI_VERSION=0.136.3`、`MINERU_DOCKER_STARLETTE_VERSION=1.6.0` 选择与该基底兼容的版本，构建时运行 `pip check`。不要把模型环境版本写入应用锁文件。版本依据为 [MinerU 4.1.2 包元数据](https://pypi.org/pypi/mineru/4.1.2/json)、[模型配置提交](https://huggingface.co/opendatalab/MinerU2.5-Pro-2605-1.2B/commit/08aaea840498d49ce16247b6263196cf02814885)和 [vLLM 0.28.0 发行说明](https://github.com/vllm-project/vllm/releases/tag/v0.28.0)。

vLLM 0.21.0 和 0.28.0 都支持 `--kv-cache-memory-bytes`。容器入口按私有 `.env` 选择固定 KV 字节数或显存比例，无需为切换预算修改镜像依赖；具体键及验证见[部署说明](mineru_4_upgrade_usage.md#docker-与多卡)。

MinerU 4.1.2 要求 DocVortex `>=0.6.8,<0.7`、mineru-vl-utils `>=2.0.8,<3`、pypdfium2 `>=5.13,<6` 和兼容平台的 ONNX Runtime；应用锁文件和新模型镜像选择 DocVortex 0.6.8、mineru-vl-utils 2.0.8。`uv.lock` 定向更新三件套及其必需传递依赖，其余兼容版本继续锁定。模型镜像保留与 vLLM/Torch/CUDA 配套的推理环境，FastAPI/Starlette 与独立应用环境分别求解，不能把应用锁文件直接安装进模型容器。约束依据见 [MinerU 4.1.2](https://pypi.org/pypi/mineru/4.1.2/json)及 [DocVortex 0.6.8](https://pypi.org/pypi/docvortex/0.6.8/json) 包元数据。

本服务通过无状态 `mineru.parser.parse` 保存 `ParseResult`/MiddleJson，再生成既有 HTTP 结果；同步和异步入口的等待、持久任务、恢复及产物保留继续由本服务管理。4.1 上游 Tasks、file-parse 和 Router 的 HTTP 迁移不改变本服务的六个 POST 入口、默认 advanced 档位或持久任务协议，本服务不接入其资源回收策略。接口差异见 [4.1 迁移说明](https://github.com/opendatalab/MinerU/blob/mineru-4.1.2-released/docs/zh/reference/migration_4_1.md)。

4.1 使用惰性的 `get_config()`，不再提供模块级 `mineru.config.config`；解析、`/ready` 和持久执行配置读取须共同适配。公开 `ParseResult` 也不再导出 `model_output.json`。本服务保留旧诊断参数的调用签名，dump 请求会警告该原始诊断不可用，仍保存 MiddleJson、内容列表和图片；不能从 MiddleJson 伪造同名原始模型文件。依据见 [配置源码](https://github.com/opendatalab/MinerU/blob/mineru-4.1.2-released/mineru/config.py)及 [ParseResult](https://github.com/opendatalab/MinerU/blob/mineru-4.1.2-released/mineru/parser/base.py)。

4.0.12 修复原生 PDF 的中日韩标点与空格处理、版面分类阈值及 Torch/ONNX 过滤一致性，并通过 DocVortex 保存 DOCX/PPTX 表格中的富文本链接；mineru-vl-utils 2.0.7 在内容裁剪和识别前抑制同类型、同角度的重复布局块。解析内容与块数量可能变化，升级须按同一源文件检查正文、表格、页码和图片。版本依据见 [MinerU 4.0.12](https://github.com/opendatalab/MinerU/releases/tag/mineru-4.0.12-released)及 [mineru-vl-utils 2.0.7](https://github.com/opendatalab/mineru-vl-utils/releases/tag/mineru_vl_utils-2.0.7-released)。

相对 4.0.7，DocVortex 0.5 系列改进原生 PDF 处理；MinerU 修复英文词间空格、改善并发渲染，并移除 OpenCV 直接依赖。上游 DocVortex 性能数据不代表本项目含模型推理的端到端提升，需按相同文件、档位和并发做实测。ONNX 会话在没有显式线程设置时读取 `MINERU_INTRA_OP_NUM_THREADS` / `MINERU_INTER_OP_NUM_THREADS`，上游最终回退为 4/1；本项目部署模板显式使用 16/1。资产文件名由 SDK 保存后的引用决定，不能依赖旧哈希命名；升级须验证真实图片可解码、分辨率和下游视觉筛选。变更依据见 [MinerU 4.0.8](https://github.com/opendatalab/MinerU/releases/tag/mineru-4.0.8-released)、[4.0.9](https://github.com/opendatalab/MinerU/releases/tag/mineru-4.0.9-released)、[4.0.11 发行说明](https://github.com/opendatalab/MinerU/releases/tag/mineru-4.0.11-released)及 [DocVortex 性能说明](https://github.com/myhloli/DocVortex#pdf-performance)。

## Python 版本选择

更换 Python 可能改善部分 CPU 工作，但模型推理、网络或磁盘占主要耗时时，端到端收益可能很小。用相同 PDF、档位、模型、并发和预热条件分别测量；不能把调度优化收益归因于 Python 升级。

迁移前确认所有依赖有兼容的 wheel，并专项验证 multiprocessing 的启动方式、嵌套渲染池、文件锁和退出清理。不要替换系统 Python；不要移动正在运行的虚拟环境。Python 版本范围和 `.python-version` 应与锁文件及验证结果同步更新。

性能测量方法见[调优指南](performance-tuning.md)；升级前的依赖快照与已完成评估由 Git 历史追溯，不作为安装依据。
