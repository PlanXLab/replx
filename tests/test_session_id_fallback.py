import unittest


class TestSessionIdFallback(unittest.TestCase):
    def test_psutil_failure_falls_back_to_os_getppid(self):
        import replx.cli.agent.client.session as sess
        import psutil

        # Ensure no cached value leaks across tests
        sess.clear_session_cache()

        old_process = sess.psutil.Process
        old_getppid = sess.os.getppid

        try:
            # Force terminal/jupyter detection to fail
            def _boom(*_args, **_kwargs):
                raise psutil.AccessDenied(pid=1, name="x")

            sess.psutil.Process = _boom
            sess.os.getppid = lambda: 4242

            sid = sess.get_session_id()
            self.assertEqual(sid, 4242)
        finally:
            sess.psutil.Process = old_process
            sess.os.getppid = old_getppid
            sess.clear_session_cache()


if __name__ == "__main__":
    unittest.main()
