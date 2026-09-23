# macOS 常驻运行手册（launchd + 统一内存分级）

把原生 `sensoryplex-runtime serve` 作为**用户级 launchd job** 常驻，并按宿主统一内存自动选择
队列/保留窗口/arena/模型并发上限。设计依据见 [ADR-015](../adr/ADR-015-macOS常驻形态与统一内存分级.md)。

只在 **Apple Silicon macOS 宿主**上执行。`launchd`、`launchctl`、`sysctl` 在容器与 Linux CI 里都不存在，
所以这一组是 Makefile 里明确的"主机例外"，不放进 `EXEC_*` 容器目标。

## 前置条件

```sh
cargo build --locked --release -p sensoryplex-runtime --features gstreamer
```

产物固定为 `target/release/sensoryplex-runtime`（可用 `--runtime-binary` 覆盖）。
未构建时 `install` 会显式失败并打印上面这条命令，不会静默跳过。

## 安装与查看

```sh
make resident-probe                 # 只读：输出分级与上限，不写任何文件
make resident-install               # 写 ~/Library + launchctl bootstrap + 等待 running
make resident-status                # 两个 job 的状态；--verify-endpoint 额外做一次真实 gRPC 调用
make resident-uninstall             # 卸载并清理用户级文件
```

直接调用工具时可选参数：

```sh
uv run python tools/macos_resident.py probe --json
uv run python tools/macos_resident.py render --output /tmp/resident   # 只渲染，不安装
uv run python tools/macos_resident.py install --dry-run               # 只写文件，不调 launchctl
uv run python tools/macos_resident.py install --without-caffeinate    # 不装防休眠 job
uv run python tools/macos_resident.py uninstall --keep-config --purge-logs
```

`probe` 的关键输出：

- `统一内存`：优先来自 `sysctl -n hw.memsize`（`source=sysctl`）；容器/CI 里才接受
  `SENSORYPLEX_TOTAL_MEMORY_BYTES` 的声明值（`source=env`）；都没有则是 `unavailable`——工具**不会**取最近一档。
- `分级`：`small` 16–24 GiB / `medium` 24–32 GiB / `large` 32–64 GiB / `xlarge` ≥64 GiB（半开区间）。
- `模型预算`：统一内存的 1/3，是**预算上限**；插件 manifest 里的 `resources.memory` 是**声明值**，不是实测 RSS。
- `queue_capacity`：`probe` 只**报告** pipeline 声明值是否与分级一致，不改写配置；**运行时**在
  `replay`/`ingest` 里按 `SENSORYPLEX_MEDIA_QUEUE_CAPACITY` 对声明值与真实保留窗口做准入，
  越界即失败（[ADR-019](../adr/ADR-019-运行时消费分级队列上限.md)）。`serve` 只转述分级值，
  不要把它读成"这个端点已按分级设了队列上限"。

## 文件位置

| 路径 | 内容 |
| --- | --- |
| `~/Library/LaunchAgents/org.sensoryplex.runtime.plist` | 由分级模板渲染，**不要手写** |
| `~/Library/LaunchAgents/org.sensoryplex.caffeinate.plist` | 防休眠 job（`/usr/bin/caffeinate -ims`） |
| `~/Library/Application Support/SensoryPlex/resident.env` | 分级值的单一事实源 |
| `~/Library/Logs/SensoryPlex/runtime.out.log` / `runtime.err.log` | job 的标准输出/错误 |

`resident.env` 是 `replay`/`ingest` 上限的来源：

```sh
deploy/macos/bin/sensoryplex-media-run replay config/pipelines/file-material.yaml \
  /absolute/path/to/authorized-sample.mp4 --report /tmp/resident-report.pb
```

包装脚本会注入 `--handoff-retained-limit` / `--handoff-arena-bytes` 并打印分级，
**缺 `resident.env` 时 exit 1**（先安装），**调用方自带同名参数时 exit 2**（拒绝静默覆盖）。

## 防休眠与系统设置

工具**不调用 `pmset`**：它需要 root，且会持久改变机器的省电行为。`install` 只读打印现状，
由你决定是否执行人工命令：

```sh
pmset -g | grep ' sleep'                             # 只读检查
sudo pmset -a sleep 0 disksleep 0                    # 需要时才由人执行
pmset -g assertions | grep -i caffeinate             # 核对 caffeinate 确实持有 assertion
```

`caffeinate -ims` 只在**交流电**下阻止系统睡眠（`PreventSystemSleep` / `PreventUserIdleSystemSleep`），
不带 `-d`，屏幕仍会按你的省电设置熄灭。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| `install` 报"无法常驻：统一内存 … 低于最低档" | 机型低于 16 GiB，或探测被容器/CI 挡住。工具不猜档位；确认机型或修改 ADR-015 的分级。 |
| `install` 报找不到 runtime | 先执行上面的 `cargo build --release`，或用 `--runtime-binary` 指定路径。 |
| `status` 显示 `state=exited` / `last_exit=非 0` | 先看 `~/Library/Logs/SensoryPlex/runtime.err.log`；`KeepAlive.SuccessfulExit=false` 会在异常退出后自动重启，`ThrottleInterval=10` 限制最小重启间隔。 |
| `status --verify-endpoint` 报端点不可用 | 确认没有别的进程占用 50051（`lsof -nP -iTCP:50051 -sTCP:LISTEN`），并确认 `resident.env` 里的 `SENSORYPLEX_RUNTIME_ADDR` 与实际一致。 |
| 校验报"进程自报统一内存 != 宿主探测" | 进程环境里的 `SENSORYPLEX_UNIFIED_MEMORY_BYTES` 与 `sysctl` 实测不一致；重跑 `install` 让两者来自同一次渲染。 |
| 想临时停掉又不想被拉起 | 用 `launchctl bootout gui/$UID/org.sensoryplex.runtime`（`KeepAlive` 只对**非正常**退出生效，正常退出不会被重启）。 |
| 卸载后仍有进程 | `uninstall` 会 `bootout` 两个 label 并删除 plist；若仍有残留，`pgrep -fl 'sensoryplex-runtime serve'` 定位后再处理。 |

## 未验证范围（不得当作已完成）

- 只在**本机一台** Apple Silicon 机型（M2 Max / 32 GiB）上验收；Mac mini 各档位与 16 GiB 的 `small`
  档没有实跑过。
- 模型并发上限仍是**配置事实**（没有 worker 按 `SENSORYPLEX_MODEL_PARALLELISM` 限流，也没有并发执行的
  端到端样本）；`queue_capacity` 已由运行时准入消费（ADR-019），但没有在 `small` 档真机上跑过。
- 断电重启自启、休眠唤醒、小时级长稳运行均未验证；CI 不跑本手册（runner 上没有用户会话与 `launchctl gui/` 域）。
