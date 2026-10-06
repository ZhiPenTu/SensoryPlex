"""M8 剩余（ADR-021）：模型 worker 的分级并发上限——真实插件与真实运行时上的验收。

ADR-015 把"模型 worker 的并发预算"写进统一内存分级表，ADR-019 让**运行时**消费了
`queue_capacity`，但 `SENSORYPLEX_MODEL_PARALLELISM` 一直只有"已声明"这一层：没有任何
执行点读它。本脚本验收 ADR-021 把它接到执行点之后的行为，结论全部来自真实进程与真实模型。

角色：
1. 本脚本（编排 + 对账）；
2. 真实上游 `ocr_blocks` 观测：每个 `--media` 跑一次**未改动**的 `tools/verify_ocr.py`
   真实链路（真实 replay → 真实 OCR 插件进程 → worker），取其中有文本的观测；
3. 真实 BGE 插件进程（`python -m edge_material_plugin_embed_bge_onnx`，Start 时真建 ONNX 会话）；
4. `tools/ai_worker.py --input-observations`：本次限流的**执行点**；
5. 真实 `sensoryplex-runtime serve`：分级上限的**权威**（`DescribeCapabilities.residency`）。

为什么上游要多个样本：`file-material` 流水线在文件回放时按 `min_interval_ms=1000` 抽帧，worker
又在**开始处理之前一次性 List** 保留表，所以单次运行只拿得到个位数的视频帧（实测 1～2 条）。
要造出"在飞 N 路"的重叠窗口，就得多跑几个授权样本、把它们**真实产出**的观测拼成一个批次——
本脚本不做任何复制或合成：每条观测都来自一次真实的 replay 加真实的 OCR 推理，id、锚点、
`contentHash` 都是那一轮的真实结果。

判定标准（全部为真实执行结果；未观测到的一律进"未验证"，不写成通过）：

- 默认什么都不注入：`not_injected` / `limit=1` / `source=none`，账目 `peak_in_flight == 1`；
- 请求值（`--model-parallelism N` 或 `SENSORYPLEX_MODEL_PARALLELISM=N`）：`admitted`，账目
  `peak_in_flight == min(limit, 输入数)`——上限真的落在 worker 侧，而不是只写了一个数字；
- 坏值 / 两个来源冲突 / 请求值超过分级上限：退出码 2、`state=rejected`，一条输入都没跑；
- 运行时是权威：`--runtime` 时按 `residency` 采纳上限（`source=runtime`，`tier`/`tier_capacity`
  如实转述，本脚本另外独立 `DescribeCapabilities` 读一次对账）；运行时不可达 → 显式失败，
  **不**退化成"没有上限"；Rust 与 Python 对同一个坏值给出逐字相同的原因串；
- 插件侧节流被**重试吸收**而不是让输入消失（ADR-021 的重试语义修正）：BGE 声明
  `maxConcurrency=1`，worker 在飞 3 路以上时后到的调用必然拿到可重试的 `concurrency_limit`；
  全部观测仍产出、`throttle_events.concurrency_limit > 0`、没有输入静默消失；
- 观测路径仍然没有数据面：`drain.leases == 0`、`runtime_stats_after.leases == 0`；
- 不外泄：报告与日志里没有权重目录、媒体路径或推流地址。
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins/python/common/src"))

import grpc  # noqa: E402
from edge_material_sdk.generated.runtime.v1 import (  # noqa: E402
    runtime_pb2,
    runtime_pb2_grpc,
)

from tools import model_limits, verify_ocr  # noqa: E402
from tools.verify_embed import (  # noqa: E402
    DEFAULT_MODEL_DIR,
    DEFAULT_MODEL_FILE,
    DEFAULT_MODEL_ID,
    DIGEST_PATTERN,
    INPUT_MODALITY,
    MAX_LENGTH,
    PLUGIN_MODULE,
    READY_TIMEOUT_S,
    RUN_TIMEOUT_S,
    SERVER_TTL_MS,
    manifest_digest,
    package_digest,
    real_weights,
)
from tools.verify_model import Process, check, free_port, runtime_binary  # noqa: E402

WORKER = ROOT / "tools/ai_worker.py"
# 一次 OCR 链路运行交付的上游帧数：`file-material` 的抽帧间隔决定它是个位数。
UPSTREAM_INPUTS_PER_MEDIA = 2
# 至少要 3 条，端到端样本里"在飞 N 路"这句话才成立（2 条最多只能证明 2 路）。
MIN_INPUTS = 3
DEFAULT_INPUTS = 4
RUNTIME_TIMEOUT_S = 60.0


def child_environment(extra: dict | None = None) -> dict:
    """子进程环境：先清掉宿主注入的 `SENSORYPLEX_*`，再由场景显式决定。

    否则"未注入"这个场景会被宿主环境偷偷改成别的语义——而"未注入"正是本 ADR 要区分的
    第一种事实（开发机上没有 `resident.env` 是常态）。
    """
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("SENSORYPLEX_")
    }
    environment.update(extra or {})
    return environment


def collect_upstream(
    media: list[pathlib.Path],
    workspace: pathlib.Path,
    ocr_model_dir: str,
    needed: int,
) -> list[dict]:
    """按顺序跑真实 OCR 链路，凑够 `needed` 条**有文本的**真实观测。

    某一轮没交出文本（或上游自己的夹具断言不成立）时跳过它继续下一个样本：观测是上游事实，
    本目标不为它编造，也不把上游的夹具断言算成自己的失败。
    """
    selected: list[dict] = []
    for index, item in enumerate(media):
        if len(selected) >= needed:
            break
        run_workspace = workspace / f"ocr-{index}"
        run_workspace.mkdir(parents=True, exist_ok=True)
        failures = verify_ocr.verify(
            item, run_workspace, ocr_model_dir, "cpu", UPSTREAM_INPUTS_PER_MEDIA, "text"
        )
        report_path = run_workspace / "ai-worker.json"
        document = json.loads(report_path.read_text()) if report_path.is_file() else {}
        produced = document.get("observations", [])
        usable = [entry for entry in produced if (entry.get("payload") or {}).get("blocks")]
        print(
            f"upstream[{index}] {item.name}: observations={len(produced)} "
            f"text_bearing={len(usable)}"
        )
        if failures:
            # 不隐藏：这一轮上游自己的夹具断言（例如"每一帧都该有文字"）不成立。
            for failure in failures:
                print(f"  upstream[{index}] fixture note: {failure}")
        if not usable:
            print(f"  upstream[{index}] skipped: 这一轮没有交出可用的文本观测")
        selected.extend(usable[: needed - len(selected)])
    return selected


def run_worker(
    context: dict, name: str, *, extra: list[str] | None = None, environment: dict | None = None
) -> tuple[int, str, dict]:
    """以独立进程跑一次 worker；返回（退出码，合并输出，报告）。"""
    report_path = context["workspace"] / f"{name}.json"
    command = [
        sys.executable,
        str(WORKER),
        "--plugin",
        context["plugin"],
        "--input-observations",
        str(context["upstream"]),
        "--plugin-config",
        str(context["config"]),
        # 必须显式给：`--max-inputs` 缺省时 worker 退回到 `--max-frames` 的 2。
        "--max-inputs",
        str(context["inputs"]),
        "--report",
        str(report_path),
        *(extra or []),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_S,
        env=environment if environment is not None else child_environment(),
    )
    document = json.loads(report_path.read_text()) if report_path.is_file() else {}
    return completed.returncode, (completed.stdout or "") + (completed.stderr or ""), document


def admitted_scenario(
    context: dict,
    name: str,
    *,
    expect_limit: int,
    expect_source: str,
    expect_state: str = "admitted",
    extra: list[str] | None = None,
    environment: dict | None = None,
    expect_tier: str | None = None,
    expect_capacity: int | None = None,
    expect_throttle: bool = False,
) -> dict:
    """一次通过准入的运行：准入结果、账目与产出必须同时自洽。

    在飞峰值不单独传参：worker 会同时提交所有输入，因此期望值就是 `min(limit, 输入数)`——
    它同时说明"上限没被突破"和"上限真的用到了"。
    """
    failures = context["failures"]
    inputs = context["inputs"]
    expected_peak = min(expect_limit, inputs)
    code, output, document = run_worker(context, name, extra=extra, environment=environment)
    account = document.get("model_concurrency") or {}
    check(code == 0, f"{name}: worker 退出码 {code}：{output[-600:]}", failures)
    check(account.get("state") == expect_state, f"{name}: state={account.get('state')}", failures)
    check(
        account.get("limit") == expect_limit,
        f"{name}: limit={account.get('limit')} != {expect_limit}",
        failures,
    )
    check(
        account.get("source") == expect_source,
        f"{name}: source={account.get('source')} != {expect_source}",
        failures,
    )
    if expect_tier is not None:
        check(
            account.get("tier") == expect_tier,
            f"{name}: tier={account.get('tier')} != {expect_tier}",
            failures,
        )
        check(
            account.get("tier_capacity") == expect_capacity,
            f"{name}: tier_capacity={account.get('tier_capacity')} != {expect_capacity}",
            failures,
        )
    # 提交数、完成数、观测数三者必须同时成立：跑漏了不能看起来像成功。
    check(
        account.get("submitted") == inputs,
        f"{name}: submitted={account.get('submitted')} != {inputs}",
        failures,
    )
    check(
        account.get("completed") == inputs,
        f"{name}: completed={account.get('completed')} != {inputs}",
        failures,
    )
    observations = document.get("observations") or []
    check(
        len(observations) == inputs,
        f"{name}: observations={len(observations)} != {inputs}",
        failures,
    )
    check(
        document.get("failures") == [],
        f"{name}: worker failures={document.get('failures')}",
        failures,
    )
    check(
        document.get("checks_failed") == 0,
        f"{name}: checks_failed={document.get('checks_failed')}",
        failures,
    )
    check(
        (document.get("drain") or {}).get("leases") == 0,
        f"{name}: 观测路径不该有 lease：{document.get('drain')}",
        failures,
    )
    check(
        (document.get("runtime_stats_after") or {}).get("leases") == 0,
        f"{name}: runtime_stats_after={document.get('runtime_stats_after')}",
        failures,
    )
    check(
        account.get("peak_in_flight") == expected_peak,
        f"{name}: peak_in_flight={account.get('peak_in_flight')} != {expected_peak}"
        f"（limit={expect_limit} 输入数={inputs}）",
        failures,
    )
    if expect_throttle:
        events = account.get("throttle_events") or {}
        check(
            int(events.get("concurrency_limit", 0)) > 0,
            f"{name}: 插件侧节流没有被观测到（throttle_events={events}）——"
            "这条证据必须来自真实重叠，不能靠断言编造",
            failures,
        )
    print(
        f"{name}: state={account.get('state')} limit={account.get('limit')} "
        f"source={account.get('source')} tier={account.get('tier')} "
        f"peak_in_flight={account.get('peak_in_flight')} retries={account.get('retries')} "
        f"throttle={account.get('throttle_events')}"
    )
    return document


def rejected_scenario(
    context: dict,
    name: str,
    *,
    expect_reason: str,
    extra: list[str] | None = None,
    environment: dict | None = None,
) -> None:
    """一次被拒的运行：配置错误必须与运行失败分开，而且不该跑起来。"""
    failures = context["failures"]
    code, output, document = run_worker(context, name, extra=extra, environment=environment)
    account = document.get("model_concurrency") or {}
    # 退出码 2 = 配置错误（不是"跑到一半失败"的 1），调用方才能把两类失败分开处理。
    check(code == 2, f"{name}: 配置错误应以退出码 2 结束，实际 {code}：{output[-600:]}", failures)
    check(account.get("state") == "rejected", f"{name}: state={account.get('state')}", failures)
    check(
        account.get("reason") == expect_reason,
        f"{name}: reason={account.get('reason')} != {expect_reason}",
        failures,
    )
    check(not document.get("frames"), f"{name}: 被拒的运行仍处理了输入", failures)
    check(not document.get("observations"), f"{name}: 被拒的运行仍产出了观测", failures)
    print(f"{name}: rejected reason={account.get('reason')}")


class RuntimeControl:
    """一个真实的 `sensoryplex-runtime serve`：分级上限的权威。"""

    def __init__(self, log_path: pathlib.Path, environment: dict):
        self.address = f"127.0.0.1:{free_port()}"
        self.log_path = log_path
        self.environment = child_environment(environment) | {
            "SENSORYPLEX_RUNTIME_ADDR": self.address
        }
        self.process: subprocess.Popen | None = None
        self._log = None

    def __enter__(self) -> "RuntimeControl":
        self._log = self.log_path.open("w")
        self.process = subprocess.Popen(
            [str(runtime_binary()), "serve"],
            env=self.environment,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return self

    def describe(self, timeout: float = RUNTIME_TIMEOUT_S):
        """独立读一次 `DescribeCapabilities`：worker 报告里的分级值必须与这里同一份事实对上。"""
        deadline = time.monotonic() + timeout
        with grpc.insecure_channel(self.address) as channel:
            stub = runtime_pb2_grpc.RuntimeServiceStub(channel)
            while time.monotonic() < deadline:
                if self.process is not None and self.process.poll() is not None:
                    raise AssertionError(f"runtime serve 提前退出：{self.output()[-600:]}")
                try:
                    return stub.DescribeCapabilities(
                        runtime_pb2.DescribeCapabilitiesRequest(), timeout=3
                    )
                except grpc.RpcError:
                    time.sleep(0.1)
        raise AssertionError("runtime 控制端点始终没有就绪")

    def output(self) -> str:
        try:
            return self.log_path.read_text()
        except OSError:
            return ""

    def __exit__(self, *_exception) -> bool:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self._log is not None:
            self._log.close()
        return False


def verify_no_leakage(
    report: dict,
    media: list[pathlib.Path],
    model_directory: str,
    plugin: Process,
    failures: list[str],
) -> None:
    blob = json.dumps(report)
    needles = [
        ("streaming uri", "srt://"),
        ("streaming uri", "rtmp://"),
        ("model directory", str(model_directory)),
    ]
    for item in media:
        needles += [
            (f"media file name ({item.name})", item.name),
            ("media absolute path", str(item)),
        ]
    for label, needle in needles:
        check(needle not in blob, f"worker report leaks {label}", failures)
    plugin_output = "\n".join(plugin.lines + plugin.stderr)
    for item in media:
        check(str(item) not in plugin_output, "plugin output leaks the media path", failures)


def verify(
    media: list[pathlib.Path],
    workspace: pathlib.Path,
    model_dir: str,
    model_file: str,
    provider: str,
    inputs: int,
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
    directory, _digests, _sizes, model_digest, dimension = real_weights(model_dir, model_file)
    print(f"weights: {directory} dimension={dimension} combined={model_digest}")

    selected = collect_upstream(media, workspace, ocr_model_dir, inputs)
    for item in selected:
        check(
            item.get("modality") == INPUT_MODALITY,
            f"upstream observation is not {INPUT_MODALITY}: {item.get('modality')}",
            failures,
        )
        check(
            bool((item.get("payload") or {}).get("blocks")),
            f"upstream observation {item.get('observationId')} carries no text block",
            failures,
        )
        check(
            bool(DIGEST_PATTERN.match(str(item.get("contentHash", "")))),
            f"upstream observation {item.get('observationId')} has no real content hash",
            failures,
        )
    identifiers = [str(item.get("observationId")) for item in selected]
    check(
        len(set(identifiers)) == len(identifiers),
        f"上游观测 id 重复（同一批次里不该有两条同 id 的输入）：{identifiers}",
        failures,
    )
    check(
        len(selected) >= MIN_INPUTS,
        f"并发验收至少需要 {MIN_INPUTS} 条真实上游观测才造得出重叠窗口，实际 {len(selected)} 条"
        "（多给几个 --media：一个样本一次真实 OCR 链路只交付个位数帧）",
        failures,
    )
    if failures:
        return failures
    print(f"upstream batch: {len(selected)} real observations")

    upstream_path = workspace / "upstream-observations.json"
    upstream_path.write_text(
        json.dumps({"observations": selected}, indent=2, sort_keys=True) + "\n"
    )
    config_path = workspace / "plugin-config.json"
    config_path.write_text(
        json.dumps(
            {
                "provider": provider,
                "model_dir": str(directory),
                "model_file": model_file,
                "model_id": DEFAULT_MODEL_ID,
                "max_length": MAX_LENGTH,
                "ttl_ms": SERVER_TTL_MS,
                "timeout_s": RUN_TIMEOUT_S,
            },
            indent=2,
        )
        + "\n"
    )

    plugin_address = f"127.0.0.1:{free_port()}"
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
    try:
        ready = plugin.wait_for("plugin ready", timeout=READY_TIMEOUT_S)
        check(
            ready.get("artifact_digest") == expected_digest,
            f"plugin started with a different artifact digest: {ready}",
            failures,
        )
        count = len(selected)
        context = {
            "workspace": workspace,
            "plugin": plugin_address,
            "upstream": upstream_path,
            "config": config_path,
            "inputs": count,
            "failures": failures,
        }
        reports = {
            # 什么都不注入：`not_injected` 是事实，不是"某一档"，串行语义与改动前一致。
            "not_injected": admitted_scenario(
                context,
                "not_injected",
                expect_state="not_injected",
                expect_limit=1,
                expect_source="none",
            ),
            "flag_1": admitted_scenario(
                context,
                "flag_1",
                expect_limit=1,
                expect_source="flag",
                extra=["--model-parallelism", "1"],
            ),
            # 请求值 = 输入数：期望峰值就是全部输入同时在飞。它同时说明上限没被突破、
            # 也没被"保守地"压成串行。
            "flag_all": admitted_scenario(
                context,
                "flag_all",
                expect_limit=count,
                expect_source="flag",
                extra=["--model-parallelism", str(count), "--max-attempts", "4"],
                expect_throttle=True,
            ),
            "env_all": admitted_scenario(
                context,
                "env_all",
                expect_limit=count,
                expect_source="env",
                environment=child_environment({model_limits.MODEL_PARALLELISM_ENV: str(count)}),
                extra=["--max-attempts", "4"],
                expect_throttle=True,
            ),
            # 明确请求 2 路：在飞数必须正好是 2，既不是 1（串行）也不是输入数（无上限）。
            "flag_2": admitted_scenario(
                context,
                "flag_2",
                expect_limit=2,
                expect_source="flag",
                extra=["--model-parallelism", "2"],
            ),
        }
        name = model_limits.MODEL_PARALLELISM_ENV
        rejected_scenario(
            context,
            "rejected_zero",
            expect_reason=f"invalid_resident_limit: {name}=0",
            environment=child_environment({name: "0"}),
        )
        rejected_scenario(
            context,
            "rejected_empty",
            expect_reason=f"invalid_resident_limit: {name} is set but empty",
            environment=child_environment({name: ""}),
        )
        rejected_scenario(
            context,
            "rejected_garbage",
            expect_reason=f"invalid_resident_limit: {name}=abc",
            environment=child_environment({name: "abc"}),
        )
        rejected_scenario(
            context,
            "rejected_flag_garbage",
            expect_reason=f"invalid_resident_limit: {model_limits.MODEL_PARALLELISM_FLAG}=abc",
            extra=["--model-parallelism", "abc"],
        )
        # 两个来源都在却不一样：拒绝，而不是静默挑一个。
        rejected_scenario(
            context,
            "rejected_conflict",
            expect_reason="model_parallelism_conflict: env=3 flag=2",
            extra=["--model-parallelism", "2"],
            environment=child_environment({name: "3"}),
        )

        # ── 运行时是分级上限的权威 ────────────────────────────────────────────
        large = {
            "SENSORYPLEX_RESIDENT_TIER": "large",
            "SENSORYPLEX_MEDIA_QUEUE_CAPACITY": "64",
            name: "3",
        }
        with RuntimeControl(workspace / "runtime-large.log", large) as runtime:
            residency = runtime.describe().residency
            check(
                residency.tier == "large",
                f"runtime 转述的档位不是 large：{residency.tier}",
                failures,
            )
            check(
                residency.model_parallelism == 3,
                f"runtime 转述的模型并发不是 3：{residency.model_parallelism}",
                failures,
            )
            reports["runtime_authority"] = admitted_scenario(
                context,
                "runtime_authority",
                expect_limit=3,
                expect_source="runtime",
                extra=["--runtime", runtime.address],
                expect_tier="large",
                expect_capacity=3,
            )
            reports["runtime_flag_within_cap"] = admitted_scenario(
                context,
                "runtime_flag_within_cap",
                expect_limit=2,
                expect_source="flag",
                extra=["--runtime", runtime.address, "--model-parallelism", "2"],
                expect_tier="large",
                expect_capacity=3,
            )
            rejected_scenario(
                context,
                "runtime_rejects_over_cap",
                expect_reason=(
                    "model_parallelism_exceeds_tier_cap: requested=8 tier_capacity=3 tier=large"
                ),
                extra=["--runtime", runtime.address, "--model-parallelism", "8"],
            )
        # worker 里没有任何分级表：它转述运行时说了什么。换一档就必须看到那一档的数字。
        medium = {
            "SENSORYPLEX_RESIDENT_TIER": "medium",
            "SENSORYPLEX_MEDIA_QUEUE_CAPACITY": "16",
            name: "2",
        }
        with RuntimeControl(workspace / "runtime-medium.log", medium) as runtime:
            admitted_scenario(
                context,
                "runtime_medium_transcribed",
                expect_limit=2,
                expect_source="runtime",
                extra=["--runtime", runtime.address],
                expect_tier="medium",
                expect_capacity=2,
            )
        # 连不上运行时不能退化成"没有上限"：那正是这条链要禁止的静默降级。
        rejected_scenario(
            context,
            "runtime_unreachable",
            expect_reason="runtime_capabilities_unavailable:UNAVAILABLE",
            extra=["--runtime", f"127.0.0.1:{free_port()}"],
        )

        # `serve` 读的是**进程环境**（没有 CLI 开关），坏值必须让它起不来，且原因串与
        # `tools/model_limits.py` 逐字相同——同一批注入值在两边不能有两套语义。
        unusable = subprocess.run(
            [str(runtime_binary()), "serve"],
            env=child_environment({name: "abc"}),
            capture_output=True,
            text=True,
            timeout=RUNTIME_TIMEOUT_S,
        )
        try:
            model_limits.parse_limit("abc")
            python_message = ""
        except model_limits.ResidentLimitError as error:
            python_message = str(error)
        runtime_output = (unusable.stdout or "") + (unusable.stderr or "")
        check(
            unusable.returncode != 0,
            f"坏的分级值必须让 serve 起不来，实际退出码 {unusable.returncode}",
            failures,
        )
        check(
            bool(python_message) and python_message in runtime_output,
            f"Rust 与 Python 对同一个坏值给出不同原因串：python={python_message!r} "
            f"rust={runtime_output[-400:]!r}",
            failures,
        )
        print(f"bad_resident_limit_parity: rust==python: {python_message}")

        verify_no_leakage(reports["flag_all"], media, str(directory), plugin, failures)
    finally:
        plugin.kill()
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--media",
        type=pathlib.Path,
        action="append",
        required=True,
        help="真实授权样本，可给多次；每个样本跑一次真实 OCR 链路取上游观测",
    )
    parser.add_argument("--model-dir", default=str(DEFAULT_MODEL_DIR))
    parser.add_argument(
        "--model-file",
        default=DEFAULT_MODEL_FILE,
        help="model_dir 内实际加载的 ONNX 相对路径（默认 HF 快照的 onnx/model_quantized.onnx）",
    )
    parser.add_argument("--ocr-model-dir", default="")
    parser.add_argument("--provider", choices=["cpu", "coreml"], default="cpu")
    parser.add_argument("--inputs", type=int, default=DEFAULT_INPUTS)
    parser.add_argument("--keep-workspace", action="store_true")
    arguments = parser.parse_args()
    for item in arguments.media:
        if not item.is_file():
            raise SystemExit(f"media not found: {item}")

    workspace = pathlib.Path(tempfile.mkdtemp(prefix="sensoryplex-parallelism-"))
    print(f"workspace: {workspace}{' (kept)' if arguments.keep_workspace else ''}")
    print(f"host: {os.uname().nodename} worker_timeout_s={RUN_TIMEOUT_S}")
    started = time.monotonic()
    failures = verify(
        arguments.media,
        workspace,
        arguments.model_dir,
        arguments.model_file,
        arguments.provider,
        arguments.inputs,
        arguments.ocr_model_dir,
    )
    if failures:
        print("\nmodel parallelism acceptance FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(
        f"\nmodel parallelism acceptance: the admitted limit reaches the worker's in-flight "
        f"plugin calls, the runtime is the authority on the cap, and plugin-side throttling is "
        f"retried instead of dropping inputs ({round(time.monotonic() - started, 1)}s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
