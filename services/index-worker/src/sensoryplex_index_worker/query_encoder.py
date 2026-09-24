"""查询编码器：把查询文本编码成与索引向量**同源**的查询向量（ADR-023）。

为什么在这里、以及为什么是这个形态：距离只有在"查询向量与索引向量出自同一份模型"
（同一份 ONNX 权重、同一份 `tokenizer.json`、同一份 `config.json`）时才有意义。因此本模块
**不重新写一遍装配**，而是把 BGE 插件的 `Start`（`EmbedPlugin.configure`）当库调用：
模型身份、维度实测、池化与归一化语义全部沿用插件那一份实现，检索面不引入第二个模型定义。
这样"插件进程编码观测"与"检索面编码查询"走的是同一条代码路径，不可能各自漂移。

不联网：`model_dir` 必须由运营显式提供；目录缺失、文件不全、ONNX 会话起不来都在这里
以**稳定原因码**失败，不下载权重、不退化到别的模型或 CPU（ADR-016 / ADR-017）。

原因码直接取自插件（`model_dir_required`、`model_file_not_found`、`onnx_session_init_failed`
…），只做一次**字符白名单**校验：插件的原因串里不应出现主机路径，一旦出现就换成通用码，
免得路径从这一跳泄漏到调用方（ADR-010 §不外泄）。
"""

from __future__ import annotations

import re

from edge_material_plugin_embed_bge_onnx import artifact as bge_artifact
from edge_material_plugin_embed_bge_onnx import models as bge_models
from edge_material_plugin_embed_bge_onnx import plugin as bge_plugin

# 原因码形状：冒号前必须是小写 snake token，冒号后允许 ONNX Runtime 的 EP 名
# （`execution_provider_not_selected:CPUExecutionProvider`）与实测值（`512!=384`），
# 最多两段以覆盖 OCR 侧的 `<role>:<actual>`。除这套字符外的形状一律换成通用码：
# 宁可少一个细节，也不要漏一条主机路径。
STABLE_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,59}(:[A-Za-z0-9_.!<>=-]{1,40}){0,2}$")
MAX_STABLE_CODE_LENGTH = 120
GENERIC_CODE = "query_encoder_config_invalid"


class QueryEncoderError(Exception):
    """带稳定原因码的编码器失败；调用方按 code 分支，不解析文本。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


def stable_code(message: str) -> str:
    """把插件抛出的原因串折成稳定码；不合规的形状不进控制面。"""
    text = (message or "").strip()
    if len(text) > MAX_STABLE_CODE_LENGTH:
        return GENERIC_CODE
    return text if STABLE_CODE_PATTERN.match(text) else GENERIC_CODE


class QueryEncoder:
    """一个已 Start 的 BGE 编码器；身份与维度都取自插件实测值，不是配置里写的数字。"""

    def __init__(self, engine: bge_plugin.EmbedPlugin):
        self._engine = engine
        self.artifact_digest = engine.artifact_digest
        self.release_id = engine.model.release_id
        self.dimension = int(engine.model.dimension)
        self.vector_index_key = engine.vector_index_key()
        self.max_length = int(engine.model.max_length)
        self.backend = engine.model.backend

    def encode(self, text: str) -> list[float]:
        try:
            encoded = self._engine.encoder.encode(text)
        except Exception as error:  # noqa: BLE001 - 会话内部细节不进控制面
            raise QueryEncoderError("query_encoding_failed", type(error).__name__) from error
        vector = [float(value) for value in encoded.vector]
        if len(vector) != self.dimension:
            # 实测维度与身份里的维度不一致：这不是"可以截断"的输入，是模型不自洽。
            raise QueryEncoderError(
                "query_dimension_mismatch", f"identity={self.dimension} actual={len(vector)}"
            )
        return vector

    def provenance(self) -> dict:
        """`model_release` 行需要的身份（ADR-025 的消费侧要把它登记进事实库）。

        全部取自插件实测的模型身份与配置摘要，**不是**配置里写的版本号：同一个 release id
        对应两份不同身份是有缺陷的，登记时会显式冲突（`records.ensure_model_release`）。
        """
        model = self._engine.model
        return {
            "model_release_id": model.release_id,
            "name": model.model_id,
            "version": model.model_version,
            "artifact_hash": model.artifact_digest,
            "backend": model.backend,
            "config_hash": self._engine.config.config_hash(),
        }

    def describe(self) -> dict:
        """可观测字段：不含权重目录、不含主机路径。"""
        return {
            "release_id": self.release_id,
            "dimension": self.dimension,
            "vector_index_key": self.vector_index_key,
            "max_length": self.max_length,
            "backend": self.backend,
            "artifact_digest": self.artifact_digest,
        }


def build_query_encoder(
    *,
    model_dir: str,
    model_file: str = "",
    provider: str = "",
    max_length: int = 0,
) -> QueryEncoder:
    """按插件的 `Start` 契约建编码器；配置键与插件完全同一套（ADR-017 的 config 面）。"""
    config: dict = {"model_dir": model_dir}
    if model_file:
        config["model_file"] = model_file
    if provider:
        config["provider"] = provider
    if max_length:
        config["max_length"] = int(max_length)
    engine = bge_plugin.EmbedPlugin(artifact_digest=bge_artifact.package_digest())
    try:
        engine.configure(config)
    except ValueError as error:
        raise QueryEncoderError(stable_code(str(error))) from None
    except Exception as error:  # noqa: BLE001 - 插件之外不该有别的失败形态，兜住并显式化
        raise QueryEncoderError("query_encoder_unavailable", type(error).__name__) from error
    return QueryEncoder(engine)


def default_model_file() -> str:
    return bge_models.DEFAULT_MODEL_FILE
