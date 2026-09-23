# ADR-015：macOS 常驻形态（launchd）与统一内存分级

**状态：** Accepted（2026-09-23）
**上游决策：** ADR-008（Apple Silicon 一等目标）、ADR-011（保留窗口按种类分配）、ADR-012（模型插件与端侧推理边界）、ADR-014（ASR 插件与音频样本布局契约）
**相关文档：** [实现状态](../implementation-status.md)、[验证记录](../verification.md)、[运行手册](../runbooks/macos-resident.md)、[待办](../TODO.md)

---

## 1. 背景与问题

ADR-008 把 Apple Silicon（含"买来当家庭工作站"的 Mac mini）定为一等目标，并要求两台机型的
常驻形态在**同一份契约**下运行；但在此之前，本仓库关于 macOS 常驻只有文档描述——
`docs/runbooks/development.md` 里那句"需显式配置 `pmset`/`caffeinate` 防休眠策略"就是全部产物。
也就是说 M5 的验收项（plist 模板 + 安装/卸载脚本、重启自启、崩溃重启、按统一内存设置队列上限）
**没有可执行的东西可跑**，因此也就没有可核对的结论。

要把这件事做成"可核对"，先得回答三个问题，且这三个问题的答案都不能靠猜：

1. **同一份默认值不可能适配 16GB 到 128GB 的所有机型**。`crates/media/src/handoff.rs` 的
   `DEFAULT_RETAINED_LIMIT = 32` 与 `DEFAULT_RETAIN_ARENA_BYTES = 64 MiB` 是本机
   （M2 Max / 32 GiB）实测可用的取值。把它原样搬到 16GB Mac mini 上，等于在没有测量依据的情况下
   声明"16GB 也撑得住"；反过来，128GB 机型上又白白浪费了容量。**同时 ADR-008 明确要求 16GB 机型
   不得默认并行加载 ASR + OCR + Fast VLM**——这本身就是"按容量分级"的需求，而不是"统一配一个数"。
2. **"防休眠"和"改系统设置"是两回事**。`pmset -a sleep 0` 需要 root，且会持久改变用户机器的
   省电行为；一个仓库里的安装脚本不应该悄悄做这件事。
3. **上限值散落在每个调用点就会漂移**。`replay`/`ingest` 的 `--handoff-retained-limit` 是命令行参数，
   如果每个调用方各写一份数字，分级就只是文档里的一段话，而不是一个被消费的事实。

本决策把这些落成规则，并用**本机真机验收**（macOS 26.5.2 / arm64 / M2 Max / 32 GiB）给出证据。
它只承诺"常驻托管与分级可执行、可核对"，**不承诺**"长稳运行、断电重启、并发模型实测"——
见 §7 与 §8。

---

## 2. 决策一：统一内存分级是数据，档位之外不吸附、探测来源必须显式

分级表写在 `tools/macos_resident.py` 的 `TIERS`，是唯一事实源；`medium` 档**锚定**今天的默认值
（`handoff.rs` 的 `DEFAULT_RETAINED_LIMIT` / `DEFAULT_RETAIN_ARENA_BYTES`），即"不改现有行为"，
其余档位相对它成整数倍展开：

| 档位 | 统一内存 | 媒体队列 | 保留窗口 | 保留 arena | 模型并发 | 说明 |
| --- | --- | --- | --- | --- | --- | --- |
| `small` | 16–24 GiB | 16 | 16 | 32 MiB | 1 | 16GB 机型：模型只串行跑一个（ADR-008） |
| `medium` | 24–32 GiB | 32 | 32 | 64 MiB | 2 | **等于当前默认上限** |
| `large` | 32–64 GiB | 64 | 64 | 128 MiB | 3 | 默认上限的 2 倍；单一种类上限随之 32 条 |
| `xlarge` | ≥64 GiB | 128 | 128 | 256 MiB | 4 | 默认上限的 4 倍；留给慢通道 VLM 与更大批次 |

区间是半开的（`min_bytes <= x < max_bytes`），与全仓库的时间轴约定一致。规则：

- **档位之外不吸附**：8 GiB 或 15.9 GiB 不"降级到 small 再凑合跑"，16 GiB 不"算进 medium"；
  `select_tier()` 返回 `None`，`unsupported_reason()` 给出可读原因，并且文案里**写明不猜**。
- **探测来源必须显式**：`probe_memory()` 优先 `sysctl -n hw.memsize`（`source="sysctl"`）；
  只有宿主探测失败（容器/CI/非 macOS）才接受 `SENSORYPLEX_TOTAL_MEMORY_BYTES` 的**声明**值
  （`source="env"`），且非法声明值（非正整数）直接 `ValueError`；两者都不可用时是
  `memory_bytes=None, source="unavailable"`——不是 0，也不是"大概 8GB"。这个三元组一路写进
  `resident.env` 与 plist，所以任何一次运行都能回答"这个上限是按哪个数字算的"。
- **模型预算**是**预算上限**而不是实测占用：`model_budget_bytes = 统一内存 // 3`，其余留给系统、
  页缓存、媒体缓冲与 Python 运行时。插件 manifest 里 `resources.memory`（ASR `4Gi`、VLM `1Gi`）
  是**声明值**，工具在 `probe` 里如实标注，绝不把它当成 RSS。

## 3. 决策二：launchd 用户级托管，`KeepAlive` 只对非正常退出生效

- 只写**用户级** `~/Library/LaunchAgents`，不碰系统级 `/Library/LaunchDaemons`：这个服务的控制端点
  默认绑定 `127.0.0.1:50051`，语义是"我登录后可用"，不是"开机即服务的系统组件"。
- `RunAtLoad = true`：登录/引导即拉起，对应 M5 的"重启自启"。
- `KeepAlive = { SuccessfulExit: false }`：**异常退出（含被 `kill -9`）自动重启，正常退出不重启**。
  这条比 `KeepAlive = true` 更严格地表达了意图——`true` 会让"主动停一次"变成"被无限拉起"，
  而运维真正需要的是"挂了要回来，不要我关不掉"。`ThrottleInterval = 10` 给崩溃重启加最小间隔，
  避免一个必然失败的启动被空转成 busy loop。
- 会话防休眠用独立的 `org.sensoryplex.caffeinate` job：`/usr/bin/caffeinate -ims`。
  `-s` 只在交流电下阻止**系统睡眠**（`PreventSystemSleep` / `PreventUserIdleSystemSleep`），
  **不带 `-d`**——屏幕仍按用户的省电设置熄灭。它和 runtime 是两个独立 label，可以单独卸载/停用。
- **工具不调用 `pmset`**（需要 root，且会持久改变机器行为）：`status` 只读 `pmset -g` 的 `sleep`
  值并打印需要人工确认的命令 `sudo pmset -a sleep 0 disksleep 0`，由使用者自己决定。
- 模板渲染是**严格**的：占位符缺值抛错，渲染后残留花括号也抛错；plist 必须在写完前先能被
  `plistlib.loads` 解析。手写 plist 会立刻与分级脱钩，所以模板里注释写明了这一点。

## 4. 决策三：`resident.env` 是上限的单一事实源，调用方不得静默覆盖

- `install` 写 `~/Library/Application Support/SensoryPlex/resident.env`，内含
  `SENSORYPLEX_RESIDENT_TIER` / `_UNIFIED_MEMORY_BYTES` / `_MEMORY_SOURCE` /
  `_MEDIA_QUEUE_CAPACITY` / `_HANDOFF_RETAINED_LIMIT` / `_HANDOFF_ARENA_BYTES` /
  `_MODEL_PARALLELISM` / `_MODEL_BUDGET_BYTES` / `_RUNTIME_ADDR`。同一组值同时渲染进 plist 的
  `EnvironmentVariables`，两处来自同一次渲染，不存在"文档一个数、进程另一个数"。
- `deploy/macos/bin/sensoryplex-media-run` 是 `replay`/`ingest` 的包装：从 `resident.env` 注入
  `--handoff-retained-limit` / `--handoff-arena-bytes`，其余参数原样透传。它的失败语义是显式的：
  - 缺 `resident.env` → **exit 1**，提示先 `install`（不猜默认值，也不退回"看起来能跑"的上限）；
  - 调用方自带同名参数 → **exit 2**，拒绝静默覆盖（上限由分级决定，不由调用点决定）。
- 分级通过后立即验证上限确实生效（见 §6 第 8 条）：包装脚本打印
  `分级 large: retained_limit=64 arena_bytes=134217728`，`handoff_stats` 里实测
  `retained_limit=64`、`retained_kind_limit=32`（默认每类上限减半，ADR-011 的语义随之等比放大）。

## 5. 已知缺口：`queue_capacity` 目前只被校验，未被运行时消费

`crates/runtime/src/lib.rs` 定义并校验 pipeline 的 `queue_capacity`（≤65536），
`config/pipelines/{file-material,srt-live}.yaml` 都是 32；但**当前没有代码读取它来设置队列深度**——
实际队列上限来自执行侧的内置常量。因此 `probe` 只**报告**分级值与 pipeline 声明值是否一致，
不改写配置、也不声称"队列已按分级调整"。这是本轮明确留下的缺口，**不要**把它读成"队列上限已生效"。

## 6. 验收证据（真机，macOS 26.5.2 / arm64 / M2 Max / 32 GiB）

| # | 验证 | 结果 |
| --- | --- | --- |
| 1 | `macos_resident.py probe` | 32.0 GiB / `source=sysctl` / 分级 `large`；`retained_limit=64`（单类 32）、arena 128 MiB、模型并发 3、预算 10.7 GiB；两个 pipeline 的 `queue_capacity=32` 报"匹配"并注明该字段只被校验 |
| 2 | `install` | 打印 `pmset` 现状 **`sleep 1`** 与人工命令（工具不改系统设置）；`org.sensoryplex.runtime` pid=6538 running |
| 3 | `status --verify-endpoint` | runtime / caffeinate 双 running；gRPC `127.0.0.1:50051` 返回 `state=degraded`、`platform=macos-aarch64`、`unified_memory_bytes=34359738368`（与宿主探测一致）、`unavailable_capabilities=[media_ingestion, model_inference, event_dispatch, semantic_index]`、`admitted_memory_kinds=[cpu_shared_memory, unified_memory]` |
| 4 | `launchctl print gui/501/org.sensoryplex.runtime` | `environment` 内含 `SENSORYPLEX_HANDOFF_ARENA_BYTES=134217728`、`SENSORYPLEX_UNIFIED_MEMORY_BYTES=34359738368`、`RUST_LOG=info` |
| 5 | `pmset -g assertions` | pid 6541 的 `caffeinate -ims` 持有 `PreventUserIdleSystemSleep` + `PreventSystemSleep`（asserting forever） |
| 6 | **崩溃重启** | `kill -9 6538` 后 3 秒 `status` 显示**新 pid 6644** running → `KeepAlive` 生效 |
| 7 | **重启自启** | `launchctl bootout` → `launchctl print` 确认 not loaded → `launchctl bootstrap gui/501 …`（不 kickstart）→ 2 秒后 running **pid=6997** → `RunAtLoad` 生效 |
| 8 | **分级上限注入媒体作业** | `sensoryplex-media-run replay config/pipelines/file-material.yaml video/1.mp4`：打印分级值，报告 `anchors=2237 decoded_items=2239 descriptors=1354 rejected=0 leases 1354/1354 segments=7`；再以 `--handoff-listen 127.0.0.1:64555`（无消费者）跑，`handoff_stats` 实测 `retained_limit=64 retained_kind_limit=32 retained_peak=47`、backpressure `state=saturated`，以 `handoff_consumer_never_connected` 退出（预期） |
| 9 | `uninstall --purge-logs` | 两个 label 已卸、plist 与 `resident.env` 已删、日志已清；残留检查：无 `serve` 进程、无 `caffeinate -ims`、50051 无监听 |

契约测试 `tests/contracts/test_macos_resident_contract.py`（11 项）锁死上表 §2/§3/§4 的语义：
分级边界不吸附（8 / 15.9 → `None`；16 → `small`；24 → `medium`；32 → `large`；64 → `xlarge`）、
档位连续且 `medium` 锚定 `handoff.rs` 源码常量、探测来源标注与非法声明值报错、
`KeepAlive == {SuccessfulExit: false}`、`ProgramArguments == ["/usr/bin/caffeinate", "-ims"]`、
`resident.env` 内容、包装脚本三种语义（注入 / 覆盖被拒 exit 2 / 缺文件 exit 1）。

## 7. 代价与已知限制

- 分级表是**工程经验值加锚定**，不是压测结果：`large` 的 arena 128 MiB 只在本机以无消费者
  场景验证过（`retained_peak=47`），没有在 16GB 机型上实跑 `small` 档。
- 模型并发只落在 plist 环境变量里，**运行时目前不消费**它，因此"并发 3"是配置事实而非执行事实；
  并发执行的实测要等 worker 侧按该变量限流之后。
- 目前只有本机 `macos-aarch64` 一个平台验收；CI 不跑 launchd（runner 上没有用户会话与
  `launchctl gui/` 域），因此本 ADR 的证据**只能**来自真机。
- 没有验证"机器断电重启后自启"（只验证了 `bootout`+`bootstrap`，等同于登录会话内的拉起）；
  没有验证 App Nap / 休眠唤醒后的端点可用性；`caffeinate` 在纯电池供电下的行为未验证。
- 未做 `resident.env` 的权限收紧（当前随 `~/Library` 的默认权限），也没有把分级值回写进
  `DescribeCapabilities`——端点目前只上报 `unified_memory_bytes`。

## 8. 最小实现清单（当前状态）

- [x] `tools/macos_resident.py`：分级表、`probe`/`render`/`install`/`status`/`uninstall` 五个子命令
- [x] `deploy/macos/launchd/org.sensoryplex.{runtime,caffeinate}.plist.template`
- [x] `deploy/macos/bin/sensoryplex-media-run`（分级注入 + 拒绝覆盖）
- [x] `tests/contracts/test_macos_resident_contract.py`（11 项）
- [x] `Makefile` 的 `resident-probe|resident-install|resident-status|resident-uninstall`（主机例外，不进容器）
- [x] `docs/runbooks/macos-resident.md`

**未验证范围（不得当作已完成）**

- 只有本机一台 Apple Silicon 机型（M2 Max / 32 GiB）验收；Mac mini 各档位与 16GB 的
  `small` 档均未实跑。
- 模型并发上限、`queue_capacity` 的分级生效都还是**配置事实**，没有并发执行的端到端样本。
- 断电重启自启、休眠唤醒、长稳运行（小时级）、内存压力下的拒绝行为都未验证。
- `golden_path_verified` 恒为 false；本节不改变这一结论。
