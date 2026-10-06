"""有界媒体文件传输与授权读取；上传完成不表示媒体准入成功。"""

import re
from typing import Annotated

from edge_material_sdk.generated.gateway.v1 import console_pb2 as pb
from fastapi import Body, Depends, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from psycopg.rows import dict_row

from ..contracts import audit, fail, identifier, one, out, parse, rows, text_field
from ..infrastructure.materials import get_material

TYPES = {"video/mp4", "video/webm"}


async def async_one(conn, query, params=()):
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(query, params)
        return await cursor.fetchone()


def record(row):
    return {
        **row,
        "reason": "media_admission_not_attached" if row["state"] == "awaiting_admission" else "",
    }


def stored_path(storage, item):
    """只解析平台保存的摘要地址；不读取目录记录中的任意路径或远程 URL。"""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", item["sha256"] or ""):
        fail(503, "blob_unavailable")
    target = storage.get_blob_path(item["sha256"])
    if target is None:
        if not storage.exists(item["sha256"]):
            fail(503, "blob_unavailable")
        return None
    if target.is_symlink() or not target.is_file() or target.stat().st_size != item["size_bytes"]:
        fail(503, "blob_unavailable")
    return target


def register(app, pool, auth, settings, upload_pool, storage=None):
    if storage is None:
        from ..infrastructure.storage import create_storage_driver

        storage = getattr(app.state, "storage", None) or create_storage_driver(settings)

    def get_storage():
        return getattr(app.state, "storage", None) or storage

    @app.get(
        "/v1/materials/{key}/sources/{asset_id}",
        dependencies=[Depends(auth.require("materials:read"))],
    )
    def material_source(
        key: str,
        asset_id: str,
        revision: int = Query(..., ge=1, le=2147483647),
        p: Annotated[object, Depends(auth.require("assets:read"))] = None,
    ):
        """解析指定 revision 的原片；upload:// 仅表示从流零点开始的完整文件。"""
        with pool.connection() as conn:
            material = get_material(conn, p.name, key, revision)
            if material is None:
                fail(404, "material_not_found")
            refs = [r for r in material.source_refs if r.asset_id == asset_id]
            if not refs:
                fail(404, "material_source_not_found")
            asset = one(
                conn,
                "SELECT a.*,s.type AS source_type FROM media_asset a "
                "JOIN stream_session stream ON stream.stream_id=a.stream_id "
                "JOIN media_source s ON s.source_id=stream.source_id "
                "WHERE a.asset_id=%s AND a.stream_id=%s AND s.owner=%s",
                (asset_id, material.stream_id, p.name),
            )
            if not asset or any(r.content_hash != asset["sha256"] for r in refs):
                fail(409, "material_source_mismatch")
            # 该命名空间是目录的显式声明，不按相同文件名或摘要猜测来源关系。
            match = re.fullmatch(r"upload://(asset_[0-9a-f]{32})", asset["object_uri"])
            if asset["source_type"] != "file" or not match:
                fail(409, "material_source_unmapped")
            if asset["duration_ms"] <= 0 or any(
                r.time_range.start_ms < 0
                or r.time_range.end_ms <= r.time_range.start_ms
                or r.time_range.end_ms > asset["duration_ms"]
                for r in refs
            ):
                fail(409, "material_source_time_invalid")
            item = one(
                conn,
                "SELECT * FROM console_upload WHERE id=%s AND owner=%s",
                (match[1], p.name),
            )
            if not item:
                fail(404, "material_source_not_found")
            if item["sha256"] != asset["sha256"]:
                fail(409, "material_source_mismatch")
            if item["state"] != "awaiting_admission":
                fail(409, "upload_incomplete")
            stored_path(storage, item)
        return out(record(item), pb.Upload)

    @app.post("/v1/uploads", status_code=201)
    def create(
        body: Annotated[dict, Body()] = ...,
        p: Annotated[object, Depends(auth.require("assets:write"))] = None,
    ):
        req = parse(body, pb.CreateUpload)
        name = text_field(req.filename, 240)
        if "/" in name or "\\" in name or req.content_type not in TYPES:
            fail(422, "unsupported_upload_type")
        if not 1 <= req.size_bytes <= settings.max_upload_bytes:
            fail(413, "upload_size_exceeded")
        with pool.connection() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("uploads:" + p.name,)
            )
            if (
                conn.execute(
                    "SELECT count(*) FROM console_upload WHERE owner=%s AND state='pending'",
                    (p.name,),
                ).fetchone()[0]
                >= 20
            ):
                fail(429, "pending_upload_limit_reached")
            result = one(
                conn,
                (
                    "INSERT INTO "
                    "console_upload(id,owner,filename,size_bytes,content_type) VALUES "
                    "(%s,%s,%s,%s,%s) RETURNING *"
                ),
                (identifier("asset"), p.name, name, req.size_bytes, req.content_type),
            )
            audit(conn, p.name, "upload.create", result["id"])
        return out(record(result), pb.Upload)

    @app.put("/v1/uploads/{key}/content")
    async def put_content(
        key: str,
        request: Request,
        p: Annotated[object, Depends(auth.require("assets:write"))] = None,
    ):
        # PostgreSQL 锁跨 API 进程限制全节点上传并发；断连会自动回滚并释放。
        async with upload_pool.connection() as conn:
            if not (
                await (await conn.execute("SELECT pg_try_advisory_xact_lock(826504121)")).fetchone()
            )[0]:
                fail(429, "upload_capacity_exhausted")
            item = await async_one(
                conn,
                "SELECT * FROM console_upload WHERE id=%s AND owner=%s FOR UPDATE",
                (key, p.name),
            )
            if not item:
                fail(404, "asset_not_found")
            if item["state"] != "pending":
                fail(409, "upload_already_completed")
            length = request.headers.get("content-length")
            if length and (not length.isdigit() or int(length) != item["size_bytes"]):
                fail(422, "upload_length_mismatch")
            blob_meta = await get_storage().put_blob(
                key=key,
                stream=request.stream(),
                expected_size=item["size_bytes"],
                content_type=item["content_type"],
                max_upload_bytes=settings.max_upload_bytes,
                timeout_s=settings.upload_timeout_s,
            )
            result = await async_one(
                conn,
                (
                    "UPDATE console_upload SET state='awaiting_admission',sha256=%s "
                    "WHERE id=%s RETURNING *"
                ),
                (blob_meta.sha256, key),
            )
            await conn.execute(
                "INSERT INTO console_audit(id,actor,action,target) VALUES (%s,%s,%s,%s)",
                (identifier("audit"), p.name, "upload.stored", key),
            )
        return out(record(result), pb.Upload)

    @app.delete("/v1/uploads/{key}")
    def cancel(key: str, p: Annotated[object, Depends(auth.require("assets:write"))] = None):
        with pool.connection() as conn:
            item = one(
                conn,
                "SELECT * FROM console_upload WHERE id=%s AND owner=%s FOR UPDATE",
                (key, p.name),
            )
            if not item:
                fail(404, "asset_not_found")
            if item["state"] != "pending":
                fail(409, "upload_already_completed")
            conn.execute("DELETE FROM console_upload WHERE id=%s", (key,))
            audit(conn, p.name, "upload.cancel", key)
        return out(pb.EmptyResponse())

    @app.get("/v1/assets")
    def assets(
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=10000),
        p: Annotated[object, Depends(auth.require("assets:read"))] = None,
    ):
        with pool.connection() as conn:
            items = rows(
                conn,
                (
                    "SELECT * FROM console_upload WHERE owner=%s ORDER BY created_at "
                    "DESC,id LIMIT %s OFFSET %s"
                ),
                (p.name, limit, offset),
            )
            total = conn.execute(
                "SELECT count(*) FROM console_upload WHERE owner=%s", (p.name,)
            ).fetchone()[0]
        return out({"items": [record(i) for i in items], "total": total}, pb.UploadList)

    @app.get("/v1/assets/{key}")
    def detail(key: str, p: Annotated[object, Depends(auth.require("assets:read"))] = None):
        with pool.connection() as conn:
            item = one(conn, "SELECT * FROM console_upload WHERE id=%s AND owner=%s", (key, p.name))
        if not item:
            fail(404, "asset_not_found")
        return out(record(item), pb.Upload)

    @app.get("/v1/assets/{key}/content")
    @app.head("/v1/assets/{key}/content")
    def content(
        key: str,
        request: Request,
        token: str | None = Query(None),
        expires: str | None = Query(None),
    ):
        is_presigned = False
        p = None
        if token and expires:
            is_presigned = True
        else:
            p = auth.identify(request)
            if "assets:read" not in p.scopes:
                fail(403, "permission_denied")

        with pool.connection() as conn:
            if is_presigned:
                if key.startswith("sha256:"):
                    item = one(conn, "SELECT * FROM console_upload WHERE sha256=%s", (key,))
                elif re.fullmatch(r"[0-9a-f]{64}", key):
                    item = one(
                        conn,
                        "SELECT * FROM console_upload WHERE sha256=%s OR id=%s",
                        ("sha256:" + key, key),
                    )
                else:
                    item = one(conn, "SELECT * FROM console_upload WHERE id=%s", (key,))
            else:
                if key.startswith("sha256:"):
                    item = one(
                        conn,
                        "SELECT * FROM console_upload WHERE sha256=%s AND owner=%s",
                        (key, p.name),
                    )
                elif re.fullmatch(r"[0-9a-f]{64}", key):
                    item = one(
                        conn,
                        "SELECT * FROM console_upload WHERE (sha256=%s OR id=%s) AND owner=%s",
                        ("sha256:" + key, key, p.name),
                    )
                else:
                    item = one(
                        conn,
                        "SELECT * FROM console_upload WHERE id=%s AND owner=%s",
                        (key, p.name),
                    )
        if not item:
            fail(404, "asset_not_found")
        if item["state"] != "awaiting_admission":
            fail(409, "upload_incomplete")

        active_storage = get_storage()
        if is_presigned and hasattr(active_storage, "verify_token"):
            clean_key = item["sha256"].removeprefix("sha256:")
            token_valid = active_storage.verify_token(
                clean_key, token, expires
            ) or active_storage.verify_token(item["id"], token, expires)
            if not token_valid:
                fail(403, "invalid_or_expired_token")

        target = stored_path(active_storage, item)
        if target is not None:
            return FileResponse(
                target,
                media_type=item["content_type"],
                filename=item["filename"],
                content_disposition_type="inline",
                headers={
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )

        # S3 / MinIO 模式：307 重定向至预签名 URL，承载 HTTP 206 切片流
        presigned_url = active_storage.presign_get_url(
            key=item["sha256"],
            expires_in_s=settings.s3_presigned_expire_s,
            filename=item["filename"],
            content_type=item["content_type"],
        )
        return RedirectResponse(
            url=presigned_url,
            status_code=307,
            headers={"Cache-Control": "private, no-store"},
        )

    @app.get("/v1/assets/{key}/presigned-url")
    def presigned_url(
        key: str,
        expires_in_s: int = Query(3600, ge=60, le=86400),
        p: Annotated[object, Depends(auth.require("assets:read"))] = None,
    ):
        with pool.connection() as conn:
            if key.startswith("sha256:"):
                item = one(
                    conn,
                    "SELECT * FROM console_upload WHERE sha256=%s AND owner=%s",
                    (key, p.name),
                )
            elif re.fullmatch(r"[0-9a-f]{64}", key):
                item = one(
                    conn,
                    "SELECT * FROM console_upload WHERE (sha256=%s OR id=%s) AND owner=%s",
                    ("sha256:" + key, key, p.name),
                )
            else:
                item = one(
                    conn,
                    "SELECT * FROM console_upload WHERE id=%s AND owner=%s",
                    (key, p.name),
                )
        if not item:
            fail(404, "asset_not_found")
        if item["state"] != "awaiting_admission":
            fail(409, "upload_incomplete")
        active_storage = get_storage()
        url = active_storage.presign_get_url(
            key=item["sha256"],
            expires_in_s=expires_in_s,
            filename=item["filename"],
            content_type=item["content_type"],
        )
        return {
            "url": url,
            "expires_in_s": expires_in_s,
            "storage_backend": active_storage.backend_name(),
            "sha256": item["sha256"],
        }

    @app.get("/v1/uploads/{key}/presigned-url")
    def upload_presigned_url(
        key: str,
        expires_in_s: int = Query(3600, ge=60, le=86400),
        p: Annotated[object, Depends(auth.require("assets:write"))] = None,
    ):
        with pool.connection() as conn:
            item = one(
                conn,
                "SELECT * FROM console_upload WHERE id=%s AND owner=%s",
                (key, p.name),
            )
        if not item:
            fail(404, "asset_not_found")
        if item["state"] != "pending":
            fail(409, "upload_already_completed")
        active_storage = get_storage()
        url = active_storage.presign_put_url(
            key=key,
            expires_in_s=expires_in_s,
            content_type=item["content_type"],
        )
        return {
            "url": url,
            "expires_in_s": expires_in_s,
            "storage_backend": active_storage.backend_name(),
            "upload_id": key,
        }
