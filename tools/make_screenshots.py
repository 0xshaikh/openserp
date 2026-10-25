#!/usr/bin/env python3
"""Generate REAL terminal screenshots for the openserp README.

Honesty rule: these PNGs are pixel-renderings of genuine CLI transcripts
captured through a real PTY (`script -q -c`). Nothing here is hand-drawn,
mocked, or retouched. The raw ANSI transcript is saved next to each PNG
(`.txt`) so anyone can diff the image against the actual output.

Usage:  python tools/make_screenshots.py
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "assets"
COLS = 92

# (filename, terminal title, shell command run inside the PTY)
SHOTS: list[tuple[str, str, str]] = [
    ("audit-example-com.png", "audit — example.com",
     "openserp audit https://example.com --max-pages 3 --link-probes 10 --no-progress"),
    ("audit-palletsprojects.png", "audit — palletsprojects.com",
     "openserp audit https://palletsprojects.com --max-pages 5 --link-probes 10 --no-progress"),
    ("keywords-seo.png", "keywords — seed 'seo'",
     "openserp keywords seo --count 25 --no-related"),
]

BG = (40, 44, 52)
FG = (220, 221, 225)
DIM = (120, 126, 135)

# Standard xterm 256-colour palette.
PAL: list[tuple[int, int, int]] = [(0, 0, 0), (194, 54, 33), (101, 126, 0), (88, 110, 117),
                                   (43, 95, 148), (130, 0, 153), (29, 90, 155), (113, 118, 125)]
PAL += [(173, 173, 173), (252, 56, 129), (85, 183, 0), (204, 204, 0), (126, 130, 5),
        (41, 107, 170), (163, 0, 239), (6, 238, 238), (224, 224, 224), (140, 140, 140),
        (255, 0, 0), (0, 205, 0), (205, 205, 0), (0, 205, 205), (200, 0, 255),
        (0, 163, 255), (255, 194, 205)]
for r in range(6):
    for g in range(6):
        for b in range(6):
            PAL.append(tuple(0 if c == 0 else 55 + c * 40 for c in (r, g, b)))
for i in range(24):
    v = 8 + i * 10
    PAL.append((v, v, v))


def _col(n: int) -> tuple[int, int, int]:
    return PAL[n] if 0 <= n < 256 else FG


def _brighten(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    return tuple(min(255, int(c * 1.45)) for c in rgb)


def capture(cmd: str) -> str:
    """Run *cmd* in a real PTY so Rich emits genuine ANSI colour."""
    inner = f"stty cols {COLS} rows 300 >/dev/null 2>&1; {cmd}"
    r = subprocess.run(["script", "-q", "-c", inner, "/dev/null"],
                       cwd=str(REPO), capture_output=True, text=True, timeout=300)
    raw = r.stdout + r.stderr
    raw = raw.replace("\x1b]11;rgb:0000/0000/0000\x1b\\", "").rstrip("\x00")
    return raw.rstrip()


SGR = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])")


def _parse(raw_line: str) -> list[tuple[str, tuple[int, int, int], bool]]:
    """Split a transcript line into (char, colour, bold) runs."""
    fg, bold = FG, False
    out: list[tuple[str, tuple[int, int, int], bool]] = []
    for tok in re.split(r"(\x1b\[[0-9;?]*[A-Za-z])", raw_line):
        if not tok:
            continue
        m = SGR.fullmatch(tok)
        if m:
            if m.group(2) != "m":
                continue
            nums = [int(x) for x in m.group(1).split(";") if x.isdigit()]
            i = 0
            while i < len(nums):
                n = nums[i]
                if n == 0:
                    fg, bold = FG, False
                    i += 1
                elif n == 1:
                    bold = True
                    i += 1
                elif n == 22:
                    bold = False
                    i += 1
                elif n == 39:
                    fg = FG
                    i += 1
                elif 30 <= n <= 37:
                    fg, i = _col(n - 30), i + 1
                elif 90 <= n <= 97:
                    fg, i = _col(n - 82), i + 1
                elif n == 38 and i + 2 < len(nums) and nums[i + 1] == 5:
                    fg, i = _col(nums[i + 2]), i + 3
                else:
                    i += 1
        else:
            for ch in tok:
                out.append((ch.replace("\t", " "), fg, bold))
    return out


def render(text: str, out_path: Path, title: str) -> None:
    """Render an ANSI transcript as a macOS-terminal-styled PNG."""
    size = 15
    mono = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", size)
    bold_mono = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", size)
    adv = mono.getlength("M")
    lines = [_parse(l) for l in text.splitlines()]
    pad, head, lh = 18, 34, size + 7
    width = int(adv * COLS) + pad * 2
    height = head + lh * (len(lines) + 1) + 10
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, head], fill=(56, 60, 68))
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([pad + i * 20, 11, pad + i * 20 + 12, 23], fill=c)
    d.text((pad + 66, 9), title, fill=(140, 146, 155), font=mono)
    right = f"cols {COLS}"
    d.text((width - pad - d.textlength(right, font=mono), 9), right,
           fill=(92, 97, 105), font=mono)
    y = head + 8
    for line in lines:
        x = pad
        if not line:
            y += lh
            continue
        for ch, col, b in line:
            f = bold_mono if b else mono
            d.text((x, y), ch, fill=_brighten(col) if b else col, font=f)
            x += adv
        y += lh
    img.save(out_path)


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    for i, (name, title, cmd) in enumerate(SHOTS, 1):
        print(f"[{i}/{len(SHOTS)}] {title}", flush=True)
        png, txt = ASSETS / name, ASSETS / Path(name).with_suffix(".txt")
        out = capture(cmd)
        txt.write_text(out, encoding="utf-8")
        render(out, png, title)
        print(f"    {len(out.splitlines())} lines -> {png.relative_to(REPO)} "
              f"({png.stat().st_size // 1024} KB)", flush=True)


if __name__ == "__main__":
    main()
