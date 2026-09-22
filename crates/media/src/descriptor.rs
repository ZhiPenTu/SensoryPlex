//! 数据面 descriptor 交接，用于解码缓冲。
//!
//! 解码后的字节只在 arena 中存在。本模块对外只暴露不透明句柄、offset/length 对、
//! 内容摘要以及运行时签发的只读 lease —— 与生产环境交给插件的契约一致。

use sensoryplex_sdk::common::{
    BufferDescriptor, BufferFormat, BufferLease, BufferLocator, TimeRange,
};
use sensoryplex_sdk::{validate_range, ContractError};
use sha2::{Digest, Sha256};

use crate::arena::Arena;
use crate::lease::LeaseRegistry;
use crate::{validate_descriptor, MediaError};

/// 解码缓冲目前在 CPU 侧；只有通过 `DescribeCapabilities` 显式声明 `unified_memory`
/// 的平台，才能启用对应的 descriptor 交接。
pub const ADMITTED_MEMORY_KINDS: [&str; 1] = ["cpu_shared_memory"];
/// 超出交接窗口的 lease 不再有任何意义，因此默认 TTL 较短。
pub const DEFAULT_LEASE_TTL_MS: u32 = 5_000;

pub fn content_hash(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

/// 构造 descriptor 所需、但无法从字节本身派生出来的全部信息。
pub struct BufferSpec<'a> {
    pub buffer_id: String,
    pub kind: &'a str,
    pub stream_id: &'a str,
    pub time_range: TimeRange,
    pub format: BufferFormat,
}

/// 单次 replay 的交接计数器。失败会被记录并暴露，绝不静默吞掉。
#[derive(Debug, Default)]
pub struct HandoffCounters {
    pub built: u64,
    pub validated: u64,
    pub failures: u64,
    pub leases_issued: u64,
    pub leases_released: u64,
    failure_reasons: Vec<String>,
}

impl HandoffCounters {
    pub fn failure_reasons(&self) -> Vec<String> {
        let mut reasons = self.failure_reasons.clone();
        reasons.sort();
        reasons.dedup();
        reasons
    }

    fn fail(&mut self, reason: String) -> MediaError {
        self.failures += 1;
        self.failure_reasons.push(reason.clone());
        MediaError::DescriptorRejected(reason)
    }
}

/// 将 `bytes` 拷贝进 arena，签发真实 lease，按准入契约校验生成的 descriptor，
/// 然后释放 lease 与 slab。
///
/// 返回的是已释放的 descriptor 作为证据，字节本身不会返回。无法完成交接的调用方
/// 收到错误，而不是拿到一个乐观的 descriptor。
pub fn hand_off(
    arena: &mut Arena,
    leases: &mut LeaseRegistry,
    spec: BufferSpec<'_>,
    bytes: &[u8],
    now_ms: i64,
    counters: &mut HandoffCounters,
) -> Result<BufferDescriptor, MediaError> {
    if bytes.is_empty() {
        return Err(counters.fail("empty_buffer_payload".into()));
    }
    if let Err(error) = validate_range(&spec.time_range) {
        return Err(counters.fail(contract_code(error)));
    }
    if spec.time_range.end_ms - spec.time_range.start_ms > 60_000 {
        return Err(counters.fail("buffer_interval_longer_than_a_minute".into()));
    }
    let offset = arena.allocate(bytes.len())?;
    arena.write(offset, bytes)?;
    counters.built += 1;
    let release_slab = |arena: &mut Arena| {
        arena.release(offset);
    };

    let lease = match leases.issue(now_ms, DEFAULT_LEASE_TTL_MS) {
        Ok(lease) => lease,
        Err(error) => {
            release_slab(arena);
            return Err(counters.fail(contract_code(error)));
        }
    };
    counters.leases_issued += 1;

    let descriptor = BufferDescriptor {
        buffer_id: spec.buffer_id,
        kind: spec.kind.to_string(),
        memory_kind: ADMITTED_MEMORY_KINDS[0].to_string(),
        locator: Some(BufferLocator {
            handle: arena.id().to_string(),
            offset: offset as u64,
            length: bytes.len() as u64,
        }),
        format: Some(spec.format),
        stream_id: spec.stream_id.to_string(),
        time_range: Some(spec.time_range),
        lease: Some(lease.clone()),
        content_hash: content_hash(bytes),
    };

    if let Err(error) = leases.validate(&lease, now_ms) {
        release_slab(arena);
        return Err(counters.fail(contract_code(error)));
    }
    if let Err(error) = validate_descriptor(&descriptor, now_ms, &ADMITTED_MEMORY_KINDS) {
        release_slab(arena);
        return Err(counters.fail(contract_code(error)));
    }
    // 句柄必须仍能解析到摘要对应的同一份 payload。指向其他位置的 descriptor
    // 是缺陷，不是警告。
    if let Err(error) = verify_payload(arena, &descriptor, bytes) {
        release_slab(arena);
        return Err(counters.fail(error.to_string()));
    }
    counters.validated += 1;

    if leases.release(&lease.lease_id) {
        counters.leases_released += 1;
    }
    release_slab(arena);
    Ok(descriptor)
}

/// 把 descriptor 的句柄解析回产生其摘要的那一份 payload。
///
/// 把 arena 内容与 `expected` 比较等价于重新做哈希 —— 摘要本来就是从 `expected` 算出的， - at memcmp cost.
pub fn verify_payload<'a>(
    arena: &'a Arena,
    descriptor: &BufferDescriptor,
    expected: &[u8],
) -> Result<&'a [u8], MediaError> {
    let locator = descriptor
        .locator
        .as_ref()
        .ok_or(MediaError::IoFailed("missing_locator".into()))?;
    if locator.handle != arena.id() {
        return Err(MediaError::IoFailed("locator_handle_mismatch".into()));
    }
    let bytes = arena.read(locator.offset as usize, locator.length as usize)?;
    if bytes != expected {
        return Err(MediaError::IoFailed("payload_mismatch".into()));
    }
    Ok(bytes)
}

fn contract_code(error: ContractError) -> String {
    error.0.to_string()
}

/// Lease 仅在有人正在读取时才有意义；这是消费端在成功、取消、出错时都必须执行的释放路径。
pub fn release_lease(leases: &mut LeaseRegistry, lease: &BufferLease) -> bool {
    leases.release(&lease.lease_id)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::arena::DEFAULT_ARENA_CAPACITY_BYTES;

    fn spec<'a>(kind: &'a str, start_ms: i64, end_ms: i64) -> BufferSpec<'a> {
        BufferSpec {
            buffer_id: format!("buf-{kind}-{start_ms}"),
            kind,
            stream_id: "stream-0123456789ab",
            time_range: TimeRange { start_ms, end_ms },
            format: BufferFormat {
                pixel_format: "RGBA".into(),
                width: 2,
                height: 1,
                ..Default::default()
            },
        }
    }

    #[test]
    fn hands_off_a_read_only_descriptor_and_releases_the_slab() {
        let mut arena = Arena::new("arena-test", DEFAULT_ARENA_CAPACITY_BYTES).unwrap();
        let mut leases = LeaseRegistry::default();
        let mut counters = HandoffCounters::default();
        let bytes = [9u8; 8];
        let descriptor = hand_off(
            &mut arena,
            &mut leases,
            spec("video_frame", 0, 33),
            &bytes,
            1_000,
            &mut counters,
        )
        .unwrap();
        assert_eq!(descriptor.kind, "video_frame");
        assert_eq!(descriptor.memory_kind, "cpu_shared_memory");
        assert!(descriptor.lease.as_ref().unwrap().read_only);
        assert_eq!(descriptor.content_hash, content_hash(&bytes));
        assert_eq!(counters.validated, 1);
        assert_eq!(counters.leases_issued, 1);
        assert_eq!(counters.leases_released, 1);
        assert_eq!(counters.failures, 0);
        assert_eq!(leases.outstanding(), 0, "the handoff must not leak a lease");
        assert_eq!(
            arena.used_bytes(),
            8,
            "allocated bytes stay reserved for reuse"
        );
        assert_eq!(
            arena.live_slabs(),
            0,
            "the slab is released after the handoff"
        );
        assert!(
            verify_payload(&arena, &descriptor, &bytes).is_err(),
            "a released slab resolves to nothing"
        );
    }

    #[test]
    fn refuses_payloads_the_contract_cannot_describe() {
        let mut arena = Arena::new("arena-test", DEFAULT_ARENA_CAPACITY_BYTES).unwrap();
        let mut leases = LeaseRegistry::default();
        let mut counters = HandoffCounters::default();
        for (range, reason) in [
            ((5, 5), "invalid_half_open_time_range"),
            ((0, 60_001), "buffer_interval_longer_than_a_minute"),
        ] {
            let error = hand_off(
                &mut arena,
                &mut leases,
                spec("video_frame", range.0, range.1),
                &[1u8; 4],
                0,
                &mut counters,
            )
            .unwrap_err();
            assert!(error.to_string().contains(reason), "{error}");
        }
        assert!(hand_off(
            &mut arena,
            &mut leases,
            spec("video_frame", 0, 10),
            &[],
            0,
            &mut counters
        )
        .is_err());
        assert_eq!(counters.failures, 3);
        assert_eq!(counters.validated, 0);
        assert_eq!(counters.failure_reasons().len(), 3);
        assert_eq!(leases.outstanding(), 0);
    }

    #[test]
    fn arena_pressure_is_reported_instead_of_growing() {
        let mut arena = Arena::new("arena-test", 8).unwrap();
        let mut leases = LeaseRegistry::default();
        let mut counters = HandoffCounters::default();
        hand_off(
            &mut arena,
            &mut leases,
            spec("video_frame", 0, 10),
            &[0u8; 8],
            0,
            &mut counters,
        )
        .unwrap();
        // 已释放的 slab 会被复用，因此同样大小仍能放下；更大的 payload 则放不下。
        hand_off(
            &mut arena,
            &mut leases,
            spec("video_frame", 10, 20),
            &[0u8; 8],
            0,
            &mut counters,
        )
        .unwrap();
        assert!(matches!(
            hand_off(
                &mut arena,
                &mut leases,
                spec("video_frame", 20, 30),
                &[0u8; 16],
                0,
                &mut counters
            ),
            Err(MediaError::ArenaCapacityExceeded { .. })
        ));
    }
}
