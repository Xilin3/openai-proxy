import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bps_proxy.auth import AuthError, ChatGPTSession, load_session


class AuthTest(unittest.TestCase):
    def test_invalid_jwt_is_an_auth_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            for token in ("header.%%%.signature", "header._w.signature", "header.e30.signature"):
                with self.subTest(token=token):
                    path.write_text(json.dumps({"tokens": {"access_token": token, "account_id": "test"}}))
                    with self.assertRaises(AuthError):
                        load_session(path)

    def test_invalid_file_encoding_is_an_auth_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            path.write_bytes(b"\xff")
            with self.assertRaises(AuthError):
                load_session(path)

    def test_codex_home_is_respected(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 3600}).encode()).decode().rstrip("=")
            token = f"header.{payload}.signature"
            (Path(directory) / "auth.json").write_text(json.dumps({"tokens": {"access_token": token, "account_id": "test"}}))
            with patch.dict("os.environ", {"CODEX_HOME": directory}), patch(
                "bps_proxy.auth.Path.home", return_value=Path(directory) / "unused-home"
            ):
                session = load_session()
            self.assertEqual(session.account_id, "test")
            self.assertEqual(session.access_token, token)

    def test_session_repr_does_not_expose_token(self):
        session = ChatGPTSession("fake-sensitive-token", "test", "user", 0)
        self.assertNotIn("fake-sensitive-token", repr(session))


if __name__ == "__main__":
    unittest.main()
