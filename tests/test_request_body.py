import gzip
import unittest
import zlib
from email.message import Message
from unittest.mock import patch

import zstandard

from bps_proxy.request_body import RequestBodyError, content_encoding, decode_body


class RequestBodyTest(unittest.TestCase):
    def compressors(self):
        return {'gzip': gzip.compress, 'deflate': zlib.compress,
                'zstd': zstandard.ZstdCompressor(write_checksum=True).compress}

    def test_roundtrip_and_identity(self):
        raw = '{"input":"你好"}'.encode()
        self.assertEqual(decode_body(raw, 'identity', 1024), raw)
        for encoding, compress in self.compressors().items():
            with self.subTest(encoding=encoding):
                self.assertEqual(decode_body(compress(raw), encoding, 1024), raw)

    def test_decoded_limit(self):
        for encoding, compress in self.compressors().items():
            with self.subTest(encoding=encoding), self.assertRaises(RequestBodyError) as error:
                decode_body(compress(b'a' * 65536), encoding, 8192)
            self.assertEqual(error.exception.status, 413)

    def test_unknown_size_zstd_is_bounded(self):
        raw = zstandard.ZstdCompressor(write_content_size=False).compress(b'a' * 65536)
        with self.assertRaises(RequestBodyError) as error:
            decode_body(raw, 'zstd', 8192)
        self.assertEqual(error.exception.status, 413)

    def test_unknown_size_zstd_roundtrip(self):
        value = b'a' * 16384
        raw = zstandard.ZstdCompressor(write_content_size=False).compress(value)
        self.assertEqual(decode_body(raw, 'zstd', 65536), value)

    def test_encoded_limit(self):
        with self.assertRaises(RequestBodyError) as error:
            decode_body(b'a' * 2048, 'identity', 1024)
        self.assertEqual(error.exception.status, 413)

    def test_truncation_trailing_data_and_concatenation_rejected(self):
        for encoding, compress in self.compressors().items():
            raw = compress(b'{"input":"hi"}')
            for broken in (raw[:-1], raw + b'junk', raw + raw):
                with self.subTest(encoding=encoding, size=len(broken)), self.assertRaises(RequestBodyError) as error:
                    decode_body(broken, encoding, 8192)
                self.assertEqual(error.exception.status, 400)

    def test_empty_and_invalid_frames_rejected(self):
        for encoding in self.compressors():
            for raw in (b'', b'private-debug-data'):
                with self.subTest(encoding=encoding), self.assertRaises(RequestBodyError) as error:
                    decode_body(raw, encoding, 8192)
                self.assertEqual(error.exception.status, 400)
                self.assertNotIn('private-debug-data', str(error.exception))

    def test_missing_zstd_dependency_has_actionable_error(self):
        with patch.dict('sys.modules', {'zstandard': None}), self.assertRaises(RequestBodyError) as error:
            decode_body(b'not-used', 'zstd', 8192)
        self.assertEqual(error.exception.status, 503)

    def test_header_normalization_and_duplicate_rejection(self):
        headers = Message()
        self.assertEqual(content_encoding(headers), 'identity')
        headers['Content-Encoding'] = ' GZip '
        self.assertEqual(content_encoding(headers), 'gzip')
        headers['Content-Encoding'] = 'identity'
        with self.assertRaises(RequestBodyError) as error:
            content_encoding(headers)
        self.assertEqual(error.exception.status, 400)

    def test_unsupported_and_stacked_encodings_rejected(self):
        for coding in ('br', 'gzip, zstd', ''):
            headers = Message()
            headers['Content-Encoding'] = coding
            with self.subTest(coding=coding), self.assertRaises(RequestBodyError) as error:
                content_encoding(headers)
            self.assertEqual(error.exception.status, 415)
