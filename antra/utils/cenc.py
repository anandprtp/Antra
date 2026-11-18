"""AES-CTR ('cenc' scheme) decryption of fragmented-MP4 samples, in memory.

Shared by the Amazon and Apple adapters: both carried a private copy of this parser,
which is how the same three defects lived in both:

* box headers are untrusted, and a truncated stream raised raw ``struct.error`` out of
  a routine documented to return an error string;
* ``size == 0`` ("box runs to end of file") was treated as malformed, so a trailing
  fragment was skipped and left ENCRYPTED while the call reported success;
* a ``trun`` without per-sample sizes (it relies on ``tfhd``'s default) parsed as all
  zeros, so every sample was skipped silently.
"""
from __future__ import annotations

import struct
from typing import Optional


def _read_box(d, pos):
    """(size, type, header_len) for the box at ``pos``, or None if unreadable."""
    if pos + 8 > len(d):
        return None
    sz = struct.unpack_from(">I", d, pos)[0]
    bt = bytes(d[pos + 4:pos + 8]).decode("latin-1", errors="replace")
    if sz == 1:
        if pos + 16 > len(d):
            return None
        return struct.unpack_from(">Q", d, pos + 8)[0], bt, 16
    if sz == 0:
        sz = len(d) - pos          # ISO BMFF: "runs to the end of the file"
    if sz < 8:
        return None
    return sz, bt, 8


def _find_first(d, name):
    pos = 0
    while pos < len(d):
        r = _read_box(d, pos)
        if r is None:
            break
        sz, bt, hs = r
        if bt == name:
            return d[pos + hs:pos + sz]
        pos += sz
    return None


def _parse_senc(d):
    """Return [(iv_bytes, subsamples_or_None), ...]."""
    if len(d) < 8:
        return []
    flags = struct.unpack_from(">I", d, 0)[0] & 0xFFFFFF
    iv_size = 8  # AES-CTR default; override if flag bit 0 set
    off = 4
    if flags & 1:
        # AlgorithmID (3 bytes) + IV_Size (1 byte) + KID (16 bytes)
        if off + 20 > len(d):
            return []
        iv_size = d[off + 3]
        off += 20
    cnt = struct.unpack_from(">I", d, off)[0]
    off += 4
    result = []
    for _ in range(cnt):
        if off + iv_size > len(d):
            break
        iv = bytes(d[off:off + iv_size])
        off += iv_size
        subs = None
        if flags & 2:
            if off + 2 > len(d):
                break
            sc = struct.unpack_from(">H", d, off)[0]
            off += 2
            subs = []
            for _ in range(sc):
                if off + 6 > len(d):
                    break
                subs.append((struct.unpack_from(">H", d, off)[0],
                             struct.unpack_from(">I", d, off + 2)[0]))
                off += 6
        result.append((iv, subs))
    return result


def _parse_trun(d):
    """Return (data_offset_or_None, [sample_sizes], has_per_sample_sizes)."""
    if len(d) < 8:
        return None, [], False
    flags = struct.unpack_from(">I", d, 0)[0] & 0xFFFFFF
    cnt = struct.unpack_from(">I", d, 4)[0]
    off = 8
    doff = None
    if flags & 0x001:
        doff = struct.unpack_from(">i", d, off)[0]
        off += 4
    if flags & 0x004:
        off += 4
    sizes = []
    for _ in range(cnt):
        sz = 0
        if flags & 0x100:
            off += 4
        if flags & 0x200:
            sz = struct.unpack_from(">I", d, off)[0]
            off += 4
        if flags & 0x400:
            off += 4
        if flags & 0x800:
            off += 4
        sizes.append(sz)
    return doff, sizes, bool(flags & 0x200)


def _parse_tfhd_default_size(d) -> Optional[int]:
    """default_sample_size from a tfhd box body, or None when absent."""
    if len(d) < 8:
        return None
    flags = struct.unpack_from(">I", d, 0)[0] & 0xFFFFFF
    off = 8                                  # version/flags + track_ID
    if flags & 0x01:
        off += 8                             # base_data_offset
    if flags & 0x02:
        off += 4                             # sample_description_index
    if flags & 0x08:
        off += 4                             # default_sample_duration
    if flags & 0x10:
        return struct.unpack_from(">I", d, off)[0]
    return None


def _decrypt(raw: bytearray, key: bytes, AES) -> Optional[str]:
    pos, n, changed = 0, len(raw), 0
    while pos < n:
        r = _read_box(raw, pos)
        if r is None:
            break
        moof_sz, bt, moof_hs = r
        if bt != "moof":
            pos += moof_sz
            continue

        moof_start = pos
        moof_end = pos + moof_sz
        traf = _find_first(raw[pos + moof_hs:moof_end], "traf")
        if traf is None:
            pos = moof_end
            continue

        senc_raw = _find_first(traf, "senc")
        trun_raw = _find_first(traf, "trun")
        if senc_raw is None or trun_raw is None:
            pos = moof_end
            continue

        samples = _parse_senc(senc_raw)
        doff, sizes, sizes_known = _parse_trun(trun_raw)
        if not sizes_known:
            default_size = _parse_tfhd_default_size(_find_first(traf, "tfhd") or b"")
            if default_size:
                sizes = [default_size] * len(samples)
                sizes_known = True

        # mdat immediately follows moof
        mr = _read_box(raw, moof_end)
        if mr is None or mr[1] != "mdat":
            pos = moof_end
            continue
        mdat_sz, _, mdat_hs = mr

        # data_offset in trun is relative to the start of moof
        sample_pos = (moof_start + doff) if doff is not None else (moof_end + mdat_hs)

        for idx, (iv, subs) in enumerate(samples):
            s_sz = sizes[idx] if idx < len(sizes) else 0
            if s_sz == 0 and subs:
                s_sz = sum(c + e for c, e in subs)   # the subsample map gives the size too
            if s_sz == 0:
                if not subs and not sizes_known:
                    # Skipping silently would yield a partly still-encrypted file.
                    return "CENC sample sizes could not be determined"
                continue
            # Pad IV to 16 bytes (AES-CTR counter initial value)
            cipher = AES.new(key, AES.MODE_CTR, initial_value=iv.ljust(16, b"\x00"), nonce=b"")
            if subs:
                cur = sample_pos
                for clear, enc in subs:
                    cur += clear
                    if enc > 0:
                        raw[cur:cur + enc] = cipher.decrypt(bytes(raw[cur:cur + enc]))
                    cur += enc
            else:
                raw[sample_pos:sample_pos + s_sz] = cipher.decrypt(
                    bytes(raw[sample_pos:sample_pos + s_sz])
                )
            sample_pos += s_sz
            changed += 1

        pos = moof_end + mdat_sz

    if not changed:
        return "No CENC samples found — file may not be fragmented MP4 or is not CENC-encrypted"
    return None


def decrypt_cenc_ctr_inplace(raw: bytearray, key: bytes) -> Optional[str]:
    """Decrypt every 'cenc' (AES-CTR) sample in ``raw`` in place.

    Returns None on success, or an error string. Never raises on malformed input.
    """
    try:
        from Cryptodome.Cipher import AES
    except ImportError:
        return "Cryptodome not available — run: pip install pycryptodomex"
    try:
        return _decrypt(raw, key, AES)
    except (struct.error, IndexError, ValueError, TypeError, OverflowError, MemoryError) as e:
        return f"Malformed CENC stream: {type(e).__name__}: {e}"
