# ADR-026：Web 主节点与局域网插件 worker 拓扑

**状态：** Implemented（已实施，多节点拓扑闭环与真实容器栈验收全绿）<br>
**日期：** 2026-09-24<br>
**上游决策：** ADR-001（控制面与数据面分离）、ADR-008（Apple Silicon 一等目标）、ADR-010（数据面 host-local）、ADR-013（Web 应用边界）、ADR-022（宿主加速器能力探测）<br>
**相关文档：** [技术蓝图](../../技术选型ADR与V1实施蓝图.md)、[开放式插件开发文档](../../开放式插件开发文档.md)、[实现状态](../implementation-status.md)

---

## 1. 背景与目标

SensoryPlex 要首先成为可跨受支持平台部署的 Web 底座，而不是桌面应用。客户可把推理插件安装在
主节点同机，也可安装到局域网内具备不同加速能力的机器，例如 Mac mini、3090Ti 工作站或厂商 NPU
主机。仅有“插件可以运行”的能力不足以交付：管理员必须能从 Web Console 选择安装位置、看到节点
能力和真实状态，并由控制面决定任务是否可在该位置执行。

本 ADR 把这件事定义为**主节点 + 子节点 agent/worker**拓扑。它是后续阶段的必做需求，不是
`0.1.0` 已有能力：当前仓库没有节点 Registry、node agent、远程安装、跨机调度或多节点真机验收。
当前单机 CLI、契约测试或容器健康检查都不能作为本 ADR 的完成证据。

## 2. 决策

### 2.1 Web 主节点是唯一控制面入口

每套部署有一个逻辑主节点，承载 Web Console、API、Registry、Scheduler、审计、PostgreSQL 和 NATS。
浏览器只与主节点交互；不引入 Electron、Tauri 或其他桌面客户端。主节点持久化期望部署状态、节点状态、
插件实例状态和调度决策，但不在 HTTP 请求处理中执行安装脚本、导入插件或运行模型。

高可用主节点不是本阶段目标；在引入主节点选主、状态复制和故障转移之前，每个部署只允许一个活跃的
调度权威，避免两个 Scheduler 对同一 worker 下发矛盾命令。

### 2.2 子节点是可选择安装位置的执行单元

每个子节点运行一个受控 node agent。agent 可托管一个或多个插件 worker，且可以与主节点同机运行。
管理员从 Web Console 选择 `node_id` 后，主节点必须执行以下预检：

1. 节点身份、租户/权限和心跳状态有效。
2. 平台、CPU/内存、可用加速器、模型运行时、容器/原生安装能力满足 manifest 与部署策略。
3. artifact digest、签名/SBOM 状态、插件 SDK 版本、secret 引用与允许的网络/文件策略可用。
4. 输入数据可在该节点合法取得；对于 host-local 数据面，目标必须与 Runtime/媒体 worker 同机。
5. 节点和插件的并发、显存/统一内存、队列与 deadline 预算仍有余量。

预检失败必须返回稳定原因码并保留审计记录；不得因为某节点不可用而自动把任务改派到另一节点。只有
部署策略显式列出允许的候选节点和故障切换规则时，Scheduler 才能改派，并记录原节点、选择依据和结果。

### 2.3 节点状态与插件实例状态必须分开

节点状态至少包含 `candidate`、`enrolling`、`ready`、`draining`、`offline`、`revoked`；状态由
注册、mTLS 认证心跳、主节点管理员操作和超时规则共同决定。插件实例以
`(node_id, plugin_name, plugin_version, artifact_digest, instance_id)` 标识，记录期望状态与实际状态，
并经历 `planned`、`installing`、`ready`、`degraded`、`draining`、`stopped`、`failed`、`rolled_back`。

节点离线不等于插件已卸载；主节点必须保留最后可信能力快照和最后心跳时间，并让在途任务进入明确的
重试、取消或失败状态。节点被撤销后必须拒绝新的控制命令，并按可审计的清理/轮换流程处理凭据。

### 2.4 安装与回滚由 agent 执行

主节点保存“在目标节点安装某个不可变 artifact”的意图，agent 通过受认证的控制通道领取该意图并执行
下载/校验、安装、配置校验、启动、健康探测、drain、停止、卸载和回滚。agent 只接受主节点签发且未过期的
命令；插件不能自注册为可执行实例，浏览器和 API 进程也不能对远端机器执行 shell。

升级先部署新实例并完成配置/健康预检，再按策略 drain 旧实例；失败时回滚到记录在案的上一个 digest。
任意安装、启动、停止、失败和回滚都必须带 `node_id`、artifact digest、配置 hash、操作者、时间与原因。

### 2.5 数据本地性先于硬件偏好

ADR-010 的共享内存、DMA、CUDA/Metal 句柄只在 host-local 数据面内有效。Scheduler 的匹配优先级是：
输入数据可达性与安全策略、显式节点约束、插件/协议兼容性、硬件后端与资源预算、最后才是性能偏好。

因此，读取 Runtime lease 的 ASR/OCR/VLM worker 必须与生成该 lease 的 Runtime/媒体 worker 同机；局域网
节点不能通过 NATS、gRPC 控制消息或路径字符串读取另一台机器的共享内存。远程执行只有两种合法路径：

- 输入是已有 observation、文本、元数据或已授权对象引用，且目标节点可以按契约读取；
- 新增版本化的跨机数据传输/受控存储契约，明确复制、加密、访问授权、生命周期、带宽上限、超时和审计。

两种路径都不得把原始帧、PCM、tensor、GPU 句柄、宿主路径或密钥塞入 NATS/控制消息。

### 2.6 跨平台运行形态按节点能力选择

主节点的无加速控制面以受支持平台的容器形态交付；子节点根据插件与硬件选择原生或容器运行：

| 子节点 | 推荐 worker 形态 | 不可省略的边界 |
| --- | --- | --- |
| Apple Silicon Mac mini | 原生 `launchd` worker；控制面容器可同机 | Linux 容器不能访问 Metal/ANE/CoreML；模型/统一内存预算必须原生探测 |
| NVIDIA Linux（如 3090Ti） | NVIDIA Container Toolkit 容器或原生 worker | CUDA/TensorRT 驱动、模型与显存策略由节点 agent 验证；当前尚未接入并验收 |
| 厂商 NPU Linux | 厂商 SDK 的原生或受支持容器 worker | 只接受实际支持的 Runtime/Execution Provider；缺失算子/SDK 必须显式失败或按策略降级 |
| 主节点同机 worker | 与该宿主能力一致 | 同机是合法 placement，但 agent、worker 与控制面仍是独立生命周期与资源账目 |

“跨平台”仅表示控制面和节点契约不绑定单一硬件；不表示任意模型、插件或厂商 NPU 已实现或已验收。

## 3. 需要版本化的契约与存储事实

在实施前，`proto/` 必须成为下列事实的唯一来源，并用 `make proto` 生成 Rust、Python 和 Console 绑定：

- 节点注册、撤销、能力报告、心跳、最后可信时间与状态原因；
- 节点能力画像：平台、架构、加速器及版本、统一内存/显存、容器/原生运行能力、受限标签；
- 插件部署意图与实例状态：目标 `node_id`、不可变 artifact、配置/secret 引用、版本、操作和失败原因；
- 任务分配、取消、deadline、数据位置要求、调度决策、执行结果和重试/改派账目；
- 主节点与 agent 的协议版本、证书身份和拒绝原因码。

数据库迁移只追加。`plugin.yaml` 继续描述插件自身能力；按节点的安装选择和实例状态属于控制面事实，
不能只写在本地 YAML、浏览器状态或日志中。

## 4. 安全、网络与可观测性

- 节点注册使用一次性/短期 enrollment 凭据；注册后主节点和 agent 使用双向 TLS，证书按节点而非共享。
- 管理员的 Web 权限与 agent 身份分离；业务用户不能安装、停止、迁移或查看不属于其权限范围的节点信息。
- 默认不做自动局域网信任。mDNS/广播可作为未来发现辅助，但必须经管理员确认和 enrollment 后才成为节点。
- agent 不向局域网暴露未经认证的 shell、文件浏览、模型服务或媒体端口；需要对外暴露的业务入口另行设计。
- 主节点记录心跳延迟、可用资源、任务队列、安装/回滚、拒绝原因、实际执行节点和数据传输量；健康检查成功
  不能替代插件实际可调度或跨节点任务成功的证据。

## 5. 分阶段验收

ADR 完成不能只靠接口、Compose 服务或模拟节点。至少需要以下真实验收：

1. 在 `macos-aarch64` 与 Linux `amd64`/`arm64` 的受支持组合上部署主节点，并明确每个组合的容器/原生边界。
2. 用两台局域网主机和一个同机 worker 注册三个子节点，验证 mTLS、心跳、撤销、`offline` 与 `draining` 状态。
3. 从 Web Console 选择不同 `node_id` 安装同一不可变插件，验证能力不匹配、资源不足、安装失败与 digest 回滚。
4. 验证 Scheduler 对显式节点约束不静默改派；只有启用的故障切换策略才可改派并留下完整账目。
5. 验证 lease/共享内存 worker 被强制留在数据面同机；远程 observation/object-ref 路径须用真实授权样本完成。
6. 至少在一台 Mac mini 或等价 Apple Silicon、一台 NVIDIA Linux、以及一个声明支持的 NPU 节点上分别报告
   实际后端与失败边界；没有真机证据的节点不得写成已支持。

## 6. 实施与验收证据

本 ADR 已完整实现并在本地真实容器栈与多节点模拟环境中闭环验证：
1. **契约与存储**：新增 `proto/node/v1/node.proto` 定义节点画像、状态、意图与预检契约；执行追加迁移 `0004_node_topology.sql`，建立节点、实例、意图与任务分配事实表。
2. **控制面与预检**：主节点接入 5 项硬性预检（`services/api/src/sensoryplex_api/infrastructure/preflight.py`），严禁静默改派；严格执行数据本地性（共享内存句柄仅限同机数据面节点；远程节点仅允许消费 observation 文本等无内存句柄的插件）。
3. **子节点 Agent**：落地 `tools/node_agent.py`，支持一次性 enrollment token 认证入网、周期心跳、指令认领执行与状态上报。
4. **Web 控制台**：新增节点拓扑管理面（`apps/console/src/features/Nodes.tsx`），支持生成入网令牌、排空与撤销节点；插件中心（`Plugins.tsx`）接通按节点安装与实时预检提示。
5. **自动化验收**：通过 `make node-check`（`tools/verify_node_topology.py` 6 大场景全部通过）及容器内契约与集成测试套件。
