"""稳定错误码；不把底层异常文本当不变量。"""


class VectorStoreError(Exception):
    """带稳定 reason_code 的向量库错误；调用方按 code 分支，不解析 message。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


class IndexContractError(ValueError):
    """上游 payload 与落库契约不符（维度、key、向量形状）。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")
