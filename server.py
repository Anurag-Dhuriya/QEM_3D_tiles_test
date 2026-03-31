import http.server
import os
import json
import subprocess
import math
import urllib.parse
import shutil
import threading
from datetime import datetime

from pipeline.decimator      import generate_all_lods
from pipeline.quadtree       import assign_cells, build_quadtree
from pipeline.tile_generator import generate_cell_tiles
from pipeline.tileset_builder import (
    build_model_tileset,
    build_cell_tileset,
    build_root_tileset
)

# ----------------------------------------------------------------
# Config
# ----------------------------------------------------------------
DIRECTORY   = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(DIRECTORY, "config.json")

DEFAULT_CONFIG = {
    "port": 8080,
    "host": "0.0.0.0",
    "models": []
}

def load_config():
    if not os.path.isfile(CONFIG_PATH):
        print(f"[Config] config.json not found — creating default")
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    with open(CONFIG_PATH) as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)

def find_model(config, name):
    for i, m in enumerate(config["models"]):
        if m["name"] == name:
            return i, m
    return None, None

def update_model_status(name, status, tileset_url=None, error=None):
    config = load_config()
    i, model = find_model(config, name)
    if model is None:
        return
    config["models"][i]["status"]      = status
    config["models"][i]["error"]       = error
    config["models"][i]["tileset_url"] = tileset_url
    if status == "ready":
        config["models"][i]["processed_at"] = \
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_config(config)

# ----------------------------------------------------------------
# Directories and tool paths
# ----------------------------------------------------------------
config = load_config()

PORT       = config.get("port", 8080)
HOST       = config.get("host", "0.0.0.0")
MODELS_DIR = os.path.join(DIRECTORY, "models")
TILES_DIR  = os.path.join(DIRECTORY, "tiles")
UPLOAD_DIR = os.path.join(DIRECTORY, "uploads")
LOD_DIR    = os.path.join(DIRECTORY, "lod")
SCENE_DIR  = os.path.join(TILES_DIR, "scene")

os.makedirs(MODELS_DIR, exist_ok=True)
os.makedirs(TILES_DIR,  exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(LOD_DIR,    exist_ok=True)
os.makedirs(SCENE_DIR,  exist_ok=True)

BLENDER_PATH   = "/Applications/Blender.app/Contents/MacOS/Blender"
BLENDER_SCRIPT = os.path.join(DIRECTORY, "blender_process.py")

print(f"[Server] Blender        : {BLENDER_PATH}")
print(f"[Server] Config         : {CONFIG_PATH}")

# ----------------------------------------------------------------
# Blender processing
# ----------------------------------------------------------------
def run_blender(obj_path, glb_path, scale_unit):
    if not os.path.isfile(BLENDER_PATH):
        return False, f"Blender not found at {BLENDER_PATH}"
    if not os.path.isfile(BLENDER_SCRIPT):
        return False, f"blender_process.py not found"

    print(f"[Server] Running Blender: {os.path.basename(obj_path)}")

    try:
        result = subprocess.run(
            [BLENDER_PATH, "--background",
             "--python", BLENDER_SCRIPT,
             "--", obj_path, glb_path, scale_unit],
            capture_output=True, text=True, timeout=300
        )

        for line in result.stdout.splitlines():
            if "[Blender]" in line:
                print(line)

        if result.returncode != 0:
            return False, "Blender failed: " + result.stderr[:200]

        if not os.path.isfile(glb_path):
            return False, f"GLB not created at {glb_path}"

        return True, "OK"

    except FileNotFoundError:
        return False, f"Blender not found at {BLENDER_PATH}"
    except subprocess.TimeoutExpired:
        return False, "Blender timed out"

# ----------------------------------------------------------------
# Read bounding box written by blender_process.py
# ----------------------------------------------------------------
def read_bbox(glb_path):
    bbox_path = glb_path.replace('.glb', '_bbox.txt')
    if os.path.isfile(bbox_path):
        try:
            with open(bbox_path) as f:
                parts = f.read().strip().split(',')
                w, d, h = float(parts[0]), float(parts[1]), float(parts[2])
            os.remove(bbox_path)
            return {"width": w, "depth": d, "height": h}
        except Exception:
            pass
    return {"width": 50, "depth": 50, "height": 25}

# ----------------------------------------------------------------
# Process a single model through full pipeline
# ----------------------------------------------------------------
def process_model(model):
    name   = model["name"]
    file   = model["file"]
    unit   = model.get("unit", "m")
    lon    = float(model.get("lon", 0))
    lat    = float(model.get("lat", 0))
    height = float(model.get("height", 0))

    print(f"\n[Server] ── Processing: {name} ──")
    update_model_status(name, "processing")

    ext    = os.path.splitext(file)[1].lower()
    is_obj = ext == ".obj"
    is_glb = ext in [".glb", ".gltf"]

    glb_path = os.path.join(MODELS_DIR, name + ".glb")

    # Step 1 — Blender (OBJ only)
    if is_obj:
        obj_path = os.path.join(UPLOAD_DIR, file)
        if not os.path.isfile(obj_path):
            err = f"OBJ not found: {obj_path}"
            update_model_status(name, "error", error=err)
            return False, err
        ok, err = run_blender(obj_path, glb_path, unit)
        if not ok:
            update_model_status(name, "error", error=err)
            return False, err

    elif is_glb:
        src = os.path.join(UPLOAD_DIR, file)
        if not os.path.isfile(src):
            src = os.path.join(MODELS_DIR, file)
        if not os.path.isfile(src):
            err = f"GLB not found: {file}"
            update_model_status(name, "error", error=err)
            return False, err
        if src != glb_path:
            shutil.copy2(src, glb_path)
    else:
        err = f"Unsupported format: {ext}"
        update_model_status(name, "error", error=err)
        return False, err

    # Step 2 — Read bounding box
    bbox = read_bbox(glb_path)
    print(f"[Server] BBox: {bbox['width']:.2f}m × "
          f"{bbox['depth']:.2f}m × {bbox['height']:.2f}m")

    # Step 3 — QEM decimation — generate LOD0, LOD1, LOD2
    print(f"[Server] Generating LOD levels via QEM...")
    lod_glbs = generate_all_lods(
        full_glb   = glb_path,
        lod_dir    = LOD_DIR,
        model_name = name
    )

    # Step 4 — Convert each LOD GLB to b3dm
    model_tile_dir = os.path.join(SCENE_DIR, "models", name)
    os.makedirs(model_tile_dir, exist_ok=True)

    b3dm_map = {}
    from pipeline.tile_generator import glb_to_b3dm
    for lod_level, lod_glb in lod_glbs.items():
        if lod_glb and os.path.isfile(lod_glb):
            b3dm_path = os.path.join(model_tile_dir, lod_level, "content.b3dm")
            os.makedirs(os.path.dirname(b3dm_path), exist_ok=True)
            ok = glb_to_b3dm(lod_glb, b3dm_path)
            if ok:
                b3dm_map[lod_level] = b3dm_path

    # Step 5 — Build model-level tileset.json with LOD hierarchy
    model_tileset_path = build_model_tileset(
        model      = model,
        b3dm_map   = b3dm_map,
        bbox       = bbox,
        output_dir = model_tile_dir
    )

    # Mark model as ready
    tileset_url = f"http://localhost:{PORT}/scene/models/{name}/{name}_tileset.json"
    update_model_status(name, "ready", tileset_url=tileset_url)
    print(f"[Server] Model ready → {tileset_url}")

    return True, tileset_url


# ----------------------------------------------------------------
# Build scene-level tileset after all models processed
# ----------------------------------------------------------------
def build_scene_tileset():
    config     = load_config()
    ready      = [m for m in config["models"] if m.get("status") == "ready"]

    if not ready:
        print(f"[Server] No ready models — skipping scene tileset")
        return

    print(f"\n[Server] Building scene tileset for {len(ready)} models...")

    # Build quadtree from ready models
    root, cells, cell_map = assign_cells(ready)

    # Build cell tilesets
    cell_tileset_paths = {}
    for cell_id, cell in cells.items():
        model_tileset_paths = {}
        for model in cell["models"]:
            name         = model["name"]
            model_dir    = os.path.join(SCENE_DIR, "models", name)
            tileset_path = os.path.join(model_dir, name + "_tileset.json")
            if os.path.isfile(tileset_path):
                model_tileset_paths[name] = tileset_path

        cell_tileset_path = build_cell_tileset(
            cell                 = cell,
            model_tileset_paths  = model_tileset_paths,
            output_dir           = SCENE_DIR
        )
        cell_tileset_paths[cell_id] = cell_tileset_path

    # Calculate overall scene bounds
    lons = [m["lon"] for m in ready]
    lats = [m["lat"] for m in ready]
    scene_bounds = {
        "min_lon": min(lons) - 0.01,
        "max_lon": max(lons) + 0.01,
        "min_lat": min(lats) - 0.01,
        "max_lat": max(lats) + 0.01
    }

    # Build root tileset
    root_path = build_root_tileset(
        cells              = cells,
        cell_tileset_paths = cell_tileset_paths,
        output_dir         = SCENE_DIR,
        scene_bounds       = scene_bounds
    )

    print(f"[Server] Scene tileset → {root_path}")
    print(f"[Server] Load in viewer: "
          f"http://localhost:{PORT}/scene/tileset.json")


# ----------------------------------------------------------------
# Process all pending models then build scene
# ----------------------------------------------------------------
def process_all_pending():
    config  = load_config()
    pending = [m for m in config["models"]
               if m.get("status") in ["pending", "error"]]

    if not pending:
        print(f"[Server] No pending models")
        build_scene_tileset()
        return

    print(f"[Server] Processing {len(pending)} pending model(s)...")
    for model in pending:
        process_model(model)

    build_scene_tileset()


# ----------------------------------------------------------------
# Tilesets list helper
# ----------------------------------------------------------------
def get_all_tilesets():
    config   = load_config()
    tilesets = []
    for model in config.get("models", []):
        if model.get("status") == "ready" and model.get("tileset_url"):
            tilesets.append({
                "name":        model["name"],
                "tileset_url": model["tileset_url"],
                "endpoint":    f"http://localhost:{PORT}/tileset/{model['name']}",
                "created":     model.get("processed_at", "unknown")
            })
    return tilesets


# ----------------------------------------------------------------
# HTTP Handler
# ----------------------------------------------------------------
class Handler(http.server.SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

        if self.path.endswith('.b3dm'):
            self.send_header("Cache-Control", "public, max-age=3600")
        elif self.path.endswith('tileset.json'):
            self.send_header("Cache-Control", "public, max-age=300")
        else:
            self.send_header("Cache-Control", "no-cache")

        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        if self.path == '/favicon.ico':
            self.send_response(204)
            self.end_headers()
            return

        if self.path == '/api/models':
            self.api_list_models()
            return

        if (self.path.startswith('/api/models/') and
                self.path.endswith('/status')):
            name = self.path.replace('/api/models/', '').replace('/status', '')
            self.api_model_status(name)
            return

        if self.path == '/tilesets':
            self._json(200, {
                "count":    len(get_all_tilesets()),
                "tilesets": get_all_tilesets()
            })
            return

        if self.path.startswith('/tileset/'):
            name = self.path.replace('/tileset/', '').strip('/')
            self.legacy_get_tileset(name)
            return

        if self.path == '/status':
            self.legacy_status_page()
            return

        super().do_GET()

    def do_POST(self):
        if self.path == '/api/models':
            self.api_add_model()
        elif (self.path.startswith('/api/models/') and
              self.path.endswith('/process')):
            name = self.path.replace(
                '/api/models/', '').replace('/process', '')
            self.api_process_model(name)
        elif self.path == '/api/process/all':
            self.api_process_all()
        elif self.path == '/api/rebuild/scene':
            self.api_rebuild_scene()
        elif self.path == '/upload':
            self.legacy_upload()
        else:
            self.send_response(404)
            self.end_headers()

    def do_PUT(self):
        if self.path.startswith('/api/models/'):
            name = self.path.replace('/api/models/', '').strip('/')
            self.api_update_model(name)
        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self):
        if self.path.startswith('/api/models/'):
            name = self.path.replace('/api/models/', '').strip('/')
            self.api_delete_model(name)
        else:
            self.send_response(404)
            self.end_headers()

    # ── GET /api/models ──────────────────────────────────────────
    def api_list_models(self):
        config = load_config()
        self._json(200, {
            "count":  len(config["models"]),
            "models": config["models"]
        })

    # ── GET /api/models/{name}/status ────────────────────────────
    def api_model_status(self, name):
        config   = load_config()
        i, model = find_model(config, name)
        if model is None:
            self._json(404, {"error": f"Model not found: {name}"})
            return
        self._json(200, model)

    # ── POST /api/models ─────────────────────────────────────────
    def api_add_model(self):
        body = self._read_body()
        if body is None:
            return

        for field in ["name", "file", "lon", "lat"]:
            if field not in body:
                self._json(400, {"error": f"Missing field: {field}"})
                return

        config    = load_config()
        i, exists = find_model(config, body["name"])
        if exists:
            self._json(409, {"error": f"Model exists: {body['name']}"})
            return

        new_model = {
            "name":         body["name"],
            "file":         body["file"],
            "unit":         body.get("unit", "m"),
            "lon":          float(body["lon"]),
            "lat":          float(body["lat"]),
            "height":       float(body.get("height", 0)),
            "status":       "pending",
            "tileset_url":  None,
            "error":        None,
            "processed_at": None
        }

        config["models"].append(new_model)
        save_config(config)
        print(f"[API] Added model: {body['name']}")
        self._json(201, {"message": f"Added: {body['name']}", "model": new_model})

    # ── PUT /api/models/{name} ────────────────────────────────────
    def api_update_model(self, name):
        body = self._read_body()
        if body is None:
            return

        config   = load_config()
        i, model = find_model(config, name)
        if model is None:
            self._json(404, {"error": f"Model not found: {name}"})
            return

        for field in ["file", "unit", "lon", "lat", "height"]:
            if field in body:
                config["models"][i][field] = body[field]

        if any(f in body for f in ["file", "lon", "lat", "height", "unit"]):
            config["models"][i]["status"]       = "pending"
            config["models"][i]["tileset_url"]  = None
            config["models"][i]["error"]        = None
            config["models"][i]["processed_at"] = None

        save_config(config)
        self._json(200, {
            "message": f"Updated: {name}",
            "model":   config["models"][i]
        })

    # ── DELETE /api/models/{name} ─────────────────────────────────
    def api_delete_model(self, name):
        config   = load_config()
        i, model = find_model(config, name)
        if model is None:
            self._json(404, {"error": f"Model not found: {name}"})
            return

        # Remove generated tiles and LOD files
        for folder in [
            os.path.join(SCENE_DIR, "models", name),
            os.path.join(LOD_DIR, "lod0", name + ".glb"),
            os.path.join(LOD_DIR, "lod1", name + ".glb"),
        ]:
            if os.path.isdir(folder):
                shutil.rmtree(folder)
            elif os.path.isfile(folder):
                os.remove(folder)

        config["models"].pop(i)
        save_config(config)
        print(f"[API] Deleted model: {name}")
        self._json(200, {"message": f"Deleted: {name}"})

    # ── POST /api/models/{name}/process ──────────────────────────
    def api_process_model(self, name):
        config   = load_config()
        i, model = find_model(config, name)
        if model is None:
            self._json(404, {"error": f"Model not found: {name}"})
            return

        def run():
            process_model(model)
            build_scene_tileset()

        threading.Thread(target=run, daemon=True).start()
        self._json(202, {
            "message":    f"Processing: {name}",
            "status_url": f"http://localhost:{PORT}/api/models/{name}/status"
        })

    # ── POST /api/process/all ─────────────────────────────────────
    def api_process_all(self):
        config  = load_config()
        pending = [m for m in config["models"]
                   if m.get("status") in ["pending", "error"]]

        if not pending:
            self._json(200, {"message": "No pending models"})
            return

        def run():
            for model in pending:
                process_model(model)
            build_scene_tileset()

        threading.Thread(target=run, daemon=True).start()
        self._json(202, {
            "message":    f"Processing {len(pending)} model(s)",
            "models":     [m["name"] for m in pending],
            "status_url": f"http://localhost:{PORT}/api/models"
        })

    # ── POST /api/rebuild/scene ───────────────────────────────────
    def api_rebuild_scene(self):
        def run():
            build_scene_tileset()

        threading.Thread(target=run, daemon=True).start()
        self._json(202, {
            "message":    "Rebuilding scene tileset",
            "tileset_url": f"http://localhost:{PORT}/scene/tileset.json"
        })

    # ── Legacy endpoints ──────────────────────────────────────────
    def legacy_get_tileset(self, model_name):
        tileset_path = os.path.join(
            SCENE_DIR, "models", model_name,
            model_name + "_tileset.json"
        )
        if not os.path.isfile(tileset_path):
            self._json(404, {"error": f"Tileset not found: {model_name}"})
            return
        with open(tileset_path) as f:
            tileset = json.load(f)
        self._json(200, tileset)

    def legacy_upload(self):
        content_type = self.headers.get('Content-Type', '')
        if 'multipart/form-data' not in content_type:
            self._json(400, {"error": "Expected multipart/form-data"})
            return

        boundary = content_type.split('boundary=')[-1].encode()
        length   = int(self.headers.get('Content-Length', 0))
        raw      = self.rfile.read(length)
        fields   = self._parse_multipart(raw, boundary)

        obj_data  = fields.get('obj_file')
        obj_fname = fields.get('obj_filename', b'model.obj').decode()
        unit      = fields.get('scale_unit',   b'm').decode()
        lon       = float(fields.get('lon',    b'0').decode())
        lat       = float(fields.get('lat',    b'0').decode())
        height    = float(fields.get('height', b'0').decode())

        if not obj_data:
            self._json(400, {"error": "No file provided"})
            return

        model_name = os.path.splitext(obj_fname)[0]
        obj_path   = os.path.join(UPLOAD_DIR, obj_fname)

        with open(obj_path, 'wb') as f:
            f.write(obj_data)

        config    = load_config()
        i, exists = find_model(config, model_name)

        if exists is None:
            config["models"].append({
                "name": model_name, "file": obj_fname,
                "unit": unit, "lon": lon, "lat": lat,
                "height": height, "status": "pending",
                "tileset_url": None, "error": None, "processed_at": None
            })
        else:
            config["models"][i].update({
                "lon": lon, "lat": lat, "height": height,
                "unit": unit, "status": "pending"
            })
        save_config(config)

        _, m       = find_model(load_config(), model_name)
        ok, result = process_model(m)
        build_scene_tileset()

        if not ok:
            self._json(500, {"error": result})
            return

        _, updated = find_model(load_config(), model_name)
        self._json(200, {
            "tileset_url": updated["tileset_url"],
            "model_name":  model_name,
            "scene_url":   f"http://localhost:{PORT}/scene/tileset.json"
        })

    def legacy_status_page(self):
        config  = load_config()
        models  = config.get("models", [])
        b_ok    = os.path.isfile(BLENDER_PATH)
        t_ok    = os.path.isfile(
                    shutil.which("3d-tiles-tools") or
                    "/opt/homebrew/bin/3d-tiles-tools"
                  )

        rows = ""
        for m in models:
            color = {
                "ready": "#5dcaa5", "pending": "#EF9F27",
                "processing": "#4a9aba", "error": "#e07a7a"
            }.get(m.get("status", "pending"), "#aac8e0")

            link = (f'<a href="{m["tileset_url"]}">{m["name"]}</a>'
                    if m.get("tileset_url") else m["name"])

            rows += f"""<tr>
              <td>{link}</td>
              <td>{m['file']}</td>
              <td style="color:{color}">{m.get('status','pending')}</td>
              <td>{m.get('processed_at') or '—'}</td>
              <td style="color:#e07a7a;font-size:11px">
                {m.get('error') or '—'}
              </td>
            </tr>"""

        if not rows:
            rows = "<tr><td colspan='5'>No models yet</td></tr>"

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>3D Tile Server v2 — Status</title>
  <meta http-equiv="refresh" content="5">
  <style>
    body{{font-family:sans-serif;background:#0d1b2a;color:#aac8e0;padding:40px}}
    h1{{color:white;margin-bottom:4px}}
    h2{{color:#aac8e0;margin:24px 0 12px}}
    table{{width:100%;border-collapse:collapse;margin-bottom:24px}}
    th{{background:#1a2a3a;color:#aac8e0;padding:10px 14px;
        text-align:left;border-bottom:1px solid #2a3a4a}}
    td{{padding:10px 14px;border-bottom:1px solid #1a2a3a;font-size:13px}}
    a{{color:#4a9aba;text-decoration:none}}
    .ok{{color:#5dcaa5}} .err{{color:#e07a7a}}
    .scene-box{{background:#1a2a3a;border-radius:8px;padding:16px;
                margin-bottom:24px;border:1px solid #2a3a4a}}
  </style>
</head>
<body>
  <h1>3D Tile Server v2</h1>
  <p style="color:#6a9ab0;font-size:13px">
    QEM Decimation + Quadtree + Discrete LOD —
    auto-refreshes every 5s
  </p>

  <div class="scene-box">
    <strong style="color:white">Scene tileset (all models)</strong><br>
    <a href="http://localhost:{PORT}/scene/tileset.json">
      http://localhost:{PORT}/scene/tileset.json
    </a>
    <br><br>
    <span style="font-size:12px;color:#6a9ab0">
      POST /api/rebuild/scene to regenerate after model changes
    </span>
  </div>

  <h2>Tool status</h2>
  <table>
    <tr><th>Tool</th><th>Status</th></tr>
    <tr><td>Blender</td>
        <td class="{'ok' if b_ok else 'err'}">
          {'Found' if b_ok else 'NOT FOUND'}
        </td></tr>
    <tr><td>3d-tiles-tools</td>
        <td class="{'ok' if t_ok else 'err'}">
          {'Found' if t_ok else 'NOT FOUND'}
        </td></tr>
  </table>

  <h2>Models ({len(models)})</h2>
  <table>
    <thead>
      <tr>
        <th>Name</th><th>File</th><th>Status</th>
        <th>Processed at</th><th>Error</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>

  <h2>API endpoints</h2>
  <table>
    <tr><th>Method</th><th>Endpoint</th><th>Description</th></tr>
    <tr><td>GET</td>
        <td><a href="/api/models">/api/models</a></td>
        <td>List all models</td></tr>
    <tr><td>POST</td><td>/api/models</td><td>Add model</td></tr>
    <tr><td>PUT</td><td>/api/models/{{name}}</td><td>Update model</td></tr>
    <tr><td>DELETE</td><td>/api/models/{{name}}</td><td>Delete model</td></tr>
    <tr><td>POST</td><td>/api/models/{{name}}/process</td>
        <td>Process one model</td></tr>
    <tr><td>POST</td><td>/api/process/all</td>
        <td>Process all pending</td></tr>
    <tr><td>POST</td><td>/api/rebuild/scene</td>
        <td>Rebuild scene tileset</td></tr>
    <tr><td>GET</td><td>/api/models/{{name}}/status</td>
        <td>Model status</td></tr>
  </table>
</body>
</html>"""

        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── Helpers ───────────────────────────────────────────────────
    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        raw    = self.rfile.read(length)
        try:
            return json.loads(raw)
        except Exception:
            self._json(400, {"error": "Invalid JSON"})
            return None

    def _parse_multipart(self, raw, boundary):
        fields = {}
        parts  = raw.split(b'--' + boundary)
        for part in parts[1:-1]:
            if b'\r\n\r\n' not in part:
                continue
            hdr, _, body = part.partition(b'\r\n\r\n')
            body    = body.rstrip(b'\r\n')
            headers = hdr.decode(errors='replace')
            name = filename = None
            for line in headers.splitlines():
                if 'Content-Disposition' in line:
                    for item in line.split(';'):
                        item = item.strip()
                        if item.startswith('name='):
                            name = item.split('=', 1)[1].strip('"')
                        if item.startswith('filename='):
                            filename = item.split('=', 1)[1].strip('"')
            if name:
                fields[name] = body
                if filename:
                    fields[name + '_filename'] = filename.encode()
        return fields

    def _json(self, code, data):
        body = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        if len(args) > 0 and 'favicon' not in str(args[0]):
            print(f"[Server] {args[0]} {args[1]}")


# ----------------------------------------------------------------
# Startup
# ----------------------------------------------------------------
config = load_config()
PORT   = config.get("port", 8080)
HOST   = config.get("host", "0.0.0.0")

print(f"")
print(f"  3D Tile Server v2")
print(f"  -----------------")
print(f"  Viewer   : http://localhost:{PORT}/index.html")
print(f"  Status   : http://localhost:{PORT}/status")
print(f"  API      : http://localhost:{PORT}/api/models")
print(f"  Scene    : http://localhost:{PORT}/scene/tileset.json")
print(f"  Press Ctrl+C to stop")
print(f"")

threading.Thread(target=process_all_pending, daemon=True).start()

httpd = http.server.HTTPServer((HOST, PORT), Handler)
httpd.serve_forever()