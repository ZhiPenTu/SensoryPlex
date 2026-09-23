//! 只处理调用方明确指定的时间窗；不裁剪观测，不推断模型文字之间的事实矛盾。

use std::collections::{BTreeMap, BTreeSet};

use prost::Message;
use prost_types::value::Kind;
use sensoryplex_sdk::{
    common::TimeRange,
    material::{MaterialUnit, Observation, SourceReference},
    validate_digest, validate_observation, validate_range, ContractError,
};

use crate::validate_material;

/// 每次调用与每个素材的硬上限；超限整批拒绝，不保留半成品。
#[derive(Clone, Debug)]
pub struct FusionLimits {
    pub max_batch_observations: usize,
    pub max_material_observations: usize,
    pub max_source_refs: usize,
    pub max_observation_bytes: usize,
    pub max_material_bytes: usize,
    pub max_payload_depth: usize,
    pub max_payload_values: usize,
    pub max_window_ms: i64,
}

impl Default for FusionLimits {
    fn default() -> Self {
        Self {
            max_batch_observations: 256,
            max_material_observations: 1024,
            max_source_refs: 1024,
            max_observation_bytes: 64 * 1024,
            max_material_bytes: 4 * 1024 * 1024,
            max_payload_depth: 32,
            max_payload_values: 4096,
            max_window_ms: 300_000,
        }
    }
}

/// readiness 按 modality 判断；调用方必须把此策略版本纳入 pipeline_version。
#[derive(Clone, Debug)]
pub struct FusionPolicy {
    pub pipeline_version: String,
    pub required_modalities: Vec<String>,
    pub enrichment_modalities: Vec<String>,
    pub limits: FusionLimits,
}

/// 已由调用方解析的素材身份与原始流上下文；同一素材的后续 revision 不可改变它。
#[derive(Clone, Debug)]
pub struct MaterialScope {
    pub material_unit_id: String,
    pub stream_id: String,
    pub source_id: String,
    pub time_range: TimeRange,
}

/// 原片引用必须由可信的媒体目录解析，不能用帧摘要冒充原视频摘要。
#[derive(Clone, Debug)]
pub struct FusionInput {
    pub observation: Observation,
    pub source_ref: SourceReference,
}

/// 所有时间由调用方提供，核心不读时钟、不访问存储、不修改 previous。
pub struct FusionRequest<'a> {
    pub scope: &'a MaterialScope,
    pub inputs: &'a [FusionInput],
    pub previous: Option<&'a MaterialUnit>,
    pub created_at_unix_ms: i64,
}

/// 仅含计数与标识符，不携带 payload；未知置信度独立计数，不折算为低置信度。
#[derive(Debug, Default, PartialEq, Eq)]
pub struct FusionReport {
    pub received_observations: usize,
    pub added_observations: usize,
    pub duplicate_observations: usize,
    pub partial_observations: usize,
    pub rejected_observations: usize,
    pub failed_observations: usize,
    pub conflict_observation_ids: Vec<String>,
    pub low_confidence_observation_ids: Vec<String>,
    pub unknown_confidence_observations: usize,
    pub unknown_timing_confidence_observations: usize,
    pub missing_required_modalities: Vec<String>,
    pub pending_enrichment_modalities: Vec<String>,
}

/// changed=false 时 material 与 previous 完全相同，调用方无需追加 revision。
#[derive(Debug)]
pub struct FusionOutcome {
    pub material: MaterialUnit,
    pub changed: bool,
    pub report: FusionReport,
}

/// 无内部队列、线程与可变状态，可由 Runtime 在有界调度器内重复调用。
pub struct FusionEngine {
    policy: FusionPolicy,
}

impl FusionEngine {
    pub fn new(mut policy: FusionPolicy) -> Result<Self, ContractError> {
        let limits = &policy.limits;
        if !valid_label(&policy.pipeline_version)
            || policy.required_modalities.is_empty()
            || policy.required_modalities.len() + policy.enrichment_modalities.len() > 32
            || limits.max_batch_observations == 0
            || limits.max_material_observations == 0
            || limits.max_source_refs == 0
            || limits.max_observation_bytes == 0
            || limits.max_material_bytes < limits.max_observation_bytes
            || limits.max_payload_depth == 0
            || limits.max_payload_depth > 64
            || limits.max_payload_values == 0
            || limits.max_window_ms <= 0
        {
            return Err(ContractError("invalid_fusion_policy"));
        }
        let mut names = BTreeSet::new();
        for name in policy
            .required_modalities
            .iter()
            .chain(&policy.enrichment_modalities)
        {
            if !valid_label(name) || !names.insert(name) {
                return Err(ContractError("invalid_fusion_modalities"));
            }
        }
        policy.required_modalities.sort();
        policy.enrichment_modalities.sort();
        Ok(Self { policy })
    }

    pub fn fuse(&self, request: FusionRequest<'_>) -> Result<FusionOutcome, ContractError> {
        let scope = request.scope;
        let limits = &self.policy.limits;
        if [&scope.material_unit_id, &scope.stream_id, &scope.source_id]
            .iter()
            .any(|s| !valid_label(s))
            || request.created_at_unix_ms <= 0
        {
            return Err(ContractError("invalid_fusion_identity"));
        }
        validate_range(&scope.time_range)?;
        if scope.time_range.end_ms - scope.time_range.start_ms > limits.max_window_ms {
            return Err(ContractError("fusion_window_limit_exceeded"));
        }
        if request.inputs.len() > limits.max_batch_observations {
            return Err(ContractError("fusion_batch_limit_exceeded"));
        }

        // 先校验并借用事实，限额通过后才复制到新的 MaterialUnit。
        let mut observations = BTreeMap::new();
        let mut references = References::default();
        let mut report = FusionReport {
            received_observations: request.inputs.len(),
            ..Default::default()
        };
        if let Some(previous) = request.previous {
            if previous.observations.len() > limits.max_material_observations
                || previous.source_refs.len() > limits.max_source_refs
                || previous.tags.len() > 64
                || previous.pending_enrichments.len() > 32
            {
                return Err(ContractError("fusion_previous_limit_exceeded"));
            }
            for observation in &previous.observations {
                self.validate_input(observation, scope)?;
            }
            if previous.encoded_len() > limits.max_material_bytes {
                return Err(ContractError("fusion_material_bytes_exceeded"));
            }
            validate_material(previous)?;
            if previous.superseded {
                return Err(ContractError("fusion_previous_superseded"));
            }
            if previous.material_unit_id != scope.material_unit_id
                || previous.stream_id != scope.stream_id
                || previous.time_range.as_ref() != Some(&scope.time_range)
                || previous.pipeline_version != self.policy.pipeline_version
            {
                return Err(ContractError("fusion_previous_identity_mismatch"));
            }
            // 已有素材的显式质量状态不得被当前策略重新推导后静默覆盖。
            let mut previous_report = FusionReport::default();
            let previous_status = self.classify(previous.observations.iter(), &mut previous_report);
            let mut expected_pending = previous_report.missing_required_modalities;
            expected_pending.extend(previous_report.pending_enrichment_modalities);
            expected_pending.sort();
            if previous.status != previous_status
                || previous.pending_enrichments != expected_pending
            {
                return Err(ContractError("fusion_previous_policy_mismatch"));
            }
            for reference in &previous.source_refs {
                references.insert(reference, &scope.time_range)?;
            }
            for observation in &previous.observations {
                if observations
                    .insert(observation.observation_id.as_str(), observation)
                    .is_some()
                {
                    return Err(ContractError("fusion_previous_duplicate_observation"));
                }
                if !previous.source_refs.iter().any(|r| {
                    contains(
                        r.time_range.as_ref().unwrap(),
                        observation.time_range.as_ref().unwrap(),
                    )
                }) {
                    return Err(ContractError("fusion_observation_source_uncovered"));
                }
            }
        }
        for input in request.inputs {
            self.validate_input(&input.observation, scope)?;
            references.insert(&input.source_ref, &scope.time_range)?;
            if !contains(
                input.source_ref.time_range.as_ref().unwrap(),
                input.observation.time_range.as_ref().unwrap(),
            ) {
                return Err(ContractError("fusion_observation_source_uncovered"));
            }
            match observations.get(input.observation.observation_id.as_str()) {
                Some(previous) if **previous != input.observation => {
                    return Err(ContractError("immutable_observation_conflict"));
                }
                Some(_) => report.duplicate_observations += 1,
                None => {
                    observations.insert(
                        input.observation.observation_id.as_str(),
                        &input.observation,
                    );
                    report.added_observations += 1;
                }
            }
            if observations.len() > limits.max_material_observations {
                return Err(ContractError("fusion_observation_limit_exceeded"));
            }
            if references.values.len() > limits.max_source_refs {
                return Err(ContractError("fusion_source_limit_exceeded"));
            }
        }
        if observations.is_empty() {
            return Err(ContractError("fusion_empty_observations"));
        }
        let content_bytes = observations
            .values()
            .map(|o| o.encoded_len())
            .sum::<usize>()
            + references
                .values
                .values()
                .map(|r| r.encoded_len())
                .sum::<usize>();
        if content_bytes > limits.max_material_bytes {
            return Err(ContractError("fusion_material_bytes_exceeded"));
        }

        let status = self.classify(observations.values().copied(), &mut report);
        let mut pending = report.missing_required_modalities.clone();
        pending.extend(report.pending_enrichment_modalities.iter().cloned());
        pending.sort();
        let mut sorted: Vec<_> = observations.into_values().collect();
        sorted.sort_by_key(|o| {
            let range = o.time_range.as_ref().unwrap();
            (
                range.start_ms,
                range.end_ms,
                o.modality.as_str(),
                o.observation_id.as_str(),
            )
        });
        let mut material = MaterialUnit {
            material_unit_id: scope.material_unit_id.clone(),
            stream_id: scope.stream_id.clone(),
            time_range: Some(scope.time_range),
            status: status.into(),
            revision: request.previous.map_or(1, |p| p.revision),
            observations: sorted.into_iter().cloned().collect(),
            tags: request.previous.map_or_else(Vec::new, |p| p.tags.clone()),
            source_refs: references.values.into_values().cloned().collect(),
            pipeline_version: self.policy.pipeline_version.clone(),
            pending_enrichments: pending,
            created_at_unix_ms: request
                .previous
                .map_or(request.created_at_unix_ms, |p| p.created_at_unix_ms),
            superseded: false,
        };
        let changed = request.previous != Some(&material);
        if changed {
            if let Some(previous) = request.previous {
                if request.created_at_unix_ms <= previous.created_at_unix_ms {
                    return Err(ContractError("fusion_revision_time_not_increasing"));
                }
                material.revision = previous
                    .revision
                    .checked_add(1)
                    .ok_or(ContractError("fusion_revision_overflow"))?;
            }
            material.created_at_unix_ms = request.created_at_unix_ms;
        }
        if material.encoded_len() > limits.max_material_bytes {
            return Err(ContractError("fusion_material_bytes_exceeded"));
        }
        validate_material(&material)?;
        Ok(FusionOutcome {
            material,
            changed,
            report,
        })
    }

    fn validate_input(
        &self,
        observation: &Observation,
        scope: &MaterialScope,
    ) -> Result<(), ContractError> {
        validate_payload(observation, &self.policy.limits)?;
        if observation.encoded_len() > self.policy.limits.max_observation_bytes {
            return Err(ContractError("fusion_observation_bytes_exceeded"));
        }
        validate_observation(observation)?;
        if observation.stream_id != scope.stream_id || observation.source_id != scope.source_id {
            return Err(ContractError("fusion_source_stream_mismatch"));
        }
        if !contains(&scope.time_range, observation.time_range.as_ref().unwrap()) {
            return Err(ContractError("fusion_observation_outside_window"));
        }
        Ok(())
    }

    fn classify<'a>(
        &self,
        observations: impl Iterator<Item = &'a Observation>,
        report: &mut FusionReport,
    ) -> &'static str {
        let mut ready = BTreeSet::new();
        let mut usable = false;
        for observation in observations {
            report.unknown_confidence_observations += usize::from(observation.confidence.is_none());
            report.unknown_timing_confidence_observations +=
                usize::from(observation.timing_confidence.is_none());
            match observation.quality_state.as_str() {
                "accepted" | "final" => {
                    ready.insert(observation.modality.as_str());
                    usable = true;
                }
                "partial" => {
                    report.partial_observations += 1;
                    usable = true;
                }
                "rejected" => report.rejected_observations += 1,
                "failed" => report.failed_observations += 1,
                "conflict" => report
                    .conflict_observation_ids
                    .push(observation.observation_id.clone()),
                "low_confidence" => report
                    .low_confidence_observation_ids
                    .push(observation.observation_id.clone()),
                _ => unreachable!("quality_state 已经通过契约校验"),
            }
        }
        report.missing_required_modalities = self
            .policy
            .required_modalities
            .iter()
            .filter(|m| !ready.contains(m.as_str()))
            .cloned()
            .collect();
        report.pending_enrichment_modalities = self
            .policy
            .enrichment_modalities
            .iter()
            .filter(|m| !ready.contains(m.as_str()))
            .cloned()
            .collect();
        if !report.conflict_observation_ids.is_empty() {
            "conflict"
        } else if !report.low_confidence_observation_ids.is_empty() {
            "low_confidence"
        } else if !usable {
            "failed"
        } else if !report.missing_required_modalities.is_empty() {
            "partial"
        } else if !self.policy.enrichment_modalities.is_empty()
            && report.pending_enrichment_modalities.is_empty()
        {
            "enriched"
        } else {
            "fast_ready"
        }
    }
}

fn valid_label(value: &str) -> bool {
    !value.trim().is_empty() && value.len() <= 256
}

fn contains(outer: &TimeRange, inner: &TimeRange) -> bool {
    outer.start_ms <= inner.start_ms && inner.end_ms <= outer.end_ms
}

#[derive(Default)]
struct References<'a> {
    values: BTreeMap<(&'a str, i64, i64), &'a SourceReference>,
    digests: BTreeMap<&'a str, &'a str>,
}

impl<'a> References<'a> {
    fn insert(
        &mut self,
        reference: &'a SourceReference,
        window: &TimeRange,
    ) -> Result<(), ContractError> {
        if !valid_label(&reference.asset_id) {
            return Err(ContractError("missing_asset_id"));
        }
        validate_digest(&reference.content_hash)?;
        let range = reference
            .time_range
            .as_ref()
            .ok_or(ContractError("missing_source_time_range"))?;
        validate_range(range)?;
        if !contains(window, range) {
            return Err(ContractError("source_reference_outside_material"));
        }
        if let Some(previous) = self
            .digests
            .insert(&reference.asset_id, &reference.content_hash)
        {
            if previous != reference.content_hash {
                return Err(ContractError("fusion_asset_digest_conflict"));
            }
        }
        self.values.insert(
            (&reference.asset_id, range.start_ms, range.end_ms),
            reference,
        );
        Ok(())
    }
}

/// 先用有界遍历约束 Struct 的深度/节点，再允许递归的 Protobuf 长度计算和复制。
fn validate_payload(observation: &Observation, limits: &FusionLimits) -> Result<(), ContractError> {
    let Some(payload) = &observation.payload else {
        return Ok(());
    };
    let mut stack: Vec<_> = Vec::new();
    if payload.fields.len() > limits.max_payload_values {
        return Err(ContractError("fusion_payload_limit_exceeded"));
    }
    stack.extend(payload.fields.values().map(|v| (v, 1)));
    let mut visited = 0;
    while let Some((value, depth)) = stack.pop() {
        visited += 1;
        if depth > limits.max_payload_depth {
            return Err(ContractError("fusion_payload_limit_exceeded"));
        }
        match value.kind.as_ref() {
            Some(Kind::StructValue(s)) => {
                if s.fields.len() > limits.max_payload_values - visited - stack.len() {
                    return Err(ContractError("fusion_payload_limit_exceeded"));
                }
                stack.extend(s.fields.values().map(|v| (v, depth + 1)));
            }
            Some(Kind::ListValue(l)) => {
                if l.values.len() > limits.max_payload_values - visited - stack.len() {
                    return Err(ContractError("fusion_payload_limit_exceeded"));
                }
                stack.extend(l.values.iter().map(|v| (v, depth + 1)));
            }
            Some(Kind::NumberValue(n)) if !n.is_finite() => {
                return Err(ContractError("fusion_invalid_payload"))
            }
            None => return Err(ContractError("fusion_invalid_payload")),
            _ => {}
        }
    }
    Ok(())
}
