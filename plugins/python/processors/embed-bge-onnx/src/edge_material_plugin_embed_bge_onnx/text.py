"""上游 Observation → 待编码文本：只认受控形态，越界一律显式失败。

本插件消费的是上游已经产出的文字，不重新识别媒体。首期只接受两种已版本化的事实形态：

- OCR 的 `ocr_blocks`：`payload.blocks[].text`；
- VLM 的 `vision.scene_description`：`payload.text`。

这不是任意 JSON 文本提取器：ASR 或未知 modality 必须显式拒绝，避免在没有独立语义契约时把
内容悄悄混入索引。两种形态最终都会计算**实际被编码文本**的摘要；块数和字符数都有上限，空文本
与越界同样不生成向量。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from edge_material_sdk import PluginError
from edge_material_sdk.generated.common.v1 import common_pb2 as common

INPUT_MODALITY = "ocr_blocks"
VLM_INPUT_MODALITY = "vision.scene_description"
INPUT_MODALITIES = frozenset({INPUT_MODALITY, VLM_INPUT_MODALITY})
BLOCKS_FIELD = "blocks"
TEXT_FIELD = "text"
# 拼接分隔符写进结果：同一批块用不同分隔符拼会得到不同向量，读者有权知道用的是哪个。
JOIN_SEPARATOR = "\n"
# 一个观测里不可能合理地出现更多文字；越界即失败，不静默截断。
MAX_TEXTS = 256
MAX_TOTAL_CHARS = 4096


@dataclass(frozen=True)
class TextSource:
    """待编码文本及其来源账目（都由真实输入算出，不是配置里写的）。"""

    text: str
    text_sha256: str
    block_count: int
    blank_blocks: int
    char_count: int
    source_modality: str
    join_separator: str

    def payload(self) -> dict:
        return {
            "text": self.text,
            "text_sha256": self.text_sha256,
            "block_count": self.block_count,
            "blank_blocks": self.blank_blocks,
            "char_count": self.char_count,
            "source_modality": self.source_modality,
            "join_separator": self.join_separator,
        }


def text_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def collect_text(observation) -> TextSource:
    """从上游观测里抽出待编码文本；任何不符合契约的形态都在这里显式失败。"""
    modality = observation.modality
    if modality == INPUT_MODALITY:
        return _collect_ocr_text(observation)
    if modality == VLM_INPUT_MODALITY:
        return _collect_vlm_text(observation)
    raise PluginError(common.INVALID_INPUT, f"unsupported_input_modality:{modality or 'unset'}")


def _collect_ocr_text(observation) -> TextSource:
    """拼接 OCR 文字块；块序和分隔符都是向量内容的一部分。"""
    payload = observation.payload
    if BLOCKS_FIELD not in payload:
        raise PluginError(common.INVALID_INPUT, "input_payload_missing_blocks")
    blocks = payload[BLOCKS_FIELD]
    if blocks is None:
        raise PluginError(common.INVALID_INPUT, "input_payload_missing_blocks")
    listed = list(blocks)
    if len(listed) > MAX_TEXTS:
        raise PluginError(common.INVALID_INPUT, "input_block_count_exceeds_bound")
    pieces: list[str] = []
    blank = 0
    for block in listed:
        if TEXT_FIELD not in block:
            raise PluginError(common.INVALID_INPUT, "input_block_missing_text")
        value = block[TEXT_FIELD]
        if not isinstance(value, str):
            raise PluginError(common.INVALID_INPUT, "input_block_text_not_string")
        stripped = value.strip()
        if stripped:
            pieces.append(stripped)
        else:
            blank += 1
    text = JOIN_SEPARATOR.join(pieces)
    if not text:
        raise PluginError(common.INVALID_INPUT, "input_text_empty")
    if len(text) > MAX_TOTAL_CHARS:
        raise PluginError(common.INVALID_INPUT, "input_text_exceeds_bound")
    return TextSource(
        text=text,
        text_sha256=text_digest(text),
        block_count=len(listed),
        blank_blocks=blank,
        char_count=len(text),
        source_modality=INPUT_MODALITY,
        join_separator=JOIN_SEPARATOR,
    )


def _collect_vlm_text(observation) -> TextSource:
    """读取 VLM 的单句场景描述；prompt 和帧元数据都不是可检索事实。"""
    payload = observation.payload
    if TEXT_FIELD not in payload or payload[TEXT_FIELD] is None:
        raise PluginError(common.INVALID_INPUT, "input_payload_missing_text")
    value = payload[TEXT_FIELD]
    if not isinstance(value, str):
        raise PluginError(common.INVALID_INPUT, "input_text_not_string")
    text = value.strip()
    if not text:
        raise PluginError(common.INVALID_INPUT, "input_text_empty")
    if len(text) > MAX_TOTAL_CHARS:
        raise PluginError(common.INVALID_INPUT, "input_text_exceeds_bound")
    return TextSource(
        text=text,
        text_sha256=text_digest(text),
        block_count=1,
        blank_blocks=0,
        char_count=len(text),
        source_modality=VLM_INPUT_MODALITY,
        join_separator="",
    )
