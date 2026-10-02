# 插件平台通用接入与能力契约（ADR-032）专项验收报告

**验收日期：** 2026-10-02  
**状态结论：** 核心底座不修改插件名单、模型分支、结果解析与页面代码的前提下，独立插件完成构建、签名、导入、装配、同步与异步编排、事实落库、向量检索与 Range 原片回放全链路闭环；故障注入（崩溃恢复、超时重试、取消阻断、重复投递幂等、候选失败保护、显式回滚、旧实例排空与 10 项拒绝矩阵）通过实机验收。  
**验收环境：**
- 容器底座（隔离栈 `sensoryplex-plugin-v2`）：FastAPI `28091`、兼容网关 `28090`、Web Console `25173`、文档站 `25174`、PostgreSQL `35432`、NATS JetStream `34222`；
- 宿主机（Apple M2 Max / macOS 15.6 / aarch64）：原生 Node Agent、LaunchAgent 插件实例、通用异步补全 Consumer（`tools/enrichment_worker.py`）。

---

## 1. 独立制品与签名凭据

SDK wheel：`edge_material_sdk-0.1.4-py3-none-any.whl`（不可变制品，哈希锁定）。  
签名体系：Ed25519 分离式签名；API 独立验签与校验整包/成员摘要。

| 插件名称 | 版本 | Release ID | Artifact Digest | Bundle Digest | 信任身份 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `com.example.frame-brightness` | 0.1.2 | `rel_32456ad700565a3793ff5d3d34223d9f` | `sha256:5c0010b88c09f6307cdbb9058a270d1591dc61b79230e995ecf97c72cc2f8fc2` | `sha256:d2cebd068d084510be6805ed1c781ab5c51c11b2d82453748195d5eb9efd0060` | `trusted_publisher` |
| `com.example.brightness-threshold` | 0.1.2 | `rel_e98d65b90f0077d4194b68a308f541e2` | `sha256:c8110e18225c170ae740e26a3826fc3009b04d314f8aa4ecb5701b33f600ddc8` | `sha256:d299bb75ed63f5f9fa826d9a15ce89ee8fb3ae2279dc0588f52b5691281a3617` | `trusted_publisher` |

主签名者：`signer_538234854ffaa22956cf55e378c775ec`（`publisher.public.pem`，已审批）。  
测试撤销签名者：`signer_3f4e24eb6a8df621cfef5df121175e11`（`revocation.public.pem`，已撤销，用于拒绝验证）。

---

## 2. 真实媒体全链路业务验收（GP-01 扩展）

测试样本：授权原片 `sample.mp4`（时长 8.033 秒，241 视频帧，音频单声道 16kHz，摘要 `sha256:9eab557585eca21dff9ea04819b6cc70bf773340496383a29e95642093d6381e`）。

| 模式 | 运行与执行标识 | 观测与素材 | 语义检索与原片回看 | 证据文件 |
| :--- | :--- | :--- | :--- | :--- |
| **同步插件图** | `run_34199fc69b3f46bf9868846c4f746eb4`<br/>`execution_7af063abaacc448cb85977ab081cdbed` | 21 观测事实（11 帧测量 + 10 阈值事件）<br/>9 秒级素材单元 | 命中 10 条（属于本 execution）<br/>Range 206 字节逐字一致 | `sync.query.json`<br/>`sync.execution.json` |
| **异步 Observation 补全** | `run_ee8a125157134bcebc81538850b442f8`<br/>`execution_4da1230f50194a7caf6135e6f6978fcd` | 21 观测事实（快路径 11 + 异步 10 事件）<br/>11 项 enrichment（含 1 项明确无检测） | 命中 10 条（属于本 execution）<br/>Range 206 字节逐字一致 | `async_enrichment.query.json`<br/>`async_enrichment.execution.json` |
| **异步媒体补全** | `run_82a82562f31a4d64b30db2bd96387df9`<br/>`execution_82f2ed1fbfae498681d963d0fd099f5b` | 20 观测事实（11 同步 + 9 延迟单帧解码）<br/>9 秒级素材单元 | 未声明文本（`not_declared`，合规）<br/>Range 206 字节逐字一致 | `media_enrichment.query.json`<br/>`media_enrichment.execution.json` |
| **显式回滚后业务运行** | `run_b3bf7f31a49b48e6b26ed33b39cadf86`<br/>`execution_f2194798b655420aa7a8e63ac3dbc351` | 21 观测事实（11 帧测量 + 10 阈值事件）<br/>回滚后调用 0.1.2 确认 | 命中 10 条（属于本 execution）<br/>Range 206 字节逐字一致 | 回执确认 0.1.2<br/>`status --mode rollback_sync` |

---

## 3. 故障注入与韧性对账

| 场景 | 触发与注入条件 | 实际系统行为 | 验收依据 |
| :--- | :--- | :--- | :--- |
| **候选失败保护** | 导入故障 0.1.90（工厂启动抛出异常） | 操作失败 `candidate_start_failed`，原 active 指针与实例未受影响 | `op_59e5037f48104d2487987881a68270d4`<br/>`candidate-failure.json` |
| **Process 超时** | 部署 0.1.91（延迟 2500ms，超时 1000ms） | attempt 2 重试耗尽，终态 `succeeded_with_partial_enrichment`，快路径保留 | `execution_7fba5f9f367c44a8923a550a4ba73575`<br/>`timeout.fault.json` |
| **有界可重试失败** | 部署 0.1.91（注入显式 retryable 错误） | attempt 2 重试耗尽后终止，快路径不受影响 | `execution_8c4d55740455459cb63c37ba70291e04`<br/>`retry.fault.json` |
| **待办取消** | 任务排队中触发 Run 取消 | 任务终态 `cancelled`（attempt 0），不改写为成功 | `execution_87c0b17893574941b79fdf5acbdb0410`<br/>`cancel2.fault.json` |
| **在飞取消与迟到丢弃** | Consumer claim 领取任务后触发取消 | 结果暂存返回 409 `enrichment_cancelled`，丢弃迟到事实 | `execution_72dc3c8933f142e59132f5ab58a07497`<br/>`rejections.json` |
| **Consumer 崩溃恢复** | 延迟 4500ms 任务 claim 后强制 kill 隔离 Worker | 重启 Worker 后队列租约超时重投，最终 `succeeded`，唯一事实 22 项无重复 | `execution_f35faa2f88a0489ea55484620124555f`<br/>`crash.fault.json` |
| **重复结果通知幂等** | 向 NATS 重投 11 条真实 Proto 结果通知 | 融合消费者去重，单位事实数保持 22 项不变 | `crash.duplicate.json`<br/>`duplicate_notifications: 11` |
| **显式回滚** | 对故障升级发起 `:rollback` 反向操作 | 恢复 0.1.2 实例并切 active 指针，配置摘要完全还原 | `op_5067f1d205fa46f1adb16c502f747b91`<br/>`rollback.json` |
| **旧实例排空** | 停用已结束方案不可变 Revision | 释放非固定旧实例，LaunchAgent 正常卸载 | `retirement.json`<br/>`retired_revisions: 13` |

---

## 4. 准入与拒绝矩阵实测（10+4 项）

1. `unknown_signer`（403）：未登记公钥导入被拒；
2. `wrong_signature`（422）：篡改描述符验签被拒；
3. `wrong_bundle_bytes`（422）：文件哈希与签名声明不符被拒；
4. `revoked_import`（403）：已撤销签名者导入新制品被拒；
5. `revoked_deploy`（403）：已撤销签名者制品部署被拒；
6. `output_schema_mismatch`（422）：输出不满足 JSON Schema 被拒（`payload_schema_invalid`）；
7. `output_limit`（422）：单次输出观测数超限被拒；
8. `unstructured_reason`（422）：outcome_reason 携带敏感路径或非有界机器码被拒；
9. `node_credential_required`（401）：未鉴权请求被拒；
10. `late_result_after_cancel`（409）：取消后提交结果被拒；
11. `input_schema_mismatch`：输入不满足 Schema 时直接拦截；
12. `cross_machine_buffer`：跨节点 Buffer 输入被拒（`data_locality_violation`）；
13. `input_byte_limit`：输入 Buffer 大小超限拦截；
14. `input_dimension_limit`：输入分辨率超限拦截。

---

## 5. 解码基线性能对比（同一硬件 Apple M2 Max / 同一媒体 8.033s）

对比文件：`.data/plugin-v2-acceptance/performance-decoded/comparison.json`。

| 指标 | 9 月 29 日主线基线（3 次中位数） | 本次候选二进制（3 次中位数） | 差异与结论 |
| :--- | :--- | :--- | :--- |
| **解码耗时** | 1.9296 秒 | 1.8351 秒 | -4.9%（平稳无退化） |
| **最大常驻内存（RSS）** | 211,304,448 字节 (~201.5 MB) | 211,714,048 字节 (~201.9 MB) | +0.19%（在测量噪声范围内） |
| **解码项目数** | 586 项（44,298,240 字节） | 586 项（44,298,240 字节） | 逐字对等 |
| **Arena 峰值** | 8,294,400 字节 | 8,294,400 字节 | 严格一致 |
| **租约签发 / 释放** | 352 / 352 | 352 / 352 | 100% 释放，无内存泄露 |

*注：本测试仅测量同配置数据面解码与抽帧开销，不代表模型推理或全链路端到端耗时。*

---

## 6. 旧首方模型与工具链回归

- **旧 OCR/ASR/VLM 全回归**：`execution_46e2deb7caa242838edf9f6c5df28d88` 真实成功；OCR 0.1.1、ASR 0.1.1、VLM 0.1.3 同机运行；
- **Python 契约与集成测试**：604 项全绿（`tests/contracts` + `tests/integration`，耗时 76.4 秒）；
- **MCP 服务测试**：14 项全绿（`services/mcp-server/tests`，真实用户鉴权与工具调用通过）；
- **Rust 工作区测试**：85 项全绿（sensoryplex-media 106 项通过，runtime/timeline/sdk/fusion 全通过）；
- **静态检查**：ruff check 全绿、ruff format 全绿、cargo fmt 全绿、cargo clippy `-D warnings` 全绿；
- **Web 控制台构建**：`npm ci && npm run build` 成功（Vite 7.3.6，产物完整打包）；
- **文档站**：中英文各 25 页严格对等，51 个静态 HTML 构建成功，链接检查 0 死链。

---

## 7. 明确未验收边界

1. **第三方音频算法完整业务链路未验收**：音频数据面适配器已通过真实音轨解码与租约释放（`audio-native.json`），但尚未接入外部第三方法音频算法插件进行 E2E 验收；
2. **Linux / NVIDIA 平台未验收**：当前所有验收均在 macOS Apple Silicon 原生环境下执行，Linux 容器/原生与 CUDA 加速器待后续环境补齐；
3. **跨机原始数据面、共享解码、容器形态插件与 Source/Sink 插件**按架构决策留在后续迭代。
