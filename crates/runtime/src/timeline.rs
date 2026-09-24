//! Runtime → Timeline 接线（ADR-028）：真探测取原片身份 → 读真运行报告 → 选窗 → 融合 → 写素材。
//!
//! 本模块只做四件事，且**不打开数据库、不发事件、不做任何网络 RPC**：
//!
//! 1. 用 `FileSource::open` 真探测授权样本，取得可信的 `stream_id` / `source_id` / 整文件摘要 / 时长；
//! 2. 读 Runtime 自己的 replay 报告与 `tools/ai_worker.py` 的运行报告，交叉校验两者与探测结果一致；
//! 3. 由描述符账本（`frames[]` 的 `buffer_id` / `source_time_range_ms`）派生 timeline item，
//!    按 pipeline 声明的固定栅格选窗，逐窗调用 `FusionEngine::fuse()`；
//! 4. 写出 MaterialUnit（二进制 protobuf）与一份 JSON 报告。
//!
//! 素材事实的追加发生在 `tools/timeline_handoff.py`（授权写入口），**不在**本模块：
//! Rust 侧没有数据库/网络依赖，`crates/storage` 的 `MetadataStore` 端口仍然没有 adapter。
//!
//! 三条写死的语义（详见 ADR-028）：
//!
//! - 跨窗观测**显式拒绝**，不裁剪（裁剪会造出从未被观测的时间区间）；
//! - `material_unit_id` / `asset_id` / `created_at_unix_ms` 全部由输入稳定派生，同一批事实重放
//!   必须得到逐字节相同的素材，否则写侧的幂等判定（内容摘要相同 ⇒ 未新增）永远不成立；
//! - 报告必须写明"还没做"的部分（追加、向量、检索），不留给读者推断。

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use prost::Message;
use prost_types::{value::Kind, ListValue, Struct, Value as ProtoValue};
use sensoryplex_media::source::{FileSource, MediaSource};
use sensoryplex_sdk::common::TimeRange;
use sensoryplex_sdk::material::{MaterialUnit, Observation, Provenance, SourceReference};
use sensoryplex_sdk::media::ReplayReport;
use sensoryplex_sdk::{validate_digest, validate_observation, ContractError};
use serde::{Deserialize, Serialize};

use sensoryplex_runtime::{
    capability, sha256_hex, FusionEngine, FusionInput, FusionLimits, FusionReport, FusionRequest,
    MaterialScope, Pipeline, TimelinePolicy,
};

/// 单次运行产出的素材窗口上限。超限即拒绝，不做"只处理前 N 个"的静默截断。
const MAX_MATERIAL_WINDOWS: usize = 512;

const TIMELINE_USAGE: &str = "usage: sensoryplex-runtime timeline <pipeline.yaml> <media-path> \
--report <replay.pb> --worker-report <ai-worker.json> --material-dir <dir> --out <timeline.json>";

/// 命令行的两个位置参数与四个必需选项。没有可选项，也不接受"临时覆盖策略"——
/// 窗口与模态来自 pipeline 声明（ADR-028 §4），不是命令行开关。
pub struct TimelineArgs {
    pub pipeline: String,
    pub media: String,
    pub report: String,
    pub worker_report: String,
    pub material_dir: String,
    pub out: String,
}

impl TimelineArgs {
    pub fn parse(args: &[String]) -> Result<Self, String> {
        let mut positional = Vec::new();
        let mut options: BTreeMap<&str, String> = BTreeMap::new();
        let mut index = 0;
        while index < args.len() {
            let token = args[index].as_str();
            if token.starts_with("--") {
                if !matches!(
                    token,
                    "--report" | "--worker-report" | "--material-dir" | "--out"
                ) {
                    return Err(format!(
                        "unknown timeline option: {token}\n{TIMELINE_USAGE}"
                    ));
                }
                let value = args
                    .get(index + 1)
                    .ok_or_else(|| format!("{token} requires a value\n{TIMELINE_USAGE}"))?;
                if options.insert(token, value.clone()).is_some() {
                    return Err(format!("duplicate timeline option: {token}"));
                }
                index += 2;
                continue;
            }
            positional.push(token.to_string());
            index += 1;
        }
        if positional.len() != 2 {
            return Err(TIMELINE_USAGE.into());
        }
        let required = |name: &'static str| {
            options
                .get(name)
                .cloned()
                .ok_or_else(|| format!("timeline requires {name} <path>\n{TIMELINE_USAGE}"))
        };
        Ok(Self {
            pipeline: positional[0].clone(),
            media: positional[1].clone(),
            report: required("--report")?,
            worker_report: required("--worker-report")?,
            material_dir: required("--material-dir")?,
            out: required("--out")?,
        })
    }
}

/// 原片身份：只来自真探测，调用方无法提供，也无法覆盖。
#[derive(Clone, Debug, Serialize)]
pub struct SourceIdentity {
    pub stream_id: String,
    pub source_id: String,
    pub asset_id: String,
    pub content_hash: String,
    pub duration_ms: i64,
    pub probe_tool: String,
}

impl SourceIdentity {
    /// `asset-<摘要前 12 位>`：与探测侧的 `stream-`/`file-` 同一段摘要，
    /// 因此同一份原片永远得到同一个 asset 标识（不引入第二个身份来源）。
    fn asset_id_for(content_hash: &str) -> String {
        format!("asset-{}", content_hash.get(7..19).unwrap_or_default())
    }

    fn short(&self) -> &str {
        self.content_hash.get(7..19).unwrap_or_default()
    }
}

// ── worker 报告（tools/ai_worker.py）的读法 ──────────────────────────────────

/// 报告里的观测是 protojson（小驼峰）。这里按契约字段逐个读，**不给宽松兜底**：
/// 少一个必需字段就是不可解析，而不是"用默认值凑合"。
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct RawObservation {
    observation_id: String,
    modality: String,
    stream_id: String,
    source_id: String,
    source_item_id: String,
    time_range: RawRange,
    #[serde(default)]
    payload: BTreeMap<String, serde_json::Value>,
    #[serde(default)]
    confidence: Option<f64>,
    #[serde(default)]
    confidence_unavailable_reason: String,
    quality_state: String,
    #[serde(default)]
    quality_reasons: Vec<String>,
    provenance: RawProvenance,
    content_hash: String,
    #[serde(default, deserialize_with = "protojson_i64")]
    created_at_unix_ms: i64,
    timing_source: String,
    #[serde(default)]
    timing_confidence: Option<f64>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct RawRange {
    #[serde(default, deserialize_with = "protojson_i64")]
    start_ms: i64,
    #[serde(default, deserialize_with = "protojson_i64")]
    end_ms: i64,
}

/// proto3 JSON 的 int64 映射：**以字符串传输**，且零值字段被省略。
///
/// 因此这里同时接受字符串与数字，缺省即 0 —— 这不是"宽松兜底"，而是协议语义本身：
/// protojson 里 `startMs` 缺失就代表 0。把它当成不可解析，会让每一条从时间 0 开始的
/// 观测都被拒，最后只剩一个 `timeline_no_window_materialized` 兜底错误。
fn protojson_i64<'de, D>(deserializer: D) -> Result<i64, D::Error>
where
    D: serde::Deserializer<'de>,
{
    match Option::<serde_json::Value>::deserialize(deserializer)? {
        None | Some(serde_json::Value::Null) => Ok(0),
        Some(serde_json::Value::Number(number)) => number
            .as_i64()
            .ok_or_else(|| serde::de::Error::custom("protojson_int64_not_an_integer")),
        Some(serde_json::Value::String(text)) => text
            .parse::<i64>()
            .map_err(|_| serde::de::Error::custom("protojson_int64_unparsable")),
        Some(_) => Err(serde::de::Error::custom("protojson_int64_unsupported_type")),
    }
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct RawProvenance {
    plugin: String,
    plugin_version: String,
    artifact_digest: String,
    model_release_id: String,
    model_id: String,
    model_version: String,
    config_hash: String,
    execution_backend: String,
    model_artifact_digest: String,
}

#[derive(Debug, Deserialize)]
struct WorkerReport {
    #[serde(default)]
    input_mode: String,
    #[serde(default)]
    data_plane: Option<String>,
    #[serde(default)]
    failures: Vec<String>,
    #[serde(default)]
    observations: Vec<serde_json::Value>,
    #[serde(default)]
    frames: Vec<RawFrame>,
}

/// 一条描述符账目。`source_time_range_ms` 是 **Runtime 签发的窗口**。
#[derive(Debug, Deserialize)]
struct RawFrame {
    #[serde(default)]
    buffer_id: Option<String>,
    #[serde(default)]
    kind: Option<String>,
    #[serde(default)]
    source_digest: Option<String>,
    #[serde(default)]
    source_time_range_ms: Option<Vec<i64>>,
    #[serde(default)]
    observation_id: Option<String>,
}

/// 账本条目：item 的身份、种类与**描述符窗口**。
#[derive(Debug, Clone, Serialize)]
struct LedgerItem {
    item_id: String,
    kind: String,
    start_ms: i64,
    end_ms: i64,
}

/// 描述符账本。它证的是"这条窗口被 Runtime 授予过"，不是"模型看懂了画面"（ADR-028 §3）。
#[derive(Debug)]
struct Ledger {
    items: BTreeMap<String, LedgerItem>,
    observation_ids: BTreeMap<String, BTreeSet<String>>,
    frames_without_window: usize,
}

impl Ledger {
    /// 由 `frames[]` 派生 timeline item。
    ///
    /// `item_id` 与 SDK 的 `describe_source_item()` **同一算法独立复算**：
    /// `<buffer_id>@<摘要去掉 'sha256:' 后前 16 位>`。`buffer_id` 是 Runtime 在保留表里签发的
    /// 句柄，消费者伪造不出来，所以"这条观测确实来自那条被授予的窗口"是可复算的。
    fn build(report: &WorkerReport) -> Result<Self, String> {
        let mut items = BTreeMap::new();
        let mut observation_ids: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
        let mut frames_without_window = 0usize;
        for frame in &report.frames {
            let (Some(buffer_id), Some(digest), Some(range)) = (
                frame.buffer_id.as_ref(),
                frame.source_digest.as_ref(),
                frame.source_time_range_ms.as_ref(),
            ) else {
                // 插件调用失败的账目行不带描述符窗口（第二跳就没走到）。它们没有观测引用，
                // 因此不进账本，但必须作为计数如实上报，不能悄悄丢。
                frames_without_window += 1;
                continue;
            };
            if validate_digest(digest).is_err() {
                return Err(format!("worker_report_invalid_digest: {buffer_id}"));
            }
            if range.len() != 2 || range[1] <= range[0] || range[0] < 0 {
                return Err(format!("worker_report_invalid_window: {buffer_id}"));
            }
            let item_id = format!("{}@{}", buffer_id, &digest[7..23]);
            items.entry(item_id.clone()).or_insert_with(|| LedgerItem {
                item_id: item_id.clone(),
                kind: frame.kind.clone().unwrap_or_else(|| "unknown".to_string()),
                start_ms: range[0],
                end_ms: range[1],
            });
            if let Some(observation_id) = frame.observation_id.as_ref() {
                observation_ids
                    .entry(item_id)
                    .or_default()
                    .insert(observation_id.clone());
            }
        }
        Ok(Self {
            items,
            observation_ids,
            frames_without_window,
        })
    }

    /// 稳定顺序：`(start_ms, end_ms, item_id)`，与融合核心排序观测的口径一致。
    fn sorted(&self) -> Vec<LedgerItem> {
        let mut items: Vec<_> = self.items.values().cloned().collect();
        items.sort_by(|a, b| {
            (a.start_ms, a.end_ms, a.item_id.as_str()).cmp(&(
                b.start_ms,
                b.end_ms,
                b.item_id.as_str(),
            ))
        });
        items
    }
}

// ── 输出报告（JSON） ───────────────────────────────────────────────────────

#[derive(Serialize)]
struct PolicyView {
    name: String,
    window_ms: i64,
    fast_modalities: Vec<String>,
    enrichment_modalities: Vec<String>,
    pipeline_version: String,
    policy_digest: String,
}

#[derive(Serialize)]
struct RunView {
    replay_report: String,
    replay_golden_path_verified: bool,
    replay_blockers: Vec<String>,
    worker_report: String,
    worker_input_mode: String,
    worker_data_plane: String,
    worker_observations: usize,
    worker_frames: usize,
    worker_frames_without_descriptor_window: usize,
}

#[derive(Serialize)]
struct SourceRefView {
    asset_id: String,
    start_ms: i64,
    end_ms: i64,
    content_hash: String,
}

#[derive(Serialize)]
struct FusionView {
    received_observations: usize,
    added_observations: usize,
    duplicate_observations: usize,
    partial_observations: usize,
    rejected_observations: usize,
    failed_observations: usize,
    conflict_observation_ids: Vec<String>,
    low_confidence_observation_ids: Vec<String>,
    unknown_confidence_observations: usize,
    unknown_timing_confidence_observations: usize,
    missing_required_modalities: Vec<String>,
    pending_enrichment_modalities: Vec<String>,
}

impl From<&FusionReport> for FusionView {
    fn from(report: &FusionReport) -> Self {
        Self {
            received_observations: report.received_observations,
            added_observations: report.added_observations,
            duplicate_observations: report.duplicate_observations,
            partial_observations: report.partial_observations,
            rejected_observations: report.rejected_observations,
            failed_observations: report.failed_observations,
            conflict_observation_ids: report.conflict_observation_ids.clone(),
            low_confidence_observation_ids: report.low_confidence_observation_ids.clone(),
            unknown_confidence_observations: report.unknown_confidence_observations,
            unknown_timing_confidence_observations: report.unknown_timing_confidence_observations,
            missing_required_modalities: report.missing_required_modalities.clone(),
            pending_enrichment_modalities: report.pending_enrichment_modalities.clone(),
        }
    }
}

/// 一条素材的引用面：素材文件、摘要、身份与融合账目。事实本体在 `.material.pb` 里。
#[derive(Serialize)]
struct MaterialView {
    material_unit_id: String,
    start_ms: i64,
    end_ms: i64,
    status: String,
    revision: u32,
    created_at_unix_ms: i64,
    stream_id: String,
    source_id: String,
    pipeline_version: String,
    pending_enrichments: Vec<String>,
    observation_ids: Vec<String>,
    source_refs: Vec<SourceRefView>,
    material_file: String,
    material_digest: String,
    fusion: FusionView,
}

#[derive(Serialize)]
struct RejectedView {
    observation_id: String,
    reason: String,
}

#[derive(Serialize, Default)]
struct Counters {
    items: usize,
    windows_total: usize,
    windows_with_observations: usize,
    windows_empty: usize,
    observations_accepted: usize,
    observations_rejected: usize,
    materials: usize,
}

#[derive(Serialize)]
struct TimelineReport {
    contract: String,
    platform: String,
    pipeline: PolicyView,
    source: SourceIdentity,
    run: RunView,
    items: Vec<LedgerItem>,
    materials: Vec<MaterialView>,
    rejected: Vec<RejectedView>,
    counters: Counters,
    blockers: Vec<String>,
    golden_path_verified: bool,
}

/// 本命令**没有**做的事，逐条写进报告：追加、向量、检索都发生在别处。
/// 含糊或省略都会让读者把"融合成功"读成"素材已可检索"。
const BLOCKERS: [&str; 4] = [
    "metadata_append_not_exercised",
    "vector_index_not_exercised",
    "semantic_search_not_exercised",
    "golden_path_not_verified",
];

// ── 主流程 ─────────────────────────────────────────────────────────────────

pub fn run(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    let args = TimelineArgs::parse(args).map_err(std::io::Error::other)?;
    let pipeline = Pipeline::parse(&std::fs::read_to_string(&args.pipeline)?)
        .map_err(std::io::Error::other)?;
    let (policy, pipeline_version) = pipeline.timeline().map_err(std::io::Error::other)?;
    let engine = policy
        .engine(&pipeline_version)
        .map_err(std::io::Error::other)?;

    let identity = probe(&pipeline, &args.media)?;
    let replay = read_replay_report(&args.report, &identity)?;
    let worker = read_worker_report(&args.worker_report)?;
    let ledger = Ledger::build(&worker)?;
    let fused = fuse_windows(
        &engine,
        policy,
        &pipeline_version,
        &identity,
        &ledger,
        &worker,
    )?;

    std::fs::create_dir_all(&args.material_dir)?;
    let mut materials = Vec::new();
    for (index, material) in fused.materials.iter().enumerate() {
        let bytes = material.encode_to_vec();
        let file = format!("{}.material.pb", material.material_unit_id);
        std::fs::write(Path::new(&args.material_dir).join(&file), &bytes)?;
        materials.push(material_view(
            material,
            &file,
            &bytes,
            &identity.source_id,
            &fused.reports[index],
        ));
    }

    let counters = Counters {
        items: ledger.items.len(),
        materials: materials.len(),
        ..fused.counters
    };
    let document = TimelineReport {
        contract: "edge.material/v1/timeline-fusion".into(),
        platform: capability::platform(),
        pipeline: PolicyView {
            name: pipeline.metadata.name.clone(),
            window_ms: policy.window_ms,
            fast_modalities: policy.normalized().fast_modalities,
            enrichment_modalities: policy.normalized().enrichment_modalities,
            pipeline_version: pipeline_version.clone(),
            policy_digest: policy.digest(),
        },
        source: identity,
        run: RunView {
            replay_report: args.report.clone(),
            replay_golden_path_verified: replay.golden_path_verified,
            replay_blockers: replay.blockers.clone(),
            worker_report: args.worker_report.clone(),
            worker_input_mode: worker.input_mode.clone(),
            worker_data_plane: worker.data_plane.clone().unwrap_or_default(),
            worker_observations: worker.observations.len(),
            worker_frames: worker.frames.len(),
            worker_frames_without_descriptor_window: ledger.frames_without_window,
        },
        items: ledger.sorted(),
        materials,
        rejected: fused.rejected,
        counters,
        blockers: BLOCKERS.iter().map(|item| (*item).to_string()).collect(),
        golden_path_verified: false,
    };

    std::fs::write(&args.out, serde_json::to_vec_pretty(&document)?)?;
    println!(
        "timeline report written: pipeline={} stream={} items={} windows={}/{} materials={} rejected={} blockers={}",
        document.pipeline.name,
        document.source.stream_id,
        document.counters.items,
        document.counters.windows_with_observations,
        document.counters.windows_total,
        document.counters.materials,
        document.counters.observations_rejected,
        document.blockers.join(",")
    );
    println!(
        "timeline golden_path_verified=false: append / vector / search are not exercised here"
    );
    Ok(())
}

/// 真探测：`stream_id` / `source_id` / 整文件摘要 / 时长只能来自这里。
fn probe(pipeline: &Pipeline, media: &str) -> Result<SourceIdentity, Box<dyn std::error::Error>> {
    let source = FileSource::open(&pipeline.spec.source.uri_secret_ref, Path::new(media))
        .map_err(|error| std::io::Error::other(error.to_string()))?;
    let description = source.describe().clone();
    let reference = description
        .source
        .clone()
        .ok_or_else(|| std::io::Error::other("probe_missing_source_identity"))?;
    if description.duration_ms <= 0 {
        return Err(std::io::Error::other("probe_missing_duration").into());
    }
    if validate_digest(&reference.content_hash).is_err() {
        return Err(std::io::Error::other("probe_invalid_content_hash").into());
    }
    Ok(SourceIdentity {
        stream_id: reference.stream_id,
        source_id: reference.source_id,
        asset_id: SourceIdentity::asset_id_for(&reference.content_hash),
        content_hash: reference.content_hash,
        duration_ms: description.duration_ms,
        probe_tool: description.probe_tool,
    })
}

/// replay 报告必须描述**同一份原片**：否则那一轮的 lease 与这一轮探测不是同一条 stream。
fn read_replay_report(path: &str, identity: &SourceIdentity) -> Result<ReplayReport, String> {
    let bytes = std::fs::read(path).map_err(|_| format!("report_unreadable: {path}"))?;
    let report =
        ReplayReport::decode(bytes.as_slice()).map_err(|_| format!("report_unreadable: {path}"))?;
    let reference = report
        .source
        .as_ref()
        .and_then(|description| description.source.as_ref())
        .ok_or_else(|| "report_probe_mismatch: report carries no source identity".to_string())?;
    if reference.stream_id != identity.stream_id
        || reference.source_id != identity.source_id
        || reference.content_hash != identity.content_hash
    {
        return Err(
            "report_probe_mismatch: report identity differs from the freshly probed media".into(),
        );
    }
    Ok(report)
}

fn read_worker_report(path: &str) -> Result<WorkerReport, String> {
    let text =
        std::fs::read_to_string(path).map_err(|_| format!("worker_report_unreadable: {path}"))?;
    let report: WorkerReport =
        serde_json::from_str(&text).map_err(|_| format!("worker_report_unreadable: {path}"))?;
    if !report.failures.is_empty() {
        // 那一轮模型链路本身没成功：拿它写素材等于把失败变成一条"部分成功"的事实。
        return Err(format!(
            "worker_report_failed: {}",
            report.failures.join(",")
        ));
    }
    if report.observations.is_empty() {
        return Err("timeline_no_observations".into());
    }
    if report.input_mode != "buffer" {
        // observation 模式没有描述符账本，本切片不接（ADR-028 §9）。
        return Err(format!(
            "worker_report_not_buffer_mode: {}",
            report.input_mode
        ));
    }
    Ok(report)
}

/// 融合结果：素材本体 + 逐条拒绝记录（拒绝必须可复盘，不能只是"少了一条"）。
struct FusedSet {
    materials: Vec<MaterialUnit>,
    reports: Vec<FusionReport>,
    rejected: Vec<RejectedView>,
    counters: Counters,
}

/// 选窗 → 逐条准入 → 逐窗融合。所有拒绝都是"这条观测不进素材"，且必须出现在报告里。
fn fuse_windows(
    engine: &FusionEngine,
    policy: &TimelinePolicy,
    pipeline_version: &str,
    identity: &SourceIdentity,
    ledger: &Ledger,
    worker: &WorkerReport,
) -> Result<FusedSet, String> {
    let limits = FusionLimits::default();
    let window_ms = policy.window_ms;
    let windows_total = ((identity.duration_ms + window_ms - 1) / window_ms) as usize;
    if windows_total == 0 || windows_total > MAX_MATERIAL_WINDOWS {
        return Err(format!(
            "timeline_window_count_exceeded: {windows_total} windows for {} ms at {window_ms} ms",
            identity.duration_ms
        ));
    }

    let mut accepted: BTreeMap<usize, Vec<FusionInput>> = BTreeMap::new();
    let mut rejected = Vec::new();
    for raw in &worker.observations {
        let observation: RawObservation = match serde_json::from_value(raw.clone()) {
            Ok(observation) => observation,
            Err(_) => {
                rejected.push(RejectedView {
                    observation_id: raw
                        .get("observationId")
                        .and_then(|value| value.as_str())
                        .unwrap_or("unknown")
                        .to_string(),
                    reason: "observation_unparsable".into(),
                });
                continue;
            }
        };
        match admit(&limits, identity, ledger, policy, &observation) {
            Ok((window, input)) => accepted.entry(window).or_default().push(input),
            Err(reason) => rejected.push(RejectedView {
                observation_id: observation.observation_id,
                reason,
            }),
        }
    }

    let mut materials = Vec::new();
    let mut reports = Vec::new();
    let mut windows_with_observations = 0usize;
    for (window, inputs) in &accepted {
        let time_range = window_range(*window, window_ms, identity.duration_ms);
        // created_at 取观测的最大值（不是取当前时间）：同一批事实重放必须逐字节相同。
        let created_at_unix_ms = inputs
            .iter()
            .map(|input| input.observation.created_at_unix_ms)
            .max()
            .ok_or_else(|| "timeline_empty_window".to_string())?;
        let scope = MaterialScope {
            material_unit_id: material_id(identity, time_range.start_ms),
            stream_id: identity.stream_id.clone(),
            source_id: identity.source_id.clone(),
            time_range,
        };
        let outcome = engine
            .fuse(FusionRequest {
                scope: &scope,
                inputs,
                // revision 前进需要一个能读事实的调用方（ADR-028 §9）；本切片一律 revision=1。
                previous: None,
                created_at_unix_ms,
            })
            .map_err(|ContractError(reason)| format!("fusion_rejected: {reason}"))?;
        if !outcome.changed {
            // previous=None 时 changed 必须是 true；为 false 说明核心与调用方的理解已经分叉。
            return Err("fusion_reported_no_change".into());
        }
        if outcome.material.pipeline_version != pipeline_version {
            return Err("fusion_policy_version_mismatch".into());
        }
        windows_with_observations += 1;
        materials.push(outcome.material);
        reports.push(outcome.report);
    }
    if materials.is_empty() {
        // 兜底错误必须带出拒绝原因计数：否则"一条都没通过"只能靠重放整条链路来定位。
        let mut counts: BTreeMap<&str, usize> = BTreeMap::new();
        for entry in &rejected {
            *counts.entry(entry.reason.as_str()).or_default() += 1;
        }
        let detail = counts
            .iter()
            .map(|(reason, count)| format!("{reason}={count}"))
            .collect::<Vec<_>>()
            .join(" ");
        return Err(format!("timeline_no_window_materialized: {detail}"));
    }
    let rejected_count = rejected.len();
    Ok(FusedSet {
        materials,
        reports,
        rejected,
        counters: Counters {
            items: ledger.items.len(),
            windows_total,
            windows_with_observations,
            windows_empty: windows_total - windows_with_observations,
            observations_accepted: accepted.values().map(Vec::len).sum(),
            observations_rejected: rejected_count,
            materials: 0,
        },
    })
}

/// 单条观测的准入：身份 → 账本 → 区间 → 载荷限制 → 窗口归属。
/// 任何一条不成立都返回稳定原因串，并且**不**把这条观测塞进任何窗口。
fn admit(
    limits: &FusionLimits,
    identity: &SourceIdentity,
    ledger: &Ledger,
    policy: &TimelinePolicy,
    raw: &RawObservation,
) -> Result<(usize, FusionInput), String> {
    if raw.stream_id != identity.stream_id {
        return Err("observation_stream_mismatch".into());
    }
    if raw.source_id != identity.source_id {
        return Err("observation_source_mismatch".into());
    }
    let item = ledger
        .items
        .get(&raw.source_item_id)
        .ok_or_else(|| "observation_without_descriptor_ledger".to_string())?;
    if let Some(declared) = ledger.observation_ids.get(&raw.source_item_id) {
        if !declared.contains(&raw.observation_id) {
            return Err("observation_ledger_observation_mismatch".into());
        }
    }
    let observation = to_observation(raw, limits)?;
    validate_observation(&observation)
        .map_err(|ContractError(reason)| format!("observation_contract_invalid: {reason}"))?;
    if observation.encoded_len() > limits.max_observation_bytes {
        return Err("observation_too_large".into());
    }
    let range = observation
        .time_range
        .ok_or_else(|| "observation_contract_invalid: missing_time_range".to_string())?;
    if range.start_ms != item.start_ms || range.end_ms != item.end_ms {
        // 观测区间与账本声明的描述符窗口必须是同一段：否则要么账本说错，要么观测改锚点。
        return Err("observation_ledger_range_mismatch".into());
    }

    let window_ms = policy.window_ms;
    let window = (range.start_ms / window_ms) as usize;
    let bounds = window_range(window, window_ms, identity.duration_ms);
    if range.end_ms > bounds.end_ms || range.start_ms < bounds.start_ms {
        // 跨窗观测不裁剪：裁掉的部分是"从未被观测的时间区间"。
        return Err("observation_crosses_window".into());
    }
    let reference = TimeRange {
        start_ms: item.start_ms,
        end_ms: item.end_ms,
    };
    if reference.start_ms < bounds.start_ms || reference.end_ms > bounds.end_ms {
        return Err("source_reference_crosses_material_boundary".into());
    }
    Ok((
        window,
        FusionInput {
            observation,
            source_ref: SourceReference {
                asset_id: identity.asset_id.clone(),
                time_range: Some(reference),
                // 原片引用必须用**整文件**摘要：用帧摘要冒充原片摘要会被融合核心拒绝。
                content_hash: identity.content_hash.clone(),
            },
        },
    ))
}

fn window_range(window: usize, window_ms: i64, duration_ms: i64) -> TimeRange {
    let start_ms = window as i64 * window_ms;
    TimeRange {
        start_ms,
        end_ms: (start_ms + window_ms).min(duration_ms),
    }
}

fn material_id(identity: &SourceIdentity, start_ms: i64) -> String {
    format!("material-{}-{}", identity.short(), start_ms)
}

fn material_view(
    material: &MaterialUnit,
    file: &str,
    bytes: &[u8],
    source_id: &str,
    report: &FusionReport,
) -> MaterialView {
    MaterialView {
        material_unit_id: material.material_unit_id.clone(),
        start_ms: material
            .time_range
            .as_ref()
            .map(|r| r.start_ms)
            .unwrap_or(0),
        end_ms: material.time_range.as_ref().map(|r| r.end_ms).unwrap_or(0),
        status: material.status.clone(),
        revision: material.revision,
        created_at_unix_ms: material.created_at_unix_ms,
        stream_id: material.stream_id.clone(),
        // `MaterialUnit` 里没有 source 字段（素材属于 stream）；运行级的 source 由探测结果给出，
        // 准入阶段已经逐条要求观测的 `source_id` 与它一致。
        source_id: source_id.to_string(),
        pipeline_version: material.pipeline_version.clone(),
        pending_enrichments: material.pending_enrichments.clone(),
        observation_ids: material
            .observations
            .iter()
            .map(|observation| observation.observation_id.clone())
            .collect(),
        source_refs: material
            .source_refs
            .iter()
            .map(|reference| SourceRefView {
                asset_id: reference.asset_id.clone(),
                start_ms: reference
                    .time_range
                    .as_ref()
                    .map(|r| r.start_ms)
                    .unwrap_or(0),
                end_ms: reference.time_range.as_ref().map(|r| r.end_ms).unwrap_or(0),
                content_hash: reference.content_hash.clone(),
            })
            .collect(),
        material_file: file.to_string(),
        material_digest: format!("sha256:{}", sha256_hex(bytes)),
        fusion: FusionView::from(report),
    }
}

/// protojson → 真的 `google.protobuf.Struct`。
///
/// 不发明一层"看起来等价"的映射：数字一律 `NumberValue`，`null` 是显式的 `NullValue`
/// （不是"空值"），并且在这里就按融合核心的同一组上限计数——超限的载荷在**准入阶段**
/// 被拒绝，而不是等到 `fuse()` 才炸掉整个窗口。
fn to_observation(raw: &RawObservation, limits: &FusionLimits) -> Result<Observation, String> {
    Ok(Observation {
        observation_id: raw.observation_id.clone(),
        modality: raw.modality.clone(),
        stream_id: raw.stream_id.clone(),
        source_id: raw.source_id.clone(),
        source_item_id: raw.source_item_id.clone(),
        time_range: Some(TimeRange {
            start_ms: raw.time_range.start_ms,
            end_ms: raw.time_range.end_ms,
        }),
        payload: Some(build_struct(&raw.payload, limits)?),
        confidence: raw.confidence,
        confidence_unavailable_reason: raw.confidence_unavailable_reason.clone(),
        quality_state: raw.quality_state.clone(),
        quality_reasons: raw.quality_reasons.clone(),
        provenance: Some(Provenance {
            plugin: raw.provenance.plugin.clone(),
            plugin_version: raw.provenance.plugin_version.clone(),
            artifact_digest: raw.provenance.artifact_digest.clone(),
            model_release_id: raw.provenance.model_release_id.clone(),
            model_id: raw.provenance.model_id.clone(),
            model_version: raw.provenance.model_version.clone(),
            config_hash: raw.provenance.config_hash.clone(),
            execution_backend: raw.provenance.execution_backend.clone(),
            model_artifact_digest: raw.provenance.model_artifact_digest.clone(),
        }),
        content_hash: raw.content_hash.clone(),
        created_at_unix_ms: raw.created_at_unix_ms,
        timing_source: raw.timing_source.clone(),
        timing_confidence: raw.timing_confidence,
    })
}

fn build_struct(
    fields: &BTreeMap<String, serde_json::Value>,
    limits: &FusionLimits,
) -> Result<Struct, String> {
    let mut values = BTreeMap::new();
    for (key, value) in fields {
        values.insert(key.clone(), build_value(value)?);
    }
    let payload = Struct { fields: values };
    // 先构造再计数：上限口径必须与融合核心的 `validate_payload` 逐字一致，
    // 在转换过程中顺手减预算会让"能不能通过"变成两套略有差别的规则。
    validate_payload_limits(&payload, limits)?;
    Ok(payload)
}

fn build_value(value: &serde_json::Value) -> Result<ProtoValue, String> {
    let kind = match value {
        serde_json::Value::Null => Kind::NullValue(0),
        serde_json::Value::Bool(flag) => Kind::BoolValue(*flag),
        serde_json::Value::Number(number) => match number.as_f64() {
            // JSON 里的数在 protojson 里一律是 double；这里不接受"差不多"的转换。
            Some(number) if number.is_finite() => Kind::NumberValue(number),
            _ => return Err("observation_payload_unsupported".into()),
        },
        serde_json::Value::String(text) => Kind::StringValue(text.clone()),
        serde_json::Value::Array(items) => {
            let mut values = Vec::with_capacity(items.len());
            for item in items {
                values.push(build_value(item)?);
            }
            Kind::ListValue(ListValue { values })
        }
        serde_json::Value::Object(fields) => {
            let mut values = BTreeMap::new();
            for (key, item) in fields {
                values.insert(key.clone(), build_value(item)?);
            }
            Kind::StructValue(Struct { fields: values })
        }
    };
    Ok(ProtoValue { kind: Some(kind) })
}

/// 与融合核心 `validate_payload` 同一口径的深度/节点计数。
///
/// 放在准入阶段是为了让"载荷太大"只拒绝**这一条观测**，而不是让整窗 `fuse()` 失败：
/// 一条超大 payload 不该把同窗其它观测一起变成没有素材。
fn validate_payload_limits(payload: &Struct, limits: &FusionLimits) -> Result<(), String> {
    if payload.fields.len() > limits.max_payload_values {
        return Err("observation_payload_limit_exceeded".into());
    }
    let mut stack: Vec<(&ProtoValue, usize)> =
        payload.fields.values().map(|value| (value, 1)).collect();
    let mut visited = 0usize;
    while let Some((value, depth)) = stack.pop() {
        visited += 1;
        if depth > limits.max_payload_depth {
            return Err("observation_payload_limit_exceeded".into());
        }
        match value.kind.as_ref() {
            Some(Kind::StructValue(inner)) => {
                if inner.fields.len() > limits.max_payload_values - visited - stack.len() {
                    return Err("observation_payload_limit_exceeded".into());
                }
                stack.extend(inner.fields.values().map(|value| (value, depth + 1)));
            }
            Some(Kind::ListValue(list)) => {
                if list.values.len() > limits.max_payload_values - visited - stack.len() {
                    return Err("observation_payload_limit_exceeded".into());
                }
                stack.extend(list.values.iter().map(|value| (value, depth + 1)));
            }
            Some(Kind::NumberValue(number)) if !number.is_finite() => {
                return Err("observation_payload_unsupported".into())
            }
            None => return Err("observation_payload_unsupported".into()),
            _ => {}
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn identity() -> SourceIdentity {
        SourceIdentity {
            stream_id: "stream-0123456789ab".into(),
            source_id: "file-0123456789ab".into(),
            asset_id: "asset-0123456789ab".into(),
            content_hash: format!("sha256:{}", "0".repeat(64)),
            duration_ms: 30_000,
            probe_tool: "ffprobe-test".into(),
        }
    }

    fn digest(seed: char) -> String {
        format!("sha256:{}", seed.to_string().repeat(64))
    }

    fn frame(buffer_id: &str, seed: char, start_ms: i64, end_ms: i64) -> RawFrame {
        RawFrame {
            buffer_id: Some(buffer_id.into()),
            kind: Some("video_frame".into()),
            source_digest: Some(digest(seed)),
            source_time_range_ms: Some(vec![start_ms, end_ms]),
            observation_id: Some(format!("obs_{seed}")),
        }
    }

    fn worker(frames: Vec<RawFrame>) -> WorkerReport {
        WorkerReport {
            input_mode: "buffer".into(),
            data_plane: Some("127.0.0.1:1".into()),
            failures: Vec::new(),
            observations: Vec::new(),
            frames,
        }
    }

    fn raw_observation(item_id: &str, seed: char, start_ms: i64, end_ms: i64) -> RawObservation {
        serde_json::from_value(observation_json(item_id, seed, start_ms, end_ms))
            .expect("the fixture is a valid protojson observation")
    }

    /// 与 `tools/ai_worker.py` 写进报告的形态一致（protojson 小驼峰）。
    fn observation_json(
        item_id: &str,
        seed: char,
        start_ms: i64,
        end_ms: i64,
    ) -> serde_json::Value {
        serde_json::json!({
            "observationId": format!("obs_{seed}"),
            "modality": "vision.scene_description",
            "streamId": "stream-0123456789ab",
            "sourceId": "file-0123456789ab",
            "sourceItemId": item_id,
            "timeRange": {"startMs": start_ms, "endMs": end_ms},
            "payload": {"text": "一盏台灯"},
            "confidenceUnavailableReason": "model_does_not_report_calibrated_confidence",
            "qualityState": "final",
            "provenance": {
                "plugin": "vlm-moondream",
                "pluginVersion": "0",
                "artifactDigest": digest('a'),
                "modelReleaseId": "ollama:moondream:v2@ad0714b7b564",
                "modelId": "moondream:v2",
                "modelVersion": "2",
                "configHash": digest('b'),
                "executionBackend": "ollama-0.4.1",
                "modelArtifactDigest": digest('c'),
            },
            "contentHash": digest(seed),
            "createdAtUnixMs": 1_790_000_000_000i64,
            "timingSource": "media_pts",
        })
    }

    fn policy() -> TimelinePolicy {
        TimelinePolicy {
            window_ms: 5_000,
            fast_modalities: vec!["vision.scene_description".into()],
            enrichment_modalities: Vec::new(),
        }
    }

    /// 真 protojson（`json_format.MessageToDict`）把 int64 写成**字符串**，并省略零值字段。
    /// 这是"单测全绿、真链路一条都过不了"的典型边界：必须先钉死。
    #[test]
    fn observation_parses_real_protojson_int64_encoding() {
        let mut document = observation_json(&format!("buffer-1@{}", "d".repeat(16)), 'd', 0, 33);
        let object = document.as_object_mut().expect("fixture is an object");
        object.insert(
            "createdAtUnixMs".into(),
            serde_json::Value::String("1790251451494".into()),
        );
        // `startMs = 0` 在 protojson 里就是"字段不存在"。
        object.insert("timeRange".into(), serde_json::json!({ "endMs": "33" }));
        let observation: RawObservation =
            serde_json::from_value(document).expect("real protojson must parse");
        assert_eq!(observation.created_at_unix_ms, 1_790_251_451_494);
        assert_eq!(observation.time_range.start_ms, 0);
        assert_eq!(observation.time_range.end_ms, 33);
    }

    /// 缺省等于 0 只对"缺省值"成立：写成非法的 int64 必须拒绝，不能悄悄当成 0。
    #[test]
    fn observation_rejects_malformed_protojson_int64() {
        let mut document = observation_json(&format!("buffer-1@{}", "d".repeat(16)), 'd', 0, 33);
        document["timeRange"] = serde_json::json!({ "startMs": "0", "endMs": "不是数字" });
        assert!(serde_json::from_value::<RawObservation>(document).is_err());
    }

    #[test]
    fn ledger_item_id_matches_the_sdk_algorithm() {
        let ledger = Ledger::build(&worker(vec![frame("buffer-1", 'd', 0, 33)])).unwrap();
        let expected = format!("buffer-1@{}", "d".repeat(16));
        assert!(ledger.items.contains_key(&expected));
        assert_eq!(ledger.observation_ids[&expected].len(), 1);
        assert_eq!(ledger.frames_without_window, 0);
    }

    #[test]
    fn ledger_dedupes_retries_and_counts_frames_without_a_window() {
        let mut retried = frame("buffer-1", 'd', 0, 33);
        retried.observation_id = Some("obs_d".into());
        let blind = RawFrame {
            buffer_id: Some("buffer-2".into()),
            kind: Some("video_frame".into()),
            source_digest: None,
            source_time_range_ms: None,
            observation_id: None,
        };
        let ledger =
            Ledger::build(&worker(vec![frame("buffer-1", 'd', 0, 33), retried, blind])).unwrap();
        assert_eq!(
            ledger.items.len(),
            1,
            "同一条 buffer 的重试必须收敛成一个 item"
        );
        assert_eq!(ledger.frames_without_window, 1);
    }

    #[test]
    fn ledger_rejects_a_malformed_digest_instead_of_guessing() {
        let mut broken = frame("buffer-1", 'd', 0, 33);
        broken.source_digest = Some("sha256:short".into());
        assert!(Ledger::build(&worker(vec![broken]))
            .expect_err("bad digest must fail")
            .starts_with("worker_report_invalid_digest"));
        let mut inverted = frame("buffer-1", 'd', 100, 100);
        inverted.source_time_range_ms = Some(vec![100, 100]);
        assert!(Ledger::build(&worker(vec![inverted]))
            .expect_err("half-open window must be non-empty")
            .starts_with("worker_report_invalid_window"));
    }

    #[test]
    fn window_grid_clamps_the_last_window_to_the_media_duration() {
        assert_eq!(window_range(0, 5_000, 30_000).end_ms, 5_000);
        assert_eq!(window_range(6, 5_000, 30_627).end_ms, 30_627);
        assert_eq!(window_range(6, 5_000, 30_627).start_ms, 30_000);
        assert_eq!(window_range(1, 1_000, 1_200).end_ms, 1_200);
    }

    #[test]
    fn identifiers_are_derived_from_the_media_digest() {
        let identity = identity();
        assert_eq!(
            SourceIdentity::asset_id_for(&identity.content_hash),
            "asset-000000000000"
        );
        assert_eq!(material_id(&identity, 5_000), "material-000000000000-5000");
    }

    #[test]
    fn admit_accepts_a_ledger_backed_observation_in_its_own_window() {
        let ledger = Ledger::build(&worker(vec![frame("buffer-1", 'd', 4_000, 4_033)])).unwrap();
        let item_id = format!("buffer-1@{}", "d".repeat(16));
        let (window, input) = admit(
            &FusionLimits::default(),
            &identity(),
            &ledger,
            &policy(),
            &raw_observation(&item_id, 'd', 4_000, 4_033),
        )
        .expect("a ledger-backed observation is admissible");
        assert_eq!(window, 0, "4000 ms 落在 [0,5000) 窗口");
        assert_eq!(input.observation.modality, "vision.scene_description");
        let reference = input.source_ref;
        assert_eq!(reference.content_hash, identity().content_hash);
        assert_eq!(reference.time_range.unwrap().end_ms, 4_033);
    }

    #[test]
    fn admit_rejects_every_identity_drift_with_a_stable_reason() {
        let ledger = Ledger::build(&worker(vec![frame("buffer-1", 'd', 0, 33)])).unwrap();
        let item_id = format!("buffer-1@{}", "d".repeat(16));
        let cases = [
            (
                {
                    let mut raw = raw_observation(&item_id, 'd', 0, 33);
                    raw.stream_id = "stream-other".into();
                    raw
                },
                "observation_stream_mismatch",
            ),
            (
                {
                    let mut raw = raw_observation(&item_id, 'd', 0, 33);
                    raw.source_id = "file-other".into();
                    raw
                },
                "observation_source_mismatch",
            ),
            (
                raw_observation("buffer-9@ffffffffffffffff", 'd', 0, 33),
                "observation_without_descriptor_ledger",
            ),
            (
                {
                    let mut raw = raw_observation(&item_id, 'd', 0, 33);
                    raw.observation_id = "obs_not_recorded".into();
                    raw
                },
                "observation_ledger_observation_mismatch",
            ),
            (
                raw_observation(&item_id, 'd', 0, 40),
                "observation_ledger_range_mismatch",
            ),
        ];
        for (raw, expected) in cases {
            let reason = admit(
                &FusionLimits::default(),
                &identity(),
                &ledger,
                &policy(),
                &raw,
            )
            .expect_err("drift must be rejected");
            assert_eq!(reason, expected);
        }
    }

    #[test]
    fn admit_rejects_cross_window_observations_instead_of_clipping_them() {
        // 描述符窗口 [4_000, 6_000) 跨越 [0,5000) 与 [5000,10000) 两个素材窗口。
        let ledger = Ledger::build(&worker(vec![frame("buffer-1", 'd', 4_000, 6_000)])).unwrap();
        let item_id = format!("buffer-1@{}", "d".repeat(16));
        let reason = admit(
            &FusionLimits::default(),
            &identity(),
            &ledger,
            &policy(),
            &raw_observation(&item_id, 'd', 4_000, 6_000),
        )
        .expect_err("crossing observations must be rejected");
        assert_eq!(reason, "observation_crosses_window");
    }

    #[test]
    fn admit_rejects_a_descriptor_window_that_crosses_the_material_boundary() {
        // 观测本身落在窗口内，但描述符窗口（原片引用）越过了素材边界。
        let ledger = Ledger::build(&worker(vec![frame("buffer-1", 'd', 4_900, 5_100)])).unwrap();
        let item_id = format!("buffer-1@{}", "d".repeat(16));
        let mut raw = raw_observation(&item_id, 'd', 4_950, 5_000);
        raw.observation_id = "obs_d".into();
        let reason = admit(
            &FusionLimits::default(),
            &identity(),
            &ledger,
            &policy(),
            &raw,
        )
        .expect_err("a straddling source reference must be rejected");
        // 观测区间与账本窗口不一致时先命中区间校验；这里两条都必须显式失败。
        assert!(
            matches!(
                reason.as_str(),
                "observation_ledger_range_mismatch" | "source_reference_crosses_material_boundary"
            ),
            "{reason}"
        );
    }

    #[test]
    fn payload_limits_are_enforced_before_fusion() {
        let limits = FusionLimits::default();
        let mut deep = serde_json::json!("leaf");
        for _ in 0..(limits.max_payload_depth + 1) {
            deep = serde_json::json!({ "nested": deep });
        }
        let fields = BTreeMap::from([("text".to_string(), deep)]);
        assert_eq!(
            build_struct(&fields, &limits).expect_err("too deep"),
            "observation_payload_limit_exceeded"
        );
        let wide: BTreeMap<String, serde_json::Value> = (0..(limits.max_payload_values + 1))
            .map(|index| (format!("k{index}"), serde_json::json!(1)))
            .collect();
        assert_eq!(
            build_struct(&wide, &limits).expect_err("too wide"),
            "observation_payload_limit_exceeded"
        );
        let ok = build_struct(
            &BTreeMap::from([
                ("text".to_string(), serde_json::json!("hello")),
                ("count".to_string(), serde_json::json!(3)),
                ("flag".to_string(), serde_json::json!(true)),
                ("nothing".to_string(), serde_json::Value::Null),
            ]),
            &limits,
        )
        .expect("a small payload is fine");
        assert!(matches!(
            ok.fields["count"].kind,
            Some(Kind::NumberValue(number)) if number == 3.0
        ));
        assert!(matches!(
            ok.fields["nothing"].kind,
            Some(Kind::NullValue(0))
        ));
    }

    #[test]
    fn fusing_the_same_facts_twice_is_byte_identical() {
        // 幂等的前提：同一批事实重放必须得到逐字节相同的素材（写侧只比对内容摘要）。
        let ledger = Ledger::build(&worker(vec![
            frame("buffer-1", 'd', 0, 33),
            frame("buffer-2", 'e', 4_000, 4_033),
        ]))
        .unwrap();
        let mut report = worker(vec![
            frame("buffer-1", 'd', 0, 33),
            frame("buffer-2", 'e', 4_000, 4_033),
        ]);
        report.observations = vec![
            observation_json(&format!("buffer-1@{}", "d".repeat(16)), 'd', 0, 33),
            observation_json(&format!("buffer-2@{}", "e".repeat(16)), 'e', 4_000, 4_033),
        ];
        let policy = policy();
        let version = policy.pipeline_version("file-material-poc");
        let engine = policy.engine(&version).unwrap();
        let first =
            fuse_windows(&engine, &policy, &version, &identity(), &ledger, &report).unwrap();
        let second =
            fuse_windows(&engine, &policy, &version, &identity(), &ledger, &report).unwrap();
        assert_eq!(first.materials.len(), 1, "两条观测同窗 ⇒ 一条素材");
        assert_eq!(first.rejected.len(), 0);
        assert_eq!(first.counters.windows_total, 6);
        assert_eq!(first.counters.windows_empty, 5);
        let bytes = first.materials[0].encode_to_vec();
        assert_eq!(bytes, second.materials[0].encode_to_vec());
        assert_eq!(first.materials[0].revision, 1);
        assert_eq!(first.materials[0].status, "fast_ready");
        assert_eq!(first.materials[0].created_at_unix_ms, 1_790_000_000_000);
    }

    #[test]
    fn timeline_arguments_are_all_or_nothing() {
        let base = |extra: &[&str]| -> Vec<String> {
            let mut args = vec![
                "p.yaml".to_string(),
                "m.mp4".to_string(),
                "--report".to_string(),
                "r.pb".to_string(),
                "--worker-report".to_string(),
                "w.json".to_string(),
                "--material-dir".to_string(),
                "materials".to_string(),
                "--out".to_string(),
                "t.json".to_string(),
            ];
            args.extend(extra.iter().map(|item| (*item).to_string()));
            args
        };
        let parsed = TimelineArgs::parse(&base(&[])).expect("complete arguments parse");
        assert_eq!(parsed.material_dir, "materials");
        assert!(TimelineArgs::parse(&base(&["--window-ms", "1000"])).is_err());
        assert!(TimelineArgs::parse(&base(&["--report", "again.pb"])).is_err());
        assert!(TimelineArgs::parse(&["p.yaml".to_string()]).is_err());
        let mut missing = base(&[]);
        missing.truncate(8);
        assert!(TimelineArgs::parse(&missing).is_err(), "--out is mandatory");
    }
}
