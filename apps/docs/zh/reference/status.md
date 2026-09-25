# 能力实现状态

本页是项目陈述“什么已被证明”的唯一地方。使用三种标签，它们**不可互换**：

| 标签 | 含义 |
| --- | --- |
| **已验证** | 在所述平台与输入上，文档化的命令被成功执行过 |
| **未验收** | 设计与代码存在，但没有成功的端到端执行记录 |
| **未实现** | 本版本里不存在 |

本页描述的是 **0.1.0（可运行工程底座）**。下面的标签反映仓库自身的验收记录；有疑问时，证据列里的命令才是权威。

## 契约、运行时与媒体

| 领域 | 状态 | 证据 | 边界 |
| --- | --- | --- | --- |
| 版本化 Protobuf 契约，Rust/Python/控制台代码生成 | 已验证 | `make proto`、`make test-contracts` | 开发预览；尚未发布稳定 SDK |
| Rust 核心：配置校验、有界队列、descriptor 校验 | 已验证 | `make check`（宿主 cargo fmt/clippy/test） | Pipeline 生命周期、调度、进程与 lease 管理尚未在 Rust 侧实现 |
| 真实 GStreamer 解码 → 有界 arena → `BufferDescriptor` | 已验证 | `make media-replay MEDIA=...` | 授权样本 + 10 个公开许可样本，其中 6 个用于 ADR-009 格式准入矩阵 |
| lease 签发/校验/释放、音频 5 秒切段、带原因的抽帧 | 已验证 | `make media-replay`、`make media-test` | 需要 GStreamer 开发文件 |
| 跨进程数据面（共享内存交接） | 已验证 | `make handoff-check MEDIA=...` | 消费方是验收脚本，还不是生产模型 worker |
| SRT 实时接入与断流/恢复测量 | 已验证 | `make stream-up && make live-check` | 本构建仅回环；SRT 加密与带凭据 publish 尚未做 |
| 运行时能力上报、宿主加速器三态 | 已验证 | `make capability-check`、`make accelerator-check` | 在 `macos-aarch64` 实测；`cuda` 分支**未验收** |
| 端侧模型插件：VLM / ASR / OCR / BGE | 已验证 | `make model-check`、`make asr-check`、`make ocr-check`、`make embed-check` | 每个都是宿主上的四个进程 + 真实授权样本 |
| 模型插件并发准入与重试账目 | 已验证 | `make parallelism-check MEDIA="..." INPUTS=4` | 档位取值来自运行时上报 |

## 事实、向量与事件

| 领域 | 状态 | 证据 | 边界 |
| --- | --- | --- | --- |
| 不可变素材 revision、血缘校验、带鉴权的关键词/标签/时间查询 | 已验证 | `make test-integration` | 需要可写的 PostgreSQL |
| 向量落库与检索闭环 | 已验证 | `make index-check EMBEDDINGS=...` | 真实 PostgreSQL + Milvus **Lite** 上 11 个场景；服务端 Milvus 未验收 |
| 事务性 outbox → NATS JetStream 发布 | 已验证 | `make outbox-check` | **仅发布这一跳**；NATS→sink 消费是另一条路径 |
| 常驻消费循环（JetStream → 向量 sink） | 已验证 | `make consume-check` | 宿主执行、单进程；Milvus Lite 进程独占 |
| compose 内的常驻事件链路与档位准入 | 已验证 | `make event-pipeline-check` | 容器内跑通到运行中的 api |
| 网关语义检索（`mode=semantic`） | 已验证 | `make semantic-check` | 13 个场景。RRF/混合检索与相关性校准未实现 |
| NATS 任务分发 | **未实现** | — | 唯一接线的方向是 事实 → 索引 |

## 拓扑与编排

| 领域 | 状态 | 证据 | 边界 |
| --- | --- | --- | --- |
| 节点拓扑：agent、注册、心跳、5 项硬性预检、数据本地性、部署意图、回滚、审计 | 已验证 | `make node-check` | 6 个验收场景 |
| 跨机 mTLS 轮换、主节点高可用选主 | **未实现** | — | 后续阶段 |
| 编排 P1：持久 Run、幂等、级联解锁/阻断、取消优先、有界重试、崩溃恢复 | 已验证 | `make orchestration-p1-check` | 真实 PostgreSQL 上 7 个场景 |
| 编排 P2：算力感知多节点调度、draining/offline 拒绝、可审计故障转移 | 已验证 | `make orchestration-p2-check` | 6 个分布式场景 |
| 编排 P3：场景产品包、控制台/API 运维、真实媒体业务闭环 | **未实现** | — | 下一个里程碑门禁 |

## 产品面

| 领域 | 状态 | 证据 | 边界 |
| --- | --- | --- | --- |
| 控制台：登录、会话/CSRF/RBAC、真实上传、Range 回看、方案/任务草稿、插件配置版本、作用域凭据、账号角色、审计 | 已验证 | 素材检索专项的浏览器验收记录；`make console-check MEDIA=...` | 运行手册：`docs/runbooks/console.md` |
| Timeline：素材/观测校验 | 已验证 | `make timeline-check MEDIA=...` | ASR/OCR/VLM 实际融合与**冲突判定**未实现 |
| 完整业务 Golden Path（GP-01） | **未验收** | `make golden-path-check`——已定义 9 个场景 | `golden_path_verified` 保持 `false`；尚无真实媒体端到端成功记录 |
| 模型到素材的自动来源映射 | **未实现** | — | — |
| Rust 侧 in-process `ExecutionBackend`（`model_inference`） | **未实现** | `make capability-check` | 目前恒在 `unavailable_capabilities` 中 |

## 平台与工程

| 领域 | 状态 | 证据 | 边界 |
| --- | --- | --- | --- |
| `macos-aarch64` 作为一等端侧目标 | 已验证 | 远端 `macos-15-arm64` runner 真实通过；宿主验收可跑 | 加速进程必须原生运行 |
| `linux-x86_64` 控制面 | 已验证 | 容器化控制面；托管 CI 作业 | 没有 NVIDIA 侧加速器验收记录 |
| `linux-aarch64` 控制面 | 已验证 | 容器形态 | — |
| Windows | **未验收** | — | 仅 WSL2/Docker 兼容；非一等目标 |
| 对象存储（NAS/MinIO）与 ONNX/TensorRT 适配 | **未实现** | — | Rust adapter trait 与 hash 契约已存在，实现没有 |

## 如何重新验证

```sh
make check                       # lint + Python 测试 + Rust fmt/clippy/test
make test-integration            # 真实 PostgreSQL
make node-check                  # 节点拓扑
make orchestration-p1-check      # 持久编排
make event-pipeline-check        # compose 内常驻事件链路
make semantic-check              # 网关语义检索
make golden-path-check           # 仍未验收的那条闭环
```

被跳过的测试与未执行的 CI 作业从不计入通过。远端 GitHub Actions 按设计**仅手动触发**，因此
“工作流存在”不等于“它跑过”。
