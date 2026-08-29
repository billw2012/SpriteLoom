"""
spriteloom_addon.py — SpriteLoom Blender Addon

Install via: Edit > Preferences > Add-ons > Install... > select this file

Adds a "SpriteLoom" tab in the 3D Viewport N-panel with a
"Render All" button that runs the full render pipeline.
"""

import math
import os
import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# CONFIGURATION — edit these values before rendering
# ---------------------------------------------------------------------------

# All 16 directions in order — subsets are taken by stepping through this list
_ALL_DIRECTIONS = [
    ("south",     math.pi),
    ("southwest", 5 * math.pi / 4),
    ("west",      3 * math.pi / 2),
    ("northwest", 7 * math.pi / 4),
    ("north",     0),
    ("northeast", math.pi / 4),
    ("east",      math.pi / 2),
    ("southeast", 3 * math.pi / 4),
    ("ssw",       9 * math.pi / 8),
    ("wsw",       11 * math.pi / 8),
    ("wnw",       13 * math.pi / 8),
    ("nnw",       15 * math.pi / 8),
    ("nne",       math.pi / 8),
    ("ene",       3 * math.pi / 8),
    ("ese",       5 * math.pi / 8),
    ("sse",       7 * math.pi / 8),
]

_DIRECTION_COUNTS = {
    "1":  [("south", math.pi)],
    "4":  [("south", math.pi), ("west", 3*math.pi/2), ("north", 0), ("east", math.pi/2)],
    "8":  [("south", math.pi), ("southwest", 5*math.pi/4), ("west", 3*math.pi/2),
           ("northwest", 7*math.pi/4), ("north", 0), ("northeast", math.pi/4),
           ("east", math.pi/2), ("southeast", 3*math.pi/4)],
    "16": _ALL_DIRECTIONS,
}


def _get_directions(num_directions_str):
    return _DIRECTION_COUNTS.get(num_directions_str, _DIRECTION_COUNTS["8"])


def _get_direction_label(angle_radians):
    """Return the name of the closest known direction for a given angle in radians."""
    tau = 2 * math.pi
    angle = angle_radians % tau
    best_name, best_dist = None, tau
    for name, candidate in _ALL_DIRECTIONS:
        dist = min(abs(angle - candidate % tau), tau - abs(angle - candidate % tau))
        if dist < best_dist:
            best_dist, best_name = dist, name
    threshold = math.pi / 32  # within ~5.6 degrees = exact match
    prefix = "" if best_dist < threshold else "~"
    return f"{prefix}{best_name}"

FRAME_WIDTH = 64
FRAME_HEIGHT = 64
FRAME_DURATION_MS = 100
FRAME_DURATION_OVERRIDES = {}  # e.g. {"chr_run": 80, "chr_idle": 150}

# ---------------------------------------------------------------------------
# END CONFIGURATION
# ---------------------------------------------------------------------------

import bpy


def _log(msg):
    print(f"[SpriteLoom] {msg}", flush=True)


# Sentinel stored in actions_include / compositors_include to mean "nothing selected".
# Empty string means "all selected" (default). This sentinel disambiguates the two.
_FILTER_NONE = "__none__"


def _parse_include(filter_str):
    """Parse an opt-in filter string.
    Returns None  → all items included (empty string default).
    Returns set() → nothing included (sentinel).
    Returns set   → only the named items are included.
    """
    s = filter_str.strip()
    if s == _FILTER_NONE:
        return set()
    if not s:
        return None
    return {n.strip() for n in s.split(",") if n.strip()}


# ---------------------------------------------------------------------------
# Run status — structured, machine-readable, and deliberately not persisted
# ---------------------------------------------------------------------------

# The status lives in a MODULE GLOBAL, not a scene property, and that is the whole
# point. `last_result` is a scene property, so it is saved into the .blend and
# inherited by every scene built from that template — a fresh scene opens already
# reporting a completed run that never happened, and there is no way for a caller
# to tell that string apart from a real one. Module state is scoped to the running
# Blender session instead: empty on load, impossible to inherit, cleared at the
# start of every run. `last_result` is still written exactly as before for anything
# already parsing it; this is the machine-readable channel beside it.
STATUS_FILENAME = "spriteloom_status.json"

_STATUS = {}
_LAST_PLAN = {}


def _utc_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _status_blank():
    return {
        "running": False,
        "finished": False,
        "cancelled": False,
        "kind": None,
        "started_utc": None,
        "ended_utc": None,
        "rendered": 0,
        "skipped": 0,
        "errors": 0,
        "total": 0,
        "progress": 0.0,
        "sheets": {"beauty": 0, "normal": 0, "depth": 0},
        "per_action": {},
        "outputs": {},
        "messages": [],
    }


def _status_start(kind, total=0, per_action=None, messages=None):
    """Begin a run. Clears every field — `finished` is false from here until _status_end."""
    global _STATUS
    _STATUS = _status_blank()
    _STATUS.update({
        "running": True,
        "finished": False,
        "kind": kind,
        "started_utc": _utc_now(),
        "total": total,
        "per_action": per_action or {},
        "messages": list(messages or []),
    })
    return _STATUS


def _status_note(msg):
    if _STATUS:
        _STATUS.setdefault("messages", []).append(msg)
    _log(msg)


def _status_progress(rendered=None, errors=None, skipped=None):
    if not _STATUS:
        return
    if rendered is not None:
        _STATUS["rendered"] = rendered
    if errors is not None:
        _STATUS["errors"] = errors
    if skipped is not None:
        _STATUS["skipped"] = skipped
    total = _STATUS.get("total") or 0
    _STATUS["progress"] = (_STATUS["rendered"] / total) if total else 0.0


def _status_end(cancelled=False, sheets=None, outputs=None, status_dir=None):
    """Mark the run complete and flush the payload to disk."""
    if not _STATUS:
        return None
    _STATUS["running"] = False
    _STATUS["finished"] = True
    _STATUS["cancelled"] = bool(cancelled)
    _STATUS["ended_utc"] = _utc_now()
    if sheets:
        _STATUS["sheets"].update(sheets)
    if outputs:
        _STATUS["outputs"].update(outputs)
    if status_dir:
        _write_status_file(status_dir, _STATUS)
    return _STATUS


def _write_status_file(status_dir, payload):
    """Write the status payload beside the sheets.

    This exists so a caller that cannot poll Python — a shell loop, a headless
    harness — has one authoritative file to watch instead of guessing from the
    mtimes of the sheet PNGs. Written last, and written whole, so its existence
    with finished=true is a real completion signal.
    """
    import json
    try:
        os.makedirs(status_dir, exist_ok=True)
        path = os.path.join(status_dir, STATUS_FILENAME)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)  # atomic: a watcher never sees a half-written file
        return path
    except Exception as exc:  # never let status reporting break a finished render
        _log(f"  WARNING: could not write status file: {exc}")
        return None


def get_status():
    """Return a copy of the current/last run status as a plain dict.

    `finished` is False until a run has actually completed in THIS session — it is
    never inherited from a saved .blend or a template scene. On a fresh load with no
    run yet, returns the blank payload (running=False, finished=False, kind=None).
    """
    import copy
    return copy.deepcopy(_STATUS) if _STATUS else _status_blank()


# ---------------------------------------------------------------------------
# Named sockets — per-frame attachment points projected into frame coordinates
# ---------------------------------------------------------------------------

# Sidecar written next to each rendered frame, read back by the sheet packer.
_SOCKET_SIDECAR_EXT = ".sockets"

# Socket names become identifiers downstream, so keep them to a conservative set.
_SOCKET_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _sanitize_socket_name(raw):
    """Normalise a user-typed socket name to a downstream-safe identifier.

    Returns "" for a name that is empty or reduces to nothing — callers must treat
    that as "not authored" rather than substituting a default.
    """
    return _SOCKET_NAME_RE.sub("_", (raw or "").strip())


def _resolve_sockets(settings):
    """Validate the socket list. Returns (sockets, issues).

    sockets: list of (sanitized_name, object) for entries that are fully authored —
             a non-empty unique name AND an assigned object. Everything else is left
             out so that an unauthored socket is *absent* downstream, never zero.
    issues:  list of (severity, message). An empty or duplicated name is an ERROR — the
             name is an identifier downstream, and silently dropping one of a colliding
             pair is worse than refusing to render. A named socket with no object assigned
             is only INFO: that is the documented "omit the key" case, not a mistake.
             Duplicates drop *all* colliding entries rather than letting the last one win.
    """
    resolved = []
    issues = []
    seen = {}
    for i, item in enumerate(getattr(settings, "sockets", [])):
        name = _sanitize_socket_name(item.name)
        if not name:
            issues.append(("ERROR", f"Socket {i + 1} has an empty name"))
            continue
        if name in seen:
            seen[name].append(i)
            continue
        seen[name] = [i]
        if item.object is None:
            issues.append(("INFO", f"Socket '{name}' has no object — it will be omitted from the JSON"))
            continue
        resolved.append((name, item.object))

    dupes = {n for n, idxs in seen.items() if len(idxs) > 1}
    for name in sorted(dupes):
        issues.append(("ERROR", f"Socket name '{name}' is used more than once"))
    resolved = [(n, o) for n, o in resolved if n not in dupes]
    return resolved, issues


def _project_sockets(context, scene):
    """Project every authored socket into normalised frame coordinates for the current frame.

    Returns {name: {"x": float, "y": float, "z": float}} — possibly empty. Sockets whose
    projection fails or yields a non-finite value are omitted, never written as zero.

    Coordinate convention matches `pivot` exactly: normalised camera view, origin at the
    top-left of the frame (Y flipped via 1.0 - y), rounded to 6 dp. `z` is the extra
    datum pivot discards — distance along the camera axis in Blender units, for compositing
    the attached sprite against a baked depth map.

    Parenting: the socket's *world* transform is projected as-is. Parent socket objects in
    the same frame as the geometry they belong to (a bone of the animated armature, or a
    child of the rotation rig) — the per-direction yaw is already baked into that world
    matrix, so no extra rotation is applied here and none must be pre-applied by the author.
    Works identically in CAMERA and OBJECT rotation modes for that reason.
    """
    settings = scene.spriteloom
    sockets, _issues = _resolve_sockets(settings)
    if not sockets or scene.camera is None:
        return {}

    from bpy_extras.object_utils import world_to_camera_view

    # Read the depsgraph-evaluated copy. For plain bone/object parenting this is identical
    # to obj.matrix_world (measured), but a socket driven by a constraint or driver is only
    # guaranteed correct on the evaluated object. `pivot` uses the raw matrix_world.
    depsgraph = context.evaluated_depsgraph_get()

    out = {}
    for name, obj in sockets:
        try:
            world_co = obj.evaluated_get(depsgraph).matrix_world.translation
            cam_co = world_to_camera_view(scene, scene.camera, world_co)
            x, y, z = float(cam_co.x), 1.0 - float(cam_co.y), float(cam_co.z)
        except Exception as exc:
            _log(f"    WARNING socket '{name}' projection failed: {exc} — omitted")
            continue
        if not all(math.isfinite(v) for v in (x, y, z)):
            _log(f"    WARNING socket '{name}' projected to a non-finite value — omitted")
            continue
        out[name] = {"x": round(x, 6), "y": round(y, 6), "z": round(z, 6)}
    return out


def _write_socket_sidecar(out_path, socket_data):
    """Write (or clear) the socket sidecar for one rendered frame.

    Any pre-existing sidecar is removed first, so a frame re-rendered after its sockets
    were removed or renamed cannot inherit the previous run's values — that matters
    because a partial/aborted run leaves sidecars behind and `clean_output` is optional.
    """
    import json as _json

    sidecar_path = os.path.splitext(out_path)[0] + _SOCKET_SIDECAR_EXT
    try:
        if os.path.exists(sidecar_path):
            os.remove(sidecar_path)
        if socket_data:
            with open(sidecar_path, "w", encoding="utf-8") as sf:
                _json.dump(socket_data, sf)
    except OSError as exc:
        _log(f"    WARNING could not write socket sidecar {sidecar_path}: {exc}")


def _resolve_compositor_scene_and_layer(ng_name):
    """From a [Matte] group name, return (scene, viewlayer_name) for Cryptomatte nodes.
    Name convention: Scene[Matte] or Scene.Viewlayer[Matte].
    Falls back to first view layer if the name-derived layer isn't found in the scene."""
    base = ng_name[:-7] if ng_name.endswith('[Matte]') else ng_name
    if '.' in base:
        scene_name, vl_name = base.split('.', 1)
    else:
        scene_name, vl_name = base, None
    scene = bpy.data.scenes.get(scene_name)
    if scene is None:
        return None, None
    layers = scene.view_layers
    if vl_name and vl_name in layers:
        return scene, vl_name
    return scene, layers[0].name


def _resolve_compositors(filter_str):
    """Return COMPOSITING node groups matching the inclusion filter (empty = all)."""
    groups = [ng for ng in bpy.data.node_groups if ng.type == 'COMPOSITING']
    included = _parse_include(filter_str)
    if included is not None:
        groups = [ng for ng in groups if ng.name in included]
    return groups


def _prefix_filtered(items, get_name, filter_text, filter_enabled, is_default=False, scene_name=""):
    if not filter_enabled:
        return items
    prefix = (scene_name if is_default else filter_text).strip().lower()
    if not prefix:
        return items
    return [item for item in items if get_name(item).lower().startswith(prefix)]


def _resolve_path(prop_value):
    """Return an absolute path. Supports // Blender-relative paths. Returns '' if unresolvable."""
    if not prop_value:
        return ""
    if prop_value.startswith("//") and not bpy.data.filepath:
        return ""
    return bpy.path.abspath(prop_value)


@dataclass(frozen=True)
class RenderKey:
    """Bundles all naming identity fields for a rendered frame or sprite sheet."""
    blendfile: str
    action_name: str
    compositor_name: str
    direction_name: str
    scene_name: str = ""

    def stem(self, frame: int, tag: str = "") -> str:
        """Filename stem (no extension): blendfile--scene--action--compositor--direction[--tag]--nnnn"""
        parts = [self.blendfile, self.scene_name, self.action_name, self.compositor_name, self.direction_name]
        if tag:
            parts.append(tag)
        parts.append(f"{frame:04d}")
        return "--".join(parts)

    def prefix(self, tag: str = "") -> str:
        """Prefix for exists-checks: blendfile--scene--action--compositor--direction[--tag]--"""
        parts = [self.blendfile, self.scene_name, self.action_name, self.compositor_name, self.direction_name]
        if tag:
            parts.append(tag)
        return "--".join(parts) + "--"

    def slot_name(self) -> str:
        """PointCache slot name for cloth bakes. Direction omitted when empty."""
        def _s(v):
            return v.replace("/", "_").replace("\\", "_").replace(" ", "_")
        base = f"{_s(self.compositor_name)}__{_s(self.action_name)}"
        return f"{base}__{_s(self.direction_name)}" if self.direction_name else base

    def sheet_key(self, split_axes: set) -> tuple:
        return (
            self.scene_name,
            self.blendfile,
            self.action_name if 'ACTION' in split_axes else "",
            self.compositor_name if 'COMPOSITOR' in split_axes else "",
            self.direction_name if 'DIRECTION' in split_axes else "",
        )

    def _scene_display(self) -> str:
        """Scene name for use in final output names: empty when it's the trivial default."""
        try:
            import bpy
            trivial = len(bpy.data.scenes) == 1 and self.scene_name == "Scene"
        except Exception:
            trivial = False
        return "" if trivial else self.scene_name

    def sheet_name(self, fmt: str, split_axes: set = frozenset({'ACTION', 'COMPOSITOR', 'DIRECTION'})) -> str:
        import re
        by_action     = 'ACTION' in split_axes
        by_compositor = 'COMPOSITOR' in split_axes
        by_dir        = 'DIRECTION' in split_axes
        scene_display = self._scene_display()
        name = (fmt or "").replace("{scene}",      scene_display) \
                          .replace("{blendfile}",  self.blendfile) \
                          .replace("{action}",     self.action_name if by_action else "") \
                          .replace("{compositor}", self.compositor_name if by_compositor else "") \
                          .replace("{direction}",  self.direction_name if by_dir else "")
        name = re.sub(r'[-_.]{2,}', '-', name).strip("-_. ")
        if not name:
            parts = [p for p in [self.blendfile, scene_display] if p]
            if by_action:     parts.append(self.action_name)
            if by_compositor: parts.append(self.compositor_name)
            if by_dir:        parts.append(self.direction_name)
            name = "-".join(p for p in parts if p)
        return name or "spritesheet"

    def frame_name(self, fmt: str, frame: int, padding: int = 2, tag: str = "") -> str:
        scene_display = self._scene_display()
        name = (fmt or "").replace("{scene}",      scene_display) \
                          .replace("{blendfile}",  self.blendfile) \
                          .replace("{action}",     self.action_name) \
                          .replace("{compositor}", self.compositor_name) \
                          .replace("{direction}",  self.direction_name) \
                          .replace("{tag}",        tag) \
                          .replace("{frame}",      str(frame).zfill(padding))
        import re
        name = re.sub(r'[-_]{2,}', '-', name)  # collapse double separators from empty {tag}
        name = name.strip("-_ ")
        return name or f"{self.action_name}--{self.compositor_name}--{self.direction_name}--{str(frame).zfill(padding)}"

    def label(self) -> str:
        return f"{self.action_name} / {self.compositor_name} / {self.direction_name}"

    @staticmethod
    def from_stem(parts: list) -> "RenderKey":
        """Reconstruct from a filename stem split on '--': [blendfile, scene, action, compositor, direction, ...]"""
        return RenderKey(blendfile=parts[0], scene_name=parts[1], action_name=parts[2], compositor_name=parts[3], direction_name=parts[4])


def _count_existing_frames(export_root, prefix):
    if not os.path.isdir(export_root):
        return 0
    return len([f for f in os.listdir(export_root) if f.startswith(prefix) and f.lower().endswith(".png")])



def _row_key(f, row_split_axes: set):
    parts = []
    if 'ACTION'      in row_split_axes: parts.append(f["key"].action_name)
    if 'COMPOSITOR'  in row_split_axes: parts.append(f["key"].compositor_name)
    if 'DIRECTION'   in row_split_axes: parts.append(f["key"].direction_name)
    return tuple(parts)


def _pack_sheet(np, spritesheet_root, sheet_name, frames,
                row_split_axes: set,
                renumber_frames=True, frame_num_padding=2, frame_tag=None, blendfile="",
                frame_name_format=None, output_format="PNG"):
    """
    Pack a list of frame dicts into one sprite sheet, optionally split into rows.
    Each frame dict: {"filepath": str, "action": str, "layer": str, "direction": str, "frame_num": int}
    Returns True on success.
    """
    import json

    frames = sorted(frames, key=lambda f: (f["key"].action_name, f["key"].compositor_name, f["key"].direction_name, f["frame_num"]))
    if not frames:
        _log(f"  WARNING: No frames for sheet '{sheet_name}' — skipping.")
        return False

    # Group into rows
    rows_ordered = []
    rows_map = {}
    for f in frames:
        key = _row_key(f, row_split_axes)
        if key not in rows_map:
            rows_map[key] = []
            rows_ordered.append(key)
        rows_map[key].append(f)

    # Detect actual frame dimensions from first image
    try:
        _first_img = bpy.data.images.load(frames[0]["filepath"])
        frame_w, frame_h = _first_img.size[0], _first_img.size[1]
        bpy.data.images.remove(_first_img)
    except Exception as exc:
        _log(f"  ERROR: Could not load first frame to detect size: {exc}")
        return False

    # Validate against scene render resolution
    _scene = bpy.context.scene
    _pct = _scene.render.resolution_percentage / 100.0
    _expected_w = int(_scene.render.resolution_x * _pct)
    _expected_h = int(_scene.render.resolution_y * _pct)
    if frame_w != _expected_w or frame_h != _expected_h:
        _log(f"  ERROR: frame size {frame_w}×{frame_h} does not match "
             f"scene render resolution {_expected_w}×{_expected_h}.")
        return False

    num_rows = len(rows_ordered)
    cols = max(len(rows_map[k]) for k in rows_ordered)
    sheet_w = frame_w * cols
    sheet_h = frame_h * num_rows

    is_float_format = output_format.startswith("OPEN_EXR")
    sheet_ext = ".exr" if is_float_format else ".png"
    sheet_png = os.path.join(spritesheet_root, f"{sheet_name}{sheet_ext}")
    sheet_json = os.path.join(spritesheet_root, f"{sheet_name}.json")
    _log(f"  Packing {sheet_name}: {len(frames)} frame(s) in {num_rows} row(s) × {cols} col(s)")

    sheet_arr = np.zeros((sheet_h, sheet_w, 4), dtype=np.float32)
    frames_meta = {}

    # Build per-(action, compositor, direction) 0-based consecutive index map for renumbering
    frame_index_map = {}
    groups = {}
    for f in frames:
        groups.setdefault((f["key"].action_name, f["key"].compositor_name, f["key"].direction_name), []).append(f)
    for group_frames in groups.values():
        for i, f in enumerate(sorted(group_frames, key=lambda x: x["frame_num"])):
            frame_index_map[id(f)] = i

    for row_idx, key in enumerate(rows_ordered):
        y_px = row_idx * frame_h
        for col_idx, f in enumerate(rows_map[key]):
            filepath = f["filepath"]
            filename = os.path.basename(filepath)
            try:
                img = bpy.data.images.load(filepath)
            except Exception as exc:
                _log(f"    WARNING: Could not load {filename}: {exc} — skipping.")
                continue

            if img.size[0] != frame_w or img.size[1] != frame_h:
                _log(f"    ERROR: {filename} is {img.size[0]}×{img.size[1]}, "
                     f"expected {frame_w}×{frame_h} — skipping frame.")
                bpy.data.images.remove(img)
                continue

            arr = np.array(img.pixels, dtype=np.float32).reshape(img.size[1], img.size[0], 4)
            bpy.data.images.remove(img)

            x_px = col_idx * frame_w
            sheet_arr[y_px:y_px + frame_h, x_px:x_px + frame_w, :] = arr[:frame_h, :frame_w, :]

            display_num = frame_index_map[id(f)] if renumber_frames else f["frame_num"]
            sprite_name = f["key"].frame_name(frame_name_format or "", display_num, padding=frame_num_padding, tag=frame_tag or "")
            frame_entry = {
                "frame": {"x": x_px, "y": sheet_h - y_px - frame_h, "w": frame_w, "h": frame_h},
                "rotated": False,
                "trimmed": False,
                "spriteSourceSize": {"x": 0, "y": 0, "w": frame_w, "h": frame_h},
                "sourceSize": {"w": frame_w, "h": frame_h},
                "duration": FRAME_DURATION_OVERRIDES.get(f["key"].action_name, FRAME_DURATION_MS),
            }
            sidecar_path = os.path.splitext(f["filepath"])[0] + ".pivot"
            if os.path.exists(sidecar_path):
                with open(sidecar_path) as _sf:
                    frame_entry["pivot"] = json.load(_sf)
            socket_path = os.path.splitext(f["filepath"])[0] + _SOCKET_SIDECAR_EXT
            if os.path.exists(socket_path):
                try:
                    with open(socket_path, encoding="utf-8") as _sf:
                        socket_data = json.load(_sf)
                except (OSError, ValueError) as exc:
                    _log(f"    WARNING: bad socket sidecar for {filename}: {exc} — ignored.")
                    socket_data = None
                # Only emit the key when there is something to say. A scene with no
                # sockets must produce byte-identical JSON to before this feature.
                if socket_data:
                    frame_entry["sockets"] = socket_data
            frames_meta[sprite_name] = frame_entry

    sheet_img = bpy.data.images.new(sheet_name, width=sheet_w, height=sheet_h, alpha=True, float_buffer=is_float_format)
    sheet_img.pixels = sheet_arr.flatten().tolist()
    sheet_img.filepath_raw = sheet_png
    sheet_img.file_format = output_format
    try:
        sheet_img.save()
    except Exception as exc:
        _log(f"  ERROR saving {sheet_png}: {exc}")
        bpy.data.images.remove(sheet_img)
        return False
    bpy.data.images.remove(sheet_img)

    meta = {
        "frames": frames_meta,
        "meta": {
            "app": "SpriteLoom",
            "image": os.path.basename(sheet_png),
            "format": "RGBA32F" if is_float_format else "RGBA8888",
            "size": {"w": sheet_w, "h": sheet_h},
            "scale": "1",
        },
    }
    # JSON is written for the beauty pass only. A tagged pass (normal, depth) packs the
    # same frames in the same layout, so its metadata would be a duplicate of the beauty
    # sheet's under a different filename — nothing downstream has a use for it.
    if frame_tag is None:
        try:
            with open(sheet_json, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
        except Exception as exc:
            _log(f"  ERROR writing {sheet_json}: {exc}")
            return False

    return True




def _run_pack(export_root, spritesheet_root, sheet_name_format,
              split_axes: set, row_split_axes: set,
              renumber_frames=True, frame_num_padding=2,
              frame_tag=None, frame_name_format=None, written=None):
    """Pack all rendered frames into sprite sheets. Returns (generated, skipped, errors).

    written: optional list; each successfully packed sheet's path is appended to it, so
             the run status can report real output paths instead of counts alone.

    frame_tag: sanitized tag string (no dashes, e.g. 'n'). When set, only 7-part stems are
               parsed (blendfile--scene--action--compositor--direction--frame--tag), the tag
               is verified against parts[6], and appended to the sheet name. When None,
               6-part beauty stems are parsed. Sheet JSON is written for the beauty pass
               only — a tagged pass shares the beauty sheet's layout and metadata.
    Output format (PNG/EXR etc.) is auto-detected from the extension of the first frame found;
    all frames in a pass must share the same extension or an error is raised per sheet.
    """
    import numpy as np

    if not os.path.isdir(export_root):
        _log(f"  WARNING: export root not found: {export_root}")
        return 0, 0, 0

    os.makedirs(spritesheet_root, exist_ok=True)
    generated = 0
    errors = 0

    # Beauty:  blendfile--scene--action--compositor--direction--0024.png      (6 parts)
    # Tagged:  blendfile--scene--action--compositor--direction--0024--n.png   (7 parts)
    all_frames = []
    for fname in os.listdir(export_root):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in _IMAGE_EXT_TO_FORMAT:
            continue
        stem = fname[: -len(ext)]
        parts = stem.split("--")
        if frame_tag:
            if len(parts) != 7 or not parts[5].isdigit() or parts[6] != frame_tag:
                continue
        else:
            if len(parts) != 6 or not parts[5].isdigit():
                continue
        all_frames.append({
            "filepath": os.path.join(export_root, fname),
            "ext": ext,
            "key": RenderKey.from_stem(parts),
            "frame_num": int(parts[5]),
        })

    if not all_frames:
        _log("  WARNING: No frames found to pack.")
        return 0, 0, 0

    # Group frames into sheets based on split settings
    sheets = {}
    for f in all_frames:
        sheets.setdefault(f["key"].sheet_key(split_axes), []).append(f)

    for _, frames in sorted(sheets.items()):
        rep_key = frames[0]["key"]
        sheet_name = rep_key.sheet_name(sheet_name_format, split_axes=split_axes) + (f"-{frame_tag}" if frame_tag else "")
        exts = {f["ext"] for f in frames}
        if len(exts) > 1:
            _log(f"  ERROR {sheet_name}: mixed file formats {exts} — skipping sheet")
            errors += 1
            continue
        output_format = _IMAGE_EXT_TO_FORMAT[exts.pop()]
        if _pack_sheet(np, spritesheet_root, sheet_name, frames,
                       row_split_axes,
                       renumber_frames, frame_num_padding, frame_tag=frame_tag,
                       frame_name_format=frame_name_format,
                       output_format=output_format):
            generated += 1
            if written is not None:
                written.append(os.path.join(
                    spritesheet_root, sheet_name + _IMAGE_FORMAT_TO_EXT.get(output_format, ".png")))
        else:
            errors += 1

    return generated, 0, errors



def _get_cloth_objects_in_layer(view_layer):
    """Return mesh objects with a Cloth modifier that are renderable in this view layer."""
    return [
        obj for obj in view_layer.objects
        if not obj.hide_render
        and any(m.type == 'CLOTH' for m in obj.modifiers)
    ]




def _find_combo_slot(mod, slot_name):
    """Return (index, slot) for the named PointCache slot, or (None, None) if not found."""
    pcs = mod.point_cache.point_caches
    for i, s in enumerate(pcs):
        if s.name == slot_name:
            return i, s
    return None, None


def _ensure_combo_slot(mod, slot_name):
    """
    Return the PointCache slot for this combo, creating it via ptcache.add() if needed.
    Uses the default blendcache disk cache (use_external=False) — the slot name becomes
    the filename prefix so each combo's files coexist without conflict.
    """
    _, slot = _find_combo_slot(mod, slot_name)
    if slot is not None:
        return slot

    # ptcache.add() creates a new slot and makes it the active one
    with bpy.context.temp_override(active_object=mod.id_data, point_cache=mod.point_cache):
        bpy.ops.ptcache.add()

    new_slot = mod.point_cache
    new_slot.name = slot_name
    new_slot.use_disk_cache = True
    new_slot.use_external = False
    return new_slot


def _activate_combo_slot(mod, slot_name):
    """
    Make the named PointCache slot the active one on mod.
    If the currently-active slot is_baked, free_bakes it first (files on disk are preserved)
    to allow switching. Returns True on success.
    """
    target_idx, _ = _find_combo_slot(mod, slot_name)
    if target_idx is None:
        return False

    current = mod.point_cache
    if current.name == slot_name:
        return True  # already active

    mod.point_cache.point_caches.active_index = target_idx
    return True


def _blend_cache_dir():
    """Return the path to the default blendcache directory, or None if blend is unsaved."""
    if not bpy.data.filepath:
        return None
    blend_dir = os.path.dirname(bpy.data.filepath)
    blend_name = os.path.splitext(os.path.basename(bpy.data.filepath))[0]
    return os.path.join(blend_dir, f"blendcache_{blend_name}")


def _is_combo_baked(vl_name, action_name, direction_name=None):
    """Return True if ANY .bphys frame exists for this combo's slot.

    Presence, not sufficiency — one cached frame satisfies this. Use cache_report()
    when the question is whether the cache can actually be replayed; this is only the
    "has a bake been attempted" test that the render path uses to pick a slot.

    Matches the slot component exactly. The previous startswith() prefix test made
    'ViewLayer__act' (a direction-less query) match 'ViewLayer__act__east' files,
    so a whole-action check was silently answered by a per-direction bake.
    """
    slot_name = RenderKey("", action_name, vl_name, direction_name or "").slot_name()
    return bool(_scan_cache_dir(_blend_cache_dir()).get(slot_name))


def _get_cloth_combos(context):
    """
    Return list of (cloth_obj, view_layer, action) for all active combos
    given the current SpriteLoom settings.
    Scans all scene view layers to find layers that have cloth enabled.
    """
    scene = context.scene
    settings = scene.spriteloom
    included_actions = _parse_include(settings.actions_include)
    actions = [a for a in bpy.data.actions if included_actions is None or a.name in included_actions]

    view_layers = list(scene.view_layers)

    combos = []
    for vl in view_layers:
        for obj in _get_cloth_objects_in_layer(vl):
            for action in actions:
                combos.append((obj, vl, action))
    return combos


def _activate_cloth_paths(action, direction_name=None):
    """
    For each cloth object in any scene view layer, switch mod's active PointCache slot
    to the one dedicated to this (view_layer, action[, direction]) combo.
    Returns a dict mapping (obj_name, mod_name) -> prev_slot_name for later restore.
    Slot names are view-layer-keyed (matching baked data) and independent of compositor names.
    direction_name is only set in OBJECT rotation mode (per-direction bakes).
    """
    scene = bpy.context.scene
    saved = {}
    claimed = set()
    all_cloth_keys = set()
    for vl in scene.view_layers:
        for obj in _get_cloth_objects_in_layer(vl):
            for mod in obj.modifiers:
                if mod.type != 'CLOTH':
                    continue
                key = (obj.name, mod.name)
                all_cloth_keys.add(key)
                if key in claimed:
                    continue
                if _is_combo_baked(vl.name, action.name, direction_name):
                    claimed.add(key)
                    saved[key] = mod.point_cache.name
                    slot_name = RenderKey("", action.name, vl.name, direction_name or "").slot_name()
                    if _activate_combo_slot(mod, slot_name):
                        _log(f"  [cloth] slot activated: {obj.name}/{mod.name} -> '{slot_name}' (vl={vl.name})")
                        # A slot can be is_baked with a single frame on disk. Blender then
                        # silently re-simulates past that frame, so say so here rather than
                        # letting the run look reproducible when it is not.
                        try:
                            n, _fr = _cache_frames_on_disk(_blend_cache_dir(), slot_name)
                            want = _expected_bake_frames(action, scene.spriteloom.cloth_warmup_frames)
                            if want and n < want:
                                _status_note(
                                    f"  [cloth] WARNING incomplete cache '{slot_name}': "
                                    f"{n}/{want} frames — cloth will re-simulate live "
                                    f"past frame {n}; this render is not reproducible")
                        except Exception:
                            pass
                    else:
                        _log(f"  [cloth] ERROR: slot '{slot_name}' not found for {obj.name}/{mod.name}")
    for key in all_cloth_keys - claimed:
        _log(f"  [cloth] ERROR: no baked cache found for {key[0]}/{key[1]} action={action.name} — cloth will not simulate correctly")
    return saved


def _restore_cloth_paths(saved, context=None):
    """Switch each cloth modifier back to its previously-active PointCache slot."""
    if context is None:
        context = bpy.context
    for obj in bpy.data.objects:
        for mod in obj.modifiers:
            if mod.type != 'CLOTH':
                continue
            key = (obj.name, mod.name)
            if key in saved and saved[key]:
                _activate_combo_slot(mod, saved[key])


def _bake_cloth_for_combo(context, obj, view_layer, action, warmup_frames, direction_name=None):
    """
    Bake cloth for a single (obj, view_layer, action[, direction]) combination using a
    dedicated PointCache slot. Each combo gets its own slot with a fixed filepath —
    switching between slots never changes any slot's filepath, so files for other combos
    are never deleted.
    direction_name is only provided in OBJECT rotation mode (per-direction bakes).
    NOTE: ptcache.bake(bake=True) is a blocking call — it runs the full simulation
    before returning. Progress is updated before this call so the user sees which
    combo is being processed.
    """
    context.window.view_layer = view_layer

    settings = context.scene.spriteloom
    armature = settings.armature
    is_real_action = isinstance(action, bpy.types.Action)
    if armature and is_real_action:
        if armature.animation_data is None:
            armature.animation_data_create()
        armature.animation_data.action = action

    for item in settings.extra_animated_objects:
        if item.object is not None and is_real_action:
            item.object.animation_data_create()
            item.object.animation_data.action = action

    bake_start = int(action.frame_range[0]) - warmup_frames
    bake_end   = int(action.frame_range[1])
    dir_info = f" / dir={direction_name}" if direction_name else ""
    _log(f"  Cloth bake '{obj.name}' / '{view_layer.name}' / '{action.name}'{dir_info}: frames {bake_start}→{bake_end}")

    slot_name = RenderKey("", action.name, view_layer.name, direction_name or "").slot_name()


    for mod in obj.modifiers:
        if mod.type != 'CLOTH':
            continue
        # Get or create the dedicated slot for this combo.
        _ensure_combo_slot(mod, slot_name)
        _activate_combo_slot(mod, slot_name)

        # If the slot was previously baked, free it before re-baking.
        if mod.point_cache.is_baked:
            with context.temp_override(active_object=obj, point_cache=mod.point_cache):
                bpy.ops.ptcache.free_bake()

        mod.point_cache.frame_start = bake_start
        mod.point_cache.frame_end   = bake_end
        with context.temp_override(active_object=obj, point_cache=mod.point_cache):
            bpy.ops.ptcache.bake(bake=True)

        cache_dir = _blend_cache_dir()
        prefix = slot_name + "_"
        written = [f for f in os.listdir(cache_dir) if f.startswith(prefix) and f.endswith('.bphys')] if cache_dir and os.path.isdir(cache_dir) else []
        if written:
            _log(f"    Verified: {len(written)} .bphys files for slot '{slot_name}'")
        else:
            _log(f"    ERROR: no .bphys files found for slot '{slot_name}' in {cache_dir}")
    _log(f"    Baked '{obj.name}'")


# ---------------------------------------------------------------------------
# Cloth cache coverage — counted from disk, because is_baked lies
# ---------------------------------------------------------------------------

# `point_cache.is_baked` is a flag on the slot, not a measurement of the cache. A
# slot that was baked and then had its .bphys files removed (or a bake that died
# partway) still reports is_baked=True while holding a single frame. Blender then
# has nothing to replay past that frame and silently re-simulates the cloth live,
# so the render is no longer reproducible — and nothing anywhere says so. Every
# coverage question below is therefore answered by counting files.

_BPHYS_RE = re.compile(r"^(?P<slot>.+)_(?P<frame>\d{6})_(?P<index>\d+)\.bphys$")


def _scan_cache_dir(cache_dir):
    """Index the blendcache directory once: {slot_name: [frame_numbers sorted]}.

    Built in a single listdir because the panel asks for coverage on every redraw and
    there is one entry per (object, view layer, action, direction) combo — scanning
    per combo turns one directory walk into dozens.

    Slots are keyed on the exact slot component of the filename rather than by a
    startswith() prefix test. Slot names are hierarchical ('ViewLayer__action__east'),
    so 'ViewLayer__farmer_idle_loop' is a literal prefix of
    'ViewLayer__farmer_idle_loop__east' and a prefix test silently reports a
    whole-action query as satisfied by any one of its per-direction bakes.
    """
    index = {}
    if not cache_dir or not os.path.isdir(cache_dir):
        return index
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return index
    for fname in names:
        m = _BPHYS_RE.match(fname)
        if m:
            index.setdefault(m.group("slot"), []).append(int(m.group("frame")))
    for frames in index.values():
        frames.sort()
    return index


def _cache_frames_on_disk(cache_dir, slot_name, index=None):
    """Return (count, frames_sorted) of cached frames for this slot."""
    if index is None:
        index = _scan_cache_dir(cache_dir)
    frames = index.get(slot_name, [])
    return len(frames), frames


def _expected_bake_frames(action, warmup_frames):
    """Number of .bphys files a complete bake of this combo should leave on disk.

    Mirrors _bake_cloth_for_combo's range (frame_range[0] - warmup .. frame_range[1]),
    with one measured correction: Blender never writes a cache file for frame 0, so a
    bake starting at or below 0 begins on disk at frame 1. Computing this from the
    nominal start instead would report every healthy bake as one frame short.
    """
    try:
        start = int(action.frame_range[0]) - int(warmup_frames)
        end = int(action.frame_range[1])
    except Exception:
        return 0
    return max(0, end - max(start, 1) + 1)


def cache_report(context=None):
    """Coverage of every cloth cache combo, measured against the files on disk.

    Returns {"<object>/<view_layer>/<action>/<direction>": {
                 "slot", "is_baked", "frames_on_disk", "expected_frames",
                 "complete", "contiguous", "first_frame", "last_frame"}}

    `complete` requires the frame count to reach the expected count AND the frames to
    be contiguous — a cache with holes replays just as badly as a short one.
    `is_baked` is reported alongside precisely so a caller can see the two disagree.
    """
    if context is None:
        context = bpy.context
    scene = context.scene
    settings = scene.spriteloom
    warmup = settings.cloth_warmup_frames
    cache_dir = _blend_cache_dir()
    index = _scan_cache_dir(cache_dir)

    if settings.rotation_mode == "OBJECT":
        direction_names = [d[0] for d in _get_directions(settings.num_directions)]
    else:
        direction_names = [None]

    report = {}
    for obj, vl, action in _get_cloth_combos(context):
        for direction_name in direction_names:
            slot_name = RenderKey("", action.name, vl.name, direction_name or "").slot_name()
            count, frames = _cache_frames_on_disk(cache_dir, slot_name, index)
            expected = _expected_bake_frames(action, warmup)

            is_baked = False
            for mod in obj.modifiers:
                if mod.type != 'CLOTH':
                    continue
                _, slot = _find_combo_slot(mod, slot_name)
                if slot is not None:
                    is_baked = bool(slot.is_baked)
                    break

            contiguous = bool(frames) and frames == list(range(frames[0], frames[-1] + 1))
            key = f"{obj.name}/{vl.name}/{action.name}/{direction_name or ''}"
            report[key] = {
                "slot": slot_name,
                "is_baked": is_baked,
                "frames_on_disk": count,
                "expected_frames": expected,
                "complete": bool(expected) and count >= expected and contiguous,
                "contiguous": contiguous,
                "first_frame": frames[0] if frames else None,
                "last_frame": frames[-1] if frames else None,
            }
    return report


def get_cache_report(context=None):
    """Public alias for cache_report(), for symmetry with get_status()."""
    return cache_report(context)


def _incomplete_cache_combos(context=None):
    """Combos whose cache cannot be replayed — the ones that will silently re-simulate."""
    return {k: v for k, v in cache_report(context).items() if not v["complete"]}


_NORMAL_OUTPUT_NODE_NAME = "Normal Output"
_DEPTH_OUTPUT_NODE_NAME = "Depth Output"

_IMAGE_EXT_TO_FORMAT = {".png": "PNG", ".exr": "OPEN_EXR"}
_IMAGE_FORMAT_TO_EXT = {v: k for k, v in _IMAGE_EXT_TO_FORMAT.items()}


def _to_camera_space_inplace(path, cam_rot_3x3, flip_y=False):
    """
    Transform world-space normal map to camera-space normals, saved back in its original format.
    s: numpy 3x3 camera-to-world rotation matrix (from matrix_world.normalized().to_3x3()).
    Applies the world-to-camera transform (transpose of cam_rot_3x3) to all three channels.
    If flip_y=True, inverts the G channel (OpenGL → DirectX for Unreal Engine).
    """
    import numpy as np
    img = bpy.data.images.load(path, check_existing=False)
    try:
        w, h = img.size
        px = np.array(img.pixels[:], dtype=np.float32).reshape(h, w, 4)
        # Remap R,G,B from [0,1] to [-1,1]
        nx = px[:, :, 0] * 2.0 - 1.0
        ny = px[:, :, 1] * 2.0 - 1.0
        nz = px[:, :, 2] * 2.0 - 1.0
        # N_cam = M.T @ N_world — each output component is a dot with a column of M
        R = cam_rot_3x3
        nx_cam = R[0, 0] * nx + R[1, 0] * ny + R[2, 0] * nz
        ny_cam = R[0, 1] * nx + R[1, 1] * ny + R[2, 1] * nz
        nz_cam = R[0, 2] * nx + R[1, 2] * ny + R[2, 2] * nz
        if flip_y:
            ny_cam = -ny_cam
        px[:, :, 0] = nx_cam * 0.5 + 0.5
        px[:, :, 1] = ny_cam * 0.5 + 0.5
        px[:, :, 2] = nz_cam * 0.5 + 0.5
        img.pixels = px.ravel().tolist()
        img.filepath_raw = path
        img.file_format = _IMAGE_EXT_TO_FORMAT.get(os.path.splitext(path)[1].lower(), 'PNG')
        img.save()
    finally:
        bpy.data.images.remove(img)


def _find_normal_output_node(nt):
    """Return the File Output node named 'Normal Output', or None."""
    return next(
        (n for n in nt.nodes
         if n.type == 'OUTPUT_FILE' and n.name == _NORMAL_OUTPUT_NODE_NAME),
        None,
    )


def _find_depth_output_node(nt):
    """Return the File Output node named 'Depth Output', or None."""
    return next(
        (n for n in nt.nodes
         if n.type == 'OUTPUT_FILE' and n.name == _DEPTH_OUTPUT_NODE_NAME),
        None,
    )


def _has_cryptomatte(ng, cache=None):
    if cache is None:
        cache = {}
    if ng in cache:
        return cache[ng]
    cache[ng] = False  # guard against cycles
    for node in ng.nodes:
        if node.type in ('CRYPTOMATTE', 'CRYPTOMATTE_V2'):
            cache[ng] = True
            break
        if node.type == 'GROUP' and node.node_tree is not None:
            if _has_cryptomatte(node.node_tree, cache):
                cache[ng] = True
                break
    return cache[ng]


def _deep_clone_node_group(ng, visited=None, needs_cache=None):
    if visited is None:
        visited = {}
    if needs_cache is None:
        needs_cache = {}
    if ng in visited:
        return visited[ng]
    copy = ng.copy()
    visited[ng] = copy
    for node in copy.nodes:
        if node.type == 'GROUP' and node.node_tree is not None:
            if _has_cryptomatte(node.node_tree, needs_cache):
                node.node_tree = _deep_clone_node_group(node.node_tree, visited, needs_cache)
    return copy


def _build_job_queue(context, export_root, dry_run=False, cleared=None):
    """
    Build the full list of render jobs. Each job is a single frame to render.
    Entire action/layer/direction combos are skipped if all frames already exist.
    Returns (jobs, skipped_count) or (None, error_message) on failure.

    dry_run: build the identical queue WITHOUT touching the filesystem — no makedirs and,
             critically, none of the overwrite-clearing deletes below. This is what makes a
             plan safe to ask for: the planner walks exactly the same code as the real run,
             so its predictions cannot drift from behaviour, but it removes nothing.
    cleared: optional list; the frame files an overwrite pass would delete are appended to it
             (in dry_run they are only recorded, never removed).
    """
    scene = context.scene
    settings = scene.spriteloom

    armature_obj = settings.armature
    rotation_rig = settings.rotation_rig

    import types as _types
    static_action = _types.SimpleNamespace(
        name="static",
        frame_range=(scene.frame_current, scene.frame_current),
        use_cyclic=False,
    )

    if armature_obj is None:
        chr_actions = [static_action]
    else:
        included_actions = _parse_include(settings.actions_include)
        chr_actions = [a for a in bpy.data.actions if included_actions is None or a.name in included_actions]
        if not chr_actions:
            chr_actions = [static_action]

    compositors = _resolve_compositors(settings.compositors_include)
    if not compositors:
        return None, "No compositor node groups to render"
    compositor_iter = [(ng.name, ng) for ng in compositors]

    directions = [("south", 0.0)] if rotation_rig is None else _get_directions(settings.num_directions)
    fmt = settings.direction_format
    if fmt == 'ANGLE':
        directions = [(f"{int(round(math.degrees(a))):03d}", a) for _, a in directions]
    elif fmt == 'INDEX':
        directions = [(f"{i:02d}", a) for i, (_, a) in enumerate(sorted(directions, key=lambda d: d[1] % (2 * math.pi)))]

    # Only truly "static" (copy instead of pack) when exactly 1 PNG would be produced total.
    is_static = armature_obj is None and len(directions) == 1 and len(compositor_iter) == 1
    frame_step = settings.frame_step
    overwrite = settings.overwrite_frames
    blendfile = os.path.splitext(os.path.basename(bpy.data.filepath))[0] if bpy.data.filepath else "untitled"
    scene_name = scene.name

    _log(f"Found {len(chr_actions)} action(s): {[a.name for a in chr_actions]}")
    _log(f"Compositors : {[name for name, _ in compositor_iter]}")
    _log(f"Directions  : {[d[0] for d in directions]}  ({len(directions)})")
    _log(f"Frame step  : {frame_step}")
    _log(f"Overwrite   : {overwrite}")

    jobs = []
    skipped = 0
    for action in chr_actions:
        frame_start = int(action.frame_range[0])
        frame_end = int(action.frame_range[1])
        loop_end = frame_end if action.use_cyclic else frame_end + 1
        frames = list(range(frame_start, loop_end, frame_step))
        expected_frames = len(frames)
        action_jobs = []
        if not dry_run:
            os.makedirs(export_root, exist_ok=True)
        for compositor_name, compositor_ng in compositor_iter:
            for direction_name, angle_radians in directions:
                rkey = RenderKey(blendfile, action.name, compositor_name, direction_name, scene_name)
                if not overwrite and _count_existing_frames(
                        export_root, rkey.prefix()) >= expected_frames:
                    _log(f"  SKIP  {rkey.label()} ({expected_frames} frames exist)")
                    skipped += 1
                    continue
                if overwrite:
                    doomed = [
                        f for f in (os.listdir(export_root) if os.path.isdir(export_root) else [])
                        if f.startswith(rkey.prefix()) and f.lower().endswith(".png")
                    ]
                    if cleared is not None:
                        cleared.extend(doomed)
                    if not dry_run:
                        for f in doomed:
                            os.remove(os.path.join(export_root, f))
                        _log(f"  CLEAR {rkey.label()}")
                for frame in frames:
                    action_jobs.append({
                        "type": "render",
                        "key": rkey,
                        "action": action,
                        "compositor_name": compositor_name,
                        "compositor_ng": compositor_ng,
                        "direction_name": direction_name,
                        "angle_radians": angle_radians,
                        "out_stem": rkey.prefix()[:-2] if is_static else rkey.stem(frame),
                        "out_path": os.path.join(export_root, (rkey.prefix()[:-2] if is_static else rkey.stem(frame)) + ".png"),
                        "frame": frame,
                        "frame_start": frame_start,
                        "frame_end": frame_end,
                        "armature_obj": armature_obj,
                        "rotation_rig": rotation_rig,
                        "is_static": is_static,
                    })
        jobs.extend(action_jobs)
    return jobs, skipped


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

_split_axes_updating = False

def _find_scene_camera(scene):
    """Return the best camera for this scene: prefer scene.camera if it's in scene.objects,
    otherwise use the first camera object that actually belongs to the scene."""
    scene_cameras = [o for o in scene.objects if o.type == 'CAMERA']
    if scene.camera and scene.camera in scene_cameras:
        return scene.camera
    if scene_cameras:
        cam = scene_cameras[0]
        scene.camera = cam
        return cam
    return scene.camera  # last resort — may be from another scene


def _apply_camera_setup(self, context):
    import math
    s = context.scene.spriteloom
    world = context.scene.world
    g = world.spriteloom_global if world else None
    cam_obj = _find_scene_camera(context.scene)
    if cam_obj is None or cam_obj.type != 'CAMERA' or g is None:
        return
    cam = cam_obj.data
    elev_rad = math.radians(g.cam_elevation)
    cam.type = 'ORTHO'
    cam.clip_start = g.cam_clip_start
    cam.clip_end = g.cam_clip_end
    size = int(s.cam_sprite_size)
    cam.ortho_scale = size * g.cam_units_per_pixel
    context.scene.render.resolution_x = size
    context.scene.render.resolution_y = size
    context.scene.render.resolution_percentage = 100
    cam_obj.rotation_mode = 'XYZ'
    cam_obj.location = (0.0, -g.cam_distance * math.cos(elev_rad), g.cam_distance * math.sin(elev_rad) + s.cam_z_offset)
    cam_obj.rotation_euler = (math.pi / 2.0 - elev_rad, 0.0, 0.0)


def _on_split_axes_update(self, context):
    global _split_axes_updating
    if _split_axes_updating:
        return
    _split_axes_updating = True
    try:
        overlap = set(self.split_axes) & set(self.row_split_axes)
        if overlap:
            self.row_split_axes = set(self.row_split_axes) - overlap
    finally:
        _split_axes_updating = False

def _on_row_split_axes_update(self, context):
    global _split_axes_updating
    if _split_axes_updating:
        return
    _split_axes_updating = True
    try:
        overlap = set(self.row_split_axes) & set(self.split_axes)
        if overlap:
            self.split_axes = set(self.split_axes) - overlap
    finally:
        _split_axes_updating = False


class SpriteLoomAnimatedObject(bpy.types.PropertyGroup):
    object: bpy.props.PointerProperty(
        name="Object",
        description="Additional object that will have the current action applied during rendering",
        type=bpy.types.Object,
    )


class SpriteLoomSocket(bpy.types.PropertyGroup):
    """One named attachment point exported per frame (saddle, hitch, muzzle, …).

    Distinct from `pivot_object`, which is the sprite's own origin. A socket says
    "another sprite attaches here"; the two are consumed by different code downstream
    and are deliberately not conflated.
    """

    name: bpy.props.StringProperty(
        name="Name",
        description="Identifier this socket is exported under. Must be unique within the scene",
        default="",
    )
    object: bpy.props.PointerProperty(
        name="Object",
        description=("Object whose world position is projected per frame. Parent it in the same "
                     "frame as the geometry it belongs to — a bone of the animated object, or a "
                     "child of the rotation rig — the direction yaw is read from its world matrix"),
        type=bpy.types.Object,
    )


class SpriteLoomGlobalSettings(bpy.types.PropertyGroup):
    """Camera geometry settings — shared across all scenes via the World datablock."""

    cam_elevation: bpy.props.FloatProperty(  # type: ignore
        name="Elevation",
        description="Camera angle above horizontal (degrees). 0=side view, 90=top-down",
        default=30.0, min=0.0, max=90.0,
        update=_apply_camera_setup, options=set(),
    )
    cam_distance: bpy.props.FloatProperty(  # type: ignore
        name="Distance",
        description="Camera distance from the rotation rig origin (Blender units)",
        default=10.0, min=0.01,
        update=_apply_camera_setup, options=set(),
    )
    cam_clip_start: bpy.props.FloatProperty(  # type: ignore
        name="Near Clip", description="Camera near clipping plane",
        default=0.1, min=0.001,
        update=_apply_camera_setup, options=set(),
    )
    cam_clip_end: bpy.props.FloatProperty(  # type: ignore
        name="Far Clip", description="Camera far clipping plane",
        default=100.0, min=0.01,
        update=_apply_camera_setup, options=set(),
    )
    cam_units_per_pixel: bpy.props.FloatProperty(  # type: ignore
        name="Units/Pixel",
        description="World units per pixel (horizontal). ortho_scale = Width × this value",
        default=1.0 / 32.0, min=0.000001, precision=6,
        update=_apply_camera_setup, options=set(),
    )


class SpriteLoomSettings(bpy.types.PropertyGroup):
    armature: bpy.props.PointerProperty(  # type: ignore
        name="Animated Object",
        description="Object whose actions drive the animation (armature, mesh, etc.)",
        type=bpy.types.Object,
        poll=lambda self, _: True,
        options=set(),
    )
    extra_animated_objects: bpy.props.CollectionProperty(  # type: ignore
        name="Extra Animated Objects",
        description="Additional objects that receive the same action during rendering",
        type=SpriteLoomAnimatedObject,
    )
    active_extra_object_index: bpy.props.IntProperty(default=0)  # type: ignore
    rotation_rig: bpy.props.PointerProperty(  # type: ignore
        name="Rotation Rig",
        description="Object to rotate for direction changes",
        type=bpy.types.Object,
        # No poll — any object type allowed
        options=set(),
    )
    pivot_object: bpy.props.PointerProperty(  # type: ignore
        name="Pivot Object",
        description="Object whose world position is projected to camera space as the sprite pivot per frame",
        type=bpy.types.Object,
        options=set(),
    )
    sockets: bpy.props.CollectionProperty(  # type: ignore
        name="Sockets",
        description=("Named attachment points projected to camera space per frame, exported "
                     "alongside (not instead of) the pivot"),
        type=SpriteLoomSocket,
    )
    active_socket_index: bpy.props.IntProperty(default=0)  # type: ignore
    rotation_mode: bpy.props.EnumProperty(  # type: ignore
        name="Rotation Mode",
        items=[
            ("CAMERA", "Camera", "Rotate rig so camera faces each direction"),
            ("OBJECT", "Object", "Rotate rig so character faces each direction (opposite angle); cloth baked per direction"),
        ],
        default="CAMERA",
        options=set(),
    )
    num_directions: bpy.props.EnumProperty(  # type: ignore
        name="Directions",
        description="Number of render directions",
        items=[
            ("1",  "1 — South only",  ""),
            ("4",  "4 — Cardinal",    ""),
            ("8",  "8 — Octagonal",   ""),
            ("16", "16 — Full",       ""),
        ],
        default="8",
        options=set(),
    )
    direction_format: bpy.props.EnumProperty(  # type: ignore
        name="Direction Format",
        description="How the direction is written in output filenames",
        items=[
            ('CARDINAL', "Cardinal", "Use cardinal name (south, northeast, …)"),
            ('ANGLE',    "Angle",    "Use zero-padded degrees (000, 045, 180, …)"),
            ('INDEX',    "Index",    "Use zero-padded index (00, 01, 02, …)"),
        ],
        default='CARDINAL',
        options=set(),
    )
    actions_include: bpy.props.StringProperty(  # type: ignore
        name="Actions",
        description="Comma-separated action names to include in rendering. Leave blank to render all",
        default="",
        options=set(),
    )
    frame_step: bpy.props.IntProperty(  # type: ignore
        name="Frame Step",
        description="Render every Nth frame (1 = all frames)",
        default=1,
        min=1,
        max=64,
        options=set(),
    )
    compositors_include: bpy.props.StringProperty(  # type: ignore
        name="Compositors",
        description="Comma-separated compositor node group names to include in rendering. Leave blank to render all",
        default="",
        options=set(),
    )
    actions_prefix_filter: bpy.props.StringProperty(  # type: ignore
        name="Actions Filter",
        description="Prefix filter for the actions list",
        default="",
        options=set(),
    )
    actions_prefix_filter_enabled: bpy.props.BoolProperty(  # type: ignore
        name="Filter Actions",
        description="Show only actions whose names start with the filter prefix",
        default=True,
        options=set(),
    )
    actions_prefix_is_default: bpy.props.BoolProperty(  # type: ignore
        name="Use Scene Name",
        description="Use the current scene name as the filter prefix (stays in sync with scene renames)",
        default=True,
        options=set(),
    )
    compositors_prefix_filter: bpy.props.StringProperty(  # type: ignore
        name="Compositors Filter",
        description="Prefix filter for the compositors list",
        default="",
        options=set(),
    )
    compositors_prefix_filter_enabled: bpy.props.BoolProperty(  # type: ignore
        name="Filter Compositors",
        description="Show only compositors whose names start with the filter prefix",
        default=True,
        options=set(),
    )
    compositors_prefix_is_default: bpy.props.BoolProperty(  # type: ignore
        name="Use Scene Name",
        description="Use the current scene name as the filter prefix (stays in sync with scene renames)",
        default=True,
        options=set(),
    )
    rebake_on_render: bpy.props.BoolProperty(  # type: ignore
        name="Rebake on Render",
        description="Automatically rebake all cloth combos before each render run",
        default=False,
        options=set(),
    )
    show_cloth: bpy.props.BoolProperty(  # type: ignore
        name="Cloth Simulation",
        description="Show/hide the Cloth Simulation section",
        default=True,
        options=set(),
    )
    cloth_warmup_frames: bpy.props.IntProperty(  # type: ignore
        name="Warmup Frames",
        description="Extra frames before the first action frame for cloth to settle",
        default=20,
        min=0,
        max=500,
        options=set(),
    )
    clean_output: bpy.props.BoolProperty(  # type: ignore
        name="Clean Before Render",
        description="Delete all files in the export directory before starting a new render",
        default=False,
        options=set(),
    )
    allow_destructive_clean: bpy.props.BoolProperty(  # type: ignore
        name="I understand — delete them",
        description=("Acknowledge that 'Clean Before Render' combined with a narrowed action "
                     "set will delete frames belonging to actions this render will not "
                     "regenerate. Only shown when that would actually lose work"),
        default=False,
        options={'SKIP_SAVE'},  # never persisted: acknowledgement is per-session, not a mode
    )
    overwrite_frames: bpy.props.BoolProperty(  # type: ignore
        name="Overwrite Existing Frames",
        description="Re-render and overwrite frames that already exist on disk (instead of skipping them)",
        default=False,
        options=set(),
    )
    sheet_name_format: bpy.props.StringProperty(  # type: ignore
        name="Name Format",
        description="Sprite sheet filename format. {action}: action name · {compositor}: compositor node group · {direction}: render direction · {scene}: scene name · {blendfile}: blend file stem",
        default="{blendfile}-{scene}-{compositor}-{action}-{direction}",
        options=set(),
    )
    frame_name_format: bpy.props.StringProperty(  # type: ignore
        name="Frame Name Format",
        description="Frame name inside sprite sheet JSON. {action}: action name · {compositor}: compositor node group · {direction}: render direction · {scene}: scene name · {blendfile}: blend file stem · {tag}: pass tag (empty for beauty, e.g. n for normals) · {frame}: zero-padded frame number",
        default="{blendfile}-{scene}-{action}-{compositor}-{direction}-{tag}-{frame}",
        options=set(),
    )
    split_axes: bpy.props.EnumProperty(  # type: ignore
        name="File Splits",
        description="Generate a separate sprite sheet per selected axis",
        items=[
            ('ACTION',      "Action",      "Separate sheet per action"),
            ('COMPOSITOR',  "Compositor",  "Separate sheet per compositor"),
            ('DIRECTION',   "Direction",   "Separate sheet per direction"),
        ],
        default={'ACTION', 'COMPOSITOR'},
        options={'ENUM_FLAG'},
        update=_on_split_axes_update,
    )
    row_split_axes: bpy.props.EnumProperty(  # type: ignore
        name="Row Splits",
        description="Put each selected axis on its own row within a sheet",
        items=[
            ('ACTION',      "Action",      "One row per action"),
            ('COMPOSITOR',  "Compositor",  "One row per compositor"),
            ('DIRECTION',   "Direction",   "One row per direction"),
        ],
        default={'DIRECTION'},
        options={'ENUM_FLAG'},
        update=_on_row_split_axes_update,
    )
    renumber_frames: bpy.props.BoolProperty(  # type: ignore
        name="Renumber Frames",
        description="Frame keys in the JSON start at 0 and are consecutive, instead of using original Blender frame numbers",
        default=True,
        options=set(),
    )
    frame_num_padding: bpy.props.IntProperty(  # type: ignore
        name="Frame Number Padding",
        description="Zero-pad frame numbers to this many digits (e.g. 4 → 0001)",
        default=2,
        min=1,
        max=8,
        options=set(),
    )
    rotation_rig_saved_rotation: bpy.props.FloatProperty(default=float('nan'), options={'SKIP_SAVE'})  # type: ignore
    show_scene_setup: bpy.props.BoolProperty(default=True, options=set())  # type: ignore
    show_output: bpy.props.BoolProperty(default=True, options=set())  # type: ignore
    show_sheet_layout: bpy.props.BoolProperty(default=True, options=set())  # type: ignore
    show_preview: bpy.props.BoolProperty(default=True, options=set())  # type: ignore
    show_render: bpy.props.BoolProperty(default=True, options=set())  # type: ignore
    progress: bpy.props.StringProperty(default="", options={'SKIP_SAVE'})  # type: ignore
    progress_factor: bpy.props.FloatProperty(default=0.0, options={'SKIP_SAVE'})  # type: ignore
    last_result: bpy.props.StringProperty(default="", options=set())  # type: ignore
    export_root: bpy.props.StringProperty(  # type: ignore
        name="Intermediate Dir",
        description="Folder for rendered frames. // paths are relative to the .blend file",
        subtype='DIR_PATH',
        options={'PATH_SUPPORTS_BLEND_RELATIVE'},
        default="//export",
    )
    spritesheet_root: bpy.props.StringProperty(  # type: ignore
        name="Final Dir",
        description="Folder for packed sprite sheets and static renders. // paths are relative to the .blend file",
        subtype='DIR_PATH',
        options={'PATH_SUPPORTS_BLEND_RELATIVE'},
        default="//spritesheets",
    )
    render_normals: bpy.props.BoolProperty(  # type: ignore
        name="Render Normal Maps",
        description="Capture Normal render pass alongside beauty, saved with a tag suffix",
        default=False,
        options=set(),
    )
    normal_tag: bpy.props.StringProperty(  # type: ignore
        name="Normal Tag",
        description="Tag used to identify normal map files (dashes are stripped; e.g. 'n' → action--layer--direction--n--0024.png)",
        default="n",
        options=set(),
    )
    normal_correct_rotation: bpy.props.BoolProperty(  # type: ignore
        name="Camera Space",
        description="Transform world-space normal map to camera space using the full inverse camera rotation (yaw + pitch)",
        default=True,
        options=set(),
    )
    normal_unreal_export: bpy.props.BoolProperty(  # type: ignore
        name="Unreal Engine Export",
        description="Flip the Y (G) channel of normal maps to DirectX convention for Unreal Engine",
        default=True,
        options=set(),
    )
    render_depth: bpy.props.BoolProperty(  # type: ignore
        name="Render Depth Maps",
        description="Capture Depth render pass alongside beauty (requires a 'Depth Output' File Output node in the compositor)",
        default=False,
        options=set(),
    )
    depth_tag: bpy.props.StringProperty(  # type: ignore
        name="Depth Tag",
        description="Tag used to identify depth map files (e.g. 'z' → …--z--0024.exr)",
        default="z",
        options=set(),
    )

    # --- Camera Setup (per-scene) ---
    show_camera_setup: bpy.props.BoolProperty(default=True, options=set())  # type: ignore
    cam_sprite_size: bpy.props.EnumProperty(  # type: ignore
        name="Sprite Size",
        description="Square sprite size in pixels",
        items=[
            ("16",  "16×16",   ""),
            ("32",  "32×32",   ""),
            ("64",  "64×64",   ""),
            ("128", "128×128", ""),
            ("256", "256×256", ""),
            ("512", "512×512", ""),
        ],
        default="64",
        update=_apply_camera_setup, options=set(),
    )
    cam_z_offset: bpy.props.FloatProperty(  # type: ignore
        name="Z Offset",
        description="Shift the camera up/down along world Z to center the sprite on the subject",
        default=0.0,
        update=_apply_camera_setup, options=set(),
    )
    cam_auto_apply: bpy.props.BoolProperty(  # type: ignore
        name="Auto-apply on Render",
        description="Apply camera setup automatically each time Render All is started",
        default=True,
        options=set(),
    )


# ---------------------------------------------------------------------------
# Cloth Bake Operators
# ---------------------------------------------------------------------------

class SPRITELOOM_OT_BakeCloth(bpy.types.Operator):
    """Bake cloth simulations to named disk caches for use during rendering"""

    bl_idname = "spriteloom.bake_cloth"
    bl_label = "Bake Cloth"
    bl_options = {"REGISTER"}

    replace_existing: bpy.props.BoolProperty(default=True)  # type: ignore
    obj_name: bpy.props.StringProperty(default="")  # type: ignore
    vl_name: bpy.props.StringProperty(default="")  # type: ignore
    action_name: bpy.props.StringProperty(default="")  # type: ignore

    _timer = None
    _jobs = []
    _job_index = 0
    _orig_action = None
    _orig_extra_actions = {}
    _orig_window_vl = None
    _orig_rig_z = None

    def execute(self, context):
        settings = context.scene.spriteloom

        if not bpy.data.filepath:
            self.report({"ERROR"}, "Save the .blend file first — cloth caches are stored relative to it")
            return {"CANCELLED"}

        base_combos = _get_cloth_combos(context)

        # Filter to single combo when obj_name is set (per-row button)
        if self.obj_name:
            base_combos = [
                (obj, vl, a) for obj, vl, a in base_combos
                if obj.name == self.obj_name and vl.name == self.vl_name and a.name == self.action_name
            ]

        # Build (obj, vl, action, direction_name, angle_radians) job tuples
        if settings.rotation_mode == "OBJECT":
            directions = _get_directions(settings.num_directions)
            all_jobs = [
                (obj, vl, action, dir_name, angle)
                for dir_name, angle in directions
                for obj, vl, action in base_combos
            ]
        else:
            all_jobs = [(obj, vl, action, None, 0.0) for obj, vl, action in base_combos]

        # Filter already-baked combos when not replacing existing
        if not self.replace_existing:
            all_jobs = [
                job for job in all_jobs
                if not _is_combo_baked(job[1].name, job[2].name, job[3])
            ]

        if not all_jobs:
            self.report({"INFO"}, "Nothing to bake — all combos already baked")
            for area in context.screen.areas:
                area.tag_redraw()
            return {"FINISHED"}

        armature = settings.armature
        self._orig_action = armature.animation_data.action if (armature and armature.animation_data) else None
        self._orig_extra_actions = {
            item.object: item.object.animation_data.action if item.object.animation_data else None
            for item in settings.extra_animated_objects if item.object
        }
        self._orig_window_vl = context.window.view_layer
        self._orig_rig_z = settings.rotation_rig.rotation_euler.z if settings.rotation_rig else None
        self._jobs = all_jobs
        self._job_index = 0
        settings.progress = f"Baking cloth (0/{len(all_jobs)})…"
        settings.progress_factor = 0.0

        self._timer = context.window_manager.event_timer_add(0.05, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        for area in context.screen.areas:
            area.tag_redraw()

        if event.type == "ESC":
            return self._finish(context, cancelled=True)

        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        if self._job_index >= len(self._jobs):
            return self._finish(context)

        obj, vl, action, direction_name, angle_radians = self._jobs[self._job_index]
        settings = context.scene.spriteloom
        total = len(self._jobs)
        dir_info = f" dir={direction_name}" if direction_name else ""
        settings.progress = f"Baking: {obj.name}/{action.name}{dir_info} ({self._job_index + 1}/{total})"
        settings.progress_factor = self._job_index / total

        rotation_rig = settings.rotation_rig
        if settings.rotation_mode == "OBJECT" and rotation_rig and direction_name:
            rotation_rig.rotation_euler.z = -angle_radians

        # NOTE: ptcache.bake(bake=True) is a blocking call — UI is unresponsive
        # during each simulation, which can take seconds to minutes.
        _log(f"=== BakeCloth: '{obj.name}' / '{vl.name}' / '{action.name}'{dir_info} ===")
        _bake_cloth_for_combo(context, obj, vl, action, settings.cloth_warmup_frames, direction_name=direction_name)
        _log(f"=== BakeCloth done ===")

        self._job_index += 1
        return {"RUNNING_MODAL"}

    def _finish(self, context, cancelled=False):
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None

        settings = context.scene.spriteloom
        # Restore armature action
        armature = settings.armature
        if armature and armature.animation_data:
            armature.animation_data.action = self._orig_action
        for obj, act in self._orig_extra_actions.items():
            if obj.animation_data:
                obj.animation_data.action = act
        # Restore window view layer
        if self._orig_window_vl is not None:
            context.window.view_layer = self._orig_window_vl
        # Restore rotation rig angle
        if settings.rotation_rig and self._orig_rig_z is not None:
            settings.rotation_rig.rotation_euler.z = self._orig_rig_z

        done = self._job_index
        total = len(self._jobs)
        if cancelled:
            settings.progress = ""
            self.report({"INFO"}, f"Bake cancelled after {done}/{total} combos")
        else:
            settings.progress = ""
            self.report({"INFO"}, f"Baked {done} cloth combo(s)")

        settings.progress_factor = 0.0
        for area in context.screen.areas:
            area.tag_redraw()

        return {"FINISHED"}


class SPRITELOOM_OT_DeleteBakes(bpy.types.Operator):
    """Delete all SpriteLoom cloth cache files"""

    bl_idname = "spriteloom.delete_bakes"
    bl_label = "Delete Cloth Bakes"
    bl_options = {"REGISTER"}

    def execute(self, context):
        if not bpy.data.filepath:
            self.report({"WARNING"}, "No .blend file saved — nothing to delete")
            return {"CANCELLED"}

        cache_dir = _blend_cache_dir()
        if not cache_dir or not os.path.isdir(cache_dir):
            self.report({"WARNING"}, "No blendcache directory found — nothing to delete")
            return {"CANCELLED"}

        combos = _get_cloth_combos(context)
        deleted_files = 0

        # Delete cache files on disk
        for _, vl, action in combos:
            slot_name = RenderKey("", action.name, vl.name, "").slot_name()
            prefix = slot_name + "_"
            for f in os.listdir(cache_dir):
                if f.startswith(prefix) and (f.endswith('.bphys') or f.endswith('.bobj.gz')):
                    os.remove(os.path.join(cache_dir, f))
                    deleted_files += 1

        # Remove all SpriteLoom slots from every cloth modifier.
        # Strategy: free bake on every slot first, then remove all but slot 0,
        # then clear slot 0's name. ptcache.remove() can't remove the last slot.
        removed_slots = 0
        visited_mods = set()
        for obj in context.scene.objects:
            for mod in obj.modifiers:
                mod_key = (obj.name, mod.name)
                if mod.type != 'CLOTH' or mod_key in visited_mods:
                    continue
                visited_mods.add(mod_key)
                pcs = mod.point_cache.point_caches
                # Free bake data on every slot (deletes disk files for each)
                for i in range(len(pcs)):
                    if pcs[i].is_baked or pcs[i].name:
                        pcs.active_index = i
                        with context.temp_override(point_cache=mod.point_cache):
                            bpy.ops.ptcache.free_bake()
                # Remove extra slots highest-to-lowest, leaving only slot 0
                while len(pcs) > 1:
                    pcs.active_index = len(pcs) - 1
                    with context.temp_override(point_cache=mod.point_cache):
                        bpy.ops.ptcache.remove()
                    removed_slots += 1
                # Clear name on the mandatory last slot
                if pcs[0].name:
                    pcs[0].name = ""
                removed_slots += 1

        self.report({"INFO"}, f"Deleted {deleted_files} cache file(s), removed {removed_slots} slot(s)")
        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Destructive-combination guard, and the render plan (dry run)
# ---------------------------------------------------------------------------


def _action_of_frame_file(fname):
    """Action name encoded in a rendered frame's filename, or None if it isn't one."""
    ext = os.path.splitext(fname)[1].lower()
    if ext not in _IMAGE_EXT_TO_FORMAT:
        return None
    parts = fname[: -len(ext)].split("--")
    return parts[2] if len(parts) >= 6 else None


def _clean_output_casualties(context, export_root):
    """Frames `clean_output` would delete that this render will NOT put back.

    This is the one genuinely destructive combination in the tool. `clean_output`
    empties the whole export root, but `actions_include` narrows what gets rendered
    back into it — so narrowing the action set with clean on deletes every other
    action's frames, and the packer then produces a sheet containing only the actions
    that happened to be selected. Nothing in the UI or the settings says the two
    interact. Returns {action_name: file_count} for the actions that would be lost.
    """
    settings = context.scene.spriteloom
    if not settings.clean_output or not os.path.isdir(export_root):
        return {}
    included = _parse_include(settings.actions_include)
    if included is None:
        return {}  # rendering every action: clean deletes nothing it will not regenerate

    casualties = {}
    for fname in os.listdir(export_root):
        action = _action_of_frame_file(fname)
        if action and action not in included:
            casualties[action] = casualties.get(action, 0) + 1
    return casualties


def _describe_casualties(casualties):
    total = sum(casualties.values())
    listed = ", ".join(f"{a} ({n} frames)" for a, n in sorted(casualties.items()))
    return total, listed


def build_plan(context=None):
    """Describe what a render would do, without rendering or deleting anything.

    Everything here is derived from the SAME job queue the real run builds (via
    dry_run), so the plan cannot silently disagree with what actually happens.
    """
    if context is None:
        context = bpy.context
    scene = context.scene
    settings = scene.spriteloom
    export_root = _resolve_path(settings.export_root)
    spritesheet_root = _resolve_path(settings.spritesheet_root)

    plan = {
        "ok": True, "errors": [], "warnings": [],
        "export_root": export_root, "spritesheet_root": spritesheet_root,
        "actions": [], "compositors": [], "directions": [],
        "frame_count": 0, "skipped_combos": 0, "per_action": {},
        "clean": {"enabled": bool(settings.clean_output), "would_delete": 0,
                  "by_action": {}, "casualties": {}, "destructive": False},
        "overwrite": {"enabled": bool(settings.overwrite_frames), "would_delete": 0},
        "cloth": {"incomplete_combos": {}, "live_sim_actions": []},
        "outputs": {"would_overwrite": [], "sheets": []},
    }

    if not export_root:
        plan["ok"] = False
        plan["errors"].append(
            "Export path is relative — save the .blend file first, or set an absolute Export Root.")
        return plan

    # --- what clean_output would remove, and whether any of it is unrecoverable ---
    if settings.clean_output and os.path.isdir(export_root):
        by_action = {}
        total = 0
        for fname in os.listdir(export_root):
            if os.path.isfile(os.path.join(export_root, fname)):
                total += 1
                a = _action_of_frame_file(fname) or "(other)"
                by_action[a] = by_action.get(a, 0) + 1
        plan["clean"]["would_delete"] = total
        plan["clean"]["by_action"] = by_action

    casualties = _clean_output_casualties(context, export_root)
    if casualties:
        n, listed = _describe_casualties(casualties)
        plan["clean"]["casualties"] = casualties
        plan["clean"]["destructive"] = True
        plan["ok"] = False
        plan["errors"].append(
            f"Refusing: 'Clean Before Render' with a narrowed action set would delete "
            f"{n} frame(s) belonging to {len(casualties)} action(s) that this render will "
            f"not regenerate — {listed}. Pass allow_destructive_clean=True to proceed anyway, "
            f"or turn off Clean Before Render to add to the existing frames."
        )

    # --- the queue itself, built dry ---
    cleared = []
    jobs, result = _build_job_queue(context, export_root, dry_run=True, cleared=cleared)
    if jobs is None:
        plan["ok"] = False
        plan["errors"].append(result)
        return plan

    plan["overwrite"]["would_delete"] = len(cleared)
    plan["skipped_combos"] = result

    render_jobs = [j for j in jobs if j["type"] == "render"]
    plan["frame_count"] = len(render_jobs)

    actions, compositors, directions = [], [], []
    per_action = {}
    for j in render_jobs:
        k = j["key"]
        if k.action_name not in actions:
            actions.append(k.action_name)
        if k.compositor_name not in compositors:
            compositors.append(k.compositor_name)
        if k.direction_name not in directions:
            directions.append(k.direction_name)
        pa = per_action.setdefault(k.action_name, {"frames": 0, "directions": set()})
        pa["frames"] += 1
        pa["directions"].add(k.direction_name)
    plan["actions"] = actions
    plan["compositors"] = compositors
    plan["directions"] = directions
    plan["per_action"] = {
        a: {"frames": v["frames"], "directions": len(v["directions"])}
        for a, v in per_action.items()
    }

    # --- cloth caches that cannot be replayed: these frames come out of a live sim ---
    planned_actions = set(actions)
    incomplete = {}
    for key, info in _incomplete_cache_combos(context).items():
        parts = key.split("/")
        combo_action = parts[2] if len(parts) > 2 else ""
        if combo_action in planned_actions:
            incomplete[key] = info
    if incomplete:
        live_actions = sorted({k.split("/")[2] for k in incomplete})
        plan["cloth"]["incomplete_combos"] = incomplete
        plan["cloth"]["live_sim_actions"] = live_actions
        if not settings.rebake_on_render:
            plan["warnings"].append(
                f"{len(incomplete)} cloth cache(s) are incomplete on disk (is_baked may still "
                f"report baked). Those frames will be simulated live and are not reproducible — "
                f"actions affected: {', '.join(live_actions)}."
            )

    # --- sheets this run would land on ---
    split_axes = set(settings.split_axes)
    sheet_names = []
    for j in render_jobs:
        n = j["key"].sheet_name(settings.sheet_name_format, split_axes=split_axes)
        if n not in sheet_names:
            sheet_names.append(n)
    plan["outputs"]["sheets"] = sheet_names
    if spritesheet_root and os.path.isdir(spritesheet_root):
        existing = set(os.listdir(spritesheet_root))
        would = []
        for n in sheet_names:
            for cand in (n + ".png", n + ".json", n + "-n.png", n + "-z.exr"):
                if cand in existing:
                    would.append(os.path.join(spritesheet_root, cand))
        plan["outputs"]["would_overwrite"] = would

    return plan


def plan_render(**overrides):
    """Return the plan for a render, optionally under temporary setting overrides.

    plan_render(actions=[...], keep_existing=True) answers "what would render() do?"
    without changing any setting permanently and without touching a single file.
    """
    context = bpy.context
    if not overrides:
        return build_plan(context)
    with _temp_settings(context, **overrides):
        return build_plan(context)


# ---------------------------------------------------------------------------
# Programmatic entry points
# ---------------------------------------------------------------------------

# The settings below are individually fine and collectively a trap: `clean_output`
# and `actions_include` have to be reasoned about together, and the combination that
# looks most natural for "render just these actions" is the one that destroys the
# others. These wrappers exist so a caller states the INTENT ("render these, keep the
# rest") and the coherent settings are derived from it. They are a correct-by-
# construction path over the existing settings, not a replacement for them.

_SETTING_ALIASES = {
    "actions": "actions_include",
    "compositors": "compositors_include",
    "directions": "num_directions",
}


def _apply_setting(settings, name, value):
    """Apply one alias-aware setting, converting lists to the CSV the props expect."""
    prop = _SETTING_ALIASES.get(name, name)
    if not hasattr(settings, prop):
        raise AttributeError(f"Unknown SpriteLoom setting: {name!r}")
    if isinstance(value, (list, tuple, set)):
        value = ", ".join(str(v) for v in value)
    if prop == "num_directions":
        value = str(value)
    setattr(settings, prop, value)


def _normalise_intent(keep_existing=None, overwrite=None, **kw):
    """Turn intent kwargs into concrete setting values."""
    out = dict(kw)
    if keep_existing is not None:
        # keep_existing is the inverse of clean_output, and is named for what the
        # caller cares about rather than for the mechanism.
        out["clean_output"] = not keep_existing
    if overwrite is not None:
        out["overwrite_frames"] = bool(overwrite)
    return out


class _temp_settings:
    """Apply settings for the duration of a block, then restore them exactly."""

    def __init__(self, context, **overrides):
        self.settings = context.scene.spriteloom
        self.overrides = _normalise_intent(**overrides)
        self.saved = {}

    def __enter__(self):
        for name, value in self.overrides.items():
            prop = _SETTING_ALIASES.get(name, name)
            self.saved[prop] = getattr(self.settings, prop)
            _apply_setting(self.settings, name, value)
        return self.settings

    def __exit__(self, *exc):
        for prop, value in self.saved.items():
            setattr(self.settings, prop, value)
        return False


def render(actions=None, keep_existing=True, wait=False, compositors=None,
           directions=None, overwrite=True, allow_destructive_clean=False,
           dry_run=False):
    """Render a chosen set of actions, coherently and without destroying the rest.

    actions:        list of action names to render. None = leave actions_include alone.
    keep_existing:  True (default) keeps every frame already in the export root, so the
                    packed sheet contains the new actions AND the ones already there.
                    This is the safe incremental form: clean_output is forced off.
                    False empties the export root first — a full rebuild.
    overwrite:      re-render the chosen actions' own frames rather than skipping them
                    because files exist. Only ever touches the chosen actions.
    wait:           run synchronously and return the finished status. The normal path is
                    a modal operator, which cannot report completion and cannot run under
                    `blender -b` at all; wait=True drives the identical job queue inline.
    dry_run:        return the plan and render nothing.

    Returns the plan dict when dry_run or when the run is refused (check ["ok"] and
    ["errors"]); otherwise the status dict — final status if wait, initial if not.
    """
    context = bpy.context
    settings = context.scene.spriteloom

    intent = _normalise_intent(keep_existing=keep_existing, overwrite=overwrite)
    if actions is not None:
        intent["actions"] = actions
    if compositors is not None:
        intent["compositors"] = compositors
    if directions is not None:
        intent["directions"] = directions

    # Plan first, under the intended settings, so a refusal costs nothing.
    with _temp_settings(context, **intent):
        plan = build_plan(context)
    if dry_run:
        return plan
    if not plan["ok"] and not (allow_destructive_clean and plan["clean"]["destructive"]):
        return plan

    # The modal run outlives this call, so the settings have to be applied for real.
    for name, value in intent.items():
        _apply_setting(settings, name, value)

    kwargs = {"allow_destructive_clean": bool(allow_destructive_clean)}
    if wait:
        kwargs["sync"] = True
    bpy.ops.spriteloom.render_all('EXEC_DEFAULT', **kwargs)
    return get_status()


def render_incremental(actions, wait=False, **kw):
    """Add actions to an existing sheet, keeping every frame already rendered."""
    return render(actions=actions, keep_existing=True, wait=wait, **kw)


class SPRITELOOM_OT_RenderAll(bpy.types.Operator):
    """Render all chr_ actions × view layers × directions to disk"""

    bl_idname = "spriteloom.render_all"
    bl_label = "Render All"
    bl_options = {"REGISTER"}

    plan_only: bpy.props.BoolProperty(  # type: ignore
        name="Plan Only",
        description="Report what this render would do and change nothing on disk",
        default=False,
        options={'SKIP_SAVE'},
    )
    sync: bpy.props.BoolProperty(  # type: ignore
        name="Synchronous",
        description="Run the whole queue inline instead of modally, and return when it is done",
        default=False,
        options={'SKIP_SAVE'},
    )
    allow_destructive_clean: bpy.props.BoolProperty(  # type: ignore
        name="Allow Destructive Clean",
        description=("Acknowledge that 'Clean Before Render' with a narrowed action set will "
                     "delete frames belonging to actions this render will not regenerate"),
        default=False,
        options={'SKIP_SAVE'},
    )

    _timer = None
    _jobs = []
    _job_index = 0
    _render_total = 0
    _skipped = 0
    _rendered = 0
    _errors = 0
    _export_root = ""
    _spritesheet_root = ""
    _active_cloth_paths = {}
    _last_cloth_combo = None

    def execute(self, context):
        settings = context.scene.spriteloom

        if settings.cam_auto_apply:
            bpy.ops.spriteloom.setup_camera()

        self._export_root = _resolve_path(settings.export_root)
        self._spritesheet_root = _resolve_path(settings.spritesheet_root)

        if not self._export_root:
            self.report({"ERROR"}, "Save the .blend file first, or set an explicit Export Root path.")
            return {"CANCELLED"}

        # Force Object Mode so frame_set() properly updates the depsgraph for
        # armature-deformed meshes. In Edit/Sculpt/etc. mode the evaluated mesh
        # is frozen at its rest state and renders come out static.
        if context.mode != 'OBJECT':
            _log(f"Switching from '{context.mode}' to OBJECT mode before render")
            bpy.ops.object.mode_set(mode='OBJECT')

        _log("=== SpriteLoom: Render All started ===")
        _log(f"Export root     : {self._export_root}")
        _log(f"Spritesheet root: {self._spritesheet_root}")

        # Plan before touching anything. This both answers plan_only and enforces the
        # destructive-combination guard while every file is still on disk.
        global _LAST_PLAN
        _LAST_PLAN = build_plan(context)

        if self.plan_only:
            self.report({"INFO"},
                        f"Plan: {_LAST_PLAN['frame_count']} frame(s), "
                        f"{_LAST_PLAN['clean']['would_delete']} file(s) would be deleted")
            return {"FINISHED"}

        if _LAST_PLAN["clean"]["destructive"] and not (
                self.allow_destructive_clean or settings.allow_destructive_clean):
            msg = _LAST_PLAN["errors"][0] if _LAST_PLAN["errors"] else "Refusing destructive clean"
            _log(f"REFUSED: {msg}")
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}

        for warning in _LAST_PLAN["warnings"]:
            _log(f"  WARNING: {warning}")
            self.report({"WARNING"}, warning)

        if settings.clean_output and os.path.isdir(self._export_root):
            removed = 0
            for fname in os.listdir(self._export_root):
                fpath = os.path.join(self._export_root, fname)
                if os.path.isfile(fpath):
                    os.remove(fpath)
                    removed += 1
            _log(f"Clean: removed {removed} file(s) from {self._export_root}")

        bake_jobs = []
        if settings.rebake_on_render:
            combos = _get_cloth_combos(context)
            if settings.rotation_mode == "OBJECT":
                directions = _get_directions(settings.num_directions)
                total = len(combos) * len(directions)
                idx = 0
                for direction_name, angle_radians in directions:
                    for obj, vl, action in combos:
                        idx += 1
                        bake_jobs.append({
                            "type": "bake",
                            "obj": obj,
                            "vl": vl,
                            "action": action,
                            "direction_name": direction_name,
                            "angle_radians": angle_radians,
                            "bake_index": idx,
                            "bake_total": total,
                        })
            else:
                total = len(combos)
                for idx, (obj, vl, action) in enumerate(combos, 1):
                    bake_jobs.append({
                        "type": "bake",
                        "obj": obj,
                        "vl": vl,
                        "action": action,
                        "direction_name": None,
                        "angle_radians": 0.0,
                        "bake_index": idx,
                        "bake_total": total,
                    })

        jobs, result = _build_job_queue(context, self._export_root)
        if jobs is None:
            self.report({"ERROR"}, result)
            return {"CANCELLED"}

        self._jobs = bake_jobs + jobs
        self._skipped = result
        self._job_index = 0
        self._is_static = any(j.get("is_static") for j in jobs)
        self._render_total = sum(1 for j in jobs if j["type"] == "render")
        self._rendered = 0
        self._errors = 0
        self._orig_frame = context.scene.frame_current
        self._orig_rotation_rig_z = settings.rotation_rig.rotation_euler.z if settings.rotation_rig else None
        self._active_cloth_paths = {}
        self._last_cloth_combo = None

        # Begin the structured run record. This clears every field, so `finished` is
        # false from here until _finish/cancel regardless of what a previous run — or a
        # template scene's inherited last_result — left behind.
        _status_start(
            "render",
            total=self._render_total,
            per_action=_LAST_PLAN.get("per_action", {}),
            messages=list(_LAST_PLAN.get("warnings", [])),
        )
        _status_progress(rendered=0, errors=0, skipped=self._skipped)

        if not jobs:
            _log("Nothing to render — all jobs skipped.")
            self._finish(context)
            return {"FINISHED"}

        context.scene.spriteloom.last_result = ""
        context.window_manager.progress_begin(0, self._render_total)

        if self.sync or context.window is None:
            # No window means no timer and no modal handler — background Blender lands
            # here too, which is the only way `blender -b` can run this operator at all.
            return self._run_sync(context)

        self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        # Redraw the panel each tick so progress updates are visible
        for area in context.screen.areas:
            area.tag_redraw()

        if event.type == "ESC":
            return self.cancel(context)

        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        if self._job_index >= len(self._jobs):
            self._finish(context)
            return {"FINISHED"}

        self._step(context)
        return {"RUNNING_MODAL"}

    def _step(self, context):
        """Execute exactly one queued job (bake or frame) and advance the index.

        Split out of modal() so the synchronous path runs the identical per-job code
        rather than a second implementation of it that could drift.
        """
        job = self._jobs[self._job_index]

        if job["type"] == "bake":
            settings = context.scene.spriteloom
            obj = job["obj"]
            action = job["action"]
            direction_name = job["direction_name"]
            bake_index = job["bake_index"]
            bake_total = job["bake_total"]
            dir_info = f" dir={direction_name}" if direction_name else ""
            settings.progress = f"Baking cloth: {obj.name}/{action.name}{dir_info} ({bake_index}/{bake_total})…"
            settings.progress_factor = bake_index / max(bake_total, 1)
            _log(f"=== Pre-bake {bake_index}/{bake_total}: '{obj.name}'/'{action.name}'{dir_info} ===")
            rotation_rig = settings.rotation_rig
            if settings.rotation_mode == "OBJECT" and rotation_rig and direction_name:
                rotation_rig.rotation_euler.z = -job["angle_radians"]
            _bake_cloth_for_combo(context, job["obj"], job["vl"], action, settings.cloth_warmup_frames, direction_name=direction_name)
            self._job_index += 1
            return

        scene = context.scene
        settings = scene.spriteloom
        action = job["action"]
        armature_obj = job["armature_obj"]
        rotation_rig = job["rotation_rig"]
        frame = job["frame"]
        frame_num = frame - job["frame_start"] + 1
        frame_total = job["frame_end"] - job["frame_start"] + 1

        # Activate pre-baked cloth cache paths when the combo changes.
        # In OBJECT mode, direction is part of the combo key (per-direction bakes).
        direction_name = job.get("direction_name")
        if settings.rotation_mode == "OBJECT":
            combo_key = (job["compositor_name"], action.name, direction_name)
        else:
            combo_key = (job["compositor_name"], action.name)
        if combo_key != self._last_cloth_combo:
            _restore_cloth_paths(self._active_cloth_paths)
            _log(f"[cloth] activating cache for combo: compositor={job['compositor_name']}, action={action.name}, dir={direction_name}")
            self._active_cloth_paths = _activate_cloth_paths(action, direction_name=direction_name)
            self._last_cloth_combo = combo_key

        label = job["key"].label()
        scene.spriteloom.progress = (
            f"{label}  frame {frame_num}/{frame_total}  "
            f"({self._rendered + 1}/{self._render_total})"
        )
        scene.spriteloom.progress_factor = self._rendered / self._render_total if self._render_total else 0.0
        context.window_manager.progress_update(self._rendered)

        is_real_action = isinstance(action, bpy.types.Action)
        orig_use_nla = False
        if armature_obj is not None:
            if armature_obj.animation_data is None:
                armature_obj.animation_data_create()
            orig_use_nla = armature_obj.animation_data.use_nla
            if orig_use_nla:
                _log(f"  [render] disabling NLA on '{armature_obj.name}' (was enabled) to prevent T-pose override")
                armature_obj.animation_data.use_nla = False
            if is_real_action:
                _log(f"  [render] assigning action '{action.name}' to '{armature_obj.name}'")
                armature_obj.animation_data.action = action
            else:
                _log(f"  [render] static frame (no action) on '{armature_obj.name}'")

        for item in settings.extra_animated_objects:
            if item.object is not None and is_real_action:
                item.object.animation_data_create()
                item.object.animation_data.action = action
                _log(f"  [render] assigning action '{action.name}' to extra '{item.object.name}'")

        if rotation_rig is not None:
            if settings.rotation_mode == "OBJECT":
                rotation_rig.rotation_euler.z = -job["angle_radians"]
            else:
                rotation_rig.rotation_euler.z = job["angle_radians"]
        scene.frame_set(frame)
        out_path = job["out_path"]

        rig_z_str = f"{rotation_rig.rotation_euler.z:.3f}" if rotation_rig is not None else "n/a"
        _log(
            f"  RENDER  {job['key'].label()}  frame={frame}  "
            f"rig_z={rig_z_str}"
        )

        fmt = scene.render.image_settings
        orig_filepath = scene.render.filepath
        orig_media_type = fmt.media_type
        orig_file_format = fmt.file_format
        orig_color_mode = fmt.color_mode

        orig_compositor = scene.compositing_node_group
        compositor_ng = job.get("compositor_ng")
        if compositor_ng:
            scene.compositing_node_group = compositor_ng

        # Redirect the existing "Normal Output" File Output compositor node for this frame.
        # Blender writes {directory}/{node.file_name}{item.name}.ext with no auto frame
        # number. We set file_name to the full per-frame path and clear item.name:
        #   action--compositor--direction--n--0024.png  (5-part stem, same export dir as beauty)
        #   action--compositor--direction--0024.png      (4-part stem, beauty)
        nt = scene.compositing_node_group
        normal_node = None
        orig_normal_directory = orig_normal_file_name = orig_normal_item_name = None
        if settings.render_normals and nt:
            normal_node = _find_normal_output_node(nt)
            if normal_node:
                orig_normal_directory = normal_node.directory
                orig_normal_file_name = normal_node.file_name
                orig_normal_item_name = normal_node.file_output_items[0].name
                normal_ext = _IMAGE_FORMAT_TO_EXT.get(normal_node.format.file_format, ".png")
                tag = settings.normal_tag.replace("-", "").strip()
                normal_node.directory = self._export_root
                # file_name has no extension — Blender appends one based on the node's own format
                normal_node.file_name = job["out_stem"] + f"--{tag}"
                normal_node.file_output_items[0].name = ""

        depth_node = None
        orig_depth_directory = orig_depth_file_name = orig_depth_item_name = None
        if settings.render_depth and nt:
            depth_node = _find_depth_output_node(nt)
            if depth_node:
                orig_depth_directory = depth_node.directory
                orig_depth_file_name = depth_node.file_name
                orig_depth_item_name = depth_node.file_output_items[0].name
                dtag = settings.depth_tag.replace("-", "").strip()
                depth_node.directory = self._export_root
                depth_node.file_name = job["out_stem"] + f"--{dtag}"
                depth_node.file_output_items[0].name = ""

        try:
            scene.render.filepath = out_path
            fmt.media_type = "IMAGE"
            fmt.file_format = "PNG"
            fmt.color_mode = "RGBA"
            bpy.ops.render.render("EXEC_DEFAULT", write_still=True)
            _log(f"    OK  saved={out_path}")
            self._rendered += 1
            _status_progress(rendered=self._rendered, errors=self._errors)
            pivot_obj = settings.pivot_object
            if pivot_obj is not None:
                import json as _json
                from bpy_extras.object_utils import world_to_camera_view
                cam_co = world_to_camera_view(scene, scene.camera, pivot_obj.matrix_world.translation)
                pivot_data = {"x": round(cam_co.x, 6), "y": round(1.0 - cam_co.y, 6)}
                sidecar_path = os.path.splitext(out_path)[0] + ".pivot"
                with open(sidecar_path, "w") as _sf:
                    _json.dump(pivot_data, _sf)
            # Sockets are independent of the pivot: written even when no pivot object
            # is set, and the sidecar is cleared when the scene has no sockets.
            _write_socket_sidecar(out_path, _project_sockets(context, scene))
            if normal_node and (settings.normal_correct_rotation or settings.normal_unreal_export):
                normal_path = os.path.join(
                    self._export_root,
                    job["out_stem"] + f"--{tag}{normal_ext}",
                )
                if os.path.exists(normal_path):
                    import numpy as np
                    if settings.normal_correct_rotation:
                        cam_mat = scene.camera.matrix_world.normalized().to_3x3()
                        cam_rot_3x3 = np.array(cam_mat, dtype=np.float32)
                    else:
                        cam_rot_3x3 = np.eye(3, dtype=np.float32)
                    _to_camera_space_inplace(normal_path, cam_rot_3x3, flip_y=settings.normal_unreal_export)
                    _log(f"    Normal: camera_space={settings.normal_correct_rotation} unreal_flip={settings.normal_unreal_export}")
                else:
                    _log(f"    WARNING normal map not found at {normal_path}")
        except Exception as exc:
            _log(f"    ERROR {label} frame {frame}: {exc}")
            self._errors += 1
            _status_note(f"ERROR {label} frame {frame}: {exc}")
            _status_progress(rendered=self._rendered, errors=self._errors)
        finally:
            if normal_node:
                normal_node.directory = orig_normal_directory
                normal_node.file_name = orig_normal_file_name
                normal_node.file_output_items[0].name = orig_normal_item_name
            if depth_node:
                depth_node.directory = orig_depth_directory
                depth_node.file_name = orig_depth_file_name
                depth_node.file_output_items[0].name = orig_depth_item_name
            scene.render.filepath = orig_filepath
            fmt.media_type = orig_media_type
            fmt.file_format = orig_file_format
            fmt.color_mode = orig_color_mode
            scene.compositing_node_group = orig_compositor
            if armature_obj is not None and armature_obj.animation_data and armature_obj.animation_data.use_nla != orig_use_nla:
                _log(f"  [render] restoring NLA on '{armature_obj.name}' to {orig_use_nla}")
                armature_obj.animation_data.use_nla = orig_use_nla

        self._job_index += 1
        return

    def _run_sync(self, context):
        """Drain the whole queue inline, then finish.

        The modal path returns RUNNING_MODAL immediately and is ticked by a window
        timer, which gives a programmatic caller no completion signal and does not run
        under `blender -b` at all (no window, no timer). This walks the same queue via
        the same _step().
        """
        while self._job_index < len(self._jobs):
            self._step(context)
        self._finish(context)
        return {"FINISHED"}

    def _restore_scene(self, context):
        context.scene.frame_set(self._orig_frame)

        settings = context.scene.spriteloom
        if settings.rotation_rig and self._orig_rotation_rig_z is not None:
            settings.rotation_rig.rotation_euler.z = self._orig_rotation_rig_z
        _restore_cloth_paths(self._active_cloth_paths)
        self._active_cloth_paths = {}
        self._last_cloth_combo = None

    def cancel(self, context):
        _log("=== SpriteLoom: Cancelled ===")
        self._restore_scene(context)
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
            context.window_manager.progress_end()
        context.scene.spriteloom.progress = ""
        context.scene.spriteloom.progress_factor = 0.0
        context.scene.spriteloom.last_result = (
            f"Cancelled after {self._rendered} rendered, {self._skipped} skipped, {self._errors} errors"
        )
        _status_progress(rendered=self._rendered, errors=self._errors, skipped=self._skipped)
        _status_note("Cancelled by user (ESC)")
        _status_end(cancelled=True, status_dir=self._spritesheet_root or None)
        return {"CANCELLED"}

    def _finish(self, context):
        self._restore_scene(context)
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
            context.window_manager.progress_end()
        context.scene.spriteloom.progress = ""
        context.scene.spriteloom.progress_factor = 0.0

        _log(f"\n=== Render complete — rendered {self._rendered}, skipped {self._skipped}, errors {self._errors} ===")

        settings = context.scene.spriteloom
        total_errors = self._errors
        packed = n_packed = d_packed = 0
        beauty_paths, normal_paths, depth_paths = [], [], []
        result_lines = [
            f"Rendered: {self._rendered}  Skipped: {self._skipped}  Errors: {self._errors}",
        ]

        if self._is_static:
            _log("=== SpriteLoom: Static render — copying to final dir ===")
            import shutil
            os.makedirs(self._spritesheet_root, exist_ok=True)
            copied = 0
            for j in self._jobs:
                if j.get("type") != "render":
                    continue
                src = j["out_path"]
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(self._spritesheet_root, os.path.basename(src)))
                    copied += 1
                # also copy normal pass if present
                if settings.render_normals:
                    tag = settings.normal_tag.replace("-", "").strip()
                    normal_src = os.path.join(self._export_root, j["out_stem"] + f"--{tag}.png")
                    if os.path.exists(normal_src):
                        shutil.copy2(normal_src, os.path.join(self._spritesheet_root, os.path.basename(normal_src)))
                        copied += 1
                if settings.render_depth:
                    dtag = settings.depth_tag.replace("-", "").strip()
                    for ext in (".exr", ".png"):
                        depth_src = os.path.join(self._export_root, j["out_stem"] + f"--{dtag}{ext}")
                        if os.path.exists(depth_src):
                            shutil.copy2(depth_src, os.path.join(self._spritesheet_root, os.path.basename(depth_src)))
                            copied += 1
                            break
            _log(f"=== Static copy complete — {copied} file(s) copied ===")
            result_lines.append(f"Static render — {copied} file(s) copied to final dir")
        else:
            _log("=== SpriteLoom: Packing sprites ===")
            beauty_paths = []
            packed, pack_skipped, pack_errors = _run_pack(
                self._export_root, self._spritesheet_root,
                settings.sheet_name_format,
                set(settings.split_axes), set(settings.row_split_axes),
                settings.renumber_frames, settings.frame_num_padding,
                frame_name_format=settings.frame_name_format, written=beauty_paths,
            )
            _log(f"=== Pack complete — generated {packed}, skipped {pack_skipped}, errors {pack_errors} ===")
            total_errors += pack_errors
            result_lines.append(f"Sheets: {packed}  Skipped: {pack_skipped}  Errors: {pack_errors}")

            if settings.render_normals:
                normal_tag = settings.normal_tag.replace("-", "").strip()
                _log("=== SpriteLoom: Packing normal map sprites ===")
                n_packed, n_skipped, n_errors = _run_pack(
                    self._export_root, self._spritesheet_root,
                    settings.sheet_name_format,
                    set(settings.split_axes), set(settings.row_split_axes),
                    settings.renumber_frames, settings.frame_num_padding,
                    frame_tag=normal_tag, frame_name_format=settings.frame_name_format,
                    written=normal_paths,
                )
                _log(f"=== Normal pack complete — {n_packed} generated, {n_skipped} skipped, {n_errors} errors ===")
                total_errors += n_errors
                result_lines.append(f"Normal sheets: {n_packed}  Skipped: {n_skipped}  Errors: {n_errors}")

            if settings.render_depth:
                depth_tag = settings.depth_tag.replace("-", "").strip()
                _log("=== SpriteLoom: Packing depth sprites ===")
                d_packed, d_skipped, d_errors = _run_pack(
                    self._export_root, self._spritesheet_root,
                    settings.sheet_name_format,
                    set(settings.split_axes), set(settings.row_split_axes),
                    settings.renumber_frames, settings.frame_num_padding,
                    frame_tag=depth_tag, frame_name_format=settings.frame_name_format,
                    written=depth_paths,
                )
                _log(f"=== Depth pack complete — {d_packed} generated, {d_skipped} skipped, {d_errors} errors ===")
                total_errors += d_errors
                result_lines.append(f"Depth sheets: {d_packed}  Skipped: {d_skipped}  Errors: {d_errors}")

        context.scene.spriteloom.last_result = "\n".join(result_lines)

        # The completion signal. `last_result` above is kept exactly as it was for
        # anything already parsing it; this is the machine-readable half beside it, and
        # writing the file last means its presence with finished=true really means done.
        _status_progress(rendered=self._rendered, errors=total_errors, skipped=self._skipped)
        _status_end(
            cancelled=False,
            sheets={"beauty": packed, "normal": n_packed, "depth": d_packed},
            outputs={
                "sheet": beauty_paths[0] if beauty_paths else None,
                "json": (os.path.splitext(beauty_paths[0])[0] + ".json") if beauty_paths else None,
                "normal": normal_paths[0] if normal_paths else None,
                "depth": depth_paths[0] if depth_paths else None,
                "sheets": beauty_paths,
                "normals": normal_paths,
                "depths": depth_paths,
                "status_file": os.path.join(self._spritesheet_root, STATUS_FILENAME)
                if self._spritesheet_root else None,
            },
            status_dir=self._spritesheet_root or None,
        )

        self.report({"WARNING"} if total_errors > 0 else {"INFO"},
                    f"Render {self._rendered} | Pack {packed if not self._is_static else 'n/a'} | Errors {total_errors}")


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class SPRITELOOM_PT_Main(bpy.types.Panel):
    """SpriteLoom main sidebar panel"""

    bl_label = "SpriteLoom"
    bl_idname = "SPRITELOOM_PT_Main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "SpriteLoom"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.spriteloom
        scene = context.scene

        if settings.armature is None or settings.rotation_rig is None:
            if not bpy.app.timers.is_registered(_auto_detect_all):
                bpy.app.timers.register(_auto_detect_all, first_interval=0.0)

        # --- Scene Setup ---
        box = layout.box()
        row = box.row()
        row.prop(settings, "show_scene_setup", icon="TRIA_DOWN" if settings.show_scene_setup else "TRIA_RIGHT", emboss=False, text="Scene Setup", icon_only=False)
        row.label(icon="SCENE_DATA")
        if settings.show_scene_setup:
            box.prop(settings, "armature")
            extra_row = box.row()
            extra_col = extra_row.column()
            extra_col.label(text="Extra Animated Objects:")
            list_row = extra_col.row()
            list_row.template_list(
                "SPRITELOOM_UL_extra_objects", "extra_animated_objects",
                settings, "extra_animated_objects",
                settings, "active_extra_object_index",
                rows=2,
            )
            btn_col = list_row.column(align=True)
            btn_col.operator("spriteloom.add_extra_object", icon="ADD", text="")
            btn_col.operator("spriteloom.remove_extra_object", icon="REMOVE", text="")
            box.prop(settings, "rotation_rig")
            box.prop(settings, "pivot_object")
            sockets_row = box.row()
            sockets_col = sockets_row.column()
            sockets_col.label(text="Sockets:")
            slist_row = sockets_col.row()
            slist_row.template_list(
                "SPRITELOOM_UL_sockets", "sockets",
                settings, "sockets",
                settings, "active_socket_index",
                rows=2,
            )
            sbtn_col = slist_row.column(align=True)
            sbtn_col.operator("spriteloom.add_socket", icon="ADD", text="")
            sbtn_col.operator("spriteloom.remove_socket", icon="REMOVE", text="")
            rot_row = box.row(align=True)
            rot_row.enabled = settings.rotation_rig is not None
            rot_row.label(text="Rotation:")
            rot_row.prop(settings, "rotation_mode", expand=True)
            dir_row = box.row()
            dir_row.enabled = settings.rotation_rig is not None
            dir_row.prop(settings, "num_directions")
            box.prop(settings, "frame_step")
            actions_box = box.box()
            actions_box.enabled = settings.armature is not None
            hrow = actions_box.row(align=False)
            hrow.label(text="Actions", icon="ACTION")
            rhs = hrow.row(align=True)
            rhs.alignment = 'RIGHT'
            if settings.actions_prefix_is_default:
                rhs.label(text=scene.name)
            else:
                rhs.prop(settings, "actions_prefix_filter", text="")
            rhs.prop(settings, "actions_prefix_is_default", text="", toggle=True, icon='LINKED')
            rhs.prop(settings, "actions_prefix_filter_enabled", text="", toggle=True, icon='FILTER')
            all_actions = list(bpy.data.actions)
            _inc_actions = _parse_include(settings.actions_include)
            display_actions = _prefix_filtered(all_actions, lambda a: a.name,
                settings.actions_prefix_filter, settings.actions_prefix_filter_enabled,
                settings.actions_prefix_is_default, scene.name)
            if _inc_actions:
                display_names = {a.name for a in display_actions}
                display_actions = display_actions + [a for a in all_actions if a.name in _inc_actions and a.name not in display_names]
            if display_actions:
                col = actions_box.column(align=True)
                col.scale_y = 0.75
                for a in display_actions:
                    frame_start = int(a.frame_range[0])
                    frame_end = int(a.frame_range[1])
                    loop_end = frame_end if a.use_cyclic else frame_end + 1
                    frame_count = len(range(frame_start, loop_end, settings.frame_step))
                    is_on = _inc_actions is None or a.name in _inc_actions
                    loop_suffix = "  \u21ba" if a.use_cyclic else ""
                    row = col.row(align=True)
                    op = row.operator("spriteloom.toggle_action",
                                      text=f"{a.name}  ({frame_count} frames){loop_suffix}",
                                      icon='CHECKBOX_HLT' if is_on else 'CHECKBOX_DEHLT',
                                      emboss=False)
                    op.action_name = a.name
                    nav = row.operator("spriteloom.focus_action", text="", icon='LINKED', emboss=False)
                    nav.action_name = a.name
                    if not a.use_fake_user:
                        warn = row.row()
                        warn.alert = True
                        warn.label(text="", icon='ERROR')
            elif all_actions:
                actions_box.label(text="All actions filtered out", icon='INFO')
            else:
                actions_box.label(text="No actions in file", icon='INFO')
        # --- Cloth Simulation ---
        box = layout.box()
        box.enabled = settings.armature is not None
        row = box.row()
        row.prop(settings, "show_cloth",
                 icon="TRIA_DOWN" if settings.show_cloth else "TRIA_RIGHT",
                 emboss=False, text="Cloth Simulation", icon_only=False)
        row.label(icon="MOD_CLOTH")
        if settings.show_cloth:
            box.prop(settings, "cloth_warmup_frames")
            combos = _get_cloth_combos(context)
            if not combos:
                box.label(text="No cloth objects in active view layers", icon='INFO')
            else:
                col = box.column(align=True)
                col.scale_y = 0.8
                MAX_ROWS = 10
                for obj, vl, action in combos[:MAX_ROWS]:
                    if settings.rotation_mode == "OBJECT":
                        directions = _get_directions(settings.num_directions)
                        baked = all(_is_combo_baked(vl.name, action.name, d[0]) for d in directions)
                    else:
                        baked = _is_combo_baked(vl.name, action.name)
                    row = col.row(align=True)
                    row.label(
                        text=f"{obj.name}  /  {vl.name}  /  {action.name}",
                        icon='CHECKMARK' if baked else 'BLANK1',
                    )
                    op = row.operator("spriteloom.bake_cloth", text="", icon='MOD_CLOTH', emboss=False)
                    op.replace_existing = True
                    op.obj_name = obj.name
                    op.vl_name = vl.name
                    op.action_name = action.name
                if len(combos) > MAX_ROWS:
                    col.label(text=f"+{len(combos) - MAX_ROWS} more…", icon='BLANK1')
                row = box.row(align=True)
                op = row.operator("spriteloom.bake_cloth", text="Bake Missing", icon='MOD_CLOTH')
                op.replace_existing = False
                op = row.operator("spriteloom.bake_cloth", text="Rebake All", icon='FILE_REFRESH')
                op.replace_existing = True
                row.operator("spriteloom.delete_bakes", text="Delete All", icon='TRASH')
                box.prop(settings, "rebake_on_render")
            if settings.progress and "Baking" in settings.progress:
                box.progress(factor=settings.progress_factor, type="BAR", text=settings.progress)

        # --- Camera Setup ---
        box = layout.box()
        row = box.row()
        row.prop(settings, "show_camera_setup",
                 icon="TRIA_DOWN" if settings.show_camera_setup else "TRIA_RIGHT",
                 emboss=False, text="Camera Setup", icon_only=False)
        row.operator("spriteloom.setup_camera", text="Apply", icon="CAMERA_DATA")
        if settings.show_camera_setup:
            world = context.scene.world
            g = world.spriteloom_global if world else None
            if g is None:
                box.label(text="Scene has no World — global camera settings unavailable", icon="ERROR")
            else:
                col = box.column(align=True)
                col.label(text="Global (all scenes):", icon="WORLD")
                row = col.row(align=True)
                row.prop(g, "cam_elevation")
                row.prop(g, "cam_distance")
                row = col.row(align=True)
                row.prop(g, "cam_clip_start")
                row.prop(g, "cam_clip_end")
                col.prop(g, "cam_units_per_pixel")
                col.separator()
                col.label(text="Per-scene:", icon="SCENE_DATA")
                col.prop(settings, "cam_sprite_size")
                col.prop(settings, "cam_z_offset")
                size = int(settings.cam_sprite_size)
                ortho_scale = size * g.cam_units_per_pixel
                col.label(text=f"ortho_scale = {ortho_scale:.4f}   ({size}px × {g.cam_units_per_pixel:.6f})")
            box.prop(settings, "cam_auto_apply")

        # --- Output Paths ---
        box = layout.box()
        row = box.row()
        row.prop(settings, "show_output", icon="TRIA_DOWN" if settings.show_output else "TRIA_RIGHT", emboss=False, text="Output", icon_only=False)
        row.label(icon="FILE_FOLDER")
        if settings.show_output:
            comp_groups = [ng for ng in bpy.data.node_groups if ng.type == 'COMPOSITING']
            comp_box = box.box()
            hrow = comp_box.row(align=False)
            hrow.label(text="Compositors", icon="NODE_COMPOSITING")
            rhs = hrow.row(align=True)
            rhs.alignment = 'RIGHT'
            if settings.compositors_prefix_is_default:
                rhs.label(text=scene.name)
            else:
                rhs.prop(settings, "compositors_prefix_filter", text="")
            rhs.prop(settings, "compositors_prefix_is_default", text="", toggle=True, icon='LINKED')
            rhs.prop(settings, "compositors_prefix_filter_enabled", text="", toggle=True, icon='FILTER')
            _inc_comps = _parse_include(settings.compositors_include)
            display_comps = _prefix_filtered(comp_groups, lambda ng: ng.name,
                settings.compositors_prefix_filter, settings.compositors_prefix_filter_enabled,
                settings.compositors_prefix_is_default, scene.name)
            if _inc_comps:
                display_names = {ng.name for ng in display_comps}
                display_comps = display_comps + [ng for ng in comp_groups if ng.name in _inc_comps and ng.name not in display_names]
            if display_comps:
                col = comp_box.column(align=True)
                col.scale_y = 0.75
                for ng in display_comps:
                    is_on = _inc_comps is None or ng.name in _inc_comps
                    row = col.row(align=True)
                    op = row.operator("spriteloom.toggle_compositor",
                                      text=ng.name,
                                      icon='CHECKBOX_HLT' if is_on else 'CHECKBOX_DEHLT',
                                      emboss=False)
                    op.compositor_name = ng.name
                    nav = row.operator("spriteloom.focus_compositor", text="", icon='LINKED', emboss=False)
                    nav.compositor_name = ng.name
                    if not ng.use_fake_user:
                        warn = row.row()
                        warn.alert = True
                        warn.label(text="", icon='ERROR')
            elif comp_groups:
                comp_box.label(text="All compositors filtered out", icon='INFO')
            else:
                comp_box.label(text="No COMPOSITING node groups found", icon='ERROR')

            comp_box.operator("spriteloom.sync_compositors", text="Sync Compositors", icon='FILE_REFRESH')

            box.prop(settings, "export_root")
            box.prop(settings, "spritesheet_root")
            box.prop(settings, "clean_output")
            box.prop(settings, "overwrite_frames")
            dir_fmt_row = box.row(align=True)
            dir_fmt_row.enabled = settings.rotation_rig is not None
            dir_fmt_row.prop(settings, "direction_format", expand=True)
            row = box.row()
            row.prop(settings, "render_normals")
            if settings.render_normals:
                row.prop(settings, "normal_tag", text="Tag")
                row.prop(settings, "normal_correct_rotation", text="Camera Space", toggle=True)
                row.prop(settings, "normal_unreal_export", text="Unreal Y-Flip", toggle=True)
            row = box.row()
            row.prop(settings, "render_depth")
            if settings.render_depth:
                row.prop(settings, "depth_tag", text="Tag")

        # --- Sheet Layout ---
        box = layout.box()
        row = box.row()
        row.prop(settings, "show_sheet_layout", icon="TRIA_DOWN" if settings.show_sheet_layout else "TRIA_RIGHT", emboss=False, text="Sheet Layout", icon_only=False)
        row.label(icon="IMAGE_DATA")
        if settings.show_sheet_layout:
            row = box.row(align=True)
            row.label(text="File splits:")
            row.prop_enum(settings, "split_axes", 'ACTION')
            row.prop_enum(settings, "split_axes", 'COMPOSITOR')
            row.prop_enum(settings, "split_axes", 'DIRECTION')
            row = box.row(align=True)
            row.label(text="Row splits:")
            row.prop_enum(settings, "row_split_axes", 'ACTION')
            row.prop_enum(settings, "row_split_axes", 'COMPOSITOR')
            row.prop_enum(settings, "row_split_axes", 'DIRECTION')

            box.separator(factor=0.5)
            row = box.row(align=True)
            row.prop(settings, "renumber_frames")
            sub = row.row(align=True)
            sub.prop(settings, "frame_num_padding")

            box.separator(factor=0.5)
            box.label(text="Name Format:")
            box.prop(settings, "sheet_name_format", text="")

            blendfile = os.path.splitext(os.path.basename(bpy.data.filepath))[0] if bpy.data.filepath else "untitled"
            _ex_inc = _parse_include(settings.actions_include)
            example_actions = [a.name for a in bpy.data.actions if _ex_inc is None or a.name in _ex_inc] or ["chr_walk", "chr_idle"]
            example_compositors = [ng.name for ng in _resolve_compositors(settings.compositors_include)] or ["compositor"]
            _ex_dirs = _get_directions(settings.num_directions)
            _fmt = settings.direction_format
            if _fmt == 'ANGLE':
                example_directions = [f"{int(round(math.degrees(a))):03d}" for _, a in _ex_dirs]
            elif _fmt == 'INDEX':
                example_directions = [f"{i:02d}" for i in range(len(_ex_dirs))]
            else:
                example_directions = [d[0] for d in _ex_dirs]
            seen = []
            for action in example_actions:
                for layer in example_compositors:
                    for direction in (example_directions if 'DIRECTION' in settings.split_axes else [""]):
                        key = RenderKey(
                            blendfile=blendfile,
                            action_name=action,
                            compositor_name=layer,
                            direction_name=direction,
                            scene_name=scene.name,
                        )
                        name = key.sheet_name(settings.sheet_name_format,
                                              split_axes=set(settings.split_axes))
                        if name not in seen:
                            seen.append(name)
            col = box.column(align=True)
            col.scale_y = 0.7
            for name in seen[:5]:
                col.label(text=f"{name}.png", icon='FILE_IMAGE')
            if len(seen) > 5:
                col.label(text=f"+{len(seen) - 5} more…", icon='BLANK1')

            box.separator(factor=0.3)
            box.label(text="Frame Name Format:")
            box.prop(settings, "frame_name_format", text="")

            if example_actions and example_compositors and example_directions:
                ex_key = RenderKey(
                    blendfile=blendfile,
                    action_name=example_actions[0],
                    compositor_name=example_compositors[0],
                    direction_name=example_directions[0],
                    scene_name=scene.name,
                )
                ex_frame = ex_key.frame_name(settings.frame_name_format, 1, padding=settings.frame_num_padding)
                col = box.column(align=True)
                col.scale_y = 0.7
                col.label(text=ex_frame, icon='FILE_IMAGE')

        # --- Validation warnings ---
        issues = []

        if settings.armature is None:
            if settings.rotation_rig is None and len(_resolve_compositors(settings.compositors_include)) <= 1:
                issues.append(("INFO", "No animated object — rendering 1 frame (no sprite sheet)"))
            else:
                issues.append(("INFO", "No animated object — rendering 1 frame per direction/compositor"))
        if settings.rotation_rig is None:
            issues.append(("INFO", "No rotation rig — rendering 1 direction"))

        if settings.armature is not None and bpy.data.actions:
            _included = _parse_include(settings.actions_include)
            chr_actions = [a for a in bpy.data.actions if _included is None or a.name in _included]
            if not chr_actions:
                issues.append(("INFO", "No actions checked — rendering 1 static frame per direction/compositor"))

        if not _resolve_compositors(settings.compositors_include):
            issues.append(("ERROR", "No compositor node groups to render"))

        issues.extend(_resolve_sockets(settings)[1])

        if not _resolve_path(settings.export_root):
            issues.append(("ERROR", "Export path is relative — save the .blend file first"))
            issues.append(("INFO", "Hint: or set an absolute Export Root path above"))

        if settings.render_normals:
            if not scene.use_nodes or scene.compositing_node_group is None:
                issues.append(("ERROR", "Normal maps require compositor nodes — enable 'Use Nodes' in the compositor"))
            elif _find_normal_output_node(scene.compositing_node_group) is None:
                issues.append(("ERROR", f"Normal maps require a File Output compositor node named \"{_NORMAL_OUTPUT_NODE_NAME}\""))

        if settings.render_depth:
            if not scene.use_nodes or scene.compositing_node_group is None:
                issues.append(("ERROR", "Depth maps require compositor nodes — enable 'Use Nodes' in the compositor"))
            elif _find_depth_output_node(scene.compositing_node_group) is None:
                issues.append(("ERROR", f"Depth maps require a File Output compositor node named \"{_DEPTH_OUTPUT_NODE_NAME}\""))

        if bpy.data.filepath:
            cloth_combos = _get_cloth_combos(context)
            if cloth_combos:
                # Coverage is measured from the .bphys files, not from is_baked. A slot
                # can report baked while holding a single frame — Blender then has
                # nothing to replay and re-simulates live, which was invisible here
                # before and quietly made every render non-reproducible.
                coverage = cache_report(context)
                empty = [v for v in coverage.values() if v["frames_on_disk"] == 0]
                partial = [v for v in coverage.values()
                           if v["frames_on_disk"] and not v["complete"]]
                if empty:
                    if settings.rebake_on_render:
                        issues.append(("INFO", f"Cloth: {len(empty)} combo(s) will be baked before render"))
                    else:
                        issues.append(("ERROR", f"Cloth: {len(empty)} combo(s) not baked — simulation plays live"))
                if partial:
                    worst = min(partial, key=lambda v: v["frames_on_disk"])
                    lying = sum(1 for v in partial if v["is_baked"])
                    # WARN, not ERROR: an incomplete cache makes the render
                    # non-reproducible but destroys nothing, so it must not disable the
                    # button the way a genuine misconfiguration does.
                    issues.append(("WARN",
                                   f"Cloth: {len(partial)} combo(s) have INCOMPLETE caches "
                                   f"(e.g. {worst['slot']}: {worst['frames_on_disk']}/"
                                   f"{worst['expected_frames']} frames)"))
                    if lying:
                        issues.append(("WARN",
                                       f"  …{lying} of those still report is_baked — they will "
                                       f"re-simulate live and are not reproducible"))
                    if not settings.rebake_on_render:
                        issues.append(("INFO", "  Enable 'Rebake On Render', or re-bake those combos"))

        # The one genuinely destructive combination in the tool: clean_output wipes the
        # export root while actions_include narrows what gets written back.
        _clean_casualties = {}
        if bpy.data.filepath:
            _export_for_check = _resolve_path(settings.export_root)
            if _export_for_check:
                _clean_casualties = _clean_output_casualties(context, _export_for_check)
        if _clean_casualties:
            _n, _listed = _describe_casualties(_clean_casualties)
            if not settings.allow_destructive_clean:
                issues.append(("ERROR",
                               f"'Clean Before Render' would DELETE {_n} frame(s) of "
                               f"{len(_clean_casualties)} unselected action(s)"))
                for _a, _c in sorted(_clean_casualties.items()):
                    issues.append(("ERROR", f"    lost: {_a} ({_c} frames)"))
                issues.append(("INFO", "Turn off Clean Before Render to keep them, or tick below"))
            else:
                issues.append(("INFO", f"Clean acknowledged — {_n} frame(s) will be deleted: {_listed}"))

        # --- Preview box ---
        layout.separator()
        vp_box = layout.box()
        vp_hdr = vp_box.row()
        vp_hdr.prop(settings, "show_preview",
                    icon="TRIA_DOWN" if settings.show_preview else "TRIA_RIGHT",
                    emboss=False, text="Preview", icon_only=False)
        vp_hdr.label(icon="RENDER_ANIMATION")

        if settings.show_preview:
            # Camera direction group
            if settings.rotation_rig:
                import math as _math
                dir_box = vp_box.box()
                dir_title = "Camera Direction" if settings.rotation_mode == "CAMERA" else "Object Direction"
                dir_box.label(text=dir_title, icon="ORIENTATION_VIEW")
                directions = _get_directions(settings.num_directions)
                cols = min(len(directions), 4)
                grid = dir_box.grid_flow(row_major=True, columns=cols, even_columns=True, even_rows=False, align=True)
                for name, angle in directions:
                    op = grid.operator("spriteloom.preview_direction", text=name)
                    op.angle = angle
                    op.label = name
                saved = settings.rotation_rig_saved_rotation
                rig_z = settings.rotation_rig.rotation_euler[2] if settings.rotation_rig else 0.0
                reset_row = dir_box.row()
                reset_row.enabled = not _math.isnan(saved) or abs(rig_z) > 1e-6
                reset_text = "Reset Camera" if settings.rotation_mode == "CAMERA" else "Reset Object"
                reset_row.operator("spriteloom.reset_camera_direction", text=reset_text, icon="LOOP_BACK")

            vp_armature = settings.armature
            vp_action = (
                vp_armature.animation_data.action
                if vp_armature and vp_armature.animation_data
                else None
            )
            vp_col = vp_box.column(align=True)
            if vp_action:
                vp_col.label(text=f"Action: {vp_action.name}", icon="ACTION")
                vp_col.label(
                    text=f"Frames: {int(vp_action.frame_range[0])}–{int(vp_action.frame_range[1])}",
                    icon="TIME",
                )
            else:
                vp_col.label(text="Action: (none active on animated object)", icon="ERROR")

            if settings.rotation_rig:
                dir_label = _get_direction_label(settings.rotation_rig.rotation_euler.z)
                mode_tag = " [Object]" if settings.rotation_mode == "OBJECT" else ""
                vp_col.label(text=f"Direction: {dir_label}{mode_tag}", icon="ORIENTATION_VIEW")
            else:
                vp_col.label(text="Direction: (no camera rig)", icon="ERROR")

            vp_row = vp_box.row()
            vp_row.enabled = vp_action is not None
            vp_row.operator("spriteloom.render_video_preview", icon="RENDER_ANIMATION")

        # --- Render box ---
        layout.separator()
        render_box = layout.box()
        render_hdr = render_box.row()
        render_hdr.prop(settings, "show_render",
                        icon="TRIA_DOWN" if settings.show_render else "TRIA_RIGHT",
                        emboss=False, text="Render", icon_only=False)
        render_hdr.label(icon="RENDERLAYERS")

        if settings.show_render:
            render_col = render_box.column(align=True)

            if issues:
                for icon, text in issues:
                    render_col.label(text=text, icon="ERROR" if icon == "WARN" else icon)

            if _clean_casualties:
                render_box.prop(settings, "allow_destructive_clean", toggle=False)

            if settings.progress:
                render_box.progress(factor=settings.progress_factor, type="BAR", text=settings.progress)
                render_box.label(text="Press ESC to cancel", icon="X")
            else:
                blocking = any(icon == "ERROR" for icon, _ in issues)
                row = render_box.row()
                row.enabled = not blocking
                row.operator("spriteloom.render_all", icon="RENDERLAYERS")

            if settings.last_result:
                for line in settings.last_result.split("\n"):
                    render_col.label(text=line)


# ---------------------------------------------------------------------------
# Video Preview Operator
# ---------------------------------------------------------------------------


class SPRITELOOM_OT_RenderVideoPreview(bpy.types.Operator):
    """Render the current active action at the current camera angle as a video, then open it"""

    bl_idname = "spriteloom.render_video_preview"
    bl_label = "Render Video Preview"
    bl_description = "Render the active action and current camera direction as an MP4, then open it"

    def execute(self, context):
        import subprocess, sys

        scene = context.scene
        settings = scene.spriteloom

        armature = settings.armature
        if not armature or not armature.animation_data or not armature.animation_data.action:
            self.report({"ERROR"}, "No active action on the armature")
            return {"CANCELLED"}

        action = armature.animation_data.action
        export_root = _resolve_path(settings.export_root)
        if not export_root:
            self.report({"ERROR"}, "Export Root path is not set or the .blend file is unsaved")
            return {"CANCELLED"}
        preview_dir = os.path.join(export_root, "previews")
        os.makedirs(preview_dir, exist_ok=True)
        video_path = os.path.join(preview_dir, f"{action.name}_preview.mp4")

        # Activate pre-baked cloth cache paths for this action
        saved_cloth = _activate_cloth_paths(action)

        # Save render state
        orig_filepath = scene.render.filepath
        orig_media_type = scene.render.image_settings.media_type
        orig_format = scene.render.image_settings.file_format
        orig_frame_start = scene.frame_start
        orig_frame_end = scene.frame_end
        orig_mode = context.mode

        if orig_mode != 'OBJECT':
            _log(f"Switching from '{orig_mode}' to OBJECT mode before preview render")
            bpy.ops.object.mode_set(mode='OBJECT')

        try:
            scene.frame_start = int(action.frame_range[0])
            scene.frame_end = int(action.frame_range[1])
            scene.render.filepath = video_path
            scene.render.image_settings.media_type = "VIDEO"
            scene.render.image_settings.file_format = "FFMPEG"
            scene.render.ffmpeg.format = "MPEG4"
            scene.render.ffmpeg.codec = "H264"
            scene.render.ffmpeg.constant_rate_factor = "HIGH"

            bpy.ops.render.render("EXEC_DEFAULT", animation=True)
        finally:
            scene.render.filepath = orig_filepath
            scene.render.image_settings.media_type = orig_media_type
            scene.render.image_settings.file_format = orig_format
            scene.frame_start = orig_frame_start
            scene.frame_end = orig_frame_end
            _restore_cloth_paths(saved_cloth)

        if os.path.exists(video_path):
            if sys.platform == "win32":
                subprocess.Popen(["start", "", video_path], shell=True)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", video_path])
            else:
                subprocess.Popen(["xdg-open", video_path])
            self.report({"INFO"}, f"Video saved: {video_path}")
        else:
            self.report({"WARNING"}, "Render finished but output file not found")

        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class SPRITELOOM_OT_FocusAction(bpy.types.Operator):
    """Set this action on the armature and switch to the Animation workspace"""
    bl_idname = "spriteloom.focus_action"
    bl_label = "Focus Action"

    action_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        action = bpy.data.actions.get(self.action_name)
        armature = context.scene.spriteloom.armature
        if not action:
            self.report({'WARNING'}, f"Action not found: {self.action_name}")
            return {'CANCELLED'}
        if not armature:
            self.report({'WARNING'}, "No armature set")
            return {'CANCELLED'}
        for obj in context.scene.objects:
            obj.select_set(False)
        armature.select_set(True)
        context.view_layer.objects.active = armature
        if not armature.animation_data:
            armature.animation_data_create()
        armature.animation_data.action = action
        ws = bpy.data.workspaces.get("Animation")
        if ws:
            context.window.workspace = ws
        return {'FINISHED'}


class SPRITELOOM_OT_FocusCompositor(bpy.types.Operator):
    """Set this compositor on the scene and switch to the Compositing workspace"""
    bl_idname = "spriteloom.focus_compositor"
    bl_label = "Focus Compositor"

    compositor_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        ng = bpy.data.node_groups.get(self.compositor_name)
        if not ng:
            self.report({'WARNING'}, f"Node group not found: {self.compositor_name}")
            return {'CANCELLED'}
        context.scene.compositing_node_group = ng
        ws = bpy.data.workspaces.get("Compositing")
        if ws:
            context.window.workspace = ws
        return {'FINISHED'}


class SPRITELOOM_OT_PreviewDirection(bpy.types.Operator):
    """Set rotation rig to preview a render direction"""
    bl_idname = "spriteloom.preview_direction"
    bl_label = "Preview Direction"

    angle: bpy.props.FloatProperty()  # type: ignore
    label: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        s = context.scene.spriteloom
        rig = s.rotation_rig
        if not rig:
            self.report({'ERROR'}, "No rotation rig set")
            return {'CANCELLED'}
        import math
        if math.isnan(s.rotation_rig_saved_rotation):
            s.rotation_rig_saved_rotation = rig.rotation_euler[2]
        if s.rotation_mode == "OBJECT":
            rig.rotation_euler[2] = -self.angle
        else:
            rig.rotation_euler[2] = self.angle
        return {'FINISHED'}


class SPRITELOOM_OT_ResetCameraDirection(bpy.types.Operator):
    """Restore rotation rig to its original rotation"""
    bl_idname = "spriteloom.reset_camera_direction"
    bl_label = "Reset"

    def execute(self, context):
        import math
        s = context.scene.spriteloom
        rig = s.rotation_rig
        if not rig:
            self.report({'ERROR'}, "No rotation rig set")
            return {'CANCELLED'}
        if not math.isnan(s.rotation_rig_saved_rotation):
            rig.rotation_euler[2] = s.rotation_rig_saved_rotation
            s.rotation_rig_saved_rotation = float('nan')
        else:
            rig.rotation_euler[2] = 0.0
        return {'FINISHED'}


class SPRITELOOM_OT_SetupCamera(bpy.types.Operator):
    """Apply SpriteLoom camera settings to the scene camera"""
    bl_idname = "spriteloom.setup_camera"
    bl_label = "Apply Camera Setup"

    def execute(self, context):
        if _find_scene_camera(context.scene) is None:
            self.report({'ERROR'}, "No camera object found in the scene")
            return {'CANCELLED'}
        if context.scene.world is None:
            self.report({'ERROR'}, "Scene has no World — camera geometry settings are stored on the World")
            return {'CANCELLED'}
        _apply_camera_setup(context.scene.spriteloom, context)
        s = context.scene.spriteloom
        g = context.scene.world.spriteloom_global
        size = int(s.cam_sprite_size)
        self.report({'INFO'}, f"Camera setup applied: {size}×{size}, ortho_scale={size * g.cam_units_per_pixel:.4f}")
        return {'FINISHED'}


class SPRITELOOM_UL_ExtraObjects(bpy.types.UIList):
    bl_idname = "SPRITELOOM_UL_extra_objects"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        layout.prop(item, "object", text="", emboss=False)


class SPRITELOOM_OT_AddExtraObject(bpy.types.Operator):
    """Add a slot to the extra animated objects list"""
    bl_idname = "spriteloom.add_extra_object"
    bl_label = "Add Extra Object"

    def execute(self, context):
        context.scene.spriteloom.extra_animated_objects.add()
        return {"FINISHED"}


class SPRITELOOM_OT_RemoveExtraObject(bpy.types.Operator):
    """Remove the selected slot from the extra animated objects list"""
    bl_idname = "spriteloom.remove_extra_object"
    bl_label = "Remove Extra Object"

    def execute(self, context):
        settings = context.scene.spriteloom
        idx = settings.active_extra_object_index
        if 0 <= idx < len(settings.extra_animated_objects):
            settings.extra_animated_objects.remove(idx)
            settings.active_extra_object_index = max(0, idx - 1)
        return {"FINISHED"}


class SPRITELOOM_UL_Sockets(bpy.types.UIList):
    bl_idname = "SPRITELOOM_UL_sockets"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        row = layout.row(align=True)
        name = _sanitize_socket_name(item.name)
        dupes = [s for s in data.sockets if _sanitize_socket_name(s.name) == name]
        bad = (not name) or len(dupes) > 1 or item.object is None
        sub = row.row(align=True)
        sub.alert = bad
        sub.prop(item, "name", text="", emboss=False, icon="EMPTY_ARROWS")
        row.prop(item, "object", text="", emboss=False)


class SPRITELOOM_OT_AddSocket(bpy.types.Operator):
    """Add a named attachment point exported per frame"""
    bl_idname = "spriteloom.add_socket"
    bl_label = "Add Socket"

    def execute(self, context):
        settings = context.scene.spriteloom
        item = settings.sockets.add()
        existing = {_sanitize_socket_name(s.name) for s in settings.sockets}
        n = len(settings.sockets)
        while f"Socket{n}" in existing:
            n += 1
        item.name = f"Socket{n}"
        settings.active_socket_index = len(settings.sockets) - 1
        return {"FINISHED"}


class SPRITELOOM_OT_RemoveSocket(bpy.types.Operator):
    """Remove the selected attachment point"""
    bl_idname = "spriteloom.remove_socket"
    bl_label = "Remove Socket"

    def execute(self, context):
        settings = context.scene.spriteloom
        idx = settings.active_socket_index
        if 0 <= idx < len(settings.sockets):
            settings.sockets.remove(idx)
            settings.active_socket_index = max(0, idx - 1)
        return {"FINISHED"}


class SPRITELOOM_OT_ToggleAction(bpy.types.Operator):
    """Toggle an action on/off for rendering"""
    bl_idname = "spriteloom.toggle_action"
    bl_label = "Toggle Action"

    action_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        settings = context.scene.spriteloom
        all_names = [a.name for a in bpy.data.actions]
        current = _parse_include(settings.actions_include)
        if current is None:
            # Was "all" — uncheck this one, include all others
            included = set(all_names) - {self.action_name}
        elif self.action_name in current:
            included = current - {self.action_name}
        else:
            included = current | {self.action_name}
        if included == set(all_names):
            settings.actions_include = ""          # all → clear to default
        elif not included:
            settings.actions_include = _FILTER_NONE  # none → sentinel
        else:
            settings.actions_include = ", ".join(n for n in all_names if n in included)
        return {'FINISHED'}


class SPRITELOOM_OT_ToggleCompositor(bpy.types.Operator):
    """Toggle a compositor node group on/off for rendering"""
    bl_idname = "spriteloom.toggle_compositor"
    bl_label = "Toggle Compositor"

    compositor_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        settings = context.scene.spriteloom
        all_names = [ng.name for ng in bpy.data.node_groups if ng.type == 'COMPOSITING']
        current = _parse_include(settings.compositors_include)
        if current is None:
            # Was "all" — uncheck this one, include all others
            included = set(all_names) - {self.compositor_name}
        elif self.compositor_name in current:
            included = current - {self.compositor_name}
        else:
            included = current | {self.compositor_name}
        if included == set(all_names):
            settings.compositors_include = ""          # all → clear to default
        elif not included:
            settings.compositors_include = _FILTER_NONE  # none → sentinel
        else:
            settings.compositors_include = ", ".join(n for n in all_names if n in included)
        return {'FINISHED'}


def _create_compositor_from_template(base_name, scene, view_layer_name=None):
    """Clone _Setup (and nested _Setup[Matte]) as `base_name`, targeting scene/view_layer_name.
    Returns the new main node group, or None if the _Setup template is missing."""
    template = bpy.data.node_groups.get('_Setup')
    if template is None:
        return None

    visited = {}
    cloned = _deep_clone_node_group(template, visited, {})

    for orig_ng, new_ng in visited.items():
        if orig_ng is template:
            new_ng.name = base_name
        elif orig_ng.name.startswith('_Setup'):
            new_ng.name = base_name + orig_ng.name[len('_Setup'):]
        else:
            new_ng.name = f"{base_name}_{orig_ng.name}"
        new_ng.use_fake_user = True

    for ng in visited.values():
        for node in ng.nodes:
            if node.type in ('CRYPTOMATTE', 'CRYPTOMATTE_V2', 'R_LAYERS') and hasattr(node, 'scene'):
                node.scene = scene
            if node.type == 'R_LAYERS' and view_layer_name:
                node.layer = view_layer_name
            if node.type == 'CRYPTOMATTE_V2' and view_layer_name:
                cur = node.layer_name
                pass_suffix = cur.split('.', 1)[1] if '.' in cur else 'CryptoMaterial'
                node.layer_name = f"{view_layer_name}.{pass_suffix}"

    return cloned


def _ensure_compositors_for_scene(scene):
    """Create any missing per-view-layer compositors for `scene` from _Setup. Returns count created."""
    view_layers = list(scene.view_layers)
    if not view_layers:
        return 0
    default_vl = view_layers[0]
    created = 0
    for vl in view_layers:
        is_default = vl == default_vl
        base_name = scene.name if is_default else f"{scene.name}.{vl.name}"
        if bpy.data.node_groups.get(base_name) is not None:
            continue
        new_ng = _create_compositor_from_template(base_name, scene, None if is_default else vl.name)
        if new_ng is not None:
            created += 1
    return created


def _sync_matte_subgraphs():
    """Rebuild all *[Matte] compositor groups from _Setup[Matte], preserving scene refs and matte node input overrides.
    Returns (updated_count, error_str_or_None)."""
    template = bpy.data.node_groups.get('_Setup[Matte]')
    if template is None:
        return 0, "_Setup[Matte] template not found"

    matte_groups = [
        ng for ng in bpy.data.node_groups
        if ng.type == 'COMPOSITING'
        and ng.name.endswith('[Matte]')
        and ng is not template
    ]

    updated = 0
    for old_ng in matte_groups:
        target_scene, target_layer = _resolve_compositor_scene_and_layer(old_ng.name)

        referencing_nodes = []
        for ng in bpy.data.node_groups:
            for node in ng.nodes:
                if node.type == 'GROUP' and node.node_tree is old_ng:
                    saved_defaults = {
                        sock.name: sock.default_value
                        for sock in node.inputs
                        if hasattr(sock, 'default_value')
                    }
                    referencing_nodes.append((node, saved_defaults))

        new_ng = template.copy()
        new_ng.use_fake_user = True

        for node in new_ng.nodes:
            if node.type in ('CRYPTOMATTE', 'CRYPTOMATTE_V2', 'R_LAYERS') and hasattr(node, 'scene'):
                if target_scene:
                    node.scene = target_scene
            if node.type == 'CRYPTOMATTE_V2' and target_layer:
                cur = node.layer_name
                pass_suffix = cur.split('.', 1)[1] if '.' in cur else 'CryptoMaterial'
                node.layer_name = f"{target_layer}.{pass_suffix}"

        for group_node, saved_defaults in referencing_nodes:
            group_node.node_tree = new_ng
            for sock in group_node.inputs:
                if sock.name in saved_defaults and hasattr(sock, 'default_value'):
                    sock.default_value = saved_defaults[sock.name]

        old_name = old_ng.name
        old_ng.name = old_name + '__old'
        new_ng.name = old_name
        bpy.data.node_groups.remove(old_ng)
        updated += 1

    return updated, None


def _sync_main_compositors():
    """Rebuild all main compositor groups from _Setup, preserving scene refs and Matte group exposure overrides.
    Returns (updated_count, error_str_or_None)."""
    template = bpy.data.node_groups.get('_Setup')
    if template is None:
        return 0, "_Setup template not found"

    checked = {
        ng
        for scene in bpy.data.scenes
        for ng in _resolve_compositors(scene.spriteloom.compositors_include)
    }
    main_groups = [
        ng for ng in checked
        if not ng.name.endswith('[Matte]')
        and ng is not template
    ]

    updated = 0
    for old_ng in main_groups:
        target_scene, target_layer = _resolve_compositor_scene_and_layer(old_ng.name)

        # Find GROUP node referencing a *Matte group — save its node_tree and exposure defaults
        saved_matte_ng = None
        saved_defaults = {}
        for node in old_ng.nodes:
            if node.type == 'GROUP' and node.node_tree and node.node_tree.name.endswith('[Matte]'):
                saved_matte_ng = node.node_tree
                saved_defaults = {
                    sock.name: sock.default_value
                    for sock in node.inputs
                    if hasattr(sock, 'default_value')
                }
                break

        new_ng = template.copy()
        new_ng.use_fake_user = True

        if target_scene:
            for node in new_ng.nodes:
                if node.type in ('R_LAYERS', 'CRYPTOMATTE', 'CRYPTOMATTE_V2') and hasattr(node, 'scene'):
                    node.scene = target_scene
                if node.type == 'R_LAYERS' and target_layer:
                    node.layer = target_layer
                if node.type == 'CRYPTOMATTE_V2' and target_layer:
                    cur = node.layer_name
                    pass_suffix = cur.split('.', 1)[1] if '.' in cur else 'CryptoMaterial'
                    node.layer_name = f"{target_layer}.{pass_suffix}"

        # Redirect the _SetupMatte GROUP node to the saved Matte group and restore exposures
        if saved_matte_ng:
            for node in new_ng.nodes:
                if node.type == 'GROUP' and node.node_tree and node.node_tree.name == '_Setup[Matte]':
                    node.node_tree = saved_matte_ng
                    for sock in node.inputs:
                        if sock.name in saved_defaults and hasattr(sock, 'default_value'):
                            sock.default_value = saved_defaults[sock.name]
                    break

        for scene in bpy.data.scenes:
            if scene.compositing_node_group is old_ng:
                scene.compositing_node_group = new_ng

        old_name = old_ng.name
        old_ng.name = old_name + '__old'
        new_ng.name = old_name
        bpy.data.node_groups.remove(old_ng)
        updated += 1

    return updated, None


class SPRITELOOM_OT_SyncCompositors(bpy.types.Operator):
    """Ensure compositors exist for the current scene and its view layers, then sync all from _Setup/_Setup[Matte]"""
    bl_idname = "spriteloom.sync_compositors"
    bl_label = "Sync Compositors"
    bl_description = "Create any missing compositors for this scene's view layers, then rebuild all compositors from _Setup/_Setup[Matte]"

    def execute(self, context):
        created = _ensure_compositors_for_scene(context.scene)

        matte_updated, matte_err = _sync_matte_subgraphs()
        if matte_err:
            self.report({'ERROR'}, matte_err)
            return {'CANCELLED'}

        main_updated, main_err = _sync_main_compositors()
        if main_err:
            self.report({'ERROR'}, main_err)
            return {'CANCELLED'}

        self.report({'INFO'}, f"Created {created}, synced {main_updated} compositor(s) and {matte_updated} matte(s)")
        return {'FINISHED'}


_classes = (
    SpriteLoomAnimatedObject,
    SpriteLoomSocket,
    SpriteLoomGlobalSettings,
    SpriteLoomSettings,
    SPRITELOOM_UL_ExtraObjects,
    SPRITELOOM_UL_Sockets,
    SPRITELOOM_OT_RenderAll,
    SPRITELOOM_OT_RenderVideoPreview,
    SPRITELOOM_OT_FocusAction,
    SPRITELOOM_OT_AddExtraObject,
    SPRITELOOM_OT_RemoveExtraObject,
    SPRITELOOM_OT_AddSocket,
    SPRITELOOM_OT_RemoveSocket,
    SPRITELOOM_OT_ToggleAction,
    SPRITELOOM_OT_ToggleCompositor,
    SPRITELOOM_OT_SyncCompositors,
    SPRITELOOM_OT_FocusCompositor,
    SPRITELOOM_OT_PreviewDirection,
    SPRITELOOM_OT_ResetCameraDirection,
    SPRITELOOM_OT_SetupCamera,
    SPRITELOOM_OT_BakeCloth,
    SPRITELOOM_OT_DeleteBakes,
    SPRITELOOM_PT_Main,
)


def _auto_detect_all():
    for scene in bpy.data.scenes:
        _auto_detect(scene)
    return None  # don't repeat


def _auto_detect(scene):
    """Auto-fill armature and rotation_rig from scene objects if not already set."""
    settings = scene.spriteloom
    if settings.armature is None:
        armatures = [o for o in scene.objects if o.type == 'ARMATURE']
        if len(armatures) == 1:
            settings.armature = armatures[0]
        elif armatures:
            by_name = {o.name.lower(): o for o in armatures}
            for name in ("rig", "armature", "metarig"):
                if name in by_name:
                    settings.armature = by_name[name]
                    break
        if settings.armature is None:
            animated = [o for o in scene.objects if o.animation_data and o.animation_data.action]
            if len(animated) == 1:
                settings.armature = animated[0]

    if settings.rotation_rig is None:
        cameras = [o for o in scene.objects if o.type == 'CAMERA']
        if scene.camera:
            cameras = [scene.camera] + [c for c in cameras if c is not scene.camera]
        for cam in cameras:
            if cam.parent and cam.parent.type == 'EMPTY':
                settings.rotation_rig = cam.parent
                break


@bpy.app.handlers.persistent
def _set_default_armature(_):
    for scene in bpy.data.scenes:
        settings = scene.spriteloom
        settings.progress = ""
        settings.progress_factor = 0.0
        _auto_detect(scene)


def _purge_render_handlers():
    """Remove any leftover SpriteLoom render handlers (e.g. from a failed previous run)."""
    for handler_list in (bpy.app.handlers.render_complete, bpy.app.handlers.render_cancel):
        for h in list(handler_list):
            if getattr(h, "__qualname__", "").startswith("SPRITELOOM_OT_RenderAll"):
                handler_list.remove(h)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.spriteloom = bpy.props.PointerProperty(type=SpriteLoomSettings)
    bpy.types.World.spriteloom_global = bpy.props.PointerProperty(type=SpriteLoomGlobalSettings)
    bpy.app.handlers.load_post.append(_set_default_armature)
    _purge_render_handlers()
    # Also apply to any scenes already open (skipped during install when bpy.data is restricted)
    try:
        _set_default_armature(None)
    except AttributeError:
        pass


def unregister():
    _purge_render_handlers()
    bpy.app.handlers.load_post.remove(_set_default_armature)
    del bpy.types.Scene.spriteloom
    del bpy.types.World.spriteloom_global
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
