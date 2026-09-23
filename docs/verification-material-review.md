# 素材查询与回看专项验证（2026-09-24）

代码位于独立分支 `codex/material-review`，基于 `ee4fccd`。验证使用隔离 Compose 项目
`sensoryplex-material-review`、独立 PostgreSQL 数据卷及查询样例，没有重启主开发栈或改动 M8。
本次只验收已有素材的检索、版本追溯与已映射原片播放；M8、真实媒体端到端和语义冲突识别仍未完成。

## 自动化检查

| 检查 | 结果 |
| --- | --- |
| `make check` 的 Python 部分 | Ruff / format 通过；113 项测试通过，0 跳过 |
| `make check` 的 Rust 部分 | fmt、Clippy `-D warnings` 通过；110 项测试通过 |
| PostgreSQL 集成测试单独运行 | 24 项通过，0 跳过；包含 13 项素材查询与来源授权测试 |
| 前端 `npm run test:materials` | 9 项通过：毫秒精度、非法 URL、未知/零置信度、嵌套文本、来源覆盖与版本边界 |
| 前端 `npm run build` / `npm run format:check` | 通过 |
| `git diff --check` | 通过 |

Python、前端及浏览器命令均经 `docker compose exec -T` 在容器内执行；Cargo 按项目例外在主机执行。
`make check` / `test-integration` 使用本地 Compose 命令覆盖指向隔离 API，避免默认 Makefile 指向
正在开发 M8 的主栈。113 项 Python 测试已经包含这 24 项集成测试，两者不是额外累加的数量。
既有 Starlette/httpx 与 AnyIO 弃用警告仍存在，没有屏蔽或算作本次新增故障。

## 浏览器验证数据边界

原片为 `tests/fixtures/media/OPEN-SAMPLES.md` 已登记的授权视频
`screencast-video2commons.480p.vp9.webm`（Juandev，CC BY-SA 4.0，854×480，552 秒），
SHA-256 为 `0a107a537ef7b24eceef2658e2fd4bc065d2861d7ce9c07e44056175f5d3d00c`。

隔离数据库中 `review_material` 的三个 revision 和 ASR/VLM/OCR 观测是明确标记
“契约验收样例 / 非模型输出”的测试输入，全部置信度未知。`review_unmapped` 专门验证
未关联原片的错误显示。它们未写入主业务库，也不代表本视频经过模型处理。

回看依赖可信目录显式登记完整原片的 `upload://` 映射；详情见
[使用与边界说明](runbooks/material-review.md)。没有实现自动媒体准入、Runtime 到素材写入接线、
直播分段时间映射、语义检索或逐帧精度剪辑。
