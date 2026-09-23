"""M8 BGE 链路验收：真实上游 OCR 观测 → 本机 BGE（ONNX Runtime）→ 版本化维度的归一化向量。

这条链路与 M8 的其他插件不同：它**不接数据面**。文本向量插件的输入是上游事实
（`payload.blocks[].text`），不是字节。因此验收除了看向量对不对，还要把"没有字节、
也没有 lease"这件事也验出来——账目里写 0 和"忘了对账"必须能被区分。

角色：
1. 本脚本（编排 + 对账）；
2. 上游 OCR 链路（`tools/verify_ocr.py`：Runtime replay + OCR 插件进程 + worker），
   产出**真实**的 `ocr_blocks` 观测；也可用 `--input-observations` 复用一份既有报告；
3. `python -m edge_material_plugin_embed_bge_onnx`（插件进程，Start 时真建会话）；
4. `tools/ai_worker.py --input-observations`（worker：原样转发上游事实，不碰数据面）。

判定标准（全部为真实执行结果）：

- manifest 的 `artifacts.digest` 与本机复算的插件包摘要一致（拒绝占位串）；
- `Describe` 声明消费 `observation.ocr_blocks`、产出 `observation.text_embedding`，且
  `memory_kinds` 为空：本插件不读任何字节；
- 维度是身份的一部分：`dimension` 等于本机独立读出的 `config.json.hidden_size`，也等于
  实际向量长度；`dimension_source` 写明它从哪来；
- `pooling=cls`、`normalize=l2` 写进结果，`norm≈1`，`vector_sha256` 可由向量独立复算；
- `content_hash` 等于**实际被编码的那段文本**的摘要：本脚本按同一条规则从上游块重拼一遍，
  而不是相信插件自己的说法；
- 锚点、`quality_state`、`timing_source` 继承上游观测，本插件不重新计时；
- 模型身份来自三份**实际加载的文件**字节（编码器 + 分词器 + 模型结构配置），可由本机复算；
- 相似度不是校准置信度：`confidence` 缺省且写明原因；
- 负路径在活进程上验：喂 buffer 得到 `buffer_reader_not_attached`（不退化成"读本地文件"），
  modality 不是 `ocr_blocks` 得到 `unsupported_input_modality:*`，一段文字都没有得到
  `input_text_empty`（不给"没有内容"编造语义）；
- 账目：本链路没有数据面，`drain.leases == 0`、`runtime_stats_after.leases == 0`，且报告里
  没有数据面统计（`runtime_stats` 缺省）；
- 不外泄：报告与插件输出里没有媒体名/路径/推流地址；payload 里塞不进整份词表。
"""

import argparse
import hashlib
import importlib
import json
import math
import os
import pathlib
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins/python/common/src"))

import grpc  # noqa: E402
from edge_material_sdk.generated.common.v1 import common_pb2 as common  # noqa: E402
from edge_material_sdk.generated.material.v1 import material_pb2 as material  # noqa: E402
from edge_material_sdk.generated.runtime.v1 import (  # noqa: E402
    runtime_pb2,
    runtime_pb2_grpc,
)
from google.protobuf import json_format, struct_pb2  # noqa: E402

from tools import verify_ocr  # noqa: E402
from tools.verify_model import Process, check, free_port  # noqa: E402
from tools.verify_ocr import combined_digest, sha256_file  # noqa: E402

WORKER = ROOT / "tools/ai_worker.py"
PLUGIN_DIR = ROOT / "plugins/python/processors/embed-bge-onnx"
PLUGIN_MANIFEST = PLUGIN_DIR / "plugin.yaml"
PLUGIN_MODULE = "edge_material_plugin_embed_bge_onnx"

# 能力串是契约里的名字；输入侧是上游事实的 modality，不是 buffer 的 kind。
CONSUMES = "observation.ocr_blocks"
PRODUCES = "observation.text_embedding"
MODALITY = "text_embedding"
INPUT_MODALITY = "ocr_blocks"
POOLING = "cls"
NORMALIZE = "l2"
DIMENSION_SOURCE = "config.json:hidden_size+probe_forward"
JOIN_SEPARATOR = "\n"
# 与插件同口径的输入边界（本脚本按契约重拼文本时要一起用）。
MAX_TEXTS = 256
# HF 快照把 ONNX 放在 onnx/ 子目录：`model_file` 是 model_dir 内的相对路径。
DEFAULT_MODEL_FILE = "onnx/model_quantized.onnx"
# 三份文件的角色：身份是它们的字节摘要的组合，不是配置里写的版本号。
MODEL_FILES = {
    "encoder": DEFAULT_MODEL_FILE,
    "tokenizer": "tokenizer.json",
    "model_config": "config.json",
}
# 本机预置的权重位置（Xenova/bge-small-zh-v1.5 的某个 revision 快照）；可用 --model-dir 覆盖。
DEFAULT_MODEL_DIR = (
    pathlib.Path.home()
    / ".cache/huggingface/hub/models--Xenova--bge-small-zh-v1.5"
    / "snapshots/75c43b069aac4d136ba6bc1122f995fedcfd2781"
)
DEFAULT_MODEL_ID = "bge-small-zh-v1.5"
PROVIDER_EP = {"cpu": "CPUExecutionProvider", "coreml": "CoreMLExecutionProvider"}
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
# 512 个 float32 的 JSON 表示约 12 KB；给足余量也远小于一份分词器词表（约 1.3 MB）。
MAX_PAYLOAD_JSON_CHARS = 131_072
NORM_TOLERANCE = 1e-4
MAX_LENGTH = 512
WAIT_TIMEOUT_S = 120.0
READY_TIMEOUT_S = 300.0
RUN_TIMEOUT_S = 1_800.0
SERVER_TTL_MS = 30_000
DEFAULT_INPUTS = 2


def package_digest() -> str:
    """本机复算插件包摘要：manifest 里的值必须与它一致，否则就是占位串。"""
    module = importlib.import_module(f"{PLUGIN_MODULE}.artifact")
    return str(module.package_digest(PLUGIN_DIR))


def as_struct(config: dict) -> struct_pb2.Struct:
    """Start(config) 是 protobuf `Struct`：与 worker 用同一条路径构造，不手拼。"""
    return json_format.ParseDict(config, struct_pb2.Struct())


def manifest_digest() -> str:
    for line in PLUGIN_MANIFEST.read_text().splitlines():
        if line.strip().startswith("digest:"):
            return line.split("digest:", 1)[1].strip()
    raise SystemExit("plugin.yaml has no artifacts.digest line")


def plugin_identity() -> tuple[str, str]:
    """插件名与版本从插件包本身读，避免验收脚本里再抄一遍。"""
    module = importlib.import_module(f"{PLUGIN_MODULE}.plugin")
    return str(module.PLUGIN_NAME), str(module.PLUGIN_VERSION)


def real_weights(model_dir: str, model_file: str):
    """独立复算权重身份：不调用插件代码，避免"自己验证自己"。"""
    directory = pathlib.Path(model_dir).expanduser()
    if not directory.is_dir():
        raise SystemExit(f"model dir not found: {directory}")
    files = dict(MODEL_FILES)
    files["encoder"] = model_file
    digests: dict[str, str] = {}
    sizes: dict[str, int] = {}
    for role, name in files.items():
        path = directory / name
        if not path.is_file():
            raise SystemExit(f"weight file missing: {path}")
        digests[role] = sha256_file(path)
        sizes[role] = path.stat().st_size
    document = json.loads((directory / "config.json").read_text())
    dimension = document.get("hidden_size")
    if not isinstance(dimension, int) or dimension <= 0:
        raise SystemExit("model config has no usable hidden_size")
    return directory, digests, sizes, combined_digest(digests), dimension


def vector_sha256(vector: list[float]) -> str:
    """向量摘要按 float32 小端字节算：与 JSON 里的十进制表示无关，可独立复算。"""
    return "sha256:" + hashlib.sha256(struct.pack(f"<{len(vector)}f", *vector)).hexdigest()


def recompute_text(payload: dict) -> tuple[str, int, int]:
    """按契约从上游块重拼一遍文本：规则与插件相同，但由本脚本独立执行。"""
    blocks = payload.get("blocks", [])
    pieces: list[str] = []
    blank = 0
    for block in blocks:
        value = block.get("text")
        if not isinstance(value, str):
            raise SystemExit("upstream block has no text")
        stripped = value.strip()
        if stripped:
            pieces.append(stripped)
        else:
            blank += 1
    return JOIN_SEPARATOR.join(pieces), len(blocks), blank


def fetch_upstream_observations(
    input_observations: str | None,
    media: pathlib.Path,
    ocr_model_dir: str,
    max_inputs: int,
    workspace: pathlib.Path,
    failures: list[str],
) -> list[dict]:
    """拿到上游 `ocr_blocks` 观测：既可以用真实 OCR 链路现跑，也可以复用既有报告。"""
    if input_observations:
        source = pathlib.Path(input_observations)
        try:
            document = json.loads(source.read_text())
        except (OSError, json.JSONDecodeError):
            failures.append(f"input observations unreadable: {source}")
            return []
        observations = document.get("observations") if isinstance(document, dict) else document
        if not isinstance(observations, list) or not observations:
            failures.append(f"input observations are empty: {source}")
            return []
        print(f"upstream: reused report {source} observations={len(observations)}")
        return observations

    ocr_workspace = workspace / "ocr"
    ocr_workspace.mkdir(parents=True, exist_ok=True)
    print(f"upstream: running the real OCR chain into {ocr_workspace}")
    ocr_failures = verify_ocr.verify(
        media,
        ocr_workspace,
        ocr_model_dir,
        "cpu",
        max_inputs,
        "text",
    )
    if ocr_failures:
        # 上游不成立时下游的向量没有意义：如实报告上游失败，而不是继续跑。
        for failure in ocr_failures:
            failures.append(f"upstream OCR chain failed: {failure}")
        return []
    ocr_report = json.loads((ocr_workspace / "ai-worker.json").read_text())
    observations = ocr_report.get("observations", [])
    print(
        f"upstream: OCR chain observations={len(observations)} "
        f"released={ocr_report.get('runtime_stats_after', {}).get('released_total')}"
    )
    return observations


def verify_payload(
    observation: dict,
    upstream: dict,
    expected_digest: str,
    model_digests: dict[str, str],
    model_sizes: dict[str, int],
    model_digest: str,
    dimension: int,
    provider: str,
    failures: list[str],
) -> None:
    label = observation.get("observationId", "<missing>")
    payload = observation.get("payload", {})
    check(
        len(json.dumps(payload)) <= MAX_PAYLOAD_JSON_CHARS,
        f"{label}: payload is far larger than a vector of this dimension could be",
        failures,
    )

    # 文本与向量：先独立复算，再和插件写出来的对。
    text, block_count, blank_blocks = recompute_text(upstream.get("payload", {}))
    text_digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    check(
        observation.get("contentHash") == text_digest,
        f"{label}: content_hash is not the digest of the text that was encoded",
        failures,
    )
    check(payload.get("text") == text, f"{label}: payload text is not the upstream text", failures)
    check(
        payload.get("text_sha256") == text_digest,
        f"{label}: payload text_sha256 is not the recomputed digest",
        failures,
    )
    check(
        payload.get("join_separator") == JOIN_SEPARATOR,
        f"{label}: the block join separator is not declared as used",
        failures,
    )
    check(
        payload.get("source_modality") == INPUT_MODALITY,
        f"{label}: payload does not record the upstream modality: {payload.get('source_modality')}",
        failures,
    )
    check(
        payload.get("block_count") == block_count,
        f"{label}: block_count {payload.get('block_count')} != upstream {block_count}",
        failures,
    )
    check(
        payload.get("blank_blocks") == blank_blocks,
        f"{label}: blank_blocks {payload.get('blank_blocks')} != upstream {blank_blocks}",
        failures,
    )
    check(
        payload.get("char_count") == len(text),
        f"{label}: char_count does not match the encoded text length",
        failures,
    )
    check(bool(text), f"{label}: an embedding was produced from an empty text", failures)
    check(
        block_count <= MAX_TEXTS,
        f"{label}: more upstream blocks than the declared input bound",
        failures,
    )

    vector = payload.get("vector")
    check(
        isinstance(vector, list) and len(vector) == dimension,
        f"{label}: vector length {len(vector) if isinstance(vector, list) else vector} "
        f"!= dimension {dimension}",
        failures,
    )
    check(
        payload.get("dimension") == dimension,
        f"{label}: declared dimension {payload.get('dimension')} != config.json {dimension}",
        failures,
    )
    check(
        payload.get("dimension_source") == DIMENSION_SOURCE,
        f"{label}: dimension provenance is missing: {payload.get('dimension_source')}",
        failures,
    )
    check(
        payload.get("pooling") == POOLING,
        f"{label}: pooling method is not declared as used: {payload.get('pooling')}",
        failures,
    )
    check(
        payload.get("normalize") == NORMALIZE,
        f"{label}: normalization is not declared as used: {payload.get('normalize')}",
        failures,
    )
    if isinstance(vector, list) and vector:
        norm = math.sqrt(sum(float(value) ** 2 for value in vector))
        check(
            abs(norm - 1.0) <= NORM_TOLERANCE,
            f"{label}: the vector is not L2-normalized (norm={norm})",
            failures,
        )
        check(
            abs(float(payload.get("norm", 0.0)) - norm) <= NORM_TOLERANCE,
            f"{label}: reported norm {payload.get('norm')} != recomputed {norm}",
            failures,
        )
        check(
            payload.get("vector_sha256") == vector_sha256([float(value) for value in vector]),
            f"{label}: vector_sha256 cannot be recomputed from the vector",
            failures,
        )
        check(
            all(math.isfinite(float(value)) for value in vector),
            f"{label}: the vector contains non-finite values",
            failures,
        )
    token_count = payload.get("token_count")
    check(
        # payload 是 `Struct`：所有数字在 JSON 里都是 double，因此比"整数性"而不是类型。
        isinstance(token_count, (int, float))
        and float(token_count).is_integer()
        and 0 < token_count <= MAX_LENGTH,
        f"{label}: token_count is missing or outside the model bound: {token_count}",
        failures,
    )

    # 本切片没有向量库：这件事必须写清楚，不能让读者以为已经落库。
    check(
        payload.get("storage") == "inline_payload",
        f"{label}: storage is not declared: {payload.get('storage')}",
        failures,
    )
    check(
        "vector_ref" in payload and payload.get("vector_ref") is None,
        f"{label}: vector_ref must be explicitly absent in this slice: {payload.get('vector_ref')}",
        failures,
    )
    slug = "".join(
        character if character.isalnum() else "_"
        for character in str(payload.get("engine", {}).get("model_id", ""))
    ).strip("_")
    check(
        payload.get("vector_index_key") == f"material_text_{slug}_d{dimension}_v1",
        f"{label}: vector_index_key does not carry model and dimension: "
        f"{payload.get('vector_index_key')}",
        failures,
    )
    check(
        payload.get("embedding_id") == observation.get("observationId"),
        f"{label}: embedding_id is not the observation id",
        failures,
    )

    # 上游身份另写 input.*：向量自己的 content_hash 与上游摘要是两件事，不能混。
    source = payload.get("input", {})
    check(
        source.get("observation_id") == upstream.get("observationId"),
        f"{label}: input.observation_id does not point at the upstream observation",
        failures,
    )
    check(
        source.get("modality") == INPUT_MODALITY,
        f"{label}: input.modality is not the upstream modality: {source.get('modality')}",
        failures,
    )
    check(
        source.get("content_hash") == upstream.get("contentHash"),
        f"{label}: input.content_hash does not match the upstream observation",
        failures,
    )
    check(
        source.get("stream_id") == upstream.get("streamId"),
        f"{label}: input.stream_id does not match the upstream observation",
        failures,
    )
    check(
        source.get("source_item_id") == upstream.get("sourceItemId"),
        f"{label}: input.source_item_id does not match the upstream observation",
        failures,
    )
    check(
        source.get("quality_state") == upstream.get("qualityState"),
        f"{label}: input.quality_state does not inherit the upstream state",
        failures,
    )
    # payload 是 `Struct`（键名原样保留 snake_case），上游观测是真实 proto 字段（camelCase）。
    source_range = source.get("time_range", {})
    upstream_range = upstream.get("timeRange", {})
    check(
        int(source_range.get("start_ms", -1)) == int(upstream_range.get("startMs", -2))
        and int(source_range.get("end_ms", -1)) == int(upstream_range.get("endMs", -2)),
        f"{label}: input.time_range does not inherit the upstream anchor: "
        f"{source_range} != {upstream_range}",
        failures,
    )
    check(
        bool(source.get("model_release_id")),
        f"{label}: the upstream model release is not recorded",
        failures,
    )

    engine = payload.get("engine", {})
    check(bool(engine.get("model_id")), f"{label}: engine.model_id is empty", failures)
    check(
        engine.get("model_source") == "local_dir",
        f"{label}: engine.model_source is not a real provenance: {engine.get('model_source')}",
        failures,
    )
    check(
        bool(engine.get("runtime_version")) and engine.get("runtime_version") != "unknown",
        f"{label}: the ONNX Runtime version could not be read: {engine.get('runtime_version')}",
        failures,
    )
    check(
        engine.get("max_length") == MAX_LENGTH,
        f"{label}: engine.max_length is not the configured truncation bound",
        failures,
    )
    check(
        isinstance(engine.get("duration_ms"), (int, float)) and engine.get("duration_ms") >= 0,
        f"{label}: the encode duration was not measured: {engine.get('duration_ms')}",
        failures,
    )
    weights = engine.get("weights", [])
    roles = sorted(str(item.get("role")) for item in weights)
    check(
        roles == sorted(MODEL_FILES),
        f"{label}: weights do not cover encoder/tokenizer/model_config: {roles}",
        failures,
    )
    for item in weights:
        role = str(item.get("role"))
        check(
            item.get("sha256") == model_digests.get(role),
            f"{label}: weight {role} digest does not match the real file bytes",
            failures,
        )
        check(
            item.get("bytes") == model_sizes.get(role),
            f"{label}: weight {role} size does not match the real file",
            failures,
        )
    providers = engine.get("providers", [])
    check(
        bool(providers) and providers[0] == PROVIDER_EP[provider],
        f"{label}: the session did not run on the requested provider "
        f"{PROVIDER_EP[provider]}: {providers}",
        failures,
    )

    provenance = observation.get("provenance", {})
    name, version = plugin_identity()
    for field, expected in (
        ("plugin", name),
        ("pluginVersion", version),
        ("artifactDigest", expected_digest),
        ("modelArtifactDigest", model_digest),
        ("modelId", str(engine.get("model_id"))),
    ):
        check(
            provenance.get(field) == expected,
            f"{label}: provenance.{field} is {provenance.get(field)!r}, expected {expected!r}",
            failures,
        )
    expected_release = f"bge:{DEFAULT_MODEL_ID}@{model_digest.removeprefix('sha256:')[:12]}"
    check(
        provenance.get("modelReleaseId") == expected_release,
        f"{label}: model release id is not derived from the file digests: "
        f"{provenance.get('modelReleaseId')} != {expected_release}",
        failures,
    )
    check(
        provenance.get("executionBackend")
        == f"onnxruntime-{engine.get('runtime_version')}/{PROVIDER_EP[provider]}",
        f"{label}: execution backend does not name runtime and provider: "
        f"{provenance.get('executionBackend')}",
        failures,
    )
    for field in ("artifactDigest", "modelArtifactDigest", "configHash"):
        check(
            bool(DIGEST_PATTERN.match(str(provenance.get(field, "")))),
            f"{label}: provenance.{field} is not a sha256 digest",
            failures,
        )


def verify_observation(
    observation: dict, frame: dict, upstream: dict, failures: list[str], **expected
) -> None:
    label = observation.get("observationId", "<missing>")
    check(
        observation.get("modality") == MODALITY,
        f"{label}: unexpected modality {observation.get('modality')}",
        failures,
    )
    check(
        observation.get("streamId") == upstream.get("streamId"),
        f"{label}: stream id drifted from the upstream observation",
        failures,
    )
    check(
        observation.get("sourceId") == upstream.get("sourceId"),
        f"{label}: source id drifted from the upstream observation",
        failures,
    )
    check(
        observation.get("sourceItemId") == upstream.get("sourceItemId"),
        f"{label}: source item id drifted from the upstream observation",
        failures,
    )
    # 锚点继承上游半开区间：编码耗时不参与时间换算。
    check(
        observation.get("timeRange") == upstream.get("timeRange"),
        f"{label}: anchor drifted from the upstream observation: "
        f"{observation.get('timeRange')} != {upstream.get('timeRange')}",
        failures,
    )
    check(
        observation.get("timingSource") == upstream.get("timingSource"),
        f"{label}: timing source does not inherit the upstream observation",
        failures,
    )
    check(
        observation.get("qualityState") == upstream.get("qualityState"),
        f"{label}: quality state does not inherit the upstream observation",
        failures,
    )
    check(
        not observation.get("confidence") and observation.get("confidenceUnavailableReason"),
        f"{label}: confidence must be explicitly unavailable with a reason, not filled in",
        failures,
    )
    check(
        int(observation.get("createdAtUnixMs", 0)) > 0,
        f"{label}: the observation has no creation time",
        failures,
    )
    check(
        frame.get("source_digest") == upstream.get("contentHash"),
        f"{label}: the worker did not pass the upstream observation through",
        failures,
    )
    verify_payload(observation, upstream, failures=failures, **expected)


def verify_rejections(stub, upstream: dict, version: str, failures: list[str]) -> None:
    """负路径必须在**活进程**上验：拒绝发生在这里，而不是只发生在单元测试里。"""
    context = common.RequestContext(
        request_id="req_bge_probe",
        trace_id="trace_bge_probe",
        pipeline_run_id="run_bge_probe",
        stream_id=str(upstream.get("streamId", "")),
        source_id=str(upstream.get("sourceId", "")),
        deadline_unix_ms=int(time.time() * 1000) + 30_000,
        attempt=1,
        idempotency_key="bge_probe_no_bytes",
        privacy_policy=common.PrivacyPolicy(data_egress="local_only"),
    )
    # 空串的摘要是一个真实摘要；这里不涉及任何字节，descriptor 会在读 lease 之前被拒。
    empty_digest = "sha256:" + hashlib.sha256(b"").hexdigest()
    buffer_request = runtime_pb2.ProcessRequest(
        context=context,
        inputs=[
            runtime_pb2.PluginInput(
                buffer=common.BufferDescriptor(
                    buffer_id="probe_no_reader",
                    kind="video_frame",
                    memory_kind="cpu_shared_memory",
                    locator=common.BufferLocator(offset=0, length=0),
                    stream_id=str(upstream.get("streamId", "")),
                    time_range=common.TimeRange(start_ms=0, end_ms=1),
                    content_hash=empty_digest,
                )
            )
        ],
        processor_release_id=version,
    )
    response = stub.Process(buffer_request, timeout=WAIT_TIMEOUT_S)
    check(
        response.HasField("error") and response.error.reason_code == "buffer_reader_not_attached",
        "a buffer input was not rejected with buffer_reader_not_attached: "
        f"{response.error.reason_code if response.HasField('error') else 'no error'}",
        failures,
    )

    mismatched = material.Observation()
    json_format.ParseDict({**upstream, "modality": "video_frame"}, mismatched)
    modality_request = runtime_pb2.ProcessRequest(
        context=context,
        inputs=[runtime_pb2.PluginInput(observation=mismatched)],
        processor_release_id=version,
    )
    response = stub.Process(modality_request, timeout=WAIT_TIMEOUT_S)
    check(
        response.HasField("error")
        and response.error.reason_code == "unsupported_input_modality:video_frame",
        "an upstream observation of the wrong modality was not rejected: "
        f"{response.error.reason_code if response.HasField('error') else 'no error'}",
        failures,
    )

    empty = material.Observation()
    json_format.ParseDict(
        {**upstream, "payload": {"blocks": []}, "observationId": "obs_probe_empty"}, empty
    )
    empty_request = runtime_pb2.ProcessRequest(
        context=context,
        inputs=[runtime_pb2.PluginInput(observation=empty)],
        processor_release_id=version,
    )
    response = stub.Process(empty_request, timeout=WAIT_TIMEOUT_S)
    check(
        response.HasField("error") and response.error.reason_code == "input_text_empty",
        "an input with no text was not rejected with input_text_empty: "
        f"{response.error.reason_code if response.HasField('error') else 'no error'}",
        failures,
    )


def verify_no_leakage(
    report: dict, media: pathlib.Path, plugin_process: Process, failures: list[str]
) -> None:
    blob = json.dumps(report)
    for label, needle in (
        ("media file name", media.name),
        ("media absolute path", str(media)),
        ("streaming uri", "srt://"),
        ("streaming uri", "rtmp://"),
        # 词表不该被塞进 payload：它是模型文件的内容，不是这条观测的内容。
        ("tokenizer vocabulary", '"vocab"'),
    ):
        check(needle not in blob, f"worker report leaks {label}", failures)
    plugin_output = "\n".join(plugin_process.lines + plugin_process.stderr)
    for label, needle in (("media file name", media.name), ("media path", str(media))):
        check(needle not in plugin_output, f"plugin output leaks {label}", failures)


def verify(
    media: pathlib.Path,
    workspace: pathlib.Path,
    model_dir: str,
    model_file: str,
    provider: str,
    max_inputs: int,
    input_observations: str | None,
    ocr_model_dir: str,
) -> list[str]:
    failures: list[str] = []
    expected_digest = package_digest()
    declared = manifest_digest()
    check(
        declared == expected_digest and bool(DIGEST_PATTERN.match(declared)),
        f"manifest artifact digest is not the real package digest: {declared} != {expected_digest}",
        failures,
    )
    directory, model_digests, model_sizes, model_digest, dimension = real_weights(
        model_dir, model_file
    )
    name, version = plugin_identity()
    print(f"weights: {directory}")
    for role in sorted(model_digests):
        print(f"  {role}: {model_digests[role]} ({model_sizes[role]} bytes)")
    print(f"  combined: {model_digest} dimension: {dimension}")

    upstream = fetch_upstream_observations(
        input_observations, media, ocr_model_dir, max_inputs, workspace, failures
    )
    if not upstream:
        failures.append("no upstream ocr_blocks observation to embed")
        return failures
    selected = upstream[:max_inputs]
    by_id = {str(item.get("observationId")): item for item in selected}
    for item in selected:
        check(
            item.get("modality") == INPUT_MODALITY,
            f"upstream observation is not {INPUT_MODALITY}: {item.get('modality')}",
            failures,
        )

    plugin_address = f"127.0.0.1:{free_port()}"
    worker_report = workspace / "ai-worker.json"
    upstream_report_path = workspace / "upstream-observations.json"
    # worker 接受"含 observations 键的报告"：把选中的上游观测原样交给它，不做加工。
    upstream_report_path.write_text(
        json.dumps({"observations": selected}, indent=2, sort_keys=True) + "\n"
    )
    plugin_config = workspace / "plugin-config.json"
    config_document = {
        "provider": provider,
        "model_dir": str(directory),
        "model_file": model_file,
        "model_id": DEFAULT_MODEL_ID,
        "max_length": MAX_LENGTH,
        "ttl_ms": SERVER_TTL_MS,
        "timeout_s": RUN_TIMEOUT_S,
    }
    plugin_config.write_text(json.dumps(config_document, indent=2) + "\n")

    plugin = Process(
        "plugin",
        [
            sys.executable,
            "-m",
            PLUGIN_MODULE,
            "--port",
            plugin_address.rsplit(":", 1)[1],
            "--expect-digest",
            expected_digest,
        ],
    )
    plugin.start()
    report: dict = {}
    try:
        started = plugin.wait_for("plugin ready", timeout=READY_TIMEOUT_S)
        check(
            started.get("artifact_digest") == expected_digest,
            f"plugin started with a different artifact digest: {started}",
            failures,
        )

        completed = subprocess.run(
            [
                sys.executable,
                str(WORKER),
                "--plugin",
                plugin_address,
                "--input-observations",
                str(upstream_report_path),
                "--plugin-config",
                str(plugin_config),
                "--max-inputs",
                str(max_inputs),
                "--report",
                str(worker_report),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=RUN_TIMEOUT_S,
        )
        check(
            completed.returncode == 0,
            f"ai worker failed ({completed.returncode}): {completed.stderr[-2000:]}",
            failures,
        )
        report = json.loads(worker_report.read_text()) if worker_report.is_file() else {}
        check(
            report.get("checks_failed") == 0, f"worker failures: {report.get('failures')}", failures
        )
        check(
            report.get("input_mode") == "observation",
            f"the worker did not take the observation path: {report.get('input_mode')}",
            failures,
        )
        check(report.get("config_valid") is True, "plugin rejected the worker's config", failures)
        check(report.get("start_state") == "ready", f"plugin not ready: {report}", failures)

        description = report.get("plugin_description", {})
        check(
            CONSUMES in description.get("consumes", []),
            f"plugin does not declare it consumes upstream OCR facts: {description}",
            failures,
        )
        check(
            PRODUCES in description.get("produces", []),
            f"plugin does not declare the modality it produces: {description}",
            failures,
        )
        check(
            description.get("memory_kinds") == [],
            f"a text embedding plugin must not claim a memory kind: {description}",
            failures,
        )

        observations = report.get("observations", [])
        frames = report.get("frames", [])
        available = int(report.get("input_buffers_available", 0))
        check(
            available == len(selected),
            f"worker did not see the upstream observations: {available} != {len(selected)}",
            failures,
        )
        check(
            len(observations) == len(selected),
            f"observation count does not match the inputs: {len(observations)} != {len(selected)}",
            failures,
        )
        check(
            len(frames) == len(observations),
            f"processed input count does not match observations: {frames}",
            failures,
        )
        ids = [item.get("observationId") for item in observations]
        check(len(set(ids)) == len(ids), f"observation ids are not unique: {ids}", failures)

        for observation in observations:
            frame = next(
                (
                    item
                    for item in frames
                    if item.get("observation_id") == observation.get("observationId")
                ),
                None,
            )
            if frame is None:
                failures.append(f"{observation.get('observationId')}: no matching input ledger")
                continue
            upstream_item = by_id.get(str(frame.get("input_observation_id")))
            if upstream_item is None:
                failures.append(
                    f"{observation.get('observationId')}: the ledger does not name the upstream "
                    f"observation: {frame.get('input_observation_id')}"
                )
                continue
            check(
                frame.get("kind") == INPUT_MODALITY,
                f"worker recorded a different input kind: {frame.get('kind')}",
                failures,
            )
            check(
                "error" not in frame,
                f"{observation.get('observationId')}: the plugin returned an error "
                f"{frame.get('error')}",
                failures,
            )
            verify_observation(
                observation,
                frame,
                upstream_item,
                failures,
                expected_digest=expected_digest,
                model_digests=model_digests,
                model_sizes=model_sizes,
                model_digest=model_digest,
                dimension=dimension,
                provider=provider,
            )

        # 这条路径没有数据面：账目必须显式写 0，而且不该出现数据面统计。
        check(
            "runtime_stats" not in report,
            "the observation path must not report data plane stats",
            failures,
        )
        check(
            report.get("data_plane") is None,
            f"the observation path must not hold a data plane: {report.get('data_plane')}",
            failures,
        )
        drain = report.get("drain", {})
        check(
            drain == {"discarded": 0, "failures": [], "leases": 0},
            f"the observation path has no lease to return: {drain}",
            failures,
        )
        check(
            report.get("runtime_stats_after", {}).get("leases") == 0,
            f"the observation path must report zero leases: {report.get('runtime_stats_after')}",
            failures,
        )
        check(
            report.get("worker_pid") != os.getpid(), "the worker must be its own process", failures
        )
        check(
            report.get("stop_state") == "stopped",
            f"the plugin was not stopped cleanly: {report.get('stop_state')}",
            failures,
        )
        verify_no_leakage(report, media, plugin, failures)

        # 负路径必须在活进程上验：worker 收尾时已经 Stop 过，这里按契约重新 Start
        # （状态 created/failed/stopped 都允许重入），再对同一个进程发不该接受的输入。
        channel = grpc.insecure_channel(plugin_address)
        stub = runtime_pb2_grpc.ProcessorPluginServiceStub(channel)
        restart = stub.Start(
            runtime_pb2.StartRequest(config=as_struct(config_document)), timeout=READY_TIMEOUT_S
        )
        check(
            restart.state == "ready",
            f"the plugin could not be restarted for the negative-path probes: {restart}",
            failures,
        )
        verify_rejections(stub, by_id[str(selected[0].get("observationId"))], version, failures)
        stub.Stop(runtime_pb2.StopRequest(), timeout=WAIT_TIMEOUT_S)
        channel.close()
    finally:
        plugin.kill()
        if plugin.process is not None:
            plugin.finish(timeout=30)

    check(
        str(media) not in "\n".join(plugin.lines + plugin.stderr),
        "plugin output leaks the media path",
        failures,
    )
    print(
        f"embedded_inputs={len(selected)} observations={len(report.get('observations', []))} "
        f"dimension={dimension} provider={provider} backend={PROVIDER_EP[provider]}"
    )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--media", type=pathlib.Path, required=True)
    parser.add_argument("--model-dir", default=str(DEFAULT_MODEL_DIR))
    parser.add_argument(
        "--model-file",
        default=DEFAULT_MODEL_FILE,
        help="model_dir 内实际加载的 ONNX 相对路径（默认 HF 快照的 onnx/model_quantized.onnx）",
    )
    parser.add_argument("--ocr-model-dir", default="")
    parser.add_argument("--provider", choices=sorted(PROVIDER_EP), default="cpu")
    parser.add_argument("--max-inputs", type=int, default=DEFAULT_INPUTS)
    parser.add_argument(
        "--input-observations",
        default=None,
        help="复用既有的上游 OCR worker 报告（跳过重跑 OCR 链路）",
    )
    parser.add_argument("--keep-workspace", action="store_true")
    arguments = parser.parse_args()
    if not arguments.media.is_file():
        raise SystemExit(f"media not found: {arguments.media}")

    workspace = pathlib.Path(tempfile.mkdtemp(prefix="sensoryplex-embed-"))
    print(f"workspace: {workspace}{' (kept)' if arguments.keep_workspace else ''}")
    print(f"host: {socket.gethostname()} worker_timeout_s={RUN_TIMEOUT_S}")
    started = time.monotonic()
    failures = verify(
        arguments.media,
        workspace,
        arguments.model_dir,
        arguments.model_file,
        arguments.provider,
        arguments.max_inputs,
        arguments.input_observations,
        arguments.ocr_model_dir,
    )
    if failures:
        print("\nembed acceptance FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(
        f"\nembed acceptance: real OCR facts -> local BGE ({arguments.provider}) -> "
        f"versioned-dimension vectors passed in {round(time.monotonic() - started, 1)}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
