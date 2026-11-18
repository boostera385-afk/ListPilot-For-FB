"""Rebuild the checked-in Windows icon using the dashboard's airplane.

Run on Windows: .venv\\Scripts\\python.exe core\\utils\\build_app_icon.py
Pillow is already an application dependency; no icon generation runs at startup.
"""
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def build_icon():
    # Tk falls back to Segoe UI Symbol for this glyph; Pillow needs it explicitly.
    font_path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "seguisym.ttf"
    destination = Path(__file__).resolve().parents[2] / "gui" / "assets" / "listpilot.ico"
    frames = []
    for size in SIZES:
        scale = 4
        edge = size * scale
        image = Image.new("RGBA", (edge, edge), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((0, 0, edge - 1, edge - 1), radius=edge * 10 / 38, fill="#3D5AFE")
        font = ImageFont.truetype(str(font_path), round(edge * 0.64))
        left, top, right, bottom = draw.textbbox((0, 0), "✈", font=font)
        draw.text(((edge - right - left) / 2, (edge - bottom - top) / 2),
                  "✈", font=font, fill="#FFFFFF")
        frames.append(image.resize((size, size), Image.Resampling.LANCZOS))
    destination.parent.mkdir(parents=True, exist_ok=True)
    frames[-1].save(destination, format="ICO", sizes=[(s, s) for s in SIZES],
                    append_images=frames[:-1])
    return destination


if __name__ == "__main__":
    print(build_icon())
