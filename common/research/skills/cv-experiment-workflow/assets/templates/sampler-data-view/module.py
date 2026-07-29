"""可关闭的数据视图接口骨架；不包含科学机制实现。"""


class SamplerDataView:
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    def __call__(self, records: object) -> object:
        if self.enabled:
            raise NotImplementedError("请实现并验证用户定义的数据视图")
        return records
