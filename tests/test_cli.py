import io
import unittest
from unittest.mock import patch

from bps_proxy.__main__ import main


class CliTest(unittest.TestCase):
    def test_invalid_admission_options_fail_before_state(self):
        for args in (['--max-concurrent', '0'], ['--max-pending', '-1'],
                     ['--queue-timeout', 'nan'], ['--queue-timeout', '0'],
                     ['--upstream-rps', '0'], ['--upstream-rps', '-1']):
            with self.subTest(args=args), patch('sys.argv', ['bps-proxy', *args]), patch(
                    'sys.stderr', new_callable=io.StringIO), patch('bps_proxy.__main__.CallMemory') as memory:
                with self.assertRaises(SystemExit) as raised:
                    main()
                self.assertEqual(raised.exception.code, 2)
                memory.assert_not_called()

    def test_invalid_bind_arguments_fail_before_loading_state(self):
        for args in (["--host", "0.0.0.0"], ["--port", "0"], ["--port", "65536"]):
            with self.subTest(args=args), patch("sys.argv", ["bps-proxy", *args]), patch(
                "sys.stderr", new_callable=io.StringIO
            ), patch("bps_proxy.__main__.CallMemory") as memory, patch("bps_proxy.__main__.serve") as serve:
                with self.assertRaises(SystemExit) as raised:
                    main()
                self.assertEqual(raised.exception.code, 2)
                memory.assert_not_called()
                serve.assert_not_called()

    def test_default_and_explicit_request_limits_reach_server(self):
        cases = [([], dict(max_concurrent=8, max_pending=32, queue_timeout=120, upstream_rps=5)),
                 (['--max-concurrent', '3', '--max-pending', '4', '--queue-timeout', '7',
                   '--upstream-rps', '2'],
                  dict(max_concurrent=3, max_pending=4, queue_timeout=7, upstream_rps=2))]
        for args, expected in cases:
            with self.subTest(args=args), patch('sys.argv', ['bps-proxy', *args]), patch(
                    'sys.stdout', new_callable=io.StringIO), patch('bps_proxy.__main__.CallMemory'), patch(
                    'bps_proxy.__main__.serve') as serve, patch('bps_proxy.__main__.logging.basicConfig'):
                main()
            self.assertEqual(serve.call_args.kwargs, expected)

    def test_ipv6_config_uses_brackets(self):
        with patch("sys.argv", ["bps-proxy", "--host", "::1"]), patch(
            "sys.stdout", new_callable=io.StringIO
        ) as output, patch("bps_proxy.__main__.CallMemory"), patch("bps_proxy.__main__.serve"), patch(
            "bps_proxy.__main__.logging.basicConfig"
        ):
            main()
        self.assertIn('openai_base_url = "http://[::1]:8787/v1"', output.getvalue())
        self.assertNotIn('model_providers', output.getvalue())


if __name__ == "__main__":
    unittest.main()
