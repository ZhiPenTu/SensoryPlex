"""通用对象存储驱动契约测试 (FileSystemStorageDriver & S3StorageDriver)。"""

import hashlib
import time
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sensoryplex_api.infrastructure.storage import (
    BlobMetadata,
    FileSystemStorageDriver,
    S3StorageDriver,
    create_storage_driver,
)


@pytest.fixture
def temp_blob_dir(tmp_path: Path) -> Path:
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir(parents=True, exist_ok=True)
    return blob_dir


async def fake_stream(data: bytes, chunk_size: int = 16):
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]


@pytest.mark.anyio
async def test_filesystem_driver_put_and_stat(temp_blob_dir: Path):
    driver = FileSystemStorageDriver(
        root=temp_blob_dir,
        secret_key="test-secret",
        base_url="http://127.0.0.1:8091",
    )
    assert driver.backend_name() == "filesystem"

    # 有效的 MP4 样本数据 (ftyp 开头)
    mp4_data = b"\x00\x00\x00\x20ftypmp42\x00\x00\x00\x00mp41isom" + b"\x00" * 32
    expected_sha = hashlib.sha256(mp4_data).hexdigest()

    meta = await driver.put_blob(
        key="upload-001",
        stream=fake_stream(mp4_data),
        expected_size=len(mp4_data),
        content_type="video/mp4",
    )

    assert isinstance(meta, BlobMetadata)
    assert meta.sha256 == f"sha256:{expected_sha}"
    assert meta.size_bytes == len(mp4_data)
    assert meta.storage_backend == "filesystem"

    # 验证本地文件存在
    path = driver.get_blob_path(expected_sha)
    assert path is not None
    assert path.is_file()
    assert path.read_bytes() == mp4_data

    # 验证 exists 与 stat
    assert driver.exists(expected_sha)
    stat_meta = driver.stat(expected_sha)
    assert stat_meta is not None
    assert stat_meta.size_bytes == len(mp4_data)

    # 验证删除
    assert driver.delete(expected_sha)
    assert not driver.exists(expected_sha)
    assert driver.get_blob_path(expected_sha) is None


def test_filesystem_presigned_url_token_verification(temp_blob_dir: Path):
    driver = FileSystemStorageDriver(
        root=temp_blob_dir,
        secret_key="my-secret-key",
        base_url="http://127.0.0.1:8091",
    )
    fake_sha = "a" * 64
    url = driver.presign_get_url(fake_sha, expires_in_s=300, filename="video.mp4")

    parsed = urllib.parse.urlparse(url)
    assert parsed.path == f"/v1/assets/{fake_sha}/content"
    query = urllib.parse.parse_qs(parsed.query)
    assert "token" in query
    assert "expires" in query
    assert query["filename"] == ["video.mp4"]

    token = query["token"][0]
    expires = query["expires"][0]

    # 正确验签
    assert driver.verify_token(fake_sha, token, expires) is True

    # 篡改哈希导致验签失败
    assert driver.verify_token("b" * 64, token, expires) is False

    # 篡改令牌导致验签失败
    assert driver.verify_token(fake_sha, "bad-token", expires) is False

    # 过期时间在过去导致验签失败
    past_expires = str(int(time.time()) - 10)
    assert driver.verify_token(fake_sha, token, past_expires) is False


def test_s3_driver_signing_and_presigned_urls():
    driver = S3StorageDriver(
        endpoint_url="http://minio:9000",
        public_endpoint_url="http://127.0.0.1:29000",
        bucket="test-bucket",
        access_key="test-access-key",
        secret_key="test-secret-key",
        region="us-east-1",
    )

    assert driver.backend_name() == "s3"
    assert driver.get_blob_path("any-key") is None

    # 测试派生签名私钥为 32 字节哈希
    k_signing = driver._get_signing_key("20261006")
    assert len(k_signing) == 32

    fake_sha = "c" * 64
    get_url = driver.presign_get_url(
        fake_sha,
        expires_in_s=1800,
        filename="clip.mp4",
        content_type="video/mp4",
    )

    parsed = urllib.parse.urlparse(get_url)
    assert parsed.scheme == "http"
    assert parsed.netloc == "127.0.0.1:29000"
    assert parsed.path == f"/test-bucket/{fake_sha}"

    query = urllib.parse.parse_qs(parsed.query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert "test-access-key/2026" in query["X-Amz-Credential"][0]
    assert query["X-Amz-Expires"] == ["1800"]
    assert "X-Amz-Signature" in query
    assert len(query["X-Amz-Signature"][0]) == 64
    assert query["response-content-disposition"] == ['inline; filename="clip.mp4"']
    assert query["response-content-type"] == ["video/mp4"]

    # 测试预签名 PUT 直传链接
    put_url = driver.presign_put_url(fake_sha, expires_in_s=900, content_type="video/mp4")
    parsed_put = urllib.parse.urlparse(put_url)
    assert parsed_put.path == f"/test-bucket/{fake_sha}"
    query_put = urllib.parse.parse_qs(parsed_put.query)
    assert query_put["X-Amz-Expires"] == ["900"]
    assert "X-Amz-Signature" in query_put


def test_create_storage_driver_factory(temp_blob_dir: Path):
    mock_settings_fs = MagicMock()
    mock_settings_fs.storage_backend = "filesystem"
    mock_settings_fs.blob_root = temp_blob_dir

    driver_fs = create_storage_driver(mock_settings_fs)
    assert isinstance(driver_fs, FileSystemStorageDriver)
    assert driver_fs.backend_name() == "filesystem"

    mock_settings_s3 = MagicMock()
    mock_settings_s3.storage_backend = "s3"
    mock_settings_s3.s3_endpoint = "http://minio:9000"
    mock_settings_s3.s3_public_endpoint = "http://127.0.0.1:29000"
    mock_settings_s3.s3_bucket = "sensoryplex-media"
    mock_settings_s3.s3_access_key = "ak"
    mock_settings_s3.s3_secret_key = MagicMock(get_secret_value=lambda: "sk")
    mock_settings_s3.s3_region = "us-east-1"

    driver_s3 = create_storage_driver(mock_settings_s3)
    assert isinstance(driver_s3, S3StorageDriver)
    assert driver_s3.backend_name() == "s3"
