"""ADR-032 真实制品与媒体的容器控制端验收；宿主执行由 Node Agent 完成。"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

import httpx
import psycopg
from edge_material_sdk.release import public_identity
from sensoryplex_api.auth import password_hash
from sensoryplex_api.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / ".data/plugin-v2-acceptance"


def client():
    credentials = json.loads((EVIDENCE / "credentials.json").read_text())
    http = httpx.Client(base_url="http://127.0.0.1:8091", timeout=120)
    if os.environ.get("SENSORYPLEX_ACCEPTANCE_DEMO") == "1":
        demo = checked(http.get("/auth/v1/demo-account"))
        if not demo["enabled"]:
            raise ValueError("acceptance_demo_unavailable")
        credentials = {"username": demo["username"], "password": demo["password"]}
    response = http.post("/auth/v1/session", json=credentials)
    response.raise_for_status()
    http.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return http


def checked(response):
    if response.status_code >= 400:
        raise ValueError(f"acceptance_http_{response.status_code}:" + response.text[:500])
    return response.json()


def prepare():
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / "credentials.json"
    if not path.exists():
        credentials = {"username": "plugin_acceptance", "password": secrets.token_urlsafe(30)}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as target:
            json.dump(credentials, target)
    credentials = json.loads(path.read_text())
    with psycopg.connect(Settings().database_url.get_secret_value()) as conn:
        conn.execute(
            "INSERT INTO console_user(username,display_name,password_hash,roles) "
            "VALUES (%s,'插件平台验收',%s,%s) ON CONFLICT DO NOTHING",
            (
                credentials["username"],
                password_hash(credentials["password"]),
                ["admin", "operator"],
            ),
        )
    print(json.dumps({"prepared": True}))


def import_releases(version=None):
    http = client()
    pem = (EVIDENCE / "external/publisher.public.pem").read_text()
    signer = public_identity(pem.encode())
    response = http.post(
        "/admin/v1/plugin-signers", json={"display_name": "独立插件验收发布者", "public_key": pem}
    )
    if response.status_code != 409:
        checked(response)
    imported = []
    for release in sorted((EVIDENCE / "external/releases").iterdir()):
        descriptor = json.loads((release / "release.json").read_text())
        if version and descriptor["plugin_version"] != version:
            continue
        headers = {
            "X-Plugin-Descriptor": base64.b64encode(
                (release / "release.json").read_bytes()
            ).decode(),
            "X-Plugin-Signature": (release / "release.sig").read_text(),
            "X-Plugin-Signer": signer,
            "Content-Type": "application/octet-stream",
        }
        with (release / "bundle.tar.gz").open("rb") as bundle:
            response = http.post(
                "/admin/v1/plugin-releases:import", content=bundle, headers=headers
            )
        result = checked(response)
        imported.append(
            {
                "release_id": descriptor["release_id"],
                "bundle_digest": descriptor["bundle_digest"],
                "artifact_digest": descriptor["artifact_digest"],
                "trust": result["trust"],
                "plugin_id": descriptor["plugin_id"],
            }
        )
    (EVIDENCE / "imports.json").write_text(json.dumps(imported, indent=2))
    print(json.dumps({"imported": imported}))


def provision(action="provision", plugin_id=None, threshold=None):
    http = client()
    existing = (
        json.loads((EVIDENCE / "deployments.json").read_text())
        if (EVIDENCE / "deployments.json").exists()
        else []
    )
    records = [r for r in existing if plugin_id and r["plugin_id"] != plugin_id]
    for release in json.loads((EVIDENCE / "imports.json").read_text()):
        if plugin_id and release["plugin_id"] != plugin_id:
            continue
        selected_action = action
        if action == "auto":
            topology = checked(http.get("/admin/v1/nodes"))
            selected_action = (
                "upgrade"
                if any(
                    i.get("active_runtime_instance_id") and i["plugin_id"] == release["plugin_id"]
                    for n in topology["items"]
                    if n["node_id"] == "local-host"
                    for i in n.get("instances", [])
                )
                else "provision"
            )
        config = checked(
            http.post(
                "/admin/v1/plugin-configurations",
                json={
                    "name": "真实验收 " + release["plugin_id"],
                    "plugin_id": release["plugin_id"],
                    "release_id": release["release_id"],
                    "config": {"threshold": threshold}
                    if threshold is not None
                    and release["plugin_id"].endswith("brightness-threshold")
                    else {},
                },
            )
        )
        operation = checked(
            http.post(
                f"/admin/v1/nodes/local-host/plugins/{release['plugin_id']}:{selected_action}",
                json={"release_id": release["release_id"], "config_id": config["id"]},
            )
        )
        records.append({**release, "config_id": config["id"], "operation": operation})
    (EVIDENCE / "deployments.json").write_text(json.dumps(records, indent=2))
    print(json.dumps({"deployments_requested": records}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "prepare",
            "import",
            "provision",
            "publish",
            "run",
            "status",
            "query",
            "wait",
            "full",
        ],
    )
    parser.add_argument("--media")
    parser.add_argument("--release-version")
    parser.add_argument("--upgrade", action="store_true")
    parser.add_argument("--plugin")
    parser.add_argument("--threshold", type=float)
    parser.add_argument(
        "--mode",
        choices=["measurement", "sync", "rollback_sync", "async_enrichment", "media_enrichment"],
        default="sync",
    )
    args = parser.parse_args()
    if args.command == "full":
        if not args.media or not args.release_version:
            parser.error("full_requires_media_and_release_version")
        prepare()
        import_releases(args.release_version)
        provision("auto", threshold=args.threshold)
        wait_deployments()
        for mode in ("sync", "async_enrichment", "media_enrichment"):
            publish(mode)
            run_media(Path(args.media), mode)
            wait_execution(mode)
            verify_query(mode, Path(args.media))
        return
    if args.command in {"prepare", "import", "provision"}:
        if args.command == "import":
            import_releases(args.release_version)
        elif args.command == "provision":
            provision("upgrade" if args.upgrade else "provision", args.plugin, args.threshold)
        else:
            prepare()
    elif args.command == "publish":
        publish(args.mode)
    elif args.command == "run":
        run_media(Path(args.media), args.mode)
    elif args.command == "query":
        verify_query(args.mode, Path(args.media) if args.media else EVIDENCE / "sample.mp4")
    elif args.command == "wait":
        wait_deployments()
    else:
        http = client()
        run = json.loads((EVIDENCE / (args.mode + ".run.json")).read_text())
        result = checked(http.get(f"/v1/jobs/{run['id']}/execution"))
        (EVIDENCE / (args.mode + ".execution.json")).write_text(json.dumps(result, indent=2))
        print(json.dumps(result))


def wait_deployments():
    http = client()
    completed = []
    for record in json.loads((EVIDENCE / "deployments.json").read_text()):
        operation_id = record["operation"]["operation_id"]
        deadline = time.monotonic() + 420
        while time.monotonic() < deadline:
            operation = checked(http.get("/admin/v1/plugin-deployments/" + operation_id))
            if operation["stage"] == "PLUGIN_OPERATION_STAGE_SUCCEEDED":
                assert operation["active"]["release_id"] == record["release_id"]
                completed.append(operation)
                break
            if operation["stage"] in {
                "PLUGIN_OPERATION_STAGE_FAILED",
                "PLUGIN_OPERATION_STAGE_CANCELLED",
            }:
                raise ValueError("acceptance_deployment_failed:" + operation["error_code"])
            time.sleep(2)
        else:
            raise ValueError("acceptance_deployment_timeout")
    (EVIDENCE / "deployments.settled.json").write_text(json.dumps(completed, indent=2))
    print(json.dumps({"deployments_verified": [r["operation_id"] for r in completed]}))


def wait_execution(mode):
    http = client()
    run = json.loads((EVIDENCE / (mode + ".run.json")).read_text())
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        try:
            value = checked(http.get(f"/v1/jobs/{run['id']}/execution"))
        except (httpx.ConnectError, httpx.TimeoutException):
            time.sleep(2)
            continue
        state = value["execution"]["state"]
        if state == "succeeded":
            (EVIDENCE / (mode + ".execution.json")).write_text(json.dumps(value, indent=2))
            return value
        if state in {"failed", "cancelled", "succeeded_with_partial_enrichment"}:
            raise ValueError(
                "acceptance_execution_failed:" + value["execution"].get("reason_code", state)
            )
        time.sleep(2)
    raise ValueError("acceptance_execution_timeout")


def publish(mode):
    http = client()
    deployments = {
        record["plugin_id"].rsplit(".", 1)[-1]: record
        for record in json.loads((EVIDENCE / "deployments.json").read_text())
    }
    nodes = []
    for name, node_id in [
        ("frame-brightness", "measurement"),
        ("brightness-threshold", "threshold"),
    ]:
        if mode in {"measurement", "media_enrichment"} and node_id == "threshold":
            continue
        record = deployments[name]
        nodes.append(
            {
                "id": node_id,
                "release_id": record["release_id"],
                "config_id": record["config_id"],
                "input_selector": "media" if node_id == "measurement" else "node:measurement",
                "execution_mode": "sync"
                if node_id == "measurement" or mode == "rollback_sync"
                else mode,
            }
        )
    if mode == "media_enrichment":
        nodes.append(
            {**nodes[0], "id": "delayed_measurement", "execution_mode": "async_enrichment"}
        )
    body = {
        "name": "独立亮度插件 " + mode + " " + str(int(time.time())),
        "nodes": nodes,
        "edges": [],
        "policy": {"evidence_retention_bytes": 268435456},
    }
    checked(http.post("/admin/v1/plugin-graphs:validate", json=body))
    result = checked(http.post("/admin/v1/plugin-graphs", json=body))
    result = checked(http.post(f"/admin/v1/pipelines/{result['id']}:publish"))
    (EVIDENCE / (mode + ".pipeline.json")).write_text(json.dumps(result, indent=2))
    print(json.dumps({"published": result["id"], "mode": mode}))


def run_media(media, mode):
    http = client()
    digest = hashlib.sha256(media.read_bytes()).hexdigest()
    upload = checked(
        http.post(
            "/v1/uploads",
            json={
                "filename": media.name,
                "size_bytes": media.stat().st_size,
                "content_type": "video/mp4",
            },
        )
    )
    with media.open("rb") as source:
        stored = checked(
            http.put(
                f"/v1/uploads/{upload['id']}/content",
                content=source,
                headers={"Content-Type": "application/octet-stream"},
            )
        )
        if stored["sha256"] != "sha256:" + digest:
            raise ValueError("acceptance_upload_digest_mismatch")
    pipeline = json.loads((EVIDENCE / (mode + ".pipeline.json")).read_text())
    job = checked(
        http.post(
            "/v1/job-drafts",
            json={
                "name": "真实独立插件验收 " + mode,
                "asset_id": upload["id"],
                "pipeline_id": pipeline["id"],
            },
        )
    )
    result = checked(http.post(f"/v1/job-drafts/{job['id']}:dispatch"))
    (EVIDENCE / (mode + ".run.json")).write_text(json.dumps(result, indent=2))
    print(json.dumps({"job_id": job["id"], "dispatched": result}))


def verify_query(mode, media):
    """对账本次执行、自定义事实、声明索引、鉴权和真实原片 Range。"""
    http = client()
    run = json.loads((EVIDENCE / (mode + ".run.json")).read_text())
    execution = checked(http.get(f"/v1/jobs/{run['id']}/execution"))["execution"]
    result = checked(
        http.post(
            "/v1/materials:search",
            json={
                "execution_id": execution["execution_id"],
                "mode": "keyword",
                "limit": 100,
            },
        )
    )
    assert result["materials"], "acceptance_materials_missing"
    ids, versions, observations, indexing = set(), set(), {}, {}
    for unit in result["materials"]:
        key = unit["material_unit_id"]
        ids.add(key)
        versions.add((key, unit["revision"]))
        scoped = checked(
            http.get(
                f"/v1/materials/{key}",
                params={
                    "revision": unit["revision"],
                    "execution_id": execution["execution_id"],
                },
            )
        )
        assert scoped["material_unit_id"] == key
        states = checked(
            http.get(
                f"/v1/materials/{key}/index-status",
                params={
                    "revision": unit["revision"],
                    "execution_id": execution["execution_id"],
                },
            )
        )
        for value in states["items"]:
            indexing[value["observation_id"]] = value
        for observation in scoped["observations"]:
            p = observation["provenance"]
            assert p["model_applicability"] == "MODEL_APPLICABILITY_NOT_APPLICABLE"
            assert p["processor_release_id"] and observation["schema_digest"]
            assert not p["model_id"] and not p["model_release_id"]
            observations[observation["observation_id"]] = observation
    hits = checked(
        http.post(
            "/v1/materials:search",
            json={
                "mode": "semantic",
                "query": "brightness above threshold",
                "limit": 100,
            },
        )
    )
    matched = [
        u
        for u in hits["materials"]
        if (u["material_unit_id"], u["revision"]) in versions
        and any(o["observation_id"] in observations for o in u["observations"])
    ]
    from edge_material_sdk.generated.orchestration.v1 import orchestration_pb2 as orchestration

    with psycopg.connect(Settings().database_url.get_secret_value()) as conn:
        outputs = conn.execute(
            "SELECT o.contract_bytes FROM pipeline_task t "
            "JOIN plugin_task_output o USING(task_id) WHERE t.run_id=%s",
            (execution["run_id"],),
        ).fetchall()
    expected = {
        o.observation_id
        for row in outputs
        for o in orchestration.PluginTaskOutput.FromString(row[0]).observations
    }
    assert expected <= set(observations), "acceptance_sync_observations_not_ingested"
    has_text = any(value["text_fields"] for value in indexing.values())
    if has_text:
        assert matched, "acceptance_current_execution_semantic_hit_missing"
        assert any(value["state"] == "ready" for value in indexing.values())
    for observation in observations.values():
        if observation["modality"].endswith("measurement"):
            assert indexing[observation["observation_id"]]["state"] == "not_declared"
    unit = next(u for u in result["materials"] if u["observations"])
    ref = unit["source_refs"][0]
    asset = checked(
        http.get(
            f"/v1/materials/{unit['material_unit_id']}/sources/{ref['asset_id']}",
            params={"revision": unit["revision"]},
        )
    )
    playback = http.get(f"/v1/assets/{asset['id']}/content", headers={"Range": "bytes=0-4095"})
    assert playback.status_code == 206 and playback.content == media.read_bytes()[:4096]
    unauth = httpx.Client(base_url=http.base_url)
    assert unauth.get(f"/v1/materials/{unit['material_unit_id']}").status_code == 401
    assert unauth.get(f"/v1/assets/{asset['id']}/content").status_code == 401
    report = {
        "execution_id": execution["execution_id"],
        "state": execution["state"],
        "materials": len(ids),
        "unique_observations": len(observations),
        "semantic_hits_in_execution": len(matched),
        "range_status": playback.status_code,
        "asset_digest": ref["content_hash"],
        "observations": list(observations.values()),
        "indexing": list(indexing.values()),
        "playback_range": unit["time_range"],
        "unauthenticated_denied": True,
    }
    (EVIDENCE / (mode + ".query.json")).write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k not in {"observations", "indexing"}}))


if __name__ == "__main__":
    main()
