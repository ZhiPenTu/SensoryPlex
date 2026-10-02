"""Console v2 的本机受控任务执行器。

它只消费 Node Agent 已领取的 `orchestrated_v2` intent：从 API 取得不可变 Task
manifest、从受认证数据面下载本次媒体、从本机 ADR-030 热部署台账读取 active endpoint，
再调用 Runtime 与插件。它不读取数据库、不接受宿主路径/命令/URL，也不调用 legacy
`task_runner.py` 或 `task_worker.py`。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import grpc
from edge_material_sdk.buffer_reader import LeaseBufferReader, is_loopback_endpoint
from edge_material_sdk.generated.common.v1 import common_pb2 as common
from edge_material_sdk.generated.material.v1 import material_pb2
from edge_material_sdk.generated.media.v1 import handoff_pb2, handoff_pb2_grpc, media_pb2
from edge_material_sdk.generated.orchestration.v1 import orchestration_pb2
from edge_material_sdk.generated.runtime.v1 import runtime_pb2, runtime_pb2_grpc
from google.protobuf import json_format
from google.protobuf.message import DecodeError

from tools.node_agent_platform import HotDeployError, read_endpoint_file

ROOT = Path(__file__).resolve().parents[1]
# 语义覆盖把"输入单位"从单帧变成**有界的一组帧**：VLM 的证据窗口是 pre+anchor+post
# 最多 9 帧，OCR 每个锚点一帧。上限按"请求数 × 每请求帧数"两重界定，任何一条被突破
# 都是显式失败，不再是静默截断。
MAX_MODEL_REQUESTS_PER_TASK = 200_000
MAX_FRAMES_PER_GROUP = 9
RUNTIME_WAIT_S = 300.0
RESULT_BODY_LIMIT_HINT = 8_000_000
# 全帧判别给音频节点只留很小的保留面：它不消费视频帧，不需要为整段视频的
# 证据窗口预留共享内存。
AUDIO_ONLY_RETENTION_BYTES = 128 * 1024**2


ANCHOR_SELECTIONS = {"baseline_anchor", "event_anchor"}
BASELINE_ANCHOR_SELECTION = "baseline_anchor"
# 数据面里的 buffer 种类：节点只消费其中一种，另一种同样占着有界保留面。
VIDEO_FRAME_KIND = "video_frame"
AUDIO_SEGMENT_KIND = "audio_segment"
# 一帧被有界保留面拒绝时的稳定原因码：与 Runtime 账本里的 skip_reason 同字面量。
RETENTION_REJECTED = "data_plane_retention_rejected"
# 逐帧账本的轮询间隔：解码还在进行时，消费者必须按窗口及时领料、及时归还，
# 否则有界保留面（arena 字节 + 保留条数）在长媒体上必然被顶满。
LEDGER_POLL_INTERVAL_S = 0.02
# 音频节点边解码边转写；没有待消费段时短暂等待，避免无意义的轮询风暴。
AUDIO_PLANE_POLL_INTERVAL_S = 0.25
# 视频节点在"这一轮没有新窗口"时的清扫间隔：它同时充当消费者心跳，并把本节点不消费的
# kind 有节奏地还回去。两次清扫之间没有新的保留进来时，重复 List 只是徒增 RPC。
PLANE_SWEEP_INTERVAL_S = 0.25
# 账本长时间没有任何新记录、生产者又没有退出：这是显式失败，不是"继续等下去"。
RUNTIME_STALL_S = 300.0
# 数据面里"账本说交接成功、保留表却已经没有这一帧"的失败是可重试的传输层失败：
# 它既不是"画面没变化"，也不是这个插件或这条 Task 的确定性错误。
RETRYABLE_PLANE_FAILURES = {"data_plane_buffer_missing"}


class TaskExecutionError(RuntimeError):
    """带稳定 reason code 的执行器错误，正文不应包含路径、令牌或媒体内容。"""

    def __init__(
        self,
        reason_code: str,
        *,
        retryable: bool = False,
        inputs: int = 0,
        outputs: int = 0,
        result_ref: str = "",
    ):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.retryable = retryable
        self.inputs = inputs
        self.outputs = outputs
        self.result_ref = result_ref


@dataclass(frozen=True)
class PluginEndpoint:
    endpoint: str
    plugin_id: str
    artifact_digest: str
    config_hash: str


def _now_ms() -> int:
    return int(time.time() * 1000)


def _sha256_json(value: dict[str, Any]) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _runtime_binary() -> Path:
    configured = os.environ.get("SENSORYPLEX_RUNTIME_BIN", "")
    if configured:
        binary = Path(configured)
    elif Path("/Users/tuzhipeng/Documents/SensoryPlex/target/release/sensoryplex-runtime").is_file():
        binary = Path("/Users/tuzhipeng/Documents/SensoryPlex/target/release/sensoryplex-runtime")
    else:
        binary = ROOT / "target/release/sensoryplex-runtime"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise TaskExecutionError("runtime_binary_unavailable")
    return binary


def _source_identity(manifest: dict) -> tuple[str, str]:
    """媒体身份由内容摘要派生，与 Runtime 的 probe 同一口径。

    摘要就是这次任务下载并校验过的媒体摘要（`_download_asset` 已经比对过），因此
    `source_id`/`stream_id` 不必等报告落盘就能用于请求上下文；收尾时再与报告对账，
    防止"下载的文件"与"Runtime 解码的文件"不是同一份。
    """
    digest = str(manifest.get("asset", {}).get("content_hash") or "")
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise TaskExecutionError("task_asset_digest_invalid")
    short = digest[7:19]
    return f"file-{short}", f"stream-{short}"


def _bounded_policy(policy: object) -> dict[str, int]:
    """把不可变 Revision 的策略夹进实现真正支持的范围；越界即拒绝，不做夹取。

    语义覆盖（evidence）那一组是**全帧判别 + 事件证据窗口**的参数，不是抽帧间隔：
    `evidence_max_gap_ms` 是静态画面的语义刷新上界，窗口上下文决定下游看到的前后帧数。
    `vlm_sample_interval_ms` 只为兼容旧 Revision 保留，不再决定 VLM 的输入集合。
    """
    if not isinstance(policy, dict):
        raise TaskExecutionError("task_execution_policy_missing")
    values = {}
    for key, default, lower, upper in (
        ("window_ms", 1_000, 1_000, 1_000),
        ("sample_interval_ms", 1_000, 250, 60_000),
        ("audio_segment_ms", 6_000, 1_000, 60_000),
        ("audio_overlap_ms", 500, 0, 59_000),
        ("vlm_sample_interval_ms", 5_000, 1_000, 60_000),
        ("evidence_max_gap_ms", 1_000, 100, 60_000),
        ("evidence_context_before", 2, 0, 4),
        ("evidence_context_after", 2, 0, 4),
        ("evidence_change_threshold", 8, 1, 128),
        ("evidence_text_change_threshold", 12, 1, 255),
        ("evidence_min_event_interval_ms", 100, 20, 5_000),
        ("evidence_retention_bytes", 2 * 1024**3, 64 * 1024**2, 8 * 1024**3),
    ):
        try:
            value = int(policy.get(key, default))
        except (TypeError, ValueError) as error:
            raise TaskExecutionError("task_execution_policy_invalid") from error
        if value < lower or value > upper:
            raise TaskExecutionError("task_execution_policy_invalid")
        values[key] = value
    if values["audio_overlap_ms"] >= values["audio_segment_ms"]:
        raise TaskExecutionError("task_execution_policy_invalid")
    if (
        values["evidence_context_before"] + values["evidence_context_after"] + 1
        > MAX_FRAMES_PER_GROUP
    ):
        raise TaskExecutionError("task_execution_policy_invalid")
    return values


def _runtime_pipeline(policy: dict[str, int], *, has_vlm: bool) -> str:
    """把已签发 Revision 的策略渲染为本次 Runtime 私有输入。

    这不是读取/改写全局 `file-material.yaml`。所有动态数值都来自不可变 Revision manifest，
    生成文件只位于 Agent 私有工作区，Runtime 结束后可与本次 receipt 一起审计。
    """
    enrichment_modalities = "\n      - vision.scene_description" if has_vlm else " []"
    slow_enrichment = (
        """  slow_enrichment:
    - type: vlm
      model: revision-bound
"""
        if has_vlm
        else "  slow_enrichment: []\n"
    )
    return f"""apiVersion: edge.material/v1
kind: Pipeline
metadata:
  name: console-orchestrated-v2
spec:
  source:
    type: file
    uri_secret_ref: AGENT_CONTROLLED_ASSET
  queue_capacity: 32
  data_egress: local_only
  processors:
    - type: adaptive_sampler
    - type: audio_segmenter
    - type: asr
      model: revision-bound
    - type: ocr
      model: revision-bound
    - type: timeline_fusion
{slow_enrichment}  sinks:
    - type: metadata_store
  timeline_fusion:
    window_ms: {policy["window_ms"]}
    fast_modalities:
      - asr_segment
      - ocr_blocks
    enrichment_modalities:{enrichment_modalities}
"""


def _receipt(
    manifest: dict,
    *,
    started_ms: int,
    completed_ms: int,
    inputs: int,
    outputs: int,
    reason: str,
    result_ref: str,
) -> dict:
    task = manifest["task"]
    plugin = manifest["plugin"]
    data = {
        "run_id": task["run_id"] if "run_id" in task else manifest["run"]["run_id"],
        "task_id": task["task_id"],
        "attempt": int(task["attempt"]),
        "assignment_id": task["assignment_id"],
        "plugin_id": plugin["plugin_id"],
        "artifact_digest": plugin["artifact_digest"],
        "config_hash": plugin["config_hash"],
        "input_count": int(inputs),
        "output_count": int(outputs),
        "result_manifest_ref": result_ref,
        "reason_code": reason,
        "started_at": datetime.fromtimestamp(started_ms / 1000, tz=UTC),
        "completed_at": datetime.fromtimestamp(completed_ms / 1000, tz=UTC),
    }
    digest_fields = {
        **{key: value for key, value in data.items() if key not in {"started_at", "completed_at"}},
        "started_at": data["started_at"].isoformat(),
        "completed_at": data["completed_at"].isoformat(),
    }
    data["receipt_digest"] = _sha256_json(digest_fields)
    return {
        **{key: value for key, value in data.items() if key not in {"started_at", "completed_at"}},
        "started_at_unix_ms": started_ms,
        "completed_at_unix_ms": completed_ms,
    }


@dataclass(frozen=True)
class _InputGroup:
    """一次 `Process` 请求的输入单位：要么是一个证据窗口，要么是一个锚点帧。"""

    label: str
    trigger: str
    frame_ids: tuple[str, ...]
    # 窗口里真正触发它的锚点帧。锚点是窗口的语义主体，缺了它整窗都不构成证据。
    anchor_id: str = ""


@dataclass(frozen=True)
class _RejectedUnit:
    """被有界数据面拒绝、因此没有送模型的输入单位。

    它必须被显式登记：数据面拒绝不是"画面没变化"，也不是"计划本来就不覆盖这里"。
    `frames` 保存这一单位在账本里的帧记录（只有时间区间与原因码，没有字节），供下游把对应
    秒格标成 `not_observed`，而不是留一片无法解释的空白。
    """

    kind: str  # anchor | window
    label: str
    trigger: str
    frames: tuple[dict, ...]
    # 单位里**已经**交接成功的帧。它们不能作为证据消费，必须立刻归还给数据面，
    # 否则一份用不上的证据会一直占着保留表，把后面的帧挤成新的拒绝。
    orphan_ids: tuple[str, ...] = ()
    reason: str = RETENTION_REJECTED


@dataclass(frozen=True)
class _EvidencePlan:
    """Runtime 全帧判别的可复核产物：锚点、证据窗口与逐帧账本口径。

    `anchors` / `windows` 是账本口径的单位（与模态无关），真正送给插件的单位由
    `units()` 按模态挑出；被数据面拒绝的单位单独成列，既不进输入分组也不被静默丢弃。
    """

    anchors: tuple[_InputGroup, ...]
    windows: tuple[_InputGroup, ...]
    refused: tuple[_RejectedUnit, ...]
    characterized_frames: int
    covered_without_model_refresh: int
    max_selected_gap_ms: int
    ledger_path: str
    ledger_entries: int

    def units(self, *, has_vlm: bool) -> tuple[list[_InputGroup], list[_RejectedUnit], list[str]]:
        """按模态取输入单位、被拒单位，以及必须立刻归还的数据面 buffer。"""
        return _modality_view(
            list(self.anchors),
            list(self.windows),
            list(self.refused),
            has_vlm=has_vlm,
        )

    @property
    def window_records(self) -> int:
        """账本里的窗口条数：已消费的窗口，加上整窗被数据面拒绝的窗口。"""
        return len(self.windows) + sum(1 for unit in self.refused if unit.kind == "window")


def _modality_view(
    anchors: list[_InputGroup],
    windows: list[_InputGroup],
    refused: list[_RejectedUnit],
    *,
    has_vlm: bool,
) -> tuple[list[_InputGroup], list[_RejectedUnit], list[str]]:
    """把账本口径的单位按模态挑成执行器真正要跑的单位。

    增量流（解码进行中）与收尾对账（完整账本）共用这一条规则，因此"流式跑过的单位"与
    "完整计划里的单位"可以逐项比较：任何一边漏了或多了都是契约漂移。
    """
    if has_vlm:
        # VLM 要的是上下文：窗口整窗消费。事件锚点已经在自己窗口里，只有保证静态画面
        # 刷新上界的基线锚点单独成组（已交接的锚点不可能再被后来的窗口引用）。
        standalone = [unit for unit in anchors if unit.trigger == "baseline_refresh"]
        groups = [*windows, *standalone]
        # 事件锚点被拒时它所在的窗口同时被判为拒绝（见 `_window_units`），不重复登记。
        refused_units = [unit for unit in refused if unit.trigger != "anchor"]
        discards = [buffer_id for unit in refused_units for buffer_id in unit.orphan_ids]
        return groups, refused_units, discards
    # OCR 要的是文字：锚点各一帧。窗口里的前后文对文字识别没有增量，
    # 但它们同样占着保留表，因此必须立刻归还给数据面。
    groups = [
        _InputGroup(label=unit.frame_ids[0], trigger="anchor", frame_ids=unit.frame_ids)
        for unit in anchors
    ]
    discards = [
        buffer_id
        for window in windows
        for buffer_id in window.frame_ids
        if buffer_id != window.anchor_id
    ]
    refused_units = [unit for unit in refused if unit.kind == "anchor"]
    discards.extend(buffer_id for unit in refused for buffer_id in unit.orphan_ids)
    return groups, refused_units, discards


def _anchor_units(records: Iterable[dict]) -> tuple[list[_InputGroup], list[_RejectedUnit]]:
    """把锚点帧记录翻译成输入单位。

    没有 `buffer_id` 的锚点是被数据面拒绝的单位，不是"这里没有锚点"：它仍然要出现在
    拒绝清单里，让下游把对应秒格标成 `not_observed`。
    """
    groups: list[_InputGroup] = []
    refused: list[_RejectedUnit] = []
    for record in records:
        selection = str(record.get("selection") or "")
        if selection not in ANCHOR_SELECTIONS:
            continue
        trigger = "baseline_refresh" if selection == BASELINE_ANCHOR_SELECTION else "anchor"
        buffer_id = str(record.get("buffer_id") or "")
        if buffer_id:
            groups.append(_InputGroup(label=buffer_id, trigger=trigger, frame_ids=(buffer_id,)))
        else:
            refused.append(
                _RejectedUnit(
                    kind="anchor",
                    label=f"anchor-{int(record.get('frame_index', 0)):08}",
                    trigger=trigger,
                    frames=(record,),
                )
            )
    return groups, refused


def _window_units(
    record: dict, frames: Sequence[dict]
) -> tuple[_InputGroup | None, _RejectedUnit | None]:
    """把一个窗口记录翻译成输入单位；锚点帧缺失时整窗拒绝。

    窗口的语义是"变化前 → 锚点 → 变化后"：锚点帧自己没进数据面，剩下几张上下文帧拼不出
    这个变化，因此整窗记为被数据面拒绝，并把已经交接的上下文帧登记为待归还。
    """
    window_id = str(record.get("window_id") or "")
    trigger = str(record.get("trigger") or "")
    frame_ids = tuple(str(value) for value in record.get("frame_buffer_ids") or [])
    anchor_id = next(
        (
            str(frame.get("buffer_id") or "")
            for frame in frames
            if str(frame.get("selection") or "") == "event_anchor"
            and str(frame.get("buffer_id") or "")
        ),
        "",
    )
    if frame_ids and anchor_id:
        return (
            _InputGroup(label=window_id, trigger=trigger, frame_ids=frame_ids, anchor_id=anchor_id),
            None,
        )
    return (
        None,
        _RejectedUnit(
            kind="window",
            label=window_id,
            trigger=trigger,
            frames=tuple(frames),
            orphan_ids=frame_ids,
        ),
    )


def _refused_records(unit: _RejectedUnit) -> list[dict]:
    """被拒单位的帧记录：下游据此把这些秒格标成 `not_observed:data_plane_retention_rejected`。

    描述符字段必须为空：被数据面拒绝的帧**从来没有** Runtime 签发的 `buffer_id` 与内容摘要，
    所以这里不能写 `buffer_id: ""` / `source_digest: ""`。Timeline 的账本构建会把
    「buffer_id + 摘要 + 窗口」三者同时在场视为一条描述符引用，空摘要会被判成
    `worker_report_invalid_digest`，让整条 Task 直接失败——那是把"诚实降级"又变回了硬失败。
    时间范围保留下来，覆盖层才认得出这一段是"计划刷过、但没送进模型"。
    """
    return [
        {
            "buffer_id": None,
            "kind": "video_frame",
            "source_digest": None,
            "source_time_range_ms": [
                int(frame.get("start_ms", 0)),
                int(frame.get("end_ms", 0)),
            ],
            "evidence_group": unit.label,
            "evidence_trigger": unit.trigger,
            "error": {"reason": unit.reason, "retryable": False},
        }
        for frame in unit.frames
    ]


def _unit_signature(units: Sequence[_InputGroup | _RejectedUnit]) -> list[tuple]:
    """单位的稳定比较键：标签、触发原因与它引用的帧序号。

    流式消费与完整计划必须逐项相等，因此比较用的是账本里的帧序号，而不是可能变动的
    `buffer_id` 顺序。
    """
    return sorted(
        (
            unit.label,
            unit.trigger,
            tuple(int(frame.get("frame_index", 0)) for frame in unit.frames)
            if isinstance(unit, _RejectedUnit)
            else tuple(unit.frame_ids),
        )
        for unit in units
    )


def _read_frame_ledger(path: Path) -> tuple[list[dict], list[dict]]:
    """读取逐帧账本；完整账本必须逐帧完整，缺行就是"输入完整性"无法成立。

    返回按落账顺序排列的帧记录与窗口记录：未被选中的帧没有 `buffer_id`，但它们同样必须
    有记录，所以"账本完整"与"有多少条交接引用"是两件事。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise TaskExecutionError("evidence_ledger_unreadable") from error
    frames: list[dict] = []
    windows: list[dict] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        record = _parse_ledger_line(line)
        if record.get("record_type") == "frame":
            frames.append(record)
        elif record.get("record_type") == "window":
            windows.append(record)
        else:
            raise TaskExecutionError("evidence_ledger_unreadable")
    return frames, windows


def _parse_ledger_line(line: str) -> dict:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        raise TaskExecutionError("evidence_ledger_unreadable") from None
    if not isinstance(record, dict):
        raise TaskExecutionError("evidence_ledger_unreadable")
    return record


class _FrameLedgerTail:
    """逐帧账本的增量读取器：只解析完整行，写了一半的最后一行留到下一次。

    账本由生产者在解码过程中一行一行落盘（`JsonlLedgerSink` 每行 flush），消费者因此可以
    在解码还在进行时就读到它；读到半行就当成"坏账本"会让并发消费永远失败。
    """

    def __init__(self, path: Path):
        self._path = path
        self._offset = 0
        self._pending = b""

    def poll(self) -> tuple[tuple[dict, ...], tuple[dict, ...]]:
        try:
            with self._path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except FileNotFoundError:
            # 账本由 Runtime 在解码前创建；还没创建就是"这次还没有记录"，不是错误。
            return (), ()
        except OSError as error:
            raise TaskExecutionError("evidence_ledger_unreadable") from error
        buffer = self._pending + chunk
        cut = buffer.rfind(b"\n")
        if cut < 0:
            self._pending = buffer
            return (), ()
        complete = buffer[: cut + 1]
        pending = buffer[cut + 1 :]
        try:
            text = complete.decode("utf-8")
        except UnicodeDecodeError:
            raise TaskExecutionError("evidence_ledger_unreadable") from None
        frames: list[dict] = []
        windows: list[dict] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            record = _parse_ledger_line(line)
            kind = record.get("record_type")
            if kind == "frame":
                frames.append(record)
            elif kind == "window":
                windows.append(record)
            else:
                raise TaskExecutionError("evidence_ledger_unreadable")
        self._offset += len(complete)
        self._pending = pending
        return tuple(frames), tuple(windows)


class _EvidenceStream:
    """把运行中的逐帧账本增量翻译成执行计划。

    契约来自 Runtime 的落账顺序（`drain_ready` + `drain_emitted_windows`）：帧记录按
    `frame_index` 连续落账，窗口记录只在它引用的**全部**帧落账之后才出现。因此读到窗口记录
    就意味着可以立刻领料，而不必等整段解码结束——有界保留面（arena 字节 + 保留条数）正是
    靠这一点才不会被长媒体顶满，拒绝也就不再是必然结局。
    """

    def __init__(self, path: Path, *, has_vlm: bool):
        self._tail = _FrameLedgerTail(path)
        self._has_vlm = has_vlm
        self._window_frames: dict[str, list[dict]] = {}
        # 账本口径的累计单位：收尾时与完整账本重算的计划逐项对账。
        self.anchors: list[_InputGroup] = []
        self.windows: list[_InputGroup] = []
        self.refused: list[_RejectedUnit] = []
        # 真正交出去的累计结果。
        self.groups: list[_InputGroup] = []
        self.rejected: list[_RejectedUnit] = []
        self.discards: list[str] = []
        self.frames_seen = 0
        self.windows_seen = 0

    def poll(self) -> tuple[list[_InputGroup], list[_RejectedUnit], list[str]]:
        frames, windows = self._tail.poll()
        for record in frames:
            self.frames_seen += 1
            window_id = str(record.get("window_id") or "")
            if window_id:
                self._window_frames.setdefault(window_id, []).append(record)
        anchors, refused = _anchor_units(frames)
        fresh_windows: list[_InputGroup] = []
        for record in windows:
            window_id = str(record.get("window_id") or "")
            group, unit = _window_units(record, tuple(self._window_frames.get(window_id, ())))
            self.windows_seen += 1
            if group is not None:
                fresh_windows.append(group)
            if unit is not None:
                refused.append(unit)
        self.anchors.extend(anchors)
        self.windows.extend(fresh_windows)
        self.refused.extend(refused)
        groups, rejected, discards = _modality_view(
            anchors, fresh_windows, refused, has_vlm=self._has_vlm
        )
        self.groups.extend(groups)
        self.rejected.extend(rejected)
        self.discards.extend(discards)
        return groups, rejected, discards


def _evidence_plan(report, ledger_path: Path) -> _EvidencePlan:
    """把 Runtime 的语义覆盖报告与逐帧账本合成执行计划。

    Runtime 是唯一给出"哪些帧被选中"的地方；执行器只消费它的结论，不重新采样、不猜窗口。
    报告只带窗口的有界预览，完整窗口表在账本里，因此计划一律以账本为准，并用报告里的
    聚合计数对账：对不上就是契约漂移，必须当场失败。
    """
    coverage = next(iter(report.decoded.semantic_coverage), None)
    if coverage is None:
        raise TaskExecutionError("evidence_plan_missing")
    frames, raw_windows = _read_frame_ledger(ledger_path)
    if len(frames) != coverage.characterized_frames:
        raise TaskExecutionError("evidence_ledger_incomplete")
    if len(raw_windows) != coverage.windows:
        raise TaskExecutionError("evidence_ledger_incomplete")
    ordered = sorted(frames, key=lambda item: int(item.get("frame_index", 0)))
    anchors, refused = _anchor_units(ordered)
    windows: list[_InputGroup] = []
    for record in raw_windows:
        window_id = str(record.get("window_id") or "")
        group, unit = _window_units(
            record, tuple(frame for frame in frames if frame.get("window_id") == window_id)
        )
        if group is not None:
            windows.append(group)
        if unit is not None:
            refused.append(unit)
    return _EvidencePlan(
        anchors=tuple(anchors),
        windows=tuple(windows),
        refused=tuple(refused),
        characterized_frames=int(coverage.characterized_frames),
        covered_without_model_refresh=int(coverage.covered_without_model_refresh),
        max_selected_gap_ms=int(coverage.max_selected_gap_ms),
        ledger_path=str(coverage.frame_ledger_path),
        ledger_entries=int(coverage.frame_ledger_entries),
    )


class TaskExecutor:
    """受控执行一条 v2 Task；业务失败也必须先提交 receipt，再完成 intent。"""

    def __init__(self, client, *, base_dir: Path):
        self.client = client
        self.base_dir = Path(base_dir)

    def execute(self, intent: dict[str, Any]) -> bool:
        intent_id = str(intent.get("intent_id", ""))
        if not intent_id:
            raise TaskExecutionError("task_intent_id_missing")
        manifest = self.client.task_manifest(intent_id)
        self._assert_intent_matches_manifest(intent, manifest)
        task = manifest["task"]
        started_ms = _now_ms()
        inputs, outputs, result_ref = 0, 0, ""
        try:
            if started_ms >= int(task["deadline_unix_ms"]):
                raise TaskExecutionError("task_deadline_exceeded", retryable=True)
            workspace = self._workspace(manifest)
            media_path = self._download_asset(manifest, workspace)
            if task["node_id"] == "timeline_fusion":
                inputs, outputs, result_ref = self._run_timeline(manifest, workspace, media_path)
            else:
                inputs, outputs, result_ref = self._run_plugin(manifest, workspace, media_path)
        except TaskExecutionError as error:
            completed_ms = _now_ms()
            self._report_result(
                manifest,
                intent_id=intent_id,
                success=False,
                retryable=error.retryable,
                reason=error.reason_code,
                inputs=getattr(error, "inputs", inputs),
                outputs=getattr(error, "outputs", outputs),
                started_ms=started_ms,
                completed_ms=completed_ms,
                result_ref=getattr(error, "result_ref", result_ref),
            )
            return True
        except subprocess.TimeoutExpired:
            completed_ms = _now_ms()
            self._report_result(
                manifest,
                intent_id=intent_id,
                success=False,
                retryable=False,
                reason="task_executor_timeout",
                inputs=inputs,
                outputs=outputs,
                started_ms=started_ms,
                completed_ms=completed_ms,
                result_ref=result_ref,
            )
            return True
        except Exception:  # noqa: BLE001 - 外部 Runtime/gRPC 错误不能泄露详情到控制面
            completed_ms = _now_ms()
            self._report_result(
                manifest,
                intent_id=intent_id,
                success=False,
                retryable=False,
                reason="task_executor_internal_error",
                inputs=inputs,
                outputs=outputs,
                started_ms=started_ms,
                completed_ms=completed_ms,
                result_ref=result_ref,
            )
            return True
        completed_ms = _now_ms()
        self._report_result(
            manifest,
            intent_id=intent_id,
            success=True,
            retryable=False,
            reason="",
            inputs=inputs,
            outputs=outputs,
            started_ms=started_ms,
            completed_ms=completed_ms,
            result_ref=result_ref,
        )
        return True

    def _assert_intent_matches_manifest(self, intent: dict[str, Any], manifest: dict) -> None:
        config = intent.get("config") or {}
        if config.get("execution_mode") != "orchestrated_v2":
            raise TaskExecutionError("agent_legacy_task_execution_unsupported")
        if (
            manifest.get("intent_id") != intent.get("intent_id")
            or manifest["execution_id"] != config.get("execution_id")
            or manifest["task"]["task_id"] != config.get("task_id")
            or manifest["task"]["assignment_id"] != config.get("assignment_id")
            or int(manifest["task"]["attempt"]) != int(config.get("attempt") or 0)
            or manifest["asset"]["content_hash"] != config.get("content_hash")
        ):
            raise TaskExecutionError("task_manifest_intent_mismatch")

    def _workspace(self, manifest: dict) -> Path:
        execution_id = str(manifest["execution_id"])
        if not execution_id or len(execution_id) > 128 or "/" in execution_id:
            raise TaskExecutionError("task_execution_id_invalid")
        directory = self.base_dir / "task-executions" / execution_id
        try:
            directory.mkdir(parents=True, exist_ok=True)
            return directory
        except OSError:
            fallback = Path("/tmp/sensoryplex-task-executions") / execution_id
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    def _download_asset(self, manifest: dict, workspace: Path) -> Path:
        asset = manifest["asset"]
        digest = str(asset["content_hash"])
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise TaskExecutionError("task_asset_digest_invalid")
        return self.client.download_task_asset(
            manifest["intent_id"], workspace / f"asset-{digest[7:]}", digest
        )

    def _endpoint(self, manifest: dict) -> PluginEndpoint:
        plugin = manifest["plugin"]
        runtime_instance_id = str(plugin.get("runtime_instance_id", ""))
        if not runtime_instance_id:
            raise TaskExecutionError("plugin_instance_unavailable")
        registry = self.base_dir / "hot-deploy.json"
        try:
            entries = json.loads(registry.read_text(encoding="utf-8"))
            entry = entries[runtime_instance_id]
            endpoint = read_endpoint_file(Path(entry["endpoint_file"]))["endpoint"]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, HotDeployError):
            raise TaskExecutionError("plugin_runtime_registry_unavailable") from None
        if (
            entry.get("desired_state") != "running"
            or entry.get("plugin_id") != plugin["plugin_id"]
            or entry.get("artifact_digest") != plugin["artifact_digest"]
            or entry.get("config_hash") != plugin["config_hash"]
            or not is_loopback_endpoint(endpoint)
        ):
            raise TaskExecutionError("plugin_runtime_identity_mismatch")
        channel = grpc.insecure_channel(endpoint)
        try:
            described = runtime_pb2_grpc.ProcessorPluginServiceStub(channel).Describe(
                runtime_pb2.DescribeRequest(), timeout=10
            )
        except grpc.RpcError as error:
            raise TaskExecutionError(
                f"plugin_describe_failed:{error.code().name}", retryable=True
            ) from None
        finally:
            channel.close()
        if (
            described.name != plugin["plugin_id"]
            or described.artifact_digest != plugin["artifact_digest"]
        ):
            raise TaskExecutionError("plugin_runtime_identity_mismatch")
        return PluginEndpoint(
            endpoint=endpoint,
            plugin_id=plugin["plugin_id"],
            artifact_digest=plugin["artifact_digest"],
            config_hash=plugin["config_hash"],
        )

    def _run_plugin(
        self, manifest: dict, workspace: Path, media_path: Path
    ) -> tuple[int, int, str]:
        policy = _bounded_policy(manifest["policy"])
        endpoint = self._endpoint(manifest)
        if manifest["plugin"].get("input_selector", "media").startswith("node:"):
            return self._run_observation_plugin(manifest, workspace, endpoint)
        plugin_id = manifest["plugin"]["plugin_id"]
        kind = (
            "audio_segment"
            if "media.audio_segment" in manifest["plugin"]["consumes"]
            else "video_frame"
        )
        has_vlm = plugin_id == "org.sensoryplex.vlm-moondream"
        report_path, handoff_endpoint, producer = self._run_replay(
            manifest,
            workspace,
            media_path,
            policy,
            has_vlm=has_vlm,
            wants_video=kind == "video_frame",
        )
        try:
            if kind == "video_frame":
                return self._consume_video(
                    manifest,
                    endpoint,
                    handoff_endpoint,
                    report_path,
                    producer,
                    has_vlm=has_vlm,
                )
            return self._consume_audio(
                manifest, workspace, endpoint, handoff_endpoint, report_path, producer
            )
        finally:
            self._stop_replay(producer)

    def _run_observation_plugin(self, manifest, workspace, endpoint):
        """上游输入从授权 API 读取；不重新解码，也不向插件暴露数据库。"""
        upstream = orchestration_pb2.PluginTaskOutput.FromString(
            base64.b64decode(
                self.client.plugin_upstream(manifest["intent_id"])["output_b64"], validate=True
            )
        )
        if len(upstream.observations) > 8192:
            raise TaskExecutionError("task_execution_input_budget_exceeded")
        observations, frames = [], []
        task = manifest["task"]
        with grpc.insecure_channel(endpoint.endpoint) as channel:
            stub = runtime_pb2_grpc.ProcessorPluginServiceStub(channel)
            for source in upstream.observations:
                if source.modality not in manifest["plugin"]["consumes"]:
                    continue
                request = runtime_pb2.ProcessRequest(
                    context=common.RequestContext(
                        request_id=task["task_id"] + ":" + source.observation_id,
                        trace_id="execution:" + manifest["execution_id"],
                        pipeline_run_id=manifest["run"]["run_id"],
                        stream_id=source.stream_id,
                        source_id=source.source_id,
                        deadline_unix_ms=min(
                            int(task["deadline_unix_ms"]),
                            _now_ms() + int(manifest["plugin"]["deadline_ms"]),
                        ),
                        attempt=int(task["attempt"]),
                        idempotency_key=_sha256_json(
                            {"task": task["task_id"], "source": source.observation_id}
                        ),
                        privacy_policy=common.PrivacyPolicy(data_egress="local_only"),
                    ),
                    inputs=[runtime_pb2.PluginInput(observation=source)],
                    processor_release_id=manifest["plugin"]["release_id"],
                )
                response = stub.Process(request, timeout=manifest["plugin"]["deadline_ms"] / 1000)
                if response.HasField("error"):
                    raise TaskExecutionError(
                        response.error.reason_code, retryable=response.error.retryable
                    )
                if (
                    not response.observations
                    and response.outcome != runtime_pb2.PROCESS_OUTCOME_NO_OBSERVATIONS
                ):
                    raise TaskExecutionError("empty_plugin_result")
                observations.extend(
                    json_format.MessageToDict(value) for value in response.observations
                )
                frames.append(
                    {
                        "buffer_id": source.observation_id,
                        "source_digest": source.content_hash,
                        "source_time_range_ms": [
                            source.time_range.start_ms,
                            source.time_range.end_ms,
                        ],
                        "outcome": int(response.outcome)
                        if response.outcome
                        else runtime_pb2.PROCESS_OUTCOME_OBSERVED,
                        "outcome_reason": response.outcome_reason,
                        "observation_count": len(response.observations),
                    }
                )
        return self._write_worker_report(
            workspace, manifest, Path(), observations, frames, input_count=len(frames), reason=""
        )

    def _consume_audio(
        self,
        manifest: dict,
        workspace: Path,
        endpoint: PluginEndpoint,
        handoff_endpoint: str,
        report_path: Path,
        producer: subprocess.Popen[str],
    ) -> tuple[int, int, str]:
        """音频段边解码边转写：每次只领取保留面内的一批，长视频不会耗尽段配额。"""
        source_id, stream_id = _source_identity(manifest)
        reader = LeaseBufferReader(handoff_endpoint, ttl_ms=30_000, timeout_s=10)
        channel = grpc.insecure_channel(endpoint.endpoint)
        plugin = runtime_pb2_grpc.ProcessorPluginServiceStub(channel)
        observations: list[dict] = []
        frames: list[dict] = []
        consumed: set[str] = set()
        inputs = 0
        failure = ""
        report = None
        active_at = time.monotonic()
        try:
            while True:
                report = self._replay_report_ready(report_path)
                if producer.poll() is not None:
                    break
                buffers = self._list_buffers(handoff_endpoint)
                foreign = [entry.buffer_id for entry in buffers if entry.kind != AUDIO_SEGMENT_KIND]
                self._return_buffers(reader, foreign)
                available = {
                    entry.buffer_id: entry
                    for entry in buffers
                    if entry.kind == AUDIO_SEGMENT_KIND and entry.buffer_id not in consumed
                }
                groups = self._audio_groups(available)
                for group in groups:
                    if inputs >= MAX_MODEL_REQUESTS_PER_TASK:
                        raise TaskExecutionError(
                            "task_execution_input_budget_exceeded", inputs=inputs
                        )
                    produced, records, used, failure = self._run_group(
                        manifest, plugin, source_id, group, available, handoff_endpoint
                    )
                    inputs += len(group.frame_ids)
                    observations.extend(produced)
                    frames.extend(records)
                    consumed.update(used)
                    self._release_foreign(reader, AUDIO_SEGMENT_KIND)
                    if failure:
                        break
                if failure:
                    break
                if report is not None:
                    break
                if groups or foreign:
                    active_at = time.monotonic()
                if producer.poll() is not None:
                    report = report or self._replay_report_ready(report_path)
                    if not groups:
                        break
                if time.monotonic() - active_at > RUNTIME_STALL_S:
                    raise TaskExecutionError("runtime_replay_stalled", retryable=True)
                if not groups:
                    time.sleep(AUDIO_PLANE_POLL_INTERVAL_S)
        finally:
            reader.close()
            channel.close()
        if report is None or "decode_failed" in report.blockers:
            raise TaskExecutionError("runtime_replay_failed", retryable=True)
        if (report.source.source.source_id, report.source.source.stream_id) != (
            source_id,
            stream_id,
        ):
            raise TaskExecutionError("task_execution_source_identity_mismatch")
        if not inputs and any(track.track_kind == "audio" for track in report.source.tracks):
            raise TaskExecutionError("no_audio_segment_buffer_to_process")
        _, _, result_ref = self._write_worker_report(
            workspace,
            manifest,
            report_path,
            observations,
            frames,
            input_count=inputs,
            reason=failure or "",
        )
        if failure:
            raise TaskExecutionError(
                failure,
                retryable=failure.startswith("plugin_retryable:"),
                inputs=inputs,
                outputs=len(observations),
                result_ref=result_ref,
            )
        if self._await_replay(producer) != 0:
            raise TaskExecutionError("runtime_replay_failed", retryable=True)
        return inputs, len(observations), result_ref

    def _consume_video(
        self,
        manifest: dict,
        endpoint: PluginEndpoint,
        handoff_endpoint: str,
        report_path: Path,
        producer: subprocess.Popen[str],
        *,
        has_vlm: bool,
    ) -> tuple[int, int, str]:
        """边解码边消费：窗口/锚点一落账就领料、跑模型、立刻归还。

        这是"真背压"的消费侧。保留面是有界的（arena 字节 + 保留条数），只有消费者与生产者
        并发，长媒体才不会从某一帧起被判成必然拒绝：生产者等消费者腾空间，消费者则在窗口
        落账的那一刻就领料。消费不过来时执行器不假装成功——被拒单位连同原因码回到报告里。

        收尾顺序同样是契约：Runtime 先原子落报告、再收尾数据面服务端，因此消费者在看到报告
        之后才清空保留表。服务端一旦看到"生产者已结束 + 消费者在场 + 保留表为空"就立刻收尾，
        不必空等空闲超时；反过来，只要还有保留项没有归宿，这次交接就不算跑完。
        """
        workspace = report_path.parent
        ledger = report_path.with_name(f"{report_path.stem}.evidence.jsonl")
        source_id, stream_id = _source_identity(manifest)
        stream = _EvidenceStream(ledger, has_vlm=has_vlm)
        plugin_channel = grpc.insecure_channel(endpoint.endpoint)
        plugin = runtime_pb2_grpc.ProcessorPluginServiceStub(plugin_channel)
        reader = LeaseBufferReader(handoff_endpoint, ttl_ms=30_000, timeout_s=10)
        observations: list[dict] = []
        frames: list[dict] = []
        consumed: set[str] = set()
        returned: set[str] = set()
        requests = 0
        inputs = 0
        failure = ""
        settled = False
        final = False
        quiet_since = time.monotonic()
        swept_at = quiet_since
        checked_at = quiet_since
        try:
            while True:
                started = time.monotonic()
                # 报告检查本身要读文件 + 解析，按清扫节奏轮询就够：它决定的是"还有没有下一批
                # 单位"，而账本停下来的那一刻正好也是消费者空转的时候。
                if not final and started - checked_at >= PLANE_SWEEP_INTERVAL_S:
                    checked_at = started
                    if self._replay_report_ready(report_path) is not None:
                        # 报告落盘 ⟹ 逐帧账本不会再增长：剩下的窗口/锚点读完就能清空保留表。
                        final = True
                groups, refused_units, discards = stream.poll()
                if discards:
                    returned.update(self._return_buffers(reader, discards))
                for unit in refused_units:
                    frames.extend(_refused_records(unit))
                handled = bool(groups or refused_units or discards)
                available: dict | None = None
                for group in groups:
                    if available is None or time.monotonic() - swept_at >= PLANE_SWEEP_INTERVAL_S:
                        available, foreign = self._describe_plane(handoff_endpoint, stream_id)
                        # 本节点不消费的 kind（音频 PCM / 音频段）立刻归还：它们不是这条
                        # 模态的输入，却同样占着有界保留面。
                        returned.update(self._return_buffers(reader, foreign))
                        swept_at = time.monotonic()
                    missing = [
                        frame_id for frame_id in group.frame_ids if frame_id not in available
                    ]
                    if missing:
                        # 账本说这些帧交接成功了，数据面却已经没有它们：这是保留/过期层面的
                        # 失败，必须显式失败，不能假装这一帧没有内容。
                        failure = "data_plane_buffer_missing"
                        break
                    if requests + len(group.frame_ids) > MAX_MODEL_REQUESTS_PER_TASK:
                        failure = "task_execution_input_budget_exceeded"
                        break
                    # 每帧一次 `Process`：请求数就是真正发给模型的帧数，不是"单位数"。
                    requests += len(group.frame_ids)
                    inputs += len(group.frame_ids)
                    produced, records, used, failure = self._run_group(
                        manifest, plugin, source_id, group, available, handoff_endpoint
                    )
                    observations.extend(produced)
                    frames.extend(records)
                    consumed.update(used)
                    if failure:
                        break
                if failure:
                    break
                if final and not handled:
                    # 账本已经读尽、不会再有新单位：把保留表彻底清空（含本节点不消费的 kind），
                    # Runtime 的收尾判据才会成立。这里不能提前归还：账本还没读完时，
                    # 保留表里"这一轮没人要"的帧可能就是下一个窗口的上下文。
                    returned.update(self._drain_plane(reader, consumed))
                    settled = True
                    break
                now = time.monotonic()
                quiet_since = now if handled else quiet_since
                exited = producer.poll() is not None
                if exited and not handled:
                    break
                if not exited and now - quiet_since > RUNTIME_STALL_S:
                    raise TaskExecutionError("runtime_replay_stalled", retryable=True)
                if not handled:
                    if now - swept_at >= PLANE_SWEEP_INTERVAL_S:
                        swept_at = now
                        _available, foreign = self._describe_plane(handoff_endpoint, stream_id)
                        returned.update(self._return_buffers(reader, foreign))
                    time.sleep(LEDGER_POLL_INTERVAL_S)
        finally:
            if not settled:
                # 显式失败、或 Runtime 已经收尾时同样尽力把保留表还干净：数据面已经关掉
                # 就没有什么可还的，失败原因由 `failure` 或退出码决定。
                self._drain_plane_quietly(reader, set())
            reader.close()
            plugin_channel.close()
        report = self._replay_report_ready(report_path)
        if failure:
            # 插件可以在 Acquire 之前拒绝请求；不能把“已调用”当成“已归还”。
            # 提前失败立即停止生产者，并保留原始错误，避免收尾超时覆盖真正原因。
            self._stop_replay(producer)
            _, _, result_ref = self._write_worker_report(
                workspace,
                manifest,
                report_path,
                observations,
                frames,
                input_count=inputs,
                reason=failure,
            )
            raise TaskExecutionError(
                failure,
                retryable=failure.startswith(("plugin_retryable:", "plugin_rpc_failed:")),
                inputs=inputs,
                outputs=len(observations),
                result_ref=result_ref,
            )
        exit_code = self._await_replay(producer)
        if report is None:
            raise TaskExecutionError("runtime_replay_report_unavailable")
        if "decode_failed" in report.blockers:
            raise TaskExecutionError("runtime_replay_failed", retryable=True)
        if exit_code != 0 and not failure:
            raise TaskExecutionError("runtime_replay_failed", retryable=True)
        if (
            report.source.source.source_id != source_id
            or report.source.source.stream_id != stream_id
        ):
            raise TaskExecutionError("task_execution_source_identity_mismatch")
        plan = _evidence_plan(report, ledger)
        # 已经显式失败的这一次运行不可能"跑完整个计划"：它的输入分组本来就该少一截，
        # 因此只在对账仍然成立时做逐项比较，避免把真正的失败原因盖成契约漂移。
        if not failure:
            self._reconcile_plan(plan, stream, has_vlm=has_vlm)
        _, _, result_ref = self._write_worker_report(
            workspace,
            manifest,
            report_path,
            observations,
            frames,
            input_count=inputs,
            reason=failure or "",
            evidence={
                "characterized_frames": plan.characterized_frames,
                "covered_without_model_refresh": plan.covered_without_model_refresh,
                "max_selected_gap_ms": plan.max_selected_gap_ms,
                "windows": plan.window_records,
                "anchors": len(plan.anchors),
                "requests": requests,
                "ledger_path": plan.ledger_path,
                "ledger_entries": plan.ledger_entries,
                "input_mode": "evidence_window" if has_vlm else "evidence_anchor",
                # 被有界数据面拒绝的输入单位：它们没有送模型，也绝不能被读成"画面没变化"。
                "retention_rejected_units": len(stream.rejected),
                "retention_rejected_frames": sum(len(unit.frames) for unit in stream.rejected),
                "returned_frames": len(returned),
                "streamed": True,
            },
        )
        if failure:
            raise TaskExecutionError(
                failure,
                retryable=failure.startswith("plugin_retryable:")
                or failure in RETRYABLE_PLANE_FAILURES,
                inputs=inputs,
                outputs=len(observations),
                result_ref=result_ref,
            )
        return inputs, len(observations), result_ref

    @staticmethod
    def _video_groups(plan: _EvidencePlan, *, has_vlm: bool) -> list[_InputGroup]:
        """视频输入单位由 Runtime 的语义覆盖决定，执行器不重新采样。

        VLM 要的是**上下文**：每个证据窗口（pre + anchor + post）作为一个单位，另外补上没有
        窗口的静态复查锚点，保证静态画面也按 `max_semantic_gap_ms` 刷新。单位内的每一帧
        各发一次 `Process`：首方 VLM 插件自报 `supports.batch=false`，单位只负责记账与归还。
        OCR 要的是**文字变化**：只送锚点帧，窗口里的前后上下文对文字识别没有增量。
        规则本身在 `_modality_view` 里，增量流与完整计划共用同一条。
        """
        return plan.units(has_vlm=has_vlm)[0]

    @staticmethod
    def _audio_groups(available: dict) -> list[_InputGroup]:
        ordered = sorted(
            available.values(), key=lambda entry: (entry.time_range.start_ms, entry.buffer_id)
        )
        return [
            _InputGroup(
                label=entry.buffer_id, trigger="audio_segment", frame_ids=(entry.buffer_id,)
            )
            for entry in ordered
        ]

    def _run_replay(
        self,
        manifest: dict,
        workspace: Path,
        media_path: Path,
        policy: dict[str, int],
        *,
        has_vlm: bool,
        wants_video: bool,
    ) -> tuple[Path, str, subprocess.Popen[str]]:
        node_id = manifest["task"]["node_id"]
        report = workspace / f"{node_id}.replay.pb"
        ledger = workspace / f"{node_id}.replay.evidence.jsonl"
        # 恢复重试不能把前次终态报告当成本次生产者已结束。
        report.unlink(missing_ok=True)
        ledger.unlink(missing_ok=True)
        pipeline = workspace / "runtime-pipeline.yaml"
        pipeline.write_text(_runtime_pipeline(policy, has_vlm=has_vlm), encoding="utf-8")
        listen = f"127.0.0.1:{_free_loopback_port()}"
        # 视频节点跑全帧判别；音频节点不消费视频帧，因此既不启用语义账本，也用最长的
        # 采样间隔，避免为整段视频的证据窗口白白占住共享内存。
        if wants_video:
            evidence_flags = [
                "--evidence",
                "--evidence-max-gap-ms",
                str(policy["evidence_max_gap_ms"]),
                "--evidence-context-before",
                str(policy["evidence_context_before"]),
                "--evidence-context-after",
                str(policy["evidence_context_after"]),
                "--evidence-change-threshold",
                str(policy["evidence_change_threshold"]),
                "--evidence-text-change-threshold",
                str(policy["evidence_text_change_threshold"]),
                "--evidence-min-event-interval-ms",
                str(policy["evidence_min_event_interval_ms"]),
                "--evidence-ledger",
                str(ledger),
            ]
            sampling_interval = policy["sample_interval_ms"]
            retention_bytes = policy["evidence_retention_bytes"]
            retained_limit = 4096
        else:
            evidence_flags = []
            sampling_interval = 60_000
            retention_bytes = AUDIO_ONLY_RETENTION_BYTES
            retained_limit = 256
        command = [
            str(_runtime_binary()),
            "replay",
            str(pipeline),
            str(media_path),
            "--report",
            str(report),
            "--sampling-min-interval-ms",
            str(sampling_interval),
            "--sampling-static-hold-ms",
            str(max(sampling_interval, 5_000)),
            "--audio-segment-ms",
            str(policy["audio_segment_ms"]),
            "--audio-overlap-ms",
            str(policy["audio_overlap_ms"]),
            *evidence_flags,
            "--handoff-listen",
            listen,
            "--handoff-retained-limit",
            str(retained_limit),
            "--handoff-arena-bytes",
            str(retention_bytes),
            "--handoff-ttl-ms",
            "30000",
            "--handoff-wait-timeout-ms",
            "300000",
            "--handoff-lossless",
            "--handoff-idle-timeout-ms",
            "300000",
        ]
        # Runtime 的输出只有计数与原因码，落进本次任务的日志文件里便于复核失败原因；
        # 它不进控制消息，也不含原始帧、音频或密钥。
        log_path = workspace / f"{node_id}.replay.log"
        replay_env = dict(os.environ, SENSORYPLEX_NO_GL="1")
        process = subprocess.Popen(
            command,
            env=replay_env,
            stdout=log_path.open("wb"),
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + RUNTIME_WAIT_S
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise TaskExecutionError("runtime_replay_failed")
            channel = grpc.insecure_channel(listen)
            try:
                grpc.channel_ready_future(channel).result(timeout=0.25)
                break
            except grpc.FutureTimeoutError:
                time.sleep(0.1)
            finally:
                channel.close()
        else:
            process.kill()
            process.wait(timeout=5)
            self._stop_replay(process)
            raise TaskExecutionError("runtime_handoff_start_timeout", retryable=True)
        # 数据面在解码之前就已经在线：调用方从这一刻起可以并发领料。调用方仍必须
        # wait/终止生产者，防止异常路径留下 Runtime 子进程或占用私有 loopback 端口。
        return report, listen, process

    def _await_replay(self, process: subprocess.Popen[str]) -> int:
        """等生产者收尾并返回退出码；超时即受控终止，绝不留下无主 Runtime。"""
        try:
            return process.wait(timeout=RUNTIME_WAIT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise TaskExecutionError("runtime_replay_timeout", retryable=True) from None

    @staticmethod
    def _replay_report_ready(report_path: Path) -> media_pb2.ReplayReport | None:
        """报告出现即"逐帧账本已经完整"；还没出现、或只写了一半都返回 `None`。

        Runtime 先原子落报告、再收尾数据面服务端（`write_report_atomically`），因此这个
        判据既能用来领最后一批窗口，也能用来清空保留表——它不能靠"等生产者退出"，
        生产者恰恰是在消费者把保留表还干净之后才退出的。
        """
        try:
            raw = report_path.read_bytes()
        except OSError:
            return None
        if not raw:
            return None
        source = media_pb2.ReplayReport()
        try:
            source.ParseFromString(raw)
        except DecodeError:
            return None
        return source

    @classmethod
    def _list_buffers(cls, handoff_endpoint: str) -> list:
        """按当前保留表取数据面此刻真实握有的 buffer；拒绝一律带原因码上抛。"""
        channel = grpc.insecure_channel(handoff_endpoint)
        try:
            grpc.channel_ready_future(channel).result(timeout=RUNTIME_WAIT_S)
            handoff = handoff_pb2_grpc.BufferHandoffServiceStub(channel)
            listing = handoff.List(handoff_pb2.ListRetainedRequest(), timeout=20)
        except grpc.RpcError as error:
            raise TaskExecutionError(
                f"data_plane_list_failed:{error.code().name}", retryable=True
            ) from None
        except grpc.FutureTimeoutError:
            raise TaskExecutionError("data_plane_not_ready", retryable=True) from None
        finally:
            channel.close()
        return list(listing.buffers)

    def _describe_plane(self, handoff_endpoint: str, stream_id: str) -> tuple[dict, list[str]]:
        """一次 `List` 同时给出"当前可读的视频帧"与"本节点不消费、要立刻归还的 kind"。

        两件事共用同一次 List：分开取会多出一段"表里明明有、我却装作没看见"的窗口，而有界
        保留面正是靠消费者及时腾空间才不会被长媒体顶满。流身份不符仍按显式失败处理。
        """
        entries = self._list_buffers(handoff_endpoint)
        frames = {entry.buffer_id: entry for entry in entries if entry.kind == VIDEO_FRAME_KIND}
        mismatched = [entry.buffer_id for entry in frames.values() if entry.stream_id != stream_id]
        if mismatched:
            raise TaskExecutionError("data_plane_stream_identity_mismatch")
        return frames, [entry.buffer_id for entry in entries if entry.kind != VIDEO_FRAME_KIND]

    @classmethod
    def _release_foreign(cls, reader: LeaseBufferReader, kind: str) -> list[str]:
        """归还本节点不消费的 kind；数据面已经收尾时返回空表，不把失败抛给心跳方。"""
        try:
            entries = list(reader.listing().buffers)
        except grpc.RpcError:
            return []
        return cls._return_buffers(
            reader, [entry.buffer_id for entry in entries if entry.kind != kind]
        )

    @staticmethod
    def _return_buffers(reader: LeaseBufferReader, buffer_ids: Iterable[str]) -> list[str]:
        """把不构成证据的保留项归还数据面；已被插件释放的同样算结束状态。"""
        returned: list[str] = []
        for buffer_id in buffer_ids:
            try:
                reader.discard(buffer_id)
            except Exception:  # noqa: BLE001 - 已被插件释放同样是允许的结束状态
                continue
            returned.append(buffer_id)
        return returned

    def _drain_plane(self, reader: LeaseBufferReader, keep: set[str]) -> list[str]:
        """把保留表里"这一轮不需要的"全部归还，直到它空掉。

        `keep` 是插件已经消费过的帧（它们的 lease 由插件自己释放）。数据面要求每条保留都有
        归宿（`released + expired + retained == retained_total`），因此"这条我不需要"不能
        实现成"装作没看见"：保留表必须被清空，Runtime 才可能按"消费者清空数据面"立刻收尾。
        这里以**当前保留表**为准而不是账本里的计划：账本只覆盖它记录过的 kind，
        同一张表里还有本节点根本不消费的东西。
        """
        try:
            entries = list(reader.listing().buffers)
        except grpc.RpcError as error:
            raise TaskExecutionError(
                f"data_plane_list_failed:{error.code().name}", retryable=True
            ) from None
        return self._return_buffers(
            reader, [entry.buffer_id for entry in entries if entry.buffer_id not in keep]
        )

    def _drain_plane_quietly(self, reader: LeaseBufferReader, keep: set[str]) -> None:
        """失败路径上的尽力归还：数据面已经收尾时无账可还，也不该掩盖真正的失败原因。"""
        try:
            self._drain_plane(reader, keep)
        except (grpc.RpcError, TaskExecutionError):
            return

    @staticmethod
    def _reconcile_plan(plan: _EvidencePlan, stream: _EvidenceStream, *, has_vlm: bool) -> None:
        """用完整账本重算计划，并与流式跑过的单位逐项对账。

        流式消费必须与完整计划等价：少一个单位就是"证据没跑"，多一个就是重复跑模型。
        两边都由 `_modality_view` 生成，因此这里的比较能真正发现漏读、重复与提前收尾。
        """
        if (
            plan.characterized_frames != stream.frames_seen
            or plan.ledger_entries != stream.frames_seen
        ):
            raise TaskExecutionError("evidence_stream_incomplete")
        if plan.window_records != stream.windows_seen:
            raise TaskExecutionError("evidence_stream_incomplete")
        groups, refused, _discards = plan.units(has_vlm=has_vlm)
        if _unit_signature(groups) != _unit_signature(stream.groups):
            raise TaskExecutionError("evidence_stream_plan_mismatch")
        if _unit_signature(refused) != _unit_signature(stream.rejected):
            raise TaskExecutionError("evidence_stream_plan_mismatch")

    @staticmethod
    def _stop_replay(process: subprocess.Popen[str]) -> None:
        """等待 Runtime 自然退出，超时后受控终止，不遗留跨任务的 handoff 服务。"""
        try:
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
        finally:
            # Runtime 的 stdout/stderr 写进本次任务的日志文件，句柄由 Popen 持有。
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except OSError:
                    pass

    @staticmethod
    def _discard_buffers(handoff_endpoint: str, buffers: list) -> None:
        """释放本次 replay 未交给插件的描述符，避免保留表阻塞下一条任务。"""
        reader = LeaseBufferReader(handoff_endpoint, ttl_ms=30_000, timeout_s=10)
        try:
            for entry in buffers:
                try:
                    reader.discard(entry.buffer_id)
                except Exception:  # noqa: BLE001 - 已被插件释放同样是允许的结束状态
                    continue
        finally:
            reader.close()

    @staticmethod
    def _descriptor(entry, handoff_endpoint: str) -> common.BufferDescriptor:
        return common.BufferDescriptor(
            buffer_id=entry.buffer_id,
            kind=entry.kind,
            memory_kind="cpu_shared_memory",
            locator=common.BufferLocator(
                offset=entry.offset_bytes,
                length=entry.length_bytes,
                handoff_endpoint=handoff_endpoint,
            ),
            format=entry.format,
            stream_id=entry.stream_id,
            time_range=entry.time_range,
            content_hash=entry.content_hash,
        )

    def _run_group(
        self,
        manifest: dict,
        plugin,
        source_id: str,
        group: _InputGroup,
        available: dict,
        handoff_endpoint: str,
    ) -> tuple[list[dict], list[dict], set[str], str]:
        """逐帧发起 `Process`：窗口/锚点是记账单位，批大小是插件声明的能力。

        首方 VLM 插件声明 `supports.batch=false` / `maxBatchSize=1`，而证据窗口最多 9 帧；
        把整个窗口塞进一次请求会越过插件自报的能力（实测被插件以 `invalid_batch_size`
        拒绝）。因此这里**每帧一次请求**，观测顺序仍按单位内的帧序，失败码与归还范围也按
        单位汇总：既不放大小插件能力，也不把少掉的帧读成"这一帧没有内容"。
        """
        entries = [available[frame_id] for frame_id in group.frame_ids]
        used: set[str] = set()
        task = manifest["task"]
        observations: list[dict] = []
        frames: list[dict] = []
        failure = ""

        def describe(entry) -> dict:
            return {
                "buffer_id": entry.buffer_id,
                "kind": entry.kind,
                "source_digest": entry.content_hash,
                "source_time_range_ms": [
                    entry.time_range.start_ms,
                    entry.time_range.end_ms,
                ],
                "evidence_group": group.label,
                "evidence_trigger": group.trigger,
            }

        def record(entry, reason: str, retryable: bool) -> dict:
            return {**describe(entry), "error": {"reason": reason, "retryable": retryable}}

        def request_for(entry) -> runtime_pb2.ProcessRequest:
            # 每次请求都是一个帧身份：`request_id` 与 `idempotency_key` 都由这一帧的
            # 内容摘要参与派生，重派同一帧才是同一次请求。
            digest = (
                "sha256:"
                + hashlib.sha256(
                    "|".join([task["task_id"], group.label, entry.content_hash]).encode()
                ).hexdigest()
            )
            return runtime_pb2.ProcessRequest(
                context=common.RequestContext(
                    request_id=f"req-{task['task_id']}-{group.label}-{entry.buffer_id}",
                    trace_id=f"execution:{manifest['execution_id']}",
                    pipeline_run_id=manifest["run"]["run_id"],
                    stream_id=entry.stream_id,
                    source_id=source_id,
                    deadline_unix_ms=min(
                        int(task["deadline_unix_ms"]),
                        _now_ms() + int(manifest["plugin"]["deadline_ms"]),
                    ),
                    attempt=int(task["attempt"]),
                    idempotency_key=digest,
                    privacy_policy=common.PrivacyPolicy(data_egress="local_only"),
                ),
                inputs=[runtime_pb2.PluginInput(buffer=self._descriptor(entry, handoff_endpoint))],
                processor_release_id=manifest["plugin"].get("release_id")
                or manifest["plugin"]["plugin_version"],
            )

        for position, entry in enumerate(entries):
            pending = entries[position:]
            # `used` 只装"真的发出去过请求的帧"：没发出去的帧没有任何人持有 lease，
            # 收尾清空保留表时必须由调度方归还，不能被当成"插件负责"。
            used.add(entry.buffer_id)
            try:
                response = plugin.Process(
                    request_for(entry),
                    timeout=max(1, manifest["plugin"]["deadline_ms"] / 1000),
                )
            except grpc.RpcError as error:
                failure = f"plugin_rpc_failed:{error.code().name}"
                frames.extend(record(item, failure, True) for item in pending)
                break
            if response.HasField("error"):
                code = response.error.reason_code or "plugin_processing_failed"
                failure = ("plugin_retryable:" if response.error.retryable else "") + code
                retryable = bool(response.error.retryable)
                frames.extend(record(item, code, retryable) for item in pending)
                break
            produced = list(response.observations)
            if not produced:
                if (
                    response.outcome == runtime_pb2.PROCESS_OUTCOME_NO_OBSERVATIONS
                    and response.outcome_reason
                ):
                    frames.append(
                        {
                            **describe(entry),
                            "outcome": int(response.outcome),
                            "outcome_reason": response.outcome_reason,
                            "observation_count": 0,
                        }
                    )
                    continue
                failure = "empty_plugin_result"
                frames.extend(record(item, failure, False) for item in pending)
                break
            if len(produced) != 1 and not manifest["plugin"].get("release_id"):
                # 单帧请求只能得到单条观测；多出来的是契约漂移，不能按顺序硬套。
                failure = "plugin_observation_count_mismatch"
                frames.extend(record(item, failure, False) for item in pending)
                break
            if len(produced) > 128:
                failure = "plugin_observation_count_exceeded"
                frames.extend(record(item, failure, False) for item in pending)
                break
            observations.extend(json_format.MessageToDict(observation) for observation in produced)
            frames.append(
                {
                    **describe(entry),
                    "observation_id": produced[0].observation_id,
                    "outcome": runtime_pb2.PROCESS_OUTCOME_OBSERVED,
                    "observation_count": len(produced),
                }
            )
        return observations, frames, used, failure

    def _process_groups(
        self,
        manifest: dict,
        endpoint: PluginEndpoint,
        source_id: str,
        groups: list[_InputGroup],
        available: dict,
        all_buffers: list,
        handoff_endpoint: str,
    ) -> tuple[list[dict], list[dict], set[str], str]:
        """按证据单位顺序调用插件；这条路径只服务音频段。"""
        plugin_channel = grpc.insecure_channel(endpoint.endpoint)
        plugin = runtime_pb2_grpc.ProcessorPluginServiceStub(plugin_channel)
        observations: list[dict] = []
        frames: list[dict] = []
        failure = ""
        used: set[str] = set()
        try:
            for group in groups:
                produced, records, group_used, failure = self._run_group(
                    manifest, plugin, source_id, group, available, handoff_endpoint
                )
                observations.extend(produced)
                frames.extend(records)
                used.update(group_used)
                if failure:
                    break
        finally:
            # 当前任务无需的 buffer 也要显式释放，不让 Runtime 因未消费的保留项滞留。
            try:
                reader = LeaseBufferReader(handoff_endpoint, ttl_ms=30_000, timeout_s=10)
                try:
                    for entry in all_buffers:
                        if entry.buffer_id not in used:
                            try:
                                reader.discard(entry.buffer_id)
                            except Exception:  # noqa: BLE001 - 插件可能已释放该 lease
                                continue
                finally:
                    reader.close()
            finally:
                plugin_channel.close()
        return observations, frames, used, failure

    def _write_worker_report(
        self,
        workspace: Path,
        manifest: dict,
        replay_report: Path,
        observations: list[dict],
        frames: list[dict],
        *,
        input_count: int,
        reason: str,
        evidence: dict | None = None,
    ) -> tuple[int, int, str]:
        node_id = manifest["task"]["node_id"]
        path = workspace / f"{node_id}.worker.json"
        document = {
            "input_mode": "buffer",
            "failures": [reason] if reason else [],
            "observations": observations,
            "frames": frames,
            "task_summary": {
                "input_count": input_count,
                "output_count": len(observations),
                "reason": reason,
            },
        }
        if evidence is not None:
            document["evidence"] = evidence
        if manifest.get("plugin", {}).get("release_id"):
            for frame in frames:
                frame["produces"] = manifest["plugin"]["produces"]
        if manifest.get("plugin", {}).get("release_id") and not reason:
            task = manifest["task"]
            output = orchestration_pb2.PluginTaskOutput(
                task_id=task["task_id"],
                assignment_id=task["assignment_id"],
                attempt=int(task["attempt"]),
                skipped_reason="upstream_has_no_matching_observations" if not frames else "",
                observations=[
                    json_format.ParseDict(value, material_pb2.Observation())
                    for value in observations
                ],
                processing_receipts=[
                    {
                        "input_id": frame["buffer_id"],
                        "time_range": {
                            "start_ms": int(frame["source_time_range_ms"][0]),
                            "end_ms": int(frame["source_time_range_ms"][1]),
                        },
                        "outcome": frame.get("outcome", runtime_pb2.PROCESS_OUTCOME_OBSERVED),
                        "reason_code": frame.get("outcome_reason", ""),
                        "observation_count": frame.get("observation_count", 1),
                    }
                    for frame in frames
                    if not frame.get("error")
                ],
            )
            encoded_output = base64.b64encode(output.SerializeToString(deterministic=True)).decode()
            if len(encoded_output) > 4_000_000:
                raise TaskExecutionError("plugin_task_output_limit_exceeded")
            result = self.client.stage_plugin_output(
                task["task_id"], {"intent_id": manifest["intent_id"], "output_b64": encoded_output}
            )
            saved = orchestration_pb2.PluginTaskOutput.FromString(
                base64.b64decode(result["output_b64"], validate=True)
            )
            observations = [json_format.MessageToDict(value) for value in saved.observations]
            document["observations"] = observations
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        path.write_bytes(encoded)
        return input_count, len(observations), "agent-result:" + hashlib.sha256(encoded).hexdigest()

    def _run_timeline(
        self, manifest: dict, workspace: Path, media_path: Path
    ) -> tuple[int, int, str]:
        policy = _bounded_policy(manifest["policy"])
        generic = bool(manifest["policy"].get("generic_plugin_graph"))
        reports = (
            [
                path
                for path in sorted(workspace.glob("*.worker.json"))
                if path.name != "timeline.worker.json"
            ]
            if generic
            else sorted(workspace.glob("*_fast.worker.json"))
            + sorted(workspace.glob("vlm_enrich.worker.json"))
        )
        replay_reports = (
            sorted(workspace.glob("*.replay.pb"))
            if generic
            else sorted(workspace.glob("*_fast.replay.pb"))
            + sorted(workspace.glob("vlm_enrich.replay.pb"))
        )
        if not replay_reports:
            raise TaskExecutionError("timeline_upstream_runtime_report_missing")
        observations: list[dict] = []
        frames: list[dict] = []
        summaries: dict[str, dict] = {}
        for path in reports:
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise TaskExecutionError("timeline_worker_report_unreadable") from None
            observations.extend(document.get("observations") or [])
            frames.extend(document.get("frames") or [])
            summaries[path.name.removesuffix(".worker.json")] = document.get("task_summary") or {}
        report_path, source = self._timeline_source(replay_reports)
        # 语义计划来自上游 worker 的逐帧记录：只有被选中的帧才带 `evidence_group`。
        selected = sorted(
            {
                (
                    int(frame["source_time_range_ms"][0]),
                    int(frame["source_time_range_ms"][1]),
                    str(frame.get("evidence_trigger", "")),
                )
                for frame in frames
                if frame.get("evidence_group") and frame.get("source_time_range_ms")
            }
        )
        # 被有界数据面拒绝的帧在上游报告里带稳定原因码 `data_plane_retention_rejected`：
        # 它们覆盖的秒格是"计划刷新过、但没有送进模型"，不能与"触发过却无观测"混为一谈。
        rejected = sorted(
            {
                (
                    int(frame["source_time_range_ms"][0]),
                    int(frame["source_time_range_ms"][1]),
                )
                for frame in frames
                if (frame.get("error") or {}).get("reason") == RETENTION_REJECTED
                and frame.get("source_time_range_ms")
            }
        )
        coverage = (
            self._generic_coverage(source, observations, frames, manifest["policy"])
            if generic
            else self._coverage(source, observations, summaries, policy, selected, rejected)
        )
        if not observations:
            self._ingest_timeline(manifest, source, [], [], coverage)
            return 0, 0, "timeline-empty:" + _sha256_json({"coverage": coverage})
        merged = workspace / "timeline.worker.json"
        media_frames = [
            frame for frame in frames if frame.get("kind") in {VIDEO_FRAME_KIND, AUDIO_SEGMENT_KIND}
        ]
        if generic:
            # 派生观测沿用真实上游数据面的来源；把本次已校验的输出身份加入原描述符账本。
            # 不把 Observation 读取回执伪装成新的媒体解码或共享内存分配。
            originals = {
                frame["buffer_id"] + "@" + frame["source_digest"][7:23]: frame
                for frame in media_frames
                if frame.get("buffer_id") and frame.get("source_digest")
            }
            recorded = {frame.get("observation_id") for frame in media_frames}
            for observation in observations:
                if observation["observationId"] in recorded:
                    continue
                source_frame = originals.get(observation["sourceItemId"])
                if not source_frame:
                    raise TaskExecutionError("timeline_observation_source_unmapped")
                media_frames.append(
                    {**source_frame, "observation_id": observation["observationId"]}
                )
        merged.write_text(
            json.dumps(
                {
                    "input_mode": "buffer",
                    "failures": [],
                    "observations": observations,
                    "frames": media_frames,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        pipeline = workspace / "runtime-pipeline.yaml"
        pipeline_content = _runtime_pipeline(policy, has_vlm="vlm_enrich" in summaries)
        if generic:
            import yaml

            definition = yaml.safe_load(pipeline_content)
            definition["spec"]["timeline_fusion"]["fast_modalities"] = manifest["policy"][
                "fast_modalities"
            ]
            # 同一事实类型可有快路径与补全生产者；补全台账单独记录各节点待办。
            # Rust 融合策略要求两组模态互斥，已经由快路径满足的类型保留在必需集合。
            definition["spec"]["timeline_fusion"]["enrichment_modalities"] = sorted(
                set(manifest["policy"]["enrichment_modalities"])
                - set(manifest["policy"]["fast_modalities"])
            )
            pipeline_content = yaml.safe_dump(definition)
        pipeline.write_text(pipeline_content, encoding="utf-8")
        material_dir = workspace / "materials"
        timeline_report = workspace / "timeline.json"
        completed = subprocess.run(
            [
                str(_runtime_binary()),
                "timeline",
                str(pipeline),
                str(media_path),
                "--report",
                str(report_path),
                "--worker-report",
                str(merged),
                "--material-dir",
                str(material_dir),
                "--out",
                str(timeline_report),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=RUNTIME_WAIT_S,
            check=False,
        )
        if completed.returncode != 0 or not timeline_report.is_file():
            raise TaskExecutionError("timeline_fusion_failed")
        try:
            document = json.loads(timeline_report.read_text(encoding="utf-8"))
            if generic and document.get("rejected"):
                raise TaskExecutionError("timeline_observation_rejected")
            items = document["items"]
            unit_bytes = [path.read_bytes() for path in sorted(material_dir.glob("*.material.pb"))]
        except (OSError, KeyError, json.JSONDecodeError):
            raise TaskExecutionError("timeline_output_unreadable") from None
        self._ingest_timeline(manifest, source, unit_bytes, items, coverage)
        return (
            len(observations),
            len(unit_bytes),
            "timeline:" + hashlib.sha256(timeline_report.read_bytes()).hexdigest(),
        )

    @staticmethod
    def _generic_coverage(source, observations, frames, policy):
        """自定义类型按锁定图和真实调用回执展示处理覆盖。"""
        windows = []
        for start in range(0, int(source.source.duration_ms), 1000):
            end = min(start + 1000, int(source.source.duration_ms))
            states, reasons = {}, {}
            receipts = [
                frame
                for frame in frames
                if frame.get("source_time_range_ms")
                and int(frame["source_time_range_ms"][0]) < end
                and int(frame["source_time_range_ms"][1]) > start
            ]
            for modality in policy["fast_modalities"]:
                modality_receipts = [
                    frame for frame in receipts if modality in frame.get("produces", [])
                ]
                matching = [
                    value
                    for value in observations
                    if value.get("modality") == modality
                    and int(value["timeRange"].get("startMs", 0)) < end
                    and int(value["timeRange"]["endMs"]) > start
                ]
                if matching:
                    states[modality] = "observed"
                elif modality_receipts:
                    states[modality] = "not_observed"
                    reasons[modality] = next(
                        (
                            frame["outcome_reason"]
                            for frame in modality_receipts
                            if frame.get("outcome_reason")
                        ),
                        "processed_without_observation",
                    )
                else:
                    states[modality] = "not_sampled_by_policy"
            for modality in policy["enrichment_modalities"]:
                states[modality] = "queued"
            windows.append(
                {
                    "start_ms": start,
                    "end_ms": end,
                    "sampling_state": "sampled" if receipts else "not_sampled_by_policy",
                    "modality_states": states,
                    "reason_codes": reasons,
                }
            )
        return windows

    @staticmethod
    def _timeline_source(replay_reports: list[Path]) -> tuple[Path, object]:
        """挑出带语义覆盖的那份 Runtime 报告：它才是视频节点真正跑出来的账本。"""
        fallback: tuple[Path, object] | None = None
        for path in replay_reports:
            report = media_pb2.ReplayReport()
            try:
                report.ParseFromString(path.read_bytes())
            except OSError as error:
                raise TaskExecutionError("timeline_replay_report_unreadable") from error
            if fallback is None:
                fallback = (path, report)
            if report.decoded.semantic_coverage:
                return path, report
        if fallback is None:
            raise TaskExecutionError("timeline_upstream_runtime_report_missing")
        return fallback

    def _coverage(
        self,
        source,
        observations: list[dict],
        summaries: dict[str, dict],
        policy: dict[str, int],
        selected: list[tuple[int, int, str]],
        rejected: list[tuple[int, int]],
    ) -> list[dict]:
        """按 1 秒窗口汇报覆盖，并把"没送模型"与"送了但没结果"分开。

        `selected` 来自上游 worker 报告的逐帧记录（`evidence_group` 非空表示这一帧被语义
        计划选中，要么是锚点、要么是证据窗口的一部分）。有它才能区分三种"没有观测"：
        计划本来就没在这里刷新、计划刷了但没有结果、以及根本没有语义计划。
        `rejected` 是被有界数据面拒绝的帧区间：它同样属于"计划刷了但没有结果"，只是原因
        码必须是 `data_plane_retention_rejected`，不能降级成泛化的缺口。
        """
        duration = int(source.source.duration_ms)
        if duration <= 0:
            raise TaskExecutionError("timeline_duration_unknown")
        by_modality: dict[str, list[tuple[int, int]]] = {}
        for item in observations:
            time_range = item.get("timeRange") or {}
            try:
                start, end = int(time_range.get("startMs", 0)), int(time_range["endMs"])
            except (KeyError, TypeError, ValueError):
                continue
            by_modality.setdefault(str(item.get("modality", "")), []).append((start, end))
        has_audio = any(track.track_kind == "audio" for track in source.source.tracks)
        has_video = any(track.track_kind == "video" for track in source.source.tracks)
        planned = [(start, end) for start, end, _trigger in selected]
        triggers = {(start, end): trigger for start, end, trigger in selected}
        coverage = []
        for start in range(0, duration, policy["window_ms"]):
            end = min(start + policy["window_ms"], duration)
            states: dict[str, str] = {}
            reasons: dict[str, str] = {}
            refresh = [
                trigger
                for (item_start, item_end), trigger in triggers.items()
                if item_start < end and item_end > start
            ]
            rejected_here = any(
                item_start < end and item_end > start for item_start, item_end in rejected
            )
            for name, modality, task_name, default in (
                ("ocr", "ocr_blocks", "ocr_fast", "not_sampled_by_policy"),
                ("asr", "asr_segment", "asr_fast", "not_scheduled"),
                ("vlm", "vision.scene_description", "vlm_enrich", "not_sampled_by_policy"),
            ):
                observed = any(
                    item_start < end and item_end > start
                    for item_start, item_end in by_modality.get(modality, [])
                )
                if observed:
                    states[name] = "observed"
                elif name == "asr" and not has_audio:
                    states[name], reasons[name] = "not_applicable", "no_audio_track"
                elif name == "ocr" and not has_video:
                    states[name], reasons[name] = "not_applicable", "no_video_track"
                elif summaries.get(task_name, {}).get("reason"):
                    states[name] = "failed"
                    reasons[name] = str(summaries[task_name]["reason"])[:160]
                elif name != "asr" and refresh:
                    # 语义计划在这里刷新过，却没有留下观测：这是真实的缺口，不是"画面没变"。
                    states[name] = "not_observed"
                    reasons[name] = (
                        RETENTION_REJECTED if rejected_here else "selected_without_observation"
                    )
                elif name != "asr" and planned:
                    # 计划明确覆盖了这一段：画面被逐帧判别过，只是不需要重新送模型。
                    states[name], reasons[name] = (
                        "covered_without_model_refresh",
                        "semantic_coverage",
                    )
                else:
                    states[name] = default
            if "observed" in states.values():
                sampling = "sampled"
            elif not has_audio and not has_video:
                sampling = "not_applicable"
            elif refresh:
                sampling = "semantic_refresh_without_observation"
            elif planned:
                sampling = "covered_without_model_refresh"
            else:
                sampling = "not_sampled_by_policy"
            coverage.append(
                {
                    "start_ms": start,
                    "end_ms": end,
                    "sampling_state": sampling,
                    "modality_states": states,
                    "reason_codes": reasons,
                    "evidence_triggers": sorted(set(refresh)),
                }
            )
        return coverage

    def _ingest_timeline(
        self,
        manifest: dict,
        source,
        units: list[bytes],
        items: list[dict],
        coverage: list[dict],
    ) -> None:
        payload = {
            "intent_id": manifest["intent_id"],
            "source_description_b64": base64.b64encode(
                source.source.SerializeToString(deterministic=True)
            ).decode(),
            "materials_b64": [base64.b64encode(unit).decode() for unit in units],
            "timeline_items": items,
            "coverage": coverage,
        }
        if len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) > RESULT_BODY_LIMIT_HINT:
            raise TaskExecutionError("timeline_ingest_payload_too_large")
        try:
            self.client.ingest_task_timeline(manifest["task"]["task_id"], payload)
        except RuntimeError as error:
            code = str(error).split(": ", 1)[-1]
            raise TaskExecutionError(f"timeline_ingest_failed:{code}") from None

    def _report_result(
        self,
        manifest: dict,
        *,
        intent_id: str,
        success: bool,
        retryable: bool,
        reason: str,
        inputs: int,
        outputs: int,
        started_ms: int,
        completed_ms: int,
        result_ref: str,
    ) -> None:
        receipt = _receipt(
            manifest,
            started_ms=started_ms,
            completed_ms=completed_ms,
            inputs=inputs,
            outputs=outputs,
            reason=reason,
            result_ref=result_ref,
        )
        task = manifest["task"]
        self.client.report_task_result(
            task["task_id"],
            {
                "intent_id": intent_id,
                "run_id": manifest["run"]["run_id"],
                "attempt": task["attempt"],
                "assignment_id": task["assignment_id"],
                "success": success,
                "output_ref": result_ref,
                "retryable": retryable,
                "reason_code": reason,
                "error_detail": "",
                "receipt": receipt,
            },
        )
