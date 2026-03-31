import os
import subprocess
import shutil
import math


TILES_TOOLS_PATH = shutil.which("3d-tiles-tools") or "/opt/homebrew/bin/3d-tiles-tools"


def glb_to_b3dm(glb_path, b3dm_path):
    os.makedirs(os.path.dirname(b3dm_path), exist_ok=True)

    if not os.path.isfile(TILES_TOOLS_PATH):
        print(f"[TileGen] ERROR: 3d-tiles-tools not found at {TILES_TOOLS_PATH}")
        return False

    print(f"[TileGen] Converting: {os.path.basename(glb_path)} → {os.path.basename(b3dm_path)}")

    try:
        result = subprocess.run(
            [TILES_TOOLS_PATH, "glbToB3dm",
             "-i", glb_path,
             "-o", b3dm_path],
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode != 0:
            print(f"[TileGen] ERROR: {result.stderr.strip()}")
            return False

        if not os.path.isfile(b3dm_path):
            print(f"[TileGen] ERROR: b3dm not created at {b3dm_path}")
            return False

        size = os.path.getsize(b3dm_path)
        print(f"[TileGen] Created {os.path.basename(b3dm_path)} — {size/1024:.1f} KB")
        return True

    except FileNotFoundError:
        print(f"[TileGen] ERROR: 3d-tiles-tools not found")
        return False
    except subprocess.TimeoutExpired:
        print(f"[TileGen] ERROR: Conversion timed out")
        return False


def generate_cell_tiles(cell, lod_glb_map, output_dir):
    cell_id  = cell["cell_id"]
    b3dm_map = {}

    for lod_level, glb_path in lod_glb_map.items():
        if glb_path is None or not os.path.isfile(glb_path):
            print(f"[TileGen] Skipping {lod_level} — GLB not found")
            continue

        b3dm_dir  = os.path.join(output_dir, cell_id, lod_level)
        b3dm_path = os.path.join(b3dm_dir, "content.b3dm")

        os.makedirs(b3dm_dir, exist_ok=True)

        ok = glb_to_b3dm(glb_path, b3dm_path)
        if ok:
            b3dm_map[lod_level] = b3dm_path

    return b3dm_map