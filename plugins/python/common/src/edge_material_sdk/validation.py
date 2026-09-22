import math
import re

from .generated.material.v1.material_pb2 import MaterialUnit, Observation


def validate_digest(value: str):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("invalid_sha256_digest")


def validate_range(value):
    if value.start_ms < 0 or value.end_ms <= value.start_ms:
        raise ValueError("invalid_half_open_time_range")


def validate_observation(value: Observation):
    if not all(
        (
            value.observation_id,
            value.stream_id,
            value.source_id,
            value.source_item_id,
            value.modality,
            value.timing_source,
        )
    ):
        raise ValueError("missing_observation_identity")
    validate_range(value.time_range)
    validate_digest(value.content_hash)
    for field in ("confidence", "timing_confidence"):
        if value.HasField(field):
            confidence = getattr(value, field)
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError("invalid_confidence")
    if not value.HasField("confidence") and not value.confidence_unavailable_reason:
        raise ValueError("missing_confidence_reason")
    if value.created_at_unix_ms <= 0:
        raise ValueError("missing_creation_time")
    if value.quality_state not in {
        "partial",
        "final",
        "accepted",
        "rejected",
        "low_confidence",
        "failed",
        "conflict",
    }:
        raise ValueError("invalid_quality_state")
    p = value.provenance
    if not all(
        (
            p.plugin,
            p.plugin_version,
            p.model_release_id,
            p.model_id,
            p.model_version,
            p.execution_backend,
        )
    ):
        raise ValueError("incomplete_provenance")
    validate_digest(p.artifact_digest)
    validate_digest(p.model_artifact_digest)
    validate_digest(p.config_hash)


def validate_material(value: MaterialUnit):
    if not all(
        (
            value.material_unit_id,
            value.stream_id,
            value.pipeline_version,
            value.revision,
            value.created_at_unix_ms > 0,
        )
    ):
        raise ValueError("invalid_material_identity")
    if value.status not in {
        "partial",
        "fast_ready",
        "enriched",
        "failed",
        "conflict",
        "low_confidence",
    }:
        raise ValueError("invalid_material_status")
    validate_range(value.time_range)
    if not value.source_refs:
        raise ValueError("missing_source_references")
    if not value.observations and value.status != "failed":
        raise ValueError("missing_observations")
    ids = set()
    for observation in value.observations:
        validate_observation(observation)
        if observation.observation_id in ids:
            raise ValueError("duplicate_observation_id")
        ids.add(observation.observation_id)
        if observation.stream_id != value.stream_id:
            raise ValueError("observation_stream_mismatch")
        _validate_containment(value.time_range, observation.time_range)
    for ref in value.source_refs:
        if not ref.asset_id:
            raise ValueError("missing_asset_id")
        validate_digest(ref.content_hash)
        validate_range(ref.time_range)
        _validate_containment(value.time_range, ref.time_range)
    if value.status == "enriched" and value.pending_enrichments:
        raise ValueError("enrichment_not_complete")


def _validate_containment(outer, inner):
    if inner.start_ms < outer.start_ms or inner.end_ms > outer.end_ms:
        raise ValueError("time_range_outside_material")
