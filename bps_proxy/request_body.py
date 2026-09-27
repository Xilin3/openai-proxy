"""Decode one HTTP content coding with bounded input and decoded sizes."""

from __future__ import annotations

import io
import zlib


class RequestBodyError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def content_encoding(headers) -> str:
    values = headers.get_all('Content-Encoding') or ['identity']
    if len(values) != 1:
        raise RequestBodyError(400, 'Content-Encoding 不能重复')
    encoding = values[0].strip().lower()
    if encoding not in ('identity', 'gzip', 'deflate', 'zstd'):
        raise RequestBodyError(415, '不支持此请求压缩格式，请使用 zstd、gzip 或 deflate')
    return encoding


def decode_body(raw: bytes, encoding: str, limit: int) -> bytes:
    if len(raw) > limit:
        raise RequestBodyError(413, '请求体超过大小上限')
    if encoding == 'identity':
        return raw
    try:
        if encoding in ('gzip', 'deflate'):
            decoder = zlib.decompressobj(31 if encoding == 'gzip' else 15)
            decoded = decoder.decompress(raw, limit + 1)
            if len(decoded) > limit or decoder.unconsumed_tail:
                raise RequestBodyError(413, '解压后的请求体超过大小上限')
            if not decoder.eof or decoder.unused_data:
                raise RequestBodyError(400, '压缩请求体不完整或含有多余数据')
            return decoded
        if encoding != 'zstd':
            raise RequestBodyError(415, '不支持此请求压缩格式')
        try:
            import zstandard
        except ImportError:
            raise RequestBodyError(503, '缺少 zstd 依赖，请在代理环境中运行 python -m pip install .') from None
        parameters = zstandard.get_frame_parameters(raw)
        if parameters.window_size > limit or (
            parameters.content_size not in (zstandard.CONTENTSIZE_UNKNOWN, zstandard.CONTENTSIZE_ERROR)
            and parameters.content_size > limit
        ):
            raise RequestBodyError(413, '解压后的请求体或解压窗口超过大小上限')
        decoder = zstandard.ZstdDecompressor(max_window_size=limit)
        # Streaming bounds unknown-size frames too. The second, bounded pass
        # verifies EOF and rejects trailing frames/data: stream_reader alone
        # can accept a truncated frame without reporting its missing checksum.
        with decoder.stream_reader(io.BytesIO(raw), read_across_frames=True) as reader:
            decoded = reader.read(limit + 1)
        if len(decoded) > limit:
            raise RequestBodyError(413, '解压后的请求体超过大小上限')
        return decoder.decompress(raw, max_output_size=limit + 1, allow_extra_data=False)
    except RequestBodyError:
        raise
    except Exception as exc:
        # Decoder errors must not echo compressed data or implementation details.
        if isinstance(exc, zlib.error) or type(exc).__name__ == 'ZstdError':
            raise RequestBodyError(400, '压缩请求体无效或不完整') from None
        raise
