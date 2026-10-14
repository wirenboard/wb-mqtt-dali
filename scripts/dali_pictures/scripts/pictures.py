#!/usr/bin/env python3
# DALI device pictures: finding the new devices, checking a downloaded candidate and normalizing.
# The whole scenario and the normalization steps are in README.md. review.py is the review page
# and uses process_file and the registry from here.
# Usage (from dali_pictures):
#   python3 scripts/pictures.py find <wb-mqtt-dali>/products.csv [--batch 25] [--again]
#   python3 scripts/pictures.py check <image-url> <gtin> [--out new/images]
#   python3 scripts/pictures.py normalize <picture>... [--out <dir>]

import argparse
import csv
import io
import json
import math
import os
import urllib.request
from pathlib import Path

import numpy as np  # pylint: disable=import-error
from PIL import Image, ImageOps  # pylint: disable=import-error
from scipy import ndimage  # pylint: disable=import-error

Image.MAX_IMAGE_PIXELS = None

ROOT = Path(__file__).resolve().parent.parent
NEW = ROOT / "new"
# Every GTIN that is not new any more: GTIN (without leading zeros) to {"status", "note"}.
# Statuses: "picture" (normalized and accepted), "not_found" and "discontinued" (searched without
# a result), "no_picture" (no picture in the first base, the reason is not recorded).
REGISTRY = ROOT / "registry.json"

MAX_SIDE = 960
QUALITY = 80
PAD = 0.04
SMALL = 200
EXTREME_RATIO = 8
# A corner counts as white when every channel is at least WHITE_MIN, as light gray when every
# channel is at least LIGHT_MIN and the tint is small.
WHITE_MIN = 240
LIGHT_MIN = 200
LIGHT_TINT = 16
TRIM_THRESHOLD = 12
WORK_SIDE = 2000
# Whitening a light gray background: the background is a smooth surface fitted to the gray pixels
# that touch the border, analysed at WHITEN_SIDE px; the picture is divided by it. A background
# pixel is gray (tint up to LIGHT_TINT), at least WHITEN_MIN and flat (neighbours within
# WHITEN_FLAT); fit outliers beyond WHITEN_RESIDUAL are dropped. The fitted surface must cover
# WHITEN_MIN_SHARE of the picture, otherwise there is no background to whiten.
WHITEN_SIDE = 200
WHITEN_MIN = 180
WHITEN_FLAT = 6
WHITEN_RESIDUAL = 8
WHITEN_MIN_SHARE = 0.2
# Diagonal detection: a long, box-shaped silhouette tilted away from both axes.
FG_MAX = 230
DIAG_MIN_ELONGATION = 2.5
DIAG_MIN_RECTANGULARITY = 0.75
DIAG_MIN_ANGLE = 10
# Auto crop (captions, logos, badges, banners next to the product), analysed at CROP_SIDE px.
CROP_SIDE = 400
# A pixel belongs to some object when it differs from the border color by more than FG.
FG = 12
DILATE = 3
# Pieces at least MAIN_REL of the largest one are the product (several units in one photo).
MAIN_REL = 0.15
# Smaller pieces count only from MIN_SHARE of the total area and when not light (shadows)...
MIN_SHARE = 0.001
SHADOW_LUM = 200
# ...and a shadow is gray: a light but colored piece is a color chart or a badge.
SHADOW_CHROMA = 20
# ...and soft: a light piece with FLAT_SHARE of it within FLAT_TOL of its median is a badge,
# unless lighter than FLAT_MAX_LUM (a white housing is flat too).
FLAT_TOL = 10
FLAT_SHARE = 0.75
FLAT_MAX_LUM = 225
MAX_PASSES = 5
# A caption: lower than a quarter of the frame, several glyphs, a line or a block of text.
TEXT_MIN_GLYPHS = 3
TEXT_LINE_RATIO = 5
TEXT_BLOCK_GLYPHS = 10
# The product itself, after crop and rotation, in source pixels: below this it goes to re-download.
MIN_PRODUCT = 400
# Frame around the whole picture: a line within FRAME_EDGE of each side, FRAME_LINE of it darker
# than white by FRAME_FG (the HEP frame is very light), and under FRAME_INSIDE right inside it.
FRAME_EDGE = 0.12
FRAME_FG = 8
FRAME_LINE = 0.6
FRAME_INSIDE = 0.1

REDOWNLOAD_FLAGS = ("background", "background-light", "small", "small-product")

WHITE = (255, 255, 255)


def normalize_gtin(gtin):
    return str(gtin).lstrip("0")


def oriented_size(img):
    # EXIF orientations 5..8 swap width and height.
    return img.size[::-1] if img.getexif().get(0x0112, 1) >= 5 else img.size


def flatten(img):
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        out = Image.new("RGB", rgba.size, WHITE)
        out.paste(rgba, mask=rgba.getchannel("A"))
        return out
    return img.convert("RGB")


def fit_inside(img, side, enlarge=True):
    scale = side / max(img.size)
    if scale >= 1 and not enlarge:
        return img
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    return img.resize(size, Image.Resampling.LANCZOS)


EXIF_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def load_work(src, side):
    """Upright copy on white, at most `side` px. Shrinks before converting: a 65 Mpx RGBA png
    converted at full size takes over a gigabyte."""
    orientation = src.getexif().get(0x0112, 1)
    src.draft("RGB", (side, side))
    factor = max(src.size) // side
    img = src
    if factor > 1:
        img = (src.convert("RGBA") if src.mode == "P" else src).reduce(factor)
    if orientation in EXIF_TRANSPOSE:
        img = img.transpose(EXIF_TRANSPOSE[orientation])
    return fit_inside(flatten(img), side, enlarge=False)


# The background is judged by the four corners: products often touch the sides of the frame,
# but rarely all four corners.
def corner_colors(img):
    data = np.asarray(img.convert("RGB").resize((100, 100), Image.Resampling.LANCZOS), dtype=np.float64)
    size = 6
    far = 100 - size
    return [
        [int(round(v)) for v in data[y : y + size, x : x + size].mean(axis=(0, 1))]
        for x, y in ((0, 0), (far, 0), (0, far), (far, far))
    ]


def is_white(rgb):
    return all(v >= WHITE_MIN for v in rgb)


def is_light_gray(rgb):
    return all(v >= LIGHT_MIN for v in rgb) and max(rgb) - min(rgb) <= LIGHT_TINT


def whiten(img):
    """Divides `img` by its light gray background, which becomes white; the product gets lighter
    by the same factor. Returns None when no such background is found."""
    coef = fit_background(np.asarray(fit_inside(img, WHITEN_SIDE)).astype(np.float64))
    if coef is None:
        return None
    y = (np.arange(img.height) / img.height)[:, None]
    x = (np.arange(img.width) / img.width)[None, :]
    out = np.asarray(img).astype(np.float32)
    for c in range(3):
        k = coef[:, c]
        surface = k[0] + k[1] * x + k[2] * y + k[3] * x * x + k[4] * x * y + k[5] * y * y
        out[..., c] *= 255 / np.clip(surface, WHITEN_MIN, 255)
    return Image.fromarray(np.clip(out, 0, 255).round().astype(np.uint8))


def background_mask(small):
    """Gray flat pixels connected to the border of `small`."""
    lum = small.mean(axis=2)
    gray = (small.max(axis=2) - small.min(axis=2) <= LIGHT_TINT) & (lum >= WHITEN_MIN)
    flat = (
        np.maximum(
            np.abs(ndimage.grey_dilation(lum, size=3) - lum), np.abs(lum - ndimage.grey_erosion(lum, size=3))
        )
        <= WHITEN_FLAT
    )
    labels, _ = ndimage.label(gray & flat)
    edge = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    return np.isin(labels, edge[edge > 0])


def fit_background(small):
    """Per-channel coefficients of the background surface of `small`, or None."""
    bg = background_mask(small)
    h, w = small.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w]
    ys, xs = ys / h, xs / w

    def terms(y, x):
        return np.stack([np.ones_like(x), x, y, x * x, x * y, y * y], axis=-1)

    for _ in range(3):
        if bg.sum() < WHITEN_MIN_SHARE * bg.size:
            return None
        coef, *_ = np.linalg.lstsq(terms(ys[bg], xs[bg]), small[bg], rcond=None)
        fitted = terms(ys, xs) @ coef
        bg &= np.abs(small - fitted).max(axis=2) <= WHITEN_RESIDUAL
    return coef


def trim_white(img):
    """Crops white margins; returns None when nothing but background is left."""
    mask = (255 - np.asarray(img).astype(np.int16)).max(axis=2) > TRIM_THRESHOLD
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return None
    return img.crop((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1))


# Principal axis of the non-white silhouette. Angle is in degrees, clockwise from the x axis
# (image y grows down), in [-90, 90). The silhouette is filled: a white housing on white shows
# only its edges and print, and would never look like a box.
def silhouette_axis(img):
    data = np.asarray(fit_inside(img, 400)).astype(np.int16)
    fg = (255 - data).max(axis=2) > TRIM_THRESHOLD
    ys, xs = np.nonzero(ndimage.binary_fill_holes(ndimage.binary_dilation(fg, iterations=2)))
    n = len(xs)
    if n < 50:
        return None
    dx, dy = xs - xs.mean(), ys - ys.mean()
    theta = 0.5 * math.atan2(2 * (dx * dy).sum(), (dx * dx).sum() - (dy * dy).sum())
    c, s = math.cos(theta), math.sin(theta)
    u, v = dx * c + dy * s, -dx * s + dy * c
    length, width = u.max() - u.min() + 1, v.max() - v.min() + 1
    return {
        "angle": round(math.degrees(theta), 1),
        "elongation": round(length / width, 2),
        "rectangularity": round(n / (length * width), 2),
    }


def analyze(img):
    """Returns the product box in `img` pixels, the extra pieces outside it and a mask of the
    pieces inside the box that do not touch the product (a "10W" in the corner)."""
    a = np.asarray(img).astype(np.int16)
    fg, labels, n = label_pieces(a)
    if not n:
        return None, [], np.zeros(fg.shape, bool)
    pieces = piece_stats(a, fg, labels, n)
    w, h = img.size
    main_pieces = product_pieces(pieces, w, h)
    box = [min(p["box"][i] for p in main_pieces) for i in (0, 1)] + [
        max(p["box"][i] for p in main_pieces) for i in (2, 3)
    ]

    def overlaps(b):
        return not (b[2] < box[0] or box[2] < b[0] or b[3] < box[1] or box[3] < b[1])

    loose = loose_pieces(pieces, main_pieces)
    extras = [p for p in loose if not overlaps(p["box"])]
    inner = [p["label"] for p in loose if overlaps(p["box"])]
    return box, extras, np.isin(labels, inner)


def label_pieces(a):
    """Foreground mask against the border color, the filled pieces labelled and their count."""
    border = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    fg = np.abs(a - np.median(border, axis=0)).max(axis=2) > FG
    filled = ndimage.binary_fill_holes(ndimage.binary_dilation(fg, iterations=DILATE))
    labels, n = ndimage.label(filled)
    return fg, labels, n


def piece_stats(a, fg, labels, n):
    idx = range(1, n + 1)
    areas = ndimage.sum(labels > 0, labels, idx)
    lums = ndimage.mean(a.mean(axis=2), labels * fg, idx)
    chromas = ndimage.mean(a.max(axis=2) - a.min(axis=2), labels * fg, idx)
    gray = a.mean(axis=2)
    medians = ndimage.median(gray, labels * fg, idx)
    flats = [
        float((np.abs(gray[(labels == i) & fg] - m) < FLAT_TOL).mean()) if (labels == i).any() else 0.0
        for i, m in zip(idx, medians)
    ]
    # Glyphs: pieces before the dilation merged them, counted per piece.
    glyphs, glyph_n = ndimage.label(ndimage.binary_dilation(fg))
    owners = ndimage.maximum(labels, glyphs, range(1, glyph_n + 1)).astype(int)
    counts = np.bincount(owners, minlength=n + 1)
    return [
        {
            "label": label,
            "box": [sl[1].start, sl[0].start, sl[1].stop, sl[0].stop],
            "area": int(area),
            "lum": round(float(lum)),
            "chroma": round(float(chroma)),
            "flat": flat,
            "glyphs": int(count),
        }
        for label, sl, area, lum, chroma, flat, count in zip(
            idx, ndimage.find_objects(labels), areas, lums, chromas, flats, counts[1:]
        )
    ]


def product_pieces(pieces, w, h):
    def is_banner(p):
        return p["box"][2] - p["box"][0] >= w - 2 and p["box"][3] - p["box"][1] < h / 3

    def is_text(p):
        pw, ph = p["box"][2] - p["box"][0], p["box"][3] - p["box"][1]
        return (
            ph < h / 4
            and p["glyphs"] >= TEXT_MIN_GLYPHS
            and (pw >= TEXT_LINE_RATIO * ph or p["glyphs"] >= TEXT_BLOCK_GLYPHS)
        )

    # Banners and captions are never the product, unless nothing else is there. The largest piece
    # stays a candidate: a long thin driver looks like a text line too.
    biggest = max(pieces, key=lambda p: p["area"])
    candidates = [p for p in pieces if p is biggest or not (is_banner(p) or is_text(p))]
    candidates = [p for p in candidates if not is_banner(p)] or candidates
    largest = max(p["area"] for p in candidates)
    return [p for p in candidates if p["area"] >= MAIN_REL * largest]


def loose_pieces(pieces, main_pieces):
    total = sum(p["area"] for p in pieces)
    return [
        p
        for p in pieces
        if p not in main_pieces
        and p["area"] >= MIN_SHARE * total
        and (
            p["lum"] < SHADOW_LUM
            or p["chroma"] > SHADOW_CHROMA
            or (p["flat"] >= FLAT_SHARE and p["lum"] < FLAT_MAX_LUM)
        )
    ]


def strip_frame(img):
    """Crops a thin rectangle drawn around the whole picture (HEP draws one), or returns None.
    A frame line is a row or column near the edge that is mostly non-white, with white right
    inside it; it must be found on at least three sides."""
    fg = (255 - np.asarray(img).astype(np.int16)).max(axis=2) > FRAME_FG
    h, w = fg.shape

    def line(profile, n, inward):
        hit = None
        for i in inward(n):
            if profile[i] >= FRAME_LINE:
                hit = i
            elif hit is not None:
                return hit if profile[i] < FRAME_INSIDE else None
        return None

    rows, cols = fg.mean(axis=1), fg.mean(axis=0)

    def zone(n):
        return int(n * FRAME_EDGE)

    top = line(rows, h, lambda n: range(zone(n)))
    bottom = line(rows, h, lambda n: range(n - 1, n - 1 - zone(n), -1))
    left = line(cols, w, lambda n: range(zone(n)))
    right = line(cols, w, lambda n: range(n - 1, n - 1 - zone(n), -1))
    if sum(x is not None for x in (top, bottom, left, right)) < 3:
        return None
    return img.crop(
        (
            left + 1 if left is not None else 0,
            top + 1 if top is not None else 0,
            right if right is not None else w,
            bottom if bottom is not None else h,
        )
    )


def auto_crop(work):
    """Crop box (fractions of `work`) to the product and a white-out mask in crop coordinates,
    or None when nothing extra is found. Crops, then looks again: a caption can hide behind a
    banner or a badge row."""
    crop = [0.0, 0.0, 1.0, 1.0]
    found = 0
    for _ in range(MAX_PASSES):
        crop, extras = crop_pass(work, crop)
        if not extras:
            break
        found += extras
    w, h = work.size
    cropped = work.crop((crop[0] * w, crop[1] * h, crop[2] * w, crop[3] * h))
    # Pieces left inside the crop but apart from the product are painted white.
    small = fit_inside(cropped, CROP_SIDE)
    _, _, inner = analyze(small)
    if not found:
        # Print on a white housing looks loose too, so only small pieces cut by the edge go,
        # light ones included: the rest of a neighbour cut by a manual frame.
        fg = (255 - np.asarray(small).astype(np.int16)).max(axis=2) > FG
        parts, _ = ndimage.label(ndimage.binary_fill_holes(ndimage.binary_dilation(fg, iterations=DILATE)))
        cut = np.unique(np.concatenate([parts[0], parts[-1], parts[:, 0], parts[:, -1]]))
        cut = [c for c in cut[cut > 0] if (parts == c).sum() < MAIN_REL * fg.sum()]
        inner = np.isin(parts, cut)
        if not inner.any():
            return None
    return {
        "crop": crop,
        "erase": Image.fromarray(inner).convert("L") if inner.any() else None,
        "extras": found,
        "erased": int(ndimage.label(inner)[1]),
    }


def crop_pass(work, crop):
    """`crop` narrowed to the product and the number of extra pieces found, `crop` itself on none."""
    w, h = work.size
    l, t, r, b = crop[0] * w, crop[1] * h, crop[2] * w, crop[3] * h
    small = fit_inside(work.crop((l, t, r, b)), CROP_SIDE)
    box, extras, _ = analyze(small)
    if not extras:
        return crop, 0
    sw, sh = small.size
    crop = [
        round(v, 4)
        for v in (
            (l + box[0] / sw * (r - l)) / w,
            (t + box[1] / sh * (b - t)) / h,
            (l + box[2] / sw * (r - l)) / w,
            (t + box[3] / sh * (b - t)) / h,
        )
    ]
    return crop, len(extras)


def finish(img):
    p = round(max(img.size) * PAD)
    out = fit_inside(ImageOps.expand(img, border=p, fill=WHITE), MAX_SIDE, enlarge=False)
    buf = io.BytesIO()
    out.save(buf, "WEBP", quality=QUALITY)
    return buf.getvalue(), list(out.size)


def apply_edit(img, edit):
    """Crop box in fractions, then an optional white-out mask (a path or an image) over the crop."""
    w, h = img.size
    l, t, r, b = edit["crop"]
    img = img.crop((round(l * w), round(t * h), round(r * w), round(b * h)))
    mask = edit.get("erase")
    if mask:
        mask = Image.open(mask) if isinstance(mask, str) else mask
        img.paste(WHITE, mask=mask.convert("L").resize(img.size, Image.Resampling.NEAREST))
    return img


def process_file(file, mark=None):
    """`mark` is the review mark of this source: "bad" turns the auto rotation off,
    `rotate` turns the rotation of a long product on whatever the detection says, `level` turns
    any product level, `crop` and `erase` are the frame and white-out boxes drawn by hand,
    `keep_background` turns the whitening of a light gray background off."""
    mark = mark or {}
    plain = mark.get("choice") == "bad"
    flags = ["plain"] if plain else []
    work, src_size = load_source(file, flags)
    # Source pixels per work pixel, to judge the product size in the source.
    scale = max(src_size) / max(work.size)
    work, corners, white_corners = whiten_background(work, mark, flags)

    # A frame drawn on the review page crops the source, then the auto crop cleans what is left,
    # on a white background only. The frame and white-out boxes are fractions of the upright source.
    work, crop = manual_crop(work, mark, flags)
    if white_corners >= 3:
        work, crop = crop_white(work, mark, crop, flags)

    # Trim against white only: on a non-white background trimming would eat into the photo.
    trimmed = work
    if white_corners >= 3:
        trimmed = trim_white(work)
        if trimmed is None:
            flags.append("trim-failed")
            trimmed = work

    axis = None
    if white_corners >= 3:
        axis = silhouette_axis(trimmed)
        if axis:
            trimmed = level(trimmed, axis, mark, plain, flags)

    product = product_size(trimmed, scale, flags)
    buffer, out_size = finish(trimmed)
    return {
        "flags": flags,
        "corners": corners,
        "axis": axis,
        "crop": crop,
        "src": list(src_size),
        "product": product,
        "out": out_size,
        "buffer": buffer,
    }


def load_source(file, flags):
    """Work copy of `file` and its upright size."""
    with Image.open(file) as src:
        src_size = oriented_size(src)
        if max(src_size) < SMALL:
            flags.append("small")
        return load_work(src, WORK_SIDE), src_size


def whiten_background(work, mark, flags):
    """Picture with a light gray background whitened, its source corners and the white corner count."""
    corners = corner_colors(work)
    white_corners = sum(map(is_white, corners))
    light = white_corners < 3 <= sum(map(is_light_gray, corners))
    whitened = whiten(work) if light and not mark.get("keep_background") else None
    if whitened:
        work = whitened
        white_corners = sum(map(is_white, corner_colors(work)))
        flags.append("whitened")
    if white_corners < 3:
        flags.append("background-light" if light else "background")
    return work, corners, white_corners


def manual_crop(work, mark, flags):
    crop = None
    if mark.get("crop") or mark.get("erase"):
        w, h = work.size
        for l, t, r, b in mark.get("erase", []):
            work.paste(WHITE, (round(l * w), round(t * h), round(r * w), round(b * h)))
        if mark.get("crop"):
            work = apply_edit(work, {"crop": mark["crop"]})
        flags.append("manual-crop")
        crop = {"crop": mark.get("crop"), "erase": mark.get("erase")}
    return work, crop


def crop_white(work, mark, crop, flags):
    unframed = None if mark.get("crop") else strip_frame(work)
    if unframed:
        work = unframed
        flags.append("frame")
    auto = auto_crop(work)
    if auto:
        work = apply_edit(work, auto)
        flags.append("cropped")
        crop = {"crop": auto["crop"], "extras": auto["extras"], "erased": auto["erased"]}
    return work, crop


def level(trimmed, axis, mark, plain, flags):
    tilt = abs(axis["angle"])
    detected = (
        axis["elongation"] >= DIAG_MIN_ELONGATION
        and axis["rectangularity"] >= DIAG_MIN_RECTANGULARITY
        and DIAG_MIN_ANGLE <= tilt <= 90 - DIAG_MIN_ANGLE
    )
    # Only a long product gains from lying flat: it gets the full-width layout. A compact
    # one would just take more width, so the "довернуть" mark is ignored for it.
    forced = mark.get("rotate") and tilt >= 1 and axis["elongation"] >= DIAG_MIN_ELONGATION
    # "level" turns any product, a box shot at an angle included.
    forced = forced or (mark.get("level") and tilt >= 1)
    if forced or (detected and not plain):
        flags.append("rotated-manual" if forced else "diagonal")
        # PIL rotates counter-clockwise, the angle is clockwise.
        turned = trimmed.rotate(axis["angle"], Image.Resampling.BICUBIC, expand=True, fillcolor=WHITE)
        trimmed = trim_white(turned) or turned

        # A turned picture can show pieces apart from the product that the crop before the turn kept.
        auto = auto_crop(trimmed)
        if auto:
            trimmed = apply_edit(trimmed, auto)
            trimmed = trim_white(trimmed) or trimmed
            flags.append("cropped-after-turn")
    return trimmed


def product_size(trimmed, scale, flags):
    """Size of the product in source pixels."""
    ratio = trimmed.width / trimmed.height
    if ratio > EXTREME_RATIO or ratio < 1 / EXTREME_RATIO:
        flags.append("extreme-ratio")
    product = round(max(trimmed.size) * scale)
    if product < MIN_PRODUCT:
        flags.append("small-product")
    return product


def load_registry():
    return json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}


def save_registry(registry):
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(registry, ensure_ascii=False, indent=1, sort_keys=True))
    os.replace(tmp, REGISTRY)


# find: queues the new devices of wb-mqtt-dali's products.csv for a picture search by agents
# (scripts/AGENT.md). A GTIN is new when it is not in the registry and has no finished search in
# new/results/. --again also queues the ones rejected on the review page, for one more search.
# Searches that ended in not_found or discontinued are moved to the registry on every run.
# Writes new/queue/batch_NNN.json (the old batches are replaced). Item fields: gtin (without
# leading zeros, the picture file name), gtins (all spellings from products.csv), brand, product,
# part (the part number, when the registry has it).
SKIP_STATUSES = ("not_found", "discontinued")


def cmd_find(args):
    known = load_registry()
    results = {}
    for f in (NEW / "results").glob("*.json"):
        r = json.loads(f.read_text())
        results[f.stem] = r
        if r.get("status") in SKIP_STATUSES and f.stem not in known:
            known[f.stem] = {"status": r["status"], "note": r.get("note", "")}
    save_registry(known)

    items = queue_items(args, known, results)
    queue = NEW / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    for f in queue.glob("batch_*.json"):
        f.unlink()
    ordered = sorted(items.values(), key=lambda x: (x["brand"].lower(), x["gtin"]))
    for n, start in enumerate(range(0, len(ordered), args.batch)):
        batch = ordered[start : start + args.batch]
        (queue / f"batch_{n:03d}.json").write_text(json.dumps(batch, ensure_ascii=False, indent=1))
    batches = (len(ordered) + args.batch - 1) // args.batch
    print(
        f"{len(ordered)} new GTINs in {batches} batches"
        + (f", new/queue/batch_000..{batches - 1:03d}.json" if batches else "")
    )


def queue_items(args, known, results):
    """GTIN to queue item, for the products of `args.products` that need a search."""
    decisions_file = NEW / "decisions.json"
    decisions = json.loads(decisions_file.read_text()) if decisions_file.exists() else {}
    items = {}
    with open(args.products, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        gtin = normalize_gtin(r["gtin"])
        if not gtin or gtin in known:
            continue
        again = args.again and decisions.get(gtin, {}).get("status") == "reject"
        if not again and results.get(gtin, {}).get("status", "unsearched") != "unsearched":
            continue
        item = items.setdefault(
            gtin, {"gtin": gtin, "gtins": [], "brand": r["brand_name"], "product": r["product_name"]}
        )
        if r["gtin"] not in item["gtins"]:
            item["gtins"].append(r["gtin"])
        if r["product_part_number"] and "part" not in item:
            item["part"] = r["product_part_number"]
        if again:
            item["again"] = True
            item["rejected_comment"] = decisions[gtin].get("comment", "")
    return items


# check: downloads a candidate picture and checks it against the same rules process_file flags
# on: white or light gray background (3 of 4 corners; light gray passes with
# "light_background": true, normalization whitens it), and rejects placeholders: no product at all, or a flat
# single-tone graphic. There is no size floor: a picture under CHECK_MIN_SIDE passes with
# "small": true and needs the user's confirmation in the review page. Saves to
# <out>/<gtin>.<ext> only when the picture passes; prints one JSON line.
CHECK_MIN_SIDE = 600
# Brightness spread over the product pixels: real photos start around 6, flat placeholders are below 1.
MIN_FG_STD = 3
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def cmd_check(args):
    def report(**r):
        print(json.dumps({"url": args.url, "gtin": args.gtin, **r}, ensure_ascii=False))

    try:
        req = urllib.request.Request(args.url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as res:
            buf = res.read()
        with Image.open(io.BytesIO(buf)) as img:
            fmt = (img.format or "").lower()
            w, h = oriented_size(img)
            flat = load_work(img, WORK_SIDE)
            corners = corner_colors(flat)
        light, reasons = check_reasons(flat, corners)
        if reasons:
            report(ok=False, width=w, height=h, format=fmt, reason="; ".join(reasons))
        else:
            args.out.mkdir(parents=True, exist_ok=True)
            file = args.out / f"{args.gtin}.{'jpg' if fmt == 'jpeg' else fmt}"
            file.write_bytes(buf)
            report(
                ok=True,
                width=w,
                height=h,
                format=fmt,
                small=max(w, h) < CHECK_MIN_SIDE,
                light_background=light,
                file=os.path.relpath(file),
            )
    except Exception as e:  # pylint: disable=broad-exception-caught
        # the agent reads the reason from the JSON line
        report(ok=False, reason=str(e))


def check_reasons(flat, corners):
    """Whether the background is light gray, and the reasons the picture fails, empty when it passes."""
    data = np.asarray(fit_inside(flat, 400, enlarge=False)).astype(np.float64)
    fg = (data < FG_MAX).any(axis=2)
    reasons = []
    light = sum(map(is_white, corners)) < 3 <= sum(map(is_light_gray, corners))
    if sum(map(is_white, corners)) < 3 and not light:
        reasons.append(f"background corners {json.dumps(corners)}")
    if not fg.any():
        reasons.append("empty")
    elif (fg_std := data.mean(axis=2)[fg].std()) < MIN_FG_STD:
        reasons.append(f"placeholder, product brightness spread {fg_std:.1f}")
    return light, reasons


# normalize: every source is processed without marks and written as <out>/<name>.webp.
def cmd_normalize(args):
    args.out.mkdir(parents=True, exist_ok=True)
    for file in args.sources:
        result = process_file(file)
        (args.out / f"{file.stem}.webp").write_bytes(result["buffer"])
        flags = [f for f in result["flags"] if f in REDOWNLOAD_FLAGS]
        print(
            json.dumps(
                {"file": str(file), "flags": result["flags"], "out": result["out"], "redownload": flags}
            )
        )


def gtin_arg(value):
    if not value.isdigit():
        raise argparse.ArgumentTypeError("a GTIN is digits only")
    return value


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(required=True)
    p = commands.add_parser("find", help="queue the new devices for a picture search")
    p.add_argument("products", type=lambda p: Path(p).resolve(), help="products.csv of wb-mqtt-dali")
    p.add_argument("--batch", type=int, default=25)
    p.add_argument("--again", action="store_true")
    p.set_defaults(run=cmd_find)
    p = commands.add_parser("check", help="download and check a candidate picture")
    p.add_argument("url")
    p.add_argument("gtin", type=gtin_arg)
    p.add_argument("--out", type=lambda p: ROOT / p, default=NEW / "images", help="relative to dali_pictures")
    p.set_defaults(run=cmd_check)
    p = commands.add_parser("normalize", help="normalize pictures without review marks")
    p.add_argument("sources", nargs="+", type=Path)
    p.add_argument("--out", type=Path, default=Path("."))
    p.set_defaults(run=cmd_normalize)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
