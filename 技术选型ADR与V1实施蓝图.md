# 端侧 AI 多模态素材预处理框架
## 技术选型 ADR 与 V1 实施蓝图

**状态：** Proposed（进入 POC 前评审）  
**版本：** V1.1<br>
**日期：** 2026-09-24<br>
**适用范围：** 本地多卡 GPU POC、私有化部署与后续端侧 NPU SDK 路线

**配套规范：** [`开放式插件开发文档.md`](./开放式插件开发文档.md) 定义第三方 Source、Processor、Sink 与硬件执行后端插件的开发、验证和发布契约。

---

## 1. 定位、边界与 V1 成功标准

本项目是面向云端大模型和 Agent 的**端侧多模态素材预处理基础设施**。它负责采集、感知、清洗、结构化、时间轴对齐、素材沉淀与检索；不负责视频剪辑、内容创作、文案生成或最终成片。

### 1.1 产品形态、跨平台边界与必做拓扑

产品当前交付形态是 **Web 控制台 + 可部署控制面**，浏览器访问 `apps/console`；本阶段**不做**
Electron、Tauri 或其他桌面客户端。文档中出现的 `DesktopSource` 仅指未来的屏幕采集 Source Plugin，
不代表交付桌面应用。

底座必须先在受支持的平台上可部署，再扩展异构推理硬件：控制面支持
`linux-x86_64`、`linux-aarch64` 的容器部署，以及 `macos-aarch64` 上通过容器运行时部署无加速组件。
这不是“任意操作系统、任意硬件都已验收”的承诺；原生 Windows 暂以 WSL2/Docker 兼容形态处理，
尚不是一等交付目标。模型执行节点则按自身硬件选择原生或容器运行时。

局域网插件 worker 拓扑是本项目**必做**能力，而不是桌面版的替代方案：

```text
浏览器
   │ HTTPS（Web Console）
   ▼
主节点：Console / API / Registry / Scheduler / PostgreSQL / NATS
   │ 受认证的控制命令、状态与受控引用
   ├───────────────┬────────────────┐
   ▼               ▼                ▼
子节点 A          子节点 B          同机子节点
Mac mini          3090Ti 主机       与主节点共机
agent + worker    agent + worker    agent + worker
MLX/CoreML        CUDA/TensorRT     按该机能力运行
```

- 每套部署有一个逻辑主节点，负责节点注册、插件 artifact/版本、安装意图、调度、审计与失败状态；
  它不在浏览器 HTTP 请求中直接安装插件或执行推理。
- 每个子节点运行受控 agent，并可承载一个或多个插件 worker。管理员必须能在 Web 控制台选择目标节点；
  主节点依据节点能力、资源、数据位置与部署策略接受或拒绝该选择，不能悄悄换到其他节点。
- 子节点可以和主节点同机，也可以是局域网内独立的 Mac mini、NVIDIA 或厂商 NPU 主机；同机不取消
  进程、权限、资源上限和生命周期隔离。
- 跨节点只传控制消息、观测和受控对象引用。共享内存、DMA、CUDA/Metal 句柄始终 host-local；需要远程
  消费原始媒体时，必须走显式的数据传输或受控存储路径，不能把字节塞入 NATS/gRPC 控制消息。

该拓扑的契约、节点状态、安装/回滚、数据本地性和验收要求由
[ADR-026](./docs/adr/ADR-026-Web主节点与局域网插件worker拓扑.md) 定义。当前 POC 尚未实现节点 agent、
节点 Registry 或跨机调度，不能把已有单机插件验收描述成此拓扑已交付。

### 1.2 V1 的唯一 Golden Path

```text
SRT 实时流 / 本地视频文件
        ↓
GStreamer 解封装、解码与自适应抽帧
        ↓
ASR + OCR + VLM + 质量过滤
        ↓
统一 Timeline Fusion
        ↓
MaterialUnit（可溯源素材单元）
        ↓
PostgreSQL + Milvus + NAS/MinIO
        ↓
REST Search API（随后补 MCP）
```

**V1 验收标准：** 一段正在输入的视频，在进入系统后 2–5 秒内可按语义检索到对应 `MaterialUnit`，检索结果必须能准确跳转回原始视频的时间区间，并携带模型、版本、置信度和原始素材引用。

**V1 交付前置条件：** 上述业务 Golden Path 之外，必须先证明控制面能在声明支持的平台组合部署，
并完成 ADR-026 的同机与局域网节点注册、安装位置选择、调度、数据本地性和失败恢复验收。单机回放、
容器健康检查或仅有 Web 页面均不构成这项前置条件的证据。

### 1.3 明确不在 V1 范围内的事项

- 不做剪辑、脚本生成、内容发布与其他创作功能。
- 不做 Electron、Tauri 或其他桌面客户端；Web 控制台是当前唯一的人机入口。
- 不在第一期同时支持所有采集源；优先实现 SRT 与离线文件，RTMP、摄像头、桌面录屏随后以 Source Plugin 接入。
- 不在第一期建设 Kubernetes、Kafka、Ray 或全功能多租户控制台。
- 不承诺所有 VLM 结果都低于 2 秒；高质量 VLM 采用异步补全路径。
- 不将“全网唯一”“所有竞品均为离线”等市场结论作为技术决策依据；相关结论须经独立竞品调研验证。

### 1.4 目标平台矩阵

Apple Silicon macOS 与 NVIDIA Linux 同为一等目标，端侧交付对象包含 Mac mini 这类常驻家庭工作站；
macOS 不是"开发机能编译"的附属平台。详见 ADR-008。

| 目标 | 平台标识 | 角色 | 加速后端 | 验收含义 |
|---|---|---|---|---|
| Apple Silicon macOS | `macos-aarch64` | 本地开发 + Mac mini 单机端侧部署 | CoreML / Metal / VideoToolbox | 真实文件与 SRT 回放、端侧单机 Golden Path |
| NVIDIA Linux | `linux-x86_64` | 性能主线（3090Ti POC） | CUDA + TensorRT | 延迟与吞吐基线 |
| 端侧 NPU Linux | `linux-aarch64` | Gate C 之后评估 | QNN / RKNN / Ascend | 适配差距报告 |

平台标识由 Runtime 通过 `DescribeCapabilities` 上报，取值为构建目标
`<os>-<arch>`（`std::env::consts` 口径）；任何跨平台结论都必须标注平台与执行后端。

---

## 2. 总体架构决策

### ADR-001：将控制面与媒体数据面分离

**决策：** 事件、调度、状态和配置通过控制面传递；原始帧、PCM、张量和 GPU buffer 通过本机零拷贝数据面传递。

```text
                           Agent / LLM
                        REST / MCP / gRPC
                                  │
                           API Gateway
                                  │
┌─────────────────────────────────▼─────────────────────────────────┐
│                           Control Plane                            │
│ Rust Runtime: Pipeline / Plugin / Scheduler / Resource / Timeline │
│ NATS JetStream: event、task、state、durable notification           │
└─────────────────────────────┬─────────────────────────────────────┘
                              │ 只传 metadata / command / event
──────────────────────────────┼──────────────────────────────────────
                              │
┌─────────────────────────────▼─────────────────────────────────────┐
│                            Data Plane                              │
│ SRT / File → GStreamer → shared memory / DMA / CUDA handle        │
│                       ├── ASR     ├── OCR     ├── VLM     └── QC   │
│                       └──────────── Timeline Fusion ──────────────│
└─────────────────────────────┬─────────────────────────────────────┘
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
         PostgreSQL         Milvus          NAS / MinIO
          metadata          vector             blob
```

**原因：** 1080p RGB 单帧约为 6 MB；30 FPS 约为 186 MB/s。若每个处理插件都经由消息总线序列化、复制和反序列化帧数据，会引入不可接受的内存带宽与延迟开销。

**后果：** NATS 只承载 `FrameReady` 的引用、帧元数据和处理状态，绝不承载整帧视频、音频或 GPU tensor。数据面协议必须支持 buffer 生命周期、背压、引用计数和消费者超时回收。

### ADR-002：Rust 负责运行时，Python 负责模型插件

**决策：** 采用 **Rust Core + C ABI/Protobuf Plugin SDK + Python AI SDK**。

| 职责 | 首选实现 | 说明 |
|---|---|---|
| Runtime、调度、插件生命周期、资源管理 | Rust | 面向 7×24、并发、共享内存、硬件 SDK 和长期 SDK 化 |
| 实时媒体管线 | GStreamer（Rust/C/C++ 绑定） | 作为实时处理的一等公民 |
| ASR、OCR、VLM、Embedding 模型适配 | Python Worker | 保持模型迭代速度，并隔离 CUDA/Python 运行时风险 |
| 跨进程与跨节点契约 | Protobuf + gRPC | 版本化、可生成多语言 SDK |
| 插件二进制 ABI | C ABI | 不直接暴露 Python ABI |

**后果：** Python 插件不得获得 Runtime 内部指针，也不得自行管理数据面内存。插件通过版本化 `Processor` 契约接收 buffer descriptor 或对象引用，返回结构化观测结果。模型稳定后可迁移到 ONNX/C++ 插件，而不改变上层业务契约。

### ADR-003：实时媒体使用 GStreamer，离线工具使用 FFmpeg

**决策：** 实时 SRT/RTMP/摄像头/录屏管线使用 GStreamer；离线探测、转码、诊断和回填可使用 FFmpeg。

**原因：** 项目本质是连续媒体 Pipeline，而不是一次性转码任务。GStreamer 提供可组合的 source、decode、tee、queue、时钟与背压语义，便于在长期运行中观察和恢复。

### ADR-004：NATS JetStream 作为控制与事件总线

**决策：** 一期采用 NATS + JetStream；不采用 Kafka 作为一期核心依赖。

**适用事件示例：**

```text
StreamStarted / StreamDisconnected / FrameReady / SceneChanged
ASRCompleted / OCRCompleted / VLMCompleted / MaterialCreated
ModelLoaded / ModelFailed / GPUOverloaded / StorageDegraded
```

**原因：** 当前需求是 edge-friendly 的 Pub/Sub、请求响应、轻量持久化事件、任务通知和状态回调。Kafka 的运维与资源成本不与一期规模匹配。

### ADR-005：以 ONNX Runtime Execution Provider 作为硬件适配主轴

**决策：** 模型准入优先要求 ONNX 可导出或具备等价可替换路径；运行时通过统一的 `ExecutionBackend` 抽象调用 CUDA、TensorRT、OpenVINO、QNN、RKNN、Ascend 或厂商 Runtime。

```text
Model Plugin
     │
ONNX / vendor model artifact
     │
Runtime Adapter (ExecutionBackend)
     │
 ┌───┼────────────┬─────────────┬────────────┬──────────────┐
 ▼   ▼            ▼             ▼            ▼              ▼
CPU CUDA      TensorRT       OpenVINO   CoreML/Metal   QNN/RKNN/Ascend
     │            │             │            │              │
   x86       RTX 3090Ti     Intel NPU   Apple Silicon   厂商端侧 NPU
```

**接口草案：**

```text
load(model_ref, options)
compile(profile)
infer(input, deadline)
capability()
metrics()
unload()
```

**后果：** 硬件厂商特有能力允许通过 `vendor_extensions` 暴露，但通用模型结果、错误码、超时、profiling 与 fallback 语义必须一致。3090Ti 路线优先采用 TensorRT；不能被 TensorRT 支持的算子应可回退至 CUDA EP 或模型插件的明确降级路径。Apple Silicon 走 ONNX Runtime CoreML EP，Apple 专有加速（如 MLX）只能作为 `vendor_extensions`；CoreML 与 TensorRT 的精度和数值结果不等价，执行后端与精度必须进入结果血缘（见 ADR-008）。

### ADR-006：Timeline 和 MaterialUnit 是事实中心，向量库不是事实数据库

**决策：** 核心链路为 **Media → Timeline → Material Graph → Embedding**，而不是“视频直接入向量库”。

| 数据类型 | 事实来源 | 保存内容 |
|---|---|---|
| Blob | NAS / MinIO | 原视频、音频、关键帧、缩略图、可选 clip |
| 结构化元数据 | PostgreSQL；端侧可用 SQLite | 流、时间轴、模型结果、血缘、置信度、状态 |
| 向量索引 | Milvus；端侧 POC 可 Milvus Lite | 文本、OCR、视觉描述、图像向量及其引用 |

**后果：** 向量检索命中后，必须使用 `material_unit_id` 回查 PostgreSQL 获取完整时间轴与溯源信息；不得仅返回裸向量内容。

### ADR-007：采用快慢双路径并拆分延迟目标

**决策：** 实时路径先提供可检索的基础素材，慢路径再补充高质量理解；延迟按能力分别承诺。

| 能力 | P95 目标 | 路径 |
|---|---:|---|
| 媒体接入 | < 200 ms | Fast |
| 图像预处理 / 场景检测 | < 100 ms | Fast |
| OCR | < 500 ms | Fast |
| Streaming ASR partial | < 500 ms | Fast |
| Streaming ASR final | < 2 s | Fast |
| 小型 VLM | < 2 s | Fast |
| Embedding | < 300 ms | Fast |
| RAG 可见性 | < 3 s | Fast |
| 大型 VLM、重排序、二次结构化 | 5–20 s（按模型记录） | Slow |

**后果：** 每个 `MaterialUnit` 必须携带 enrichment 状态。API 允许先返回 fast 结果，并明确返回尚未完成的字段，不能伪装为最终完整结果。

### ADR-008：Apple Silicon 是一等端侧目标

**决策：** `macos-aarch64` 与 `linux-x86_64` 并列为 V1 支持目标（矩阵见 §1.4）。macOS 侧的实现、部署与验收独立成立，不依赖 NVIDIA 假设，也不以 Gate C 为前提。

**理由：** 端侧 AI 的真实交付对象包含 Mac mini 这类常驻家庭工作站；若把 macOS 仅当开发环境，契约、部署脚本与验收口径都会按 CUDA/TensorRT 写死，返工成本高于现在支持。

**后果（必须遵守）：**

- **容器不承载 Apple 加速。** Docker Desktop 的 Linux 容器无法访问 Metal / ANE / CoreML。macOS 上 `runtime`、`media-worker`、`ai-worker` 必须以宿主原生进程运行（`launchd` 托管）；Compose 只承载 `postgres`、`nats` 与无加速依赖的 `gateway`。§9 的 `gpus: all` 拓扑仅适用于 Linux 节点。
- **精度与后端必须可追溯。** CoreML fp16/int8 与 TensorRT 结果不等价；`Provenance.execution_backend` 与模型版本必须记录平台与精度，禁止用 macOS 结果覆盖或混淆 Linux 历史结果。
- **统一内存要显式声明。** Apple Silicon 的 CPU/GPU/ANE 共享统一内存，Runtime 必须通过 `DescribeCapabilities` 上报 `unified_memory_bytes` 与允许的 `memory_kinds`；媒体准入使用的 memory kind 不得超过上报集合，零拷贝语义不得演变为传递裸指针（locator 仍是 Runtime 签发的 opaque handle）。
- **容量按统一内存规划。** Mac mini 部署需给出 `pmset`/`caffeinate` 防休眠策略与统一内存预算；16GB 机型不得默认并行加载 ASR + OCR + Fast VLM，队列上限与模型量化档位必须随内存容量分级。
- **能力缺失保持可见。** `DescribeCapabilities` 中不可用的后端必须给出 `unavailable_reason`；禁止把"尚未接入"表现为"零结果成功"，也禁止用健康检查通过代替能力验证。

---

## 3. 组件选型清单

| 分层 | V1 首选 | 可替换项 / 使用条件 | 决策说明 |
|---|---|---|---|
| Runtime | Rust | C++ | 为 SDK 化、内存安全和长期运行服务 |
| 实时媒体 | GStreamer + libsrt | MediaMTX 作为接入层 | 实时流用 GStreamer，离线辅助用 FFmpeg |
| macOS 媒体栈 | Homebrew GStreamer（自带 libsrt 依赖）+ VideoToolbox 硬解 | FFmpeg 仅用于离线探测与回退 | Apple Silicon 原生 SR 支持；bottle 需较新 macOS，旧系统走源码编译 |
| 事件与任务 | NATS JetStream | ZeroMQ（节点内轻量场景） | 不传大媒体数据 |
| 内部 RPC | gRPC + Protobuf | — | 所有边界协议版本化 |
| Agent API | FastAPI REST；后续 MCP | gRPC API | 先提供稳定 REST 检索能力 |
| GPU 推理 | CUDA + TensorRT | ONNX Runtime CUDA EP | 3090Ti POC 默认路径 |
| Apple Silicon 加速 | ONNX Runtime CoreML EP | Metal / MLX（`vendor_extensions`） | 与 NVIDIA 并列的一等目标；精度与 TensorRT 不等价，必须记录 |
| 多模型服务 | Triton（多 GPU/多模型后启用） | 自管 worker | Triton 只负责 inference，不取代业务调度器 |
| 跨硬件运行时 | ONNX Runtime EP | 厂商 Runtime Adapter | 硬件可插拔的关键 |
| ASR | faster-whisper、FunASR 适配器 | 商业模型适配器 | 统一 ASR 输出契约 |
| OCR | PaddleOCR | RapidOCR / 厂商 OCR | 中文优先，保留替换能力 |
| VLM | Qwen-VL 系列适配器 | InternVL / 厂商 VLM | 不把模型名称写进上层业务 |
| Embedding | BGE 系列 | Qwen Embedding | 同一 embedding 维度须版本化 |
| 元数据 | PostgreSQL | Edge: SQLite | 时间轴与血缘事实来源 |
| 向量检索 | Milvus | Edge: Milvus Lite；Qdrant 需单独 ADR | 独立于元数据事实库 |
| 素材对象 | NAS / MinIO | 本地文件系统（POC） | Blob 不写入数据库行 |
| Cache | 本地 cache；集群再引入 Redis | RocksDB | 不将 cache、queue、event 混用 |
| 可观测性 | Prometheus + Grafana + OpenTelemetry | Loki / 本地滚动日志 | 对延迟、丢帧和模型失败可审计 |
| 部署 | Docker Compose | K3s（机器规模与运维成熟后） | 一期避免直接 Kubernetes |

---

## 4. V1 服务边界

V1 不应将每一个函数拆成独立微服务。建议先按故障域、伸缩方式和运行时边界拆为下列可独立部署单元。

| 服务 / 进程 | 责任 | 扩缩容方式 | 绝不负责 |
|---|---|---|---|
| `gateway` | REST、鉴权、查询编排、回调 | 无状态水平扩展 | 媒体解码与模型推理 |
| `runtime` | Pipeline 生命周期、插件注册、调度、资源与背压 | 按节点部署，一个活跃管理实例 | 对外长时间 API 请求 |
| `media-worker` | SRT/File 接入、GStreamer、抽帧、音频切段、数据面 buffer | 按流和节点扩展 | 业务检索与永久元数据写入 |
| `ai-worker` | ASR、OCR、VLM、Embedding 插件执行 | 按模型/GPU 扩展 | 直接读取或写入其他插件内存 |
| `timeline-worker` | 对齐、冲突标注、MaterialUnit 构建、写事实库 | 按流分区扩展 | 保存原始 Blob |
| `index-worker` | 向量生成、Milvus upsert、索引重试 | 按 backlog 扩展 | 成为元数据真相来源 |
| `storage-adapter` | MinIO/NAS 写入、校验、归档、回收策略 | 按吞吐扩展 | 自行决定素材删除 |

### 4.1 推荐的事件与命令边界

```text
Command: start_stream, stop_stream, retry_job, reload_model
Event:   stream.started, frame.ready, audio.segment.ready,
         observation.created, material.upserted, embedding.ready,
         storage.degraded, runtime.backpressure
```

事件只包含稳定 ID、时间范围、版本、状态、优先级和 payload 引用。任何消费者都必须幂等；消息至少一次投递时，以 `(event_id, consumer_name)` 去重。

### 4.2 背压与故障策略

- 每个流和每种 processor 都有有界队列；队列超过阈值时，先降低非关键帧采样率，再暂停慢路径，最后报告可见的 `backpressure` 状态。
- 原始媒体接入优先级高于 VLM 慢路径；不得为了补全 VLM 让直播流整体断裂。
- 所有外部写入使用可重试、幂等键与死信记录；失败的素材单元必须是 `failed` 或 `partial`，不可静默丢失。
- 一个模型或硬件后端失效时，系统记录 fallback 决策、结果质量等级和影响范围。

---

## 5. V1 仓库目录建议

```text
edge-material-runtime/
├── README.md
├── docs/
│   ├── adr/
│   ├── contracts/
│   ├── runbooks/
│   └── benchmarks/
├── proto/
│   ├── common/v1/
│   ├── runtime/v1/
│   ├── material/v1/
│   └── gateway/v1/
├── crates/
│   ├── runtime/                 # Pipeline、scheduler、plugin lifecycle
│   ├── media/                   # GStreamer integration、buffer descriptor
│   ├── timeline/                # time alignment、material assembly
│   ├── storage/                 # metadata/blob/vector adapter traits
│   ├── execution/               # ExecutionBackend traits
│   └── sdk/                     # C ABI、generated protobuf bindings
├── plugins/
│   ├── python/
│   │   ├── common/
│   │   ├── asr_faster_whisper/
│   │   ├── asr_funasr/
│   │   ├── ocr_paddle/
│   │   ├── vlm_qwen/
│   │   ├── embedding_bge/
│   │   └── quality_score/
│   └── native/
│       └── vendor_backends/
├── services/
│   ├── gateway/
│   ├── media-worker/
│   ├── ai-worker/
│   ├── timeline-worker/
│   ├── index-worker/
│   └── storage-adapter/
├── db/
│   ├── migrations/
│   └── seed/
├── deploy/
│   ├── compose/
│   │   ├── docker-compose.poc.yml
│   │   └── env.example
│   ├── monitoring/
│   └── systemd/
├── config/
│   ├── pipelines/
│   ├── models/
│   └── policy/
├── tests/
│   ├── contracts/
│   ├── integration/
│   ├── e2e/
│   └── performance/
└── tools/
    ├── replay_stream/
    ├── benchmark/
    └── data_fixture/
```

**目录规则：**

- `proto/` 是所有跨进程、跨语言边界的唯一契约来源；禁止在服务间手写临时 JSON 结构。
- `plugins/` 只能实现稳定 SDK，不允许反向依赖 `services/` 的内部模块。
- 数据库迁移只能追加，禁止在部署环境通过 ORM 自动建表或隐式改表。
- `tests/e2e/` 必须使用真实视频样本或受控测试流，不使用合成“成功结果”替代媒体与模型链路。

---

## 6. Pipeline 与插件契约

### 6.1 插件类型

```text
SourcePlugin     SRTSource / RTMPSource / CameraSource / DesktopSource / FileSource
ProcessorPlugin  Decoder / Sampler / ASR / OCR / VLM / Quality / Embedder / TimelineFusion
SinkPlugin       BlobStorage / MetadataStore / VectorStore / Webhook / AgentGateway
```

### 6.2 Pipeline 配置示例

```yaml
apiVersion: edge.material/v1
kind: Pipeline
metadata:
  name: srt-material-poc
spec:
  source:
    type: srt
    latency_mode: low
  processors:
    - type: adaptive_sampler
      profile: live-default
    - type: audio_segmenter
    - type: asr
      model: faster-whisper-large-v3
      mode: streaming
    - type: ocr
      model: paddleocr-v5
    - type: vlm
      model: qwen-vl-fast
      path: fast
    - type: timeline_fusion
    - type: embedding
      model: bge-m3
  slow_enrichment:
    - type: vlm
      model: qwen-vl-quality
      path: slow
  sinks:
    - type: blob_storage
    - type: metadata_store
    - type: vector_store
```

### 6.3 关键输出契约

所有观察结果至少应包含：`stream_id`、`source_id`、`start_ms`、`end_ms`、`model_id`、`model_version`、`confidence`、`content_hash`、`created_at`。

```json
{
  "material_unit_id": "mu_01J...",
  "stream_id": "st_01J...",
  "start_ms": 872300,
  "end_ms": 880100,
  "status": "fast_ready",
  "asr": [{"text": "我们来看第三季度销售数据", "confidence": 0.94}],
  "ocr": [{"text": "Q3 Revenue +18%", "confidence": 0.97}],
  "vision": [{"caption": "讲师站在销售增长柱状图前", "confidence": 0.89}],
  "tags": ["PPT", "财务", "Q3", "销售"],
  "source_refs": [{"asset_id": "asset_01J...", "start_ms": 872300, "end_ms": 880100}],
  "lineage": {"pipeline_version": "v1", "model_versions": ["..."]}
}
```

### 6.4 自适应抽帧策略

抽帧不是固定 FPS。采样器综合 SSIM、pHash、直方图差异、运动矢量、OCR 差异、场景变更和音频 VAD，并记录每一次采样原因。

| 场景 | 建议行为 |
|---|---|
| 静态 PPT | 0.1 FPS；OCR 变化或翻页立即补帧 |
| PPT 动画 / 屏幕操作 | 基线 1 FPS；变化阶段短时提升 |
| 户外稳定直播 | 0.5–1 FPS |
| 快速运动或 scene change | 4–5 FPS burst，受队列背压限制 |

采样器的成本、丢弃原因和覆盖率是一级监控指标；它很可能比切换某个 VLM 更直接决定总体质量与成本。

---

## 7. Timeline 与数据模型

### 7.1 实体关系

```text
MediaSource ──< StreamSession ──< MediaAsset
                    │
                    └──< TimelineItem >── VideoFrame
                                         ├── AudioSegment
                                         ├── ASRSegment
                                         ├── OCRBlock
                                         ├── VisionObservation
                                         └── Scene
                                                     │
TimelineItem ───────────────────────────────────────┘
                    │
                    └──< MaterialUnit >──< EmbeddingRecord
```

### 7.2 PostgreSQL 逻辑表

| 表 | 主键 | 关键字段 | 用途 |
|---|---|---|---|
| `media_source` | `source_id` | type, uri_redacted, owner, policy | 采集源定义与访问策略 |
| `stream_session` | `stream_id` | source_id, started_at, ended_at, clock_offset_ms, status | 单次连续流会话 |
| `media_asset` | `asset_id` | stream_id, object_uri, sha256, codec, duration_ms | 原始或派生 Blob 元数据 |
| `timeline_item` | `item_id` | stream_id, kind, start_ms, end_ms, parent_item_id | 所有时间轴对象的公共主表 |
| `observation` | `observation_id` | item_id, modality, payload_jsonb, confidence, model_release_id | ASR/OCR/VLM/QC 结果 |
| `material_unit` | `material_unit_id` | stream_id, start_ms, end_ms, status, quality_score, revision | 面向 Agent 的聚合素材 |
| `material_observation` | 复合键 | material_unit_id, observation_id, role | MaterialUnit 与观测的血缘 |
| `embedding_record` | `embedding_id` | material_unit_id, model_release_id, vector_ref, content_hash | 向量索引引用与重建依据 |
| `model_release` | `model_release_id` | name, version, artifact_hash, backend, config_hash | 模型及推理配置可追溯 |
| `processing_job` | `job_id` | idempotency_key, state, attempt, error_code | 处理任务与错误审计 |

### 7.3 不可省略的数据规则

1. 所有时间均以 `stream_id + 毫秒偏移` 为主锚点；需要展示时再映射为 UTC 时间。不得混用帧序号、系统时钟和媒体 PTS 而不记录换算关系。
2. `model_release_id` 必须指向不可变模型版本，包括模型 artifact、prompt/template、预处理配置和执行后端配置哈希。
3. 原始媒体和派生素材使用 `sha256`；相同内容不重复归档，删除必须通过引用计数与保留策略执行。
4. `confidence` 表示模型或融合置信度，不等同于事实正确性。跨模态冲突必须以显式 `conflict` 或 `low_confidence` 状态保存。
5. `MaterialUnit` 允许 revision；已被 Agent 消费的旧 revision 不可覆盖，应保留血缘并标记 superseded。

### 7.4 Milvus collection 约束

建议按 embedding 模型与维度分 collection，例如 `material_text_bge_m3_v1`。每条向量至少保存：

```text
embedding_id, material_unit_id, stream_id, start_ms, end_ms,
modality, model_release_id, content_hash, visibility, created_at
```

写入向量后才把 `embedding_record.state` 标记为 `ready`。检索接口只返回 `ready` 且满足可见性策略的记录。

---

## 8. API 与 Agent 接口

### 8.1 REST 优先的 V1 接口

| 接口 | 用途 | 最小响应要求 |
|---|---|---|
| `POST /v1/streams` | 创建/启动媒体流 | stream_id、状态、采集策略 |
| `POST /v1/streams/{id}:stop` | 停止流并完成收尾 | 最终状态与未完成任务数 |
| `GET /v1/streams/{id}` | 查询流、延迟、背压、错误 | 当前状态、关键水位、最近错误 |
| `POST /v1/materials:search` | 语义、关键词、标签、时间范围检索 | MaterialUnit、来源时间区间、置信度、索引版本 |
| `GET /v1/materials/{id}` | 查询完整时间轴与血缘 | 观测结果、revision、blob 引用 |
| `GET /v1/health` | 运行健康检查 | 依赖状态，不泄露敏感配置 |

`materials:search` 必须支持：`query`、`stream_id`、`start_ms/end_ms`、`modalities`、`tags`、`min_confidence`、`limit`。其响应中的媒体 URL 应为短期授权 URL 或受鉴权的对象引用，不直接暴露 NAS 内部路径。

### 8.2 MCP 的引入时机

在 REST 契约、鉴权、溯源字段和错误语义稳定后，映射为 MCP tools：

```text
search_materials
get_material_detail
get_stream_status
```

MCP 不是第二套业务实现；它只是同一 Gateway service contract 的 Agent 适配层。

---

## 9. Docker Compose POC 拓扑

一期采用 Docker Compose，GPU 节点通过 NVIDIA Container Toolkit 暴露指定 GPU。以下为拓扑模板；Milvus 的版本、镜像 digest、持久化路径、账号和密钥应在实际部署时锁定并写入受管理的配置，不得直接照抄到生产环境。

```yaml
services:
  nats:
    image: nats:2.10
    command: ["-js", "-sd", "/data"]
    volumes: ["nats-data:/data"]

  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: edge_material
      POSTGRES_USER: edge_material
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes: ["postgres-data:/var/lib/postgresql/data"]

  minio:
    image: minio/minio:latest
    command: server /data --console-address :9001
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD}
    volumes: ["minio-data:/data"]

  milvus:
    # 使用官方当前版本的 standalone compose 依赖（etcd + MinIO）补全。
    # POC 可复用本 compose 的 MinIO；生产环境需单独容量与备份设计。
    image: milvusdb/milvus:latest
    command: ["milvus", "run", "standalone"]
    depends_on: [minio]

  gateway:
    build: ../../services/gateway
    depends_on: [nats, postgres, milvus, minio]
    environment:
      DATABASE_URL: postgresql://edge_material:${POSTGRES_PASSWORD}@postgres/edge_material

  runtime:
    build: ../../crates/runtime
    depends_on: [nats]

  media-worker:
    build: ../../services/media-worker
    depends_on: [nats, runtime]

  ai-worker:
    build: ../../services/ai-worker
    depends_on: [nats, runtime]
    gpus: all

volumes:
  nats-data:
  postgres-data:
  minio-data:
```

**macOS 部署形态（ADR-008）：** Apple Silicon 上容器无法访问 Metal/CoreML，因此上述拓扑只用于 Linux 节点。
Mac mini 部署时 Compose 仅运行 `postgres` 与 `nats`（`gateway` 无加速依赖，可继续容器化）；
`runtime`、`media-worker`、`ai-worker` 以 `launchd` 托管的原生进程启动，并在启动日志与
`DescribeCapabilities` 中上报芯片、统一内存、后端与精度。

### 9.1 部署与运维门槛

- Compose 文件中仅使用镜像 tag 作为草图；实际环境必须锁定 digest、配置镜像扫描、最小权限、密钥注入及升级回滚版本。
- PostgreSQL、NATS 与对象存储需要独立备份与恢复演练；“容器能启动”不等于数据可恢复。
- GPU worker 在启动时记录 NVIDIA driver、CUDA、TensorRT、模型 hash、GPU UUID 与显存策略。
- Apple Silicon 节点在启动时记录芯片型号、统一内存容量、macOS 版本、CoreML/Metal 精度与 VideoToolbox 硬解状态。
- POC 的正式验收必须覆盖写路径：真实视频/流进入、素材产出、元数据写入、向量检索和时间点回跳；不能只检查 `/health`。

---

## 10. 质量、安全与可观测性

### 10.1 必测指标

```text
ingest_to_material_visible_ms (P50/P95/P99)
per_model_infer_ms / queue_wait_ms / batch_size
frame_sampled_total / frame_dropped_total（按 reason）
stream_reconnect_total / stream_gap_ms
gpu_utilization / gpu_memory / thermal / OOM
event_retry_total / dead_letter_total
timeline_conflict_total / low_confidence_total
blob_write_failure_total / vector_index_lag_ms
```

### 10.2 安全与数据治理最低要求

- 原始视频默认本地保存；任何云端上传必须由独立配置和审计策略明确授权。
- 推流地址、对象存储密钥、访问 token 和设备标识不得进入日志、事件 payload 或异常栈。
- 对外检索基于 source/stream/material 的访问控制做二次过滤；不能只隐藏前端入口。
- 素材删除、归档和保留周期采用可配置策略，并保留审批/审计记录。删除索引前先确认 Blob 和元数据的引用关系。
- 个人信息、敏感字幕与人脸等能力在接入前定义数据分类、脱敏、保留期限及导出策略。

---

## 11. 12 周开发路线

| 周次 | 目标 | 可验证产出 |
|---:|---|---|
| 1 | 定义边界与仓库骨架 | ADR、Proto 初版、Docker 开发环境、真实测试媒体集与验收脚本 |
| 2 | 打通媒体接入 | SRT/File → GStreamer → 时间戳正确的帧/音频 descriptor；断流重连测试（macOS 与 Linux 各执行一次） |
| 3 | 建立 Runtime 最小闭环 | Pipeline 生命周期、NATS 控制事件、有界队列和背压指标 |
| 4 | 实现自适应抽帧与质量过滤 | 静态 PPT、翻页、运动视频三类回放数据的采样覆盖率报告 |
| 5 | 接入流式 ASR | 分段、partial/final、时间轴及模型版本可追溯；延迟报告 |
| 6 | 接入 OCR | OCR blocks、坐标、时间锚点与图像引用；PPT 样本准确性基线 |
| 7 | 接入 Fast VLM 与硬件后端 | Qwen-VL fast adapter、TensorRT/ORT 执行记录、失败降级语义 |
| 8 | 完成 Timeline Fusion | 生成 MaterialUnit、跨模态冲突标记、revision 与血缘查询 |
| 9 | 打通存储与索引 | Blob、PostgreSQL、Milvus 一致性；向量重建与失败重试 |
| 10 | 提供 Gateway 检索 API | 语义/标签/时间检索、原视频定位、鉴权及可审计错误响应 |
| 11 | 稳定性与性能压测 | 连续推流、断网重连、GPU OOM、存储短暂故障、背压行为测试 |
| 12 | 试运行与决策复盘 | 可重复部署、E2E 验收报告、容量模型、NPU 适配差距与 V2 ADR |

### 每周均需满足的工程门槛

- 新增跨进程字段必须先修改 Protobuf/契约，再写实现与契约测试。
- 每个 bug 修复都包含可复现测试或可回放媒体样本。
- 性能结论必须标明平台（`macos-aarch64` / `linux-x86_64`）、执行后端与精度、硬件、模型、输入分辨率、并发、队列策略与统计口径；跨平台数据不得合并计算。
- 任何模型、硬件或存储 fallback 都必须对上层暴露状态，禁止静默合成成功结果。

---

## 12. 阶段门禁与后续决策

### Gate A：第 4 周后是否进入完整 AI 链路

前提：SRT/File 连续回放稳定、时间戳对齐正确、背压不会无限占用内存、抽帧结果可解释。

### Gate B：第 8 周后是否扩大模型与输入源

前提：`MaterialUnit` 可稳定生成，能从任一结果回查原始媒体和模型血缘；Fast/Slow 补全不会覆盖或混淆历史结果。

### Gate C：第 12 周后是否进入 NPU SDK 与集群化

前提：Golden Path 达标，部署可重复，指标与容量模型可用，且已有至少一种模型在 ONNX/运行时抽象上验证成功。

Apple Silicon 支持不受 Gate C 限制：`macos-aarch64` 的媒体接入与端侧单机 Golden Path 属第 2–4 周交付物；
CoreML 后端可作为 Gate C「至少一种模型在 ONNX/运行时抽象上验证成功」的证据之一，但仍需真实样本与延迟报告。

只有通过 Gate C 后，才评估：K3s、GPU Operator、Triton 的更大规模使用、RTMP/桌面录屏、NAS 多节点同步、MCP 对外发布以及 Qualcomm/RKNN/Ascend 等 NPU 后端。

---

## 13. 最终技术结论

本项目的产品和技术核心不是一个“VideoRAG”工具，而是：

> **Edge Multimodal Material Runtime（端侧多模态素材运行时）**

其可持续壁垒应建立在以下四项共同形成的协议和工程能力上：

1. 可追溯、可修订的 **Timeline Schema 与 Material Protocol**；
2. 面向连续媒体的 **低复制实时数据面**；
3. 面向 GPU/NPU 演进的 **Hardware Runtime Abstraction**；
4. 快慢分层、可度量、可降级的 **实时多模态 Pipeline**。

具体 ASR、OCR、VLM 或向量模型都必须可以替换；系统价值在于模型结果如何被正确地对齐、清洗、溯源、组织并稳定交付给上层 Agent。
