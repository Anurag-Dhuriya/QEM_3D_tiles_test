import os
import shutil

try:
    import pymeshlab
    PYMESHLAB_AVAILABLE = True
except ImportError:
    PYMESHLAB_AVAILABLE = False
    print("[Decimator] WARNING: pymeshlab not installed — decimation disabled")
    print("[Decimator] Install with: pip install pymeshlab")


# LOD target percentages of original face count
LOD_TARGETS = {
    "lod2": 1.00,   # full detail
    "lod1": 0.25,   # 25% of original
    "lod0": 0.05    # 5% of original
}

# Minimum face count — never decimate below this
MIN_FACES = 500


def get_face_count(glb_path):
    if not PYMESHLAB_AVAILABLE:
        return None
    try:
        ms = pymeshlab.MeshSet()
        ms.load_new_mesh(glb_path)
        return ms.current_mesh().face_number()
    except Exception as e:
        print(f"[Decimator] Could not read face count: {e}")
        return None


def decimate(input_glb, output_glb, lod_level):
    if not PYMESHLAB_AVAILABLE:
        print(f"[Decimator] pymeshlab not available — copying {lod_level} as-is")
        shutil.copy2(input_glb, output_glb)
        return True

    if lod_level == "lod2":
        # LOD2 is full detail — just copy
        shutil.copy2(input_glb, output_glb)
        print(f"[Decimator] LOD2 — full detail, copied as-is")
        return True

    try:
        ms = pymeshlab.MeshSet()
        ms.load_new_mesh(input_glb)

        original_faces = ms.current_mesh().face_number()
        target_ratio   = LOD_TARGETS[lod_level]
        target_faces   = max(MIN_FACES, int(original_faces * target_ratio))

        print(f"[Decimator] {lod_level} — {original_faces} → {target_faces} faces "
              f"({target_ratio*100:.0f}%)")

        if target_faces >= original_faces:
            shutil.copy2(input_glb, output_glb)
            print(f"[Decimator] Target >= original, copying as-is")
            return True

        # QEM decimation — Quadric Error Metrics
        ms.meshing_decimation_quadric_edge_collapse(
            targetfacenum    = target_faces,
            qualitythr       = 0.3,
            preserveboundary = True,
            preservenormal   = True,
            preservetopology = True,
            autoclean        = True
        )

        actual_faces = ms.current_mesh().face_number()
        print(f"[Decimator] Result: {actual_faces} faces")

        os.makedirs(os.path.dirname(output_glb), exist_ok=True)
        ms.save_current_mesh(output_glb)
        return True

    except Exception as e:
        print(f"[Decimator] ERROR during decimation: {e}")
        print(f"[Decimator] Falling back to copy for {lod_level}")
        shutil.copy2(input_glb, output_glb)
        return True


def generate_all_lods(full_glb, lod_dir, model_name):
    results = {}
    for lod_level in ["lod2", "lod1", "lod0"]:
        out_dir = os.path.join(lod_dir, lod_level)
        os.makedirs(out_dir, exist_ok=True)
        out_glb = os.path.join(out_dir, model_name + ".glb")
        ok = decimate(full_glb, out_glb, lod_level)
        results[lod_level] = out_glb if ok else None
        print(f"[Decimator] {lod_level} → {out_glb}")
    return results