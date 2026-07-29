import unittest

from module import FusionGate


class ContractTest(unittest.TestCase):
    def test_disabled_gate_passes_primary_through(self) -> None:
        primary = object()
        self.assertIs(primary, FusionGate(enabled=False)(primary, object()))


if __name__ == "__main__":
    unittest.main()
