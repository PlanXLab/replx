import unittest


class TestConnectionInfoLookup(unittest.TestCase):
    def test_case_insensitive_lookup_on_windows(self):
        from replx.cli.commands import utility

        # Force Windows behavior without touching sys.platform
        old_is_windows = utility._is_windows
        utility._is_windows = lambda: True
        try:
            connections = {
                "COM24": {"version": "1.27.0", "core": "ESP32C5", "device": "ESP32C5"},
                "com1": {"version": "1.27.0", "core": "RP2350", "device": "ticle-lite"},
            }

            self.assertEqual(
                utility._get_connection_info_for_port(connections, "com24")["core"],
                "ESP32C5",
            )
            self.assertEqual(
                utility._get_connection_info_for_port(connections, "COM1")["core"],
                "RP2350",
            )
        finally:
            utility._is_windows = old_is_windows


if __name__ == "__main__":
    unittest.main()
