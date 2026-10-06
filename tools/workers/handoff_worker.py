"""作为**独立进程**消费 lease：经 gRPC 领取读取窗口，按段名 mmap 读 arena，校验摘要后显式释放。

这个脚本刻意不知道媒体文件、不参与解码、也不拥有任何缓冲：它只能看到 Runtime 授予的窗口。
它证明的是数据面契约，而不是"Python 也能读文件"：

- 只有 lease 才会告诉消费者段名（段名按运行随机派生，无法从报告里的 arena handle 推导）；
- descriptor 的摘要覆盖**被授予的窗口**，不是整段共享内存；
- 越界、未知 buffer、非法 TTL、重复领取、迟到释放一律得到显式拒绝码；
- lease 过期后 buffer 被回收，迟到的释放与再次领取都必须失败；
- 消费者把整段映射进自己的地址空间，因此同 UID 进程之间**没有**逐 buffer 的内存隔离 ——
  这一点作为显式限制上报（`same_uid_segment_visibility`），不伪装成安全边界。
"""

import argparse
import hashlib
import json
import mmap
import os
import sys
import time

import grpc
from edge_material_sdk.generated.media.v1 import handoff_pb2, handoff_pb2_grpc

try:
    from _posixshmem import shm_open
except ImportError as error:  # pragma: no cover - 非 POSIX 平台无法消费共享内存
    raise SystemExit(f"POSIX shared memory is required to consume a lease: {error}") from error

# lease 过期路径用的短 TTL：必须是服务端接受的范围（50–60000 ms）内的真实值。
SHORT_TTL_MS = 50
SHORT_TTL_SETTLE_S = 0.4
# 子窗口摘要测试使用的窗口长度。
SUB_WINDOW_BYTES = 1 << 16
DEFAULT_TIMEOUT_S = 60.0


class Report:
    """把每条断言写进结构化结果，便于编排脚本复算，而不是只靠人类读日志。"""

    def __init__(self):
        self.checks: list[dict] = []
        self.failures: list[dict] = []
        self.notes: dict[str, object] = {}

    def check(self, name: str, ok: bool, detail: object = "") -> bool:
        record = {"name": name, "ok": bool(ok), "detail": detail}
        self.checks.append(record)
        if not ok:
            self.failures.append(record)
        return bool(ok)

    def equal(self, name: str, actual, expected) -> bool:
        return self.check(name, actual == expected, f"actual={actual!r} expected={expected!r}")

    def as_dict(self) -> dict:
        return {
            "checks": self.checks,
            "failures": self.failures,
            "checks_total": len(self.checks),
            "checks_failed": len(self.failures),
            **self.notes,
        }


def reason_code(response) -> str:
    if response.HasField("error"):
        return response.error.reason_code
    return ""


def digest_of(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def read_window(mapping, offset: int, length: int) -> bytes:
    return bytes(mapping[offset : offset + length])


class Consumer:
    """gRPC 客户端，顺带把授权/释放次数记清楚，供编排脚本对账。"""

    def __init__(self, stub, report: Report, timeout: float):
        self.stub = stub
        self.report = report
        self.timeout = timeout
        self.grants = 0
        self.releases = 0

    def list(self):
        return self.stub.List(handoff_pb2.ListRetainedRequest(), timeout=self.timeout)

    def stats(self):
        return self.stub.Stats(handoff_pb2.HandoffStatsRequest(), timeout=self.timeout).stats

    def acquire(self, buffer_id: str, offset: int = 0, length: int = 0, ttl_ms: int = 0):
        response = self.stub.Acquire(
            handoff_pb2.AcquireBufferRequest(
                buffer_id=buffer_id, offset_bytes=offset, length_bytes=length, ttl_ms=ttl_ms
            ),
            timeout=self.timeout,
        )
        if response.granted:
            self.grants += 1
        return response

    def release(self, lease_id: str):
        response = self.stub.Release(
            handoff_pb2.ReleaseBufferRequest(lease_id=lease_id), timeout=self.timeout
        )
        if response.released:
            self.releases += 1
        return response

    def refuse(self, name: str, expected: str, **kwargs) -> None:
        response = self.acquire(**kwargs)
        self.report.check(f"refused.{name}", not response.granted, reason_code(response))
        self.report.equal(f"refused.{name}.reason", reason_code(response), expected)


def verify_window(report: Report, mapping, entry, descriptor, label: str) -> None:
    """按 descriptor 校验一条被授予的窗口：locator、几何与摘要。"""
    locator = descriptor.locator
    report.equal(f"{label}.locator_length", locator.length, entry.length_bytes)
    report.equal(f"{label}.locator_offset", locator.offset, entry.offset_bytes)
    report.equal(f"{label}.kind", descriptor.kind, entry.kind)
    report.equal(f"{label}.stream", descriptor.stream_id, entry.stream_id)
    report.check(f"{label}.lease_read_only", descriptor.lease.read_only, descriptor.lease.lease_id)
    report.check(
        f"{label}.lease_expiry_set",
        descriptor.lease.expires_at_unix_ms > 0,
        descriptor.lease.expires_at_unix_ms,
    )
    report.check(f"{label}.handle_is_opaque", "/" not in locator.handle, locator.handle)
    report.equal(f"{label}.memory_kind", descriptor.memory_kind, "cpu_shared_memory")
    payload = read_window(mapping, locator.offset, locator.length)
    report.equal(f"{label}.digest", digest_of(payload), descriptor.content_hash)


def consume_video(report: Report, consumer: Consumer, mapping, entry, listing) -> None:
    """整条视频 buffer：摘要、邻接字节、窗口边界，然后释放。"""
    response = consumer.acquire(entry.buffer_id)
    if not report.check("video.granted", response.granted, reason_code(response)):
        return
    descriptor = response.buffer
    report.check("video.segment_matches_listing", response.segment_name == listing.segment_name)
    report.check(
        "video.capacity_matches_listing",
        response.arena_capacity_bytes == listing.stats.arena_capacity_bytes,
    )
    verify_window(report, mapping, entry, descriptor, "video")
    report.equal("video.buffer_digest_matches_listing", entry.content_hash, descriptor.content_hash)

    start, length = entry.offset_bytes, entry.length_bytes
    neighbour = next(
        (
            other
            for other in listing.buffers
            if other.buffer_id != entry.buffer_id and other.offset_bytes >= start + length
        ),
        None,
    )
    report.check("video.has_neighbour_in_segment", neighbour is not None)
    if neighbour is not None:
        report.check(
            "video.neighbour_is_a_different_payload",
            neighbour.content_hash != entry.content_hash,
        )
        shifted = read_window(mapping, start + 1, length)
        report.check(
            "video.digest_pins_the_exact_window",
            digest_of(shifted) != descriptor.content_hash,
            "a one-byte-shifted window must not hash to the lease digest",
        )
        report.notes["neighbour_offset_bytes"] = neighbour.offset_bytes

    # 窗口只能落在自己的 buffer 内：跨到邻居、或越过 buffer 末尾都必须被拒绝。
    consumer.refuse(
        "spanning_two_buffers",
        "mapping_out_of_range",
        buffer_id=entry.buffer_id,
        offset=length - 1,
        length=64,
    )
    consumer.refuse(
        "window_past_the_buffer",
        "mapping_out_of_range",
        buffer_id=entry.buffer_id,
        offset=length + 1,
        length=1,
    )

    released = consumer.release(descriptor.lease.lease_id)
    report.check("video.released", released.released, reason_code(released))
    report.equal("video.released_buffer_id", released.buffer_id, entry.buffer_id)


def consume_sub_window(report: Report, consumer: Consumer, mapping, candidates, taken: set[str]):
    """子窗口：摘要只覆盖被授予的那一段，locator 指向窗口起点。

    需要一条**未被领走**、且足够长的 buffer：窗口是整条 buffer 的子集，而 buffer 一经释放
    就不再保留，所以这里用另一条视频帧来测。没有符合条件的样本时如实记为"未测"。
    """
    entry = next(
        (
            candidate
            for candidate in candidates
            if candidate.buffer_id not in taken
            and candidate.kind == "video_frame"
            and candidate.length_bytes > SUB_WINDOW_BYTES
        ),
        None,
    )
    report.notes["sub_window_tested"] = entry is not None
    if entry is None:
        return
    window_offset = entry.length_bytes - SUB_WINDOW_BYTES
    sub = consumer.acquire(entry.buffer_id, offset=window_offset, length=SUB_WINDOW_BYTES)
    if not report.check("video.sub_window_granted", sub.granted, reason_code(sub)):
        return
    locator = sub.buffer.locator
    report.equal(
        "video.sub_window_locator_offset", locator.offset, entry.offset_bytes + window_offset
    )
    report.equal("video.sub_window_locator_length", locator.length, SUB_WINDOW_BYTES)
    payload = read_window(mapping, locator.offset, locator.length)
    report.equal("video.sub_window_digest", digest_of(payload), sub.buffer.content_hash)
    report.check(
        "video.sub_window_digest_differs_from_buffer",
        sub.buffer.content_hash != entry.content_hash,
    )
    report.equal(
        "video.sub_window_time_range_unchanged",
        sub.buffer.time_range.end_ms - sub.buffer.time_range.start_ms,
        entry.time_range.end_ms - entry.time_range.start_ms,
    )
    released = consumer.release(sub.buffer.lease.lease_id)
    report.check("video.sub_window_released", released.released, reason_code(released))


def consume_audio(report: Report, consumer: Consumer, mapping, entry) -> None:
    """音频 buffer：单消费者语义 + 摘要校验，然后释放。"""
    response = consumer.acquire(entry.buffer_id)
    if not report.check("audio.granted", response.granted, reason_code(response)):
        return
    verify_window(report, mapping, entry, response.buffer, "audio")
    second = consumer.acquire(entry.buffer_id)
    report.check("audio.single_consumer_enforced", not second.granted, reason_code(second))
    report.equal("audio.single_consumer_reason", reason_code(second), "buffer_already_leased")
    released = consumer.release(response.buffer.lease.lease_id)
    report.check("audio.released", released.released, reason_code(released))
    report.equal("audio.released_buffer_id", released.buffer_id, entry.buffer_id)


def consume_expiry(report: Report, consumer: Consumer, listing, taken: set[str]) -> None:
    """lease 过期路径：过期后 buffer 被回收，迟到释放与再次领取都必须失败。"""
    candidate = next(
        (entry for entry in listing.buffers if entry.buffer_id not in taken), listing.buffers[0]
    )
    before = consumer.stats().expired_total
    response = consumer.acquire(candidate.buffer_id, ttl_ms=SHORT_TTL_MS)
    if not report.check("expiry.granted", response.granted, reason_code(response)):
        return
    lease_id = response.buffer.lease.lease_id
    time.sleep(SHORT_TTL_SETTLE_S)
    late = consumer.release(lease_id)
    report.check("expiry.late_release_refused", not late.released, late.buffer_id)
    report.equal("expiry.late_release_reason", reason_code(late), "unknown_or_released_lease")
    again = consumer.acquire(candidate.buffer_id)
    report.check("expiry.reacquire_refused", not again.granted, reason_code(again))
    report.equal("expiry.reacquire_reason", reason_code(again), "unknown_buffer")
    after = consumer.stats()
    report.check("expiry.expired_counter", after.expired_total > before, after.expired_total)
    report.check(
        "expiry.buffer_no_longer_listed",
        all(entry.buffer_id != candidate.buffer_id for entry in consumer.list().buffers),
    )
    report.notes["expired_buffer_id"] = candidate.buffer_id


def consume_refusals(report: Report, consumer: Consumer, listing) -> None:
    """非法请求的显式拒绝码：不夹取、不猜测、不静默成功。"""
    entry = listing.buffers[0]
    consumer.refuse("ttl_below_minimum", "invalid_lease_ttl", buffer_id=entry.buffer_id, ttl_ms=1)
    consumer.refuse(
        "offset_without_length",
        "ambiguous_window",
        buffer_id=entry.buffer_id,
        offset=8,
        length=0,
    )
    consumer.refuse("unknown_buffer", "unknown_buffer", buffer_id="buf-does-not-exist")
    unknown_lease = consumer.release("lease-does-not-exist")
    report.check("refused.unknown_lease_release", not unknown_lease.released)
    report.equal(
        "refused.unknown_lease_release.reason",
        reason_code(unknown_lease),
        "unknown_or_released_lease",
    )


def drain(report: Report, consumer: Consumer, mapping) -> None:
    """把此刻仍保留的 buffer 全部读走并释放：数据面不留悬挂引用。"""
    remaining = consumer.list().buffers
    drained = 0
    mismatched = 0
    for entry in remaining:
        response = consumer.acquire(entry.buffer_id)
        if not response.granted:
            mismatched += 1
            report.check(f"drain.{entry.buffer_id}", False, reason_code(response))
            continue
        locator = response.buffer.locator
        payload = read_window(mapping, locator.offset, locator.length)
        if digest_of(payload) != response.buffer.content_hash:
            mismatched += 1
            report.check(f"drain.{entry.buffer_id}", False, "digest mismatch")
        released = consumer.release(response.buffer.lease.lease_id)
        if not released.released or released.buffer_id != entry.buffer_id:
            mismatched += 1
            report.check(f"drain.{entry.buffer_id}", False, reason_code(released))
        drained += 1
    report.notes["drain_attempted"] = len(remaining)
    report.notes["drained_buffers"] = drained
    report.check(
        "drain.everything_consumed",
        drained == len(remaining) and mismatched == 0,
        f"drained={drained} remaining={len(remaining)} mismatched={mismatched}",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", required=True, help="runtime handoff endpoint, host:port")
    parser.add_argument("--report", type=str, default=None, help="write the JSON result here")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    args = parser.parse_args()

    report = Report()
    report.notes["worker_pid"] = os.getpid()
    report.notes["listen"] = args.listen
    report.notes["same_uid_segment_visibility"] = (
        "POSIX 共享内存按段授权：同 UID 进程映射整段后可以看到相邻 buffer 的字节；"
        "lease 是契约级约束（不越界、摘要绑定窗口、单消费者、TTL），不是逐 buffer 的内存隔离"
    )

    channel = grpc.insecure_channel(args.listen)
    grpc.channel_ready_future(channel).result(timeout=args.timeout)
    consumer = Consumer(handoff_pb2_grpc.BufferHandoffServiceStub(channel), report, args.timeout)

    listing = consumer.list()
    segment_name = listing.segment_name
    report.notes["segment_name"] = segment_name
    report.notes["retained_total"] = listing.stats.retained_total
    report.notes["offered_total"] = listing.stats.offered_total
    report.notes["retained_at_connect"] = listing.stats.retained
    report.check("listing.segment_name", bool(segment_name), segment_name)
    report.check(
        "listing.segment_name_is_bounded",
        segment_name.startswith("/") and segment_name.count("/") == 1 and len(segment_name) < 32,
        segment_name,
    )
    report.check("listing.retained_non_empty", listing.stats.retained > 0, listing.stats.retained)
    report.check(
        "listing.rejection_reasons_reported",
        listing.stats.retain_rejections + listing.stats.request_rejections
        == sum(listing.stats.rejection_reasons.values()),
        dict(listing.stats.rejection_reasons),
    )
    report.notes["retain_rejections"] = listing.stats.retain_rejections
    report.check(
        "listing.retention_accounting",
        listing.stats.offered_total == listing.stats.retained + listing.stats.retain_rejections,
        f"offered={listing.stats.offered_total} retained={listing.stats.retained} "
        f"rejected={listing.stats.retain_rejections}",
    )
    report.notes["rejection_reasons"] = dict(listing.stats.rejection_reasons)
    if not listing.buffers:
        report.check("listing.has_buffers", False, "no retained buffers")
        return finish(report, args.report)

    video = [entry for entry in listing.buffers if entry.kind == "video_frame"]
    audio = [entry for entry in listing.buffers if entry.kind.startswith("audio")]
    report.notes["video_buffers"] = len(video)
    report.notes["audio_buffers"] = len(audio)
    report.check("listing.has_video", bool(video), len(video))
    report.check("listing.has_audio", bool(audio), len(audio))

    # 直接用 POSIX shm 打开段名再整段映射：不改写名字，也不注册到 multiprocessing 的
    # 资源追踪器（段的生命周期属于生产者，消费者只关闭自己的映射）。
    fd = shm_open(segment_name, os.O_RDWR, 0o600)
    segment_size = os.fstat(fd).st_size
    mapping = mmap.mmap(fd, segment_size)
    os.close(fd)
    report.notes["segment_size_bytes"] = segment_size
    try:
        report.check(
            "segment.covers_the_advertised_capacity",
            segment_size >= listing.stats.arena_capacity_bytes,
            f"segment={segment_size} capacity={listing.stats.arena_capacity_bytes}",
        )
        taken: set[str] = set()
        ordered_video = sorted(video, key=lambda entry: entry.length_bytes, reverse=True)
        if ordered_video:
            largest_video = ordered_video[0]
            consume_video(report, consumer, mapping, largest_video, listing)
            taken.add(largest_video.buffer_id)
        consume_sub_window(report, consumer, mapping, ordered_video, taken)
        if audio:
            largest_audio = max(audio, key=lambda entry: entry.length_bytes)
            consume_audio(report, consumer, mapping, largest_audio)
            taken.add(largest_audio.buffer_id)
        consume_expiry(report, consumer, listing, taken)
        consume_refusals(report, consumer, listing)
        drain(report, consumer, mapping)
        final = consumer.stats()
        report.check("final.no_buffer_left_leased", final.leased == 0, final.leased)
        report.check("final.arena_released", final.arena_live_slabs == 0, final.arena_live_slabs)
        report.notes["runtime_stats"] = {
            "offered_total": final.offered_total,
            "retained_total": final.retained_total,
            "released_total": final.released_total,
            "expired_total": final.expired_total,
            "retained": final.retained,
            "leased": final.leased,
            "retain_rejections": final.retain_rejections,
            "request_rejections": final.request_rejections,
            "rejection_reasons": dict(final.rejection_reasons),
            "arena_live_slabs": final.arena_live_slabs,
            "arena_used_bytes": final.arena_used_bytes,
            "arena_capacity_bytes": final.arena_capacity_bytes,
        }
    finally:
        # 只关闭自己的映射：段的生命周期由生产者的 `shm_unlink` 负责，消费者不去删名。
        mapping.close()

    report.notes["grants"] = consumer.grants
    report.notes["releases"] = consumer.releases
    report.check(
        "accounting.grants_match_releases_and_expiry",
        consumer.grants == consumer.releases + 1,
        f"grants={consumer.grants} releases={consumer.releases} (one deliberate expiry)",
    )
    return finish(report, args.report)


def finish(report: Report, output: str | None) -> int:
    payload = report.as_dict()
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if output:
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(text)
    print(text)
    if report.failures:
        print(f"handoff worker failed {len(report.failures)} check(s)", file=sys.stderr)
        return 1
    print(f"handoff worker ok: {len(report.checks)} checks, pid={os.getpid()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
