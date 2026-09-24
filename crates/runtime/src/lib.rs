use serde::Deserialize;
use std::num::NonZeroUsize;
use tokio::sync::mpsc;

/// 宿主加速器探测（ADR-022）。
///
/// 与 `capability` 分工：`capability::backends()` 回答"**本进程**能不能执行推理"，
/// 本模块回答"**这台宿主**有没有这块加速器"。两张表分开，任何一张都不能替另一张说话。
pub mod accelerator;

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

    /// `limits` 是本进程启动时的常驻分级（ADR-015）。它被**转述**而不是在此执行：
    /// `serve` 不跑 pipeline，没有队列可设上限；模型并发也还没有任何 worker 读。
    /// 真正消费 `media_queue_capacity` 的是 `replay`/`ingest`，它们的报告里带准入结果。
    pub fn describe(limits: &crate::ResidentLimits) -> DescribeCapabilitiesResponse {
        DescribeCapabilitiesResponse {
            platform: platform(),
            host: Some(host_resources()),
            backends: backends(),
            unavailable_capabilities: unavailable_capabilities(),
            admitted_memory_kinds: admitted_memory_kinds(),
            host_accelerators: crate::accelerator::report().to_vec(),
            residency: Some(limits.describe_residency()),
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

/// 常驻分级（ADR-015）注入的执行上限（ADR-019 / ADR-021）。
///
/// 三个变量都必须区分"未注入"与"注入了坏值"：开发机上没有 `resident.env` 是常态，
/// 但 `SENSORYPLEX_MEDIA_QUEUE_CAPACITY=0` 不能被读成"没有上限"。
pub const RESIDENT_TIER_ENV: &str = "SENSORYPLEX_RESIDENT_TIER";
pub const MEDIA_QUEUE_CAPACITY_ENV: &str = "SENSORYPLEX_MEDIA_QUEUE_CAPACITY";
pub const MODEL_PARALLELISM_ENV: &str = "SENSORYPLEX_MODEL_PARALLELISM";

/// 分级注入的上限集合。`None` 一律表示**未注入**，不是"某一档"也不是零。
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ResidentLimits {
    /// `resident.env` 的档位名；只用于报告，不参与判定。
    pub tier: Option<String>,
    /// pipeline `queue_capacity` 与本次运行保留窗口的上限。
    pub media_queue_capacity: Option<NonZeroUsize>,
    /// 模型 worker 的并发预算。运行时只**转述**它：真正的消费方是模型 worker
    /// （`tools/ai_worker.py`，按 `--model-parallelism` / 本变量与 `residency` 做准入并在飞限流，
    /// 见 ADR-021）。因此这里报的是"进程读到了什么"，不是"并发已在本进程内限流"。
    pub model_parallelism: Option<NonZeroUsize>,
}

/// 一个上限变量：缺失是事实，空串与非法值都是坏配置。
fn parse_resident_limit(name: &str, raw: Option<&str>) -> Result<Option<NonZeroUsize>, String> {
    let Some(raw) = raw else {
        return Ok(None);
    };
    let value = raw.trim();
    if value.is_empty() {
        return Err(format!("invalid_resident_limit: {name} is set but empty"));
    }
    value
        .parse::<usize>()
        .ok()
        .and_then(NonZeroUsize::new)
        .map(Some)
        .ok_or_else(|| format!("invalid_resident_limit: {name}={value}"))
}

impl ResidentLimits {
    /// 从三个显式取值构造。把取值与解析分开，测试才能在不改宿主环境的情况下覆盖全部分支。
    pub fn parse(
        tier: Option<&str>,
        media_queue_capacity: Option<&str>,
        model_parallelism: Option<&str>,
    ) -> Result<Self, String> {
        let tier = match tier.map(str::trim) {
            None => None,
            Some("") => {
                return Err(format!(
                    "invalid_resident_limit: {RESIDENT_TIER_ENV} is set but empty"
                ))
            }
            Some(value) => Some(value.to_string()),
        };
        Ok(Self {
            tier,
            media_queue_capacity: parse_resident_limit(
                MEDIA_QUEUE_CAPACITY_ENV,
                media_queue_capacity,
            )?,
            model_parallelism: parse_resident_limit(MODEL_PARALLELISM_ENV, model_parallelism)?,
        })
    }

    /// 非 UTF-8 的环境变量也是坏配置，不能静默当成"未注入"。
    pub fn from_env() -> Result<Self, String> {
        let read = |name: &str| match std::env::var(name) {
            Ok(value) => Ok(Some(value)),
            Err(std::env::VarError::NotPresent) => Ok(None),
            Err(std::env::VarError::NotUnicode(_)) => {
                Err(format!("invalid_resident_limit: {name} is not valid UTF-8"))
            }
        };
        let tier = read(RESIDENT_TIER_ENV)?;
        let media_queue_capacity = read(MEDIA_QUEUE_CAPACITY_ENV)?;
        let model_parallelism = read(MODEL_PARALLELISM_ENV)?;
        Self::parse(
            tier.as_deref(),
            media_queue_capacity.as_deref(),
            model_parallelism.as_deref(),
        )
    }

    /// 契约类型里的分级字段：本进程只**转述**这些值。`serve` 不跑 pipeline、也不跑模型，
    /// 因此这里既不能声称队列上限已生效，也不能声称模型并发已被**本进程**限流
    /// （模型并发的消费方是 worker 进程，见 ADR-021）。
    pub fn describe_residency(&self) -> sensoryplex_sdk::runtime::ResidencyLimits {
        sensoryplex_sdk::runtime::ResidencyLimits {
            tier: self.tier.clone().unwrap_or_default(),
            media_queue_capacity: self
                .media_queue_capacity
                .map_or(0, |value| value.get() as u64),
            model_parallelism: self.model_parallelism.map_or(0, |value| value.get() as u64),
        }
    }
}

/// 一次媒体运行对分级上限的准入结果。
///
/// 只有通过准入的运行才拿得到这个值，因此报告里只会出现 `not_injected` 与 `admitted`：
/// 越界的运行在写报告之前就以显式错误退出。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MediaQueueAdmission {
    pub tier: Option<String>,
    pub declared_capacity: NonZeroUsize,
    pub tier_capacity: Option<NonZeroUsize>,
    pub retained_limit: usize,
}

impl MediaQueueAdmission {
    pub const STATE_NOT_INJECTED: &'static str = "not_injected";
    pub const STATE_ADMITTED: &'static str = "admitted";

    /// 分级是**上限**，不是默认值：越界即拒绝，绝不把声明值改写成上限值。改写会让
    /// 配置文件与实际执行对不上，也会让 `probe` 的"只报告、不改写"语义失效。
    ///
    /// 保留窗口（本次运行真正使用的队列深度）同样受上限约束：16GB 机型上照用内置默认值
    /// 32 条就已经超出 `small` 档的 16，必须显式失败而不是"照跑"。
    pub fn new(
        limits: &ResidentLimits,
        declared_capacity: NonZeroUsize,
        retained_limit: usize,
    ) -> Result<Self, String> {
        if let Some(capacity) = limits.media_queue_capacity {
            let tier = limits.tier.as_deref().unwrap_or("unknown");
            if declared_capacity.get() > capacity.get() {
                return Err(format!(
                    "queue_capacity_exceeds_tier_cap: declared={declared_capacity} tier_capacity={capacity} tier={tier}"
                ));
            }
            if retained_limit > capacity.get() {
                return Err(format!(
                    "retained_limit_exceeds_tier_cap: retained_limit={retained_limit} tier_capacity={capacity} tier={tier}"
                ));
            }
        }
        Ok(Self {
            tier: limits.tier.clone(),
            declared_capacity,
            tier_capacity: limits.media_queue_capacity,
            retained_limit,
        })
    }

    pub fn state(&self) -> &'static str {
        match self.tier_capacity {
            Some(_) => Self::STATE_ADMITTED,
            None => Self::STATE_NOT_INJECTED,
        }
    }

    /// 运行日志里的一行：把声明值、分级上限、档位与实际保留窗口放在一起，
    /// 读的人不必在三个来源之间自己对账。
    pub fn describe(&self) -> String {
        format!(
            "queue_capacity state={} declared={} tier_capacity={} tier={} retained_limit={}",
            self.state(),
            self.declared_capacity,
            self.tier_capacity
                .map_or("not_injected".to_string(), |value| value.to_string()),
            self.tier
                .clone()
                .unwrap_or_else(|| "not_injected".to_string()),
            self.retained_limit
        )
    }
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

    #[test]
    fn resident_limits_split_absent_from_malformed() {
        // 没有任何变量 = 未注入：开发机上的常态，不是"某一档"。
        let absent = ResidentLimits::parse(None, None, None).expect("absent is legal");
        assert_eq!(absent, ResidentLimits::default());
        assert!(ResidentLimits::parse(Some("large"), None, None)
            .expect("tier alone is fine")
            .media_queue_capacity
            .is_none());

        let values = ResidentLimits::parse(Some(" large "), Some(" 64 "), Some("3"))
            .expect("values are trimmed");
        assert_eq!(values.tier.as_deref(), Some("large"));
        assert_eq!(values.media_queue_capacity.map(NonZeroUsize::get), Some(64));
        assert_eq!(values.model_parallelism.map(NonZeroUsize::get), Some(3));

        // 空串与非法值都是坏配置：不能读成"未注入"，更不能读成"没有上限"。
        for (tier, queue, parallelism) in [
            (Some(""), Some("64"), Some("3")),
            (Some("large"), Some(""), Some("3")),
            (Some("large"), Some("0"), Some("3")),
            (Some("large"), Some("16GiB"), Some("3")),
            (Some("large"), Some("64"), Some("0")),
        ] {
            let error = ResidentLimits::parse(tier, queue, parallelism)
                .expect_err("malformed limits must fail");
            assert!(error.starts_with("invalid_resident_limit: "), "{error}");
        }
        assert!(ResidentLimits::parse(Some("large"), Some("0"), Some("3"))
            .expect_err("zero is not a limit")
            .contains(MEDIA_QUEUE_CAPACITY_ENV));
    }

    #[test]
    fn tier_cap_admits_or_rejects_but_never_clips() {
        let declared = NonZeroUsize::new(32).unwrap();
        let small = ResidentLimits::parse(Some("small"), Some("16"), Some("1")).expect("legal");

        // 声明值超上限：显式拒绝，且原因里同时给出声明值与上限，便于定位是哪台机器的哪一档。
        let error =
            MediaQueueAdmission::new(&small, declared, 16).expect_err("declared exceeds cap");
        assert!(
            error.starts_with("queue_capacity_exceeds_tier_cap: "),
            "{error}"
        );
        assert!(error.contains("declared=32") && error.contains("tier_capacity=16"));

        // 保留窗口超上限：这是"没按分级跑"的另一半，不能只看 pipeline 声明值。
        let error = MediaQueueAdmission::new(&small, NonZeroUsize::new(16).unwrap(), 32)
            .expect_err("retained window exceeds cap");
        assert!(
            error.starts_with("retained_limit_exceeds_tier_cap: "),
            "{error}"
        );

        // 声明值正好等于上限：允许运行，且声明值原样保留（允许运行 ≠ 允许改写配置）。
        let medium = ResidentLimits::parse(Some("medium"), Some("32"), Some("2")).expect("legal");
        let admitted =
            MediaQueueAdmission::new(&medium, declared, 16).expect("32 fits the medium tier cap");
        assert_eq!(admitted.state(), MediaQueueAdmission::STATE_ADMITTED);
        assert_eq!(admitted.declared_capacity.get(), 32);
        assert!(admitted.describe().contains("declared=32"));
        assert!(admitted.describe().contains("tier=medium"));
        assert!(admitted.describe().contains("retained_limit=16"));

        // 同一条 pipeline 在 `small` 档下就必须被拒：分级确实改变了执行结果。
        assert!(MediaQueueAdmission::new(&small, declared, 16).is_err());

        // 没有分级时不判定、也不报"通过"：状态必须是 not_injected。
        let absent = ResidentLimits::default();
        let ungraded = MediaQueueAdmission::new(&absent, declared, 4_096).expect("no cap, no gate");
        assert_eq!(ungraded.state(), MediaQueueAdmission::STATE_NOT_INJECTED);
        assert!(ungraded.describe().contains("tier_capacity=not_injected"));
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
        let described = describe(&crate::ResidentLimits::default());
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
        // 没有分级时必须显式报零值加空档位名，而不是省略该字段或填一个"看起来合理"的档位。
        let residency = described.residency.expect("residency is always reported");
        assert!(residency.tier.is_empty());
        assert_eq!(residency.media_queue_capacity, 0);
        assert_eq!(residency.model_parallelism, 0);
    }

    #[test]
    fn residency_reports_injected_limits_verbatim() {
        let limits = crate::ResidentLimits::parse(Some("small"), Some("16"), Some("1"))
            .expect("legal tier values");
        let residency = describe(&limits).residency.expect("residency reported");
        assert_eq!(residency.tier, "small");
        assert_eq!(residency.media_queue_capacity, 16);
        assert_eq!(residency.model_parallelism, 1);
    }

    #[test]
    fn apple_silicon_lists_accelerators_expected_on_that_target() {
        let names = backend_names();
        if platform() == "macos-aarch64" {
            assert!(names.contains(&"coreml".to_string()));
            assert!(names.contains(&"metal".to_string()));
        }
    }

    /// 宿主加速器表必须与"本进程能不能执行"分开报告，且三态不得互相塌陷（ADR-022）。
    #[test]
    fn host_accelerators_never_collapse_probe_failure_into_absence() {
        use sensoryplex_sdk::runtime::AcceleratorState;

        let described = describe(&crate::ResidentLimits::default());
        let facts = described.host_accelerators;
        assert_eq!(
            facts.len(),
            crate::accelerator::expected().len(),
            "报告条数必须等于本目标的预期加速器清单，不能凭空增减"
        );
        let mut seen = Vec::new();
        for fact in &facts {
            assert!(
                crate::accelerator::expected().contains(&fact.accelerator.as_str()),
                "{} 不在本目标的预期清单里",
                fact.accelerator
            );
            assert!(
                !seen.contains(&fact.accelerator),
                "{} 重复上报",
                fact.accelerator
            );
            seen.push(fact.accelerator.clone());
            assert_eq!(fact.platform, platform());
            let state = AcceleratorState::try_from(fact.state).expect("state must be known");
            assert_ne!(
                state,
                AcceleratorState::Unspecified,
                "{} 必须给出三种结局之一，不能留 unspecified",
                fact.accelerator
            );
            assert!(!fact.detection_source.is_empty());
            // 只有"宿主确实有"才允许去掉原因；其余两态都必须解释自己。
            assert_eq!(
                fact.unavailable_reason.is_empty(),
                state == AcceleratorState::Available,
                "{} 在 {state:?} 下的原因串与状态不匹配",
                fact.accelerator
            );
            // 证据里不允许出现文件系统路径或整段工具输出。
            for item in &fact.evidence {
                assert!(!item.contains('/'), "evidence 不得携带路径: {item}");
                assert!(item.len() <= 128, "evidence 过长: {item}");
            }
        }
        if platform() == "macos-aarch64" {
            // `AVAILABLE` 必须带真读到的版本；这条是实现的硬不变量。
            // 反过来的"本目标不许出现 unknown"不在这里断言：无头 macOS 虚拟机可能拿不到
            // 显示报告，那是环境的真结论，不是回归——对账交给 tools/verify_accelerator_report.py
            // 用独立探测出的宿主事实去比。
            for fact in &facts {
                let state = AcceleratorState::try_from(fact.state).expect("known state");
                if state == AcceleratorState::Available {
                    assert!(
                        !fact.runtime_version.is_empty(),
                        "{} 报 available 就必须带上真读到的版本",
                        fact.accelerator
                    );
                }
            }
        }
    }
}
