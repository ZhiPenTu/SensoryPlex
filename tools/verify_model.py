"""M8 模型链路验收：真实帧 → 本机 VLM → 带锚点/来源/版本/置信度语义的 observation。

四个角色必须是四个进程，否则"模型链路"只是口号：
1. 本脚本（编排 + 对账）；
2. `sensoryplex-runtime replay --handoff-listen`（生产者，持有字节与保留表）；
3. `python -m edge_material_plugin_vlm_moondream`（插件进程，自己按 lease 读字节、跑模型）；
4. `tools/ai_worker.py`（worker：只发现与调用，不读字节）。

判定标准（全部为真实执行结果）：

- manifest 里的 `artifacts.digest` 与本机复算的插件包摘要一致（拒绝占位串）；
- 插件 `Describe` 声明消费 `media.video_frame`，且只声明 `cpu_shared_memory`；
- observation 的时间锚点等于源帧的半开区间（不重新计时）；
- `content_hash` 等于数据面给出的源窗口摘要（observation 绑定到具体字节）；
- provenance 完整，且 `model_artifact_digest` 与本机模型服务报告的真实摘要一致；
- 模型不给校准置信度：`confidence` 必须缺省、`confidence_unavailable_reason` 必须写明；
- 帧字节不外泄：插件 stdout、worker 报告、observation 里都没有段名/媒体路径/URI/像素数据；
- 账目：插件读完即归还 lease，`released + expired + retained == retained_total`、无悬挂 slab。
"""

import argparse
import importlib
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "config/pipelines/file-material.yaml"
WORKER = ROOT / "tools/ai_worker.py"
PLUGIN_DIR = ROOT / "plugins/python/processors/vlm-moondream"
PLUGIN_MANIFEST = PLUGIN_DIR / "plugin.yaml"
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

# 空闲超时必须大于单帧推理时间：模型调用期间没有数据面 RPC，超时太短会在推理中途关掉数据面。
IDLE_TIMEOUT_MS = 60_000
WAIT_TIMEOUT_MS = 60_000
SERVER_TTL_MS = 30_000
RETAINED_LIMIT = 32
ARENA_BYTES = 256 * 1024 * 1024
READY_TIMEOUT_S = 900.0
RUN_TIMEOUT_S = 1_800.0
DEFAULT_FRAMES = 2


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Process:
    """按行观察 stdout 的子进程；用于生产者与插件服务。"""

    def __init__(self, name: str, command: list[str]):
        self.name = name
        self.command = command
        self.lines: list[str] = []
        self.stderr: list[str] = []
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        self.process = subprocess.Popen(
            self.command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
        )
        for stream, sink in ((self.process.stdout, self.lines), (self.process.stderr, self.stderr)):
            threading.Thread(target=self._pump, args=(stream, sink), daemon=True).start()

    @staticmethod
    def _pump(stream, sink: list[str]) -> None:
        for line in stream:
            sink.append(line.rstrip("\n"))

    def wait_for(self, prefix: str, timeout: float) -> dict[str, str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in list(self.lines):
                if line.startswith(prefix):
                    return parse_fields(line)
            if self.process is not None and self.process.poll() is not None:
                raise AssertionError(
                    f"{self.name} exited before {prefix!r}: stdout={self.lines[-5:]} "
                    f"stderr={self.stderr[-5:]}"
                )
            time.sleep(0.05)
        raise AssertionError(f"{self.name} never reported {prefix!r}: {self.lines[-5:]}")

    def finish(self, timeout: float) -> int:
        assert self.process is not None
        return self.process.wait(timeout=timeout)

    def kill(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()


def parse_fields(line: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for token in line.split(" "):
        key, separator, value = token.partition("=")
        if separator:
            fields[key] = value
    return fields


def check(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def package_digest() -> str:
    """本机复算插件包摘要：manifest 里的值必须与它一致，否则就是占位串。"""
    module = importlib.import_module("edge_material_plugin_vlm_moondream.artifact")
    return str(module.package_digest(PLUGIN_DIR))


def manifest_digest() -> str:
    text = PLUGIN_MANIFEST.read_text()
    for line in text.splitlines():
        if line.strip().startswith("digest:"):
            return line.split("digest:", 1)[1].strip()
    raise SystemExit("plugin.yaml has no artifacts.digest line")


def model_service_digest(endpoint: str, model: str) -> str:
    with urllib.request.urlopen(f"{endpoint}/api/tags", timeout=10) as response:
        payload = json.load(response)
    entry = next((item for item in payload.get("models", []) if item.get("name") == model), None)
    if entry is None:
        raise SystemExit(f"model {model!r} is not available on {endpoint}")
    return "sha256:" + str(entry["digest"])


def probe_model_service(endpoint: str, model: str) -> str:
    """模型服务是验收前置条件：不可达必须显式失败，不能静默跳过。"""
    try:
        return model_service_digest(endpoint, model)
    except (OSError, KeyError, StopIteration, json.JSONDecodeError) as error:
        raise SystemExit(
            f"local model service is required for M8 acceptance: {endpoint} ({error}). "
            f"Start it and make sure {model!r} is pulled."
        ) from error


def verify(
    media: pathlib.Path, workspace: pathlib.Path, model_endpoint: str, model: str, max_frames: int
) -> list[str]:
    failures: list[str] = []

    expected_digest = package_digest()
    declared = manifest_digest()
    check(
        declared == expected_digest and bool(DIGEST_PATTERN.match(declared)),
        f"manifest artifact digest is not the real package digest: {declared} != {expected_digest}",
        failures,
    )
    real_model_digest = probe_model_service(model_endpoint, model)

    data_plane_port = free_port()
    data_plane = f"127.0.0.1:{data_plane_port}"
    plugin_port = free_port()
    plugin_address = f"127.0.0.1:{plugin_port}"
    report_path = workspace / "replay.pb"
    worker_report = workspace / "ai-worker.json"

    producer = Process(
        "runtime",
        [
            str(runtime_binary()),
            "replay",
            str(PIPELINE),
            str(media),
            "--report",
            str(report_path),
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
            "edge_material_plugin_vlm_moondream",
            "--port",
            str(plugin_port),
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
        started = plugin.wait_for("plugin ready", timeout=120.0)
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
                "--model-endpoint",
                model_endpoint,
                "--model",
                model,
                "--max-frames",
                str(max_frames),
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
            "media.video_frame" in description.get("consumes", []),
            f"plugin does not declare it consumes video frames: {description}",
            failures,
        )
        check(
            description.get("memory_kinds") == ["cpu_shared_memory"],
            f"plugin claims a memory kind it cannot read: {description}",
            failures,
        )

        observations = payload.get("observations", [])
        frames = payload.get("frames", [])
        check(len(observations) > 0, "no observation was produced", failures)
        check(
            len(observations)
            == len(frames)
            == min(max_frames, payload.get("video_buffers_available", 0)),
            f"observation count does not match processed frames: {payload.get('frames')}",
            failures,
        )
        for observation in observations:
            verify_observation(observation, payload, real_model_digest, expected_digest, failures)

        ids = [item.get("observationId") for item in observations]
        check(len(set(ids)) == len(ids), f"observation ids are not unique: {ids}", failures)

        drain = payload.get("drain", {})
        check(
            not drain.get("failures"),
            f"the worker could not hand back every retained buffer: {drain}",
            failures,
        )
        check(
            drain.get("discarded", 0) > 0,
            "no non-video buffer was handed back; the data plane requires every lease to settle",
            failures,
        )

        after = payload.get("runtime_stats_after", {})
        total = int(after.get("retained_total", 0))
        released = int(after.get("released_total", 0))
        expired = int(after.get("expired_total", 0))
        retained = int(after.get("retained", 0))
        check(
            released + expired + retained == total and retained == 0,
            f"the plugin did not return every lease: {after}",
            failures,
        )
        check(
            released >= len(observations),
            f"fewer releases than observations: released={released} "
            f"observations={len(observations)}",
            failures,
        )
        check(after.get("arena_live_slabs") == 0, f"arena still holds slabs: {after}", failures)

        producer_pid = producer.process.pid if producer.process is not None else None
        plugin_pid = plugin.process.pid if plugin.process is not None else None
        check(
            payload.get("worker_pid") not in {os.getpid(), producer_pid, plugin_pid},
            f"the worker must be its own process: worker={payload.get('worker_pid')} "
            f"orchestrator={os.getpid()} producer={producer_pid} plugin={plugin_pid}",
            failures,
        )

        verify_no_leakage(payload, media, plugin.lines, failures)
    finally:
        plugin.kill()
        plugin.finish(timeout=30) if plugin.process is not None else None

    exit_code = producer.finish(timeout=300)
    stats = producer.wait_for("handoff_stats", timeout=10)
    check(exit_code == 0, f"producer exited {exit_code}: {producer.stderr[-2000:]}", failures)
    check(stats.get("consumer_seen") == "true", f"no consumer was seen: {stats}", failures)
    check(stats.get("arena_live_slabs") == "0", f"arena still holds slabs: {stats}", failures)

    plugin_lines = "\n".join(plugin.lines + plugin.stderr)
    check(
        "plugin ready" in plugin_lines,
        f"plugin never reported readiness: {plugin_lines[:2000]}",
        failures,
    )
    return failures


def verify_observation(
    observation: dict,
    payload: dict,
    real_model_digest: str,
    expected_digest: str,
    failures: list[str],
) -> None:
    label = observation.get("observationId", "<missing>")
    check(
        observation.get("modality") == "vision.scene_description",
        f"{label}: wrong modality",
        failures,
    )
    time_range = observation.get("timeRange", {})
    start, end = int(time_range.get("startMs", 0)), int(time_range.get("endMs", 0))
    check(
        end > start >= 0, f"{label}: time range is not a half-open interval: {time_range}", failures
    )
    frame = next(
        (
            item
            for item in payload.get("frames", [])
            if item.get("observation_id") == observation.get("observationId")
        ),
        None,
    )
    check(frame is not None, f"{label}: no matching processed frame", failures)
    if frame is not None:
        check(
            [start, end] == frame.get("source_time_range_ms"),
            f"{label}: anchor drifted from the source frame: {[start, end]} != "
            f"{frame.get('source_time_range_ms')}",
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
        f"{label}: model digest does not match the model service: "
        f"{provenance.get('modelArtifactDigest')} != {real_model_digest}",
        failures,
    )
    for field in ("artifactDigest", "configHash", "modelArtifactDigest"):
        check(
            bool(DIGEST_PATTERN.match(str(provenance.get(field, "")))),
            f"{label}: provenance.{field} is not a sha256 digest",
            failures,
        )
    text = str(observation.get("payload", {}).get("text", "")).strip()
    check(bool(text), f"{label}: model produced no text", failures)
    check(
        len(text) < 2000,
        f"{label}: payload text is implausibly long for a one-sentence description",
        failures,
    )


def verify_no_leakage(
    payload: dict, media: pathlib.Path, plugin_lines: list[str], failures: list[str]
) -> None:
    """帧字节、段名与媒体路径都必须在控制面之外。"""
    blob = json.dumps(payload)
    forbidden = {
        "media file name": media.name,
        "media absolute path": str(media),
        "data plane uri": "srt://",
        "segment name": "/sp.",
        "base64 pixel payload": "iVBORw0KGgo",
    }
    for label, needle in forbidden.items():
        check(needle not in blob, f"worker report leaks {label}", failures)
    plugin_stdout = "\n".join(plugin_lines)
    for label, needle in (
        ("media file name", media.name),
        ("segment name", "/sp."),
        ("base64 pixel payload", "iVBORw0KGgo"),
    ):
        check(needle not in plugin_stdout, f"plugin stdout leaks {label}", failures)
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
    raise SystemExit("build the runtime first: make model-check builds it for you")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--media", type=pathlib.Path, required=True)
    parser.add_argument("--model-endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="moondream:v2")
    parser.add_argument("--max-frames", type=int, default=DEFAULT_FRAMES)
    parser.add_argument("--keep-workspace", action="store_true")
    arguments = parser.parse_args()
    if not arguments.media.is_file():
        raise SystemExit(f"media not found: {arguments.media}")

    import tempfile  # noqa: PLC0415 - 只在需要时创建临时工作区

    workspace = pathlib.Path(tempfile.mkdtemp(prefix="sensoryplex-model-"))
    print(f"workspace: {workspace}{' (kept)' if arguments.keep_workspace else ''}")
    failures = verify(
        arguments.media, workspace, arguments.model_endpoint, arguments.model, arguments.max_frames
    )
    if failures:
        print("\nmodel acceptance FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nmodel acceptance: real frames -> local VLM -> anchored observations passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
