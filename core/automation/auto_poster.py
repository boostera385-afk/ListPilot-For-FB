import math
import shutil
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import urllib.parse
import os
import time
import uuid
from core.engine.exceptions import AutomationStopped, RetryableError, PermanentError
from core.engine.state_machine import AutomationState
import random
import re
import requests
from playwright.sync_api import sync_playwright



def _largest_rotated_rect(w, h, angle):
    """Largest axis-aligned rectangle that fits inside a w x h image rotated by angle (radians)."""
    if w <= 0 or h <= 0:
        return 0, 0
    width_is_longer = w >= h
    side_long, side_short = (w, h) if width_is_longer else (h, w)
    sin_a, cos_a = abs(math.sin(angle)), abs(math.cos(angle))
    if sin_a < 1e-10:
        return w, h
    if side_short <= 2.0 * sin_a * cos_a * side_long or abs(sin_a - cos_a) < 1e-10:
        x = 0.5 * side_short
        wr, hr = (x / sin_a, x / cos_a) if width_is_longer else (x / cos_a, x / sin_a)
    else:
        cos_2a = cos_a * cos_a - sin_a * sin_a
        wr, hr = (w * cos_a - h * sin_a) / cos_2a, (h * cos_a - w * sin_a) / cos_2a
    return int(wr), int(hr)


def _filter_color_tint(img):
    """Warm/cool cast by nudging each RGB channel independently."""
    multipliers = [random.uniform(0.90, 1.10) for _ in range(3)]
    channels = [
        band.point([max(0, min(255, int(v * m))) for v in range(256)])
        for band, m in zip(img.split(), multipliers)
    ]
    return Image.merge("RGB", channels)


def _filter_gamma(img):
    gamma = random.uniform(0.80, 1.20)
    lut = [max(0, min(255, int(255 * ((v / 255) ** gamma)))) for v in range(256)]
    return img.point(lut * 3)


def _filter_hue_shift(img):
    shift = random.choice([-1, 1]) * random.randint(4, 10)
    h, s, v = img.convert("HSV").split()
    h = h.point([(value + shift) % 256 for value in range(256)])
    return Image.merge("HSV", (h, s, v)).convert("RGB")


def _filter_vignette(img):
    strength = random.uniform(0.15, 0.35)
    mask = Image.radial_gradient("L").resize(img.size, Image.Resampling.BILINEAR)
    mask = mask.point([int(v * strength) for v in range(256)])
    darker = ImageEnhance.Brightness(img).enhance(0.55)
    return Image.composite(darker, img, mask)


def _filter_crisp(img):
    """Light sharpening; changes every edge without softening the photo."""
    return img.filter(
        ImageFilter.UnsharpMask(radius=1.2, percent=random.randint(40, 90), threshold=3)
    )


def _filter_fade(img):
    """Lifted blacks / softened whites, like a mild photo-app preset."""
    low = random.randint(6, 22)
    high = random.randint(233, 250)
    lut = [int(low + (high - low) * v / 255) for v in range(256)]
    return img.point(lut * 3)


def _filter_autocontrast(img):
    return ImageOps.autocontrast(img, cutoff=random.uniform(0.5, 2.0))


_IMAGE_FILTERS = (
    ("tint", _filter_color_tint),
    ("gamma", _filter_gamma),
    ("hue", _filter_hue_shift),
    ("vignette", _filter_vignette),
    ("crisp", _filter_crisp),
    ("fade", _filter_fade),
    ("autocontrast", _filter_autocontrast),
)


def randomize_image(image_path, temp_dir, log):
    """Create a unique, normalized, heavily edited upload copy of an image to defeat hash detection.

    Every copy gets a little tilt and crop, a slight perspective change and a
    resize, a colour/sharpness jitter, 3-5 random photo filters, light pixel
    noise and a random high JPEG quality (nothing that blurs the photo). Images are never mirrored so any text in
    the picture stays readable.
    """
    new_path = None
    try:
        with Image.open(image_path) as source:
            img = ImageOps.exif_transpose(source).convert("RGB")

        applied = []
        width, height = img.size
        if width > 50 and height > 50:
            # 1. Little tilt (0.4-1.5 degrees either way), then trim to the
            # largest clean rectangle so no black corners are left behind.
            angle = random.choice((-1, 1)) * random.uniform(0.4, 1.5)
            img = img.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False)
            clean_w, clean_h = _largest_rotated_rect(width, height, math.radians(angle))
            clean_w, clean_h = max(1, min(width, clean_w)), max(1, min(height, clean_h))
            left = (width - clean_w) // 2
            top = (height - clean_h) // 2
            img = img.crop((left, top, left + clean_w, top + clean_h))
            applied.append(f"tilt {angle:+.1f}deg")

            # 2. Little crop: 3%-6% in total per axis, split fairly evenly
            # between both sides so edge text (titles, phone numbers) stays in.
            w, h = img.size
            crop_w = int(w * random.uniform(0.03, 0.06))
            crop_h = int(h * random.uniform(0.03, 0.06))
            left = int(crop_w * random.uniform(0.3, 0.7))
            top = int(crop_h * random.uniform(0.3, 0.7))
            img = img.crop((left, top, w - (crop_w - left), h - (crop_h - top)))
            applied.append("crop")

            # 3. Slight perspective skew: each corner moves inward 0-1.5%.
            w, h = img.size
            def jitter(size):
                return random.randint(0, max(1, int(size * 0.015)))
            quad = (
                jitter(w), jitter(h),
                jitter(w), h - jitter(h),
                w - jitter(w), h - jitter(h),
                w - jitter(w), jitter(h),
            )
            img = img.transform((w, h), Image.Transform.QUAD, quad, resample=Image.Resampling.BICUBIC)
            applied.append("perspective")

            # 4. Dynamic resize to 88%-98% of the cropped size.
            scale = random.uniform(0.88, 0.98)
            img = img.resize(
                (max(1, int(img.size[0] * scale)), max(1, int(img.size[1] * scale))),
                Image.Resampling.LANCZOS,
            )

            # 5. Colour/brightness/sharpness jitter (about double the old range).
            img = ImageEnhance.Brightness(img).enhance(random.uniform(0.80, 1.20))
            img = ImageEnhance.Contrast(img).enhance(random.uniform(0.80, 1.20))
            img = ImageEnhance.Color(img).enhance(random.uniform(0.80, 1.25))
            # Sharpness never drops below normal, so photos are not blurred.
            img = ImageEnhance.Sharpness(img).enhance(random.uniform(1.00, 1.35))

            # 6. 3-5 random photo filters in random order.
            for name, photo_filter in random.sample(_IMAGE_FILTERS, random.randint(3, 5)):
                img = photo_filter(img)
                applied.append(name)

            # Tilt, perspective and resize all resample the image; a gentle
            # sharpen restores the detail they soften.
            img = img.filter(ImageFilter.UnsharpMask(radius=1, percent=50, threshold=3))

            # 7. Light pixel noise (a few hundred faint pixel changes to break exact matches).
            pixels = img.load()
            for _ in range(random.randint(400, 900)):
                x = random.randint(0, img.size[0] - 1)
                y = random.randint(0, img.size[1] - 1)
                r, g, b = pixels[x, y]
                pixels[x, y] = (
                    max(0, min(255, r + random.randint(-6, 6))),
                    max(0, min(255, g + random.randint(-6, 6))),
                    max(0, min(255, b + random.randint(-6, 6))),
                )

        base_name = os.path.basename(image_path)
        name, _ = os.path.splitext(base_name)
        # 8. Save in a different format from the source (JPG/PNG/WEBP, all
        # accepted by Facebook), with randomized quality settings.
        source_ext = os.path.splitext(image_path)[1].lower().replace("jpeg", "jpg")
        formats = [fmt for fmt in (".jpg", ".png", ".webp") if fmt != source_ext] or [".jpg"]
        ext = random.choice(formats)
        new_name = f"{name}_mod_{uuid.uuid4().hex[:12]}{ext}"
        new_path = os.path.join(temp_dir, new_name)
        if ext == ".png":
            img.save(new_path, "PNG", compress_level=random.randint(3, 9))
        elif ext == ".webp":
            img.save(new_path, "WEBP", quality=random.randint(88, 95), method=random.randint(3, 6))
        else:
            img.save(
                new_path,
                "JPEG",
                quality=random.randint(88, 95),
                subsampling=random.choice((0, 1)),
            )
        applied.append(ext.lstrip(".").upper())
        if not os.path.isfile(new_path) or os.path.getsize(new_path) == 0:
            raise OSError("edited image was not written")
        log(f"Prepared fresh upload image: {os.path.basename(new_path)} ({', '.join(applied) or 're-encoded'})")
        return new_path
    except Exception as e:
        if new_path and os.path.exists(new_path):
            try:
                os.remove(new_path)
            except OSError:
                pass
        log(f"Failed to prepare a fresh upload copy for {image_path}: {e}")
        return None

def parse_and_download_images(cell_text, log):
    if not cell_text:
        return []

    local_paths = []
    temp_dir = os.path.join(os.getcwd(), "temp_images")
    os.makedirs(temp_dir, exist_ok=True)

    # 1. Process Google Drive IDs
    gdrive_pattern = r"drive\.google\.com/(?:file/d/|open\?id=)([a-zA-Z0-9_-]+)"
    gdrive_ids = list(dict.fromkeys(re.findall(gdrive_pattern, cell_text)))
    for file_id in gdrive_ids:
        download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        log(f"Downloading Google Drive image: {file_id}")
        try:
            response = requests.get(download_url, allow_redirects=True, timeout=30)
            if response.status_code == 200:
                local_path = os.path.join(temp_dir, f"img_{file_id}.jpg")
                with open(local_path, "wb") as f:
                    f.write(response.content)
                local_paths.append(local_path)
            else:
                log(f"Failed to download image {file_id}: HTTP {response.status_code}")
        except Exception as e:
            log(f"Error downloading {file_id}: {str(e)}")

    # 2. Process direct HTTP URLs (that aren't Google Drive)
    url_pattern = r"(https?://[^\s\,\]\)]+)"
    all_urls = re.findall(url_pattern, cell_text)
    for url in all_urls:
        if "drive.google.com" not in url:
            log(f"Downloading direct image: {url}")
            try:
                response = requests.get(url, allow_redirects=True, timeout=30)
                if response.status_code == 200:
                    safe_name = "".join(
                        c for c in url.split("/")[-1] if c.isalnum() or c in "._-"
                    )
                    if not safe_name:
                        safe_name = f"img_{int(time.time())}.jpg"
                    local_path = os.path.join(temp_dir, safe_name)
                    with open(local_path, "wb") as f:
                        f.write(response.content)
                    local_paths.append(local_path)
            except Exception as e:
                log(f"Error downloading direct image {url}: {e}")

    # 3. Process local file names / paths
    possible_files = [f.strip() for f in str(cell_text).split(",")]
    for file_str in possible_files:
        if not file_str or file_str.startswith("http"):
            continue

        # Check standard path
        file_str = file_str.strip('"\'')
        if os.path.isfile(file_str):
            local_paths.append(os.path.abspath(file_str))
            log(f"Found local image: {file_str}")
        else:
            # Check 'pictures' or 'images' subfolders
            in_pics = os.path.join(os.getcwd(), "pictures", file_str)
            in_imgs = os.path.join(os.getcwd(), "images", file_str)
            if os.path.isfile(in_pics):
                local_paths.append(os.path.abspath(in_pics))
                log(f"Found local image in pictures folder: {file_str}")
            elif os.path.isfile(in_imgs):
                local_paths.append(os.path.abspath(in_imgs))
                log(f"Found local image in images folder: {file_str}")
            else:
                if "." in file_str:
                    log(
                        f"Warning: Could not find local image '{file_str}'. Please ensure it is in the same folder as the app."
                    )

    # Every upload must use a newly edited copy; never fall back to the source.
    final_paths = []
    for path in dict.fromkeys(local_paths):
        mod_path = randomize_image(path, temp_dir, log)
        if mod_path:
            final_paths.append(mod_path)
        else:
            log(f"Skipping image because it could not be edited safely: {path}")
        
    return final_paths
