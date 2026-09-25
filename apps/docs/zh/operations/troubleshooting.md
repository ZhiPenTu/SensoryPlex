# 故障排查

项目的失败风格是刻意的：大多数问题应该表现为显式的错误码或拒绝启动。本页把最可能遇到的症状映射到原因。

## 控制台能打开，但接口调用失败

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| API 重建后控制台返回 `502` | 控制台的 nginx **在启动时**解析 `api` upstream，可能继续持有旧容器 IP | `./deploy/up.sh console` |
| 控制台能打开但所有请求 401 | 未登录，或会话/令牌来自上一套数据库 | 重新登录；用 `make demo-seed` 重新播种演示账号 |
| 登录页没有演示按钮 | `.env` 里 `SENSORYPLEX_DEMO_USERNAME` 为空 | `make demo-seed`，再 `./deploy/up.sh api` |
| 忘记演示密码 | 密码被重新生成过 | 读 `.data/demo-password`，或用 `make demo-reset` 重置 |

## 语义检索

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| `503` 且 `retryable=false` | 检索令牌不匹配，或检索面未配置 | 检查 `SENSORYPLEX_INDEX_SEARCH_TOKEN` 与 `SENSORYPLEX_INDEX_AUTH_TOKEN` 是否同一个值 |
| `503` 且 `retryable=true` | 检索面未运行或不可达 | `./deploy/up-events.sh`，再 `./deploy/status.sh` |
| `502` | 检索面答了但不符合契约（例如模型发布版本不匹配） | 读错误体；不要盲目重试 |
| 语义模式报不可用 | events profile 没起时的正确行为。API 显式失败，而不是返回降级排名 | 启动 events profile |
| 第二个进程打不开向量库 | Milvus Lite **进程独占** | 停掉占用 `.data/index` 的另一个进程；同一时刻只能有一个进程持有它 |

## 事件链路拒绝启动

| 提示 | 原因 | 处理 |
| --- | --- | --- |
| `event_inflight_exceeds_tier_cap` | 批深度越过了档位容量 | 调低 `SENSORYPLEX_EVENT_RELAY_BATCH` / `SENSORYPLEX_EVENT_CONSUME_BATCH`，或有意识地抬高档位容量 |
| `not_injected` | 容量或批深度变量缺失 | 在 `.env` 补齐；缺失的档位变量从不被静默默认 |
| stream 缺失 / durable 漂移 | JetStream 状态与契约不符 | 查看被报告的 stream 或 durable；有意识地重置，绝不要靠随手重建 stream 来“修好它” |
| `up-events.sh` 在启动前就非零退出 | BGE 权重文件不存在 | 准备 `.data/models/bge-small-zh-v1.5/onnx/model_quantized.onnx`；脚本刻意不替你下载 |

## 素材闭环跑不完

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| 节点报离线（`503`） | 宿主工作器没跑，`local-host` 心跳停了 | `make task-worker-daemon`，再 `make task-worker-status` |
| 节点被以 `node_heartbeat_stale` 拒绝 | 心跳超过 60 秒 | 重启工作器；检查宿主是否休眠/挂起 |
| 任务一直因 `data_locality_violation` 不可调度 | 方案要求共享内存访问，但目标节点不同机 | 把该步骤放到同机节点执行——这是硬过滤，不是警告 |
| `make golden-path-check` 报前置条件未满足 | 通常是缺真实视频、工作器停了，或事件链路没起 | 依次检查上面三条后重跑 |
| 全新安装时搜索返回空数组 | 正确行为：没有业务数据，也不会编造 | 导入视频并跑一个任务 |

## 媒体解码

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| `ffprobe` 对 SRT 地址报 `Protocol not found` | 本机 `ffprobe` 不支持 SRT 协议 | 用 GStreamer（`srtsink`/`srtsrc`）驱动 SRT；`make live-check` 已经是这样做的 |
| 解码报告全零 | 本次构建没有发生解码 | 安装 GStreamer 开发文件；或接受 `MEDIA_FEATURES=` 的纯锚点报告，此时缺口在 `blockers` 中声明 |
| `make media-test` 失败 | 缺 GStreamer 开发文件 | 装上，或跳过该解码专项测试——但不要把被跳过的路径描述成已验证 |
| 轨道看起来被重排过 | 含 B 帧的流在解码顺序下 PTS 非单调 | 预期行为：重排是显式的，并计入 `out_of_order_items` |

## 工具链与环境

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| 容器里跑二进制报 `Exec format error` | 该产物是宿主二进制（Mach-O），不是容器产物 | 在宿主运行；见 [安装与前置要求](/zh/guide/installation) 里的主机例外 |
| 容器里找不到 `cargo` / `rustc` | 现有镜像都不带 Rust 工具链 | Rust 命令在宿主执行，直到批准专用工具链容器 |
| `make test-integration` 报错而不是跳过 | 没有 `SENSORYPLEX_TEST_DATABASE_URL` | 提供它——缺数据库被设计成显式失败，而不是静默减少覆盖 |
| 镜像拉取失败 | registry 可达性 | compose 镜像按 digest 固定，控制台构建已使用镜像加速前缀。服务端 Milvus 正是因此仍未验收 |
| 容器里 `make configure` 写不了 `.env` | 仓库 bind mount 无法写回宿主 | 在宿主执行；它是明确记录的主机目标 |

## 证据容易混淆的两种情况

1. **某一跳通过不等于整链通过。** `make outbox-check` 只覆盖发布这一跳；向量由另一个进程写入——
   要用 `make consume-check` 或 `make event-pipeline-check` 验证。
2. **跳过不等于通过。** 被跳过的测试、没执行的 CI 作业、`healthy` 的容器，都不算验收。任何依赖真实媒体的
   宣称都需要一份真实授权样本。
