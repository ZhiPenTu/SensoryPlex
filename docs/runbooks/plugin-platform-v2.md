# ADR-032 独立插件验收与维护

本入口面向隔离开发栈；底座命令走容器，插件构建与原生 Worker 走宿主。执行会创建真实账号、
配置、部署与任务，不清除历史事实。先按 ADR-032 和双语独立插件指南准备受信制品。

## 真实正向验收

1. 通过 `deploy/up.sh` 启动隔离基础栈，`deploy/up-events.sh` 启动索引，
   `deploy/up-enrichments.sh` 启动通用 publisher/fuser。迁移仅追加 `0016`–`0021`。
2. 在独立项目安装交付 SDK wheel；校验、构建、签名、验证两个 0.1.2 项目。
3. 将公开制品放在 `.data/plugin-v2-acceptance/external/releases/<名称>-<版本>/`，
   将批准的公钥放在同级 `external/publisher.public.pem`。私钥不挂载进底座。
4. 按原节点准入流程配置宿主 Node Agent，再启动 `tools/enrichment_worker.py`，
   使用该 Agent 的状态文件和隔离 NATS 的回环地址。
5. 将真实授权原片放在 `.env` 的 `MEDIA_DIR`，执行：

```sh
make plugin-platform-check MEDIA=/授权目录/sample.mp4 RELEASE_VERSION=0.1.2
make plugin-platform-fault-check
```

第一条依序导入、配置、安装、发布、提交、等待与鉴权查询，覆盖同步 Observation 节点、
异步 Observation 补全及异步媒体补全。记录保存到 `.data/plugin-v2-acceptance`；失败直接返回非零。
不修改底座名单，不用模拟 Observation。语义命中逐条与本执行的素材 revision/Observation 身份对账。
未声明文本的测量输出必须 `not_declared`；不能将没有文本当作索引故障。

## 故障、蓝绿与回滚

下面均在 `api` 容器内以 `/app/.venv/bin/python -m tools.verify_plugin_faults` 执行。
需要独立示例的故障制品，签名者撤销使用独立第二公钥，不能撤销正常发布者。

| 命令 | 验证目的 |
| --- | --- |
| `candidate-failure` | 0.1.90 工厂启动失败，active 保持原身份 |
| `deploy --version 0.1.91 --delay-ms 2500` | Process 超过 1000ms 预算 |
| `deploy --version 0.1.91 --retryable` | 有界 retryable failure |
| `start --case timeout` / `wait --case timeout` | 实际提交并核对重试耗尽与部分补全 |
| `cancel --case cancel` | 取消待办，不改写成功 |
| `cancel-inflight --case cancel-inflight` | claim 后取消；迟到结果明确拒绝 |
| `deploy --version 0.1.92 --delay-ms 4500` | 10000ms 预算的崩溃夹具 |
| `claimed --case crash` | 等真实 claim 后才允许强制退出隔离 Consumer |
| `wait --case crash` | 重启 Consumer，等租约和队列真实重投恢复 |
| `duplicate --case crash` | 重投真实 Proto 结果通知，等 fuser pending/ack 清空再查唯一结果数 |
| `retire` | 停用验收用户已结束方案并真实 Drain 可释放旧实例，不删事实 |
| `rollback` | 对故障升级创建反向操作，核对 release 与配置摘要；重验不重复发起 |

崩溃操作必须只针对隔离 Consumer，保留主环境 Agent 与 Worker。Worker 修改后重启对应隔离进程。
`retire` 保留 revision 和结果；驻留上限不能通过删除台账绕过。新图显式选择新 release，旧图继续绑定旧版。

宿主 `tools.verify_plugin_inputs` 验证已安装 Processor 对 schema、跨机 Buffer、大小与尺寸的拒绝；
`tools.verify_enrichment_audio` 使用真实音轨验证 Rust 解码、摘要与租约释放。
`tools.verify_plugin_performance` 以同媒体/策略对比新旧解码程序，记录时间、实际最大 RSS、
arena 和解码数量。无真实解码的程序会拒绝验收，不能用其 ffprobe 锚点数冒充解码。

## 回归与交付

容器内跑 `tests/contracts` 与 `tests/integration`，MCP 单独跑 `services/mcp-server/tests`，
前端与文档使用 `make console-build docs-build docs-check`；Rust 底座检查默认在 `rust` 容器执行，
宿主原生执行器所需构建按 `AGENTS.md` 的既定边界执行。
旧 OCR/ASR/VLM 使用同原片、已安装首方 release 和锁定配置回归，旧 VLM Consumer 使用制品
原 payload 的模块身份，不改历史消息。实际日志与身份见 [专项验收报告](../verification-plugin-platform-v2.md)。

交付 wheel、独立源码/锁/schema、正常 bundle/描述符/签名、公钥、迁移与白名单证据；
排除私钥、账号/Agent 状态、原视频、绝对宿主媒体路径及环境凭据。
