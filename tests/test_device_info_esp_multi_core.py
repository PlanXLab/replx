import unittest


class TestDeviceInfoEspMultiCore(unittest.TestCase):
    def test_parse_esp32p4_external_wifi_c6_reports_variant_core(self):
        from replx.utils.device_info import parse_device_banner

        banner = (
            "MicroPython v1.27.0 on 2026-01-01; "
            "Generic ESP32P4 module with WIFI module of external ESP32C6 with ESP32P4\r\n"
            ">>> "
        )

        parsed = parse_device_banner(banner)
        self.assertIsNotNone(parsed)
        version, core, device, manufacturer = parsed
        self.assertEqual(version, "1.27.0")
        self.assertEqual(core, "ESP32P4C6")
        self.assertEqual(device, "ESP32P4C6")
        self.assertIn("ESP32C6", manufacturer)

    def test_parse_esp32p4_external_wifi_c5_reports_variant_core(self):
        from replx.utils.device_info import parse_device_banner

        banner = (
            "MicroPython v1.27.0 on 2026-01-01; "
            "Generic ESP32P4 module with WIFI module of external ESP32C5 with ESP32P4\r\n"
            ">>> "
        )

        parsed = parse_device_banner(banner)
        self.assertIsNotNone(parsed)
        version, core, device, manufacturer = parsed
        self.assertEqual(version, "1.27.0")
        self.assertEqual(core, "ESP32P4C5")
        self.assertEqual(device, "ESP32P4C5")
        self.assertIn("ESP32C5", manufacturer)

    def test_parse_esp32p4_standalone_reports_base_core(self):
        from replx.utils.device_info import parse_device_banner

        banner = (
            "MicroPython v1.27.0 on 2026-01-01; "
            "Generic ESP32P4 module with ESP32P4\r\n"
            ">>> "
        )

        parsed = parse_device_banner(banner)
        self.assertIsNotNone(parsed)
        _version, core, device, _manufacturer = parsed
        self.assertEqual(core, "ESP32P4")
        self.assertEqual(device, "ESP32P4")


if __name__ == "__main__":
    unittest.main()
