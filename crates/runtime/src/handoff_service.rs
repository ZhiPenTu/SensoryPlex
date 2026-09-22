//! `BufferHandoffService`：把数据面契约暴露成 gRPC，供**独立进程**的消费者领取 lease。
//!
//! 边界：
//! - 消息里只有不透明句柄、offset/length 与摘要，没有字节、没有路径、没有密钥；
//! - 段名只对本机、同 UID 的进程可打开，lease 是唯一的读取凭证；
//! - 所有拒绝都在应答里给出 `reason_code`，调用方不必读日志猜原因；
//! - 这里不做"兜底"：TTL 越界、窗口越界、迟到释放一律失败。

use std::sync::{Arc, Mutex};

use sensoryplex_media::handoff::{BufferHandoff, HandoffStats, RetainedBuffer};
use sensoryplex_sdk::common::{ErrorCode, ProcessingError};
use sensoryplex_sdk::media as contract;
use sensoryplex_sdk::media::buffer_handoff_service_server::{
    BufferHandoffService, BufferHandoffServiceServer,
};
use tonic::{Request, Response, Status};

pub use sensoryplex_sdk::media::buffer_handoff_service_server::BufferHandoffServiceServer as Server;

/// 消费者未指定 TTL 时使用的服务端默认值，必须落在 `BufferHandoff` 接受的范围里。
pub const DEFAULT_HANDOFF_TTL_MS: u32 = 5_000;

pub struct HandoffService {
    plane: Arc<Mutex<BufferHandoff>>,
    default_ttl_ms: u32,
    /// 每次 RPC 都会刷新；空闲超过阈值即认为消费者已经离开。
    last_activity_ms: Arc<std::sync::atomic::AtomicI64>,
    /// 是否已经有过消费者。没等到消费者和"消费者走了"是两种不同的情况：
    /// 前者说明数据面没人用，账面对不上，必须显式失败。
    connected: Arc<std::sync::atomic::AtomicBool>,
}

impl HandoffService {
    pub fn new(plane: Arc<Mutex<BufferHandoff>>, default_ttl_ms: u32) -> Self {
        Self {
            plane,
            default_ttl_ms,
            last_activity_ms: Arc::new(std::sync::atomic::AtomicI64::new(
                sensoryplex_media::now_unix_ms(),
            )),
            connected: Arc::new(std::sync::atomic::AtomicBool::new(false)),
        }
    }

    pub fn last_activity_ms(&self) -> Arc<std::sync::atomic::AtomicI64> {
        Arc::clone(&self.last_activity_ms)
    }

    pub fn connected(&self) -> Arc<std::sync::atomic::AtomicBool> {
        Arc::clone(&self.connected)
    }

    pub fn server(self) -> Server<Self> {
        BufferHandoffServiceServer::new(self)
    }

    /// 取数据面并把这次调用记为"消费者还活着"。锁中毒是内部错误，必须显式失败。
    fn plane(&self) -> Result<std::sync::MutexGuard<'_, BufferHandoff>, &'static str> {
        self.last_activity_ms.store(
            sensoryplex_media::now_unix_ms(),
            std::sync::atomic::Ordering::Relaxed,
        );
        self.connected
            .store(true, std::sync::atomic::Ordering::Relaxed);
        self.plane.lock().map_err(|_| "handoff_plane_poisoned")
    }
}

fn processing_error(error: &sensoryplex_media::MediaError) -> ProcessingError {
    let reason = reason_code(error);
    ProcessingError {
        code: error_code(&reason) as i32,
        reason_code: reason,
        retryable: false,
        retry_after_ms: 0,
    }
}

/// 失败原因码的权威形式来自 media crate；这里只做分类，绝不改写原因。
pub fn reason_code(error: &sensoryplex_media::MediaError) -> String {
    use sensoryplex_media::MediaError;
    match error {
        MediaError::DescriptorRejected(reason) => reason.clone(),
        MediaError::ArenaCapacityExceeded { .. } => "arena_capacity_exceeded".to_string(),
        MediaError::IoFailed(reason) => reason.clone(),
        other => other.to_string(),
    }
}

/// 原因 → 契约错误码。列表覆盖 media crate 在当前调用路径上可能产生的全部原因；
/// 未列出的原因归为 INTERNAL，而不是悄悄变成某个"看起来合理"的码。
fn error_code(reason: &str) -> ErrorCode {
    match reason {
        "invalid_lease_ttl"
        | "unknown_buffer"
        | "mapping_out_of_range"
        | "ambiguous_window"
        | "empty_buffer_payload"
        | "missing_buffer_identity"
        | "invalid_half_open_time_range"
        | "buffer_interval_longer_than_a_minute"
        | "duplicate_buffer_id"
        | "invalid_buffer_locator"
        | "invalid_sha256_digest"
        | "missing_locator"
        | "missing_time_range"
        | "missing_lease" => ErrorCode::InvalidInput,
        "buffer_already_leased"
        | "lease_capacity_exhausted"
        | "handoff_backlog_full"
        | "arena_capacity_exceeded" => ErrorCode::ResourceExhausted,
        // lease 消失只有两种解释：已被释放，或 TTL 已过。两种都属于"截止时间已过"。
        "unknown_or_released_lease" | "invalid_or_expired_lease" => ErrorCode::DeadlineExceeded,
        _ => ErrorCode::InternalPluginError,
    }
}

fn retained_to_contract(entry: &RetainedBuffer) -> contract::RetainedBuffer {
    contract::RetainedBuffer {
        buffer_id: entry.buffer_id.clone(),
        kind: entry.kind.clone(),
        stream_id: entry.stream_id.clone(),
        time_range: Some(entry.time_range),
        format: Some(entry.format.clone()),
        offset_bytes: entry.offset,
        length_bytes: entry.length,
        content_hash: entry.content_hash.clone(),
        lease_id: entry.lease_id.clone().unwrap_or_default(),
    }
}

fn stats_to_contract(stats: &HandoffStats, arena_capacity_bytes: u64) -> contract::HandoffStats {
    contract::HandoffStats {
        retained_limit: stats.retained_limit,
        retained: stats.retained,
        leased: stats.leased,
        retained_total: stats.retained_total,
        released_total: stats.released_total,
        expired_total: stats.expired_total,
        retain_rejections: stats.retain_rejections,
        request_rejections: stats.request_rejections,
        rejection_reasons: stats.rejection_reasons.clone().into_iter().collect(),
        arena_capacity_bytes,
        arena_used_bytes: stats.arena_used_bytes,
        arena_peak_bytes: stats.arena_peak_bytes,
        arena_live_slabs: stats.arena_live_slabs,
        offered_total: stats.offered_total,
    }
}

#[tonic::async_trait]
impl BufferHandoffService for HandoffService {
    async fn list(
        &self,
        _: Request<contract::ListRetainedRequest>,
    ) -> Result<Response<contract::ListRetainedResponse>, Status> {
        let mut plane = self.plane().map_err(Status::internal)?;
        plane.expire(sensoryplex_media::now_unix_ms());
        let stats = plane.stats();
        let capacity = plane.arena_capacity_bytes();
        let buffers = plane
            .list()
            .iter()
            .map(retained_to_contract)
            .collect::<Vec<_>>();
        Ok(Response::new(contract::ListRetainedResponse {
            buffers,
            stats: Some(stats_to_contract(&stats, capacity)),
            segment_name: plane.segment_name().unwrap_or_default().to_string(),
        }))
    }

    async fn stats(
        &self,
        _: Request<contract::HandoffStatsRequest>,
    ) -> Result<Response<contract::HandoffStatsResponse>, Status> {
        let mut plane = self.plane().map_err(Status::internal)?;
        plane.expire(sensoryplex_media::now_unix_ms());
        let stats = plane.stats();
        let capacity = plane.arena_capacity_bytes();
        Ok(Response::new(contract::HandoffStatsResponse {
            stats: Some(stats_to_contract(&stats, capacity)),
            segment_name: plane.segment_name().unwrap_or_default().to_string(),
        }))
    }

    async fn acquire(
        &self,
        request: Request<contract::AcquireBufferRequest>,
    ) -> Result<Response<contract::AcquireBufferResponse>, Status> {
        let request = request.into_inner();
        let mut plane = self.plane().map_err(Status::internal)?;
        let now_ms = sensoryplex_media::now_unix_ms();
        let window = match (request.offset_bytes, request.length_bytes) {
            (0, 0) => None,
            (offset, 0) => {
                // 只给 offset 不给长度是歧义请求：不猜调用方的意思，直接拒绝。
                let _ = offset;
                return Ok(Response::new(contract::AcquireBufferResponse {
                    granted: false,
                    buffer: None,
                    segment_name: String::new(),
                    arena_capacity_bytes: 0,
                    error: Some(ProcessingError {
                        code: ErrorCode::InvalidInput as i32,
                        reason_code: "ambiguous_window".to_string(),
                        retryable: false,
                        retry_after_ms: 0,
                    }),
                }));
            }
            (offset, length) => Some((offset, length)),
        };
        let ttl_ms = match request.ttl_ms {
            0 => self.default_ttl_ms,
            explicit => explicit,
        };
        match plane.acquire(&request.buffer_id, window, ttl_ms, now_ms) {
            Ok(leased) => Ok(Response::new(contract::AcquireBufferResponse {
                granted: true,
                buffer: Some(leased.descriptor),
                segment_name: leased.segment_name.clone().unwrap_or_default(),
                arena_capacity_bytes: leased.arena_capacity_bytes,
                error: None,
            })),
            Err(error) => Ok(Response::new(contract::AcquireBufferResponse {
                granted: false,
                buffer: None,
                segment_name: String::new(),
                arena_capacity_bytes: 0,
                error: Some(processing_error(&error)),
            })),
        }
    }

    async fn release(
        &self,
        request: Request<contract::ReleaseBufferRequest>,
    ) -> Result<Response<contract::ReleaseBufferResponse>, Status> {
        let lease_id = request.into_inner().lease_id;
        let mut plane = self.plane().map_err(Status::internal)?;
        // 先结算过期：迟到的释放必须失败，而且失败原因要和"已经释放过"区分开。
        plane.expire(sensoryplex_media::now_unix_ms());
        match plane.release(&lease_id, sensoryplex_media::now_unix_ms()) {
            Ok(buffer_id) => Ok(Response::new(contract::ReleaseBufferResponse {
                released: true,
                buffer_id,
                error: None,
            })),
            Err(error) => Ok(Response::new(contract::ReleaseBufferResponse {
                released: false,
                buffer_id: String::new(),
                error: Some(processing_error(&error)),
            })),
        }
    }
}
