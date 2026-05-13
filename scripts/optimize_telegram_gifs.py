# -*- coding: utf-8 -*-
"""
Сжимает анимированные GIF из gifs/new для Telegram (меньше трафика, та же логика имён файлов).

Требует: pip install pillow
Рекомендуется сделать копию папки gifs/new перед запуском. Скрипт создаёт *.bak3mb рядом с исходником (один раз).

Дальнейшее сжатие (часто в 3–8 раз меньше при похожей картинке) — через ffmpeg, не «костыль», а нормальный путь:

  ffmpeg -y -i "gifs/new/dialog_3.gif" -vf "fps=14,scale=400:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=80[p];[s1][p]paletteuse=dither=bayer:bayer_scale=3" "gifs/new/dialog_3_small.gif"

Или цикл как MP4 для sendAnimation (часто 200–600 КБ):

  ffmpeg -y -i "gifs/new/dialog_3.gif" -movflags +faststart -pix_fmt yuv420p -vf "scale=400:-2" "gifs/new/dialog_3.mp4"
  # тогда в main.py заменить константу имени файла на .mp4 (Telegram принимает).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageSequence

# Имена как в main.py (GIFS_DIR = gifs/new)
GIF_NAMES = ("privet_1.gif", "dialog_3.gif", "radost_2.gif", "razocharovanie_4.gif")
# Компромисс «вес / картинка» для мобильного Telegram; при необходимости уменьшите MAX_WIDTH ещё на 40–60 px
MAX_WIDTH = 400
PALETTE_COLORS = 80


def _rgba_to_rgb_on_white(img: Image.Image) -> Image.Image:
    bg = Image.new("RGB", img.size, (255, 255, 255))
    bg.paste(img, mask=img.split()[3])
    return bg


def backup_once(path: Path) -> None:
    bak = path.with_name(path.name + ".bak3mb")
    if not bak.exists():
        shutil.copy2(path, bak)
        print(f"backup: {bak.name}")


def optimize_gif(path: Path) -> tuple[int, int]:
    backup_once(path)
    old_size = path.stat().st_size
    im = Image.open(path)
    loop = im.info.get("loop", 0)
    frames_rgba: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(im):
        rgba = frame.convert("RGBA")
        w, h = rgba.size
        if w > MAX_WIDTH:
            nh = max(1, int(h * (MAX_WIDTH / w)))
            rgba = rgba.resize((MAX_WIDTH, nh), Image.Resampling.LANCZOS)
        frames_rgba.append(rgba.copy())
        durations.append(int(frame.info.get("duration", im.info.get("duration", 80)) or 80))
    if not frames_rgba:
        raise RuntimeError("empty gif")
    rgb0 = _rgba_to_rgb_on_white(frames_rgba[0])
    base_q = rgb0.quantize(
        colors=PALETTE_COLORS,
        method=Image.Quantize.MEDIANCUT,
        dither=Image.Dither.FLOYDSTEINBERG,
    )
    out_frames: list[Image.Image] = [base_q]
    for fr in frames_rgba[1:]:
        rgb = _rgba_to_rgb_on_white(fr)
        out_frames.append(rgb.quantize(palette=base_q, dither=Image.Dither.FLOYDSTEINBERG))
    tmp = path.with_suffix(".opt.tmp.gif")
    out_frames[0].save(
        tmp,
        save_all=True,
        append_images=out_frames[1:],
        duration=durations,
        loop=loop,
        optimize=True,
        disposal=2,
    )
    im.close()
    new_size = tmp.stat().st_size
    # Windows: os.replace иногда падает, если исходник открыт в другом процессе — копируем поверх
    try:
        os.replace(tmp, path)
    except OSError:
        shutil.copy2(tmp, path)
        tmp.unlink(missing_ok=True)
    return old_size, new_size


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    gdir = root / "gifs" / "new"
    if not gdir.is_dir():
        print(f"Нет папки: {gdir}", file=sys.stderr)
        return 1
    for name in GIF_NAMES:
        p = gdir / name
        if not p.is_file():
            print(f"skip (нет файла): {p}", file=sys.stderr)
            continue
        old, new = optimize_gif(p)
        pct = (1 - new / old) * 100 if old else 0
        print(f"{name}: {old // 1024} KiB -> {new // 1024} KiB ({pct:.0f}% меньше)")
    print("\nГотово. Проверьте визуально в Telegram. Откат: переименуйте *.bak3mb обратно при необходимости.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
