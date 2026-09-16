"""Auto-reframe crop-box math. Kept in its own module and free of any I/O (no ffmpeg calls,
no network) specifically so it can be unit-tested with a synthetic image, the same way
`learning.mjs`'s pure functions are tested with `node --test` — the goal is to catch a bad
crop calculation with a fast, deterministic test rather than by eyeballing exported video.

Scope, stated plainly: this computes ONE crop rectangle from ONE representative frame's
subject mask, then applies that same fixed rectangle to the whole clip. That's a legitimate,
honest v1 of "auto-reframe" — it is NOT frame-by-frame subject tracking (what CapCut's
auto-reframe actually does for a moving subject). A subject that moves far from its starting
position over the course of the clip will drift out of a fixed crop. Say so if you want real
per-frame tracking added later — it's a materially bigger feature, not a tweak to this one.
"""
from PIL import Image


def bbox_from_alpha(mask_image: Image.Image, alpha_threshold: int = 10) -> tuple[int, int, int, int] | None:
    """Returns (left, top, right, bottom) of the foreground subject from an RGBA image's
    alpha channel (as produced by rembg-style background removal), or None if the image has
    no alpha channel or the mask is empty (nothing above the threshold anywhere)."""
    if mask_image.mode != 'RGBA':
        return None
    alpha = mask_image.split()[-1]
    bbox = alpha.point(lambda p: 255 if p > alpha_threshold else 0).getbbox()
    return bbox


def compute_crop_box(
    frame_size: tuple[int, int],
    subject_bbox: tuple[int, int, int, int] | None,
    target_aspect_w: int,
    target_aspect_h: int,
) -> tuple[int, int, int, int]:
    """Computes the largest crop rectangle of the target aspect ratio that fits inside
    frame_size and is centered on subject_bbox's center (or the frame's own center if no
    subject was detected — a sane fallback, not a silent failure)."""
    frame_w, frame_h = frame_size
    if frame_w <= 0 or frame_h <= 0:
        raise ValueError('frame_size must be positive')
    if target_aspect_w <= 0 or target_aspect_h <= 0:
        raise ValueError('target aspect ratio must be positive')

    if subject_bbox:
        left, top, right, bottom = subject_bbox
        center_x, center_y = (left + right) / 2, (top + bottom) / 2
    else:
        center_x, center_y = frame_w / 2, frame_h / 2

    target_ratio = target_aspect_w / target_aspect_h
    frame_ratio = frame_w / frame_h

    if target_ratio > frame_ratio:
        # Target is relatively wider than the source frame -> width-constrained.
        crop_w = frame_w
        crop_h = round(crop_w / target_ratio)
    else:
        crop_h = frame_h
        crop_w = round(crop_h * target_ratio)

    # Center on the subject, then clamp so the box never runs off the frame edges.
    x = round(center_x - crop_w / 2)
    y = round(center_y - crop_h / 2)
    x = max(0, min(x, frame_w - crop_w))
    y = max(0, min(y, frame_h - crop_h))

    return (x, y, crop_w, crop_h)
