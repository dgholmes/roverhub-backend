"""A PNG writer and a 5x7 bitmap font, numpy only.

The backend deliberately carries neither Pillow nor matplotlib, so the run
figure (runs/figure.py) is rasterised here: an 8-bit grey or RGB array in,
PNG bytes out (filter 0 on every row, one zlib stream).
"""
from __future__ import annotations

import struct
import zlib

import numpy as np

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def encode_png(image: np.ndarray, level: int = 6) -> bytes:
    """`image`: uint8, (H, W) grey or (H, W, 3) RGB."""
    array = np.ascontiguousarray(image, dtype=np.uint8)
    if array.ndim == 2:
        color, channels = 0, 1
    elif array.ndim == 3 and array.shape[2] == 3:
        color, channels = 2, 3
    else:
        raise ValueError(f"cannot encode an image of shape {array.shape}")
    height, width = array.shape[:2]
    rows = array.reshape(height, width * channels)
    raw = np.empty((height, width * channels + 1), dtype=np.uint8)
    raw[:, 0] = 0
    raw[:, 1:] = rows
    header = struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0)
    return (PNG_SIGNATURE + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(raw.tobytes(), level)) + _chunk(b"IEND", b""))


# The classic 5x7 font, ASCII 0x20-0x7E: five column bytes per glyph, bit 0 the
# top row. Characters outside it are drawn as "?".
_FONT_HEX = (
    "0000000000 00005f0000 0007000700 147f147f14 242a7f2a12 2313086462 3649552250 0005030000"
    " 001c224100 0041221c00 082a1c2a08 08083e0808 0050300000 0808080808 0060600000 2010080402"
    " 3e5149453e 00427f4000 4261514946 2141454b31 1814127f10 2745454539 3c4a494930 0171090503"
    " 3649494936 064949291e 0036360000 0056360000 0008142241 1414141414 4122140800 0201510906"
    " 324979413e 7e1111117e 7f49494936 3e41414122 7f4141221c 7f49494941 7f09090101 3e41415132"
    " 7f0808087f 00417f4100 2040413f01 7f08142241 7f40404040 7f0204027f 7f0408107f 3e4141413e"
    " 7f09090906 3e4151215e 7f09192946 4649494931 01017f0101 3f4040403f 1f2040201f 7f2018207f"
    " 6314081463 0304780403 6151494543 00007f4141 0204081020 41417f0000 0402010204 4040404040"
    " 0001020400 2054545478 7f48444438 3844444420 384444487f 3854545418 087e090102 081454543c"
    " 7f08040478 00447d4000 2040443d00 007f102844 00417f4000 7c04180478 7c08040478 3844444438"
    " 7c14141408 081414187c 7c08040408 4854545420 043f444020 3c4040207c 1c2040201c 3c4030403c"
    " 4428102844 0c5050503c 4464544c44 0008364100 00007f0000 0041360800 08082a1c08"
)
_GLYPHS = np.zeros((95, 7, 5), dtype=bool)
for _i, _word in enumerate(_FONT_HEX.split()):
    for _col in range(5):
        _bits = int(_word[_col * 2:_col * 2 + 2], 16)
        for _row in range(7):
            _GLYPHS[_i, _row, _col] = bool(_bits >> _row & 1)

GLYPH_W, GLYPH_H, ADVANCE = 5, 7, 6


def text_mask(text: str, scale: int = 1) -> np.ndarray:
    """A bool mask of `text`, (7*scale, (6*len - 1)*scale)."""
    if not text:
        return np.zeros((GLYPH_H * scale, 0), dtype=bool)
    mask = np.zeros((GLYPH_H, ADVANCE * len(text) - 1), dtype=bool)
    for i, char in enumerate(text):
        code = ord(char)
        glyph = _GLYPHS[code - 32] if 32 <= code <= 126 else _GLYPHS[ord("?") - 32]
        mask[:, i * ADVANCE:i * ADVANCE + GLYPH_W] = glyph
    if scale > 1:
        mask = mask.repeat(scale, axis=0).repeat(scale, axis=1)
    return mask


def text_size(text: str, scale: int = 1) -> tuple:
    """(width, height) in pixels."""
    return (max(0, ADVANCE * len(text) - 1) * scale, GLYPH_H * scale)
