# MinerU 4 多卡后续工作

当前基线是 MinerU 4、请求默认 standard、CPU ONNX 小模型和 Docker VLM。质量 tier 与推理端点独立配置；本计划不再沿用 3.x 的 backend/原生 async-engine 路线。单卡安装及多 Compose 实例命令见[部署说明](mineru_4_upgrade_usage.md#docker-与多卡)。

## 已具备

- [x] MinerU VLM 独立容器；每个 Compose project 可选择 GPU、端口和显存比例。
- [x] 配置单个 `MINERU_MODEL_VLM_SERVER_URL` 或 `MINERU_VLLM_SERVER_URLS` 列表；单 URL 优先，列表仅进程内轮换。
- [x] SDK 兼容层与页面/图片/Office/MinIO 合同；大模型不在应用进程加载。
- [x] two-stage 分离 parse、vision、dispatch、merge，支持 normal/urgent 队列。
- [x] 单卡 Docker、同步/异步任务和 input PDF 基线验收。

上述能力不等于已完成多卡负载均衡。scheduler 仍按 `GPU_IDS` 创建应用进程槽位，未按照远端 endpoint 的容量调度；任务子进程生命周期也可能使进程内轮换无法在任务之间分摊负载。解析端点尚无明确的故障重试、熔断或健康摘除机制。

## 待完成

### 1. 测量与拓扑

- [ ] 记录 GPU/显存/驱动、每容器模型配置、API/worker 数和同步/异步流量比例。
- [ ] 固定同一组 PDF、tier、页范围和视觉开关，对比吞吐、P95/P99、失败率、CPU 与 GPU 使用率。
- [ ] 确定 MinerU VLM 与图片描述模型的 GPU 分配，评估共享显卡时的资源竞争。
- [ ] 从两个独立 Docker 端点开始验证，再扩至目标卡数；不将多 API 实例数直接视为模型并发容量。

### 2. 调度与端点容错

- [ ] 将应用并发槽位与 GPU ID 解耦，定义远端解析槽位的配置、上限和队列背压。
- [ ] 在调度层明确为任务选择 endpoint，使选择能跨任务持续，而不是依赖新子进程内的初始计数。
- [ ] 对连接失败、读超时、5xx 和业务错误分别规定重试策略；明确文档重试的资产清理和幂等规则。
- [ ] 增加健康探测、临时摘除和恢复机制，验证一张卡停止时其他任务的表现。
- [ ] 保留现有 hard timeout、进程组清理和正常退出等待，不因远端推理而丢失任务收尾。

### 3. 队列、可观测性与验收

- [ ] 在 API 和全部 worker 统一端点池、队列、共享工作区及超时配置，避免跨版本混用任务。
- [ ] 记录 endpoint、排队时长、解析/视觉耗时与错误分类；日志不包含凭证或完整上传内容。
- [ ] 验证 urgent/normal 负载、chord 汇总及视觉服务满载时的背压，防止一阶段耗尽另一阶段容量。
- [ ] 加入跨进程端点分配、故障恢复、超时清理和多卡真实 PDF 集成测试。
- [ ] 定义发布与回滚门槛，逐步增加流量；以相同样本质量、成功率及尾延迟评估是否扩容。

## 当前边界

当前 `MINERU_VLLM_SERVER_URLS` 是已有配置；未来槽位数、熔断和租户路由字段尚未实现，不应写入 `.env.example` 或对外承诺。保持默认 standard 和现有 API 合同，新增多卡机制需独立实现与验收。
