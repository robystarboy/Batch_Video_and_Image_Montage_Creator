#!/usr/bin/env python3
"""
Batch Video and Image Montage Creator

Creates video montages from folders of video clips and images. Appends a Polaroid-style image slideshow if images are present.

Features:
- Processes all subfolders in the script directory
- User can set montage length (default 60 seconds) at runtime
- Appends a Polaroid-style image slideshow if images are present
- Deduplicates similar videos and images
- Handles HEIC image conversion (macOS only)
- Progress bars and detailed status output

Requirements:
- Python 3.7+
- ffmpeg and ffprobe (must be installed and in your PATH)
- macOS: sips (for HEIC conversion, built-in)
- Python packages: pillow, imagehash, certifi (see requirements.txt)

Usage:
1. Place this script in a directory containing one or more subfolders. Each subfolder should contain video clips and/or images.
2. Run the script:
    python make_all_montages.py
3. The script will process each subfolder, creating a montage_<foldername>.mp4 in each.

See README.md for full instructions and details.
"""


import re
import sys
import subprocess
import tempfile
import json
import math
import os
import ssl
from pathlib import Path

# Dependency checks
missing_deps = []
try:
    import certifi
    ssl_context = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    missing_deps.append('certifi')
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

try:
    from PIL import Image, ImageDraw, ImageFilter
    PIL_AVAILABLE = True
except ImportError:
    missing_deps.append('pillow')
    PIL_AVAILABLE = False

try:
    import imagehash
    IMAGEHASH_AVAILABLE = True
except ImportError:
    missing_deps.append('imagehash')
    IMAGEHASH_AVAILABLE = False

if missing_deps:
    print("\n❌ Missing required Python packages:")
    for dep in missing_deps:
        print(f"   - {dep}")
    print("\nInstall them with:  pip install -r requirements.txt\n")
    sys.exit(1)

BASE_DIR    = Path(__file__).parent.resolve()  # subfolders in same directory as script
TRANSITION  = 0.5
VIDEO_EXTS  = {".mp4", ".mov", ".m4v", ".avi", ".mts", ".mkv"}
PHOTO_EXTS  = {".jpg", ".jpeg", ".png", ".heic", ".tiff", ".tif", ".bmp"}
PHOTO_HOLD  = 5        # seconds to hold the polaroid fan frame
CANVAS_W    = 1920
CANVAS_H    = 1080

def natural_sort_key(p):
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", p.name)]

def check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        subprocess.run(["ffprobe", "-version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

def get_duration(path):
    result = subprocess.run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ], capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except:
        return None

def probe_clip(path):
    result = subprocess.run([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-of", "default=noprint_wrappers=1",
        str(path)
    ], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() != ""

def is_portrait(path):
    result = subprocess.run([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,side_data_list",
        "-show_entries", "stream_side_data=rotation",
        "-of", "json",
        str(path)
    ], capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        w = stream.get("width", 0)
        h = stream.get("height", 0)
        rotation = 0
        for sd in stream.get("side_data_list", []):
            if "rotation" in sd:
                rotation = abs(int(sd["rotation"]))
        if rotation in (90, 270):
            w, h = h, w
        return h > w
    except:
        return False

def convert_heic(path, tmpdir):
    """Convert HEIC to JPEG using sips (macOS built-in)."""
    out = Path(tmpdir) / (path.stem + "_converted.jpg")
    result = subprocess.run(
        ["sips", "-s", "format", "jpeg", str(path), "--out", str(out)],
        capture_output=True
    )
    return out if result.returncode == 0 and out.exists() else None


def compute_polaroid_layout(n, pol_sizes, canvas_w, canvas_h):
    """
    Compute position and angle for each Polaroid so that:
    - All cards stay within canvas boundaries
    - Photos are displayed simultaneously (some overlap OK)
    - Layout adapts to count: 1=center, 2-3=row, 4-6=2-row grid, 7+=3-row grid
    Returns list of (cx, cy, angle) per photo.
    """
    import math

    # Gentle rotation: small angles so cards don't poke outside bounds much
    def angle_for(i, total):
        if total == 1:
            return 0
        spread = min(25, 8 * total)
        return -spread/2 + i * (spread / max(total - 1, 1))

    if n == 1:
        return [(canvas_w // 2, canvas_h // 2, 0)]

    # Determine grid layout
    if n <= 3:
        cols, rows = n, 1
    elif n <= 6:
        cols = math.ceil(n / 2)
        rows = 2
    elif n <= 9:
        cols = math.ceil(n / 3)
        rows = 3
    else:
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)

    # Compute max polaroid size across all photos for spacing
    max_pw = max(pw for pw, ph in pol_sizes)
    max_ph = max(ph for pw, ph in pol_sizes)

    # Overlap factor: 0.75 means 25% overlap between adjacent cards
    overlap = 0.88  # less overlap so photos are mostly visible
    cell_w = max_pw * overlap
    cell_h = max_ph * overlap

    # Total grid size
    grid_w = cell_w * cols
    grid_h = cell_h * rows

    # Scale down if grid exceeds canvas (with margin)
    margin = 40
    scale = min(
        (canvas_w - margin * 2) / max(grid_w, 1),
        (canvas_h - margin * 2) / max(grid_h, 1),
        1.0  # never scale up
    )
    cell_w *= scale
    cell_h *= scale

    # Center the grid on canvas
    start_x = (canvas_w - cell_w * cols) / 2 + cell_w / 2
    start_y = (canvas_h - cell_h * rows) / 2 + cell_h / 2

    positions = []
    for i in range(n):
        row = i // cols
        col = i % cols
        # Center last row if incomplete
        last_row_count = n - (rows - 1) * cols
        if row == rows - 1 and last_row_count < cols:
            x_offset = (cols - last_row_count) * cell_w / 2
        else:
            x_offset = 0
        cx = start_x + col * cell_w + x_offset
        cy = start_y + row * cell_h
        angle = angle_for(i, n)
        positions.append((int(cx), int(cy), angle))

    return positions

def make_polaroid_frame(photo_paths, tmpdir):
    """
    Compose a 1920x1080 black canvas with photos arranged as fanned/stacked Polaroids.
    Returns path to the output PNG, or None on failure.
    """
    if not PIL_AVAILABLE:
        print("  ⚠️  Pillow not installed — skipping photo slide. Run: pip install pillow")
        return None

    # Convert HEIC files first
    usable = []
    for p in photo_paths:
        if p.suffix.lower() == ".heic":
            converted = convert_heic(p, tmpdir)
            if converted:
                usable.append(converted)
            else:
                print(f"  ⚠️  Could not convert HEIC: {p.name}")
        else:
            usable.append(p)

    if not usable:
        return None

    n = len(usable)

    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (0, 0, 0))

    # Polaroid dimensions
    MAX_PHOTO  = 580     # sized to fit well within 1920x1080 canvas
    BORDER     = 30      # fixed border on all four sides

    # Pre-load all images to get polaroid sizes for layout calculation
    loaded = []
    for photo_path in usable:
        try:
            img = Image.open(photo_path).convert("RGB")
            img.thumbnail((MAX_PHOTO, MAX_PHOTO), Image.LANCZOS)
            pw, ph = img.size
            loaded.append((photo_path, img, pw + BORDER * 2, ph + BORDER * 2))
        except Exception as e:
            print(f"  ⚠️  Could not open {Path(photo_path).name}: {e}")

    if not loaded:
        return None

    n = len(loaded)
    pol_sizes = [(pol_w, pol_h) for _, _, pol_w, pol_h in loaded]
    layout = compute_polaroid_layout(n, pol_sizes, CANVAS_W, CANVAS_H)

    # Draw shadow + polaroid layers back to front
    for i, ((photo_path, img, pol_w, pol_h), (cx, cy, angle)) in enumerate(zip(loaded, layout)):
        pw, ph = img.size
        px_final_base = cx
        py_final_base = cy

        # Create Polaroid card, rotate at 2x for anti-aliasing, downscale
        AA = 2
        pol_aa = Image.new("RGBA", (pol_w * AA, pol_h * AA), (255, 255, 255, 255))
        img_aa = img.convert("RGBA").resize((img.width * AA, img.height * AA), Image.LANCZOS)
        pol_aa.paste(img_aa, (BORDER * AA, BORDER * AA))
        rotated_aa = pol_aa.rotate(angle, expand=True, resample=Image.BICUBIC)
        rotated = rotated_aa.resize(
            (rotated_aa.width // AA, rotated_aa.height // AA), Image.LANCZOS
        )
        rw, rh = rotated.size
        px_final = px_final_base - rw // 2
        py_final = py_final_base - rh // 2

        # Clamp to canvas bounds
        px_final = max(0, min(px_final, CANVAS_W - rw))
        py_final = max(0, min(py_final, CANVAS_H - rh))

        # Tight shadow to simulate photos stacked nearly on top of each other
        _, _, _, a_ch = rotated.split()
        shadow_mask = a_ch.filter(ImageFilter.GaussianBlur(4))
        shadow_img = Image.new("RGBA", rotated.size, (0, 0, 0, 220))
        shadow_img.putalpha(shadow_mask)
        shadow_layer = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        sx = min(max(0, px_final + 5), CANVAS_W - rw)
        sy = min(max(0, py_final + 5), CANVAS_H - rh)
        shadow_layer.paste(shadow_img, (sx, sy), shadow_img)

        # Composite shadow then polaroid onto canvas
        canvas_rgba = canvas.convert("RGBA")
        canvas_rgba = Image.alpha_composite(canvas_rgba, shadow_layer)
        pol_layer = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        pol_layer.paste(rotated, (px_final, py_final), rotated)
        canvas_rgba = Image.alpha_composite(canvas_rgba, pol_layer)
        canvas = canvas_rgba.convert("RGB")

    out_path = Path(tmpdir) / "polaroid_frame.png"
    canvas.save(out_path)
    return out_path



def extract_media_metadata(path):
    """
    Extract creation datetime and GPS coordinates from a video or photo.
    Returns dict with keys: 'datetime' (datetime obj or None),
                            'lat' (float or None), 'lon' (float or None)
    For videos: uses ffprobe tags.
    For photos: uses EXIF data via Pillow.
    """
    import json as json_mod
    from datetime import datetime, timezone

    result = {"datetime": None, "lat": None, "lon": None}
    suffix = Path(path).suffix.lower()

    # ── Video metadata via ffprobe ──────────────────────────────────────
    if suffix in {".mov", ".mp4", ".m4v", ".avi", ".mts", ".mkv"}:
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", str(path)],
                capture_output=True, text=True
            )
            data = json_mod.loads(probe.stdout)
            tags = data.get("format", {}).get("tags", {})

            # Creation time — prefer creationdate (local tz) over creation_time (UTC)
            for key in ["com.apple.quicktime.creationdate", "creation_time"]:
                raw = tags.get(key, "")
                if raw:
                    for fmt in ["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
                                 "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"]:
                        try:
                            dt = datetime.strptime(raw.replace("Z", "+00:00"), fmt)
                            result["datetime"] = dt.astimezone(timezone.utc)
                            break
                        except:
                            pass
                    if result["datetime"]:
                        break

            # GPS from ISO6709 string e.g. "+64.0485-016.1792+014.202/"
            iso = tags.get("com.apple.quicktime.location.ISO6709", "")
            if iso:
                import re
                m = re.match(r"([+-]\d+\.\d+)([+-]\d+\.\d+)", iso)
                if m:
                    result["lat"] = float(m.group(1))
                    result["lon"] = float(m.group(2))
        except:
            pass

    # ── Photo metadata via Pillow EXIF ─────────────────────────────────
    else:
        try:
            from PIL import Image as PilImage, ExifTags
            img = PilImage.open(path)
            exif_raw = img._getexif()
            if exif_raw:
                exif = {ExifTags.TAGS.get(k, k): v for k, v in exif_raw.items()}

                # Datetime — prefer DateTimeOriginal
                for key in ["DateTimeOriginal", "DateTimeDigitized", "DateTime"]:
                    raw = exif.get(key, "")
                    if raw:
                        try:
                            result["datetime"] = datetime.strptime(
                                str(raw), "%Y:%m:%d %H:%M:%S"
                            ).replace(tzinfo=timezone.utc)
                            break
                        except:
                            pass

                # GPS
                gps_info = exif.get("GPSInfo", {})
                if gps_info:
                    gps = {ExifTags.GPSTAGS.get(k, k): v for k, v in gps_info.items()}
                    def dms_to_dd(dms, ref):
                        try:
                            d = float(dms[0]); m = float(dms[1]); s = float(dms[2])
                            dd = d + m/60 + s/3600
                            return -dd if ref in ("S", "W") else dd
                        except:
                            return None
                    lat = dms_to_dd(gps.get("GPSLatitude", []), gps.get("GPSLatitudeRef", ""))
                    lon = dms_to_dd(gps.get("GPSLongitude", []), gps.get("GPSLongitudeRef", ""))
                    if lat is not None and lon is not None:
                        result["lat"] = lat
                        result["lon"] = lon
        except:
            pass

    return result

def deduplicate_photos(photo_paths, tmpdir, hash_threshold=8):
    """
    Remove near-duplicate photos using a weighted signal approach:
      STRONG signals (any one alone = duplicate):
        - Exact creation datetime match (same second) — burst mode detection
        - Exact GPS lat/lon + within 1 second — Live Photo / burst pair
      SUPPORTING signals (need visual confirmation too):
        - pHash + dHash both within threshold
        - Color histogram similarity (BC >= 0.85)
    Also detects burst mode sequences: consecutive photos within 1 second
    of each other at the same location — keeps only the best (highest res).
    Keeps the highest resolution version when duplicates found.
    """
    if len(photo_paths) <= 1:
        return photo_paths

    try:
        import imagehash
    except ImportError:
        print("    ⚠️  imagehash not installed — skipping deduplication.")
        return photo_paths

    print(f"    Analysing {len(photo_paths)} photos (hashes + metadata)...")

    from PIL import ImageOps

    def color_histogram_similar(img_a, img_b, threshold=0.85):
        import math
        def hist(img):
            h = img.resize((64, 64)).histogram()
            t = sum(h) or 1
            return [v/t for v in h]
        return sum(math.sqrt(a*b) for a,b in zip(hist(img_a), hist(img_b))) >= threshold

    photo_data = []
    for p in photo_paths:
        try:
            img = Image.open(p)
            img = ImageOps.exif_transpose(img).convert("RGB")
            img.thumbnail((256, 256), Image.LANCZOS)
            ph = imagehash.phash(img)
            dh = imagehash.dhash(img)
            try:
                res = Image.open(p).size
                resolution = res[0] * res[1]
            except:
                resolution = 0
            meta = extract_media_metadata(p)
            photo_data.append((p, ph, dh, img, resolution, meta))
        except Exception as e:
            photo_data.append((p, None, None, None, 0, {"datetime": None, "lat": None, "lon": None}))

    def gps_exact_match(m1, m2):
        if m1["lat"] is None or m2["lat"] is None:
            return False
        return (round(m1["lat"], 4) == round(m2["lat"], 4) and
                round(m1["lon"], 4) == round(m2["lon"], 4))

    def time_within(m1, m2, seconds=1):
        if m1["datetime"] is None or m2["datetime"] is None:
            return False
        return abs((m1["datetime"] - m2["datetime"]).total_seconds()) <= seconds

    def visual_similar(ph_i, dh_i, img_i, ph_j, dh_j, img_j):
        if ph_i is None or ph_j is None:
            return False
        if (ph_i - ph_j) > hash_threshold or (dh_i - dh_j) > hash_threshold:
            return False
        if img_i is not None and img_j is not None:
            if not color_histogram_similar(img_i, img_j):
                return False
        return True

    def are_duplicates(pd_i, pd_j):
        _, ph_i, dh_i, img_i, _, m_i = pd_i
        _, ph_j, dh_j, img_j, _, m_j = pd_j

        # STRONG: exact same datetime (burst mode — same second)
        if (m_i["datetime"] and m_j["datetime"] and
                m_i["datetime"] == m_j["datetime"]):
            return True

        # STRONG: exact GPS + within 1 second (Live Photo / rapid burst)
        if gps_exact_match(m_i, m_j) and time_within(m_i, m_j, seconds=1):
            return True

        # SUPPORTING: visual similarity
        if visual_similar(ph_i, dh_i, img_i, ph_j, dh_j, img_j):
            # If both have GPS and they differ, don't flag as duplicate
            if (m_i["lat"] is not None and m_j["lat"] is not None and
                    not gps_exact_match(m_i, m_j)):
                return False
            return True

        return False

    # Greedy dedup — keep highest resolution
    kept = []
    dropped = set()
    for i, pd_i in enumerate(photo_data):
        if i in dropped:
            continue
        best_idx, best_res = i, pd_i[4]
        for j, pd_j in enumerate(photo_data):
            if j <= i or j in dropped:
                continue
            if are_duplicates(pd_i, pd_j):
                dropped.add(j)
                if pd_j[4] > best_res:
                    dropped.add(best_idx)
                    best_idx, best_res = j, pd_j[4]
        kept.append(photo_data[best_idx][0])
        dropped.add(i)

    removed = len(photo_paths) - len(kept)
    print(f"    ✅ Kept {len(kept)}/{len(photo_paths)} photos ({removed} near-duplicates removed)")
    return kept
def make_photo_video(photo_paths, tmpdir, output_path):
    """
    Paginated photo slideshow:
    - Photos split into pages of PAGE_SIZE
    - Each page: ALL photos appear simultaneously with fade+zoom (~2s)
    - Hold HOLD_SECS after page is fully shown
    - Next page fades in on top of previous (no black wipe)
    - PAUSE_SECS between pages (previous page still visible)
    """
    if not PIL_AVAILABLE:
        print("  ⚠️  Pillow not installed — skipping photo slide.")
        return False

    usable = []
    for p in photo_paths:
        if p.suffix.lower() == ".heic":
            converted = convert_heic(p, tmpdir)
            if converted:
                usable.append(converted)
        else:
            usable.append(p)

    if not usable:
        return False

    usable = deduplicate_photos([Path(p) for p in usable], tmpdir)
    if not usable:
        return False

    MAX_PHOTO  = 580
    BORDER     = 30
    FPS        = 30
    INTRO_SECS = 2.0    # all photos fade in together over this duration
    HOLD_SECS  = 3.0    # hold after page fully shown
    PAUSE_SECS = 3.0    # pause (previous page visible) before next page animates in
    PAGE_SIZE  = 6      # max photos per page

    pages = [usable[i:i+PAGE_SIZE] for i in range(0, len(usable), PAGE_SIZE)]
    print(f"    {len(usable)} photos across {len(pages)} page(s) of up to {PAGE_SIZE}")

    # Pre-render each page as a fully composited PIL image
    def render_page_image(page_paths):
        loaded = []
        for photo_path in page_paths:
            try:
                img = Image.open(photo_path)
                # Respect EXIF orientation as-is — no rotation applied
                from PIL import ImageOps, ImageEnhance
                img = ImageOps.exif_transpose(img)
                img = img.convert("RGB")
                # Normalize saturation to match video level (0.88)
                img = ImageEnhance.Color(img).enhance(0.88)
                img.thumbnail((MAX_PHOTO, MAX_PHOTO), Image.LANCZOS)
                pw, ph = img.size
                loaded.append((photo_path, img, pw + BORDER * 2, ph + BORDER * 2))
            except:
                pass
        if not loaded:
            return None

        n = len(loaded)
        pol_sizes = [(pw, ph) for _, _, pw, ph in loaded]
        layout    = compute_polaroid_layout(n, pol_sizes, CANVAS_W, CANVAS_H)

        # Render at 2x resolution for supersampling anti-aliasing, then downsample
        SS    = 2
        CW    = CANVAS_W * SS
        CH    = CANVAS_H * SS

        canvas = Image.new("RGB", (CW, CH), (0, 0, 0))
        for (photo_path, img, pol_w, pol_h), (cx, cy, angle) in zip(loaded, layout):
            pw, ph = img.size

            # Build Polaroid at 2x with rounded corners for smooth edges
            pol_w2, pol_h2 = pol_w * SS, pol_h * SS
            pw2,    ph2    = pw    * SS, ph    * SS
            border2        = BORDER * SS

            # Scale photo up to 2x
            img2 = img.resize((pw2, ph2), Image.LANCZOS)

            # Create Polaroid card at 2x with rounded corners mask
            pol = Image.new("RGBA", (pol_w2, pol_h2), (255, 255, 255, 255))
            pol.paste(img2, (border2, border2))

            # Rounded corner mask (radius = 8px at final res → 16 at 2x)
            radius = 16
            mask = Image.new("L", (pol_w2, pol_h2), 0)
            draw = ImageDraw.Draw(mask)
            draw.rounded_rectangle([0, 0, pol_w2-1, pol_h2-1], radius=radius, fill=255)
            pol.putalpha(mask)

            # Rotate at 2x — LANCZOS for best quality
            rotated = pol.rotate(angle, expand=True, resample=Image.BICUBIC)
            rw2, rh2 = rotated.size

            # Position on 2x canvas
            px2 = max(0, min(cx * SS - rw2 // 2, CW - rw2))
            py2 = max(0, min(cy * SS - rh2 // 2, CH - rh2))

            # Shadow at 2x
            _, _, _, a_ch = rotated.split()
            shadow_blur = a_ch.filter(ImageFilter.GaussianBlur(8))
            shadow_img  = Image.new("RGBA", rotated.size, (0, 0, 0, 200))
            shadow_img.putalpha(shadow_blur)
            shadow_layer = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
            shadow_layer.paste(shadow_img,
                               (min(max(0, px2+10), CW-rw2),
                                min(max(0, py2+10), CH-rh2)), shadow_img)

            canvas_rgba = canvas.convert("RGBA")
            canvas_rgba = Image.alpha_composite(canvas_rgba, shadow_layer)
            pol_layer   = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
            pol_layer.paste(rotated, (px2, py2), rotated)
            canvas_rgba = Image.alpha_composite(canvas_rgba, pol_layer)
            canvas      = canvas_rgba.convert("RGB")

        # Downsample 2x → 1x with LANCZOS for smooth anti-aliased edges
        return canvas.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)

    # Pre-render all page images upfront
    page_images = []
    for i, page_paths in enumerate(pages):
        img = render_page_image(page_paths)
        if img:
            page_images.append(img)
            print(f"      ✅ Page {i+1} composited")
    if not page_images:
        return False

    intro_frames = int(INTRO_SECS * FPS)
    hold_frames  = int(HOLD_SECS  * FPS)
    pause_frames = int(PAUSE_SECS * FPS)

    # Total frames:
    # For each page: intro + hold
    # Between pages: pause (showing previous page, no black)
    # Last page: intro + hold (no trailing pause)
    n_pages      = len(page_images)
    total_frames = n_pages * (intro_frames + hold_frames) + (n_pages - 1) * pause_frames

    print(f"    Rendering {total_frames} frames ({total_frames/FPS:.1f}s)...")

    frame_dir = Path(tmpdir) / "photo_frames"
    frame_dir.mkdir()

    frame_idx = 0
    for page_num, page_img in enumerate(page_images):
        prev_img = page_images[page_num - 1] if page_num > 0 else Image.new("RGB", (CANVAS_W, CANVAS_H), (0, 0, 0))

        # --- Pause phase (show previous page, no black) ---
        if page_num > 0:
            for f in range(pause_frames):
                prev_img.save(frame_dir / f"frame_{frame_idx:06d}.png")
                frame_idx += 1

        # --- Intro phase: crossfade new page over previous ---
        for f in range(intro_frames):
            t     = f / max(intro_frames - 1, 1)
            t     = t * t * (3 - 2 * t)   # ease in-out
            alpha = int(t * 255)
            blended = Image.blend(prev_img, page_img, t)
            blended.save(frame_dir / f"frame_{frame_idx:06d}.png")
            frame_idx += 1

        # --- Hold phase: show current page ---
        for f in range(hold_frames):
            page_img.save(frame_dir / f"frame_{frame_idx:06d}.png")
            frame_idx += 1

        pct    = int(frame_idx / total_frames * 100)
        filled = "█" * (pct // 5)
        empty  = "░" * (20 - pct // 5)
        sys.stdout.write(f"\r      [{filled}{empty}] {pct:3d}%  page {page_num+1}/{n_pages}")
        sys.stdout.flush()

    print()

    # Encode all frames to video
    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(FPS),
        "-i", str(frame_dir / "frame_%06d.png"),
        "-c:v", "libx264", "-preset", "slow", "-crf", "16",
        "-pix_fmt", "yuv420p",
        "-colorspace", "bt709",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-color_range", "tv",
        "-vf", "colorspace=bt709:iall=bt601-6-625:fast=1",
        "-an",
        str(output_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0 and Path(output_path).exists()

def deduplicate_videos(good_paths, tmpdir, hash_threshold=8):
    """
    Remove near-duplicate video clips using a weighted signal approach:
      STRONG signals (any one alone = duplicate):
        - Exact creation datetime match
        - Exact GPS lat/lon match + creation within 2 seconds
      SUPPORTING signals (need visual confirmation too):
        - Multi-frame perceptual hash similarity (5 frames, 60% match rate)
        - Color histogram similarity
        - Duration within 50% of each other
    Keeps the longest clip when duplicates found.
    """
    try:
        import imagehash
    except ImportError:
        print("  ⚠️  imagehash not installed — skipping video dedup.")
        return good_paths

    if len(good_paths) <= 1:
        return good_paths

    NUM_FRAMES = 5

    total = len(good_paths)
    print(f"  Checking {total} clips for duplicates...")

    # Gather metadata + frame hashes for every clip
    clip_data = []
    for idx, item in enumerate(good_paths):
        p, dur, portrait = item
        meta = extract_media_metadata(p)
        frame_hashes = []
        for fi in range(NUM_FRAMES):
            t = dur * (fi + 1) / (NUM_FRAMES + 1)
            frame_path = Path(tmpdir) / f"thumb_{p.stem}_f{fi}.jpg"
            cmd = ["ffmpeg", "-y", "-ss", str(t), "-i", str(p),
                   "-vframes", "1", "-q:v", "5", str(frame_path)]
            r = subprocess.run(cmd, capture_output=True)
            if r.returncode == 0 and frame_path.exists():
                try:
                    img = Image.open(frame_path).convert("RGB")
                    frame_hashes.append((imagehash.phash(img), imagehash.dhash(img), img))
                except:
                    pass
        clip_data.append((item, meta, frame_hashes, dur))
        pct = int((idx + 1) / total * 100)
        filled = "█" * (pct // 5)
        empty  = "░" * (20 - pct // 5)
        print(f"\r    [{filled}{empty}] {pct:3d}%  {p.name}", end="", flush=True)
    print()  # newline after progress

    def gps_exact_match(m1, m2):
        """True if lat/lon match to 4 decimal places (~11m)."""
        if m1["lat"] is None or m2["lat"] is None:
            return False
        return (round(m1["lat"], 4) == round(m2["lat"], 4) and
                round(m1["lon"], 4) == round(m2["lon"], 4))

    def time_within(m1, m2, seconds=2):
        if m1["datetime"] is None or m2["datetime"] is None:
            return False
        return abs((m1["datetime"] - m2["datetime"]).total_seconds()) <= seconds

    def visual_similar(hashes_a, hashes_b, dur_a, dur_b):
        # Duration gate: must be within 50%
        if dur_a > 0 and dur_b > 0:
            if min(dur_a, dur_b) / max(dur_a, dur_b) < 0.50:
                return False
        if not hashes_a or not hashes_b:
            return False
        matches = sum(
            1 for ph_a, dh_a, _ in hashes_a
            for ph_b, dh_b, _ in hashes_b
            if (ph_a - ph_b) <= hash_threshold and (dh_a - dh_b) <= hash_threshold
        )
        total = len(hashes_a) * len(hashes_b)
        if total == 0 or matches / total < 0.60:
            return False
        # Color histogram on first available frames
        try:
            import math
            img_a = hashes_a[0][2]
            img_b = hashes_b[0][2]
            def hist(img):
                h = img.resize((64,64)).histogram()
                t = sum(h) or 1
                return [v/t for v in h]
            bc = sum(math.sqrt(a*b) for a,b in zip(hist(img_a), hist(img_b)))
            if bc < 0.85:
                return False
        except:
            pass
        return True

    def are_duplicates(cd_i, cd_j):
        _, m_i, h_i, dur_i = cd_i
        _, m_j, h_j, dur_j = cd_j

        # STRONG: exact datetime match
        if (m_i["datetime"] and m_j["datetime"] and
                m_i["datetime"] == m_j["datetime"]):
            return True

        # STRONG: exact GPS + within 2 seconds
        if gps_exact_match(m_i, m_j) and time_within(m_i, m_j, seconds=2):
            return True

        # SUPPORTING: visual similarity (requires GPS or time proximity if available)
        if visual_similar(h_i, h_j, dur_i, dur_j):
            # If both have GPS but it differs significantly, don't flag
            if (m_i["lat"] is not None and m_j["lat"] is not None and
                    not gps_exact_match(m_i, m_j)):
                return False
            return True

        return False

    # Greedy dedup — keep longest
    kept = []
    dropped = set()
    for i, cd_i in enumerate(clip_data):
        if i in dropped:
            continue
        best_idx, best_dur = i, cd_i[3]
        for j, cd_j in enumerate(clip_data):
            if j <= i or j in dropped:
                continue
            if are_duplicates(cd_i, cd_j):
                dropped.add(j)
                if cd_j[3] > best_dur:
                    dropped.add(best_idx)
                    best_idx, best_dur = j, cd_j[3]
        kept.append(clip_data[best_idx][0])
        dropped.add(i)

    removed = len(good_paths) - len(kept)
    if removed:
        print(f"  ✅ Removed {removed} duplicate clip(s), {len(kept)} remaining")
    else:
        print(f"  ✅ No duplicate clips found")
    return kept
def make_montage(folder, total_secs):
    video_paths = sorted(
        [p for p in folder.iterdir() if p.suffix.lower() in VIDEO_EXTS],
        key=natural_sort_key
    )
    photo_paths = sorted(
        [p for p in folder.iterdir() if p.suffix.lower() in PHOTO_EXTS],
        key=natural_sort_key
    )

    if not video_paths and not photo_paths:
        print(f"  ⚠️  No video or photo files found, skipping.\n")
        return

    # ── Report photos found ────────────────────────────────────────────────
    if photo_paths:
        print(f"  📷 Found {len(photo_paths)} photo(s) — will append Polaroid slide")

    if not video_paths:
        print(f"  ⚠️  No video files found.\n")

    # ── Probe video files ──────────────────────────────────────────────────
    good_paths = []
    if video_paths:
        print(f"\n  {'FILE':<55} STATUS")
        print(f"  {'-'*55} ------")
        for p in video_paths:
            ok = probe_clip(p)
            dur = get_duration(p) if ok else None
            if ok and dur:
                print(f"  {p.name:<55} ✅ pass  ({dur:.1f}s)")
                good_paths.append((p, dur, False))
            else:
                print(f"  {p.name:<55} ❌ fail")
        print()

    safe_name = re.sub(r"[^\w\-.]", "_", folder.name)
    output_file = folder / f"montage_{safe_name}.mp4"

    with tempfile.TemporaryDirectory() as tmpdir:
        segment_files = []

        # ── Trim video clips ───────────────────────────────────────────────
        if good_paths:
            good_paths = deduplicate_videos(good_paths, tmpdir)
            n = len(good_paths)
            seg_dur = (total_secs + (n - 1) * TRANSITION) / n
            print(f"  {n} usable clip(s), ~{seg_dur:.2f}s each\n")
            total_clips = len(good_paths)
            print(f"  Trimming clips...")
            for i, (p, dur, portrait) in enumerate(good_paths):
                seg_path = Path(tmpdir) / f"seg_{i:03d}.mp4"
                if dur <= seg_dur:
                    start = 0
                    length = dur
                else:
                    mid = dur / 2
                    start = max(0, mid - seg_dur / 2)
                    length = seg_dur

                # No rotation — preserve original video orientation as-is
                vf = f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=decrease,pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2,eq=saturation=0.88"

                print(f"    [{i+1}/{total_clips}] {p.name} ", end="", flush=True)
                cmd = [
                    "ffmpeg", "-y",
                    "-ss", str(start),
                    "-i", str(p),
                    "-t", str(length),
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k",
                    "-vf", vf,
                    "-r", "30",
                    "-movflags", "+faststart",
                    "-progress", "pipe:1",
                    "-nostats",
                    str(seg_path)
                ]
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                rc = 1
                try:
                    for line in process.stdout:
                        line = line.strip()
                        if line.startswith("out_time_ms="):
                            try:
                                ms = int(line.split("=")[1])
                                elapsed = ms / 1_000_000
                                pct = min(100, int((elapsed / length) * 100))
                                filled = "█" * (pct // 5)
                                empty  = "░" * (20 - pct // 5)
                                print(f"\r    [{i+1}/{total_clips}] {p.name}  [{filled}{empty}] {pct:3d}%", end="", flush=True)
                            except:
                                pass
                finally:
                    process.wait()
                    rc = process.returncode

                if rc == 0 and seg_path.exists():
                    segment_files.append(seg_path)
                    # Overwrite progress bar line with clean checkmark
                    label = f"    [{i+1}/{total_clips}] {p.name}  ✅"
                    print(f"\r{label:<80}")
                else:
                    diag_cmd = [c for c in cmd if c not in ("-progress", "pipe:1", "-nostats")]
                    diag = subprocess.run(diag_cmd, capture_output=True, text=True)
                    label = f"    [{i+1}/{total_clips}] {p.name}  ❌"
                    print(f"\r{label:<80}")
                    print(f"         {diag.stderr[-200:].strip()}")

        # ── Build photo slide ──────────────────────────────────────────────
        if photo_paths:
            print(f"\n  Building Polaroid photo slide...")
            photo_seg = Path(tmpdir) / "photo_slide.mp4"
            ok = make_photo_video(photo_paths, tmpdir, photo_seg)
            if ok:
                segment_files.append(photo_seg)
                print(f"    ✅ Polaroid slide created ({len(photo_paths)} photo(s), {PHOTO_HOLD}s)")
            else:
                print(f"    ❌ Failed to create photo slide")

        if not segment_files:
            print(f"  ❌  No segments created.\n")
            return

        # ── Concat all segments ────────────────────────────────────────────

        concat_list = Path(tmpdir) / "concat.txt"
        def ffmpeg_quote(path):
            # Escape single quotes for ffmpeg concat
            return "'" + str(path).replace("'", "'\\''") + "'"
        with open(concat_list, "w") as f:
            for seg in segment_files:
                f.write(f"file {ffmpeg_quote(seg)}\n")

        concat_output = Path(tmpdir) / "concat_raw.mp4"
        print(f"\n  Concatenating {len(segment_files)} segment(s)...")
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            str(concat_output)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  ❌  Concat failed: {result.stderr[-300:]}\n")
            return

        # Total duration = video + photo slide
        total_dur = get_duration(concat_output) or TOTAL_SECS

        # ── Final export with progress ─────────────────────────────────────
        print(f"  Finalizing → {output_file.name}")
        cmd = [
            "ffmpeg", "-y",
            "-i", str(concat_output),
            "-t", str(total_dur),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-vf", f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=decrease,pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2",
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            "-nostats",
            str(output_file)
        ]
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            for line in process.stdout:
                line = line.strip()
                if line.startswith("out_time_ms="):
                    try:
                        ms = int(line.split("=")[1])
                        current_time = ms / 1_000_000
                        pct = min(100, int((current_time / total_dur) * 100))
                        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
                        print(f"\r  [{bar}] {pct:3d}%  {current_time:.1f}s / {total_dur:.1f}s", end="", flush=True)
                    except:
                        pass
                elif line == "progress=end":
                    print(f"\r  [{'█' * 20}] 100%  {total_dur:.1f}s / {total_dur:.1f}s", flush=True)
        finally:
            process.wait()
            returncode = process.returncode

        if returncode == 0 and output_file.exists():
            size_mb = output_file.stat().st_size / 1024 / 1024
            print(f"  ✅  Montage complete → {output_file.name}  ({size_mb:.1f} MB)\n")
        else:
            print(f"\n  ❌  Final export failed.\n")

def main():
    if not check_ffmpeg():
        print("❌  ffmpeg not found.")
        print("Install it with:  brew install ffmpeg")
        sys.exit(1)

    if not BASE_DIR.exists():
        sys.exit(f"❌  Directory not found:\n    {BASE_DIR}")

    default_secs = 60
    try:
        user_input = input(f"\nEnter montage length in seconds [default {default_secs}]: ")
        total_secs = int(user_input.strip()) if user_input.strip() else default_secs
        if total_secs < 10:
            print("Minimum montage length is 10 seconds. Using default.")
            total_secs = default_secs
    except Exception:
        print("Invalid input. Using default.")
        total_secs = default_secs

    folders = sorted(
        [p for p in BASE_DIR.iterdir()
         if p.is_dir() and not p.name.startswith(".")],
        key=natural_sort_key
    )

    print(f"Found {len(folders)} folder(s) in {BASE_DIR}:\n")
    for f in folders:
        print(f"  • {f.name}")
    print()

    for folder in folders:
        safe_name = re.sub(r"[^\w\-.]", "_", folder.name)
        output_file = folder / f"montage_{safe_name}.mp4"

        print(f"{'='*62}")
        print(f"  📁 {folder.name}")
        print(f"{'='*62}")

        if output_file.exists():
            print(f"  ⏭️  Montage already exists, skipping.\n")
            continue

        make_montage(folder, total_secs)

    print(f"{'='*62}")
    print("🎬  All folders processed!")

if __name__ == "__main__":
    main()