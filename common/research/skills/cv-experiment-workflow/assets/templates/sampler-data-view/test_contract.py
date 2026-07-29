import unittest

from module import SamplerDataView


class ContractTest(unittest.TestCase):
    def test_disabled_view_is_identity(self) -> None:
        records = object()
        self.assertIs(records, SamplerDataView(enabled=False)(records))


if __name__ == "__main__":
    unittest.main()
