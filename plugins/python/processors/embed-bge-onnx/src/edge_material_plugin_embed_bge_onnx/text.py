"""上游观测 → 待编码文本：只认一种输入形态，越界一律显式失败。

本插件消费的是**上游观测里已经存在的文字**，不是自己重新识别一遍：`payload.blocks[].text`
（OCR 插件的输出形态）。因此这里做三件事，且每件事都必须可观测：

- 准入：modality 不是 `ocr_blocks` 就显式拒绝（不猜别的形态、不"尽力而为"）；
- 抽取：把块文本按固定分隔符拼成一段文本，并把**实际被编码的那段文本**的摘要算出来；
- 边界：块数量与字符数都有上限，越界是失败而不是静默截断；一段文字都没有也是失败
  （给空文本算向量等于给"没有内容"编一个语义）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from edge_material_sdk import PluginError
from edge_material_sdk.generated.common.v1 import common_pb2 as common

INPUT_MODALITY = "ocr_blocks"
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
    if modality != INPUT_MODALITY:
        raise PluginError(common.INVALID_INPUT, f"unsupported_input_modality:{modality or 'unset'}")
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
        # 空文本不是"向量为 0 的结果"，是本插件不该被调用的输入：显式失败，不编造语义。
        raise PluginError(common.INVALID_INPUT, "input_text_empty")
    if len(text) > MAX_TOTAL_CHARS:
        raise PluginError(common.INVALID_INPUT, "input_text_exceeds_bound")
    return TextSource(
        text=text,
        text_sha256=text_digest(text),
        block_count=len(listed),
        blank_blocks=blank,
        char_count=len(text),
        source_modality=modality,
        join_separator=JOIN_SEPARATOR,
    )
