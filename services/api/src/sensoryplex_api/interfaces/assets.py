"""有界媒体文件传输与授权读取；上传完成不表示媒体准入成功。"""

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Annotated

import anyio
from edge_material_sdk.generated.gateway.v1 import console_pb2 as pb
from fastapi import Body, Depends, Query, Request
from fastapi.responses import FileResponse
from psycopg.rows import dict_row

from ..contracts import audit, fail, identifier, one, out, parse, rows, text_field

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


def register(app, pool, auth, settings, upload_pool):
    root = settings.blob_root.resolve()

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
        staging = None
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
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if shutil.disk_usage(root).free < item["size_bytes"] + 100 * 1024**2:
                fail(507, "insufficient_storage")
            total, prefix = 0, bytearray()
            sha = hashlib.sha256()
            try:
                with tempfile.NamedTemporaryFile(
                    dir=root, prefix="upload-", delete=False
                ) as handle:
                    staging = Path(handle.name)
                    with anyio.fail_after(settings.upload_timeout_s):
                        async for chunk in request.stream():
                            total += len(chunk)
                            if total > min(item["size_bytes"], settings.max_upload_bytes):
                                fail(413, "upload_size_exceeded")
                            prefix.extend(chunk[: max(0, 16 - len(prefix))])
                            sha.update(chunk)
                            await anyio.to_thread.run_sync(handle.write, chunk)
                    await anyio.to_thread.run_sync(handle.flush)
                    await anyio.to_thread.run_sync(os.fsync, handle.fileno())
                if total != item["size_bytes"]:
                    fail(422, "upload_length_mismatch")
                valid = (item["content_type"] == "video/mp4" and prefix[4:8] == b"ftyp") or (
                    item["content_type"] == "video/webm" and prefix[:4] == b"\x1aE\xdf\xa3"
                )
                if not valid:
                    fail(422, "upload_container_signature_mismatch")
                target = root / sha.hexdigest()
                if target.is_symlink():
                    fail(409, "blob_path_invalid")
                os.replace(staging, target)
                directory = os.open(root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                result = await async_one(
                    conn,
                    (
                        "UPDATE console_upload SET state='awaiting_admission',sha256=%s "
                        "WHERE id=%s RETURNING *"
                    ),
                    ("sha256:" + sha.hexdigest(), key),
                )
                await conn.execute(
                    "INSERT INTO console_audit(id,actor,action,target) VALUES (%s,%s,%s,%s)",
                    (identifier("audit"), p.name, "upload.stored", key),
                )
            except TimeoutError:
                fail(408, "upload_deadline_exceeded")
            finally:
                if staging and staging.exists():
                    staging.unlink()
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
    def content(key: str, p: Annotated[object, Depends(auth.require("assets:read"))] = None):
        with pool.connection() as conn:
            item = one(conn, "SELECT * FROM console_upload WHERE id=%s AND owner=%s", (key, p.name))
        if not item:
            fail(404, "asset_not_found")
        if item["state"] != "awaiting_admission":
            fail(409, "upload_incomplete")
        target = root / item["sha256"].removeprefix("sha256:")
        if (
            target.is_symlink()
            or not target.is_file()
            or target.stat().st_size != item["size_bytes"]
        ):
            fail(503, "blob_unavailable")
        return FileResponse(
            target,
            media_type=item["content_type"],
            filename=item["filename"],
            content_disposition_type="inline",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )
