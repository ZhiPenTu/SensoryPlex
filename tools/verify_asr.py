"""M10 模型链路验收：真实音频段 → 本机 MLX Whisper → 带锚点/来源/版本/置信度语义的转写。

四个角色必须是四个进程，否则"模型链路"只是口号：
1. 本脚本（编排 + 对账）；
2. `sensoryplex-runtime replay --handoff-listen`（生产者，持有字节与保留表）；
3. `python -m edge_material_plugin_asr_whisper_mlx`（插件进程，自己按 lease 读字节、跑模型）；
4. `tools/ai_worker.py`（worker：只发现与调用，不读字节）。

判定标准（全部为真实执行结果）：

- manifest 里的 `artifacts.digest` 与本机复算的插件包摘要一致（拒绝占位串）；
- 插件 `Describe` 声明消费 `media.audio_segment`，且只声明 `cpu_shared_memory`；
- observation 的时间锚点等于**源音频段**的半开区间（不重新计时），`content_hash` 等于该段
  的 lease 窗口摘要（observation 绑定到具体字节）；
- `model_artifact_digest` 与本机独立复算的**权重文件**摘要一致（不是配置里写的版本号）；
- 模型不给校准置信度：`confidence` 必须缺省、`confidence_unavailable_reason` 必须写明；
- 输入事实可对账：样本布局/采样率/声道数来自 descriptor，重采样后的样本数与输入成比例；
- 音频字节不外泄：插件 stdout/stderr、worker 报告、observation 里都没有媒体名/路径/整段 PCM；
- 账目：插件读完即归还 lease，`released + expired + retained == retained_total`、无悬挂 slab。

这段验收需要一个**有语音**的样本：静音样本上"文本为空"是正确结果，不能用来证明转写可用。
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

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins/python/common/src"))

from tools.verify_model import Process, check, free_port  # noqa: E402

PIPELINE = ROOT / "config/pipelines/file-material.yaml"
WORKER = ROOT / "tools/ai_worker.py"
PLUGIN_DIR = ROOT / "plugins/python/processors/asr-whisper-mlx"
PLUGIN_MANIFEST = PLUGIN_DIR / "plugin.yaml"
PLUGIN_MODULE = "edge_material_plugin_asr_whisper_mlx"
INPUT_KIND = "audio_segment"
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
WEIGHT_FILES = ("weights.safetensors", "weights.npz")
HASH_CHUNK_BYTES = 1 << 20
# 子段时间戳由模型按 20 ms 粒度给出，与窗口边界对齐时允许一个很小的松弛。
SEGMENT_SLACK_MS = 1_000
MAX_PAYLOAD_JSON_CHARS = 16_384

# 消费者连上来之前，生产者按"等不到消费者"计时。这段窗口要覆盖插件 Start（冷缓存时要下载
# 1.6 GB 权重）加首次推理——这期间 worker 还没碰过数据面，一个 RPC 都没有。
WAIT_TIMEOUT_MS = 900_000
# 消费者离开之后的空闲收尾，决定脚本能多快等到生产者自行退出并打印 `handoff_stats`。
IDLE_TIMEOUT_MS = 60_000
SERVER_TTL_MS = 30_000
RETAINED_LIMIT = 32
ARENA_BYTES = 256 * 1024 * 1024
# 解码上限：这一段验收只需要几十秒音频，不需要整片素材。
MAX_POINTS = 2_000
READY_TIMEOUT_S = 900.0
RUN_TIMEOUT_S = 3_600.0
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


def weights_path(model_dir: str, model: str, revision: str) -> pathlib.Path:
    """独立解析权重文件：与 mlx_whisper.load_model 的取值顺序一致（先 safetensors 再 npz）。"""
    if model_dir:
        directory = pathlib.Path(model_dir).expanduser()
    else:
        from huggingface_hub import snapshot_download

        directory = pathlib.Path(
            snapshot_download(repo_id=model, revision=revision or None, allow_patterns=None)
        )
    for name in WEIGHT_FILES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    raise SystemExit(f"no weight file under {directory}: expected one of {WEIGHT_FILES}")


def weights_digest(path: pathlib.Path) -> str:
    """独立复算摘要：不调用插件代码，避免"自己验证自己"。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def replay_report(path: pathlib.Path):
    from edge_material_sdk.generated.media.v1 import media_pb2

    report = media_pb2.ReplayReport()
    report.ParseFromString(path.read_bytes())
    return report


def verify_segments(report, failures: list[str]) -> None:
    """运行时报告本身要能证明'段描述符带上了样本布局'，否则插件只能靠猜。"""
    listed = list(report.decoded.audio_segments.listed)
    check(bool(listed), "runtime report listed no audio segment", failures)
    check(
        all(segment.sample_format == "F32LE" for segment in listed),
        "runtime reported an audio segment without the F32LE sample format: "
        f"{[segment.sample_format for segment in listed]}",
        failures,
    )
    audio_tracks = [track for track in report.decoded.tracks if track.track_kind == "audio"]
    check(bool(audio_tracks), "runtime reported no audio track", failures)
    check(
        all(track.audio_format == "F32LE" for track in audio_tracks),
        f"decoded audio is not normalized to F32LE: {[t.audio_format for t in audio_tracks]}",
        failures,
    )


def verify_observation(
    observation: dict,
    frame: dict,
    expected_digest: str,
    real_model_digest: str,
    failures: list[str],
) -> None:
    label = observation.get("observationId", "<missing>")
    check(
        observation.get("modality") == "asr_segment",
        f"{label}: unexpected modality {observation.get('modality')}",
        failures,
    )
    time_range = observation.get("timeRange", {})
    start = int(time_range.get("startMs", 0))
    end = int(time_range.get("endMs", 0))
    source = frame.get("source_time_range_ms")
    check(
        [start, end] == source,
        f"{label}: anchor drifted from the source audio segment: {[start, end]} != {source}",
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
    check(bool(observation.get("timingSource")), f"{label}: missing timing source", failures)
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
        provenance.get("modelArtifactDigest") == real_model_digest,
        f"{label}: model digest does not match the weights on disk: "
        f"{provenance.get('modelArtifactDigest')} != {real_model_digest}",
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
        f"{label}: payload is far larger than a transcript of this window could be",
        failures,
    )
    text = str(payload.get("text", "")).strip()
    check(bool(text), f"{label}: the model produced no transcript for a speech sample", failures)
    check(len(text) <= 8_000, f"{label}: transcript exceeds the declared bound", failures)
    verify_payload_facts(payload, frame, failures, label)


def verify_payload_facts(payload: dict, frame: dict, failures: list[str], label: str) -> None:
    """输入事实必须能对上 descriptor：布局、采样率、声道与重采样比例都不是自证。"""
    facts = payload.get("input", {})
    window_ms = int(facts.get("window_ms", -1))
    source = frame.get("source_time_range_ms") or [0, 0]
    check(
        window_ms == source[1] - source[0],
        f"{label}: window_ms {window_ms} does not match the source segment width",
        failures,
    )
    check(
        facts.get("sample_format") == "F32LE",
        f"{label}: transcript does not record the input sample layout: {facts}",
        failures,
    )
    check(
        facts.get("whisper_sample_rate") == 16_000,
        f"{label}: the model entry sample rate is not the documented 16 kHz: {facts}",
        failures,
    )
    source_rate = int(facts.get("input_sample_rate", 0))
    check(
        source_rate > 0 and int(facts.get("input_channels", 0)) > 0,
        f"{label}: no input facts",
        failures,
    )
    if source_rate > 0:
        expected = round(int(facts.get("input_samples", 0)) * 16_000 / source_rate)
        check(
            abs(int(facts.get("whisper_samples", 0)) - expected) <= 1,
            f"{label}: resampled sample count {facts.get('whisper_samples')} != {expected}",
            failures,
        )
    check(
        payload.get("segment_timing") == "media_pts_window_relative_plus_window_start",
        f"{label}: segment timing relation is not declared explicitly",
        failures,
    )
    start, end = source
    # 模型会给越窗时间戳（Whisper 幻觉时实测到 5 秒窗口上的 [940, 29880]）。契约要求的是
    # **不静默**：越窗的子段必须被显式标记并计数，而不是被夹取成合法区间，也不是悄悄丢掉。
    flagged = 0
    for segment in payload.get("segments", []):
        segment_start = int(segment.get("start_ms", 0))
        segment_end = int(segment.get("end_ms", 0))
        exact_within = start <= segment_start <= segment_end <= end
        slack_within = (
            start - SEGMENT_SLACK_MS <= segment_start <= segment_end <= end + SEGMENT_SLACK_MS
        )
        if segment.get("timing_outside_window"):
            flagged += 1
            check(
                not exact_within,
                f"{label}: sub-segment {[segment_start, segment_end]} is flagged as outside "
                f"but lies inside its window {source}",
                failures,
            )
            continue
        check(
            slack_within,
            f"{label}: sub-segment {[segment_start, segment_end]} leaves its own window {source} "
            "without being flagged",
            failures,
        )
    check(
        int(payload.get("segments_outside_window", 0)) == flagged,
        f"{label}: out-of-window count {payload.get('segments_outside_window')} does not match the "
        f"{flagged} flagged sub-segment(s)",
        failures,
    )
    # 解码诊断不是置信度：必须原样带出，且不能冒充 `confidence`。
    for segment in payload.get("segments", []):
        check("avg_logprob" in segment, f"{label}: decode diagnostics were dropped", failures)


def verify_no_leakage(
    payload: dict, media: pathlib.Path, plugin_process: Process, failures: list[str]
) -> None:
    blob = json.dumps(payload)
    forbidden = {
        "media file name": media.name,
        "media absolute path": str(media),
        "data plane uri": "srt://",
        "segment name": "/sp.",
    }
    for label, needle in forbidden.items():
        check(needle not in blob, f"worker report leaks {label}", failures)
    plugin_output = "\n".join(plugin_process.lines + plugin_process.stderr)
    for label, needle in (
        ("media file name", media.name),
        ("media path", str(media)),
        ("segment name", "/sp."),
    ):
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
    raise SystemExit("build the runtime first: make asr-check builds it for you")


def verify(
    media: pathlib.Path,
    workspace: pathlib.Path,
    model_dir: str,
    model: str,
    revision: str,
    language: str | None,
    max_inputs: int,
) -> list[str]:
    failures: list[str] = []
    expected_digest = package_digest()
    declared = manifest_digest()
    check(
        declared == expected_digest and bool(DIGEST_PATTERN.match(declared)),
        f"manifest artifact digest is not the real package digest: {declared} != {expected_digest}",
        failures,
    )
    weights = weights_path(model_dir, model, revision)
    real_model_digest = weights_digest(weights)
    print(f"weights: {weights.name} ({weights.stat().st_size} bytes) {real_model_digest}")

    data_plane = f"127.0.0.1:{free_port()}"
    plugin_address = f"127.0.0.1:{free_port()}"
    report_path = workspace / "replay.pb"
    worker_report = workspace / "ai-worker.json"
    plugin_config = workspace / "plugin-config.json"
    plugin_config.write_text(
        json.dumps(
            {
                "model": model,
                "model_revision": revision,
                "model_dir": model_dir,
                "language": language,
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
        check(int(ready.get("retained", "0")) > 0, "no input was retained", failures)

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
            "media.audio_segment" in description.get("consumes", []),
            f"plugin does not declare it consumes audio segments: {description}",
            failures,
        )
        check(
            "observation.asr_segment" in description.get("produces", []),
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

        check(
            bool(payload.get("segment_name_hint")),
            "worker did not report segment visibility",
            failures,
        )
        for observation in observations:
            frame = next(
                (
                    item
                    for item in frames
                    if item.get("observation_id") == observation.get("observationId")
                ),
                None,
            )
            check(
                frame is not None,
                f"no matching processed input for {observation.get('observationId')}",
                failures,
            )
            if frame is None:
                continue
            check(
                frame.get("kind") == INPUT_KIND,
                f"worker processed a different kind than requested: {frame.get('kind')}",
                failures,
            )
            verify_observation(observation, frame, expected_digest, real_model_digest, failures)

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
        verify_segments(replay_report(report_path), failures)
    else:
        failures.append("runtime wrote no report")

    reported = int(stats.get("released", 0))
    print(
        f"processed_inputs={len(payload.get('frames', []))} "
        f"observations={len(payload.get('observations', []))} released={reported} "
        f"engine={payload.get('plugin_description', {}).get('name')}"
    )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--media", type=pathlib.Path, required=True)
    parser.add_argument("--model", default="mlx-community/whisper-large-v3-turbo")
    parser.add_argument("--model-dir", default="")
    parser.add_argument("--model-revision", default="")
    parser.add_argument("--language", default=None)
    parser.add_argument("--max-inputs", type=int, default=DEFAULT_INPUTS)
    parser.add_argument("--keep-workspace", action="store_true")
    arguments = parser.parse_args()
    if not arguments.media.is_file():
        raise SystemExit(f"media not found: {arguments.media}")

    workspace = pathlib.Path(tempfile.mkdtemp(prefix="sensoryplex-asr-"))
    print(f"workspace: {workspace}{' (kept)' if arguments.keep_workspace else ''}")
    print(f"host: {socket.gethostname()} worker_timeout_s={RUN_TIMEOUT_S}")
    started = time.monotonic()
    failures = verify(
        arguments.media,
        workspace,
        arguments.model_dir,
        arguments.model,
        arguments.model_revision,
        arguments.language,
        arguments.max_inputs,
    )
    if failures:
        print("\nasr acceptance FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(
        f"\nasr acceptance: real audio segments -> local MLX Whisper -> anchored observations "
        f"passed in {round(time.monotonic() - started, 1)}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
