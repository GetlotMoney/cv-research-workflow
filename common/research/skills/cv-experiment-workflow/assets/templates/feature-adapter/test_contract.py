import unittest

from module import FeatureAdapter


class ContractTest(unittest.TestCase):
    def test_disabled_adapter_is_identity(self) -> None:
        value = object()
        self.assertIs(value, FeatureAdapter(enabled=False)(value))


if __name__ == "__main__":
    unittest.main()
