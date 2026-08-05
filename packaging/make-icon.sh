#!/usr/bin/env bash
# Build the app icon from the Handled brand mark.
# Canon: references/brand-assets.md in the AIOS repo. Digital Amber #ffbf00 on
# near-black, which is the same pairing the app UI uses.
set -euo pipefail
cd "$(dirname "$0")/.."

MARK="$HOME/Documents/Obsidian Vault/wiki/brand/assets/handled-mark-amber.png"
OUT="packaging/NoBrakes.app/Contents/Resources"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [[ ! -f "$MARK" ]]; then
  echo "brand mark not found at $MARK" >&2
  exit 1
fi

mkdir -p "$OUT" static

./.venv/bin/python3 - "$MARK" "$WORK/icon-1024.png" <<'PY'
import sys
from PIL import Image, ImageDraw

src_path, out_path = sys.argv[1], sys.argv[2]
SIZE = 1024
BG = (11, 11, 12, 255)          # --bg from dialer.css
RADIUS = int(SIZE * 0.2237)      # macOS squircle approximation
INSET = int(SIZE * 0.20)         # keep the mark off the corners

canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

# rounded-rect background
mask = Image.new("L", (SIZE, SIZE), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], RADIUS, fill=255)
bg = Image.new("RGBA", (SIZE, SIZE), BG)
canvas.paste(bg, (0, 0), mask)

# the mark, trimmed to its own ink then centered
mark = Image.open(src_path).convert("RGBA")
bbox = mark.getbbox()
if bbox:
    mark = mark.crop(bbox)
target = SIZE - 2 * INSET
scale = min(target / mark.width, target / mark.height)
mark = mark.resize((max(1, int(mark.width * scale)),
                    max(1, int(mark.height * scale))), Image.LANCZOS)
canvas.alpha_composite(mark, ((SIZE - mark.width) // 2, (SIZE - mark.height) // 2))

canvas.save(out_path)
print(f"built {out_path}")
PY

# PWA icons for the manifest
sips -z 192 192 "$WORK/icon-1024.png" --out static/icon-192.png >/dev/null
sips -z 512 512 "$WORK/icon-1024.png" --out static/icon-512.png >/dev/null

# .icns for the bundle
ICONSET="$WORK/nobrakes.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s        "$WORK/icon-1024.png" --out "$ICONSET/icon_${s}x${s}.png"     >/dev/null
  sips -z $((s*2)) $((s*2)) "$WORK/icon-1024.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$OUT/nobrakes.icns"

echo "icon: $OUT/nobrakes.icns"
echo "pwa:  static/icon-192.png static/icon-512.png"
