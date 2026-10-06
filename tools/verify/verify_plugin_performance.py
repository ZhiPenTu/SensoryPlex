"""宿主真实媒体解码基线对比；报告实际时间、RSS、解码数量与 arena，不替代业务验收。"""

import argparse
import hashlib
import json
import re
import statistics
import subprocess
import time
from pathlib import Path

from edge_material_sdk.generated.media.v1 import media_pb2


def digest(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def measure(binary, pipeline, media, destination, index):
    prefix = destination / str(index)
    command = [
        "/usr/bin/time",
        "-l",
        str(binary),
        "replay",
        str(pipeline),
        str(media),
        "--report",
        str(prefix.with_suffix(".pb")),
        "--sampling-min-interval-ms",
        "1000",
        "--sampling-static-hold-ms",
        "5000",
        "--audio-segment-ms",
        "6000",
        "--audio-overlap-ms",
        "500",
    ]
    start = time.monotonic()
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    elapsed = time.monotonic() - start
    prefix.with_suffix(".log").write_text(result.stdout + result.stderr)
    if result.returncode:
        raise ValueError("performance_decode_failed")
    report = media_pb2.ReplayReport.FromString(prefix.with_suffix(".pb").read_bytes())
    assert report.HasField("decoded") and not report.decoded.failure_reasons
    peak = re.search(r"(\d+)\s+maximum resident set size", result.stderr)
    if not peak:
        raise ValueError("performance_peak_rss_unavailable")
    return {
        "elapsed_s": round(elapsed, 4),
        "peak_rss_bytes": int(peak[1]),
        "decoded_items": report.decoded_items,
        "decoded_bytes": report.decoded.decoded_bytes,
        "arena_peak_bytes": report.decoded.arena_peak_bytes,
        "leases_issued": report.decoded.leases_issued,
        "leases_released": report.decoded.leases_released,
        "decode_invocations": 1,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-binary", required=True, type=Path)
    parser.add_argument("--candidate-binary", required=True, type=Path)
    parser.add_argument("--pipeline", required=True, type=Path)
    parser.add_argument("--media", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    records = {}
    for name, binary in [("baseline", args.baseline_binary), ("candidate", args.candidate_binary)]:
        target = args.out / name
        target.mkdir()
        runs = [
            measure(binary.resolve(), args.pipeline.resolve(), args.media.resolve(), target, i)
            for i in range(3)
        ]
        records[name] = {
            "binary_digest": digest(binary),
            "runs": runs,
            "median_elapsed_s": statistics.median(r["elapsed_s"] for r in runs),
            "max_peak_rss_bytes": max(r["peak_rss_bytes"] for r in runs),
        }
    result = {
        "asset_digest": digest(args.media),
        "pipeline_digest": digest(args.pipeline),
        "hardware": subprocess.check_output(
            ["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], text=True
        ).strip(),
        "scope": "same_policy_decode_only_no_business_peak_claim",
        "measurements": records,
    }
    (args.out / "comparison.json").write_text(json.dumps(result, indent=2))
    print(
        json.dumps(
            {
                k: {p: v[p] for p in ("median_elapsed_s", "max_peak_rss_bytes")}
                for k, v in records.items()
            }
        )
    )


if __name__ == "__main__":
    main()
