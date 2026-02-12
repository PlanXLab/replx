import unittest


class TestPkgLocalVersion(unittest.TestCase):
    def test_variant_key_matches(self):
        from replx.cli.commands.package import _find_local_pkg_version

        local_meta = {
            "packages": {
                "device:ticle_lite:basic@_ticle": {
                    "version": "1.0.0",
                    "source": "device/_ticle/src/basic.py",
                },
                # Historical oddity: key name doesn't match filename, but source is stable
                "device:ticle_lite:utils@_ticle": {
                    "version": "1.0.0",
                    "source": "device/_ticle/src/utools.py",
                },
            }
        }

        v, missing = _find_local_pkg_version(
            local_meta,
            scope="device",
            target="ticle_lite",
            source_path="device/_ticle/src/basic.py",
            pkg_name="basic",
        )
        self.assertFalse(missing)
        self.assertEqual(v, 1.0)

        v, missing = _find_local_pkg_version(
            local_meta,
            scope="device",
            target="ticle_lite",
            source_path="device/_ticle/src/utools.py",
            pkg_name="utools",
        )
        self.assertFalse(missing)
        self.assertEqual(v, 1.0)


if __name__ == "__main__":
    unittest.main()
