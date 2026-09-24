pub mod edge {
    pub mod material {
        pub mod common {
            pub mod v1 {
                tonic::include_proto!("edge.material.common.v1");
            }
        }
        #[allow(clippy::module_inception)] // Preserve the versioned protobuf package hierarchy.
        pub mod material {
            pub mod v1 {
                tonic::include_proto!("edge.material.material.v1");
            }
        }
        pub mod media {
            pub mod v1 {
                tonic::include_proto!("edge.material.media.v1");
            }
        }
        pub mod runtime {
            pub mod v1 {
                tonic::include_proto!("edge.material.runtime.v1");
            }
        }
        pub mod gateway {
            pub mod v1 {
                tonic::include_proto!("edge.material.gateway.v1");
            }
        }
        pub mod index {
            pub mod v1 {
                tonic::include_proto!("edge.material.index.v1");
            }
        }
        pub mod node {
            pub mod v1 {
                tonic::include_proto!("edge.material.node.v1");
            }
        }
    }
}

pub use edge::material::{
    common::v1 as common, gateway::v1 as gateway, index::v1 as index, node::v1 as node, material::v1 as material,
    media::v1 as media, runtime::v1 as runtime,
};

#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct ContractError(pub &'static str);

pub fn validate_range(range: &common::TimeRange) -> Result<(), ContractError> {
    if range.start_ms < 0 || range.end_ms <= range.start_ms {
        return Err(ContractError("invalid_half_open_time_range"));
    }
    Ok(())
}

pub fn validate_digest(value: &str) -> Result<(), ContractError> {
    match value.strip_prefix("sha256:") {
        Some(hash)
            if hash.len() == 64
                && hash
                    .bytes()
                    .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c)) =>
        {
            Ok(())
        }
        _ => Err(ContractError("invalid_sha256_digest")),
    }
}

pub fn validate_observation(value: &material::Observation) -> Result<(), ContractError> {
    if [
        &value.observation_id,
        &value.stream_id,
        &value.source_id,
        &value.source_item_id,
        &value.modality,
        &value.timing_source,
    ]
    .iter()
    .any(|s| s.is_empty())
    {
        return Err(ContractError("missing_observation_identity"));
    }
    validate_range(
        value
            .time_range
            .as_ref()
            .ok_or(ContractError("missing_time_range"))?,
    )?;
    validate_digest(&value.content_hash)?;
    for confidence in [value.confidence, value.timing_confidence]
        .into_iter()
        .flatten()
    {
        if !confidence.is_finite() || !(0.0..=1.0).contains(&confidence) {
            return Err(ContractError("invalid_confidence"));
        }
    }
    if value.confidence.is_none() && value.confidence_unavailable_reason.is_empty() {
        return Err(ContractError("missing_confidence_reason"));
    }
    if value.created_at_unix_ms <= 0 {
        return Err(ContractError("missing_creation_time"));
    }
    if ![
        "partial",
        "final",
        "accepted",
        "rejected",
        "low_confidence",
        "failed",
        "conflict",
    ]
    .contains(&value.quality_state.as_str())
    {
        return Err(ContractError("invalid_quality_state"));
    }
    let p = value
        .provenance
        .as_ref()
        .ok_or(ContractError("missing_provenance"))?;
    if [
        &p.plugin,
        &p.plugin_version,
        &p.model_release_id,
        &p.model_id,
        &p.model_version,
        &p.execution_backend,
    ]
    .iter()
    .any(|s| s.is_empty())
    {
        return Err(ContractError("incomplete_provenance"));
    }
    validate_digest(&p.artifact_digest)?;
    validate_digest(&p.model_artifact_digest)?;
    validate_digest(&p.config_hash)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_invalid_ranges_and_digests() {
        for (start_ms, end_ms) in [(-1, 1), (5, 5), (6, 5)] {
            assert!(validate_range(&common::TimeRange { start_ms, end_ms }).is_err());
        }
        assert!(validate_range(&common::TimeRange {
            start_ms: 0,
            end_ms: 1
        })
        .is_ok());
        assert!(validate_digest("sha256:placeholder").is_err());
        assert!(validate_digest(&format!("sha256:{}", "a".repeat(64))).is_ok());
    }
}
