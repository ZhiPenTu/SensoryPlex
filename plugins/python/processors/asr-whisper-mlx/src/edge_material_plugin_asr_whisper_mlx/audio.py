"""把数据面里的音频字节变成 MLX Whisper 的输入，只做可复算、可解释的转换。

每一步都是**输入契约的一部分**，而不是"顺手做的归一化"：

- 样本布局只认 descriptor 报出来的 `sample_format`；未知布局显式拒绝。
  按猜的宽度（比如一律当 4 字节/样本）解释字节，会静默改变下游模型看到的内容；
- 多声道按算术平均下混成单声道：确定性的下混，而不是"取第一路"这种随实现而变的选择；
- 采样率用多相重采样换算到 Whisper 的固定入口采样率（16 kHz）；不做零阶保持那种
  会引入镜像频谱的取巧做法；
- 空载荷、非整帧对齐的载荷都显式失败：它们不是"安静"，是数据不完整。
"""

from __future__ import annotations

import math

import numpy as np
from scipy import signal

WHISPER_SAMPLE_RATE = 16_000
SUPPORTED_SAMPLE_FORMAT = "F32LE"
SUPPORTED_SAMPLE_BYTES = 4


class AudioError(Exception):
    """带稳定原因串的显式失败；原因串本身就是契约，不用日志代替它。"""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def frames_from_f32le(payload: bytes, sample_format: str, channels: int) -> np.ndarray:
    """按 interleaved F32LE 解释载荷，返回 `(frames, channels)` 的只读视图。"""
    if sample_format != SUPPORTED_SAMPLE_FORMAT:
        raise AudioError(f"unsupported_sample_format:{sample_format or 'unset'}")
    if channels <= 0:
        raise AudioError("audio_channel_count_unknown")
    if not payload:
        raise AudioError("empty_audio_payload")
    frame_bytes = channels * SUPPORTED_SAMPLE_BYTES
    if len(payload) % frame_bytes:
        raise AudioError("audio_payload_not_frame_aligned")
    # `<f4` 就是协议里写明的 F32LE；不换成平台原生序，否则在大端机器上又是另一种猜测。
    return np.frombuffer(payload, dtype="<f4").reshape(-1, channels)


def to_mono(frames: np.ndarray) -> np.ndarray:
    if frames.shape[1] == 1:
        return np.ascontiguousarray(frames[:, 0], dtype=np.float32)
    return np.ascontiguousarray(frames.mean(axis=1), dtype=np.float32)


def resample(array: np.ndarray, source_rate: int, target_rate: int = WHISPER_SAMPLE_RATE):
    """把单声道波形换算到目标采样率；整数比时用多相滤波，避免镜像频谱。"""
    if source_rate <= 0:
        raise AudioError("audio_sample_rate_unknown")
    if source_rate == target_rate:
        return np.ascontiguousarray(array, dtype=np.float32)
    divisor = math.gcd(source_rate, target_rate)
    converted = signal.resample_poly(
        array.astype(np.float64), target_rate // divisor, source_rate // divisor
    )
    return np.ascontiguousarray(converted, dtype=np.float32)


def prepare(
    payload: bytes, sample_format: str, source_rate: int, channels: int
) -> tuple[np.ndarray, dict]:
    """返回 `(16 kHz 单声道 float32, 输入事实)`；输入事实用于报告，不是估计值。"""
    frames = frames_from_f32le(payload, sample_format, channels)
    mono = to_mono(frames)
    resampled = resample(mono, source_rate)
    stats = {
        "sample_format": sample_format,
        "input_sample_rate": int(source_rate),
        "input_channels": int(channels),
        "input_samples": int(frames.shape[0]),
        "whisper_sample_rate": WHISPER_SAMPLE_RATE,
        "whisper_samples": int(resampled.shape[0]),
    }
    return resampled, stats
