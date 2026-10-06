"""宿主真实授权音轨验收：Rust 按锚点解码、租约读取和释放；不生成 Observation。"""

import argparse
import hashlib
import json
import math
import struct
import subprocess
import tempfile
import time
from pathlib import Path

from edge_material_sdk.buffer_reader import LeaseBufferReader
from edge_material_sdk.generated.common.v1 import common_pb2 as common
from edge_material_sdk.generated.orchestration.v1 import orchestration_pb2 as pb


def verify(media, binary, report):
    digest = "sha256:" + hashlib.sha256(media.read_bytes()).hexdigest()
    short = digest[7:19]
    task = pb.EnrichmentTask(
        task_id="enr_audio_acceptance",
        content_hash=digest,
        stream_id="stream-" + short,
        source_id="file-" + short,
        input_modality="media.audio_segment",
        time_range={"start_ms": 1000, "end_ms": 3000},
    )
    with tempfile.TemporaryDirectory(prefix="sensoryplex-audio-acceptance-") as directory:
        root = Path(directory)
        (root / "task.pb").write_bytes(task.SerializeToString(deterministic=True))
        process = subprocess.Popen(
            [
                str(binary),
                "enrichment-media",
                str(root / "task.pb"),
                str(media),
                str(root / "input.pb"),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 40
            while not (root / "input.pb").exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise ValueError("audio_anchor_decode_failed")
                time.sleep(0.05)
            descriptor = common.BufferDescriptor.FromString((root / "input.pb").read_bytes())
            reader = LeaseBufferReader(descriptor.locator.handoff_endpoint)
            try:
                value = reader.read(descriptor.buffer_id)
                assert value.kind == "audio_segment"
                assert value.format.sample_format == "F32LE"
                assert value.format.sample_rate == 16000 and value.format.channels == 1
                samples = [v[0] for v in struct.iter_unpack("<f", value.payload)]
                assert samples and all(math.isfinite(v) for v in samples)
                assert any(abs(v) > 0.00001 for v in samples)
                assert 1000 <= value.time_range.start_ms < value.time_range.end_ms <= 3000
                assert len(samples) <= 32000
                result = {
                    "asset_digest": digest,
                    "sample_format": "F32LE",
                    "sample_rate": 16000,
                    "channels": 1,
                    "samples": len(samples),
                    "decoded_digest": value.digest,
                    "start_ms": value.time_range.start_ms,
                    "end_ms": value.time_range.end_ms,
                    "lease_released": True,
                }
            finally:
                reader.close()
            assert process.wait(timeout=10) == 0
            report.write_text(json.dumps(result, indent=2))
            print(json.dumps(result))
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--media", required=True, type=Path)
    parser.add_argument("--binary", type=Path, default=Path("target/release/sensoryplex-runtime"))
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    verify(args.media.resolve(), args.binary.resolve(), args.report)


if __name__ == "__main__":
    main()
