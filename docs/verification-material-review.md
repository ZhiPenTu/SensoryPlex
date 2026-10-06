# 素材查询与回看专项验证（2026-09-24）

首轮代码位于独立分支 `codex/material-review`，基于 `ee4fccd`，后经 `c05ba8d` 合并主线。
收尾基于主线 `4593d08`，分支为 `codex/material-review-finish`。验证使用隔离 Compose 项目
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

## 已执行的浏览器验收

Chromium 136.0.7103.113 / agent-browser 0.27.0，均在 ARM64 console 容器内执行。
登录使用隔离数据库的 `review` 账户；页面请求真实 API 和 PostgreSQL，没有拦截替换响应。

| 操作 | 结果 |
| --- | --- |
| 关键词检索并进入详情、返回列表 | 关键词与显示上限保留在 URL 和输入框；匹配 2 条明确标记的 fixture |
| 来源流、0–30 秒、标签、OCR 模态联合筛选 | 2 条匹配；改到 60–70 秒显示明确空结果；清除后 URL 条件清空 |
| 非法 URL `start=bad` | 显示时间格式错误；浏览器资源记录中 `materials:search` 请求数为 0 |
| 最低置信度设为 0 | 0 条匹配；fixture 的未知置信度没有被当作 0 |
| 版本 3 → 2 → 1 → 最新 | 观测数依次 3/2/1/3，旧版本显示历史快照；指定不存在的 99 返回错误并移除播放器 |
| 点击画面描述 / OCR 观测 | `video.currentTime` 分别为 10 / 20 秒；OCR 两个嵌套文字块完整显示 |
| 同一 asset 的第二来源区间 | 手动切换后实际定位到 40 秒，两个来源窗口没有互相覆盖 |
| 播放 10–13 秒区间 | 实际解码播放，`currentTime=13.150455` 时已暂停；不是逐帧精确截断 |
| 刷新含 `observation=review_obs_1` 的详情 URL | 自动恢复选中观测并定位到 10 秒，实际媒体时长 552.022 秒 |
| 授权 Range 读取 | `206`，`Content-Range: bytes 0-31/11983463`，实际返回 32 字节 |
| 未映射来源 | 明确显示“原片尚未关联”与重试入口，没有生成播放器或替代视频 |
| 桌面 1440×1050 / 窄屏 390×844 | 无水平溢出；窄屏播放器的暂停选项换行挤压已修复，标签高 19px |
| JS 错误与控制台 | agent-browser `errors` / `console` 最终均无输出，无开发错误覆盖层 |

收尾在新工作区重新构建前端、执行 9 项前端测试和 13 项素材集成测试，全部通过；格式检查与
部署脚本 `sh -n` 通过。上表全量 Rust / Python 数量对应首轮验证，没有把它描述为新主线全量复验。

截图：最新控制台界面详见 [素材检索与HUD](images/console/04-materials-search.png) · [原片回看](images/console/04-materials-playback.png)（早期历史专项截图已统一清理归档，视频画面沿用上述 CC BY-SA 4.0 原片署名与许可，测试文字仍为契约输入）。

浏览器环境已保存在本机镜像 `sensoryplex-browser-tools:chromium136-agent0.27.0`，
关闭浏览器并清理会话目录后保存，未包含挂载的代码、数据库凭据或原片。
随后用该镜像重建隔离 console，`--pull never`，实际完成本节验收；没有再次下载依赖。
