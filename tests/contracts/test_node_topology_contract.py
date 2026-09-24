"""ADR-026 契约测试：验证 Protobuf 序列化、字段存在性与 Preflight 预检规则。"""

from datetime import UTC, datetime, timedelta

from edge_material_sdk.generated.node.v1 import node_pb2 as pb
from google.protobuf.json_format import MessageToDict, ParseDict
from sensoryplex_api.infrastructure.preflight import check_preflight, parse_memory_bytes


def test_protobuf_contracts_roundtrip():
    profile = pb.NodeCapabilityProfile(
        platform="macos",
        arch="aarch64",
        cpu_cores=8,
        memory_bytes=16 * 1024 * 1024 * 1024,
        unified_memory_bytes=16 * 1024 * 1024 * 1024,
        supported_artifacts=["local_native", "container"],
    )
    profile.labels["env"] = "lab"

    node = pb.NodeInfo(
        node_id="test-node-1",
        display_name="Mac mini Lab",
        status=pb.NodeStatus.NODE_STATUS_READY,
        status_reason="",
        capabilities=profile,
        is_co_located=True,
        last_heartbeat_at=datetime.now(UTC).isoformat(),
        enrolled_at=datetime.now(UTC).isoformat(),
    )
    as_dict = MessageToDict(node, preserving_proto_field_name=True)
    assert as_dict["node_id"] == "test-node-1"
    assert as_dict["status"] == "NODE_STATUS_READY"
    assert as_dict["capabilities"]["cpu_cores"] == 8

    recovered = ParseDict(as_dict, pb.NodeInfo())
    assert recovered.node_id == "test-node-1"
    assert recovered.status == pb.NodeStatus.NODE_STATUS_READY
    assert recovered.capabilities.cpu_cores == 8


def test_parse_memory_bytes():
    assert parse_memory_bytes("1Gi") == 1024 * 1024 * 1024
    assert parse_memory_bytes("512Mi") == 512 * 1024 * 1024
    assert parse_memory_bytes("2G") == 2 * 1000 * 1000 * 1000
    assert parse_memory_bytes("1048576") == 1048576
    assert parse_memory_bytes("invalid") == 0


def test_preflight_node_missing_and_revoked():
    plugin_entry = {
        "id": "org.sensoryplex.test",
        "digest": "sha256:" + "a" * 64,
        "accepts_memory_kinds": [],
        "form": "local_native",
        "resources": {"cpu": "1", "memory": "512Mi"},
    }
    # 节点不存在
    res = check_preflight(None, plugin_entry)
    assert not res["eligible"]
    assert res["reason_code"] == "node_not_found"

    # 节点已被撤销
    revoked_node = {"node_id": "n1", "status": "revoked"}
    res = check_preflight(revoked_node, plugin_entry)
    assert not res["eligible"]
    assert res["reason_code"] == "node_revoked"

    # 节点离线
    offline_node = {"node_id": "n1", "status": "offline"}
    res = check_preflight(offline_node, plugin_entry)
    assert not res["eligible"]
    assert res["reason_code"] == "node_offline"

    # 节点排空中
    draining_node = {"node_id": "n1", "status": "draining"}
    res = check_preflight(draining_node, plugin_entry)
    assert not res["eligible"]
    assert res["reason_code"] == "node_draining"


def test_preflight_heartbeat_stale():
    plugin_entry = {
        "id": "org.sensoryplex.test",
        "digest": "sha256:" + "a" * 64,
        "accepts_memory_kinds": [],
        "form": "local_native",
        "resources": {"cpu": "1", "memory": "512Mi"},
    }
    stale_time = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    stale_node = {
        "node_id": "n1",
        "status": "ready",
        "last_heartbeat_at": stale_time,
        "supported_artifacts": ["local_native"],
        "cpu_cores": 4,
        "memory_bytes": 4 * 1024 * 1024 * 1024,
    }
    res = check_preflight(stale_node, plugin_entry)
    assert not res["eligible"]
    assert res["reason_code"] == "node_heartbeat_stale"


def test_preflight_data_locality_enforcement():
    """ADR-026 §2.5 / ADR-010: 共享内存句柄仅在同机有效；远端节点必须拒绝。"""
    shm_plugin = {
        "id": "org.sensoryplex.vlm-moondream",
        "digest": "sha256:" + "b" * 64,
        "accepts_memory_kinds": ["cpu_shared_memory"],
        "form": "local_native",
        "resources": {"cpu": "1", "memory": "1Gi"},
    }
    fresh_time = datetime.now(UTC).isoformat()
    remote_node = {
        "node_id": "remote-lan-1",
        "status": "ready",
        "last_heartbeat_at": fresh_time,
        "is_co_located": False,
        "platform": "linux",
        "arch": "x86_64",
        "supported_artifacts": ["local_native", "container"],
        "cpu_cores": 16,
        "memory_bytes": 32 * 1024 * 1024 * 1024,
    }
    # 远端局域网节点跑共享内存插件 -> 必须拒绝并返回 data_locality_violation
    res = check_preflight(remote_node, shm_plugin)
    assert not res["eligible"]
    assert res["reason_code"] == "data_locality_violation"

    # 同机节点跑共享内存插件 -> 允许通过
    colocated_node = dict(remote_node)
    colocated_node["is_co_located"] = True
    res_colocated = check_preflight(colocated_node, shm_plugin)
    assert res_colocated["eligible"]


def test_preflight_remote_execution_permitted_for_observations():
    """文本或观测等非共享内存制品允许在局域网节点执行。"""
    text_plugin = {
        "id": "org.sensoryplex.embed-bge-onnx",
        "digest": "sha256:" + "c" * 64,
        "accepts_memory_kinds": [],  # 不读共享内存
        "form": "local_native",
        "resources": {"cpu": "1", "memory": "512Mi"},
    }
    fresh_time = datetime.now(UTC).isoformat()
    remote_node = {
        "node_id": "remote-lan-1",
        "status": "ready",
        "last_heartbeat_at": fresh_time,
        "is_co_located": False,
        "platform": "linux",
        "arch": "x86_64",
        "supported_artifacts": ["local_native"],
        "cpu_cores": 8,
        "memory_bytes": 16 * 1024 * 1024 * 1024,
    }
    res = check_preflight(remote_node, text_plugin)
    assert res["eligible"]


def test_preflight_platform_and_accelerator_checks():
    mlx_plugin = {
        "id": "org.sensoryplex.asr-whisper-mlx",
        "digest": "sha256:" + "d" * 64,
        "accepts_memory_kinds": [],
        "form": "local_native",
        "resources": {"cpu": "1", "memory": "1Gi"},
    }
    fresh_time = datetime.now(UTC).isoformat()
    linux_node = {
        "node_id": "linux-node",
        "status": "ready",
        "last_heartbeat_at": fresh_time,
        "is_co_located": True,
        "platform": "linux",
        "arch": "x86_64",
        "supported_artifacts": ["local_native"],
        "cpu_cores": 8,
        "memory_bytes": 16 * 1024 * 1024 * 1024,
    }
    res = check_preflight(linux_node, mlx_plugin)
    assert not res["eligible"]
    assert res["reason_code"] == "unsupported_platform"

    # 测试加速器缺失
    generic_plugin = {
        "id": "org.sensoryplex.generic",
        "digest": "sha256:" + "e" * 64,
        "accepts_memory_kinds": [],
        "form": "local_native",
        "resources": {"cpu": "1", "memory": "512Mi"},
    }
    mac_node_no_cuda = {
        "node_id": "mac-node",
        "status": "ready",
        "last_heartbeat_at": fresh_time,
        "is_co_located": True,
        "platform": "macos",
        "arch": "aarch64",
        "supported_artifacts": ["local_native"],
        "accelerators": [{"accelerator": "metal", "state": 1}],
        "cpu_cores": 8,
        "memory_bytes": 16 * 1024 * 1024 * 1024,
    }
    res_cuda = check_preflight(mac_node_no_cuda, generic_plugin, config={"provider": "cuda"})
    assert not res_cuda["eligible"]
    assert res_cuda["reason_code"] == "accelerator_not_available"


def test_preflight_insufficient_resources_and_digest():
    fresh_time = datetime.now(UTC).isoformat()
    node = {
        "node_id": "n1",
        "status": "ready",
        "last_heartbeat_at": fresh_time,
        "is_co_located": True,
        "platform": "linux",
        "arch": "x86_64",
        "supported_artifacts": ["local_native"],
        "cpu_cores": 2,
        "memory_bytes": 1024 * 1024 * 1024,  # 1GiB
    }
    heavy_cpu_plugin = {
        "id": "org.sensoryplex.heavy",
        "digest": "sha256:" + "f" * 64,
        "accepts_memory_kinds": [],
        "form": "local_native",
        "resources": {"cpu": "4", "memory": "512Mi"},
    }
    assert check_preflight(node, heavy_cpu_plugin)["reason_code"] == "insufficient_cpu"

    heavy_mem_plugin = {
        "id": "org.sensoryplex.heavy",
        "digest": "sha256:" + "f" * 64,
        "accepts_memory_kinds": [],
        "form": "local_native",
        "resources": {"cpu": "1", "memory": "4Gi"},
    }
    assert check_preflight(node, heavy_mem_plugin)["reason_code"] == "insufficient_memory"

    bad_digest_plugin = {
        "id": "org.sensoryplex.baddigest",
        "digest": "invalid-digest",
        "accepts_memory_kinds": [],
        "form": "local_native",
        "resources": {"cpu": "1", "memory": "512Mi"},
    }
    assert check_preflight(node, bad_digest_plugin)["reason_code"] == "digest_mismatch"
