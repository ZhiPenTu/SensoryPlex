# 贡献指南与社区治理 (Contributing & Governance)

欢迎来到 SensoryPlex 开源社区！

SensoryPlex 作为一个面向边缘设备与本地端侧的高性能 AI 多模态素材预处理基座，融合了高性能媒体解码（Rust / GStreamer）、端侧硬件推理加速（Apple Silicon Metal/MLX、CoreML、CUDA、NPU）、流式事件与向量检索（PostgreSQL、NATS JetStream、Milvus Lite）以及现代 Web 控制面。

我们坚信，打造一个工业级、高可靠且面向异构硬件的开源基础设施，需要全球优秀开发者的共同参与。我们热切欢迎音视频处理、端侧模型优化、分布式流计算及全栈领域的开发者加入我们，共同推动 SensoryPlex 的长期更新与演进！

---

## 社区治理与成长梯队

SensoryPlex 倡导**开放、透明、精英共治与证据导向**的社区文化。我们建立了清晰的贡献者晋升路径：

```mermaid
flowchart LR
    A[Contributor 贡献者] -->|积极贡献 / 提交优质 PR| B[Reviewer 审阅者]
    B -->|深度主导核心模块 / 持续贡献| C[Committer 提交者]
    C -->|参与全局架构演进 / 社区治理| D[Maintainer 核心维护者]
```

1. **Contributor (贡献者)**：
   - 任何提交过有效 Issue、文档修订、Bug 修复、新测试用例或功能 PR 并被合并的开发者。
   - 成果将列入项目 Contributors 致谢榜。
2. **Reviewer / Triager (审阅者)**：
   - 熟悉项目架构与工程红线，积极协助审阅社区 PR、复现并分类 GitHub Issues，引导新贡献者符合工程标准。
3. **Committer (代码提交者)**：
   - 在 Rust Crates 底座、Python 模型插件、事件流栈或控制台前端等至少一个核心领域具有持续深度贡献。
   - 获得仓库 Write/Triage 权限，主导相关模块的代码评审与合并。
4. **Maintainer / PMC (核心维护者)**：
   - 具备全局架构设计把控力，负责项目 Roadmap 规划、RFC 裁决、版本发布、安全响应与社区治理。

---

## 重点招募与贡献方向

我们诚邀社区在以下方向提出建议或主导实现：
- **异构硬件与边缘 NPU 适配**：适配 NVIDIA Jetson (TensorRT)、华为昇腾 (CANN)、瑞芯微 (RK3588/RKNN)、Intel OpenVINO 等端侧加速卡；
- **多模态感知模型扩展**：接入轻量 SOTA 视觉语言模型（InternVL、Qwen2-VL 等）及声音事件检测（SED）等专用感知插件；
- **流媒体协议与编解码优化**：扩展 WebRTC、RTSP 接入通道，强化 AV1/H.265 硬件解码与自适应动态抽帧；
- **生产级集群拓扑加固**：跨节点 mTLS 自动化证书轮换与控制面主备高可用架构；
- **Web 控制台与交互**：大规模时间轴长视频切片的高性能渲染与人机协同纠错。

---

## 写代码之前：架构与执行规范

在动手写代码之前，请按顺序阅读：
1. 仓库根目录的需求文档；
2. `docs/implementation-status.md`（了解各链路真实完成与验收状态）；
3. 覆盖你即将修改模块的对应 ADR（架构设计决策）。

与 ADR 冲突的改动，要么附带一份 ADR 修订/RFC 提案，要么重新考虑——ADR 是承重结构，不是历史备忘录。

### 代码在哪里运行（底座容器 vs 宿主原生）

| 层面 | 规则与执行位置 | 核心原因 |
| --- | --- | --- |
| **控制面底座**（`api`、`gateway`、`console`、`postgres`、`nats`、`relay`、`index`） | 构建、lint、迁移与集成验证一律**在容器内**通过 `docker compose exec -T <service> ...` 执行 | 消除“本地能跑但在生产容器报错”的环境与依赖漂移。宿主机无需安装也不要直接调用底座的 Python/Node 工具链。 |
| **子节点插件与 Workers**（`ocr-rapidocr`、`vlm-moondream`、`asr-whisper-mlx`、`task_worker.py`） | **允许在宿主机原生运行**<br/>(Host Native) | 强依赖宿主机专属物理硬件与加速后端（Apple Silicon Metal/MLX、CoreML、CUDA、NPU），轻量 Linux 容器无法直接透传编译原生驱动。 |
| **宿主系统例外** | `cargo` / Rust 构建测试、`make configure`（写入凭据）、macOS LaunchAgent 常驻管理 | 现有容器未内置 Rust 编译套件；macOS launchd 常驻管理仅存在于宿主机。 |

---

## 核心工程红线（不可妥协的规则）

以下 13 条核心工程约定是保障系统高可用、不可变与确定性的底线，所有 PR 必须严格遵守：

1. **`proto/` 是唯一的跨语言契约源。** 修改跨服务/跨语言接口必须先改 Proto，并执行 `make proto` 提交重新生成的产物，严禁手改生成代码。
2. **字段号是永久的。** 删除字段必须标记 `reserved`；破坏性语义升级必须引入新的协议 major 版本。
3. **迁移只追加，禁止修改历史。** 数据库变动仅允许单调递增追加，并通过 `tools/migrate.py` 执行，严禁篡改已发布的历史 revision。
4. **禁止合成业务数据。** 严禁编造模型结果、图表序列或伪造虚假测试通过；无检测结果时显式返回空或说明原因。
5. **未知保持未知。** 置信度缺失、PTS 未知、探测不出的加速器与未知容量均须如实上报并说明原因，绝不私自填入默认值。
6. **原始媒体与凭据不出数据面。** 原始视频帧、PCM 音频、Tensor 矩阵、密钥及宿主私有文件绝对路径严禁进入控制消息、事件与日志。
7. **所有队列与并发必须有上限。** 每个并发窗口、缓冲队列都必须受机型档位硬上限约束；超时与失败具备明确的结构化可观察语义。
8. **真实证据原则。** 媒体端到端工作必须使用真实授权样本（`tests/fixtures/media/OPEN-SAMPLES.md`）。被跳过的测试、未执行的 CI 作业、`healthy` 的容器状态绝不构成通过证据。
9. **编排确定性与取消优先（ADR-029）。** 必需上游成功才解锁下游，上游失败递归阻断；任务取消优先，迟到结果安全丢弃。
10. **热部署蓝绿隔离（ADR-030）。** 原生插件部署采用双槽位独立进程，新版本连续 3 次通过就绪探测才原子切指针，依赖安装 100% 离线闭环。
11. **慢路径异步解耦（ADR-031）。** 慢路径模型（VLM）走 NATS WorkQueue 异步延迟补全，快路径（OCR/ASR）入库后立即标记 `ready_for_review`，不阻塞首屏交互。
12. **时间轴绝对基准（1秒连续切片）。** 严格依据媒体流真实的 `[start_ms, end_ms)` 建立完整 1 秒切片网格，严禁虚构 Observation 或合成空秒素材。
13. **双语注释规范。** 手写 Rust 与 Python 注释默认中文；Proto 文件与生成代码保持全英文；专有名词（GStreamer、Opus、gRPC、NATS、ADR 等）保留英文原名。

---

## 本地验证期望

提交 PR 前，请在本地运行覆盖改动范围的检查，并在 PR 描述中附带命令输出作为通过证据：

```sh
# 基础静态检查与单元测试（容器内）
make lint-ruff
make proto
make test-contracts

# 集成测试与流水线验证（容器内）
make test-integration
make orchestration-p1-check
make event-pipeline-check

# 全面准入（含 Rust 格式与测试）
make check
```

---

## 提交流程与规范

1. **Fork 仓库** 并基于最新 `master` 创建特性分支（如 `feat/whisper-large-v3`）；
2. **规范提交信息**：遵循 Conventional Commits（`feat:`、`fix:`、`docs:`、`refactor:`、`test:` 等）；
3. **创建 Pull Request**：
   - 填写仓库根目录的 `.github/pull_request_template.md` 模板；
   - 逐项勾选「核心工程红线自检清单」；
   - 贴上本地执行 `make check` 或集成检查的真实证据；
4. **代码审查**：至少由一位 Committer 或 Maintainer 审批通过后方可合并。

---

## RFC 机制（重大架构演进）

针对重大架构决策（如破坏性 Proto 升级、新增重度外部依赖、支持新部署拓扑等）：
1. 复制 `.github/ISSUE_TEMPLATE/rfc_template.md` 模板；
2. 在 GitHub Issues 中以 `[RFC] <提案标题>` 发起社区论证；
3. 充分讨论并取得共识后，归档至 ADR 文档并组织实施。

---

## 持续集成说明

GitHub Actions 按设计**仅手动触发**：工作流不会在每次 push 或 PR 上自动消耗托管 runner 额度。日常门禁完全在本地完成。
手动派发时，昂贵的 macOS 作业只在勾选 `run_apple_silicon` 后才会启动——未执行的远端 macOS 作业绝不能被描述为通过。

已发布的文档站遵循同一条规则：`docs-pages.yml` 仅在手动派发时把 `apps/docs` 发布到 GitHub Pages，最新程度取决于上一次手动构建。

---

## 参与本文档站 (apps/docs)

文档站使用 VitePress 构建，支持中英双语言，源码位于 `apps/docs`：

```text
apps/docs/
├── .vitepress/config.mts     # locales、nav、sidebar、edit link
├── public/                   # favicon.svg / logo.svg，原样拷进产物
├── scripts/check-docs.mjs    # 语言树对等 + 产物链接校验（无第三方依赖）
├── <page>.md                 # 英文页（服务于 /）
└── zh/<page>.md              # 中文页（服务于 /zh/）
```

站点静态资源放在 `public/`，**不是** `.vitepress/public/`：VitePress 把 public 目录解析为 `srcDir/public`。

### 新增或修改页面

1. 在英文树里创建页面（`apps/docs/<section>/<slug>.md`）。
2. 在 `zh/` 下同路径创建对应中文页——**两种语言都必须有该页**。VitePress 不做跨语言回退，缺文件就是 404，而不是显示未翻译文本。
3. 在 `.vitepress/config.mts` 的**两个** sidebar（`EN_SIDEBAR` 与 `ZH_SIDEBAR`）里都加上条目，路径形状一致（`/section/slug` 与 `/zh/section/slug`）。
4. 运行 `make docs-check`——它会构建站点并校验内部链接与资源。容器里还没有 `node_modules` 时先跑一次 `make docs-install`。

边写边看可以跑 `make docs-dev`（带热更新的 VitePress dev server，`http://127.0.0.1:5175`）；`http://127.0.0.1:5174` 上跑的是已构建产物。

### 文档风格

- 先说读者能做什么，再说边界。
- 每条能力陈述都应带上它的证据命令，或明确写“未验收”。
- 宁给一条可运行命令，也不描述一条命令。
- 链接到 ADR 或源码文件，而不是用可能漂移的方式复述契约。
