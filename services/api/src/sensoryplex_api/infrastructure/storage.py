"""通用对象存储驱动抽象与实现 (S3 / MinIO / 本地文件系统)。

提供统一数据面读写契约：
- FileSystemStorageDriver: 本地文件系统持久化，兼容现有目录规范与基于 HMAC-SHA256 的临时令牌预签名；
- S3StorageDriver: 兼容 AWS S3 与 MinIO，纯标准库 AWS SigV4 签名，支持预签名与流式切片。
"""

from __future__ import annotations

import abc
import dataclasses
import datetime
import hashlib
import hmac
import http.client
import os
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Any

from ..contracts import fail


@dataclasses.dataclass(frozen=True)
class BlobMetadata:
    sha256: str
    size_bytes: int
    content_type: str
    storage_backend: str
    object_key: str


class StorageDriver(abc.ABC):
    """通用对象存储驱动接口基类。"""

    @abc.abstractmethod
    def backend_name(self) -> str:
        """返回存储驱动标识 (filesystem / s3 / minio)。"""

    @abc.abstractmethod
    async def put_blob(
        self,
        key: str,
        stream: AsyncIterable[bytes],
        expected_size: int,
        content_type: str,
        max_upload_bytes: int = 10 * 1024**3,
        timeout_s: float = 900.0,
    ) -> BlobMetadata:
        """流式保存对象数据，返回元数据；校验内容长度与 SHA-256。"""

    @abc.abstractmethod
    def get_blob_path(self, key: str) -> Path | None:
        """如果底层为本地文件系统则返回本地文件绝对路径；对象存储返回 None。"""

    @abc.abstractmethod
    def exists(self, key: str) -> bool:
        """检查对象是否存在。"""

    @abc.abstractmethod
    def stat(self, key: str) -> BlobMetadata | None:
        """查询对象元数据。"""

    @abc.abstractmethod
    def delete(self, key: str) -> bool:
        """删除对象。"""

    @abc.abstractmethod
    def presign_get_url(
        self,
        key: str,
        expires_in_s: int = 3600,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> str:
        """生成预签名 GET 下载与流式回看 URL。"""

    @abc.abstractmethod
    def presign_put_url(
        self,
        key: str,
        expires_in_s: int = 3600,
        content_type: str | None = None,
    ) -> str:
        """生成预签名 PUT 客户端直传 URL。"""


class FileSystemStorageDriver(StorageDriver):
    """本地文件系统存储驱动。完全兼容现有 .data/console-media 目录规范。"""

    def __init__(
        self,
        root: Path,
        secret_key: str = "sensoryplex-default-fs-secret",
        base_url: str = "http://127.0.0.1:8091",
    ):
        self.root = root.resolve()
        self.secret_key = secret_key.encode("utf-8")
        self.base_url = base_url.rstrip("/")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def backend_name(self) -> str:
        return "filesystem"

    def _clean_key(self, key: str) -> str:
        clean = key.removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", clean):
            fail(503, "blob_unavailable")
        return clean

    def get_blob_path(self, key: str) -> Path | None:
        clean = self._clean_key(key)
        target = self.root / clean
        if target.is_symlink() or not target.is_file():
            return None
        return target

    def exists(self, key: str) -> bool:
        path = self.get_blob_path(key)
        return path is not None and path.is_file()

    def stat(self, key: str) -> BlobMetadata | None:
        path = self.get_blob_path(key)
        if not path:
            return None
        st = path.stat()
        clean = self._clean_key(key)
        return BlobMetadata(
            sha256="sha256:" + clean,
            size_bytes=st.st_size,
            content_type="video/mp4",
            storage_backend="filesystem",
            object_key=clean,
        )

    def delete(self, key: str) -> bool:
        path = self.get_blob_path(key)
        if path and path.exists():
            path.unlink()
            return True
        return False

    async def put_blob(
        self,
        key: str,
        stream: AsyncIterable[bytes],
        expected_size: int,
        content_type: str,
        max_upload_bytes: int = 10 * 1024**3,
        timeout_s: float = 900.0,
    ) -> BlobMetadata:
        import anyio

        if shutil.disk_usage(self.root).free < expected_size + 100 * 1024**2:
            fail(507, "insufficient_storage")

        total = 0
        prefix = bytearray()
        sha = hashlib.sha256()
        staging: Path | None = None

        try:
            with tempfile.NamedTemporaryFile(
                dir=self.root, prefix="upload-", delete=False
            ) as handle:
                staging = Path(handle.name)
                with anyio.fail_after(timeout_s):
                    async for chunk in stream:
                        total += len(chunk)
                        if total > min(expected_size, max_upload_bytes):
                            fail(413, "upload_size_exceeded")
                        prefix.extend(chunk[: max(0, 16 - len(prefix))])
                        sha.update(chunk)
                        await anyio.to_thread.run_sync(handle.write, chunk)
                await anyio.to_thread.run_sync(handle.flush)
                await anyio.to_thread.run_sync(os.fsync, handle.fileno())

            if total != expected_size:
                fail(422, "upload_length_mismatch")

            valid = (content_type == "video/mp4" and prefix[4:8] == b"ftyp") or (
                content_type == "video/webm" and prefix[:4] == b"\x1aE\xdf\xa3"
            )
            if not valid:
                fail(422, "upload_container_signature_mismatch")

            hex_digest = sha.hexdigest()
            target = self.root / hex_digest
            if target.is_symlink():
                fail(409, "blob_path_invalid")
            os.replace(staging, target)
            staging = None

            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

            return BlobMetadata(
                sha256="sha256:" + hex_digest,
                size_bytes=total,
                content_type=content_type,
                storage_backend="filesystem",
                object_key=hex_digest,
            )
        except TimeoutError:
            fail(408, "upload_deadline_exceeded")
        finally:
            if staging and staging.exists():
                staging.unlink()

    def presign_get_url(
        self,
        key: str,
        expires_in_s: int = 3600,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> str:
        clean = self._clean_key(key)
        now_ts = int(datetime.datetime.now(datetime.UTC).timestamp())
        expire_ts = now_ts + expires_in_s
        payload = f"{clean}:{expire_ts}"
        token = hmac.new(self.secret_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        query = {"token": token, "expires": str(expire_ts)}
        if filename:
            query["filename"] = filename
        qs = urllib.parse.urlencode(query)
        return f"{self.base_url}/v1/assets/{clean}/content?{qs}"

    def presign_put_url(
        self,
        key: str,
        expires_in_s: int = 3600,
        content_type: str | None = None,
    ) -> str:
        now_ts = int(datetime.datetime.now(datetime.UTC).timestamp())
        expire_ts = now_ts + expires_in_s
        payload = f"put:{key}:{expire_ts}"
        token = hmac.new(self.secret_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        qs = urllib.parse.urlencode({"token": token, "expires": str(expire_ts)})
        return f"{self.base_url}/v1/uploads/{key}/content?{qs}"

    def verify_token(self, key: str, token: str, expires_str: str) -> bool:
        try:
            expires = int(expires_str)
        except (ValueError, TypeError):
            return False
        now_ts = int(datetime.datetime.now(datetime.UTC).timestamp())
        if now_ts > expires:
            return False
        clean = key.removeprefix("sha256:")
        payload = f"{clean}:{expires}"
        expected = hmac.new(self.secret_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(token, expected)


class S3StorageDriver(StorageDriver):
    """通用 S3 / MinIO 兼容对象存储驱动，纯标准库 AWS SigV4 协议实现。"""

    def __init__(
        self,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        public_endpoint_url: str | None = None,
        use_path_style: bool = True,
    ):
        self.endpoint_url = endpoint_url.rstrip("/")
        self.public_endpoint_url = (public_endpoint_url or endpoint_url).rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.use_path_style = use_path_style
        self._bucket_verified = False

    def backend_name(self) -> str:
        return "s3"

    def _clean_key(self, key: str) -> str:
        clean = key.removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", clean):
            fail(503, "blob_unavailable")
        return clean

    def get_blob_path(self, key: str) -> Path | None:
        return None

    def _get_signing_key(self, date_stamp: str) -> bytes:
        k_date = hmac.new(
            b"AWS4" + self.secret_key.encode("utf-8"), date_stamp.encode("utf-8"), hashlib.sha256
        ).digest()
        k_region = hmac.new(k_date, self.region.encode("utf-8"), hashlib.sha256).digest()
        k_service = hmac.new(k_region, b"s3", hashlib.sha256).digest()
        return hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()

    def _build_sigv4_query(
        self,
        method: str,
        path: str,
        params: dict[str, str],
        expires_in_s: int,
        host: str,
    ) -> str:
        now = datetime.datetime.now(datetime.UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        scope = f"{datestamp}/{self.region}/s3/aws4_request"

        query_params = {
            **params,
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self.access_key}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(expires_in_s),
            "X-Amz-SignedHeaders": "host",
        }

        canonical_query_string = "&".join(
            f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(str(v), safe='-_.~')}"
            for k, v in sorted(query_params.items())
        )

        canonical_headers = f"host:{host}\n"
        signed_headers = "host"
        payload_hash = "UNSIGNED-PAYLOAD"

        canonical_uri = urllib.parse.quote(path, safe="/-_.~")
        canonical_request = "\n".join(
            [
                method,
                canonical_uri,
                canonical_query_string,
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )

        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )

        signing_key = self._get_signing_key(datestamp)
        signature = hmac.new(
            signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        return f"{canonical_query_string}&X-Amz-Signature={signature}"

    def presign_get_url(
        self,
        key: str,
        expires_in_s: int = 3600,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> str:
        clean = self._clean_key(key)
        parsed = urllib.parse.urlparse(self.public_endpoint_url)
        host = parsed.netloc

        path = f"/{self.bucket}/{clean}" if self.use_path_style else f"/{clean}"
        params: dict[str, str] = {}
        if filename:
            params["response-content-disposition"] = f'inline; filename="{filename}"'
        if content_type:
            params["response-content-type"] = content_type

        qs = self._build_sigv4_query("GET", path, params, expires_in_s, host)
        return f"{self.public_endpoint_url}{path}?{qs}"

    def presign_put_url(
        self,
        key: str,
        expires_in_s: int = 3600,
        content_type: str | None = None,
    ) -> str:
        clean = self._clean_key(key)
        parsed = urllib.parse.urlparse(self.public_endpoint_url)
        host = parsed.netloc

        path = f"/{self.bucket}/{clean}" if self.use_path_style else f"/{clean}"
        params: dict[str, str] = {}
        if content_type:
            params["response-content-type"] = content_type

        qs = self._build_sigv4_query("PUT", path, params, expires_in_s, host)
        return f"{self.public_endpoint_url}{path}?{qs}"

    def _sign_header_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        payload: bytes = b"",
    ) -> dict[str, str]:
        now = datetime.datetime.now(datetime.UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        scope = f"{datestamp}/{self.region}/s3/aws4_request"

        parsed = urllib.parse.urlparse(self.endpoint_url)
        host = parsed.netloc

        payload_hash = hashlib.sha256(payload).hexdigest()
        req_headers = {
            **headers,
            "host": host,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
        }

        canonical_headers = "".join(
            f"{k.lower()}:{v.strip()}\n" for k, v in sorted(req_headers.items())
        )
        signed_headers = ";".join(sorted(k.lower() for k in req_headers))

        canonical_uri = urllib.parse.quote(path, safe="/-_.~")
        canonical_request = "\n".join(
            [
                method,
                canonical_uri,
                "",  # empty query
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )

        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )

        signing_key = self._get_signing_key(datestamp)
        signature = hmac.new(
            signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        auth_header = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return {**req_headers, "Authorization": auth_header}

    def _ensure_bucket(self) -> None:
        if self._bucket_verified:
            return
        path = f"/{self.bucket}"
        signed = self._sign_header_request("HEAD", path, {})
        req = urllib.request.Request(f"{self.endpoint_url}{path}", headers=signed, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status in (200, 204):
                    self._bucket_verified = True
                    return
        except urllib.error.HTTPError as err:
            if err.code == 404:
                # 桶不存在，自动创建
                put_signed = self._sign_header_request("PUT", path, {})
                put_req = urllib.request.Request(
                    f"{self.endpoint_url}{path}", headers=put_signed, method="PUT"
                )
                try:
                    with urllib.request.urlopen(put_req, timeout=10) as create_resp:
                        if create_resp.status in (200, 204):
                            self._bucket_verified = True
                            return
                except Exception:
                    pass
        except Exception:
            pass
        self._bucket_verified = True

    def exists(self, key: str) -> bool:
        clean = self._clean_key(key)
        path = f"/{self.bucket}/{clean}"
        signed = self._sign_header_request("HEAD", path, {})
        req = urllib.request.Request(f"{self.endpoint_url}{path}", headers=signed, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

    def stat(self, key: str) -> BlobMetadata | None:
        clean = self._clean_key(key)
        path = f"/{self.bucket}/{clean}"
        signed = self._sign_header_request("HEAD", path, {})
        req = urllib.request.Request(f"{self.endpoint_url}{path}", headers=signed, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                headers = dict(resp.headers)
                return BlobMetadata(
                    sha256="sha256:" + clean,
                    size_bytes=int(headers.get("Content-Length", 0)),
                    content_type=headers.get("Content-Type", "application/octet-stream"),
                    storage_backend="s3",
                    object_key=clean,
                )
        except Exception:
            return None

    def delete(self, key: str) -> bool:
        clean = self._clean_key(key)
        path = f"/{self.bucket}/{clean}"
        signed = self._sign_header_request("DELETE", path, {})
        req = urllib.request.Request(f"{self.endpoint_url}{path}", headers=signed, method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

    async def put_blob(
        self,
        key: str,
        stream: AsyncIterable[bytes],
        expected_size: int,
        content_type: str,
        max_upload_bytes: int = 10 * 1024**3,
        timeout_s: float = 900.0,
    ) -> BlobMetadata:
        import anyio

        self._ensure_bucket()

        total = 0
        prefix = bytearray()
        sha = hashlib.sha256()

        # 收集上传数据并做签名与完整性校验
        chunks: list[bytes] = []
        with anyio.fail_after(timeout_s):
            async for chunk in stream:
                total += len(chunk)
                if total > min(expected_size, max_upload_bytes):
                    fail(413, "upload_size_exceeded")
                prefix.extend(chunk[: max(0, 16 - len(prefix))])
                sha.update(chunk)
                chunks.append(chunk)

        if total != expected_size:
            fail(422, "upload_length_mismatch")

        valid = (content_type == "video/mp4" and prefix[4:8] == b"ftyp") or (
            content_type == "video/webm" and prefix[:4] == b"\x1aE\xdf\xa3"
        )
        if not valid:
            fail(422, "upload_container_signature_mismatch")

        hex_digest = sha.hexdigest()
        payload = b"".join(chunks)

        path = f"/{self.bucket}/{hex_digest}"
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(payload)),
        }
        signed = self._sign_header_request("PUT", path, headers, payload=payload)

        parsed = urllib.parse.urlparse(self.endpoint_url)
        is_ssl = parsed.scheme == "https"
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if is_ssl else 80)

        conn_cls = http.client.HTTPSConnection if is_ssl else http.client.HTTPConnection
        conn = conn_cls(host, port, timeout=30)
        try:
            conn.request("PUT", path, body=payload, headers=signed)
            resp = conn.getresponse()
            if resp.status not in (200, 204):
                fail(503, "s3_upload_failed")
        finally:
            conn.close()

        return BlobMetadata(
            sha256="sha256:" + hex_digest,
            size_bytes=total,
            content_type=content_type,
            storage_backend="s3",
            object_key=hex_digest,
        )


def create_storage_driver(settings: Any) -> StorageDriver:
    """根据应用 Settings 创建并初始化存储驱动实例。"""
    backend = getattr(settings, "storage_backend", "filesystem")
    if backend in ("s3", "minio"):
        endpoint = getattr(settings, "s3_endpoint", "") or "http://minio:9000"
        public_endpoint = getattr(settings, "s3_public_endpoint", "") or "http://127.0.0.1:29000"
        bucket = getattr(settings, "s3_bucket", "sensoryplex-media")
        access_key = getattr(settings, "s3_access_key", "") or os.getenv(
            "MINIO_ROOT_USER", "sensoryplex-local"
        )
        secret_key_val = ""
        secret_attr = getattr(settings, "s3_secret_key", None)
        if secret_attr is not None:
            if hasattr(secret_attr, "get_secret_value"):
                secret_key_val = secret_attr.get_secret_value()
            else:
                secret_key_val = str(secret_attr)
        if not secret_key_val:
            secret_key_val = os.getenv("MINIO_ROOT_PASSWORD", "sensoryplex-secret-key-2026")
        region = getattr(settings, "s3_region", "us-east-1")
        return S3StorageDriver(
            endpoint_url=endpoint,
            public_endpoint_url=public_endpoint,
            bucket=bucket,
            access_key=access_key,
            secret_key=secret_key_val,
            region=region,
        )
    return FileSystemStorageDriver(root=settings.blob_root)
