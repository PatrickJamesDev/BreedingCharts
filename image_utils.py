"""
image_utils.py — Shared thumbnail generation.

The app keeps full-resolution originals byte-for-byte in the database /
lineage files (nothing is thrown away, nothing is recompressed on ingest).
What this module produces is a SEPARATE small derived copy — a JPEG capped
at THUMB_MAX_DIM on the long edge — used for on-screen card rendering
everywhere (grid view cards, canvas view cards, list icons, previews).

Full-resolution bytes are only ever decoded when a thumbnail is generated
(once per image, cached thereafter) or when the user explicitly needs the
original (viewing/editing the Images tab, export, duplicate detection).
Normal browsing never touches them.
"""
from __future__ import annotations

import base64
import logging
from typing import Optional

from PyQt6.QtCore import QBuffer, QByteArray, Qt
from PyQt6.QtCore import QSize
from PyQt6.QtGui import QImage, QImageReader, QPainter

log = logging.getLogger(__name__)

THUMB_MAX_DIM = 1200  # long edge, px — covers card + preview sizes even at max zoom
THUMB_QUALITY = 85    # JPEG quality


def _flatten_and_scale(img: QImage, max_dim: int) -> QImage:
    if max(img.width(), img.height()) > max_dim:
        img = img.scaled(
            max_dim, max_dim,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
    if img.hasAlphaChannel():
        # JPEG has no alpha channel — composite onto white first so
        # transparent PNGs don't turn black.
        flat = QImage(img.size(), QImage.Format.Format_RGB32)
        flat.fill(Qt.GlobalColor.white)
        p = QPainter(flat)
        p.drawImage(0, 0, img)
        p.end()
        img = flat
    return img


def _encode_jpeg_b64(img: QImage, quality: int) -> Optional[str]:
    if img.isNull():
        return None
    buf = QByteArray()
    qbuf = QBuffer(buf)
    qbuf.open(QBuffer.OpenModeFlag.WriteOnly)
    ok = img.save(qbuf, "JPEG", quality)
    qbuf.close()
    if ok and buf.size() > 0:
        return base64.b64encode(bytes(buf)).decode()
    return None


def _decode_scaled(raw: bytes, max_dim: int) -> Optional[QImage]:
    """Decode `raw`, asking the image plugin to scale while decoding.  For JPEG,
    Qt/libjpeg then decodes at 1/2, 1/4 or 1/8 size, which is several times
    faster and needs far less RAM than decoding the full frame first.  Falls
    back to a plain decode whenever the size cannot be determined."""
    buf = QBuffer(); buf.setData(raw); buf.open(QBuffer.OpenModeFlag.ReadOnly)
    rd = QImageReader(buf)
    sz = rd.size()
    if sz.isValid() and max(sz.width(), sz.height()) > max_dim:
        s = max_dim / float(max(sz.width(), sz.height()))
        rd.setScaledSize(QSize(max(1, round(sz.width() * s)), max(1, round(sz.height() * s))))
        img = rd.read()
        if not img.isNull():
            return img
    img = QImage()
    return img if img.loadFromData(raw) else None


def photo_format(img: QImage):
    """(format, quality) for storing a pasted/dropped photo: JPEG q92 unless the
    image really uses transparency (then lossless PNG).  PNG of a photo is typically
    5-10x larger and ~20x slower to encode."""
    if img.hasAlphaChannel():
        probe = img.scaled(48, 48, Qt.AspectRatioMode.IgnoreAspectRatio,
                           Qt.TransformationMode.FastTransformation)
        for y in range(probe.height()):
            for x in range(probe.width()):
                if (probe.pixel(x, y) >> 24) != 0xFF:
                    return "PNG", -1
    return "JPEG", 92


def make_thumb_from_bytes(raw: bytes, max_dim: int = THUMB_MAX_DIM,
                           quality: int = THUMB_QUALITY) -> Optional[str]:
    """Decode raw image bytes and return a small base64-JPEG thumbnail."""
    try:
        img = _decode_scaled(raw, max_dim)          # F22: JPEG DCT-scaled decode when possible
        if img is None or img.isNull():
            return None
        img = _flatten_and_scale(img, max_dim)
        return _encode_jpeg_b64(img, quality)
    except Exception as e:
        log.warning("make_thumb_from_bytes: %s", e)
        return None


def make_thumb_from_b64(b64_full: str, max_dim: int = THUMB_MAX_DIM,
                         quality: int = THUMB_QUALITY) -> Optional[str]:
    """Same as make_thumb_from_bytes, but takes a base64-encoded original
    (as stored in PlantNode.images) instead of raw bytes."""
    try:
        raw = base64.b64decode(b64_full)
    except Exception as e:
        log.warning("make_thumb_from_b64: bad base64: %s", e)
        return None
    return make_thumb_from_bytes(raw, max_dim, quality)


def make_thumb_from_qimage(qimage: QImage, max_dim: int = THUMB_MAX_DIM,
                            quality: int = THUMB_QUALITY) -> Optional[str]:
    """Same as make_thumb_from_bytes, but takes an already-decoded QImage
    (e.g. straight from the clipboard) instead of encoded bytes."""
    try:
        img = _flatten_and_scale(QImage(qimage), max_dim)
        return _encode_jpeg_b64(img, quality)
    except Exception as e:
        log.warning("make_thumb_from_qimage: %s", e)
        return None
