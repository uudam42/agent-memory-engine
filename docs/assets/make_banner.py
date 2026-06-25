"""Generate docs/assets/banner.png using Pillow only (no cairosvg dependency).

Run:  python docs/assets/make_banner.py
"""

from __future__ import annotations
import math
import os
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ── Canvas ────────────────────────────────────────────────────────────────────
W, H = 1600, 420
BG       = (13,  17,  23)      # #0D1117
EDGE_COL = (31, 111, 235, 45)  # #1F6FEB 18% opacity
NODE_COL = (88, 166, 255)      # #58A6FF
NODE_DIM = (88, 166, 255, 120) # dimmer variant
HL_GREEN = (63, 185,  80)      # #3FB950
HL_PUR   = (138, 43, 226)      # #8A2BE2
WHITE    = (255, 255, 255)
GRAY     = (139, 148, 158)     # #8B949E
ACCENT   = (88, 166, 255)      # #58A6FF

# ── Reproducible layout ───────────────────────────────────────────────────────
random.seed(42)

def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Best-effort font loader — falls back to default bitmap font."""
    candidates = [
        "/System/Library/Fonts/SFNSDisplay.ttf",      # macOS SF
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def make_banner(out_path: str = "docs/assets/banner.png") -> None:
    img = Image.new("RGB", (W, H), BG)
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw_ov = ImageDraw.Draw(overlay)
    draw    = ImageDraw.Draw(img)

    # ── 1. Subtle horizontal scanlines ────────────────────────────────────────
    for y in range(0, H, 6):
        draw.line([(0, y), (W, y)], fill=(255, 255, 255, 6), width=1)

    # ── 2. Node graph (right two-thirds) ─────────────────────────────────────
    # Centre of the graph cluster
    CX, CY = 1100, 210
    SPREAD_X, SPREAD_Y = 360, 155

    # Define named nodes
    named = {
        "Memory Tree":     (CX - 10,  CY - 20,  9, NODE_COL),
        "Knowledge Base":  (CX + 170, CY + 80,  7, HL_GREEN),
        "Agent Context":   (CX - 160, CY + 90,  7, HL_PUR),
    }
    # Random peripheral nodes
    peripherals = []
    for _ in range(18):
        nx = CX + int(random.gauss(0, SPREAD_X * 0.5))
        ny = CY + int(random.gauss(0, SPREAD_Y * 0.5))
        nr = random.randint(2, 5)
        peripherals.append((nx, ny, nr, NODE_DIM))

    all_nodes = list(named.values()) + peripherals

    # Draw edges on overlay (RGBA for transparency)
    for i, (ax, ay, _, _) in enumerate(all_nodes):
        for j, (bx, by, _, _) in enumerate(all_nodes):
            if i >= j:
                continue
            dist = math.hypot(bx - ax, by - ay)
            if dist < 220:
                alpha = max(15, int(55 * (1 - dist / 220)))
                draw_ov.line([(ax, ay), (bx, by)],
                             fill=(31, 111, 235, alpha), width=1)

    # Named node halos
    for label, (nx, ny, nr, col) in named.items():
        for ring in (28, 20, 13):
            a = max(8, 40 - ring * 1)
            draw_ov.ellipse([(nx - ring, ny - ring), (nx + ring, ny + ring)],
                            fill=(*col, a))

    # Draw all nodes
    for (nx, ny, nr, col) in all_nodes:
        # col may be a 3-tuple (RGB) or 4-tuple (RGBA)
        fill_col = col[:3] + (200,) if len(col) == 3 else (col[0], col[1], col[2], 200)
        draw_ov.ellipse([(nx - nr, ny - nr), (nx + nr, ny + nr)],
                        fill=fill_col)

    # ── 3. MCP stdio arrow from left ─────────────────────────────────────────
    ax0, ay0 = 50,  CY
    ax1, ay1 = 300, CY
    # Dashed line
    seg = 12
    x = ax0
    while x < ax1 - seg:
        draw_ov.line([(x, ay0), (x + seg, ay0)], fill=(138, 43, 226, 180), width=2)
        x += seg * 2
    # Arrow head
    draw_ov.polygon([(ax1, ay0),
                     (ax1 - 14, ay0 - 7),
                     (ax1 - 14, ay0 + 7)],
                    fill=(138, 43, 226, 220))
    # "MCP stdio" label
    fnt_small = _font(16)
    draw_ov.text((ax0, ay0 - 26), "MCP stdio", font=fnt_small, fill=(138, 43, 226, 220))

    # ── 4. Source-type glyphs (bottom-right cluster) ──────────────────────────
    glyph_labels = ["README", "ADR", "Code", "Tests", "Logs", "Diff"]
    gx0, gy0 = 880, 340
    fnt_glyph = _font(13)
    for i, gl in enumerate(glyph_labels):
        gx = gx0 + i * 90
        draw_ov.rectangle([(gx, gy0), (gx + 68, gy0 + 22)],
                          outline=(88, 166, 255, 90), width=1)
        draw_ov.text((gx + 6, gy0 + 4), gl, font=fnt_glyph,
                     fill=(88, 166, 255, 200))

    # Composite overlay onto image
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    # ── 5. Text ───────────────────────────────────────────────────────────────
    # Title
    fnt_title = _font(62, bold=True)
    title = "AGENT MEMORY ENGINE"
    tx = 58
    draw.text((tx, 80), title, font=fnt_title, fill=WHITE)

    # Thin accent line under title
    draw.rectangle([(tx, 162), (tx + 540, 165)], fill=ACCENT)

    # Subtitle line 1
    fnt_sub1 = _font(21)
    sub1 = "A LOCAL-FIRST MCP RUNTIME FOR PERSISTENT CODING AGENT MEMORY"
    draw.text((tx, 180), sub1, font=fnt_sub1, fill=GRAY)

    # Subtitle line 2
    fnt_sub2 = _font(17)
    sub2 = "Evidence-Backed Memory  ·  Project Knowledge  ·  Agent Context Retrieval"
    draw.text((tx, 220), sub2, font=fnt_sub2, fill=ACCENT)

    # Named node labels
    fnt_node = _font(13)
    for label, (nx, ny, nr, col) in named.items():
        draw.text((nx - 40, ny + nr + 6), label, font=fnt_node, fill=col)

    # ── 6. Save ───────────────────────────────────────────────────────────────
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG", optimize=True)
    size_kb = os.path.getsize(out_path) // 1024
    print(f"Banner written → {out_path}  ({size_kb} KB)")


if __name__ == "__main__":
    make_banner()
