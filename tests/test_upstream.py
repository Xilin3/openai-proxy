import io
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from bps_proxy.auth import ChatGPTSession
from bps_proxy.upstream import UpstreamError, iter_events


class Response(io.BytesIO):
    def __init__(self, data, content_type="text/event-stream; charset=utf-8"):
        super().__init__(data)
        self.headers = {"Content-Type": content_type}


class UpstreamTest(unittest.TestCase):
    session = ChatGPTSession("fake-token", "test", "", 0)

    def read(self, raw):
        response = Response(raw)
        with patch("bps_proxy.upstream.request.urlopen", return_value=response):
            events = list(iter_events(self.session, {}))
        self.assertTrue(response.closed)
        return events

    def test_multiline_comments_crlf_and_trailing_event(self):
        events = self.read(b': ping\r\nevent: response.created\r\ndata: {"type":\r\ndata: "response.created"}\r\n\r\ndata: {"type":"response.completed"}')
        self.assertEqual([event for event, _ in events], ["response.created", "response.completed"])

    def test_done_stops_reading(self):
        events = self.read(b'data: [DONE]\n\ndata: {"type":"unexpected"}\n\n')
        self.assertEqual(events, [])

    def test_invalid_event_is_not_silently_lost(self):
        for data in (b"data: broken\n\n", b"data: []\n\n", b'data: {"text":"\xff"}\n\n'):
            with self.subTest(data=data), self.assertRaises(UpstreamError):
                self.read(data)

    def test_non_sse_response_is_rejected(self):
        response = Response(b"<html>login</html>", "text/html")
        with patch("bps_proxy.upstream.request.urlopen", return_value=response):
            with self.assertRaises(UpstreamError):
                list(iter_events(self.session, {}))
        self.assertTrue(response.closed)

    def test_network_failure_becomes_a_gateway_error(self):
        with patch("bps_proxy.upstream.request.urlopen", side_effect=URLError("connection refused")):
            with self.assertRaises(UpstreamError) as raised:
                list(iter_events(self.session, {}))
        self.assertEqual(raised.exception.status, 502)

    def test_timeout_becomes_a_gateway_timeout(self):
        with patch("bps_proxy.upstream.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(UpstreamError) as raised:
                list(iter_events(self.session, {}))
        self.assertEqual(raised.exception.status, 504)

    def test_closing_iterator_closes_upstream(self):
        response = Response(b'data: {"type":"response.created"}\n\ndata: {"type":"response.completed"}\n\n')
        with patch("bps_proxy.upstream.request.urlopen", return_value=response):
            events = iter_events(self.session, {})
            next(events)
            events.close()
        self.assertTrue(response.closed)

    def test_http_error_body_is_bounded_and_closed(self):
        response = Response(b'<html>private implementation details</html>', "text/html")
        error = HTTPError("https://example.invalid", 502, "bad gateway", {}, response)
        with patch("bps_proxy.upstream.request.urlopen", side_effect=error):
            with self.assertRaises(UpstreamError) as raised:
                list(iter_events(self.session, {}))
        self.assertEqual(raised.exception.status, 502)
        self.assertNotIn("private", raised.exception.message)
        self.assertTrue(response.closed)

    def test_oversized_event_is_rejected(self):
        with patch("bps_proxy.upstream.MAX_EVENT_BYTES", 32), self.assertRaises(UpstreamError):
            self.read(b"data: " + b"x" * 33 + b"\n\n")

    def test_utf8_bom_is_accepted(self):
        events = self.read(b'\xef\xbb\xbfdata: {"type":"response.completed"}\n\n')
        self.assertEqual(events[0][0], "response.completed")


if __name__ == "__main__":
    unittest.main()
