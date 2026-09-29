//! 有界、确定性的 Timeline 聚合与事实校验；不得合成模型结果或置信度。
#![doc = include_str!("../README.md")]
mod fusion;
pub use fusion::{
    FusionEngine, FusionInput, FusionLimits, FusionOutcome, FusionPolicy, FusionReport,
    FusionRequest, MaterialScope,
};
use sensoryplex_sdk::{
    material::MaterialUnit, validate_digest, validate_observation, validate_range, ContractError,
};

pub fn validate_material(material: &MaterialUnit) -> Result<(), ContractError> {
    if material.material_unit_id.is_empty()
        || material.stream_id.is_empty()
        || material.pipeline_version.is_empty()
        || material.revision == 0
        || material.created_at_unix_ms <= 0
    {
        return Err(ContractError("invalid_material_identity"));
    }
    if ![
        "partial",
        "fast_ready",
        "enriched",
        "failed",
        "conflict",
        "low_confidence",
    ]
    .contains(&material.status.as_str())
    {
        return Err(ContractError("invalid_material_status"));
    }
    let range = material
        .time_range
        .as_ref()
        .ok_or(ContractError("missing_time_range"))?;
    validate_range(range)?;
    if material.source_refs.is_empty() {
        return Err(ContractError("missing_source_references"));
    }
    // 来源切片允许先落库；没有观测时必须显式声明待补充，不能宣称完成。
    let pending_window = material.status == "partial" && !material.pending_enrichments.is_empty();
    if material.observations.is_empty() && material.status != "failed" && !pending_window {
        return Err(ContractError("missing_observations"));
    }
    for observation in &material.observations {
        validate_observation(observation)?;
        let r = observation.time_range.as_ref().unwrap();
        if observation.stream_id != material.stream_id
            || r.start_ms < range.start_ms
            || r.end_ms > range.end_ms
        {
            return Err(ContractError("observation_outside_material"));
        }
    }
    for reference in &material.source_refs {
        if reference.asset_id.is_empty() {
            return Err(ContractError("missing_asset_id"));
        }
        validate_digest(&reference.content_hash)?;
        let r = reference
            .time_range
            .as_ref()
            .ok_or(ContractError("missing_source_time_range"))?;
        validate_range(r)?;
        if r.start_ms < range.start_ms || r.end_ms > range.end_ms {
            return Err(ContractError("source_reference_outside_material"));
        }
    }
    if material.status == "enriched" && !material.pending_enrichments.is_empty() {
        return Err(ContractError("enrichment_not_complete"));
    }
    Ok(())
}
