"""最小 PNG 编码器：只支持 8-bit RGBA（彩色类型 6），只用标准库。

为什么不用 Pillow：这个插件是端侧常驻进程，运行环境只应包含 SDK + 标准库；
每帧一次的编码不值得引入一个二进制 wheel 依赖。输出是标准 PNG，
任何解码器或模型服务都能读。
"""

from __future__ import annotations

import struct
import zlib

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_COMPRESSION_LEVEL = 6


def _chunk(tag: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)


def encode_rgba(width: int, height: int, pixels: bytes, stride: int = 0) -> bytes:
    """把 RGBA8 像素编码成 PNG。`stride=0` 表示紧凑排布（行距 = width * 4）。"""
    if width <= 0 or height <= 0:
        raise ValueError("invalid_image_size")
    row_bytes = width * 4
    stride = stride or row_bytes
    if stride < row_bytes:
        raise ValueError("stride_smaller_than_row")
    if len(pixels) < stride * (height - 1) + row_bytes:
        raise ValueError("pixel_buffer_too_small")
    raw = bytearray()
    for row in range(height):
        start = row * stride
        raw.append(0)  # filter type 0 (None)：模型输入不需要预测滤波
        raw += pixels[start : start + row_bytes]
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"".join(
        (
            _SIGNATURE,
            _chunk(b"IHDR", header),
            _chunk(b"IDAT", zlib.compress(bytes(raw), _COMPRESSION_LEVEL)),
            _chunk(b"IEND", b""),
        )
    )
