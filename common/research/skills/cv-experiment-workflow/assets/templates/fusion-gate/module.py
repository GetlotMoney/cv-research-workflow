"""可关闭的融合门接口骨架；不包含科学机制实现。"""


class FusionGate:
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    def __call__(self, primary: object, secondary: object) -> object:
        del secondary
        if self.enabled:
            raise NotImplementedError("请实现并验证用户定义的融合机制")
        return primary
