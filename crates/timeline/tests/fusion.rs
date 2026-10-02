//! 纯契约样例验证聚合语义；这些构造数据不代表真实媒体或模型质量验收。

use prost::Message;
use prost_types::{value::Kind, ListValue, Struct, Value};
use sensoryplex_sdk::{
    common::TimeRange,
    material::{MaterialUnit, Observation, Provenance, SourceReference},
};
use sensoryplex_timeline::{
    FusionEngine, FusionInput, FusionLimits, FusionOutcome, FusionPolicy, FusionRequest,
    MaterialScope,
};

const ASR: &str = "asr_segment";
const VLM: &str = "vision.scene_description";
const OCR: &str = "ocr_block";

fn digest(c: char) -> String {
    format!("sha256:{}", c.to_string().repeat(64))
}
fn range(start_ms: i64, end_ms: i64) -> TimeRange {
    TimeRange { start_ms, end_ms }
}
fn scope() -> MaterialScope {
    MaterialScope {
        material_unit_id: "material-1".into(),
        stream_id: "stream-1".into(),
        source_id: "source-1".into(),
        time_range: range(0, 10_000),
    }
}
fn policy() -> FusionPolicy {
    FusionPolicy {
        pipeline_version: "fusion-v1".into(),
        required_modalities: vec![ASR.into(), VLM.into()],
        enrichment_modalities: vec![OCR.into()],
        limits: FusionLimits::default(),
    }
}
fn input(id: &str, modality: &str, state: &str, start: i64, end: i64) -> FusionInput {
    FusionInput {
        observation: Observation {
            observation_id: id.into(),
            modality: modality.into(),
            stream_id: "stream-1".into(),
            source_id: "source-1".into(),
            source_item_id: format!("item-{id}"),
            time_range: Some(range(start, end)),
            payload: Some(Struct {
                fields: [(
                    "text".into(),
                    Value {
                        kind: Some(Kind::StringValue(format!("契约样例 {id}"))),
                    },
                )]
                .into(),
            }),
            confidence: None,
            confidence_unavailable_reason: "model_does_not_provide_confidence".into(),
            quality_state: state.into(),
            quality_reasons: vec![],
            provenance: Some(Provenance {
                plugin: "test.plugin".into(),
                plugin_version: "1.0.0".into(),
                artifact_digest: digest('a'),
                model_release_id: format!("release-{modality}"),
                model_id: modality.into(),
                model_version: "1".into(),
                config_hash: digest('b'),
                execution_backend: "contract_test".into(),
                model_artifact_digest: digest('c'),
                ..Default::default()
            }),
            content_hash: digest('d'),
            created_at_unix_ms: 100,
            timing_source: "pts".into(),
            timing_confidence: None,
            ..Default::default()
        },
        source_ref: SourceReference {
            asset_id: "asset-1".into(),
            time_range: Some(range(start, end)),
            content_hash: digest('e'),
        },
    }
}
fn run(
    policy: FusionPolicy,
    inputs: &[FusionInput],
    previous: Option<&MaterialUnit>,
    now: i64,
) -> Result<FusionOutcome, sensoryplex_sdk::ContractError> {
    FusionEngine::new(policy)?.fuse(FusionRequest {
        scope: &scope(),
        inputs,
        previous,
        created_at_unix_ms: now,
    })
}
fn fuse(inputs: &[FusionInput]) -> FusionOutcome {
    run(policy(), inputs, None, 1000).unwrap()
}
fn failure(inputs: &[FusionInput]) -> &'static str {
    run(policy(), inputs, None, 1000).unwrap_err().0
}

#[test]
fn partial_fast_and_slow_paths_append_without_overwriting_history() {
    let asr = input("asr", ASR, "final", 0, 5000);
    let first = fuse(std::slice::from_ref(&asr));
    let snapshot = first.material.encode_to_vec();
    assert_eq!(first.material.status, "partial");
    assert_eq!(first.material.revision, 1);
    assert_eq!(first.material.pending_enrichments, [OCR, VLM]);
    let vlm = input("vlm", VLM, "final", 1000, 1033);
    let second = run(policy(), &[vlm], Some(&first.material), 2000).unwrap();
    assert_eq!(second.material.status, "fast_ready");
    assert_eq!(second.material.revision, 2);
    assert_eq!(second.material.pending_enrichments, [OCR]);
    assert_eq!(second.material.observations[0], asr.observation);
    let third = run(
        policy(),
        &[input("ocr", OCR, "accepted", 1000, 1033)],
        Some(&second.material),
        3000,
    )
    .unwrap();
    assert_eq!(third.material.status, "enriched");
    assert_eq!(third.material.revision, 3);
    assert!(third.material.pending_enrichments.is_empty());
    assert_eq!(third.material.observations.len(), 3);
    assert_eq!(first.material.encode_to_vec(), snapshot);
    assert!(!first.material.superseded);
}

#[test]
fn reordered_and_repeated_inputs_produce_identical_material_bytes() {
    let a = input("a", ASR, "final", 0, 5000);
    let b = input("b", VLM, "final", 1000, 1033);
    let first = fuse(&[a.clone(), b.clone()]);
    let other = fuse(&[b.clone(), a.clone(), b.clone()]);
    assert_eq!(
        first.material.encode_to_vec(),
        other.material.encode_to_vec()
    );
    assert_eq!(other.report.duplicate_observations, 1);
    let replay = run(policy(), &[b, a], Some(&first.material), 9999).unwrap();
    assert!(!replay.changed);
    assert_eq!(
        replay.material.encode_to_vec(),
        first.material.encode_to_vec()
    );
    assert_eq!(replay.report.added_observations, 0);
    assert_eq!(replay.report.duplicate_observations, 2);
}

#[test]
fn empty_retry_is_a_noop_but_empty_initial_input_is_an_error() {
    assert_eq!(failure(&[]), "fusion_empty_observations");
    let first = fuse(&[input("a", ASR, "final", 0, 5000)]);
    let retry = run(policy(), &[], Some(&first.material), 900).unwrap();
    assert!(!retry.changed);
    assert_eq!(retry.material, first.material);
}

#[test]
fn source_window_without_facts_requires_explicit_pending_state() {
    let mut material = fuse(&[input("a", ASR, "final", 0, 5000)]).material;
    material.observations.clear();
    material.status = "partial".into();
    material.pending_enrichments = vec![VLM.into()];
    assert!(sensoryplex_timeline::validate_material(&material).is_ok());
    material.status = "fast_ready".into();
    assert!(sensoryplex_timeline::validate_material(&material).is_err());
    material.status = "partial".into();
    material.pending_enrichments.clear();
    assert!(sensoryplex_timeline::validate_material(&material).is_err());
}

#[test]
fn conflicting_observation_identity_is_rejected_atomically() {
    let a = input("a", ASR, "final", 0, 5000);
    let first = fuse(std::slice::from_ref(&a));
    let original = first.material.clone();
    let mut changed = a.clone();
    changed
        .observation
        .provenance
        .as_mut()
        .unwrap()
        .model_version = "2".into();
    assert_eq!(
        failure(&[a, changed.clone()]),
        "immutable_observation_conflict"
    );
    assert_eq!(
        run(
            policy(),
            &[input("new", VLM, "final", 1000, 1033), changed],
            Some(&first.material),
            2000
        )
        .unwrap_err()
        .0,
        "immutable_observation_conflict"
    );
    assert_eq!(first.material, original);
}

#[test]
fn unknown_confidence_and_nested_payload_are_preserved_exactly() {
    let mut a = input("a", ASR, "final", 0, 5000);
    a.observation.payload.as_mut().unwrap().fields.insert(
        "segments".into(),
        Value {
            kind: Some(Kind::ListValue(ListValue {
                values: vec![Value {
                    kind: Some(Kind::StructValue(Struct {
                        fields: [(
                            "timing_outside_window".into(),
                            Value {
                                kind: Some(Kind::BoolValue(true)),
                            },
                        )]
                        .into(),
                    })),
                }],
            })),
        },
    );
    a.observation
        .quality_reasons
        .push("timing_outside_window".into());
    let result = fuse(std::slice::from_ref(&a));
    assert_eq!(result.material.observations[0], a.observation);
    assert_eq!(result.report.unknown_confidence_observations, 1);
    assert_eq!(result.report.unknown_timing_confidence_observations, 1);
    assert!(result.report.low_confidence_observation_ids.is_empty());
}

#[test]
fn same_stream_and_source_are_required() {
    for field in ["stream", "source"] {
        let mut a = input("a", ASR, "final", 0, 5000);
        if field == "stream" {
            a.observation.stream_id = "other".into();
        } else {
            a.observation.source_id = "other".into();
        }
        assert_eq!(failure(&[a]), "fusion_source_stream_mismatch");
    }
}

#[test]
fn half_open_window_accepts_exact_end_and_rejects_crossing_or_adjacent() {
    fuse(&[input("a", ASR, "final", 0, 10_000)]);
    for (start, end) in [(9999, 10_001), (10_000, 10_033)] {
        assert_eq!(
            failure(&[input("a", ASR, "final", start, end)]),
            "fusion_observation_outside_window"
        );
    }
    assert_eq!(
        failure(&[input("a", ASR, "final", 5, 5)]),
        "invalid_half_open_time_range"
    );
    assert_eq!(
        failure(&[input("a", ASR, "final", -1, 5)]),
        "invalid_half_open_time_range"
    );
}

#[test]
fn references_must_cover_observation_and_stay_inside_material() {
    let mut a = input("a", ASR, "final", 0, 5000);
    a.source_ref.time_range = Some(range(1, 5000));
    assert_eq!(failure(&[a.clone()]), "fusion_observation_source_uncovered");
    a.source_ref.time_range = Some(range(0, 10_001));
    assert_eq!(failure(&[a.clone()]), "source_reference_outside_material");
    a.source_ref.time_range = None;
    assert_eq!(failure(&[a]), "missing_source_time_range");
}

#[test]
fn asset_digest_is_immutable_across_windows_and_revisions() {
    let a = input("a", ASR, "final", 0, 5000);
    let mut b = input("b", VLM, "final", 5000, 6000);
    b.source_ref.content_hash = digest('f');
    assert_eq!(
        failure(&[a.clone(), b.clone()]),
        "fusion_asset_digest_conflict"
    );
    let first = fuse(&[a]);
    assert_eq!(
        run(policy(), &[b], Some(&first.material), 2000)
            .unwrap_err()
            .0,
        "fusion_asset_digest_conflict"
    );
}

#[test]
fn same_source_reference_is_deduplicated_but_observations_are_not_lost() {
    let a = input("a", VLM, "final", 1000, 1033);
    let b = input("b", OCR, "final", 1000, 1033);
    let result = fuse(&[a.clone(), b]);
    assert_eq!(result.material.observations.len(), 2);
    assert_eq!(result.material.source_refs, [a.source_ref]);
    assert_ne!(
        result.material.source_refs[0].content_hash,
        result.material.observations[0].content_hash
    );
}

#[test]
fn explicit_conflict_and_low_confidence_have_priority_and_keep_evidence() {
    let result = fuse(&[
        input("a", ASR, "low_confidence", 0, 5000),
        input("b", VLM, "conflict", 1000, 1033),
    ]);
    assert_eq!(result.material.status, "conflict");
    assert_eq!(result.report.conflict_observation_ids, ["b"]);
    assert_eq!(result.report.low_confidence_observation_ids, ["a"]);
    assert_eq!(result.material.observations.len(), 2);
    assert_eq!(
        fuse(&[input("a", ASR, "low_confidence", 0, 5000)])
            .material
            .status,
        "low_confidence"
    );
}

#[test]
fn different_model_text_is_not_an_inferred_semantic_conflict() {
    let result = fuse(&[
        input("a", ASR, "final", 0, 5000),
        input("b", VLM, "final", 0, 5000),
    ]);
    assert_eq!(result.material.status, "fast_ready");
    assert!(result.report.conflict_observation_ids.is_empty());
}

#[test]
fn failed_rejected_and_partial_are_never_counted_as_ready() {
    for state in ["failed", "rejected", "partial"] {
        let result = fuse(&[input("a", ASR, state, 0, 5000)]);
        assert_eq!(
            result.material.status,
            if state == "partial" {
                "partial"
            } else {
                "failed"
            }
        );
        assert!(result
            .report
            .missing_required_modalities
            .contains(&ASR.into()));
        assert_eq!(result.material.observations[0].quality_state, state);
    }
    let result = fuse(&[
        input("a", ASR, "failed", 0, 5000),
        input("b", VLM, "final", 1000, 1033),
    ]);
    assert_eq!(result.material.status, "partial");
    assert_eq!(result.report.failed_observations, 1);
}

#[test]
fn no_slow_path_means_fast_ready_not_enriched() {
    let mut p = policy();
    p.enrichment_modalities.clear();
    let result = run(
        p,
        &[
            input("a", ASR, "final", 0, 5000),
            input("b", VLM, "final", 1000, 1033),
        ],
        None,
        1000,
    )
    .unwrap();
    assert_eq!(result.material.status, "fast_ready");
    assert!(result.material.pending_enrichments.is_empty());
}

#[test]
fn invalid_policy_and_duplicate_or_overlapping_modalities_fail() {
    let mut p = policy();
    p.required_modalities.clear();
    assert_eq!(
        FusionEngine::new(p).err().unwrap().0,
        "invalid_fusion_policy"
    );
    let mut p = policy();
    p.limits.max_payload_depth = 65;
    assert_eq!(
        FusionEngine::new(p).err().unwrap().0,
        "invalid_fusion_policy"
    );
    for extra in [ASR, VLM, " "] {
        let mut p = policy();
        p.enrichment_modalities.push(extra.into());
        assert_eq!(
            FusionEngine::new(p).err().unwrap().0,
            "invalid_fusion_modalities"
        );
    }
}

#[test]
fn batch_and_cumulative_observation_limits_reject_instead_of_truncating() {
    let a = input("a", ASR, "final", 0, 5000);
    let b = input("b", VLM, "final", 1000, 1033);
    let mut p = policy();
    p.limits.max_batch_observations = 1;
    assert_eq!(
        run(p, &[a.clone(), b.clone()], None, 1000).unwrap_err().0,
        "fusion_batch_limit_exceeded"
    );
    let first = fuse(&[a]);
    let mut p = policy();
    p.limits.max_material_observations = 1;
    assert_eq!(
        run(p, &[b], Some(&first.material), 2000).unwrap_err().0,
        "fusion_observation_limit_exceeded"
    );
}

#[test]
fn source_and_window_limits_are_enforced() {
    let mut p = policy();
    p.limits.max_source_refs = 1;
    assert_eq!(
        run(
            p,
            &[
                input("a", ASR, "final", 0, 5000),
                input("b", VLM, "final", 1000, 1033)
            ],
            None,
            1000
        )
        .unwrap_err()
        .0,
        "fusion_source_limit_exceeded"
    );
    let mut p = policy();
    p.limits.max_window_ms = 9999;
    assert_eq!(
        run(p, &[], None, 1000).unwrap_err().0,
        "fusion_window_limit_exceeded"
    );
}

#[test]
fn per_observation_and_total_wire_bytes_are_bounded() {
    let a = input("a", ASR, "final", 0, 5000);
    let mut p = policy();
    p.limits.max_observation_bytes = a.observation.encoded_len() - 1;
    assert_eq!(
        run(p, std::slice::from_ref(&a), None, 1000).unwrap_err().0,
        "fusion_observation_bytes_exceeded"
    );
    let complete = fuse(std::slice::from_ref(&a));
    let mut p = policy();
    p.limits.max_observation_bytes = a.observation.encoded_len();
    p.limits.max_material_bytes = complete.material.encoded_len() - 1;
    assert_eq!(
        run(p, &[a], None, 1000).unwrap_err().0,
        "fusion_material_bytes_exceeded"
    );
}

#[test]
fn nested_payload_depth_and_value_count_are_bounded() {
    let mut a = input("a", ASR, "final", 0, 5000);
    let nested = Value {
        kind: Some(Kind::ListValue(ListValue {
            values: vec![Value {
                kind: Some(Kind::BoolValue(true)),
            }],
        })),
    };
    a.observation
        .payload
        .as_mut()
        .unwrap()
        .fields
        .insert("nested".into(), nested);
    let mut p = policy();
    p.limits.max_payload_depth = 1;
    assert_eq!(
        run(p, std::slice::from_ref(&a), None, 1000).unwrap_err().0,
        "fusion_payload_limit_exceeded"
    );
    let mut p = policy();
    p.limits.max_payload_values = 2;
    assert_eq!(
        run(p, &[a], None, 1000).unwrap_err().0,
        "fusion_payload_limit_exceeded"
    );
}

#[test]
fn nonfinite_payload_and_confidence_are_rejected() {
    for value in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        let mut a = input("a", ASR, "final", 0, 5000);
        a.observation.confidence = Some(value);
        assert_eq!(failure(&[a.clone()]), "invalid_confidence");
        a.observation.confidence = None;
        a.observation.payload.as_mut().unwrap().fields.insert(
            "bad".into(),
            Value {
                kind: Some(Kind::NumberValue(value)),
            },
        );
        assert_eq!(failure(&[a]), "fusion_invalid_payload");
    }
}

#[test]
fn missing_confidence_reason_and_invalid_provenance_are_rejected() {
    let mut a = input("a", ASR, "final", 0, 5000);
    a.observation.confidence_unavailable_reason.clear();
    assert_eq!(failure(&[a.clone()]), "missing_confidence_reason");
    a.observation.confidence = Some(0.8);
    a.observation
        .provenance
        .as_mut()
        .unwrap()
        .model_artifact_digest = "sha256:bad".into();
    assert_eq!(failure(&[a]), "invalid_sha256_digest");
}

#[test]
fn previous_identity_policy_and_superseded_state_are_checked() {
    let original = fuse(&[input("a", ASR, "final", 0, 5000)]).material;
    for field in ["id", "pipeline", "window"] {
        let mut previous = original.clone();
        match field {
            "id" => previous.material_unit_id = "other".into(),
            "pipeline" => previous.pipeline_version = "other".into(),
            _ => previous.time_range = Some(range(0, 9000)),
        }
        assert_eq!(
            run(policy(), &[], Some(&previous), 2000).unwrap_err().0,
            "fusion_previous_identity_mismatch"
        );
    }
    let mut previous = original;
    previous.superseded = true;
    assert_eq!(
        run(policy(), &[], Some(&previous), 2000).unwrap_err().0,
        "fusion_previous_superseded"
    );
}

#[test]
fn changed_revision_requires_increasing_time_and_checked_counter() {
    let mut previous = fuse(&[input("a", ASR, "final", 0, 5000)]).material;
    let b = input("b", VLM, "final", 1000, 1033);
    for now in [999, 1000] {
        assert_eq!(
            run(policy(), std::slice::from_ref(&b), Some(&previous), now)
                .unwrap_err()
                .0,
            "fusion_revision_time_not_increasing"
        );
    }
    previous.revision = u32::MAX;
    assert_eq!(
        run(policy(), &[b], Some(&previous), 2000).unwrap_err().0,
        "fusion_revision_overflow"
    );
    assert!(!run(policy(), &[], Some(&previous), 2000).unwrap().changed);
}

#[test]
fn previous_duplicate_or_uncovered_evidence_is_rejected() {
    let mut previous = fuse(&[input("a", ASR, "final", 0, 5000)]).material;
    previous.observations.push(previous.observations[0].clone());
    assert_eq!(
        run(policy(), &[], Some(&previous), 2000).unwrap_err().0,
        "fusion_previous_duplicate_observation"
    );
    previous.observations.pop();
    previous.source_refs[0].time_range = Some(range(5000, 9000));
    assert_eq!(
        run(policy(), &[], Some(&previous), 2000).unwrap_err().0,
        "fusion_observation_source_uncovered"
    );
}

#[test]
fn labels_are_retained_without_inventing_tags_from_model_text() {
    let mut previous = fuse(&[input("a", ASR, "final", 0, 5000)]).material;
    assert!(previous.tags.is_empty());
    previous.tags = vec!["人工标签".into()];
    let result = run(
        policy(),
        &[input("b", VLM, "final", 1000, 1033)],
        Some(&previous),
        2000,
    )
    .unwrap();
    assert_eq!(result.material.tags, previous.tags);
}

#[test]
fn late_observation_is_ordered_by_media_time_not_arrival_time() {
    let first = fuse(&[input("z", VLM, "final", 9000, 9033)]);
    let result = run(
        policy(),
        &[input("a", ASR, "final", 0, 5000)],
        Some(&first.material),
        2000,
    )
    .unwrap();
    assert_eq!(
        result
            .material
            .observations
            .iter()
            .map(|o| o.observation_id.as_str())
            .collect::<Vec<_>>(),
        ["a", "z"]
    );
    assert_eq!(result.material.time_range, Some(scope().time_range));
}

#[test]
fn material_level_conflict_or_changed_policy_cannot_be_silently_erased() {
    let mut previous = fuse(&[input("a", ASR, "final", 0, 5000)]).material;
    previous.status = "conflict".into();
    assert_eq!(
        run(policy(), &[], Some(&previous), 2000).unwrap_err().0,
        "fusion_previous_policy_mismatch"
    );
    previous.status = "partial".into();
    let mut changed = policy();
    changed.enrichment_modalities.clear();
    assert_eq!(
        run(changed, &[], Some(&previous), 2000).unwrap_err().0,
        "fusion_previous_policy_mismatch"
    );
}
