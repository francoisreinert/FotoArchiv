"""Verschlüsselung für private Fotos in der Archiv-Ansicht – nur Standardbibliothek.

AES-256 im CTR-Modus (eigene, kleine AES-Implementierung, nur Verschlüsselungsrichtung – CTR braucht keine andere)
plus HMAC-SHA256 über Nonce + Geheimtext (encrypt-then-MAC). Der Browser entschlüsselt mit WebCrypto
(AES-CTR, HMAC) – siehe static/archview.html, Abgleich-Test gegen node.

Format: nonce(16) | geheimtext | mac(32). Schlüssel: PBKDF2-SHA256(Passwort, Salz, ITER) → 64 Bytes
(32 AES, 32 HMAC).
"""
import hashlib
import hmac
import os
import struct

ITER = 310000


def derive(password, salt):
    k = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITER, 64)
    return k[:32], k[32:]


# ------------------------------------------------------------------ AES-256 ----
_SBOX = [0] * 256
_p = _q = 1
while True:  # S-Box erzeugen (FIPS-197)
    _p = _p ^ ((_p << 1) & 0xFF) ^ (0x1B if _p & 0x80 else 0)
    _q ^= _q << 1
    _q ^= _q << 2
    _q ^= _q << 4
    _q &= 0xFF
    if _q & 0x80:
        _q ^= 0x09
    x = _q ^ (((_q << 1) | (_q >> 7)) & 0xFF) ^ (((_q << 2) | (_q >> 6)) & 0xFF) ^ \
        (((_q << 3) | (_q >> 5)) & 0xFF) ^ (((_q << 4) | (_q >> 4)) & 0xFF) ^ 0x63
    _SBOX[_p] = x
    if _p == 1:
        break
_SBOX[0] = 0x63


def _xt(a):
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else a << 1


# T-Tabellen: eine Runde = 16 Nachschläge
_T0 = [0] * 256
for _i in range(256):
    s = _SBOX[_i]
    _T0[_i] = (_xt(s) << 24) | (s << 16) | (s << 8) | (_xt(s) ^ s)
_T1 = [((t >> 8) | (t << 24)) & 0xFFFFFFFF for t in _T0]
_T2 = [((t >> 16) | (t << 16)) & 0xFFFFFFFF for t in _T0]
_T3 = [((t >> 24) | (t << 8)) & 0xFFFFFFFF for t in _T0]


def _expand(key):
    w = list(struct.unpack(">8I", key))
    rcon = 1
    for i in range(8, 60):
        t = w[i - 1]
        if i % 8 == 0:
            t = ((t << 8) | (t >> 24)) & 0xFFFFFFFF
            t = (_SBOX[t >> 24] << 24) | (_SBOX[(t >> 16) & 255] << 16) | (_SBOX[(t >> 8) & 255] << 8) | _SBOX[t & 255]
            t ^= rcon << 24
            rcon = _xt(rcon)
        elif i % 8 == 4:
            t = (_SBOX[t >> 24] << 24) | (_SBOX[(t >> 16) & 255] << 16) | (_SBOX[(t >> 8) & 255] << 8) | _SBOX[t & 255]
        w.append(w[i - 8] ^ t)
    return w


def _block(w, s0, s1, s2, s3):
    T0, T1, T2, T3, S = _T0, _T1, _T2, _T3, _SBOX
    s0 ^= w[0]
    s1 ^= w[1]
    s2 ^= w[2]
    s3 ^= w[3]
    k = 4
    for _ in range(13):
        t0 = T0[s0 >> 24] ^ T1[(s1 >> 16) & 255] ^ T2[(s2 >> 8) & 255] ^ T3[s3 & 255] ^ w[k]
        t1 = T0[s1 >> 24] ^ T1[(s2 >> 16) & 255] ^ T2[(s3 >> 8) & 255] ^ T3[s0 & 255] ^ w[k + 1]
        t2 = T0[s2 >> 24] ^ T1[(s3 >> 16) & 255] ^ T2[(s0 >> 8) & 255] ^ T3[s1 & 255] ^ w[k + 2]
        t3 = T0[s3 >> 24] ^ T1[(s0 >> 16) & 255] ^ T2[(s1 >> 8) & 255] ^ T3[s2 & 255] ^ w[k + 3]
        s0, s1, s2, s3 = t0, t1, t2, t3
        k += 4
    r0 = ((S[s0 >> 24] << 24) | (S[(s1 >> 16) & 255] << 16) | (S[(s2 >> 8) & 255] << 8) | S[s3 & 255]) ^ w[k]
    r1 = ((S[s1 >> 24] << 24) | (S[(s2 >> 16) & 255] << 16) | (S[(s3 >> 8) & 255] << 8) | S[s0 & 255]) ^ w[k + 1]
    r2 = ((S[s2 >> 24] << 24) | (S[(s3 >> 16) & 255] << 16) | (S[(s0 >> 8) & 255] << 8) | S[s1 & 255]) ^ w[k + 2]
    r3 = ((S[s3 >> 24] << 24) | (S[(s0 >> 16) & 255] << 16) | (S[(s1 >> 8) & 255] << 8) | S[s2 & 255]) ^ w[k + 3]
    return r0, r1, r2, r3


def aes_block(key, block):
    """Ein Block verschlüsseln (für den Prüfwert-Test)."""
    return struct.pack(">4I", *_block(_expand(key), *struct.unpack(">4I", block)))


def ctr(key, nonce, data):
    """AES-256-CTR (128-Bit-Zähler, big endian wie WebCrypto AES-CTR mit length=128)."""
    w = _expand(key)
    ctr_val = int.from_bytes(nonce, "big")
    out = bytearray(len(data))
    n = len(data)
    pack = struct.pack
    for off in range(0, n, 16):
        c = (ctr_val + off // 16) & ((1 << 128) - 1)
        ks = pack(">4I", *_block(w, c >> 96, (c >> 64) & 0xFFFFFFFF, (c >> 32) & 0xFFFFFFFF, c & 0xFFFFFFFF))
        chunk = data[off:off + 16]
        out[off:off + len(chunk)] = (int.from_bytes(chunk, "big") ^ int.from_bytes(ks[:len(chunk)], "big")).to_bytes(len(chunk), "big")
    return bytes(out)


def seal(keys, data):
    """nonce | AES-CTR(data) | HMAC-SHA256(nonce | geheimtext)"""
    ek, mk = keys
    nonce = os.urandom(16)
    ct = ctr(ek, nonce, data)
    return nonce + ct + hmac.new(mk, nonce + ct, hashlib.sha256).digest()


def open_(keys, blob):
    ek, mk = keys
    nonce, ct, tag = blob[:16], blob[16:-32], blob[-32:]
    if not hmac.compare_digest(hmac.new(mk, nonce + ct, hashlib.sha256).digest(), tag):
        raise ValueError("falscher Schlüssel oder beschädigt")
    return ctr(ek, nonce, ct)
