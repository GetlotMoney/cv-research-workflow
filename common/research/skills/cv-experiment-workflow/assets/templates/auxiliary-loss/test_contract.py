import unittest

from module import AuxiliaryLoss


class ContractTest(unittest.TestCase):
    def test_disabled_loss_is_neutral(self) -> None:
        self.assertEqual(0.0, AuxiliaryLoss(enabled=False)(object()))


if __name__ == "__main__":
    unittest.main()
