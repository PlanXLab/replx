import unittest

from replx.cli.helpers.compiler import CompilerHelper


class TestCompilerArch(unittest.TestCase):
    def test_esp32c5_maps_to_riscv(self):
        args = CompilerHelper._march_for_core("ESP32C5", "1.27.0")
        self.assertIn("-march=rv32imc", args)

    def test_esp32s2_maps_to_xtensa(self):
        args = CompilerHelper._march_for_core("ESP32S2", "1.27.0")
        self.assertIn("-march=xtensa", args)


if __name__ == "__main__":
    unittest.main()
