"""管理员签名者管理和有界流式外部制品导入。"""

import asyncio
import base64
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Annotated

import yaml
from edge_material_sdk.generated.node.v1 import node_pb2 as pb
from edge_material_sdk.release import (
    MAX_BUNDLE_BYTES,
    public_identity,
    verify_bundle,
    verify_signature,
)
from fastapi import Body, Depends, Header, Request
from starlette.concurrency import run_in_threadpool

from ..contracts import audit, fail, one, out, parse, rows, text_field
from ..infrastructure.plugin_registry import import_registration
from .plugin_deploy import release_proto


def administrator(principal):
    if "admin" not in principal.roles:
        fail(403, "administrator_required")


def register(app, pool, auth, settings):
    @app.get("/admin/v1/plugin-signers")
    def list_signers(p: Annotated[object, Depends(auth.require("plugins:manage"))] = None):
        administrator(p)
        with pool.connection() as conn:
            return out(
                {
                    "items": rows(
                        conn, "SELECT * FROM plugin_signer ORDER BY created_at DESC LIMIT 100"
                    )
                },
                pb.PluginSignerList,
            )

    @app.post("/admin/v1/plugin-signers", status_code=201)
    def approve(
        body: Annotated[dict, Body()],
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        administrator(p)
        request = parse(body, pb.ApprovePluginSignerRequest)
        name = text_field(request.display_name, 120)
        if len(request.public_key) > 512:
            fail(422, "signer_key_invalid")
        try:
            key_id = public_identity(request.public_key.encode())
        except (ValueError, TypeError):
            fail(422, "signer_key_invalid")
        with pool.connection() as conn:
            signer = one(
                conn,
                "INSERT INTO plugin_signer(signer_id,display_name,public_key,created_by) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING *",
                (key_id, name, request.public_key, p.name),
            )
            if not signer:
                fail(409, "plugin_signer_already_registered")
            audit(conn, p.name, "plugin.signer.approve", key_id)
            return out(signer, pb.PluginSigner)

    @app.post("/admin/v1/plugin-signers/{signer_id}:revoke")
    def revoke(
        signer_id: str, p: Annotated[object, Depends(auth.require("plugins:manage"))] = None
    ):
        administrator(p)
        with pool.connection() as conn:
            signer = one(
                conn,
                "UPDATE plugin_signer SET revoked_at=coalesce(revoked_at,now()) "
                "WHERE signer_id=%s RETURNING *",
                (signer_id,),
            )
            if not signer:
                fail(404, "plugin_signer_unknown")
            audit(conn, p.name, "plugin.signer.revoke", signer_id)
            return out(signer, pb.PluginSigner)

    @app.post("/admin/v1/plugin-releases:import", status_code=201)
    async def import_release(
        request: Request,
        x_plugin_descriptor: Annotated[str, Header()],
        x_plugin_signature: Annotated[str, Header()],
        x_plugin_signer: Annotated[str, Header()],
        p: Annotated[object, Depends(auth.require("plugins:manage"))] = None,
    ):
        administrator(p)
        try:
            if len(x_plugin_descriptor) > 22000 or len(x_plugin_signature) > 128:
                raise ValueError("release_descriptor_size_rejected")
            descriptor = json.loads(base64.b64decode(x_plugin_descriptor, validate=True))
            with pool.connection() as conn:
                signer = one(
                    conn, "SELECT * FROM plugin_signer WHERE signer_id=%s", (x_plugin_signer,)
                )
            if not signer:
                fail(403, "plugin_signer_unknown")
            if signer["revoked_at"]:
                fail(403, "plugin_signer_revoked")
            verify_signature(descriptor, x_plugin_signature, signer["public_key"].encode())
        except (ValueError, KeyError, TypeError):
            fail(422, "release_signature_invalid")
        expected = descriptor.get("bundle_bytes")
        if type(expected) is not int or not 0 < expected <= MAX_BUNDLE_BYTES:
            fail(413, "release_bundle_size_rejected")
        repository = settings.release_repository.resolve()
        repository.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=repository, prefix=".import-") as temporary:
            bundle = Path(temporary) / "bundle.tar.gz"
            size = 0
            try:
                async with asyncio.timeout(settings.upload_timeout_s):
                    with bundle.open("wb") as output:
                        async for chunk in request.stream():
                            size += len(chunk)
                            if size > expected:
                                fail(413, "release_bundle_size_rejected")
                            output.write(chunk)
            except TimeoutError:
                fail(408, "release_upload_timeout")
            try:
                registration = await run_in_threadpool(verify_bundle, bundle, descriptor)
            except (ValueError, KeyError, TypeError, OSError, tarfile.TarError, yaml.YAMLError):
                fail(422, "release_bundle_verification_failed")
            relative = (
                Path(descriptor["plugin_id"])
                / descriptor["plugin_version"]
                / (descriptor["platform"] + "-" + descriptor["arch"])
            )
            destination = repository / relative
            if not destination.resolve().is_relative_to(repository):
                fail(422, "release_path_invalid")
            with pool.connection() as conn:
                result = import_registration(
                    conn,
                    descriptor,
                    registration,
                    x_plugin_signer,
                    p.name,
                    (relative / "bundle.tar.gz").as_posix(),
                )
                destination.mkdir(parents=True, exist_ok=True)
                target = destination / "bundle.tar.gz"
                if target.exists():
                    from edge_material_sdk.release import file_digest

                    if file_digest(target) != descriptor["bundle_digest"]:
                        fail(409, "plugin_release_content_conflict")
                else:
                    shutil.copyfile(bundle, Path(temporary) / "verified.tar.gz")
                    os.replace(Path(temporary) / "verified.tar.gz", target)
                return out(release_proto(result), pb.PluginRelease)
