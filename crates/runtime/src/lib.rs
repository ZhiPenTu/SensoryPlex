use serde::Deserialize;
use std::num::NonZeroUsize;
use tokio::sync::mpsc;

/// 平台、宿主资源清单与后端能力上报。
///
/// 尚未实现的能力会按稳定顺序列在"不可用"中，并附上原因；调用方不会把
/// "尚未接入"误判为"正常运行但结果为零"。
pub mod capability {
    use sensoryplex_sdk::runtime::{
        BackendCapability, CapabilityState, DescribeCapabilitiesResponse, HostResources,
    };

    /// 本次构建尚未提供的能力，按稳定顺序排列。
    const PENDING_CAPABILITIES: [&str; 4] = [
        "media_ingestion",
        "model_inference",
        "event_dispatch",
        "semantic_index",
    ];

    /// 尚不存在任何 `ExecutionBackend` 实现，包括 CPU 基线。
    const BACKEND_UNAVAILABLE_REASON: &str = "execution_backend_not_implemented";

    pub fn platform() -> String {
        format!("{}-{}", std::env::consts::OS, std::env::consts::ARCH)
    }

    pub fn state() -> &'static str {
        if PENDING_CAPABILITIES.is_empty() {
            "ready"
        } else {
            "degraded"
        }
    }

    pub fn unavailable_capabilities() -> Vec<String> {
        PENDING_CAPABILITIES
            .iter()
            .map(|name| (*name).to_string())
            .collect()
    }

    /// 本构建目标预期存在的加速器；缺失是事实，不视为回退。
    pub fn backend_names() -> Vec<String> {
        let mut names = vec!["cpu".to_string()];
        let accelerated: &[&str] = match platform().as_str() {
            "macos-aarch64" => &["coreml", "metal"],
            "linux-x86_64" => &["cuda_tensorrt", "onnxruntime_cuda"],
            "linux-aarch64" => &["onnxruntime_cpu"],
            _ => &[],
        };
        names.extend(accelerated.iter().map(|name| (*name).to_string()));
        names
    }

    pub fn backends() -> Vec<BackendCapability> {
        let platform = platform();
        backend_names()
            .into_iter()
            .map(|name| BackendCapability {
                backend: name,
                platform: platform.clone(),
                runtime_version: String::new(),
                precisions: Vec::new(),
                memory_kinds: Vec::new(),
                max_concurrency: 0,
                state: CapabilityState::Unavailable as i32,
                unavailable_reason: BACKEND_UNAVAILABLE_REASON.to_string(),
            })
            .collect()
    }

    /// 运行时在此处允许使用的内存种类。Apple Silicon 与其加速器共享同一块内存池，
    /// 因此 `unified_memory` 仅在该目标上允许 zero-copy descriptor。
    pub fn admitted_memory_kinds() -> Vec<String> {
        let mut kinds = vec!["cpu_shared_memory".to_string()];
        if has_unified_memory() {
            kinds.push("unified_memory".to_string());
        }
        kinds
    }

    pub fn has_unified_memory() -> bool {
        cfg!(all(target_os = "macos", target_arch = "aarch64"))
    }

    pub fn host_resources() -> HostResources {
        let total_memory_bytes = reported_total_memory_bytes();
        HostResources {
            unified_memory_bytes: if has_unified_memory() {
                total_memory_bytes
            } else {
                0
            },
            total_memory_bytes,
            logical_cores: std::thread::available_parallelism()
                .map(|cores| cores.get() as u32)
                .unwrap_or(0),
        }
    }

    pub fn describe() -> DescribeCapabilitiesResponse {
        DescribeCapabilitiesResponse {
            platform: platform(),
            host: Some(host_resources()),
            backends: backends(),
            unavailable_capabilities: unavailable_capabilities(),
            admitted_memory_kinds: admitted_memory_kinds(),
        }
    }

    /// `SENSORYPLEX_TOTAL_MEMORY_BYTES` 是宿主无法自动检测总内存（容器、CI）
    /// 时的显式清单覆盖值；它不会用来伪造容量。
    fn reported_total_memory_bytes() -> u64 {
        std::env::var("SENSORYPLEX_TOTAL_MEMORY_BYTES")
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
            .filter(|bytes| *bytes > 0)
            .unwrap_or_else(detect_total_memory_bytes)
    }

    #[cfg(target_os = "macos")]
    fn detect_total_memory_bytes() -> u64 {
        match std::process::Command::new("sysctl")
            .args(["-n", "hw.memsize"])
            .output()
        {
            Ok(output) if output.status.success() => String::from_utf8_lossy(&output.stdout)
                .trim()
                .parse()
                .unwrap_or(0),
            _ => 0,
        }
    }

    #[cfg(target_os = "linux")]
    fn detect_total_memory_bytes() -> u64 {
        std::fs::read_to_string("/proc/meminfo")
            .ok()
            .and_then(|text| {
                text.lines().find_map(|line| {
                    let (key, value) = line.split_once(':')?;
                    if key != "MemTotal" {
                        return None;
                    }
                    value.split_whitespace().next()?.parse::<u64>().ok()
                })
            })
            .map(|kib| kib * 1024)
            .unwrap_or(0)
    }

    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    fn detect_total_memory_bytes() -> u64 {
        0
    }
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Pipeline {
    pub api_version: String,
    pub kind: String,
    pub metadata: Metadata,
    pub spec: PipelineSpec,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Metadata {
    pub name: String,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PipelineSpec {
    pub source: Source,
    pub processors: Vec<Processor>,
    pub slow_enrichment: Vec<Processor>,
    pub sinks: Vec<Processor>,
    pub queue_capacity: NonZeroUsize,
    pub data_egress: String,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Source {
    pub r#type: String,
    pub uri_secret_ref: String,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Processor {
    pub r#type: String,
    pub model: Option<String>,
}

impl Pipeline {
    pub fn parse(yaml: &str) -> Result<Self, String> {
        let p: Self =
            serde_yaml::from_str(yaml).map_err(|_| "invalid_pipeline_schema".to_string())?;
        if p.api_version != "edge.material/v1" || p.kind != "Pipeline" || p.metadata.name.is_empty()
        {
            return Err("unsupported_pipeline_contract".into());
        }
        if !["file", "srt"].contains(&p.spec.source.r#type.as_str())
            || p.spec.source.uri_secret_ref.is_empty()
        {
            return Err("invalid_source".into());
        }
        if p.spec.data_egress != "local_only" {
            return Err("data_policy_denied".into());
        }
        if p.spec.processors.is_empty()
            || p.spec.sinks.is_empty()
            || p.spec.queue_capacity.get() > 65536
        {
            return Err("invalid_pipeline_capacity_or_stages".into());
        }
        if p.spec
            .processors
            .iter()
            .chain(&p.spec.slow_enrichment)
            .chain(&p.spec.sinks)
            .any(|p| p.r#type.is_empty())
        {
            return Err("missing_processor_capability".into());
        }
        Ok(p)
    }
}

/// 准入（admission）从不创建无界队列，也不在慢路径满载时阻塞等待。
pub fn bounded_queue<T>(capacity: NonZeroUsize) -> (mpsc::Sender<T>, mpsc::Receiver<T>) {
    mpsc::channel(capacity.get())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn full_queue_reports_backpressure_without_losing_accepted_work() {
        let (tx, mut rx) = bounded_queue(NonZeroUsize::new(1).unwrap());
        tx.try_send(1).unwrap();
        assert!(matches!(
            tx.try_send(2),
            Err(mpsc::error::TrySendError::Full(2))
        ));
        assert_eq!(rx.recv().await, Some(1));
        tx.try_send(2).unwrap();
        drop(tx);
        assert_eq!(rx.recv().await, Some(2));
        assert_eq!(rx.recv().await, None);
    }
    #[test]
    fn pipeline_rejects_unbounded_queue_and_egress() {
        let example = include_str!("../../../config/pipelines/file-material.yaml");
        assert!(Pipeline::parse(example).is_ok());
        assert!(
            Pipeline::parse(&example.replace("queue_capacity: 32", "queue_capacity: 0")).is_err()
        );
        assert!(Pipeline::parse(&example.replace("local_only", "cloud")).is_err());
    }
}

#[cfg(test)]
mod capability_tests {
    use super::capability::*;
    use sensoryplex_sdk::runtime::CapabilityState;

    #[test]
    fn platform_matches_build_target() {
        assert_eq!(
            platform(),
            format!("{}-{}", std::env::consts::OS, std::env::consts::ARCH)
        );
    }

    #[test]
    fn every_unavailable_backend_explains_itself() {
        let backends = backends();
        assert!(backends.iter().any(|backend| backend.backend == "cpu"));
        for backend in &backends {
            assert_eq!(backend.state, CapabilityState::Unavailable as i32);
            assert!(
                !backend.unavailable_reason.is_empty(),
                "{} must state why it is unavailable",
                backend.backend
            );
            assert!(backend.runtime_version.is_empty());
            assert!(backend.precisions.is_empty());
            assert_eq!(backend.max_concurrency, 0);
        }
    }

    #[test]
    fn describe_reports_platform_host_and_unknown_state() {
        let described = describe();
        assert_eq!(described.platform, platform());
        let host = described.host.expect("host resources are always reported");
        assert!(host.total_memory_bytes > 0);
        assert!(host.unified_memory_bytes <= host.total_memory_bytes);
        assert_eq!(host.unified_memory_bytes > 0, has_unified_memory());
        assert!(!described.unavailable_capabilities.is_empty());
        assert!(described
            .admitted_memory_kinds
            .contains(&"cpu_shared_memory".to_string()));
        assert_eq!(
            described
                .admitted_memory_kinds
                .contains(&"unified_memory".to_string()),
            has_unified_memory()
        );
    }

    #[test]
    fn apple_silicon_lists_accelerators_expected_on_that_target() {
        let names = backend_names();
        if platform() == "macos-aarch64" {
            assert!(names.contains(&"coreml".to_string()));
            assert!(names.contains(&"metal".to_string()));
        }
    }
}
