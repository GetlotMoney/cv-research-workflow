"""可关闭的辅助目标接口骨架；不包含科学机制实现。"""


class AuxiliaryLoss:
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    def __call__(self, context: object) -> float:
        del context
        if self.enabled:
            raise NotImplementedError("请实现并验证用户定义的辅助目标")
        return 0.0
