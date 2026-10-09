---
docType: runbook
scope: repo
status: current
authoritative: true
owner: unstructure-serve
language: zh-CN
whenToUse: "When moving an existing MinerU model runtime to GPUStack workers."
whenToUpdate: "When model backend parameters, gateway authentication or rollback change."
checkPaths:
  - deploy/gpustack/mineru-backend.example.json
  - deploy/mineru-vllm/model-entrypoint.sh
lastReviewedAt: 2026-10-09
lastReviewedCommit: 0d58a3e
---

# GPUStack 原生模型托管

GPUStack worker 管理模型容器的创建、停止与恢复，应用 API、Celery、解析池继续由本项目管理。使用 GPUStack 内置网关的模型路由进行负载均衡，后端端口由 worker 动态分配。应用不记录实例端口，也不另设 Nginx 负载均衡。

## 模型与共享 GPU

后端模板见 `deploy/gpustack/mineru-backend.example.json`。沿用已验证的本机镜像、模型卷与下载缓存，worker 必须挂载相同模型卷到 `/opt/mineru`，并只读挂载 `deploy/mineru-vllm/model-entrypoint.sh` 到模板指定路径。不要重新下载权重或在应用环境安装推理引擎。

创建 deployment 时显式复制后端参数，并按目标主机设置 DP 数量、GPU selector 和启动显存比例；TP 保持 1、上下文 8192、每副本 16 序列和 3 GiB KV，保留输出层绑定与 MinerU logits processor。同机 Embedding 与 MinerU 使用不同的 DP RPC、master 和运行时通信端口段，避免 host network 下冲突。错开共享 GPU 上的模型启动，核对预热后的逐卡峰值。

同一原生模型对应一个 deployment，多个 deployment 可以加入同一 GPUStack model route。HTTP 负载均衡负责选择模型实例，各实例内部保留 DP；不能把路由目标数量当作一个请求的 TP 数量。

## 调用方切换

多个应用节点使用同一个仓库、同一份 main 与锁文件。共享参数骨架为 `deploy/gpustack/application.env.example`，逐项合并到现有私有 `.env`；不要整份覆盖，更不要跨节点复制 key、Redis、任务存储或锁目录。模板显式设置各类任务超时，避免 API 的 PM2 env 与普通 worker 的代码缺省不同。应用的实际解析并发与本机 GPU 数量独立；三卡和四卡模型都由各自 GPUStack deployment 管理。

GPUStack 专用部署同时设置 `VISION_PROVIDER=vllm` 和 `VISION_PROVIDER_CHOICES=vllm`。前者只选首选项，不能阻止向其他已配置 provider 回退；API 模板中的允许列表也不会自动传给 Celery。逐个核对 API、ordinary、parse/dispatch/vision/merge 的有效配置及任务 manifest 中的 fallbacks。改变允许列表须先排空，因为执行 profile 也包含回退策略；保留旧结果的真实来源，不能把外部 provider 补做的成功当作纯 GPUStack 验收。

启动应用使用 `deploy/manage.sh start app4`、`deploy/manage.sh start ordinary`，更新后使用相应 restart，并在排空后 `pm2 save`。不按本机三张卡误选 app，也不调用 model/model4。PM2 模板中的每 parser VLM 并发、窗口和 ONNX 线程会优先于 `.env`；调整这些参数时须同步审查模板和所有进程的生效环境，不把单独编辑 `.env` 当作已调优。

应用使用网关通用代理路径 `http://gateway/model/proxy/ROUTE_ID`，保留 `mineru4` 模型别名。私有配置设置 `MINERU_MODEL_VLM_SERVER_URL` 和 `MINERU_MODEL_VLM_API_KEY`；API key 仅具有 inference 权限且限制到对应模型路由。健康检查和 SDK 请求均携带该 key，禁止在命令行、日志或公共配置中打印。

切换前检查 ready/unacked 及 active/reserved/scheduled，等待已提交任务完成。先停止原模型 PM2 组并保存停止状态，防止 Compose 和 GPUStack 同时管理 GPU；再启动 deployment。新模型真实推理通过后更新应用私有连接配置，并按应用解析进程数选择 app/app4 组重载。独立 Qwen 图片模型的地址和 key 分开配置，不替换成 MinerU 路由。

共享网关下，app4 仅表示四个应用 parse worker，不要求本机有四张 GPU，也不会启动模型组。采用四个解析进程时，私有配置设 `MINERU_PARSE_SLOTS=4`、`MINERU_SCHEDULER_WORKERS=4`，API 与全部 Celery 使用同一本机槽目录；管理使用 `deploy/manage.sh ... app4`，普通任务另用 ordinary。不能只重载 app 的前三个 parse worker，留下第四个继续使用旧参数；GPUStack 托管时不执行 model/model4 启动。

MinerU 与独立图片模型分别预算：每文档 VLM 并发、整机解析槽、每文档图片窗口及整机视觉槽不是同一个参数。调优先核对两端实际采样，测单份、合计在途和各阶段等待，再决定进程数。方法及边界见 [调优指南](performance-tuning.md)。

按 `docs/validation.md` 执行真实 DP PDF、HTTP ready 和任务验收，检查每个目标实例及 DP engine 收到请求；单一网关健康响应不足以证明所有副本正确。通用代理路径保留前缀，验收包括带鉴权的 `/health`、`/v1/models` 和实际 PDF。与 Embedding 共卡时另做联合请求并记录显存。

逐卡测试设置 `MINERU_TEST_VLM_URL` 为仅包含一个 deployment 的路由，并在进程内加载 `MINERU_TEST_VLM_API_KEY`，同时认证 `/metrics` 与解析请求；不要把密钥写在命令行。应用的 `MINERU_MODEL_VLM_API_KEY` 与测试变量职责独立。迁移应用时检查 PM2 进程环境的优先级，重载后验证有效连接配置、保存 PM2，并保持原模型组停止。修改执行配置 revision，防止旧任务检查点与新配置混用。

## 回退

先将 GPUStack deployment 副本数设为 0，确认子容器和显存释放；恢复原私有应用配置及原 PM2 模型组，再验证实际 PDF。保留原容器、镜像、模型卷与缓存，不执行 prune。仅停止 worker 不能保证已启动模型已释放。
