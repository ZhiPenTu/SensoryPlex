# Timeline 融合核心

状态：第一阶段纯 Rust 聚合核心。输入、输出沿用 `proto/material/v1/material.proto`；
本 crate 的配置和请求类型只供进程内调用，不是新的跨进程协议。

## 调用边界

1. 调用方从媒体目录取得可信的 `source_id`、`stream_id`、原片 `asset_id` 和原片 SHA-256。
   `Observation.content_hash` 是模型输入片段/帧的摘要，不能拿来代替原片摘要。
2. 指定稳定的 `material_unit_id` 与同一 stream 的 `[start_ms, end_ms)` 时间窗。
   核心不会自动切窗、合并相邻素材或裁剪跨窗观测。跨度过大的观测应由上游选择适当的素材窗口。
3. 每条 `FusionInput` 携带完整 Observation 与一个覆盖该观测的 SourceReference。
   核心验证区间与摘要格式；asset 所属 stream、数据库模型版本、媒体访问权限仍由目录/存储层验证。
4. `FusionEngine::fuse()` 返回 `FusionOutcome { material, changed, report }`。
   `changed=true` 时交给已有的事务性 metadata writer；`changed=false` 时无需追加。
5. 追加前必须由存储层校验最新 revision。两个调用者从同一旧版本并发生成的 revision
   不能靠此无状态核心仲裁，应由存储的锁/不可变冲突拒绝并重新加载后重试。

modality 必须使用 **Observation.modality 的实际值**，不能照抄 manifest 的 `produces` 前缀。
当前 ASR 为 `asr_segment`，VLM 为 `vision.scene_description`。OCR/BGE 接入时以它们的实际输出值配置。

```rust,no_run
use sensoryplex_sdk::{common::TimeRange, ContractError};
use sensoryplex_timeline::{
    FusionEngine, FusionInput, FusionLimits, FusionPolicy, FusionRequest, MaterialScope,
};

fn fuse_existing_observations(inputs: &[FusionInput]) -> Result<(), ContractError> {
    let engine = FusionEngine::new(FusionPolicy {
        pipeline_version: "timeline-asr-vlm-v1".into(),
        required_modalities: vec!["asr_segment".into(), "vision.scene_description".into()],
        enrichment_modalities: vec![],
        limits: FusionLimits::default(),
    })?;
    let scope = MaterialScope {
        material_unit_id: "material-from-caller".into(),
        stream_id: "stream-from-catalog".into(),
        source_id: "source-from-catalog".into(),
        time_range: TimeRange { start_ms: 0, end_ms: 5000 },
    };
    let result = engine.fuse(FusionRequest {
        scope: &scope,
        inputs,
        previous: None,
        created_at_unix_ms: 1_790_000_000_000,
    })?;
    // 将 result.material 交给事务性存储；不在这里读取媒体字节或执行模型。
    assert!(result.changed);
    Ok(())
}
```

## 确定性与历史

- 观测按 `(start_ms, end_ms, modality, observation_id)` 排序。原始 payload、时序来源、
  置信度/缺失原因、质量原因、模型与插件血缘完全保留，不生成融合置信度或推断标签。
- 同 ID 的完全相同观测去重；同 ID 内容变化返回 `immutable_observation_conflict`。
  ASR partial/final 等内容变化必须使用不同 observation ID，旧事实继续保留。
- 同一 asset 的不同区间可共存，但摘要必须一致。引用按 `(asset_id, start_ms, end_ms)` 去重排序。
- 初版 revision=1；新增事实或来源引用生成下一 revision。由本核心输出的相同事实重放
  （包括空重试）返回完整旧对象，revision 与创建时间均不变。
- 后续 revision 的 material ID、stream、source、时间窗、pipeline version 固定；
  创建时间必须严格递增，revision 溢出显式拒绝。旧对象不会被修改或被写成 superseded；
  superseded 是现有查询层推导的只读状态。
- 调用方必须把融合策略纳入不可变 pipeline version。同名策略内容的注册/哈希验证属于
  后续 Runtime 接线；核心无法从现有 Proto 单独证明策略没有被替换。
- previous 的完成状态与待补全列表必须能由相同策略和其观测事实复算。
  不一致返回 `fusion_previous_policy_mismatch`，不会把其它流程标记的素材级冲突静默改成成功。

## 完成状态与显式质量

`required_modalities` 是 fast 路径必需模态，`enrichment_modalities` 是 slow 路径补全模态；
两组必须互斥且不重复。仅 `accepted`/`final` 表示该模态已有可用结果。
`pending_enrichments` 保存所有缺失的模态，report 分别列出 fast 缺失项与 slow 缺失项。

状态优先级：

| 条件 | MaterialUnit.status |
| --- | --- |
| 任一观测显式 `conflict` | `conflict` |
| 任一观测显式 `low_confidence` | `low_confidence` |
| 只有 `failed`/`rejected` | `failed` |
| 存在可用/partial 观测，但 fast 模态缺失 | `partial` |
| fast 模态齐全，slow 仍缺失或未配置 slow | `fast_ready` |
| fast 与已配置的 slow 模态齐全 | `enriched` |

无观测是 `fusion_empty_observations`，不会合成成功或失败素材。
缺少模型置信度和时序置信度分别计数；缺失模型置信度必须保留其显式原因。
所有 rejected/failed/partial 观测继续保存在素材中，report 提供数量。

**readiness 仅表示窗口内出现了相应模态的结果，不代表整段时间已覆盖、所有任务已完成，
也不代表模型内容正确。** 一次不同内容的识别不会自动成为跨模态冲突。
本阶段只传递显式冲突，不做自然语言矛盾识别、冲突消解、说话人融合、文本去重或纠错。
已有冲突/低置信度事实不会因补入其它观测而被静默抹去。

## 资源上限与失败

默认：单批 256 条观测、单素材 1024 条观测/1024 条引用、单观测 64 KiB、
单素材 Protobuf 4 MiB、Struct 深度 32/节点数 4096、素材窗口最多 300 秒。
策略最多 32 个模态；控制身份字段最多 256 字节。可通过 `FusionLimits` 显式配置。
先验证嵌套 payload 再计算 Protobuf 长度；非有限浮点与缺失 Value kind 显式拒绝。
所有限额都整批拒绝，既不截断事实也不部分提交。

核心无 I/O、线程、重试或内部队列。Runtime 后续负责有界任务调度、deadline、取消与重试；
不得因本核心有输入上限而声称整个 Pipeline 已具备这些能力。
失败为稳定 `ContractError` reason code，成功 report 只携带计数/标识符，不打印 payload。

## 验证与集成边界

Rust 按项目现有工具链例外在主机执行：

```sh
cargo fmt -p sensoryplex-timeline -- --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
```

`tests/fusion.rs` 使用明确标记的契约 fixture，覆盖聚合、重放、不可变事实、迟到观测、
半开区间、来源、质量状态、置信度、revision 和资源边界。它不是媒体 E2E 或质量验收。
本轮执行环境、检查结果与未验证范围见 [VERIFICATION.md](VERIFICATION.md)。

本阶段不修改 Proto、插件、SDK、Runtime、API、数据库迁移、Compose 或 Makefile。
`Cargo.lock` 只给本 crate 增加已有的 `prost` / `prost-types` 依赖关系，不升级任何依赖版本。
独立 worktree 保留这些改动，M8 工作目录不受影响。公共 TODO/实现状态/验证汇总由合并时统一更新。

后续接线：Runtime 选择窗口及原片映射 → 本核心 → 事务性追加 MaterialUnit/血缘/outbox。
此路径、真实媒体端到端、跨模态语义冲突判定、Milvus 与语义搜索尚未由本次改动实现或验收。
