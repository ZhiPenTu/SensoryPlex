"""隔离验收栈的真实插件故障控制与只读证据；不伪造任务或观测。"""

import argparse
import asyncio
import base64
import json
import time

import psycopg
from sensoryplex_api.settings import Settings

from tools.verify_plugin_platform import EVIDENCE, checked, client, run_media

PLUGIN = "com.example.brightness-threshold"


def poll_operation(http, identity, expected="PLUGIN_OPERATION_STAGE_SUCCEEDED"):
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        result = checked(http.get("/admin/v1/plugin-deployments/" + identity))
        if result["stage"] in {
            "PLUGIN_OPERATION_STAGE_SUCCEEDED",
            "PLUGIN_OPERATION_STAGE_FAILED",
            "PLUGIN_OPERATION_STAGE_CANCELLED",
        }:
            assert result["stage"] == expected, result["error_code"]
            return result
        time.sleep(2)
    raise ValueError("acceptance_operation_timeout")


def deploy(version, config, *, candidate_failure=False):
    http = client()
    folder = EVIDENCE / "external/releases" / ("brightness-threshold-" + version)
    descriptor = json.loads((folder / "release.json").read_text())
    from edge_material_sdk.release import public_identity

    signer = public_identity((EVIDENCE / "external/publisher.public.pem").read_bytes())
    checked(
        http.post(
            "/admin/v1/plugin-releases:import",
            content=(folder / "bundle.tar.gz").read_bytes(),
            headers={
                "X-Plugin-Descriptor": base64.b64encode(
                    (folder / "release.json").read_bytes()
                ).decode(),
                "X-Plugin-Signer": signer,
                "X-Plugin-Signature": (folder / "release.sig").read_text(),
                "Content-Type": "application/octet-stream",
            },
        )
    )
    before = checked(http.get("/admin/v1/nodes"))
    old = next(
        i
        for n in before["items"]
        if n["node_id"] == "local-host"
        for i in n["instances"]
        if i["plugin_id"] == PLUGIN
    )
    configuration = checked(
        http.post(
            "/admin/v1/plugin-configurations",
            json={
                "name": "独立故障验收 " + version,
                "plugin_id": PLUGIN,
                "release_id": descriptor["release_id"],
                "config": config,
            },
        )
    )
    operation = checked(
        http.post(
            f"/admin/v1/nodes/local-host/plugins/{PLUGIN}:upgrade",
            json={"release_id": descriptor["release_id"], "config_id": configuration["id"]},
        )
    )
    result = poll_operation(
        http,
        operation["operation_id"],
        "PLUGIN_OPERATION_STAGE_FAILED"
        if candidate_failure
        else "PLUGIN_OPERATION_STAGE_SUCCEEDED",
    )
    if candidate_failure:
        assert result["active"]["runtime_instance_id"] == old["active_runtime_instance_id"]
        assert result["active"]["release_id"] != descriptor["release_id"]
        assert result["error_code"]
        name = "candidate-failure"
    else:
        assert result["active"]["release_id"] == descriptor["release_id"]
        name = "fault-deployment"
    record = {
        "release_id": descriptor["release_id"],
        "config_id": configuration["id"],
        "operation": result,
    }
    (EVIDENCE / (name + ".json")).write_text(json.dumps(record, indent=2))
    print(
        json.dumps(
            {
                "operation_id": result["operation_id"],
                "stage": result["stage"],
                "error_code": result["error_code"],
                "active": result["active"]["runtime_instance_id"],
            }
        )
    )


def start(name):
    http = client()
    releases = {r["plugin_id"]: r for r in json.loads((EVIDENCE / "deployments.json").read_text())}
    measurement = releases["com.example.frame-brightness"]
    fault = json.loads((EVIDENCE / "fault-deployment.json").read_text())
    body = {
        "name": "真实故障验收 " + name,
        "nodes": [
            {
                "id": "measurement",
                "release_id": measurement["release_id"],
                "config_id": measurement["config_id"],
                "input_selector": "media",
                "execution_mode": "sync",
            },
            {
                "id": "threshold",
                "release_id": fault["release_id"],
                "config_id": fault["config_id"],
                "input_selector": "node:measurement",
                "execution_mode": "async_enrichment",
            },
        ],
        "edges": [],
    }
    pipeline = checked(http.post("/admin/v1/plugin-graphs", json=body))
    pipeline = checked(http.post(f"/admin/v1/pipelines/{pipeline['id']}:publish"))
    (EVIDENCE / (name + ".pipeline.json")).write_text(json.dumps(pipeline, indent=2))
    run_media(EVIDENCE / "sample.mp4", name)


def snapshot(name, http=None):
    http = http or client()
    run = json.loads((EVIDENCE / (name + ".run.json")).read_text())
    result = checked(http.get(f"/v1/jobs/{run['id']}/execution"))
    execution = result["execution"]
    from sensoryplex_api.contracts import rows

    with psycopg.connect(Settings().database_url.get_secret_value()) as conn:
        tasks = rows(
            conn,
            "SELECT task_id,state,attempt,reason_code,lease_expires_at FROM enrichment_task "
            "WHERE execution_id=%s ORDER BY start_ms,task_id",
            (execution["execution_id"],),
        )
        counts = conn.execute(
            "SELECT count(DISTINCT mo.observation_id) FROM material_execution e JOIN "
            "material_observation mo ON (mo.material_unit_id,mo.revision)="
            "(e.material_unit_id,e.material_revision) WHERE e.execution_id=%s",
            (execution["execution_id"],),
        ).fetchone()[0]
    for task in tasks:
        if task["lease_expires_at"]:
            task["lease_expires_at"] = task["lease_expires_at"].isoformat()
    evidence = {"execution": execution, "enrichment_tasks": tasks, "unique_observations": counts}
    (EVIDENCE / (name + ".fault.json")).write_text(json.dumps(evidence, indent=2))
    return evidence


def wait(name, *, cancel=False):
    http = client()
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        result = snapshot(name, http)
        execution = result["execution"]
        if cancel and execution["state"] == "ready_for_review":
            checked(
                http.post(
                    f"/v1/orchestration/runs/{execution['run_id']}:cancel",
                    json={"reason": "acceptance_cancel"},
                )
            )
            cancel = False
        if execution["state"] in {
            "succeeded",
            "succeeded_with_partial_enrichment",
            "cancelled",
            "failed",
        }:
            print(
                json.dumps(
                    {
                        "case": name,
                        "execution_id": execution["execution_id"],
                        "state": execution["state"],
                        "observations": result["unique_observations"],
                        "attempts": sorted({t["attempt"] for t in result["enrichment_tasks"]}),
                        "reasons": sorted({t["reason_code"] for t in result["enrichment_tasks"]}),
                    }
                )
            )
            return result
        time.sleep(0.2 if cancel else 2)
    raise ValueError("acceptance_fault_timeout")


def claimed(name, cancel=False):
    """等待真实 Consumer 获取租约，支持在 Process 在飞时发起取消。"""
    http = client()
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        result = snapshot(name, http)
        leased = [t for t in result["enrichment_tasks"] if t["lease_expires_at"]]
        if leased:
            if cancel:
                checked(
                    http.post(
                        f"/v1/orchestration/runs/{result['execution']['run_id']}:cancel",
                        json={"reason": "acceptance_cancel_inflight"},
                    )
                )
            print(
                json.dumps(
                    {
                        "claimed_task_id": leased[0]["task_id"],
                        "attempt": leased[0]["attempt"],
                        "cancelled": cancel,
                    }
                ),
                flush=True,
            )
            return
        time.sleep(0.05)
    raise ValueError("acceptance_claim_timeout")


def duplicate(name):
    """重新投递真实已暂存的结果通知，核对融合不重复追加。"""
    import nats
    from edge_material_sdk.enrichments import STREAM
    from sensoryplex_api.infrastructure import enrichments

    before = snapshot(name)
    with psycopg.connect(Settings().database_url.get_secret_value()) as conn:
        results = conn.execute(
            "SELECT r.task_id,r.result_digest FROM enrichment_result r "
            "JOIN enrichment_task t USING(task_id) WHERE t.execution_id=%s",
            (before["execution"]["execution_id"],),
        ).fetchall()
    assert results, "acceptance_staged_result_missing"

    async def deliver():
        from edge_material_sdk.generated.orchestration.v1 import orchestration_pb2 as pb

        bus = await nats.connect("nats://nats:4222")
        try:
            js = bus.jetstream()
            for identity, digest in results:
                await js.publish(
                    enrichments.RESULT_SUBJECT,
                    pb.EnrichmentResultReference(
                        task_id=identity, result_digest=digest
                    ).SerializeToString(deterministic=True),
                )
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                info = await js.consumer_info(STREAM, "enrichment-fuser-v1")
                if info.num_pending == 0 and info.num_ack_pending == 0:
                    return
                await asyncio.sleep(0.2)
            raise ValueError("acceptance_duplicate_not_consumed")
        finally:
            await bus.close()

    asyncio.run(deliver())
    after = snapshot(name)
    assert before["unique_observations"] == after["unique_observations"]
    (EVIDENCE / (name + ".duplicate.json")).write_text(
        json.dumps(
            {
                "execution_id": before["execution"]["execution_id"],
                "notifications": len(results),
                "before": before["unique_observations"],
                "after": after["unique_observations"],
            },
            indent=2,
        )
    )
    print(
        json.dumps(
            {
                "duplicate_notifications": len(results),
                "unique_observations": after["unique_observations"],
            }
        )
    )


def rollback():
    http = client()
    deployment = json.loads((EVIDENCE / "fault-deployment.json").read_text())
    old = deployment["operation"]["from_runtime_instance_id"]
    with psycopg.connect(Settings().database_url.get_secret_value()) as conn:
        target = conn.execute(
            "SELECT release_id,config_hash FROM plugin_runtime_instance "
            "WHERE runtime_instance_id=%s",
            (old,),
        ).fetchone()
        prior = conn.execute(
            "SELECT operation_id FROM plugin_deployment_operation "
            "WHERE rollback_of_operation_id=%s "
            "ORDER BY created_at DESC LIMIT 1",
            (deployment["operation"]["operation_id"],),
        ).fetchone()
    operation = (
        {"operation_id": prior[0]}
        if prior
        else checked(
            http.post(
                "/admin/v1/plugin-deployments/"
                + deployment["operation"]["operation_id"]
                + ":rollback"
            )
        )
    )
    result = poll_operation(http, operation["operation_id"])
    with psycopg.connect(Settings().database_url.get_secret_value()) as conn:
        actual = conn.execute(
            "SELECT release_id,config_hash FROM plugin_runtime_instance "
            "WHERE runtime_instance_id=%s",
            (result["active"]["runtime_instance_id"],),
        ).fetchone()
    assert actual == target, "acceptance_rollback_identity_mismatch"
    result["verified_config_hash"] = actual[1]
    (EVIDENCE / "rollback.json").write_text(json.dumps(result, indent=2))
    print(
        json.dumps(
            {
                "rollback_operation": result["operation_id"],
                "active_release": result["active"]["release_id"],
            }
        )
    )


def retire_unpinned():
    """只停用本验收用户的已结束方案，再走真实旧实例排空；保留全部事实。"""
    http = client()
    with psycopg.connect(Settings().database_url.get_secret_value()) as conn:
        revisions = conn.execute(
            "SELECT DISTINCT r.pipeline_id,r.revision FROM pipeline_revision r "
            "WHERE r.created_by='plugin_acceptance' AND NOT EXISTS "
            "(SELECT 1 FROM pipeline_run u WHERE u.pipeline_id=r.pipeline_id "
            "AND u.revision=r.revision "
            "AND (u.state IN ('accepted','validating','queued','running') "
            "OR EXISTS (SELECT 1 FROM enrichment_task t "
            "WHERE t.run_id=u.run_id AND t.state='queued')))"
        ).fetchall()
    for pipeline, revision in revisions:
        checked(http.post(f"/v1/orchestration/pipelines/{pipeline}/revisions/{revision}:retire"))
    from sensoryplex_api.contracts import rows
    from sensoryplex_api.infrastructure.runtime_bindings import runtime_is_pinned

    with psycopg.connect(Settings().database_url.get_secret_value()) as conn:
        runtimes = rows(
            conn,
            "SELECT * FROM plugin_runtime_instance WHERE node_id='local-host' "
            "AND plugin_id=%s AND role='previous' AND state='active'",
            (PLUGIN,),
        )
        selected = [r for r in runtimes if not runtime_is_pinned(conn, r)]
    completed = []
    for runtime in selected:
        operation = checked(
            http.post(
                f"/admin/v1/nodes/local-host/plugins/{PLUGIN}/runtimes/"
                f"{runtime['runtime_instance_id']}:retire"
            )
        )
        completed.append(poll_operation(http, operation["operation_id"]))
    (EVIDENCE / "retirement.json").write_text(json.dumps(completed, indent=2))
    print(
        json.dumps(
            {
                "retired_revisions": len(revisions),
                "drained_instances": [o["from_runtime_instance_id"] for o in completed],
            }
        )
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "deploy",
            "candidate-failure",
            "start",
            "wait",
            "cancel",
            "snapshot",
            "retire",
            "claimed",
            "cancel-inflight",
            "duplicate",
            "rollback",
        ],
    )
    parser.add_argument("--case", default="timeout")
    parser.add_argument("--delay-ms", type=int, default=0)
    parser.add_argument("--retryable", action="store_true")
    parser.add_argument("--version", default="0.1.91")
    args = parser.parse_args()
    if args.command in {"deploy", "candidate-failure"}:
        deploy(
            "0.1.90" if args.command == "candidate-failure" else args.version,
            {"threshold": 0}
            if args.command == "candidate-failure"
            else {
                "threshold": 0,
                "test_delay_ms": args.delay_ms,
                "test_retryable_failure": args.retryable,
            },
            candidate_failure=args.command == "candidate-failure",
        )
    elif args.command == "start":
        start(args.case)
    elif args.command in {"wait", "cancel"}:
        wait(args.case, cancel=args.command == "cancel")
    elif args.command == "retire":
        retire_unpinned()
    elif args.command in {"claimed", "cancel-inflight"}:
        claimed(args.case, cancel=args.command == "cancel-inflight")
    elif args.command == "duplicate":
        duplicate(args.case)
    elif args.command == "rollback":
        rollback()
    else:
        result = snapshot(args.case)
        print(
            json.dumps(
                {
                    "execution_id": result["execution"]["execution_id"],
                    "state": result["execution"]["state"],
                    "tasks": result["enrichment_tasks"],
                }
            )
        )


if __name__ == "__main__":
    main()
