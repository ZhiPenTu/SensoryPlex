# 开发执行清单

**更新日期：** 2026-09-24  
**适用版本：** `0.1.0` 之后的 V1 收口  
**用途：** 这是日常排期与关闭工作的唯一执行清单；[TODO](TODO.md) 保留详细背景、历史待办和样本缺口，[验证记录](verification.md) 保留可复核证据，不再承担日常任务排序。

## 使用规则

- 状态只使用：`未开始`、`进行中`、`验证中`、`已完成`、`受阻`。只有真实执行完验收并把证据补入 `docs/verification.md` 后，才能勾选完成。
- 任何跨进程字段先改 `proto/`，再运行 `make proto`；数据库变更只能追加迁移。不得以健康检查、跳过测试、合成业务成功数据或页面可打开替代验收。
- Python、前端、集成和事件链路验证在 Compose 容器内执行；Rust/Cargo 与依赖宿主硬件的媒体/模型验收仅按 `AGENTS.md` 的既定例外在主机运行。
- 完成条目时同步更新本文件、`implementation-status.md` 与 `verification.md`。未实现能力必须继续暴露明确的 unavailable/blocker/error 语义。

## 当前基线（已核对）

- [x] 控制面、鉴权、迁移、素材查询/回看、Web Console 和可审计的 revision/来源血缘已具备；素材回看不等于自动媒体入库。
- [x] 文件/SRT 媒体数据面已具备真实解码、抽帧、音频切段、lease、跨进程交接、背压和格式准入；这些均有 macOS 本机证据。
- [x] VLM、ASR、OCR、BGE、Milvus Lite 索引、纯语义检索、outbox → JetStream → sink 消费与事件 profile 已有单项/链路验收。
- [x] ADR-026 的节点注册、预检、数据本地性、部署意图、执行/回滚和审计已验收；本轮 `make node-check` 通过 6 个场景。
- [ ] 业务 Golden Path 尚未完成：没有证据证明真实媒体会自动经过 Runtime → Timeline → metadata/outbox → 查询与回看；`golden_path_verified` 必须继续为 `false`。

当前依赖顺序：`CP-01 收口当前节点改动` → `GP-01 真实媒体 Golden Path` → `QL-01 质量基线` 与 `OP-01 韧性安全` → `PF-01 跨平台/性能`。

## P0：先关闭正在进行的节点工作

- [ ] **CP-01 候选节点自动发现、审批与清理**  
  状态：**验证中**（工作区存在未提交改动；2026-09-24 console 容器生产构建已通过）。  
  范围：候选节点只能进入 `candidate` 状态；管理员可接纳、拒绝、删除或清理陈旧节点；Agent 安装器可显式以候选模式自报到。  
  完成条件：
  - 为 candidate-register、接纳/拒绝、删除/清理分别补充 API 集成测试，覆盖未授权、重复注册、撤销后心跳/部署拒绝、审计记录和状态迁移。
  - 接纳前禁止部署/调度，接纳后必须重新走已有预检和数据本地性约束；不能绕过短期令牌或把 candidate 误报为 ready。
  - 在真实 console 中完成一次候选节点出现、审批、拒绝和错误提示的浏览器验收；容器内运行对应 pytest 与 `make console-build`。
  - 验收通过后再提交当前 5 个工作区改动，并将命令与结果写入 `verification.md`。

## P0：V1 的唯一业务闭环

- [ ] **GP-01 真实媒体自动入库与可回看 Golden Path**  
  状态：**未开始**。  
  范围：把已分别验证的 Runtime、Timeline、元数据、outbox/index、语义检索和 Console 串成一个真实写路径，而非靠手工插入素材事实。  
  完成条件：
  - 使用一段授权本地文件，随后使用受控 SRT 流，各产生至少一个带 `stream_id + [start_ms, end_ms)`、来源、模型/版本、显式置信度语义与 revision 的 `MaterialUnit`。
  - Timeline 能把 ASR/OCR/VLM observation 融合进素材；事务性 metadata/outbox 写入后，由既有事件链路生成 ready embedding。
  - 通过 API/Console 按语义检索命中该素材，并准确回跳到授权原片相同时间区间；失败、缺模型、冲突和超时必须可见，不能补造结果。
  - 新增可重复的端到端验收脚本与真实授权样本登记；容器内验证 API/Console/事件链路，按既定 Rust 例外验证实际媒体和宿主模型路径；记录端到端延迟，尚未达到 2–5 秒则明确失败或待优化。

- [ ] **GP-02 Timeline 语义冲突识别与 revision 策略**  
  状态：**未开始**；依赖 `GP-01`。  
  完成条件：定义可解释的跨模态冲突输入与状态机；旧 revision 不覆盖；至少覆盖冲突、低置信度、模型缺失和重放幂等四种场景。

## P1：让链路结果可信、可运营

- [ ] **QL-01 模型与检索质量基线**  
  状态：**未开始**；依赖 `GP-01`。  
  完成条件：补齐有授权与参考答案的 720p/原始分辨率屏幕文字、带参考文本的语音、多人对话、VFR 长间隙和设备直出样本；报告 OCR 指标、ASR WER/CER、语义检索 recall/MRR，以及抽帧语义漏检率。没有参考样本时只报告链路语义，不能声明质量达标。

- [ ] **OP-01 插件与事件链路韧性**  
  状态：**未开始**；依赖 `GP-01`。  
  完成条件：实现并验收 worker durable 幂等、崩溃/取消/超时后的 lease 回收、dead-letter 与按原因分流、`ack_wait` 重投、向量 GC；将插件签名/SBOM、默认无外网和最小权限从结构检查升级为强制策略。

- [ ] **OP-02 生产存储与节点安全边界**  
  状态：**未开始**。  
  完成条件：验收服务端 Milvus 写入/检索与恢复，不把 Lite 单写进程外推为服务端方案；为跨机 Agent 落地 mTLS、证书轮换、失联恢复与数据本地性审计。高可用主节点选主另行 ADR 后实施。

## P1：目标平台与性能门禁

- [ ] **PF-01 macOS 真机与 Linux NVIDIA 覆盖**  
  状态：**未开始**。  
  完成条件：在 Mac mini 各相关内存档和 Linux NVIDIA 真机上重跑相同媒体、模型、节点和 Golden Path 验收；记录平台、硬件、执行后端、精度、并发、队列策略与 P50/P95/P99，绝不合并跨平台数据。

- [ ] **PF-02 2–5 秒可见性与长稳压力**  
  状态：**未开始**；依赖 `GP-01` 与 `PF-01`。  
  完成条件：以真实流测量 ingest-to-material-visible 延迟、吞吐、背压、断流、OOM 和存储短故障；完成小时级稳定运行、睡眠/唤醒或断电恢复（macOS）与连续重连测试。未达目标时保留可观察降级，不宣称 Golden Path 达标。

## P2：格式、体验与优化（不阻塞 P0 首次闭环）

- [ ] **FM-01 媒体格式剩余边界**：旋转采集/归一化、容器级 VFR、CAPS 动态变化、设备直出样本、SRT 加密与带凭据 publish；每新增支持组合遵守 ADR-009 的矩阵、正样本、拒绝样本和验证记录四件套。
- [ ] **UX-01 检索排序与工作流体验**：在质量集建立后再实现 RRF/混合检索与相关性校准；不能以纯 COSINE distance 充当置信度。随后补方案发布、任务执行和自动来源映射。
- [ ] **PE-01 数据面优化**：只在性能基线证明瓶颈后评估零拷贝、分块哈希/抽样校验、静音切分和机型自动模型/量化选择；每项需保留资源上限与失败语义。

## 本次核对的运行证据

| 检查 | 结果 | 说明 |
| --- | --- | --- |
| `./deploy/status.sh` | 通过 | console、api、gateway、PostgreSQL、NATS、relay、index 均健康；console/api/gateway HTTP 探测成功。 |
| `make node-check` | 通过 | api 容器内 ADR-026 6 个场景全通过；不覆盖 CP-01 的候选节点新流程。 |
| `make console-build` | 通过 | console 容器内 `tsc -b && vite build` 成功；不替代候选审批的浏览器验收。 |

## 暂不纳入当前承诺

Electron/Tauri、Kubernetes、Kafka、RTMP/桌面采集全面支持、NPU SDK 扩展和 MCP 对外发布不在上述 P0/P1 完成前推进。它们属于蓝图 Gate C 之后的独立决策，不应稀释真实媒体 Golden Path 的收口。
