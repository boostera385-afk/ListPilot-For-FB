"""Fast, thread-safe number recognition for Marketplace listing images."""

from __future__ import annotations

import concurrent.futures
import io
import re
import threading
import time


_OCR_READER = None
_OCR_READER_LOCK = threading.Lock()
_OCR_INFERENCE_LOCK = threading.Lock()
_OCR_IMAGE_SIZE = 640
_OCR_BATCH_SIZE = 4
_OCR_ALLOWLIST = "0123456789-+(). /"


def normalize_target_number(value):
    """Keep only digits so formatted and unformatted numbers compare equally."""
    return re.sub(r"\D", "", str(value or ""))


def text_contains_target_number(target, text_fragments):
    """Match a number within one OCR fragment or a few adjacent fragments."""
    clean_target = normalize_target_number(target)
    if not clean_target:
        return False

    if isinstance(text_fragments, str):
        text_fragments = [text_fragments]

    digit_groups = []
    for fragment in text_fragments or []:
        groups = re.findall(r"\d+", str(fragment or ""))
        digit_groups.extend(group for group in groups if group)

    if any(clean_target in group for group in digit_groups):
        return True

    # OCR frequently splits a phone number into 2-4 neighboring text boxes.
    # Limit joining to four groups to avoid concatenating unrelated prices.
    for start in range(len(digit_groups)):
        combined = ""
        for group in digit_groups[start:start + 4]:
            combined += group
            if clean_target in combined:
                return True
            if len(combined) > len(clean_target) + 4:
                break
    return False


def _check_stop(stop_event):
    if stop_event is not None and stop_event.is_set():
        from core.engine.exceptions import AutomationStopped

        raise AutomationStopped("User manually stopped automation.")


def _get_reader(logger):
    global _OCR_READER
    if _OCR_READER is not None:
        return _OCR_READER

    with _OCR_READER_LOCK:
        if _OCR_READER is not None:
            return _OCR_READER

        started = time.monotonic()
        logger("Loading the OCR model for the first scan...")
        import easyocr
        import torch

        use_gpu = bool(torch.cuda.is_available())
        _OCR_READER = easyocr.Reader(["en"], gpu=use_gpu, verbose=False)
        device = "GPU" if use_gpu else "CPU"
        logger(f"OCR model ready on {device} in {time.monotonic() - started:.1f}s. It will be reused for later scans.")
        return _OCR_READER


def _download_and_prepare(position, url):
    import numpy as np
    import requests
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    response = requests.get(
        url,
        timeout=(5, 12),
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()

    with Image.open(io.BytesIO(response.content)) as opened:
        image = ImageOps.exif_transpose(opened).convert("L")
        image = ImageOps.autocontrast(image)
        image = ImageEnhance.Contrast(image).enhance(1.25)
        image = image.filter(ImageFilter.SHARPEN)
        image.thumbnail((_OCR_IMAGE_SIZE, _OCR_IMAGE_SIZE), Image.Resampling.LANCZOS)

        canvas = Image.new("L", (_OCR_IMAGE_SIZE, _OCR_IMAGE_SIZE), color=255)
        left = (_OCR_IMAGE_SIZE - image.width) // 2
        top = (_OCR_IMAGE_SIZE - image.height) // 2
        canvas.paste(image, (left, top))
        return position, np.asarray(canvas.convert("RGB"))


def find_number_in_images(image_items, target, logger=print, stop_event=None):
    """Return 1-based listing positions whose images contain ``target``.

    ``image_items`` is an iterable of ``(position, image_url)`` pairs. Downloads
    overlap, while OCR runs in thread-safe batches on one shared model.
    """
    clean_target = normalize_target_number(target)
    if not clean_target:
        return []

    items = [(int(position), str(url)) for position, url in image_items if url]
    if not items:
        return []

    prepared = []
    failed_downloads = 0
    download_workers = min(6, len(items))
    with concurrent.futures.ThreadPoolExecutor(max_workers=download_workers) as executor:
        futures = {
            executor.submit(_download_and_prepare, position, url): position
            for position, url in items
        }
        for future in concurrent.futures.as_completed(futures):
            _check_stop(stop_event)
            try:
                prepared.append(future.result())
            except Exception:
                failed_downloads += 1

    if failed_downloads:
        logger(f"Skipped {failed_downloads} listing image(s) that could not be downloaded.")
    if not prepared:
        return []

    prepared.sort(key=lambda item: item[0])
    reader = _get_reader(logger)
    matches = []

    with _OCR_INFERENCE_LOCK:
        for offset in range(0, len(prepared), _OCR_BATCH_SIZE):
            _check_stop(stop_event)
            chunk = prepared[offset:offset + _OCR_BATCH_SIZE]
            images = [image for _, image in chunk]
            try:
                results = reader.readtext_batched(
                    images,
                    batch_size=_OCR_BATCH_SIZE,
                    workers=0,
                    allowlist=_OCR_ALLOWLIST,
                    detail=0,
                    paragraph=False,
                    min_size=8,
                    text_threshold=0.6,
                    low_text=0.3,
                    link_threshold=0.3,
                    canvas_size=1280,
                    mag_ratio=1.0,
                )
            except Exception:
                # One malformed image must not discard the rest of the batch.
                results = []
                for image in images:
                    results.append(
                        reader.readtext(
                            image,
                            allowlist=_OCR_ALLOWLIST,
                            detail=0,
                            paragraph=False,
                            min_size=8,
                            text_threshold=0.6,
                            low_text=0.3,
                            link_threshold=0.3,
                            canvas_size=1280,
                        )
                    )

            for (position, _), fragments in zip(chunk, results):
                if text_contains_target_number(clean_target, fragments):
                    matches.append(position)

    return sorted(set(matches))
