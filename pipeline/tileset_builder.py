import os
import json
import math


# Geometric error values per LOD level
# Higher = shown from further away
LOD_GEOMETRIC_ERROR = {
    "lod0": 500,   # rough — shown from far
    "lod1": 100,   # medium
    "lod2": 0      # full detail — shown only when close
}

# Camera distance thresholds in meters
LOD_DISTANCES = {
    "lod0": 2000,
    "lod1": 500,
    "lod2": 0
}


def compute_ecef_transform(lon, lat, height=0):
    lon_r = math.radians(lon)
    lat_r = math.radians(lat)
    R     = 6378137 + height

    return [
        -math.sin(lon_r),
         math.cos(lon_r),
         0, 0,
        -math.sin(lat_r) * math.cos(lon_r),
        -math.sin(lat_r) * math.sin(lon_r),
         math.cos(lat_r), 0,
         math.cos(lat_r) * math.cos(lon_r),
         math.cos(lat_r) * math.sin(lon_r),
         math.sin(lat_r), 0,
         R * math.cos(lat_r) * math.cos(lon_r),
         R * math.cos(lat_r) * math.sin(lon_r),
         R * math.sin(lat_r), 1
    ]


def compute_region_bounding_volume(bounds, min_height=0, max_height=100):
    return {
        "region": [
            math.radians(bounds["min_lon"]),
            math.radians(bounds["min_lat"]),
            math.radians(bounds["max_lon"]),
            math.radians(bounds["max_lat"]),
            min_height,
            max_height
        ]
    }


def compute_box_bounding_volume(width=50, depth=50, height=25):
    hw = width  / 2
    hd = depth  / 2
    hh = height / 2
    return {
        "box": [
            0, 0, hh,
            hw, 0, 0,
            0, hd, 0,
            0, 0, hh
        ]
    }


def build_model_tileset(model, b3dm_map, bbox, output_dir):
    name      = model["name"]
    lon       = model["lon"]
    lat       = model["lat"]
    height    = model.get("height", 0)

    width        = bbox.get("width", 50)
    depth        = bbox.get("depth", 50)
    model_height = bbox.get("height", 25)

    transform = compute_ecef_transform(lon, lat, height)

    # Build LOD children from finest to coarsest
    # LOD2 is child of LOD1 which is child of LOD0
    lod_order = ["lod0", "lod1", "lod2"]

    def build_lod_node(lod_index):
        lod_level = lod_order[lod_index]

        if lod_level not in b3dm_map:
            return None

        b3dm_path   = b3dm_map[lod_level]
        # Make path relative to tileset.json location
        b3dm_rel    = os.path.relpath(b3dm_path, output_dir)

        geometric_error = LOD_GEOMETRIC_ERROR[lod_level]

        node = {
            "boundingVolume": compute_box_bounding_volume(width, depth, model_height),
            "geometricError": geometric_error,
            "refine": "REPLACE",
            "content": {
                "uri": b3dm_rel.replace("\\", "/")
            }
        }

        # Add finer LOD as child
        if lod_index + 1 < len(lod_order):
            child = build_lod_node(lod_index + 1)
            if child:
                node["children"] = [child]

        return node

    root_node = build_lod_node(0)

    if root_node is None:
        return None

    root_node["transform"] = transform

    tileset = {
        "asset":        { "version": "1.0" },
        "geometricError": LOD_GEOMETRIC_ERROR["lod0"],
        "root":           root_node
    }

    tileset_path = os.path.join(output_dir, name + "_tileset.json")
    with open(tileset_path, "w") as f:
        json.dump(tileset, f, indent=2)

    print(f"[TilesetBuilder] Model tileset → {tileset_path}")
    return tileset_path


def build_cell_tileset(cell, model_tileset_paths, output_dir):
    cell_id = cell["cell_id"]
    bounds  = cell["bounds"]

    children = []
    for model_name, tileset_path in model_tileset_paths.items():
        if tileset_path is None:
            continue
        rel_path = os.path.relpath(tileset_path, output_dir)
        children.append({
            "boundingVolume": compute_region_bounding_volume(bounds),
            "geometricError": LOD_GEOMETRIC_ERROR["lod0"],
            "refine": "ADD",
            "content": {
                "uri": rel_path.replace("\\", "/")
            }
        })

    if not children:
        return None

    tileset = {
        "asset": { "version": "1.0" },
        "geometricError": LOD_GEOMETRIC_ERROR["lod0"] * 2,
        "root": {
            "boundingVolume": compute_region_bounding_volume(bounds),
            "geometricError": LOD_GEOMETRIC_ERROR["lod0"],
            "refine": "ADD",
            "children": children
        }
    }

    tileset_path = os.path.join(output_dir, cell_id, "tileset.json")
    os.makedirs(os.path.dirname(tileset_path), exist_ok=True)

    with open(tileset_path, "w") as f:
        json.dump(tileset, f, indent=2)

    print(f"[TilesetBuilder] Cell tileset → {tileset_path}")
    return tileset_path


def build_root_tileset(cells, cell_tileset_paths, output_dir, scene_bounds):
    children = []

    for cell_id, tileset_path in cell_tileset_paths.items():
        if tileset_path is None:
            continue
        cell   = cells[cell_id]
        bounds = cell["bounds"]
        rel    = os.path.relpath(tileset_path, output_dir)

        children.append({
            "boundingVolume": compute_region_bounding_volume(bounds),
            "geometricError": LOD_GEOMETRIC_ERROR["lod0"] * 2,
            "refine": "ADD",
            "content": {
                "uri": rel.replace("\\", "/")
            }
        })

    tileset = {
        "asset": { "version": "1.0" },
        "geometricError": LOD_GEOMETRIC_ERROR["lod0"] * 10,
        "root": {
            "boundingVolume": compute_region_bounding_volume(scene_bounds),
            "geometricError": LOD_GEOMETRIC_ERROR["lod0"] * 2,
            "refine": "ADD",
            "children": children
        }
    }

    tileset_path = os.path.join(output_dir, "tileset.json")
    with open(tileset_path, "w") as f:
        json.dump(tileset, f, indent=2)

    print(f"[TilesetBuilder] Root tileset → {tileset_path}")
    return tileset_path