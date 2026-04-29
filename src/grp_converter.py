#!/usr/bin/env python3
"""GRP Converter: extracted GRP resources -> Wavefront OBJ.

This version is intentionally strict: it rejects invalid geometry instead of
emitting corrupted OBJ files.

Module structure:
  oodle.py                  — OodleDecompressor (ctypes wrapper for oo2core DLL)
  decoders/mesh_utils.py    — MeshBuilderMixin (geometry helpers, _build_*)
  decoders/collision.py     — CollisionDecoderMixin (ACE50000, cls boxes, ...)
  decoders/skeleton.py      — SkeletonDecoderMixin (GeomNodeTree, phobj)
  decoders/vertex_stream.py — VertexStreamMixin (fp16, infer_faces, b4 legacy)
  decoders/rendinst.py      — RendInstDecoderMixin (77F8232F, meshopt)
  decoders/dynmodel.py      — DynModelDecoderMixin (B4B7D9C4 DynModel)
  grp_converter.py          — GRPResourceParser (orchestrator: parse_file/directory)
"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    import zstandard as zstd
except Exception:
    zstd = None

from dagor_resources import (
    decode_ace50000_payload,
    looks_like_dag_editor_file,
    parse_ace50000_header,
    parse_rendinst_preamble,
)
from file_specific_decoders import NullFileSpecificDecoder
from oodle import OodleDecompressor
from decoders.mesh_utils import MeshBuilderMixin
from decoders.collision import CollisionDecoderMixin
from decoders.skeleton import SkeletonDecoderMixin
from decoders.vertex_stream import VertexStreamMixin
from decoders.rendinst import RendInstDecoderMixin
from decoders.dynmodel import DynModelDecoderMixin


Mesh = Dict[str, object]


class GRPResourceParser(
    CollisionDecoderMixin,
    SkeletonDecoderMixin,
    VertexStreamMixin,
    RendInstDecoderMixin,
    DynModelDecoderMixin,
    MeshBuilderMixin,
):
    """Parse extracted GRP resources and extract plausible mesh data."""

    MAX_ABS_COORD = 500000.0

    def __init__(
        self,
        verbose: bool,
        oodle_dll: Optional[str],
        global_scale: float = 1.0,
        auto_scale_fp16: bool = True,
        comparison_mode: bool = False,
        include_mesh_types: Optional[Set[str]] = None,
        include_lods: Optional[Set[int]] = None,
    ) -> None:
        self.verbose = verbose
        self.oodle_dll = oodle_dll
        self.global_scale = global_scale
        self.auto_scale_fp16 = auto_scale_fp16
        self.comparison_mode = comparison_mode
        self.include_mesh_types: Set[str] = (
            set(include_mesh_types) if include_mesh_types is not None else {"dynmodel"}
        )
        self.include_lods: Optional[Set[int]] = (
            set(include_lods) if include_lods is not None else {0}
        )
        self.oodle = OodleDecompressor(oodle_dll, verbose)
        self.rejections: List[str] = []
        self._current_file: Optional[Path] = None
        # Cache of multi-LOD rendInst meshes decoded by parse_file so
        # parse_directory can emit one Mesh per LOD.
        self._rendinst_lod_cache: Dict[str, List[Mesh]] = {}
        # Cache of per-bone meshes decoded from a Dagor GeomNodeTree
        # (skeleton, class id 56F81B6D). Populated by the skeleton decoder
        # and expanded by parse_directory so each bone becomes its own
        # ``o BONE_NAME`` object in the OBJ output.
        self._skeleton_bone_cache: Dict[str, List[Mesh]] = {}
        # Cache of per-(LOD, rigid) meshes decoded from a Dagor DynModel
        # (B4B7D9C4 — DynamicRenderableSceneLodsResource). Populated by
        # ``_decode_b4b7d9c4_dynmodel`` and expanded by parse_directory.
        self._dynmodel_lod_cache: Dict[str, List[Mesh]] = {}
        # Cache of per-skeleton bone world transforms (mat44f cols 0..3
        # x first 3 floats), keyed by skeleton stem and bone name.
        # Populated when a 56F81B6D resource is decoded; consumed by
        # ``_decode_b4b7d9c4_dynmodel`` so each rigid mesh is placed in
        # its node-world space (otherwise every rigid sits at the local
        # origin, e.g. all wheels stacked on top of each other).
        self._skeleton_wtm_cache: Dict[str, Dict[str, Tuple[float, ...]]] = {}
        # File-specific (filename-keyed) heuristics have been removed.
        # All decoding follows a single structural pipeline; the helper is
        # pinned to the no-op stand-in so legacy call sites remain inert.
        self._file_specific = NullFileSpecificDecoder(self)
        self._log(
            f"file-specific decoder: {type(self._file_specific).__name__}"
            f" (comparison_mode={comparison_mode})"
        )

    @staticmethod
    def _mesh_type_of(mesh: Mesh) -> str:
        low = str(mesh.get("filename", "")).lower()
        if low.endswith(".77f8232f"):
            return "rendinst"
        if low.endswith(".b4b7d9c4"):
            return "dynmodel"
        if low.endswith(".56f81b6d"):
            return "skeleton"
        if low.endswith(".ace50000"):
            return "collision"
        if low.endswith(".d543e771"):
            return "phobj"
        if low.endswith(".4e1d5f5e") or low.endswith(".40c586f9"):
            return "generic"
        return "unknown"

    @staticmethod
    def _mesh_lod_of(mesh: Mesh) -> Optional[int]:
        # DynModel path stores explicit metadata.
        dyn_lod = mesh.get("_dynmodel_lod_index")
        if isinstance(dyn_lod, int):
            return dyn_lod
        # Fallback: parse `_lodN` token from object name or filename stem.
        obj_name = str(mesh.get("obj_object_name", ""))
        m = re.search(r"_lod(\d+)(?:_|$)", obj_name)
        if m:
            return int(m.group(1))
        stem = str(mesh.get("filename", "")).rsplit(".", 1)[0].lower()
        m = re.search(r"_lod(\d+)$", stem)
        if m:
            return int(m.group(1))
        return None

    def _filter_meshes(self, meshes: List[Mesh]) -> List[Mesh]:
        mesh_types = self.include_mesh_types
        lods = self.include_lods
        if "all" in mesh_types and lods is None:
            return meshes
        out: List[Mesh] = []
        for m in meshes:
            mt = self._mesh_type_of(m)
            if "all" not in mesh_types and mt not in mesh_types:
                continue
            if lods is not None and mt in ("rendinst", "dynmodel"):
                lod = self._mesh_lod_of(m)
                # If no explicit LOD token exists, treat it as LOD0.
                if lod is None:
                    lod = 0
                if lod not in lods:
                    continue
            out.append(m)

        # If dynmodel was requested but nothing matched, fall back to rendinst.
        # This keeps default extraction resilient for assets lacking dynmodel
        # resources while preserving explicit non-dynmodel choices.
        if not out and "dynmodel" in mesh_types and "rendinst" not in mesh_types:
            fallback: List[Mesh] = []
            for m in meshes:
                if self._mesh_type_of(m) != "rendinst":
                    continue
                if lods is not None:
                    lod = self._mesh_lod_of(m)
                    if lod is None:
                        lod = 0
                    if lod not in lods:
                        continue
                fallback.append(m)
            if fallback and self.verbose:
                self._log(
                    f"DynModel filter empty; falling back to rendinst ({len(fallback)} meshes)"
                )
            return fallback
        return out

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[GRP] {msg}")

    def _zstd_decompress(self, payload: bytes) -> Optional[bytes]:
        if zstd is None:
            if self.verbose:
                self._log("zstd block found but zstandard module is not installed")
            return None
        try:
            if hasattr(zstd, "decompress"):
                return zstd.decompress(payload)
            return zstd.ZstdDecompressor().decompress(payload)
        except Exception:
            return None

    @staticmethod
    def _class_id_from_name(filename: str) -> str:
        dot = filename.rfind(".")
        if dot < 0:
            return ""
        return filename[dot + 1:].lower()

    def parse_file(self, filepath: Path) -> Optional[Mesh]:
        try:
            data = filepath.read_bytes()
        except Exception as exc:
            self.rejections.append(f"{filepath.name}: read failed ({exc})")
            return None

        self._current_file = filepath
        class_id = self._class_id_from_name(filepath.name)
        mesh: Optional[Mesh] = None

        stem = filepath.name.rsplit(".", 1)[0].lower()
        skip_reason = self._file_specific.pre_parse_filter_reason(class_id, stem)
        if skip_reason is not None:
            self.rejections.append(f"{filepath.name}: {skip_reason}")
            return None

        if class_id == "77f8232f":
            ri_meshes = self._decode_77f8232f_rendinst(data, filepath.name)
            if ri_meshes:
                # Stash the full list so parse_directory can emit one Mesh
                # per LOD instead of just LOD0.
                self._rendinst_lod_cache[filepath.name.lower()] = ri_meshes
                mesh = ri_meshes[0]
            else:
                mesh = self._decode_b4b7d9c4(data, filepath.name)
        elif class_id == "b4b7d9c4":
            dyn_meshes = self._decode_b4b7d9c4_dynmodel(data, filepath.name)
            if dyn_meshes:
                self._dynmodel_lod_cache[filepath.name.lower()] = dyn_meshes
                mesh = dyn_meshes[0]
            else:
                mesh = self._decode_b4b7d9c4(data, filepath.name)
        elif class_id == "56f81b6d":
            # Try the proper Dagor GeomNodeTree decoder first; if that
            # detects the format, stash the per-bone meshes and return
            # the first one as a placeholder so parse_directory expands
            # the rest. Fall back to the legacy heuristic decoder for
            # placeholder/tiny skeletons that don't match the
            # GeomNodeTree binary layout.
            bone_meshes = self._decode_geom_node_tree(data, filepath.name)
            if bone_meshes:
                self._skeleton_bone_cache[filepath.name.lower()] = bone_meshes
                mesh = bone_meshes[0]
            else:
                mesh = self._decode_tiny_skeleton_marker(data, filepath.name)
        elif class_id == "ace50000":
            mesh = self._decode_ace50000_collision_mesh(data, filepath.name)
        elif class_id == "d543e771":
            mesh = self._decode_phobj_cbox_wire(data, filepath.name)
        elif class_id in ("4e1d5f5e", "40c586f9"):
            mesh = self._decode_generic(data, filepath.name)

        if mesh:
            mesh = self._normalize_oversized_collision_mesh(filepath.name, mesh)
            self._log(
                f"Parsed {filepath.name}: {mesh['vertex_count']} verts, {mesh['face_count']} faces"
            )
            return mesh

        if self.rejections and self.rejections[-1].startswith(f"{filepath.name}:"):
            return None

        self.rejections.append(f"{filepath.name}: no valid mesh decode")
        return None

    @staticmethod
    def _mesh_base_name(filename: str) -> str:
        """Strip class-id extension and any ``_lodN`` / ``_destr`` suffix."""
        stem = filename.rsplit(".", 1)[0].lower()
        m = re.match(r"^(.*)_lod\d+$", stem)
        if m:
            stem = m.group(1)
        if stem.endswith("_destr"):
            stem = stem[: -len("_destr")]
        return stem

    def _substitute_collision_for_nonflat(self, meshes: List[Mesh]) -> None:
        """Replace geometry of non-flat rendInst/destr meshes with the
        sibling ``*_collision.ACE50000`` mesh's geometry, in-place.

        Heuristic: we currently approximate rendInst index buffers with a
        2D Delaunay in the XZ plane. That works for flat ground assets
        (lakes, water decals) but is meaningless for vertical structures
        like ``p39a_post`` or ``p39a_railing``. When such a mesh has
        ``y_extent / max(x_extent, z_extent) > 0.2`` AND a collision
        sibling exists in this directory, we copy the collision mesh's
        vertices/faces/lines into the rendInst/destr mesh while keeping
        its filename.
        """
        if not meshes:
            return

        # Build an index of available collision meshes by base stem.
        collision_by_base: Dict[str, Mesh] = {}
        for m in meshes:
            fname = str(m.get("filename", ""))
            low = fname.lower()
            if not low.endswith(".ace50000"):
                continue
            stem = low.rsplit(".", 1)[0]
            if stem.endswith("_collision"):
                base = stem[: -len("_collision")]
                if base.endswith("_destr"):
                    base = base[: -len("_destr")]
                # Prefer the base (non-_destr) collision when both exist.
                collision_by_base.setdefault(base, m)

        if not collision_by_base:
            return

        for m in meshes:
            fname = str(m.get("filename", ""))
            low = fname.lower()
            if not (low.endswith(".77f8232f") or low.endswith(".b4b7d9c4")):
                continue
            verts = m.get("vertices") or []
            if not verts:
                continue
            faces = m.get("faces") or []
            # Heuristic: our XZ-Delaunay triangulation is meaningful only
            # when the projection captures the shape well. When the
            # resulting triangle count is much smaller than the vertex
            # count, the projection collapsed (vertical structure such as
            # railings/posts) — fall back to the collision sibling.
            sparse_faces = len(faces) < max(4, len(verts) * 0.7)
            if not sparse_faces:
                continue

            base = self._mesh_base_name(fname)
            coll = collision_by_base.get(base)
            if coll is None:
                continue

            new_verts = list(coll.get("vertices") or [])
            new_faces = list(coll.get("faces") or [])
            new_lines = list(coll.get("lines") or [])
            # Skip placeholder gyro collisions (lines-only, no faces) —
            # they are visualisation aids, not real geometry, e.g. for
            # bushes whose collision is just an icon billboard.
            if not new_faces:
                continue
            if not new_verts:
                continue

            self._log(
                f"Substituting collision geometry into {fname} "
                f"(from {coll.get('filename')})"
            )
            m["vertices"] = new_verts
            m["faces"] = new_faces
            m["lines"] = new_lines
            m["vertex_count"] = len(new_verts)
            m["face_count"] = len(new_faces)
            m["line_count"] = len(new_lines)
            m["normals"] = None
            m["_collision_substituted_from"] = coll.get("filename")

    def parse_directory(self, dirpath: Path) -> List[Mesh]:
        meshes: List[Mesh] = []
        if not dirpath.exists():
            raise FileNotFoundError(f"Directory not found: {dirpath}")

        files: List[Path] = []
        for p in sorted(dirpath.glob("*")):
            if not p.is_file():
                continue
            class_id = self._class_id_from_name(p.name)
            if len(class_id) == 8 and all(ch in "0123456789abcdef" for ch in class_id):
                files.append(p)
        self._log(f"Found {len(files)} resources")
        # Pre-pass: decode skeletons (56F81B6D) first so the world-tm
        # cache is populated before sibling DynModel resources read it.
        # The full bone Mesh list is stashed so the regular pass below
        # picks it up via ``_skeleton_bone_cache``.
        for fp in files:
            if not fp.name.lower().endswith(".56f81b6d"):
                continue
            try:
                sk_data = fp.read_bytes()
            except Exception:
                continue
            bone_meshes = self._decode_geom_node_tree(sk_data, fp.name)
            if bone_meshes:
                self._skeleton_bone_cache[fp.name.lower()] = bone_meshes
        # Index by lowercase name so the destr-phobj duplication step below
        # can detect missing `_destr_collision` resources cheaply.
        existing_names = {fp.name.lower() for fp in files}
        for fp in files:
            mesh = self.parse_file(fp)
            if mesh:
                # If this is an ACE50000 collision blob carrying multiple
                # named ``*_cls`` sections, replace the concatenated mesh
                # with one mesh per named sub-part.
                if fp.name.lower().endswith(".ace50000"):
                    try:
                        raw_bytes = fp.read_bytes()
                    except Exception:
                        raw_bytes = b""
                    if raw_bytes:
                        sub = self._try_split_ace50000_named_cls(raw_bytes, fp.name)
                        if sub:
                            self._log(
                                f"Splitting {fp.name} into {len(sub)} named cls sub-meshes"
                            )
                            meshes.extend(sub)
                            continue
                # Multi-LOD rendInst: emit one Mesh per LOD instead of just LOD0.
                if fp.name.lower().endswith(".77f8232f"):
                    ri_subs = self._rendinst_lod_cache.pop(fp.name.lower(), None)
                    if ri_subs and len(ri_subs) >= 2:
                        self._log(
                            f"Splitting {fp.name} into {len(ri_subs)} LOD meshes"
                        )
                        meshes.extend(ri_subs)
                        continue
                # Skeleton (GeomNodeTree): emit one named ``o BONE_NAME``
                # mesh per bone instead of a single combined wireframe.
                if fp.name.lower().endswith(".56f81b6d"):
                    bone_subs = self._skeleton_bone_cache.pop(fp.name.lower(), None)
                    if bone_subs and len(bone_subs) >= 2:
                        self._log(
                            f"Splitting {fp.name} into {len(bone_subs)} named bone meshes"
                        )
                        meshes.extend(bone_subs)
                        continue
                # DynModel (B4B7D9C4): emit one named mesh per
                # (LOD x rigid node) instead of just the first rigid.
                if fp.name.lower().endswith(".b4b7d9c4"):
                    dyn_subs = self._dynmodel_lod_cache.pop(fp.name.lower(), None)
                    if dyn_subs and len(dyn_subs) >= 2:
                        self._log(
                            f"Splitting {fp.name} into {len(dyn_subs)} DynModel rigid meshes"
                        )
                        meshes.extend(dyn_subs)
                        continue
                meshes.append(mesh)
                # Some assets (e.g. lamp_post_metal_350) ship a multi-box
                # `_destr_phobj` but no separate `_destr_collision.ACE50000`.
                # When the phobj decoded into a multi-box wireframe (>=3
                # boxes / >=24 verts), emit a renamed clone as
                # `<stem>_destr_collision` so downstream consumers see the
                # collision shape under its expected name.
                if (
                    fp.name.lower().endswith("_destr_phobj.d543e771")
                    and mesh.get("vertex_count", 0) >= 24
                    and mesh.get("line_count", 0) >= 36
                    and mesh.get("_phobj_body_count", 0) >= 2
                ):
                    stem = fp.stem
                    if stem.lower().endswith("_destr_phobj"):
                        base = stem[: -len("_destr_phobj")]
                        coll_stem = base + "_destr_collision"
                        ace_name = f"{coll_stem}.ACE50000".lower()
                        base_ace = f"{base}_collision.ACE50000".lower()
                        if (
                            ace_name not in existing_names
                            and base_ace not in existing_names
                        ):
                            clone = dict(mesh)
                            clone["filename"] = f"{coll_stem}.ACE50000"
                            meshes.append(clone)
                            self._log(
                                f"Emitting derived collision mesh {coll_stem} from {fp.name}"
                            )
        # Post-pass: for non-flat rendInst (77F8232F) and destr (B4B7D9C4)
        # meshes whose triangulation is approximate (we have not yet
        # decoded the index codec), substitute the geometry from a sibling
        # ``*_collision.ACE50000`` mesh when one is available.
        self._substitute_collision_for_nonflat(meshes)
        filtered = self._filter_meshes(meshes)
        if self.verbose:
            self._log(f"Filtering retained {len(filtered)}/{len(meshes)} meshes")
        return filtered
