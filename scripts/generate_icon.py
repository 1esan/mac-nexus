#!/opt/anaconda3/bin/python3
"""Generate the custom Neon Retina .icns icon for Mac Nexus."""

from __future__ import annotations

import argparse
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageChops, ImageDraw, ImageFilter

DEFAULT_ICNS_PATH = Path("/Users/egorlabutin/Desktop/Mac Nexus.app/Contents/Resources/AppIcon.icns")

# Render at 2048x2048 and downsample to 1024x1024 for crisp anti-aliasing.
CANVAS_SIZE = 2048
FINAL_SIZE = 1024
CENTER = CANVAS_SIZE // 2

BG_OBSIDIAN = (7, 11, 20, 255)
CYAN = (0, 240, 255)
CYAN_BRIGHT = (180, 252, 255)
VIOLET = (139, 92, 246)
VIOLET_BRIGHT = (214, 194, 255)
EMERALD = (16, 185, 129)

ICONSET_SPECS: list[tuple[str, int]] = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]


def superellipse_points(
    cx: float,
    cy: float,
    radius: float,
    exponent: float = 4.8,
    steps: int = 720,
) -> list[tuple[float, float]]:
    """Return polygon vertices for a continuous-curvature macOS squircle."""
    power = 2.0 / exponent
    points: list[tuple[float, float]] = []
    for i in range(steps):
        theta = (2.0 * math.pi * i) / steps
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)
        x = cx + radius * math.copysign(abs(cos_t) ** power, cos_t)
        y = cy + radius * math.copysign(abs(sin_t) ** power, sin_t)
        points.append((x, y))
    return points


def make_squircle_mask(size: int, radius: float, exponent: float = 4.8) -> Image.Image:
    """Create a smooth 2x-supersampled alpha mask for the macOS squircle."""
    scale = 2
    hi_size = size * scale
    mask_hi = Image.new("L", (hi_size, hi_size), 0)
    draw = ImageDraw.Draw(mask_hi)
    pts = superellipse_points(hi_size / 2.0, hi_size / 2.0, radius * scale, exponent=exponent)
    draw.polygon(pts, fill=255)
    return mask_hi.resize((size, size), Image.Resampling.LANCZOS)


def polar(cx: float, cy: float, radius: float, deg: float) -> tuple[float, float]:
    """Convert polar coordinates (degrees, 0 at right, clockwise) to XY."""
    rad = math.radians(deg)
    return (cx + radius * math.cos(rad), cy + radius * math.sin(rad))


def draw_glow_arcs(
    canvas: Image.Image,
    specs: Iterable[tuple[float, float, float, int, tuple[int, int, int, int]]],
    blur_wide: float = 28.0,
    blur_tight: float = 9.0,
) -> None:
    """Render glowing arcs using wide + tight bloom passes plus crisp foreground strokes."""
    wide_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    tight_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sharp_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))

    draw_wide = ImageDraw.Draw(wide_layer)
    draw_tight = ImageDraw.Draw(tight_layer)
    draw_sharp = ImageDraw.Draw(sharp_layer)

    for radius, start_deg, end_deg, width, color in specs:
        bbox = [
            CENTER - radius,
            CENTER - radius,
            CENTER + radius,
            CENTER + radius,
        ]
        r, g, b, a = color
        draw_wide.arc(bbox, start=start_deg, end=end_deg, fill=(r, g, b, int(a * 0.55)), width=width + 20)
        draw_tight.arc(bbox, start=start_deg, end=end_deg, fill=(r, g, b, int(a * 0.85)), width=width + 6)
        draw_sharp.arc(bbox, start=start_deg, end=end_deg, fill=(r, g, b, a), width=width)

        # Highlight inner filament for crisp neon-tube core
        hi_color = (
            min(255, r + 130),
            min(255, g + 130),
            min(255, b + 130),
            int(a * 0.72),
        )
        draw_sharp.arc(bbox, start=start_deg, end=end_deg, fill=hi_color, width=max(3, width // 4))

    wide_layer = wide_layer.filter(ImageFilter.GaussianBlur(blur_wide))
    tight_layer = tight_layer.filter(ImageFilter.GaussianBlur(blur_tight))

    canvas.alpha_composite(wide_layer)
    canvas.alpha_composite(tight_layer)
    canvas.alpha_composite(sharp_layer)


def draw_radial_glow(canvas: Image.Image, radius: float, color: tuple[int, int, int], alpha: int) -> None:
    """Composite a soft radial glow centered on the icon."""
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.ellipse(
        [CENTER - radius, CENTER - radius, CENTER + radius, CENTER + radius],
        fill=(*color, alpha),
    )
    layer = layer.filter(ImageFilter.GaussianBlur(radius * 0.45))
    canvas.alpha_composite(layer)


def render_master_icon() -> Image.Image:
    """Render the 1024x1024 RGBA master icon."""
    squircle_radius = 824.0  # 1648x1648 body inside 2048x2048 canvas (macOS Dock proportion)
    squircle_mask = make_squircle_mask(CANVAS_SIZE, squircle_radius)

    # Base transparent canvas with subtle dark ambient drop shadow under the squircle
    master = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
    shadow_mask = make_squircle_mask(CANVAS_SIZE, squircle_radius + 4)
    shadow_fill = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 2, 8, 175))
    shadow.paste(shadow_fill, (0, 24), shadow_mask)
    shadow = shadow.filter(ImageFilter.GaussianBlur(36))
    master.alpha_composite(shadow)

    # Interior squircle artwork layer
    interior = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), BG_OBSIDIAN)

    # Subtle radial cyber-reactor background atmosphere
    draw_radial_glow(interior, 720, (12, 26, 56), 210)
    draw_radial_glow(interior, 540, VIOLET, 42)
    draw_radial_glow(interior, 360, CYAN, 48)

    # Faint orbital HUD grid & crosshairs
    grid = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(grid)
    for r in (210, 340, 470, 600, 725):
        gdraw.ellipse([CENTER - r, CENTER - r, CENTER + r, CENTER + r], outline=(0, 240, 255, 24), width=2)
    for angle in (0, 45, 90, 135):
        p1 = polar(CENTER, CENTER, 740, angle)
        p2 = polar(CENTER, CENTER, 740, angle + 180)
        gdraw.line([p1, p2], fill=(139, 92, 246, 28), width=2)
    interior.alpha_composite(grid)

    # Radar ticks around outer telemetry perimeter
    ticks = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(ticks)
    for i in range(72):
        deg = i * 5.0
        if i % 18 == 0:
            r_in, r_out, w, col = 636, 706, 8, (*CYAN_BRIGHT, 235)
        elif i % 6 == 0:
            r_in, r_out, w, col = 652, 698, 5, (*CYAN, 195)
        elif i % 2 == 0:
            r_in, r_out, w, col = 666, 690, 3, (*VIOLET, 135)
        else:
            r_in, r_out, w, col = 672, 688, 2, (*CYAN, 85)
        tdraw.line([polar(CENTER, CENTER, r_in, deg), polar(CENTER, CENTER, r_out, deg)], fill=col, width=w)
    interior.alpha_composite(ticks)

    # Multi-ring glowing cyan + violet orbital reactor rings
    arc_specs = [
        # Outer cyan reactor arcs (radius 565)
        (565, -62, 34, 24, (*CYAN, 245)),
        (565, 62, 168, 24, (*CYAN, 245)),
        (565, 198, 272, 24, (*CYAN, 225)),
        # Middle electric violet orbital ring (radius 462)
        (462, -135, -18, 20, (*VIOLET, 245)),
        (462, 14, 132, 20, (*VIOLET, 245)),
        (462, 166, 202, 18, (*VIOLET, 205)),
        # Inner high-energy cyan gyro ring (radius 356)
        (356, -28, 128, 16, (*CYAN, 250)),
        (356, 152, 308, 16, (*CYAN, 250)),
        # Fine telemetry ring (radius 276)
        (276, 0, 359, 5, (*VIOLET_BRIGHT, 165)),
    ]
    draw_glow_arcs(interior, arc_specs, blur_wide=32.0, blur_tight=10.0)

    # Orbiting satellite nodes along the cyan and violet rings
    nodes_glow = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
    nodes_sharp = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
    ngdraw = ImageDraw.Draw(nodes_glow)
    nsdraw = ImageDraw.Draw(nodes_sharp)

    node_positions = [
        (565, -62, 18, CYAN),
        (565, 168, 16, CYAN),
        (462, -18, 16, VIOLET),
        (462, 132, 15, VIOLET),
        (356, -28, 13, EMERALD),  # Live healthy telemetry node accent
    ]
    for r_orbit, deg, nr, col in node_positions:
        nx, ny = polar(CENTER, CENTER, r_orbit, deg)
        ngdraw.ellipse([nx - nr * 1.9, ny - nr * 1.9, nx + nr * 1.9, ny + nr * 1.9], fill=(*col, 190))
        nsdraw.ellipse([nx - nr, ny - nr, nx + nr, ny + nr], fill=(*col, 255))
        nsdraw.ellipse(
            [nx - nr * 0.48, ny - nr * 0.48, nx + nr * 0.48, ny + nr * 0.48],
            fill=(245, 253, 255, 255),
        )

    interior.alpha_composite(nodes_glow.filter(ImageFilter.GaussianBlur(16)))
    interior.alpha_composite(nodes_sharp)

    # Central Nexus Core (hexagonal reactor aperture + 3-plane triad + luminous core)
    core_glow = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
    core_sharp = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
    cgdraw = ImageDraw.Draw(core_glow)
    csdraw = ImageDraw.Draw(core_sharp)

    # Dark core backdrop disc
    csdraw.ellipse([CENTER - 232, CENTER - 232, CENTER + 232, CENTER + 232], fill=(6, 10, 20, 235))

    # Hexagonal outer housing
    hex_pts = [polar(CENTER, CENTER, 210, -90 + i * 60) for i in range(6)]
    cgdraw.polygon(hex_pts, outline=(*CYAN, 210), width=18)
    csdraw.polygon(hex_pts, outline=(*CYAN, 245), width=10)

    # Inner rotated violet hexagon
    inner_hex_pts = [polar(CENTER, CENTER, 156, -60 + i * 60) for i in range(6)]
    cgdraw.polygon(inner_hex_pts, outline=(*VIOLET, 190), width=12)
    csdraw.polygon(inner_hex_pts, outline=(*VIOLET_BRIGHT, 220), width=6)

    # Three-plane nexus beams (120 deg symmetry: NVMe, SSD, SecondBrain converging)
    for beam_deg in (-90, 30, 150):
        p_start = polar(CENTER, CENTER, 48, beam_deg)
        p_end = polar(CENTER, CENTER, 196, beam_deg)
        cgdraw.line([p_start, p_end], fill=(*CYAN, 230), width=18)
        csdraw.line([p_start, p_end], fill=(*CYAN_BRIGHT, 245), width=9)

    # Crisp central geometric Nexus diamond / core iris
    diamond = [
        (CENTER, CENTER - 84),
        (CENTER + 84, CENTER),
        (CENTER, CENTER + 84),
        (CENTER - 84, CENTER),
    ]
    cgdraw.polygon(diamond, fill=(*CYAN, 230))
    csdraw.polygon(diamond, fill=(*CYAN, 255), outline=(*CYAN_BRIGHT, 255), width=4)

    # Inner bright core jewel
    inner_diamond = [
        (CENTER, CENTER - 42),
        (CENTER + 42, CENTER),
        (CENTER, CENTER + 42),
        (CENTER - 42, CENTER),
    ]
    csdraw.polygon(inner_diamond, fill=(240, 253, 255, 255))

    interior.alpha_composite(core_glow.filter(ImageFilter.GaussianBlur(22)))
    interior.alpha_composite(core_sharp)

    # Subtle diagonal HUD corner brackets cleanly inset inside the squircle
    brackets = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(brackets)
    pad = 358
    arm = 62
    corners = [
        (pad, pad, 1, 1, CYAN),
        (CANVAS_SIZE - pad, pad, -1, 1, VIOLET),
        (pad, CANVAS_SIZE - pad, 1, -1, VIOLET),
        (CANVAS_SIZE - pad, CANVAS_SIZE - pad, -1, -1, CYAN),
    ]
    for bx, by, sx, sy, col in corners:
        bdraw.line([(bx, by + sy * arm), (bx, by), (bx + sx * arm, by)], fill=(*col, 165), width=6)
    interior.alpha_composite(brackets)

    # Silky-smooth cyan-to-violet squircle rim via mask subtraction
    outer_rim_mask = make_squircle_mask(CANVAS_SIZE, squircle_radius)
    inner_rim_mask = make_squircle_mask(CANVAS_SIZE, squircle_radius - 6.0)
    rim_band = ImageChops.subtract(outer_rim_mask, inner_rim_mask)
    rim_grad = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 240, 255, 130))
    g_draw = ImageDraw.Draw(rim_grad)
    # Subtle violet glow toward bottom-right edge of the rim
    g_draw.ellipse([CENTER - 100, CENTER - 100, CANVAS_SIZE + 450, CANVAS_SIZE + 450], fill=(139, 92, 246, 135))
    rim_grad = rim_grad.filter(ImageFilter.GaussianBlur(120))
    rim_layer = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
    rim_layer.paste(rim_grad, (0, 0), rim_band)
    interior.alpha_composite(rim_layer)

    # Clip interior to macOS squircle and composite onto shadow base
    clipped_interior = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
    clipped_interior.paste(interior, (0, 0), squircle_mask)
    master.alpha_composite(clipped_interior)

    return master.resize((FINAL_SIZE, FINAL_SIZE), Image.Resampling.LANCZOS)


def build_icns(output_icns: Path, preview_png: Path | None = None) -> None:
    """Render master icon, populate temporary .iconset, and compile .icns via iconutil."""
    master_1024 = render_master_icon()
    output_icns.parent.mkdir(parents=True, exist_ok=True)

    if preview_png is not None:
        preview_png.parent.mkdir(parents=True, exist_ok=True)
        master_1024.save(preview_png, format="PNG")

    with tempfile.TemporaryDirectory(prefix="mac_nexus_icon_") as tmpdir:
        iconset_dir = Path(tmpdir) / "AppIcon.iconset"
        iconset_dir.mkdir(parents=True, exist_ok=True)

        for filename, px in ICONSET_SPECS:
            resized = (
                master_1024
                if px == FINAL_SIZE
                else master_1024.resize((px, px), Image.Resampling.LANCZOS)
            )
            resized.save(iconset_dir / filename, format="PNG")

        subprocess.run(
            ["/usr/bin/iconutil", "-c", "icns", str(iconset_dir), "-o", str(output_icns)],
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Mac Nexus Retina .icns icon")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_ICNS_PATH,
        help="Destination .icns file path",
    )
    parser.add_argument(
        "--preview",
        type=Path,
        default=None,
        help="Optional path to save a 1024x1024 PNG preview",
    )
    args = parser.parse_args()
    build_icns(args.output, preview_png=args.preview)
    print(f"Generated ICNS: {args.output} ({args.output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
