import base64
import struct
import zlib


def png(width=2, height=2, rgb=(255, 0, 0)):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)
    header = struct.pack('!IIBBBBB', width, height, 8, 2, 0, 0, 0)
    rows = (bytes([0]) + bytes(rgb) * width) * height
    return bytes.fromhex('89504e470d0a1a0a') + chunk(b'IHDR', header) + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b'')


def picture(rgb=(255, 0, 0)):
    return {'type': 'input_image', 'image_url': 'data:image/png;base64,' + base64.b64encode(png(rgb=rgb)).decode()}
