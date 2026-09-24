# Timeline 融合核心验证记录

日期：2026-09-23。基线：`76743e6`。独立分支：`codex/timeline-fusion`。
范围：纯 Rust Observation → MaterialUnit 聚合；未连接 Runtime 或新增数据库写入路径。

## 执行环境与隔离

- 主机 `Darwin arm64`：按 AGENTS.md 的 Rust 工具链例外运行 Cargo。
- Python 验证全部经 `docker compose --env-file .env -f deploy/compose/docker-compose.poc.yml exec -T api`。
- 现有容器栈已经运行；本轮没有重启、重建容器，也没有安装主机 Python/Node 工具链。
- M8 正在使用主工作目录，因此将本分支源码复制到 api 容器的独立临时目录，
  显式设置该副本的 SDK、插件、services 与 tools 导入路径。
- 主目录的 `make check` / `make integration` 通过临时 `PY_API` 包装器执行容器内副本，
  通过临时 `CARGO_HOST` 包装器执行本 worktree；没有修改 Makefile、Compose 或 .env。
- 数据库测试使用现有测试 fixture 创建/清理的随机 schema，不迁移业务 schema。

复核命令（本轮专用临时包装器，不作为项目依赖）：

```sh
make check \
  PY_API=/tmp/sensoryplex-timeline-fusion-01a0ceea/python \
  CARGO_HOST=/tmp/sensoryplex-timeline-fusion-01a0ceea/cargo
make integration \
  PY_API=/tmp/sensoryplex-timeline-fusion-01a0ceea/python \
  CARGO_HOST=/tmp/sensoryplex-timeline-fusion-01a0ceea/cargo
```

以上包装器分别切换到容器内源码副本与本 worktree。
合并到标准工作目录后，正常运行 `make check` / `make integration` 即可，无需包装器。

## 已执行结果

| 验证 | 结果 |
| --- | --- |
| Timeline 契约测试 | 27 passed，0 failed / ignored |
| README 调用示例编译 | 1 passed |
| Rust 工作区测试 | 137 passed（含 27 个 Timeline 测试），0 failed / ignored |
| Rust fmt / Clippy `-D warnings` | 通过 |
| 容器内 Ruff / 格式检查 | 通过，94 files already formatted |
| 容器内 Python contracts + integration | 100 passed，0 skipped |
| 单独 `make integration` | 11 passed，0 skipped；是上述 100 项的子集 |

Python 运行保留两条现有依赖弃用警告（Starlette/httpx、AnyIO BlockingPortal）；
本次没有修改这些依赖。

核心场景包括：partial → fast_ready → enriched、旧 revision 不变、完全重放幂等、
乱序/重复输入、迟到观测、同 ID 不同事实拒绝、素材级冲突防覆盖、源/stream 不匹配、
半开区间越界、原片引用覆盖、同 asset 摘要冲突、payload/血缘/未知置信度保留、
失败与拒绝不可计为 ready、revision 时间回退/溢出、数量/字节/窗口/嵌套深度限制。

## 证据边界

- 上述是契约与已有平台回归测试。测试使用明确标注的构造 fixture，不能当作业务数据、
  真实模型结果或媒体 E2E 证据。
- PostgreSQL 集成验证的是已有 metadata writer 与平台行为的回归，
  不等于新 Rust 核心已通过 Runtime 写入数据库。
- 没有执行真实模型推理、长流测试、跨平台性能基准或语义质量评估。
- 冲突只传递显式质量标记；readiness 只按模态存在性，不证明全窗口覆盖。
- Golden Path、2–5 秒语义可见性、语义搜索和完整 M11 仍未完成。

合并时仅需整合 `crates/timeline/` 与 Cargo.lock 中本 crate 的两条依赖关系；
不升级依赖版本。随后统一更新公共 implementation-status/TODO/verification 的 Timeline 范围，
并在 M8 契约稳定后执行 Runtime → Timeline → metadata writer 联调。

## 追加：Runtime → Timeline → metadata writer/outbox 联调（2026-09-24）

上面最后一句要求的联调**已经完成**，决策与边界见
[ADR-028](../../docs/adr/ADR-028-Runtime到Timeline接线与授权追加.md)：`crates/runtime` 新增
`timeline` 子命令（真探测 → 读真运行报告 → 选窗 → 调本核心 → 写素材 protobuf），
追加由授权写入口 `tools/timeline_handoff.py` 执行，验收为
`make timeline-check MEDIA=<授权样本>`（主机执行）。

真实授权样本实测：6 帧观测 → 7 窗（5 窗有观测）→ 5 条素材、`rejected=0`；
追加后 `material_unit=5` / `observation=6` / `timeline_item=6` / `event_outbox=5`，
入库字节与磁盘 protobuf 逐字节相同；第二遍追加 `appended=0 replayed=5`，relay 第二遍 `published=0`。
逐项断言与仍未验证范围见 [验证记录](../../docs/verification.md) 的 ADR-028 一节。

联调中修掉一个本 crate 之外的真实缺陷（记在这里以免重复踩）：worker 报告的观测是 protojson，
int64 以**字符串**传输且零值字段被省略，`crates/runtime/src/timeline.rs` 的读取侧当时只接受
JSON 数字，导致**每一条从 0 ms 开始的观测都被判为不可解析**。fixture 用的是数字形态，
所以本 crate 的契约测试当时是全绿的——这正是"契约 fixture 不能替代真实链路证据"的又一例。
