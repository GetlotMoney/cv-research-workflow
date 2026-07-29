"""可关闭的特征适配接口骨架；不包含科学机制实现。"""


class FeatureAdapter:
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    def __call__(self, value: object) -> object:
        if self.enabled:
            raise NotImplementedError("请实现并验证用户定义的适配机制")
        return value
