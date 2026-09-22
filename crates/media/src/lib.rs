//! Data-plane admission, lease lifecycle and offline media probing.
//!
//! Buffer allocation/mapping and live GStreamer ingest remain Runtime-owned; this crate
//! either produces verified anchors or reports exactly why it cannot.
use sensoryplex_sdk::{common::BufferDescriptor, validate_digest, validate_range, ContractError};

pub mod arena;
#[cfg(feature = "gstreamer")]
pub mod decode;
pub mod descriptor;
pub mod lease;
pub mod probe;
pub mod segment;
pub mod source;

#[derive(Debug, thiserror::Error)]
pub enum MediaError {
    #[error("media_tool_missing: {0}")]
    ToolMissing(String),
    #[error("media_probe_failed: {0}")]
    ProbeFailed(String),
    #[error("invalid_probe_output: {0}")]
    InvalidProbeOutput(String),
    #[error("unsupported_media_source: {0}")]
    UnsupportedSource(String),
    #[error("media_io_failed: {0}")]
    IoFailed(String),
    #[error("arena_capacity_exceeded: requested {requested} bytes of {capacity}")]
    ArenaCapacityExceeded { requested: usize, capacity: usize },
    #[error("decode_failed: {0}")]
    DecodeFailed(String),
    #[error("descriptor_rejected: {0}")]
    DescriptorRejected(String),
}

pub fn validate_descriptor(
    buffer: &BufferDescriptor,
    now_ms: i64,
    supported: &[&str],
) -> Result<(), ContractError> {
    if buffer.buffer_id.is_empty() || buffer.stream_id.is_empty() || buffer.kind.is_empty() {
        return Err(ContractError("missing_buffer_identity"));
    }
    if !supported.contains(&buffer.memory_kind.as_str()) {
        return Err(ContractError("unsupported_memory_kind"));
    }
    validate_range(
        buffer
            .time_range
            .as_ref()
            .ok_or(ContractError("missing_time_range"))?,
    )?;
    validate_digest(&buffer.content_hash)?;
    let locator = buffer
        .locator
        .as_ref()
        .ok_or(ContractError("missing_locator"))?;
    if locator.handle.is_empty()
        || locator.length == 0
        || locator.offset.checked_add(locator.length).is_none()
    {
        return Err(ContractError("invalid_buffer_locator"));
    }
    let lease = buffer
        .lease
        .as_ref()
        .ok_or(ContractError("missing_lease"))?;
    if lease.lease_id.is_empty() || !lease.read_only || lease.expires_at_unix_ms <= now_ms {
        return Err(ContractError("invalid_or_expired_lease"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use sensoryplex_sdk::common::{BufferLease, BufferLocator, TimeRange};
    #[test]
    fn rejects_expired_writable_and_unknown_memory() {
        let mut b = BufferDescriptor {
            buffer_id: "buf".into(),
            stream_id: "stream".into(),
            kind: "video_frame".into(),
            memory_kind: "cpu_shared_memory".into(),
            content_hash: format!("sha256:{}", "0".repeat(64)),
            time_range: Some(TimeRange {
                start_ms: 0,
                end_ms: 1,
            }),
            locator: Some(BufferLocator {
                handle: "opaque".into(),
                offset: 0,
                length: 12,
            }),
            lease: Some(BufferLease {
                lease_id: "lease".into(),
                expires_at_unix_ms: 100,
                read_only: true,
            }),
            ..Default::default()
        };
        assert!(validate_descriptor(&b, 99, &["cpu_shared_memory"]).is_ok());
        assert!(validate_descriptor(&b, 100, &["cpu_shared_memory"]).is_err());
        assert!(validate_descriptor(&b, 99, &["cuda_ipc"]).is_err());
        b.lease.as_mut().unwrap().read_only = false;
        assert!(validate_descriptor(&b, 99, &["cpu_shared_memory"]).is_err());
    }
}
