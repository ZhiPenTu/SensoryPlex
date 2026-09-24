//! 宿主加速器的能力上报（ADR-022）。
//!
//! ADR-012 §"未验证范围"、ADR-016 §8 与 ADR-017 §8 记录的是同一个缺口：`capability::backends()`
//! 把所有后端（含 `coreml` / `metal`）一律记为 `execution_backend_not_implemented`。这对**本进程**
//! 是事实——运行时自己不做推理——但报告里再也读不到"宿主到底有没有这块加速器"，于是很容易被下一个人
//! 读成"CoreML 在这台机器上不可用"。本模块补上另一半：**真正去探测宿主**，并把
//! "宿主有没有"与"本进程能不能执行"分成两张表。
//!
//! 三条硬规则：
//!
//! - **探测不到 ≠ 不存在。** 工具缺失、超时、输出看不懂都落 `unknown` 并带稳定原因，
//!   既不写成 `available` 也不写成 `unavailable`。
//! - **只写真读到的值。** `runtime_version` 与 `evidence` 只能来自探测输出；读不到就留空，
//!   不填"看起来合理"的版本号。
//! - **有界。** 探测子进程有超时上限（超时即 kill 并落 `unknown`），每条只保留少量受控事实；
//!   工具原始输出绝不被整段塞进控制面或日志。

use std::io::Read;
use std::process::{Command, Stdio};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use sensoryplex_sdk::runtime::{AcceleratorState, HostAccelerator};

use crate::capability;

/// 探测子进程的时间上限。探测工具卡住不能拖住 `DescribeCapabilities`。
pub const PROBE_TIMEOUT: Duration = Duration::from_secs(3);

/// CoreML 的系统框架位置。它是宿主事实的查询目标，不会被写进任何 evidence。
const COREML_FRAMEWORK: &str = "/System/Library/Frameworks/CoreML.framework";

/// 探测工具一次调用的结局。三种失败必须彼此可分：工具不在 PATH、超时、工具报错
/// 说明的东西不一样，混成一个"探测失败"就没法判断该不该重试或换探测方式。
#[derive(Debug, PartialEq, Eq)]
pub enum Probe {
    Output(String),
    ToolMissing,
    TimedOut,
    Failed(String),
}

/// 本构建目标**预期**存在的加速器；探测只覆盖这张清单，不凭空造条目。
pub fn expected() -> &'static [&'static str] {
    match capability::platform().as_str() {
        "macos-aarch64" => &["coreml", "metal"],
        "linux-x86_64" => &["cuda"],
        _ => &[],
    }
}

/// `system_profiler -json SPDisplaysDataType` 里被用到的受控事实。
///
/// 只用 JSON 的**字段名**做判定、只把**值**当证据：macOS 会本地化显示文本，但
/// `spdisplays_*` / `sppci_*` 这些键是稳定标识符，因此判定本身与语言环境无关。
/// 值的本地化不影响"这个键存在且非空"这个事实，所以它被原样记成证据。
#[derive(Debug, Default, PartialEq, Eq)]
pub struct DisplaysFacts {
    /// `sppci_model`：GPU 型号。
    pub model: String,
    /// `spdisplays_mtlgpufamilysupport`：Metal 家族令牌。
    pub metal_family: String,
    /// `sppci_cores`：GPU 核心数。
    pub cores: String,
}

/// 解析 `system_profiler` 的显示报告；只认第一个适配器条目。
pub fn parse_displays_json(json: &str) -> Result<DisplaysFacts, String> {
    let value: serde_json::Value =
        serde_json::from_str(json).map_err(|_| "displays_json_unparsable".to_string())?;
    let entry = value
        .get("SPDisplaysDataType")
        .and_then(|section| section.as_array())
        .and_then(|adapters| adapters.first())
        .ok_or_else(|| "displays_report_empty".to_string())?;
    let text = |key: &str| {
        entry
            .get(key)
            .and_then(|value| value.as_str())
            .unwrap_or("")
            .trim()
            .to_string()
    };
    Ok(DisplaysFacts {
        model: text("sppci_model"),
        metal_family: text("spdisplays_mtlgpufamilysupport"),
        cores: text("sppci_cores"),
    })
}

/// Metal：判定与证据都来自 `system_profiler` 的显示报告。
pub fn metal_fact(probe: Probe) -> HostAccelerator {
    let json = match probe {
        Probe::Output(json) => json,
        other => return probe_failed("metal", "system_profiler", other),
    };
    let facts = match parse_displays_json(&json) {
        Ok(facts) => facts,
        Err(reason) => return unknown("metal", &format!("{reason}:system_profiler")),
    };
    let mut evidence = Vec::new();
    if !facts.model.is_empty() {
        evidence.push(format!("sppci_model={}", facts.model));
    }
    if !facts.cores.is_empty() {
        evidence.push(format!("sppci_cores={}", facts.cores));
    }
    if facts.metal_family.is_empty() {
        // 适配器条目在、但没有 Metal 家族令牌：这是"宿主说没有"，不是"探测不到"。
        if evidence.is_empty() {
            return unknown("metal", "displays_entry_without_identity:system_profiler");
        }
        return HostAccelerator {
            accelerator: "metal".into(),
            platform: capability::platform(),
            state: AcceleratorState::Unavailable as i32,
            detection_source: "system_profiler".into(),
            runtime_version: String::new(),
            evidence,
            unavailable_reason: "host_reports_no_metal_family".into(),
        };
    }
    evidence.push(format!(
        "spdisplays_mtlgpufamilysupport={}",
        facts.metal_family
    ));
    // `runtime_version` 只放家族名（`metal4`）；带 `spdisplays_` 前缀的原始枚举令牌留在 evidence 里，
    // 这样消费方读到的版本号不必再剥前缀，而"宿主到底写了什么"也没有被改写掉。
    let version = facts
        .metal_family
        .strip_prefix("spdisplays_")
        .unwrap_or(&facts.metal_family)
        .to_string();
    if version.is_empty() {
        return unknown("metal", "metal_family_token_unreadable:system_profiler");
    }
    HostAccelerator {
        accelerator: "metal".into(),
        platform: capability::platform(),
        state: AcceleratorState::Available as i32,
        detection_source: "system_profiler".into(),
        runtime_version: version,
        evidence,
        unavailable_reason: String::new(),
    }
}

/// CoreML：先确认系统框架在，再读它自己写下的 `CFBundleVersion`。
///
/// 分成两步是为了让"框架不在"与"读不出版本"给出不同结论：前者是宿主的确定事实，
/// 后者只是这次探测没拿到答案。
pub fn coreml_fact(framework_present: bool, version: Probe) -> HostAccelerator {
    if !framework_present {
        return HostAccelerator {
            accelerator: "coreml".into(),
            platform: capability::platform(),
            state: AcceleratorState::Unavailable as i32,
            detection_source: "framework_info".into(),
            runtime_version: String::new(),
            evidence: vec!["framework=CoreML.framework".into()],
            unavailable_reason: "framework_absent:CoreML.framework".into(),
        };
    }
    let output = match version {
        Probe::Output(output) => output,
        other => return probe_failed("coreml", "framework_info", other),
    };
    let version = output.trim().to_string();
    if version.is_empty() {
        return unknown("coreml", "framework_version_empty:framework_info");
    }
    HostAccelerator {
        accelerator: "coreml".into(),
        platform: capability::platform(),
        state: AcceleratorState::Available as i32,
        detection_source: "framework_info".into(),
        runtime_version: version.clone(),
        evidence: vec![
            "framework=CoreML.framework".into(),
            format!("cf_bundle_version={version}"),
        ],
        unavailable_reason: String::new(),
    }
}

/// CUDA / TensorRT 家族：`nvidia-smi` 报告的 GPU 型号与驱动版本。
pub fn cuda_fact(probe: Probe) -> HostAccelerator {
    let output = match probe {
        Probe::Output(output) => output,
        other => return probe_failed("cuda", "nvidia_smi", other),
    };
    let first = output
        .lines()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .unwrap_or("");
    if first.is_empty() {
        return unknown("cuda", "nvidia_smi_reported_no_gpu:nvidia_smi");
    }
    let mut parts = first.split(',').map(str::trim);
    let model = parts.next().unwrap_or("");
    let driver = parts.next().unwrap_or("");
    if model.is_empty() {
        return unknown("cuda", "nvidia_smi_reported_no_model:nvidia_smi");
    }
    let mut evidence = vec![format!("gpu={model}")];
    if !driver.is_empty() {
        evidence.push(format!("driver_version={driver}"));
    }
    HostAccelerator {
        accelerator: "cuda".into(),
        platform: capability::platform(),
        state: AcceleratorState::Available as i32,
        detection_source: "nvidia_smi".into(),
        runtime_version: driver.to_string(),
        evidence,
        unavailable_reason: String::new(),
    }
}

/// "这次探测没能给出答案"的统一出口：`reason` 必须是完整原因串，调用方负责带上来源，
/// 因为 `detection_source` 记的是"哪条探测路径"，不是"为什么失败"。
fn unknown(accelerator: &str, reason: &str) -> HostAccelerator {
    HostAccelerator {
        accelerator: accelerator.into(),
        platform: capability::platform(),
        state: AcceleratorState::Unknown as i32,
        detection_source: "unavailable".into(),
        runtime_version: String::new(),
        evidence: Vec::new(),
        unavailable_reason: reason.into(),
    }
}

fn probe_failed(accelerator: &str, source: &str, probe: Probe) -> HostAccelerator {
    let reason = match probe {
        Probe::ToolMissing => format!("probe_tool_missing:{source}"),
        Probe::TimedOut => format!("probe_timed_out:{source}"),
        Probe::Failed(detail) => format!("probe_failed:{source}:{detail}"),
        Probe::Output(_) => unreachable!("callers pass a failed probe here"),
    };
    unknown(accelerator, &reason)
}

/// 直接探测（不走缓存）；验收脚本用 `report()`，测试用这个。
pub fn probe_all() -> Vec<HostAccelerator> {
    let mut facts = Vec::new();
    for accelerator in expected() {
        match *accelerator {
            "metal" => facts.push(metal_fact(run(
                "system_profiler",
                &["-json", "SPDisplaysDataType"],
            ))),
            "coreml" => facts.push(coreml_fact(
                std::path::Path::new(COREML_FRAMEWORK).exists(),
                run(
                    "plutil",
                    &[
                        "-extract",
                        "CFBundleVersion",
                        "raw",
                        &format!("{COREML_FRAMEWORK}/Versions/A/Resources/Info.plist"),
                    ],
                ),
            )),
            "cuda" => facts.push(cuda_fact(run(
                "nvidia-smi",
                &["--query-gpu=name,driver_version", "--format=csv,noheader"],
            ))),
            other => facts.push(unknown(other, "accelerator_has_no_probe:unavailable")),
        }
    }
    facts
}

/// 本进程的宿主加速器报告；探测按进程只做一次。
pub fn report() -> &'static [HostAccelerator] {
    static REPORT: OnceLock<Vec<HostAccelerator>> = OnceLock::new();
    REPORT.get_or_init(probe_all)
}

/// 稳定的一行摘要，给日志用：`coreml=available(3520.5.1),metal=available(metal4)`。
pub fn summary() -> String {
    report()
        .iter()
        .map(|fact| {
            let state = match AcceleratorState::try_from(fact.state) {
                Ok(AcceleratorState::Available) => "available".to_string(),
                Ok(AcceleratorState::Unavailable) => "unavailable".to_string(),
                Ok(AcceleratorState::Unknown) => "unknown".to_string(),
                _ => "unspecified".to_string(),
            };
            if fact.runtime_version.is_empty() {
                format!("{}={state}", fact.accelerator)
            } else {
                format!("{}={state}({})", fact.accelerator, fact.runtime_version)
            }
        })
        .collect::<Vec<_>>()
        .join(",")
}

/// 调用探测工具，并保证有界：超时即 kill。
fn run(tool: &str, args: &[&str]) -> Probe {
    let mut child = match Command::new(tool)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(child) => child,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Probe::ToolMissing,
        Err(error) => return Probe::Failed(error.kind().to_string()),
    };
    let deadline = Instant::now() + PROBE_TIMEOUT;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let mut stdout = String::new();
                if let Some(mut pipe) = child.stdout.take() {
                    let _ = pipe.read_to_string(&mut stdout);
                }
                if status.success() {
                    return Probe::Output(stdout);
                }
                return Probe::Failed(match status.code() {
                    Some(code) => format!("exit_status={code}"),
                    None => "exit_status=signal".to_string(),
                });
            }
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Probe::TimedOut;
                }
                std::thread::sleep(Duration::from_millis(25));
            }
            Err(error) => return Probe::Failed(error.kind().to_string()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 真实 `system_profiler -json SPDisplaysDataType` 输出（Apple M2 Max，macOS 15）。
    /// 保留整段是为了同时验证"嵌套的显示条目不影响判定"。
    const REAL_DISPLAYS_JSON: &str = r#"{
  "SPDisplaysDataType" : [
    {
      "_name" : "Apple M2 Max",
      "spdisplays_mtlgpufamilysupport" : "spdisplays_metal4",
      "spdisplays_ndrvs" : [
        {
          "_name" : "Color LCD",
          "spdisplays_connection_type" : "spdisplays_internal",
          "spdisplays_online" : "spdisplays_yes"
        }
      ],
      "spdisplays_vendor" : "sppci_vendor_Apple",
      "sppci_bus" : "spdisplays_builtin",
      "sppci_cores" : "30",
      "sppci_device_type" : "spdisplays_gpu",
      "sppci_model" : "Apple M2 Max"
    }
  ]
}"#;

    fn state(fact: &HostAccelerator) -> AcceleratorState {
        AcceleratorState::try_from(fact.state).expect("报告里的 state 必须是已知取值")
    }

    #[test]
    fn parses_real_displays_report() {
        let facts = parse_displays_json(REAL_DISPLAYS_JSON).expect("真实输出必须可解析");
        assert_eq!(facts.model, "Apple M2 Max");
        assert_eq!(facts.cores, "30");
        assert_eq!(facts.metal_family, "spdisplays_metal4");
    }

    #[test]
    fn judgement_follows_keys_not_localized_text() {
        // macOS 会本地化显示文本，因此判定只认 `spdisplays_*` / `sppci_*` 这些稳定键；
        // 值被本地化成中文时结论必须不变。
        let localized = r#"{"SPDisplaysDataType":[{"_name":"内置显示器","sppci_model":"Apple M2 Max",
            "sppci_cores":"30","spdisplays_mtlgpufamilysupport":"spdisplays_metal4"}]}"#;
        let facts = parse_displays_json(localized).expect("键名未变即可解析");
        assert_eq!(facts.metal_family, "spdisplays_metal4");
        let fact = metal_fact(Probe::Output(localized.to_string()));
        assert_eq!(state(&fact), AcceleratorState::Available);
        assert_eq!(fact.runtime_version, "metal4");
    }

    #[test]
    fn empty_or_unparsable_report_is_ignorance_not_absence() {
        for (payload, reason) in [
            ("not json at all", "displays_json_unparsable"),
            ("{}", "displays_report_empty"),
            (r#"{"SPDisplaysDataType":[]}"#, "displays_report_empty"),
        ] {
            assert_eq!(
                parse_displays_json(payload).expect_err("必须是解析失败"),
                reason
            );
            let fact = metal_fact(Probe::Output(payload.to_string()));
            assert_eq!(state(&fact), AcceleratorState::Unknown);
            assert!(fact.unavailable_reason.starts_with(reason));
            assert!(fact.runtime_version.is_empty());
            assert!(fact.evidence.is_empty());
        }
    }

    #[test]
    fn metal_available_carries_probed_version_and_bounded_evidence() {
        let fact = metal_fact(Probe::Output(REAL_DISPLAYS_JSON.to_string()));
        assert_eq!(state(&fact), AcceleratorState::Available);
        assert_eq!(fact.detection_source, "system_profiler");
        assert_eq!(fact.runtime_version, "metal4");
        assert_eq!(
            fact.evidence,
            vec![
                "sppci_model=Apple M2 Max".to_string(),
                "sppci_cores=30".to_string(),
                "spdisplays_mtlgpufamilysupport=spdisplays_metal4".to_string(),
            ]
        );
        assert!(fact.unavailable_reason.is_empty());
    }

    #[test]
    fn metal_without_family_token_is_host_says_no_not_probe_failure() {
        // 适配器条目在、但没有 Metal 家族令牌 = 宿主明确报告"没有"，与"探测不到"必须分开。
        let payload = r#"{"SPDisplaysDataType":[{"sppci_model":"Some GPU","sppci_cores":"8"}]}"#;
        let fact = metal_fact(Probe::Output(payload.to_string()));
        assert_eq!(state(&fact), AcceleratorState::Unavailable);
        assert_eq!(fact.unavailable_reason, "host_reports_no_metal_family");
        assert!(fact.runtime_version.is_empty());
        assert_eq!(fact.evidence.len(), 2);

        // 连型号都读不到时只能说"没探测到"，不能反过来断言宿主没有 GPU。
        let blank = r#"{"SPDisplaysDataType":[{"spdisplays_vendor":"sppci_vendor_Apple"}]}"#;
        let fact = metal_fact(Probe::Output(blank.to_string()));
        assert_eq!(state(&fact), AcceleratorState::Unknown);
        assert!(fact
            .unavailable_reason
            .starts_with("displays_entry_without_identity"));
    }

    #[test]
    fn every_probe_failure_outcome_stays_unknown() {
        for (probe, reason) in [
            (Probe::ToolMissing, "probe_tool_missing:system_profiler"),
            (Probe::TimedOut, "probe_timed_out:system_profiler"),
            (
                Probe::Failed("exit_status=1".to_string()),
                "probe_failed:system_profiler:exit_status=1",
            ),
        ] {
            let fact = metal_fact(probe);
            assert_eq!(state(&fact), AcceleratorState::Unknown);
            assert_eq!(fact.unavailable_reason, reason);
            assert_eq!(fact.detection_source, "unavailable");
        }
    }

    #[test]
    fn coreml_separates_absent_framework_from_unreadable_version() {
        // 框架不在 = 宿主的确定事实。
        let absent = coreml_fact(false, Probe::ToolMissing);
        assert_eq!(state(&absent), AcceleratorState::Unavailable);
        assert_eq!(
            absent.unavailable_reason,
            "framework_absent:CoreML.framework"
        );

        // 框架在、但版本读不出来 = 探测没给答案，不能顺手写成 unavailable。
        let unreadable = coreml_fact(true, Probe::ToolMissing);
        assert_eq!(state(&unreadable), AcceleratorState::Unknown);
        assert_eq!(
            unreadable.unavailable_reason,
            "probe_tool_missing:framework_info"
        );
        let empty = coreml_fact(true, Probe::Output("  \n".to_string()));
        assert_eq!(state(&empty), AcceleratorState::Unknown);
        assert_eq!(
            empty.unavailable_reason,
            "framework_version_empty:framework_info"
        );

        let available = coreml_fact(true, Probe::Output("3520.5.1\n".to_string()));
        assert_eq!(state(&available), AcceleratorState::Available);
        assert_eq!(available.runtime_version, "3520.5.1");
        assert_eq!(
            available.evidence,
            vec![
                "framework=CoreML.framework".to_string(),
                "cf_bundle_version=3520.5.1".to_string(),
            ]
        );
    }

    #[test]
    fn cuda_reads_gpu_and_driver_or_says_it_could_not() {
        let available = cuda_fact(Probe::Output(
            "NVIDIA GeForce RTX 4090, 550.54.14\n".to_string(),
        ));
        assert_eq!(state(&available), AcceleratorState::Available);
        assert_eq!(available.runtime_version, "550.54.14");
        assert_eq!(
            available.evidence,
            vec![
                "gpu=NVIDIA GeForce RTX 4090".to_string(),
                "driver_version=550.54.14".to_string(),
            ]
        );

        // 没有驱动版本时只上报真读到的那一项，不补一个猜的版本号。
        let model_only = cuda_fact(Probe::Output("NVIDIA GeForce RTX 4090\n".to_string()));
        assert_eq!(state(&model_only), AcceleratorState::Available);
        assert!(model_only.runtime_version.is_empty());
        assert_eq!(model_only.evidence.len(), 1);

        for (output, reason) in [
            ("", "nvidia_smi_reported_no_gpu:nvidia_smi"),
            (", 550.54.14", "nvidia_smi_reported_no_model:nvidia_smi"),
        ] {
            let fact = cuda_fact(Probe::Output(output.to_string()));
            assert_eq!(state(&fact), AcceleratorState::Unknown);
            assert_eq!(fact.unavailable_reason, reason);
        }
        assert_eq!(
            state(&cuda_fact(Probe::ToolMissing)),
            AcceleratorState::Unknown
        );
    }

    #[test]
    fn probe_never_claims_an_unexpected_accelerator() {
        // 探测只覆盖 `expected()` 的清单：多一条就是造假，少一条就是漏报。
        let probed = probe_all();
        assert_eq!(probed.len(), expected().len());
        for (fact, name) in probed.iter().zip(expected()) {
            assert_eq!(&fact.accelerator, name);
            assert_eq!(fact.platform, capability::platform());
            assert_eq!(report().len(), probe_all().len());
        }
    }

    #[test]
    fn summary_is_one_bounded_log_line() {
        let line = summary();
        assert!(!line.contains('\n'));
        assert!(!line.contains(' '));
        if !expected().is_empty() {
            assert_eq!(line.split(',').count(), expected().len());
        }
    }

    #[test]
    fn missing_probe_tool_is_not_mistaken_for_a_failure_output() {
        let probe = run("sensoryplex-probe-tool-does-not-exist", &[]);
        assert_eq!(probe, Probe::ToolMissing);
    }

    /// 探测必须有界：工具卡住时 kill 掉并落 `unknown`，不能拖住 `DescribeCapabilities`。
    #[test]
    fn a_probe_that_hangs_is_killed_at_the_deadline() {
        let started = Instant::now();
        let probe = run("sleep", &["30"]);
        assert_eq!(probe, Probe::TimedOut);
        assert!(
            started.elapsed() < PROBE_TIMEOUT * 3,
            "超时保护没有生效：{:?}",
            started.elapsed()
        );
    }
}
