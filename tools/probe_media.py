"""Inspect authorized local media without producing fabricated observations."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def probe(path: Path):
    with path.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name:stream=index,codec_type,codec_name,width,height,sample_rate",
            "-of",
            "json",
            str(path.resolve()),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError("media_probe_failed")
    return {"content_hash": "sha256:" + digest, "probe": json.loads(result.stdout)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    print(json.dumps(probe(args.path), ensure_ascii=False, indent=2))
