"""M8 OCR 链路验收：真实视频帧 → 本机 PP-OCR（ONNX Runtime）→ 带坐标/来源/版本的文字块。

四个角色必须是四个进程，否则"模型链路"只是口号：
1. 本脚本（编排 + 对账）；
2. `sensoryplex-runtime replay --handoff-listen`（生产者，持有字节与保留表）；
3. `python -m edge_material_plugin_ocr_rapidocr`（插件进程，自己按 lease 读字节、跑模型）；
4. `tools/ai_worker.py`（worker：只发现与调用，不读字节）。

判定标准（全部为真实执行结果）：

- manifest 里的 `artifacts.digest` 与本机复算的插件包摘要一致（拒绝占位串）；
- 插件 `Describe` 声明消费 `media.video_frame`，且只声明 `cpu_shared_memory`；
- observation 的时间锚点等于**源帧**的半开区间（不重新计时），`content_hash` 等于该帧的
  lease 窗口摘要（observation 绑定到具体字节）；
- `model_artifact_digest` 与本机独立复算的**三份 ONNX 权重**的组合摘要一致（不是配置里的版本号）；
- 每个块都带帧像素坐标，且坐标落在 `image.width/height` 之内；归一化坐标在 [0, 1]；
- 检测/识别分数原样带出但**不冒充置信度**：`confidence` 必须缺省并写明原因；
- 空结果与失败能分开：这一帧没有文字是 `empty_reason=model_found_no_text`，不是错误；
- 帧字节不外泄：插件 stdout/stderr、worker 报告、observation 里都没有媒体名/路径/像素数据；
- 账目：插件读完即归还 lease，`released + expired + retained == retained_total`、无悬挂 slab。

`--expect empty` 用于**无文字**样本：这时 `blocks=[]` 才是正确结果，
"模型没找到文字"必须与"插件失败"可区分。
"""

import argparse
import hashlib
import importlib
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins/python/common/src"))

from tools.verify_model import Process, check, free_port  # noqa: E402

PIPELINE = ROOT / "config/pipelines/file-material.yaml"
WORKER = ROOT / "tools/ai_worker.py"
PLUGIN_DIR = ROOT / "plugins/python/processors/ocr-rapidocr"
PLUGIN_MANIFEST = PLUGIN_DIR / "plugin.yaml"
PLUGIN_MODULE = "edge_material_plugin_ocr_rapidocr"
INPUT_KIND = "video_frame"
# 能力串是契约里的名字（带 media. 前缀），不是 buffer 的 kind。
CONSUMES = "media.video_frame"
PRODUCES = "observation.ocr_blocks"
PIXEL_FORMAT = "RGBA"
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
HASH_CHUNK_BYTES = 1 << 20
# 三份权重各自的角色与文件名：身份由这三份字节的组合摘要承担。
MODEL_FILES = {
    "det": "PP-OCRv6_det_small.onnx",
    "cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "rec": "PP-OCRv6_rec_small.onnx",
}
PROVIDER_EP = {"cpu": "CPUExecutionProvider", "coreml": "CoreMLExecutionProvider"}
MAX_BLOCKS = 512
MAX_BLOCK_CHARS = 512
# 92 个块的 payload 约 8 KB；给 8 倍余量仍然只允许"这一帧的文字"，不允许塞进别的东西。
MAX_PAYLOAD_JSON_CHARS = 65_536
# 坐标按 3 位小数写出，边界处允许的舍入余量。
BOX_TOLERANCE_PX = 1.0

WAIT_TIMEOUT_MS = 120_000
IDLE_TIMEOUT_MS = 60_000
SERVER_TTL_MS = 30_000
RETAINED_LIMIT = 32
ARENA_BYTES = 256 * 1024 * 1024
# 只解码到够用为止：两帧 OCR 已经能证明链路，不必跑完整片。
MAX_POINTS = 400
READY_TIMEOUT_S = 300.0
RUN_TIMEOUT_S = 1_800.0
DEFAULT_INPUTS = 2


def package_digest() -> str:
    """本机复算插件包摘要：manifest 里的值必须与它一致，否则就是占位串。"""
    module = importlib.import_module(f"{PLUGIN_MODULE}.artifact")
    return str(module.package_digest(PLUGIN_DIR))


def manifest_digest() -> str:
    for line in PLUGIN_MANIFEST.read_text().splitlines():
        if line.strip().startswith("digest:"):
            return line.split("digest:", 1)[1].strip()
    raise SystemExit("plugin.yaml has no artifacts.digest line")


def weights_dir(model_dir: str) -> pathlib.Path:
    """独立定位权重目录：不调用插件代码，避免"自己验证自己"。"""
    if model_dir:
        return pathlib.Path(model_dir).expanduser()
    import rapidocr

    return pathlib.Path(rapidocr.__file__).parent / "models"


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def combined_digest(files: dict[str, str]) -> str:
    """按契约复算组合摘要：角色排序后用 (角色, 单文件摘要) 折叠，顺序无关。"""
    digest = hashlib.sha256()
    for role in sorted(files):
        digest.update(role.encode())
        digest.update(b"\0")
        digest.update(files[role].encode())
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def real_weights(model_dir: str) -> tuple[pathlib.Path, dict[str, str], str]:
    directory = weights_dir(model_dir)
    digests = {}
    for role, name in MODEL_FILES.items():
        path = directory / name
        if not path.is_file():
            raise SystemExit(f"weight file missing: {directory}/{name}")
        digests[role] = sha256_file(path)
    return directory, digests, combined_digest(digests)


def replay_report(path: pathlib.Path):
    from edge_material_sdk.generated.media.v1 import media_pb2

    report = media_pb2.ReplayReport()
    report.ParseFromString(path.read_bytes())
    return report


def verify_decoded_frame_descriptors(report, failures: list[str]) -> None:
    """独立读一次 replay 报告：输入确实是 RGBA 视频帧，不是别的布局。"""
    descriptors = list(report.decoded.evidence_descriptors)
    frames = [item for item in descriptors if item.kind == INPUT_KIND]
    check(bool(frames), "runtime report carried no video frame descriptor", failures)
    check(
        all(item.format.pixel_format == PIXEL_FORMAT for item in frames),
        f"decoded frames are not {PIXEL_FORMAT}: {[i.format.pixel_format for i in frames]}",
        failures,
    )
    check(
        all(item.format.width > 0 and item.format.height > 0 for item in frames),
        "runtime reported a frame with unusable dimensions",
        failures,
    )


def verify_block(block: dict, width: int, height: int, failures: list[str], label: str) -> None:
    text = block.get("text")
    check(isinstance(text, str), f"{label}: block text is not a string", failures)
    check(
        isinstance(text, str) and len(text) <= MAX_BLOCK_CHARS,
        f"{label}: block exceeds the declared character bound",
        failures,
    )
    check(
        block.get("chars") == len(text or ""), f"{label}: chars does not match the text", failures
    )
    score = block.get("score")
    check(
        isinstance(score, float) and 0.0 <= score <= 1.0,
        f"{label}: model score missing or out of range: {score}",
        failures,
    )
    box = block.get("box")
    check(
        isinstance(box, list) and len(box) == 4,
        f"{label}: a text block must carry a four-point box: {box}",
        failures,
    )
    if not isinstance(box, list) or len(box) != 4:
        return
    for point in box:
        check(
            isinstance(point, list) and len(point) == 2,
            f"{label}: box point is not a 2-tuple: {point}",
            failures,
        )
        if not isinstance(point, list) or len(point) != 2:
            continue
        x, y = float(point[0]), float(point[1])
        check(
            -BOX_TOLERANCE_PX <= x <= width + BOX_TOLERANCE_PX,
            f"{label}: box x {x} leaves the frame width {width}",
            failures,
        )
        check(
            -BOX_TOLERANCE_PX <= y <= height + BOX_TOLERANCE_PX,
            f"{label}: box y {y} leaves the frame height {height}",
            failures,
        )
    normalized = block.get("box_normalized")
    check(
        isinstance(normalized, list) and len(normalized) == 4,
        f"{label}: normalized box is missing",
        failures,
    )
    if isinstance(normalized, list) and len(normalized) == 4:
        for point in normalized:
            for value in point:
                check(
                    -0.01 <= float(value) <= 1.01,
                    f"{label}: normalized coordinate {value} is outside [0, 1]",
                    failures,
                )


def verify_observation(
    observation: dict,
    frame: dict,
    expected_digest: str,
    model_digest: str,
    provider: str,
    expect: str,
    failures: list[str],
) -> None:
    label = observation.get("observationId", "<missing>")
    check(
        observation.get("modality") == "ocr_blocks",
        f"{label}: unexpected modality {observation.get('modality')}",
        failures,
    )
    time_range = observation.get("timeRange", {})
    start = int(time_range.get("startMs", 0))
    end = int(time_range.get("endMs", 0))
    source = frame.get("source_time_range_ms")
    check(
        [start, end] == source,
        f"{label}: anchor drifted from the source frame: {[start, end]} != {source}",
        failures,
    )
    check(
        observation.get("contentHash") == frame.get("source_digest"),
        f"{label}: observation is not bound to the source bytes it was derived from",
        failures,
    )
    check(
        not observation.get("confidence") and observation.get("confidenceUnavailableReason"),
        f"{label}: confidence must be explicitly unavailable with a reason, not filled in",
        failures,
    )
    check(
        observation.get("qualityState") == "final", f"{label}: unexpected quality state", failures
    )
    check(
        observation.get("timingSource") == "media_pts",
        f"{label}: frame anchors must come from media PTS",
        failures,
    )
    check(
        int(observation.get("createdAtUnixMs", 0)) > 0, f"{label}: missing creation time", failures
    )
    provenance = observation.get("provenance", {})
    for field in (
        "plugin",
        "pluginVersion",
        "artifactDigest",
        "modelReleaseId",
        "modelId",
        "modelVersion",
        "configHash",
        "executionBackend",
        "modelArtifactDigest",
    ):
        check(bool(provenance.get(field)), f"{label}: provenance.{field} is empty", failures)
    check(
        provenance.get("artifactDigest") == expected_digest,
        f"{label}: provenance does not carry the running plugin artifact digest",
        failures,
    )
    check(
        provenance.get("modelArtifactDigest") == model_digest,
        f"{label}: model digest does not match the ONNX weights on disk: "
        f"{provenance.get('modelArtifactDigest')} != {model_digest}",
        failures,
    )
    for field in ("artifactDigest", "configHash", "modelArtifactDigest"):
        check(
            bool(DIGEST_PATTERN.match(str(provenance.get(field, "")))),
            f"{label}: provenance.{field} is not a sha256 digest",
            failures,
        )
    payload = observation.get("payload", {})
    check(
        len(json.dumps(payload)) <= MAX_PAYLOAD_JSON_CHARS,
        f"{label}: payload is far larger than the text of this frame could be",
        failures,
    )
    verify_payload(payload, provider, expect, label, failures)


def verify_payload(
    payload: dict, provider: str, expect: str, label: str, failures: list[str]
) -> None:
    image = payload.get("image", {})
    width = int(image.get("width", 0))
    height = int(image.get("height", 0))
    check(width > 0 and height > 0, f"{label}: payload has no usable frame size", failures)
    check(
        image.get("pixel_format") == PIXEL_FORMAT,
        f"{label}: payload does not record the input pixel format: {image}",
        failures,
    )
    stride = int(image.get("stride", 0))
    check(
        stride >= width * 4,
        f"{label}: recorded stride {stride} is smaller than a {width}px row",
        failures,
    )
    check(
        payload.get("coordinate_space") == "frame_pixels_top_left",
        f"{label}: the box coordinate space is not declared explicitly",
        failures,
    )
    blocks = payload.get("blocks", [])
    block_count = int(payload.get("block_count", 0))
    omitted = int(payload.get("blocks_omitted", 0))
    check(
        block_count == len(blocks) + omitted,
        f"{label}: block_count {block_count} != {len(blocks)} listed + {omitted} omitted",
        failures,
    )
    check(block_count <= MAX_BLOCKS, f"{label}: more blocks than the declared bound", failures)
    check(
        int(payload.get("char_count", -1)) == sum(len(block.get("text", "")) for block in blocks),
        f"{label}: char_count does not add up to the listed blocks",
        failures,
    )
    for block in blocks:
        verify_block(block, width, height, failures, label)

    engine = payload.get("engine", {})
    check(bool(engine.get("model_id")), f"{label}: engine.model_id is empty", failures)
    check(
        bool(engine.get("runtime_version")), f"{label}: engine.runtime_version is empty", failures
    )
    check(
        engine.get("runtime_version") not in {"unknown"},
        f"{label}: the OCR runtime version could not be read from package metadata",
        failures,
    )
    check(
        engine.get("model_source") in {"bundled", "local_dir"},
        f"{label}: engine.model_source is not a real provenance: {engine.get('model_source')}",
        failures,
    )
    weights = engine.get("weights", [])
    roles = sorted(item.get("role") for item in weights)
    check(
        roles == sorted(MODEL_FILES),
        f"{label}: weights do not cover the three PP-OCR roles: {roles}",
        failures,
    )
    for item in weights:
        check(
            bool(DIGEST_PATTERN.match(str(item.get("sha256", "")))),
            f"{label}: weight {item.get('name')} has no real digest",
            failures,
        )
        check(
            int(item.get("bytes", 0)) > 0,
            f"{label}: weight {item.get('name')} has no measured size",
            failures,
        )
    providers = engine.get("providers", {})
    check(
        sorted(providers) == sorted(MODEL_FILES),
        f"{label}: providers do not cover the three roles: {sorted(providers)}",
        failures,
    )
    for role, item in providers.items():
        check(
            item and item[0] == PROVIDER_EP[provider],
            f"{label}: role {role} did not run on the requested provider "
            f"{PROVIDER_EP[provider]}: {item}",
            failures,
        )

    # "模型没找到文字"与"插件失败"必须能分开：前者是空 blocks + 显式原因。
    if expect == "empty":
        check(not blocks, f"{label}: a text-free sample produced {len(blocks)} block(s)", failures)
        check(
            payload.get("empty_reason") == "model_found_no_text",
            f"{label}: an empty result must say why it is empty: {payload.get('empty_reason')}",
            failures,
        )
    else:
        check(bool(blocks), f"{label}: the model found no text on a text-bearing sample", failures)
        check(
            "empty_reason" not in payload,
            f"{label}: a non-empty result must not claim an empty reason",
            failures,
        )


def verify_no_leakage(
    payload: dict, media: pathlib.Path, plugin_process: Process, failures: list[str]
) -> None:
    blob = json.dumps(payload)
    for label, needle in (
        ("media file name", media.name),
        ("media absolute path", str(media)),
        ("data plane uri", "srt://"),
    ):
        check(needle not in blob, f"worker report leaks {label}", failures)
    plugin_output = "\n".join(plugin_process.lines + plugin_process.stderr)
    for label, needle in (("media file name", media.name), ("media path", str(media))):
        check(needle not in plugin_output, f"plugin output leaks {label}", failures)
    check(
        payload.get("segment_name_hint") == "opaque",
        "worker should only note that the segment name exists, never carry it",
        failures,
    )


def runtime_binary() -> pathlib.Path:
    for profile in ("release", "debug"):
        candidate = ROOT / f"target/{profile}/sensoryplex-runtime"
        if candidate.is_file():
            return candidate
    raise SystemExit("build the runtime first: make ocr-check builds it for you")


def verify(
    media: pathlib.Path,
    workspace: pathlib.Path,
    model_dir: str,
    provider: str,
    max_inputs: int,
    expect: str,
) -> list[str]:
    failures: list[str] = []
    expected_digest = package_digest()
    declared = manifest_digest()
    check(
        declared == expected_digest and bool(DIGEST_PATTERN.match(declared)),
        f"manifest artifact digest is not the real package digest: {declared} != {expected_digest}",
        failures,
    )
    directory, digests, model_digest = real_weights(model_dir)
    print(f"weights: {directory}")
    for role in sorted(digests):
        print(f"  {role}: {MODEL_FILES[role]} {digests[role]}")
    print(f"  combined: {model_digest}")

    data_plane = f"127.0.0.1:{free_port()}"
    plugin_address = f"127.0.0.1:{free_port()}"
    report_path = workspace / "replay.pb"
    worker_report = workspace / "ai-worker.json"
    plugin_config = workspace / "plugin-config.json"
    plugin_config.write_text(
        json.dumps(
            {
                "provider": provider,
                "model_dir": model_dir,
                "ttl_ms": SERVER_TTL_MS,
                "timeout_s": RUN_TIMEOUT_S,
            },
            indent=2,
        )
        + "\n"
    )

    producer = Process(
        "runtime",
        [
            str(runtime_binary()),
            "replay",
            str(PIPELINE),
            str(media),
            "--report",
            str(report_path),
            "--max-points",
            str(MAX_POINTS),
            "--handoff-listen",
            data_plane,
            "--handoff-retained-limit",
            str(RETAINED_LIMIT),
            "--handoff-arena-bytes",
            str(ARENA_BYTES),
            "--handoff-ttl-ms",
            str(SERVER_TTL_MS),
            "--handoff-wait-timeout-ms",
            str(WAIT_TIMEOUT_MS),
            "--handoff-idle-timeout-ms",
            str(IDLE_TIMEOUT_MS),
        ],
    )
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
    producer.start()
    try:
        ready = producer.wait_for("handoff_ready", timeout=READY_TIMEOUT_S)
        check(ready.get("listen") == data_plane, f"unexpected data plane: {ready}", failures)
        check(int(ready.get("retained", "0")) > 0, "no frame was retained", failures)

        plugin.start()
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
                "--data-plane",
                data_plane,
                "--plugin",
                plugin_address,
                "--input-kind",
                INPUT_KIND,
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
        payload = json.loads(worker_report.read_text()) if worker_report.is_file() else {}
        check(
            payload.get("checks_failed") == 0,
            f"worker failures: {payload.get('failures')}",
            failures,
        )
        check(payload.get("config_valid") is True, "plugin rejected the worker's config", failures)
        check(payload.get("start_state") == "ready", f"plugin not ready: {payload}", failures)

        description = payload.get("plugin_description", {})
        check(
            CONSUMES in description.get("consumes", []),
            f"plugin does not declare it consumes video frames: {description}",
            failures,
        )
        check(
            PRODUCES in description.get("produces", []),
            f"plugin does not declare the modality it produces: {description}",
            failures,
        )
        check(
            description.get("memory_kinds") == ["cpu_shared_memory"],
            f"plugin claims a memory kind it cannot read: {description}",
            failures,
        )

        observations = payload.get("observations", [])
        frames = payload.get("frames", [])
        check(
            len(observations) == min(max_inputs, payload.get("input_buffers_available", 0)),
            f"observation count does not match processed inputs: {payload.get('frames')}",
            failures,
        )
        check(
            len(observations) == len(frames),
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
                failures.append(f"{observation.get('observationId')}: no matching input descriptor")
                continue
            check(
                frame.get("kind") == INPUT_KIND,
                f"worker processed a different kind than requested: {frame.get('kind')}",
                failures,
            )
            verify_observation(
                observation,
                frame,
                expected_digest,
                model_digest,
                provider,
                expect,
                failures,
            )

        # 泄漏检查的对象是 worker 的整份报告（含每条 observation），而不是单条 observation：
        # `segment_name_hint` 是 worker 自己写的，只查 observation 等于永远查不到它。
        verify_no_leakage(payload, media, plugin, failures)

        drain = payload.get("drain", {})
        check(
            not drain.get("failures"), f"worker could not dispose of every input: {drain}", failures
        )
        check(
            drain.get("discarded", 0) > 0,
            "every retained input was consumed; the drain path was not exercised",
            failures,
        )

        after = payload.get("runtime_stats_after", {})
        total = int(after.get("retained_total", 0))
        released = int(after.get("released_total", 0))
        expired = int(after.get("expired_total", 0))
        retained = int(after.get("retained", 0))
        check(
            released + expired + retained == total,
            f"retained inputs are unaccounted for: {after}",
            failures,
        )
        check(retained == 0, f"inputs are still retained after the run: {after}", failures)
        check(after.get("arena_live_slabs") == 0, f"arena still holds slabs: {after}", failures)
        check(
            payload.get("worker_pid") not in {os.getpid()},
            "the worker must be its own process",
            failures,
        )
    finally:
        # 收尾顺序和 M8 一样有因果：先摘掉插件，生产者才会判定"消费者已离开"并走完空闲
        # 收尾，把 `handoff_stats` 打印出来。先 kill 生产者就再也读不到这行对账。
        plugin.kill()
        if plugin.process is not None:
            plugin.finish(timeout=30)

    exit_code = producer.finish(timeout=300)
    check(
        exit_code == 0,
        f"producer exited {exit_code}: {producer.stderr[-2000:]}",
        failures,
    )
    for process in (producer, plugin):
        check(
            str(media) not in "\n".join(process.lines + process.stderr),
            f"{process.name} output leaks the media path",
            failures,
        )

    stats = producer.wait_for("handoff_stats", timeout=60.0)
    check(stats.get("consumer_seen") == "true", f"no consumer was seen: {stats}", failures)
    check(stats.get("arena_live_slabs") == "0", f"arena still holds slabs: {stats}", failures)
    check(
        int(stats.get("released", 0)) + int(stats.get("expired", 0)) + int(stats.get("retained", 0))
        == int(stats.get("retained_total", 0)),
        f"producer ledger does not balance: {stats}",
        failures,
    )

    if report_path.is_file():
        report = replay_report(report_path)
        verify_decoded_frame_descriptors(report, failures)
        # `--max-points` 会诚实地写出截断原因；这里只拒绝"没实现的能力"，
        # 因为截断是本次刻意设定的上限，不是能力缺失。
        unimplemented = [item for item in report.blockers if "not_implemented" in item]
        check(
            not unimplemented,
            f"runtime reported unimplemented capabilities: {unimplemented}",
            failures,
        )
    else:
        failures.append("runtime wrote no report")

    reported = int(stats.get("released", 0))
    print(
        f"processed_inputs={len(payload.get('frames', []))} "
        f"observations={len(payload.get('observations', []))} released={reported} "
        f"engine={payload.get('plugin_description', {}).get('name')} provider={provider}"
    )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--media", type=pathlib.Path, required=True)
    parser.add_argument("--model-dir", default="")
    parser.add_argument("--provider", choices=sorted(PROVIDER_EP), default="cpu")
    parser.add_argument("--max-inputs", type=int, default=DEFAULT_INPUTS)
    parser.add_argument(
        "--expect",
        choices=("text", "empty"),
        default="text",
        help="样本的预期：有文字样本要求有块，无文字样本要求带 empty_reason 的空结果",
    )
    parser.add_argument("--keep-workspace", action="store_true")
    arguments = parser.parse_args()
    if not arguments.media.is_file():
        raise SystemExit(f"media not found: {arguments.media}")

    workspace = pathlib.Path(tempfile.mkdtemp(prefix="sensoryplex-ocr-"))
    print(f"workspace: {workspace}{' (kept)' if arguments.keep_workspace else ''}")
    print(f"host: {socket.gethostname()} worker_timeout_s={RUN_TIMEOUT_S}")
    started = time.monotonic()
    failures = verify(
        arguments.media,
        workspace,
        arguments.model_dir,
        arguments.provider,
        arguments.max_inputs,
        arguments.expect,
    )
    if failures:
        print("\nocr acceptance FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(
        f"\nocr acceptance: real video frames -> local PP-OCR ({arguments.provider}) -> "
        f"anchored text blocks passed in {round(time.monotonic() - started, 1)}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
