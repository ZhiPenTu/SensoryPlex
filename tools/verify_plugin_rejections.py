"""真实隔离 API 的拒绝证据；篡改既有制品/结果只用于负向准入，不制造成功事实。"""

import base64
import json

import httpx
import psycopg
from edge_material_sdk.generated.orchestration.v1 import orchestration_pb2 as pb
from edge_material_sdk.release import public_identity
from sensoryplex_api.infrastructure.enrichments import digest_result
from sensoryplex_api.settings import Settings

from tools.verify_plugin_platform import EVIDENCE, checked, client


def verify():
    http = client()
    records = []

    def denied(case, response, status, reason):
        assert response.status_code == status, (case, response.status_code)
        body = response.json()
        code = body.get("reason_code", body.get("detail"))
        assert code == reason, (case, code)
        records.append({"case": case, "status": status, "reason_code": code})
        (EVIDENCE / "rejections.json").write_text(json.dumps(records, indent=2))

    release = EVIDENCE / "external/releases/brightness-threshold-0.1.2"
    descriptor = (release / "release.json").read_bytes()
    signer = public_identity((EVIDENCE / "external/publisher.public.pem").read_bytes())
    headers = {
        "X-Plugin-Descriptor": base64.b64encode(descriptor).decode(),
        "X-Plugin-Signer": signer,
        "X-Plugin-Signature": (release / "release.sig").read_text(),
        "Content-Type": "application/octet-stream",
    }
    bundle = (release / "bundle.tar.gz").read_bytes()
    endpoint = "/admin/v1/plugin-releases:import"
    denied(
        "unknown_signer",
        http.post(
            endpoint, content=bundle, headers={**headers, "X-Plugin-Signer": "signer_unknown"}
        ),
        403,
        "plugin_signer_unknown",
    )
    denied(
        "wrong_signature",
        http.post(
            endpoint,
            content=bundle,
            headers={**headers, "X-Plugin-Signature": base64.b64encode(bytes(64)).decode()},
        ),
        422,
        "release_signature_invalid",
    )
    denied(
        "wrong_bundle_bytes",
        http.post(endpoint, content=bundle[:-1] + bytes([bundle[-1] ^ 1]), headers=headers),
        422,
        "release_bundle_verification_failed",
    )

    folder = EVIDENCE / "external/releases/brightness-threshold-0.1.93"
    public = (EVIDENCE / "external/revocation.public.pem").read_bytes()
    revoked = public_identity(public)
    approved = http.post(
        "/admin/v1/plugin-signers",
        json={"display_name": "撤销验收夹具", "public_key": public.decode()},
    )
    if approved.status_code != 409:
        checked(approved)
    revoke_headers = {
        **headers,
        "X-Plugin-Signer": revoked,
        "X-Plugin-Descriptor": base64.b64encode((folder / "release.json").read_bytes()).decode(),
        "X-Plugin-Signature": (folder / "release.sig").read_text(),
    }
    existing = next(
        (
            s
            for s in checked(http.get("/admin/v1/plugin-signers"))["items"]
            if s["signer_id"] == revoked
        ),
        None,
    )
    if existing.get("revoked_at_unix_ms") or existing.get("revoked_at"):
        imported = json.loads((folder / "release.json").read_text())
        assert any(
            r["release_id"] == imported["release_id"]
            for r in checked(http.get("/admin/v1/plugin-releases"))["items"]
        )
    else:
        imported = checked(
            http.post(
                endpoint, content=(folder / "bundle.tar.gz").read_bytes(), headers=revoke_headers
            )
        )
    checked(http.post("/admin/v1/plugin-signers/" + revoked + ":revoke"))
    denied(
        "revoked_import",
        http.post(
            endpoint, content=(folder / "bundle.tar.gz").read_bytes(), headers=revoke_headers
        ),
        403,
        "plugin_signer_revoked",
    )
    denied(
        "revoked_deploy",
        http.post(
            "/admin/v1/nodes/local-host/plugins/com.example.brightness-threshold:upgrade",
            json={"release_id": imported["release_id"]},
        ),
        403,
        "plugin_signer_revoked",
    )

    agent = json.loads((EVIDENCE / "agent/local-host.json").read_text())
    node = httpx.Client(
        base_url=http.base_url, headers={"Authorization": "Bearer " + agent["session_token"]}
    )
    with psycopg.connect(Settings().database_url.get_secret_value()) as conn:
        identity = json.loads((EVIDENCE / "crash.run.json").read_text())["execution_id"]
        raw = conn.execute(
            "SELECT r.contract_bytes FROM enrichment_result r "
            "JOIN enrichment_task t USING(task_id) WHERE t.execution_id=%s LIMIT 1",
            (identity,),
        ).fetchone()[0]
    original = pb.EnrichmentResult.FromString(raw)

    def result_case(case, mutate, reason):
        result = pb.EnrichmentResult.FromString(raw)
        mutate(result)
        result.result_digest = digest_result(result)
        denied(
            case,
            node.post(
                "/v1/agent/enrichments/" + result.task.task_id + ":result",
                json={
                    "lease_id": "expired",
                    "result_b64": base64.b64encode(result.SerializeToString()).decode(),
                },
            ),
            422,
            reason,
        )

    result_case(
        "output_schema_mismatch",
        lambda r: setattr(r.observations[0].payload.fields["mean_luma"], "string_value", "wrong"),
        "payload_schema_invalid",
    )
    result_case(
        "output_limit",
        lambda r: r.observations.extend([r.observations[0]] * 1025),
        "enrichment_result_limit_exceeded",
    )
    result_case(
        "unstructured_reason",
        lambda r: setattr(r, "outcome_reason", "/private/media.mov"),
        "plugin_result_reason_invalid",
    )
    denied(
        "node_credential_required",
        http.post("/v1/agent/enrichments/" + original.task.task_id + ":claim"),
        401,
        "missing_node_session_token",
    )
    # 已取消的真实在飞任务拒绝迟到的 Process 结果。
    with psycopg.connect(Settings().database_url.get_secret_value()) as conn:
        identity = json.loads((EVIDENCE / "cancel-inflight.run.json").read_text())["execution_id"]
        row = conn.execute(
            "SELECT manifest_bytes FROM enrichment_task WHERE execution_id=%s "
            "AND attempt=1 LIMIT 1",
            (identity,),
        ).fetchone()
    task = pb.EnrichmentTask.FromString(row[0])
    late = pb.EnrichmentResult(task=task, outcome=2, outcome_reason="below_threshold")
    late.result_digest = digest_result(late)
    denied(
        "late_result_after_cancel",
        node.post(
            "/v1/agent/enrichments/" + task.task_id + ":result",
            json={
                "lease_id": "expired",
                "result_b64": base64.b64encode(late.SerializeToString()).decode(),
            },
        ),
        409,
        "enrichment_cancelled",
    )
    (EVIDENCE / "rejections.json").write_text(json.dumps(records, indent=2))
    print(json.dumps(records))


if __name__ == "__main__":
    verify()
