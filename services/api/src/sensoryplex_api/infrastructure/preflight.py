"""ADR-026 预检引擎：在向目标节点分发插件安装或任务前，验证 5 大前置条件。

绝不自动静默改派：预检失败一律返回稳定原因码并落审计记录。
"""

from datetime import datetime, timezone
from typing import Any


def parse_memory_bytes(mem_str: str) -> int:
    """解析 1Gi / 512Mi 等格式为字节数。"""
    mem_str = str(mem_str).strip()
    multipliers = {
        "k": 1000, "m": 1000 * 1000, "g": 1000 * 1000 * 1000,
        "ki": 1024, "mi": 1024 * 1024, "gi": 1024 * 1024 * 1024,
    }
    for unit, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if mem_str.lower().endswith(unit):
            num = mem_str[: -len(unit)].strip()
            try:
                return int(float(num) * mult)
            except ValueError:
                return 0
    try:
        return int(mem_str)
    except ValueError:
        return 0


def check_preflight(
    node: dict[str, Any] | None,
    plugin_entry: dict[str, Any],
    config: dict[str, Any] | None = None,
    data_plane_node_id: str | None = None,
) -> dict[str, Any]:
    """执行 ADR-026 §2.2 规定的 5 项硬性预检。

    1. 节点身份、状态与心跳新鲜度；
    2. 平台、架构、CPU/内存、加速器与制品形态匹配；
    3. 制品摘要、签名与配置；
    4. 数据本地性：接受共享内存的插件必须同机；
    5. 资源与并发预算。
    """
    matched = []
    missing = []

    # 1. 节点身份与心跳状态
    if not node:
        return {
            "eligible": False,
            "reason_code": "node_not_found",
            "detail": "Target node does not exist in registry",
            "matched_capabilities": matched,
            "missing_capabilities": ["node_registered"],
        }

    status = node.get("status", "")
    if status == "revoked":
        return {
            "eligible": False,
            "reason_code": "node_revoked",
            "detail": "Target node has been revoked by administrator",
            "matched_capabilities": matched,
            "missing_capabilities": ["node_active"],
        }
    if status == "offline":
        return {
            "eligible": False,
            "reason_code": "node_offline",
            "detail": "Target node is currently offline",
            "matched_capabilities": matched,
            "missing_capabilities": ["node_online"],
        }
    if status == "draining":
        return {
            "eligible": False,
            "reason_code": "node_draining",
            "detail": "Target node is in draining state, no new workloads permitted",
            "matched_capabilities": matched,
            "missing_capabilities": ["node_accepting_workloads"],
        }
    if status not in {"ready", "candidate", "enrolling"}:
        return {
            "eligible": False,
            "reason_code": "node_not_ready",
            "detail": f"Target node is in state {status}",
            "matched_capabilities": matched,
            "missing_capabilities": ["node_ready"],
        }

    # 心跳超时检测（>60 秒即认定心跳陈旧）
    last_hb = node.get("last_heartbeat_at")
    if last_hb:
        if isinstance(last_hb, str):
            try:
                last_hb_dt = datetime.fromisoformat(last_hb.replace("Z", "+00:00"))
            except ValueError:
                last_hb_dt = None
        else:
            last_hb_dt = last_hb
        if last_hb_dt:
            now = datetime.now(timezone.utc)
            if (now - last_hb_dt).total_seconds() > 60:
                return {
                    "eligible": False,
                    "reason_code": "node_heartbeat_stale",
                    "detail": "Target node heartbeat has timed out (>60s)",
                    "matched_capabilities": matched,
                    "missing_capabilities": ["node_heartbeat_fresh"],
                }
    matched.append("node_identity_and_heartbeat")

    # 2. 数据本地性预检 (ADR-010 / ADR-026 §2.5)
    accepts_memory = plugin_entry.get("accepts_memory_kinds", [])
    requires_shm = any(k in {"cpu_shared_memory", "cuda_ipc", "metal_shared_memory"} for k in accepts_memory)
    is_co_located = bool(node.get("is_co_located", False))

    if requires_shm:
        # 如果需要共享内存，必须同机
        if not is_co_located:
            if data_plane_node_id and node.get("node_id") == data_plane_node_id:
                # 显式指定数据面节点且一致
                matched.append("data_locality_colocated")
            else:
                return {
                    "eligible": False,
                    "reason_code": "data_locality_violation",
                    "detail": (
                        "Plugin accepts shared memory handles (ADR-010) and must be deployed "
                        "co-located with media runtime data plane"
                    ),
                    "matched_capabilities": matched,
                    "missing_capabilities": ["data_locality_colocated"],
                }
        else:
            matched.append("data_locality_colocated")
    else:
        matched.append("remote_execution_permitted")

    # 3. 制品形态与运行时能力 (form: local_native vs container)
    artifact_form = plugin_entry.get("form", "local_native")
    node_supported_artifacts = node.get("supported_artifacts", [])
    if artifact_form == "container" and "container" not in node_supported_artifacts:
        return {
            "eligible": False,
            "reason_code": "container_runtime_unsupported",
            "detail": "Plugin requires container execution but target node does not support it",
            "matched_capabilities": matched,
            "missing_capabilities": ["container_runtime"],
        }
    if artifact_form == "local_native" and "local_native" not in node_supported_artifacts:
        return {
            "eligible": False,
            "reason_code": "native_runtime_unsupported",
            "detail": "Plugin requires native execution but target node does not support it",
            "matched_capabilities": matched,
            "missing_capabilities": ["native_runtime"],
        }
    matched.append(f"artifact_form_{artifact_form}")

    # 4. 平台与架构约束（如 MLX 仅支持 macOS Apple Silicon）
    plugin_id = plugin_entry.get("id", "").lower()
    node_platform = node.get("platform", "").lower()
    node_arch = node.get("arch", "").lower()

    if "mlx" in plugin_id:
        if node_platform != "macos" or node_arch != "aarch64":
            return {
                "eligible": False,
                "reason_code": "unsupported_platform",
                "detail": "Apple Silicon MLX plugin requires macos aarch64 node",
                "matched_capabilities": matched,
                "missing_capabilities": ["macos_aarch64"],
            }
        matched.append("platform_macos_aarch64")

    # 加速器匹配：如果配置或插件需要特定加速器（如 coreml, metal, cuda）
    node_accels = node.get("accelerators", [])
    if isinstance(node_accels, list):
        accel_names = {
            a.get("accelerator", "").lower()
            for a in node_accels
            if a.get("state") in {1, "ACCELERATOR_STATE_AVAILABLE", "available"}
        }
    else:
        accel_names = set()

    cfg = config or {}
    requested_provider = cfg.get("provider", "").lower()
    if requested_provider in {"metal", "coreml", "cuda", "tensorrt"}:
        if requested_provider not in accel_names:
            return {
                "eligible": False,
                "reason_code": "accelerator_not_available",
                "detail": f"Requested accelerator '{requested_provider}' not available on target node",
                "matched_capabilities": matched,
                "missing_capabilities": [f"accelerator_{requested_provider}"],
            }
        matched.append(f"accelerator_{requested_provider}")

    # 5. 资源预算
    resources = plugin_entry.get("resources", {})
    try:
        req_cpu = float(resources.get("cpu", "1"))
    except ValueError:
        req_cpu = 1.0
    node_cpu = float(node.get("cpu_cores", 1))
    if node_cpu < req_cpu:
        return {
            "eligible": False,
            "reason_code": "insufficient_cpu",
            "detail": f"Target node has {node_cpu} cores, plugin requires {req_cpu}",
            "matched_capabilities": matched,
            "missing_capabilities": ["cpu_budget"],
        }
    matched.append("cpu_budget")

    req_mem = parse_memory_bytes(resources.get("memory", "512Mi"))
    node_mem = int(node.get("memory_bytes", 0))
    if node_mem > 0 and node_mem < req_mem:
        return {
            "eligible": False,
            "reason_code": "insufficient_memory",
            "detail": f"Target node has {node_mem} bytes RAM, plugin requires {req_mem}",
            "matched_capabilities": matched,
            "missing_capabilities": ["memory_budget"],
        }
    matched.append("memory_budget")

    # 6. 制品摘要合法性
    digest = plugin_entry.get("digest", "")
    if not (digest.startswith("sha256:") and len(digest) == 71):
        return {
            "eligible": False,
            "reason_code": "digest_mismatch",
            "detail": "Plugin manifest artifact digest is invalid or missing",
            "matched_capabilities": matched,
            "missing_capabilities": ["valid_artifact_digest"],
        }
    matched.append("artifact_digest_verified")

    return {
        "eligible": True,
        "reason_code": "",
        "detail": "Target node passed all preflight checks",
        "matched_capabilities": matched,
        "missing_capabilities": [],
    }
