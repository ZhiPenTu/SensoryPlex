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
- [x] ADR-026 的节点注册、预检、数据本地性与审计已验收；本轮 `make node-check` 通过 6 个场景。其中“目录创建/删除”这一路只是**部署意图通路**（创建槽位 + 下发意图 + 状态回执），**不含**真实制品下载、离线安装、进程托管与候选验证。
- [x] ADR-030 插件热部署执行器（`local_native` 首期）已在 macOS 实机验收：`make plugin-deploy-check-api` 92 项（含可观测性指标聚合）+ `make plugin-deploy-check-native`（真实 LaunchAgent、真实首方插件蓝绿/排空/回滚/重启对账）+ Console 浏览器验收；Linux 平台适配器尚未在 Linux 节点验收，不得宣称支持 Linux。
- [ ] 业务 Golden Path 尚未完成：没有证据证明真实媒体会自动经过 Runtime → Timeline → metadata/outbox → 查询与回看；`golden_path_verified` 必须继续为 `false`。

当前依赖顺序：`CP-01 收口当前节点改动` → `GP-01 真实媒体 Golden Path` → `QL-01 质量基线` 与 `OP-01 韧性安全` → `PF-01 跨平台/性能`。

## P0：先关闭正在进行的节点工作

- [x] **CP-01 候选节点自动发现、审批与清理**  
  状态：**已完成**（2026-09-25 经 `make node-check` 与 Console 构建验证闭环）。  
  范围：候选节点只能进入 `candidate` 状态；管理员可接纳、拒绝、删除或清理陈旧节点；Agent 安装器可显式以候选模式自报到。  
  完成条件：
  - 为 candidate-register、接纳/拒绝、删除/清理分别补充 API 集成测试，覆盖未授权、重复注册、撤销后心跳/部署拒绝、审计记录和状态迁移。
  - 接纳前禁止部署/调度，接纳后必须重新走已有预检和数据本地性约束；不能绕过短期令牌或把 candidate 误报为 ready。
  - 在真实 console 中完成一次候选节点出现、审批、拒绝和错误提示的浏览器验收；容器内运行对应 pytest 与 `make console-build`。
  - 验收通过后再提交当前 5 个工作区改动，并将命令与结果写入 `verification.md`。

## P0：V1 的唯一业务闭环

- [x] **GP-01 真实媒体自动入库与可回看 Golden Path**  
  状态：**已完成**（2026-09-25 经 `make golden-path-check` 9 个场景全量验证）。  
  范围：串通 Console 上传 -> 方案发布 -> 任务创建与分发 -> 节点 Agent 调度 Runtime/模型/Timeline 融合 -> 数据库/outbox 授权追加 -> 常驻 relay/index 向量落库 -> 语义检索命中 -> 原片回看流式播放的完整业务闭环。

- [x] **OP-03 真实执行编排闭环（ADR-029 P1）**  
  状态：**已完成**（2026-09-25 经 `make orchestration-p1-check` 7 个场景全量验证）。  
  范围：追加数据库迁移落地 immutable Pipeline revision、Run、Task、Edge 与 assignment 租约；实现事务 CAS/幂等提交、取消传播、有界重试（`retry_wait` / `retry_exhausted`）、崩溃恢复（租约过期自动回收）与数据本地性拒绝（`data_locality_violation`）。

- [x] **OP-04 受控多节点集群编排（ADR-029 P2）**  
  状态：**已完成**（2026-09-25 经 `make orchestration-p2-check` 6 个场景全量验证）。  
  范围：基于多节点注册画像的候选调度、数据本地性过滤（raw buffer 仅限同机，observation 跨机分发至 GPU/Edge 节点）、节点排空/离线硬阻断、双向任务认领（`tasks:claim`）与可审计故障转移（Failover：第一任过期记录保留，第二任备用节点成功接手）。

- [x] **OP-05 插件热部署执行器（ADR-030，local_native 首期）**  
  状态：**已完成**（2026-09-26 经 `make plugin-deploy-check-api`（容器内 92 项）与 `make plugin-deploy-check-native`（宿主真实平台服务 + 真实首方插件 embed-bge-onnx 的 `Describe → ValidateConfig → Start → Health → 蓝绿 → Drain → Stop → 回滚`）验证；另补 `tests/integration/test_plugin_hot_deploy_api.py`（14 项，含此前修掉的意图插件身份、阶段枚举数值、槽位 generation、余量重复扣减、撤销节点清理五个回归，以及本轮新增的指标聚合 / 窗口与节点过滤 / 鉴权三项）与 `tests/contracts/test_plugin_release_bundle.py` / `test_node_agent_hot_deploy.py` / `test_node_agent_unpack_safety.py`（解包上限类拒收）；native 层**可重复运行**——槽位为空时首次部署走 `provision`，槽位已有 active 时自动改走蓝绿 `upgrade` 并在报告里注明；Console「热部署（ADR-030）」页签已通过浏览器验收（UI 真实发起升级并轮询到已完成，「当前 active」= 槽位实时指针 = 本次候选端点）。  
  范围：把“部署意图 + 状态上报”升级为真实执行闭环；**版本化蓝绿切换**（不做进程内 `hotReload`）；不可变 `plugin_release`（可执行代码身份 + 整包传输摘要）；受控平台定向 bundle 与 Agent 离线安装（解包前拒绝路径穿越/符号链接/超限成员/摘要不符，部署期不联网）；generation CAS 切换 active 指针；排空旧实例并保留其 bundle 作回滚版本；显式回滚 = 反向部署操作；Agent 重启只报 `reconciliation_required`。  
  边界：Linux systemd user unit 适配器**尚未在 Linux 节点验收**；容器插件、第三方未签名插件、意图内任意 URL/命令/宿主路径/密钥、跨机共享内存、业务 `Process` 验证与自动 CPU fallback 不做。

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
| `make console-build` | 通过 | console 容器内 `tsc -b && vite build` 成功；插件中心新增“热部署（ADR-030）”页签（release 选择、升级确认、实时阶段/错误码、当前与上一版本、显式回滚、下载/启动/排空耗时）。 |
| `make lint-ruff` / `make test-py` | 通过 | 容器内 ruff 全绿；契约 414 项 + 集成 92 项通过（含本轮新增的热部署契约与集成用例）。 |
| `make plugin-deploy-check-api` | 通过 | api 容器内 92/92：制品仓同步与 5 种拒收、意图形状白名单、逐级推进与 validating 不切换、fencing/重复/陈旧回报、蓝绿与排空、显式回滚、下载授权、余量不足、候选准入、可观测性指标聚合（按 node/plugin/release/kind/stage/reason 分桶且与台账逐项一致、`operation_id` 不进 label、无敏感内容泄漏、鉴权）。 |
| `make plugin-deploy-check-native` | 通过 | 宿主 56/56：真实 LaunchAgent 与真实进程、endpoint 文件契约、8 种 bundle 拒收、7 种候选失败模式、真实首方插件蓝绿/排空/回滚与 Agent 重启对账；**不代表 Linux 已验收**。 |

## 暂不纳入当前承诺

Electron/Tauri、Kubernetes、Kafka、RTMP/桌面采集全面支持、NPU SDK 扩展和 MCP 对外发布不在上述 P0/P1 完成前推进。它们属于蓝图 Gate C 之后的独立决策，不应稀释真实媒体 Golden Path 的收口。
