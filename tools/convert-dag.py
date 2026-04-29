"""Headless Blender script: import a .dag file via dag4blend and export .glb.

Invoked by convert-dag.sh. Not intended for direct use.
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
import traceback
import zipfile
import tempfile

import bpy
import addon_utils


def parse_args() -> argparse.Namespace:
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--addon", required=True, help="path to dag4blend folder")
    return p.parse_args(argv)


def format_blender_version() -> str:
    return ".".join(str(part) for part in bpy.app.version)


def try_load_addon_from_source(addon_src: str):
    """Try to import dag4blend directly from the configured source tree."""
    addon_name = os.path.basename(os.path.normpath(addon_src))
    addon_parent = os.path.dirname(os.path.normpath(addon_src))
    if addon_parent not in sys.path:
        sys.path.insert(0, addon_parent)

    importlib.invalidate_caches()
    sys.modules.pop(addon_name, None)

    module = importlib.import_module(addon_name)
    if hasattr(module, "register"):
        module.register()
    return module


def is_addon_enabled(addon_name: str) -> bool:
    _default_enabled, runtime_enabled = addon_utils.check(addon_name)
    return bool(runtime_enabled)


def ensure_addon(addon_src: str) -> None:
    if not os.path.isdir(addon_src):
        raise SystemExit(f"dag4blend addon folder not found: {addon_src}")

    direct_load_err: Exception | None = None
    try:
        try_load_addon_from_source(addon_src)
        return
    except Exception as exc:
        direct_load_err = exc
        op_path, _op = find_import_operator()
        if op_path is not None:
            print(
                "[convert-dag] dag4blend source load raised an exception but "
                f"registered importer '{op_path}' is available; continuing."
            )
            return

    if "dag4blend" in {m.__name__ for m in addon_utils.modules()}:
        try:
            addon_utils.enable("dag4blend", default_set=True, persistent=True)
            if is_addon_enabled("dag4blend"):
                return
        except Exception as exc:
            raise SystemExit(
                "Failed to enable dag4blend in Blender "
                f"{format_blender_version()}. Direct source load failed with: "
                f"{direct_load_err}. Installed addon enable failed with: {exc}"
            ) from exc

        raise SystemExit(
            "Failed to load dag4blend during preflight in Blender "
            f"{format_blender_version()}. Direct source load failed with: "
            f"{direct_load_err}. Blender discovered the installed addon but did "
            "not enable it successfully. This usually means the configured "
            "dag4blend build is not compatible with the configured Blender version."
        )

    # Pack to a zip and install via Blender's addon installer.
    with tempfile.TemporaryDirectory() as td:
        zip_path = os.path.join(td, "dag4blend.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(addon_src):
                for f in files:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, os.path.dirname(addon_src))
                    zf.write(full, rel)
        bpy.ops.preferences.addon_install(filepath=zip_path, overwrite=True)

    try:
        addon_utils.enable("dag4blend", default_set=True, persistent=True)
        if is_addon_enabled("dag4blend"):
            return
    except Exception as exc:
        raise SystemExit(
            "Failed to load dag4blend during preflight in Blender "
            f"{format_blender_version()}. Direct source load failed with: "
            f"{direct_load_err}. Installed addon enable failed with: {exc}. "
            "This usually means the configured dag4blend build is not compatible "
            "with the configured Blender version."
        ) from exc

    raise SystemExit(
        "Failed to load dag4blend during preflight in Blender "
        f"{format_blender_version()}. Direct source load failed with: "
        f"{direct_load_err}. Blender installed the addon package but did not "
        "activate it successfully. This usually means the configured dag4blend "
        "build is not compatible with the configured Blender version."
    )


def clear_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if len(bpy.data.worlds) == 0:
        world = bpy.data.worlds.new("World")
        if bpy.context.scene is not None:
            bpy.context.scene.world = world


def importer_candidates() -> list[str]:
    # dag4blend exposes its importer as bpy.ops.import_scene.dag (operator id may vary by version).
    return [
        "import_scene.dag",
        "dag.import_dag",
        "import_dag.import_dag",
    ]


def find_import_operator() -> tuple[str, object] | tuple[None, None]:
    for op_path in importer_candidates():
        try:
            mod, name = op_path.split(".")
            op = getattr(getattr(bpy.ops, mod), name)
            str(op)
            return op_path, op
        except Exception:
            continue
    return None, None


def validate_addon_ready() -> None:
    op_path, _op = find_import_operator()
    if op_path is not None:
        return

    raise SystemExit(
        "dag4blend preflight failed in Blender "
        f"{format_blender_version()}: the addon loaded but did not register any "
        f"known import operator ({', '.join(importer_candidates())}). "
        "This usually means the configured dag4blend build is incompatible with "
        "the configured Blender version."
    )


def import_dag_direct(path: str) -> None:
    cmp_module = importlib.import_module("dag4blend.cmp.composite_functions")
    importer_module = importlib.import_module("dag4blend.importer.importer")

    cmp_module.upd_scenes()

    class DirectDagImporter:
        pass

    importer = DirectDagImporter()
    importer.reader = importer_module.DagImporter.reader

    for name, value in importer_module.DagImporter.__dict__.items():
        if callable(value):
            setattr(importer, name, value.__get__(importer, DirectDagImporter))

    importer.filepath = path
    importer.includes_re = ""
    importer.excludes_re = ""
    importer.dirpath = ""
    importer.check_subdirs = False
    importer.includes = ""
    importer.excludes = ""
    importer.with_lods = False
    importer.with_dps = False
    importer.with_dmgs = False
    importer.with_destr = False
    importer.mopt = True
    importer.fix_mat_ids = True
    importer.remove_degenerates = True
    importer.remove_loose = True
    importer.replace_existing = False
    importer.preserve_path = False
    importer.preserve_sg = False
    importer.load(path)


def import_dag(path: str) -> None:
    errors: list[str] = []
    for op_path in importer_candidates():
        try:
            mod, name = op_path.split(".")
            op = getattr(getattr(bpy.ops, mod), name)
            op(filepath=path)
            return
        except Exception as e:  # pragma: no cover - depends on addon version
            errors.append(f"{op_path}: {e}")
            continue
    try:
        import_dag_direct(path)
        return
    except Exception as exc:
        errors.append(
            "direct-class-import: "
            f"{exc}\n{traceback.format_exc()}"
        )
    raise SystemExit(
        "Could not invoke dag4blend importer. "
        f"Tried {importer_candidates()}. Errors: {' | '.join(errors)}"
    )


def export_glb(path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    bpy.ops.export_scene.gltf(
        filepath=path,
        export_format="GLB",
        export_apply=True,
        export_yup=True,
        export_image_format="AUTO",
    )


def import_obj(path: str) -> None:
    """Import OBJ file using bmesh and Blender mesh APIs."""
    import bmesh
    
    try:
        # Create a new mesh
        mesh = bpy.data.meshes.new("ImportedMesh")
        
        # Load OBJ via bmesh
        bm = bmesh.new()
        
        # Try using bpy.ops first if available
        try:
            bpy.ops.import_scene.obj(filepath=path)
            return
        except (RuntimeError, TypeError, AttributeError):
            pass
        
        # Fallback: manual OBJ parser using bmesh
        with open(path, 'r') as f:
            vertices = []
            faces = []
            
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                
                parts = line.split()
                if not parts:
                    continue
                
                cmd = parts[0]
                
                # Parse vertex positions
                if cmd == 'v' and len(parts) >= 4:
                    try:
                        vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
                    except (ValueError, IndexError):
                        pass
                
                # Parse faces
                elif cmd == 'f' and len(parts) >= 4:
                    face_indices = []
                    for i in range(1, len(parts)):
                        try:
                            # Handle "v", "v/vt", "v/vt/vn", or "v//vn" formats
                            vertex_data = parts[i].split('/')
                            v_idx = int(vertex_data[0]) - 1  # OBJ uses 1-based indexing
                            if v_idx < len(vertices):
                                face_indices.append(v_idx)
                        except (ValueError, IndexError):
                            pass
                    
                    if len(face_indices) >= 3:
                        # Add vertices to bmesh
                        bmesh_verts = [bm.verts.new(vertices[vi]) for vi in face_indices[:3]]
                        try:
                            bm.faces.new(bmesh_verts)
                        except ValueError:
                            pass  # Degenerate face
        
        # Ensure lookup table is current
        bm.to_mesh(mesh)
        bm.free()
        
        # Create object and link to scene
        obj = bpy.data.objects.new("ImportedObject", mesh)
        bpy.context.collection.objects.link(obj)
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        
    except Exception as e:
        raise SystemExit(
            f"Failed to import OBJ file: {path}\n"
            f"Error: {e}"
        )


def main() -> None:
    args = parse_args()
    input_lower = args.input.lower()
    
    if input_lower.endswith(".dag"):
        clear_scene()
        ensure_addon(args.addon)
        validate_addon_ready()
        import_dag(args.input)
    elif input_lower.endswith(".obj"):
        clear_scene()
        import_obj(args.input)
    else:
        raise SystemExit(
            f"Unsupported input format: {args.input}. "
            "Supported formats: .dag (requires dag4blend), .obj (Wavefront OBJ)"
        )
    
    export_glb(args.output)
    print(f"[convert-dag] wrote {args.output}")


if __name__ == "__main__":
    main()
