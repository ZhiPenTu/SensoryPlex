"""宿主已安装插件的真实 Process 拒绝路径，不读取越权媒体字节。"""

import argparse
import json
from pathlib import Path

import grpc
from edge_material_sdk.generated.common.v1 import common_pb2 as common
from edge_material_sdk.generated.material.v1 import material_pb2 as material
from edge_material_sdk.generated.runtime.v1 import runtime_pb2 as pb
from edge_material_sdk.generated.runtime.v1 import runtime_pb2_grpc as rpc

from tools.node_agent_platform import read_endpoint_file


def verify(args):
    registry = json.loads((args.state_file.parent / "plugins/hot-deploy.json").read_text())
    records = []

    def endpoint(release):
        entries = [
            e
            for e in registry.values()
            if e["release_id"] == release and e.get("desired_state") == "running"
        ]
        entry = max(entries, key=lambda e: int(e.get("generation", 0)))
        return read_endpoint_file(Path(entry["endpoint_file"]))["endpoint"]

    source = material.Observation.FromString(args.observation.read_bytes())
    context = common.RequestContext(
        request_id="acceptance-rejection",
        trace_id="acceptance-rejection",
        stream_id=source.stream_id,
        source_id=source.source_id,
    )

    def reject(name, release, items, reason):
        with grpc.insecure_channel(endpoint(release)) as channel:
            result = rpc.ProcessorPluginServiceStub(channel).Process(
                pb.ProcessRequest(context=context, processor_release_id=release, inputs=items),
                timeout=10,
            )
        assert result.HasField("error") and result.error.reason_code == reason, (
            name,
            result.error.reason_code,
        )
        assert not result.observations
        records.append({"case": name, "reason_code": reason, "observations": 0})

    corrupted = material.Observation.FromString(source.SerializeToString())
    corrupted.payload.fields["mean_luma"].string_value = "wrong"
    reject(
        "input_schema_mismatch",
        args.threshold_release,
        [pb.PluginInput(observation=corrupted)],
        "payload_schema_invalid",
    )
    buffer = common.BufferDescriptor(
        kind="video_frame",
        buffer_id="rejected",
        stream_id=source.stream_id,
        time_range=source.time_range,
        locator={"handoff_endpoint": "192.0.2.1:5000", "length": 4},
        format={"pixel_format": "RGBA", "width": 1, "height": 1},
    )
    reject(
        "cross_machine_buffer",
        args.measurement_release,
        [pb.PluginInput(buffer=buffer)],
        "data_locality_violation",
    )
    buffer.locator.handoff_endpoint = "127.0.0.1:1"
    buffer.locator.length = (64 << 20) + 1
    reject(
        "input_byte_limit",
        args.measurement_release,
        [pb.PluginInput(buffer=buffer)],
        "input_byte_limit_exceeded",
    )
    buffer.locator.length = 4
    buffer.format.width = 16385
    reject(
        "input_dimension_limit",
        args.measurement_release,
        [pb.PluginInput(buffer=buffer)],
        "input_dimension_limit_exceeded",
    )
    args.report.write_text(json.dumps(records, indent=2))
    print(json.dumps(records))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--observation", type=Path, required=True)
    parser.add_argument("--measurement-release", required=True)
    parser.add_argument("--threshold-release", required=True)
    parser.add_argument("--report", type=Path, required=True)
    verify(parser.parse_args())


if __name__ == "__main__":
    main()
