"""启动隔离栈的宿主原生 Node Agent 与通用补全 Worker。"""

import os
import subprocess


def main():
    root = str(Path(__file__).resolve().parents[2])
    env = dict(
        os.environ,
        PYTHONPATH=f"{root}:{root}/plugins/python/common/src",
        SENSORYPLEX_RUNTIME_BIN="/Users/tuzhipeng/Documents/SensoryPlex/target/release/sensoryplex-runtime",
        PYTHONUNBUFFERED="1",
        SENSORYPLEX_NO_GL="1",
    )
    py = "/Users/tuzhipeng/Documents/SensoryPlex-plugin-examples/.venv/bin/python"
    state = ".data/plugin-v2-acceptance/agent/local-host.json"
    log1 = open("/tmp/agent-final.log", "w")
    log2 = open("/tmp/enrichment-worker.log", "w")

    p1 = subprocess.Popen(
        [py, "-m", "tools.node_agent", "run", "--node-id", "local-host", "--state-file", state],
        env=env,
        stdout=log1,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    p2 = subprocess.Popen(
        [
            py,
            "-m",
            "tools.enrichment_worker",
            "--state-file",
            state,
            "--nats-url",
            "nats://127.0.0.1:34222",
        ],
        env=env,
        stdout=log2,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    print(f"Started native Agent PID: {p1.pid}, Worker PID: {p2.pid}")


if __name__ == "__main__":
    from pathlib import Path

    main()
