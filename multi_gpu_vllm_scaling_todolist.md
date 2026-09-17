# MinerU 4 多卡后续工作

当前基线是 MinerU 4、请求默认 advanced、CPU ONNX 小模型和 Docker VLM。三卡部署采用 **一个容器、三个完整模型副本、一个 API 地址**，恢复旧部署的 vLLM 内部数据并行。启动及维护见[部署说明](mineru_4_upgrade_usage.md#docker-与多卡)。

## 已具备

- [x] `ecosystem.vllm.parallele.config.json` 通过前台 Compose 管理三卡模型服务；GPU 0/1/2，DP=3、TP=1。
- [x] 单个 `MINERU_MODEL_VLM_SERVER_URL`，由 vLLM 根据副本队列分配推理请求，无需应用维护三个 URL。
- [x] Docker 模型与缓存持久化，应用进程不加载大模型。
- [x] SDK 兼容层与页面/图片/Office/MinIO 合同；two-stage 分离 parse、vision、dispatch、merge，支持 normal/urgent 队列。
- [x] 三卡 Compose/PM2 配置回归，以及使用 input PDF 检查三个 engine 推理增量的可选集成测试。
- [x] 重启后的 UVM 设备映射修复、启动等待和模型 `/ready` 探测。
- [x] 隔离解析的渲染池正常退出、共享 Unicode 清理；生产批量脚本滚动窗口和任务 ID 续跑，已比较窗口 3/6/30。详见[第二轮记录](mineru_4_upgrade_usage.md#队列与单文件优化2026-09-18第二轮)。
- [x] 1/3/6 个解析进程、VLM 并发 8/16 的真实 PDF 对照；PM2 配置三个独立 solo/1 parse worker，保留整本及跨页处理。[测量结果与限制](mineru_4_upgrade_usage.md#重启修复与并发优化2026-09-18)见部署记录。

应用 scheduler 的 `GPU_IDS` 仍表示应用进程槽位，未按远端容量调度。三卡内部负载均衡不保证吞吐达到单卡三倍，也不提供单副本故障时的应用重试合同。`flash/basic` 主要不使用 VLM，吞吐也可能受 CPU 小模型和解析任务并发限制。

## 待完成

### 1. 测量与容量

- [ ] 固定同一组 PDF、tier、页范围和视觉开关，对比单卡与三卡吞吐、P95/P99、失败率、CPU 和 GPU 使用率。
- [ ] 在现有 GPU 共享条件下，调整每副本显存、VLM 并发及应用 worker 数，记录可承载的同步/异步流量。
- [ ] 区分解析 VLM 与图片描述模型的瓶颈，评估共享资源竞争。

### 2. 调度与容错

- [ ] 将应用并发槽位与 GPU ID 解耦，定义远端解析容量上限与队列背压。
- [ ] 对连接失败、读超时、5xx 和业务错误分别规定重试策略，明确文档重试的资产清理和幂等规则。
- [ ] 验证 DP 副本或容器失效后的恢复行为；多节点独立端点部署另行实现健康摘除、恢复与跨进程路由。
- [ ] 保留 hard timeout、进程组清理和正常退出等待，不因远端推理丢失任务收尾。

### 3. 队列与可观测性

- [ ] 验证 urgent/normal 负载、chord 汇总及视觉服务满载时的背压。
- [ ] 汇总各 engine 的运行/等待请求、成功/失败计数，记录解析和视觉耗时；日志不包含凭证或上传全文。
- [ ] 补充故障恢复、超时清理和持续负载验收，以同样本质量、成功率和尾延迟确定容量。

## 独立端点部署的边界

`MINERU_VLLM_SERVER_URLS` 是可选的进程内轮换列表，单 URL 优先；新任务子进程会重置轮换状态，不能当成跨任务负载均衡。当前三卡单 URL 方案不依赖此列表。未来多节点路由、熔断和租户配置尚未实现，不应写入 `.env.example` 或对外承诺。
