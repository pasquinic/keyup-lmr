"""Reed-Solomon codes over GF(2^6) used by P25 for link control and encryption sync words.

P25 protects the 72-bit Link Control word with RS(24,12,13) and the 96-bit Encryption Sync word with
RS(24,16,9); both are shortened codes over GF(64) (6-bit "hex bits"). DFSI carries the coded words
spread over voice frames 3-8 (LC, LDU1) and 12-17 (ES, LDU2), 3 bytes per frame (24 bits = 4 hex bits).

Field: primitive polynomial x^6 + x + 1. Generator: product of (x - a^i), i = 1 .. n-k (systematic,
data symbols first). Decoding here only strips the parity (the data symbols are sent in the clear);
link errors are left to the equipment's own FEC.
"""
from __future__ import annotations

PRIM = 0x43  # x^6 + x + 1
EXP = [0] * 128
LOG = [0] * 64
_x = 1
for _i in range(63):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x40:
        _x ^= PRIM
for _i in range(63, 128):
    EXP[_i] = EXP[_i - 63]


def gmul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return EXP[LOG[a] + LOG[b]]


def generator(nsym: int) -> list[int]:
    g = [1]
    for i in range(1, nsym + 1):
        root = EXP[i]
        out = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            out[j] ^= c
            out[j + 1] ^= gmul(c, root)
        g = out
    return g


_GEN: dict[int, list[int]] = {}


def rs_encode(data: list[int], nsym: int) -> list[int]:
    """Systematic encode: returns data + nsym parity symbols (each 0..63)."""
    g = _GEN.setdefault(nsym, generator(nsym))
    rem = [0] * nsym
    for d in data:
        fb = d ^ rem[0]
        rem = rem[1:] + [0]
        if fb:
            for j in range(nsym):
                rem[j] ^= gmul(g[j + 1], fb)
    return list(data) + rem


def bytes_to_hexbits(b: bytes, count: int) -> list[int]:
    v = int.from_bytes(b, "big")
    total = len(b) * 8
    return [(v >> (total - 6 * (i + 1))) & 0x3F for i in range(count)]


def hexbits_to_bytes(h: list[int]) -> bytes:
    v = 0
    for s in h:
        v = (v << 6) | (s & 0x3F)
    return v.to_bytes(len(h) * 6 // 8, "big")


def encode_lc(lc: bytes) -> bytes:
    """9-byte link control -> 18 bytes RS(24,12,13) coded (12 data + 12 parity hex bits)."""
    assert len(lc) == 9
    return hexbits_to_bytes(rs_encode(bytes_to_hexbits(lc, 12), 12))


def decode_lc(coded: bytes) -> bytes:
    return hexbits_to_bytes(bytes_to_hexbits(coded, 24)[:12])


def encode_es(es: bytes) -> bytes:
    """12-byte encryption sync (MI 9, ALGID 1, KID 2) -> 18 bytes RS(24,16,9) coded."""
    assert len(es) == 12
    return hexbits_to_bytes(rs_encode(bytes_to_hexbits(es, 16), 8))


def decode_es(coded: bytes) -> bytes:
    return hexbits_to_bytes(bytes_to_hexbits(coded, 24)[:16])


def syndromes_ok(coded: bytes, nsym: int) -> bool:
    """True when a coded word (24 hex bits) has no detectable errors."""
    sym = bytes_to_hexbits(coded, 24)
    for i in range(1, nsym + 1):
        acc = 0
        for s in sym:
            acc = gmul(acc, EXP[i]) ^ s
        if acc:
            return False
    return True
