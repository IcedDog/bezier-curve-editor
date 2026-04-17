import bpy
import gpu
import blf
import math
import time
import os
import json
from gpu_extras.batch import batch_for_shader
from .translation import translations_dict

from .config import (
    ADDON_KEY,
    INFO_ADDON_KEY,
    GRAPH_ADDON_KEY,
    EDITOR_UI_KEY,
    ENABLED_AREAS_KEY,
    PRESET_FILE,
    HANDLE_HIT_RADIUS_PX,
    SIDEBAR_EDGE_HIT_PX,
    REDRAW_LOAD_EWMA_KEY,
    REDRAW_LAST_TICK_KEY,
    REDRAW_LAST_INTERVAL_KEY,
    MODAL_SESSION_KEY,
    SWITCH_BLOCK_UNTIL_KEY,
    TLFC_PROPERTY_DEFAULTS,
    TLFC_PROPERTY_RANGES,
    TLFC_UI_NUMBERS,
    TLFC_COLORS,
)

_PRESET_CACHE = []
_PRESET_MTIME = -1.0
ADDON_MODULE_KEY = __package__ or __name__.split(".")[0]


def _prop_default(name):
    return TLFC_PROPERTY_DEFAULTS[name]


def _prop_range(name):
    return TLFC_PROPERTY_RANGES[name]


def _clamp_prop(name, value):
    min_v, max_v = _prop_range(name)
    out = value
    if min_v is not None:
        out = max(min_v, out)
    if max_v is not None:
        out = min(max_v, out)
    return out


def _prop_to_unit(name, value):
    """Map a property value to normalized 0..1 space based on configured range."""
    min_v, max_v = _prop_range(name)
    if min_v is None or max_v is None:
        return _clamp01(float(value))
    span = max(1e-8, float(max_v) - float(min_v))
    return _clamp01((float(value) - float(min_v)) / span)


def _unit_to_prop(name, unit_value):
    """Map normalized 0..1 value back to configured property range."""
    min_v, max_v = _prop_range(name)
    u = _clamp01(float(unit_value))
    if min_v is None or max_v is None:
        return _clamp_prop(name, u)
    return _clamp_prop(name, float(min_v) + u * (float(max_v) - float(min_v)))


def _reset_ui_state(wm, *, clear_drag=True, clear_hover=True, clear_buttons=True, clear_handle=True):
    if clear_hover:
        wm.tlfc_hover_sidebar = False
        wm.tlfc_hover_sidebar_edge = False
        wm.tlfc_hover_graph_sep = False
    if clear_drag:
        wm.tlfc_dragging_sidebar = False
    if clear_buttons:
        wm.tlfc_hover_button = ""
        wm.tlfc_pressed_button = ""
    if clear_handle:
        wm.tlfc_hover_handle = ""


def _addon_prefs():
    addons = bpy.context.preferences.addons
    addon = addons.get(ADDON_MODULE_KEY)
    return addon.preferences if addon else None


def _pref_bool(name, default=False):
    prefs = _addon_prefs()
    if prefs is None:
        return bool(default)
    return bool(getattr(prefs, name, default))


def _pref_float(name, default=0.0):
    prefs = _addon_prefs()
    if prefs is None:
        return float(default)
    return float(getattr(prefs, name, default))


def _sync_graph_height_to_prefs(self, context):
    """Update callback: persist graph height ratio to addon preferences."""
    prefs = _addon_prefs()
    if prefs is not None:
        prefs.tlfc_graph_height_ratio = self.tlfc_graph_height_ratio


def _set_editor_enabled_exclusive(area):
    key = _area_key(area)
    if key == 0:
        return
    data = _enabled_areas_map()
    # Keep only the requested area enabled to avoid conflicting modal/UI state.
    for k in list(data.keys()):
        if k != key:
            data.pop(k, None)
    data[key] = True

def _tag_redraw_dopesheet():
    try:
        for w in bpy.data.window_managers:
            for win in w.windows:
                scr = win.screen
                if not scr:
                    continue
                for area in scr.areas:
                    if area.type in {'DOPESHEET_EDITOR', 'INFO', 'GRAPH_EDITOR'}:
                        area.tag_redraw()
    except Exception:
        pass


def _supports_editor_space(space):
    if not space:
        return False
    return space.type in {'DOPESHEET_EDITOR', 'INFO', 'GRAPH_EDITOR'}


def _enabled_areas_map():
    ns = bpy.app.driver_namespace
    data = ns.get(ENABLED_AREAS_KEY)
    if not isinstance(data, dict):
        data = {}
        ns[ENABLED_AREAS_KEY] = data
    return data


def _area_key(area):
    return int(area.as_pointer()) if area else 0


def _find_area_by_ptr(area_ptr):
    if not area_ptr:
        return None
    try:
        for w in bpy.data.window_managers:
            for win in w.windows:
                scr = win.screen
                if not scr:
                    continue
                for area in scr.areas:
                    if _area_key(area) == int(area_ptr):
                        return area
    except Exception:
        return None
    return None


def _is_editor_enabled(area):
    key = _area_key(area)
    if key == 0:
        return False
    return bool(_enabled_areas_map().get(key, False))


def _set_editor_enabled(area, enabled):
    key = _area_key(area)
    if key == 0:
        return
    data = _enabled_areas_map()
    if enabled:
        data[key] = True
    else:
        data.pop(key, None)


def _any_timeline_editor_enabled():
    try:
        for w in bpy.data.window_managers:
            for win in w.windows:
                scr = win.screen
                if not scr:
                    continue
                for area in scr.areas:
                    if area.type not in {'DOPESHEET_EDITOR', 'INFO', 'GRAPH_EDITOR'}:
                        continue
                    sp = area.spaces.active
                    if not _supports_editor_space(sp):
                        continue
                    if _is_editor_enabled(area):
                        return True
    except Exception:
        pass
    return False


def _prune_invalid_enabled_areas():
    """Remove enabled-area entries that no longer map to a supported editor space."""
    data = _enabled_areas_map()
    if not data:
        return False

    valid_keys = set()
    try:
        for w in bpy.data.window_managers:
            for win in w.windows:
                scr = win.screen
                if not scr:
                    continue
                for area in scr.areas:
                    if area.type not in {'DOPESHEET_EDITOR', 'INFO', 'GRAPH_EDITOR'}:
                        continue
                    sp = area.spaces.active
                    if not _supports_editor_space(sp):
                        continue
                    valid_keys.add(_area_key(area))
    except Exception:
        return False

    changed = False
    for key in list(data.keys()):
        if key not in valid_keys:
            data.pop(key, None)
            changed = True
    return changed


def _truncate_text_to_width(text, max_width, size=10):
    if max_width <= 1.0:
        return ""
    font_id = 0
    blf.size(font_id, size)
    if blf.dimensions(font_id, text)[0] <= max_width:
        return text

    ellipsis = "..."
    ell_w = blf.dimensions(font_id, ellipsis)[0]
    if ell_w >= max_width:
        return ""

    lo = 0
    hi = len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = text[:mid] + ellipsis
        if blf.dimensions(font_id, candidate)[0] <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ellipsis


def _draw_text_centered(x0, y0, x1, y1, text, size=10, color=TLFC_COLORS["white"], truncate=False, pad=4):
    font_id = 0
    if truncate:
        text = _truncate_text_to_width(str(text), max(1.0, (x1 - x0) - (pad * 2.0)), size=size)
        if not text:
            return
    blf.size(font_id, size)
    tw, th = blf.dimensions(font_id, text)
    tx = x0 + ((x1 - x0) - tw) * 0.5
    ty = y0 + ((y1 - y0) - th) * 0.5 + 1.0
    _draw_text(tx, ty, text, size=size, color=color)


def _t(wm, key, default=None):
    del wm
    msgid = str(default if default is not None else key)
    return bpy.app.translations.pgettext(msgid)




def _load_presets(force=False):
    global _PRESET_CACHE, _PRESET_MTIME
    path = os.path.join(bpy.utils.extension_path_user(ADDON_MODULE_KEY, create=True), PRESET_FILE)
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        _PRESET_CACHE = []
        _PRESET_MTIME = -1.0
        return []

    if not force and mtime == _PRESET_MTIME:
        return _PRESET_CACHE

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        presets = []
        for p in data if isinstance(data, list) else []:
            preset = {
                "name": str(p.get("name", "Preset")),
                "type": str(p.get("type", "BEZIER")),  # Default to BEZIER for backwards compatibility
            }
            # Load type-specific parameters
            if preset["type"] == "ELASTIC":
                preset["amplitude"] = float(p.get("amplitude", _prop_default("tlfc_elastic_amplitude")))
                preset["period"] = float(p.get("period", _prop_default("tlfc_elastic_period")))
            else:
                # BEZIER or fallback
                preset["h1x"] = float(p.get("h1x", _prop_default("tlfc_h1x")))
                preset["h1y"] = float(p.get("h1y", _prop_default("tlfc_h1y")))
                preset["h2x"] = float(p.get("h2x", _prop_default("tlfc_h2x")))
                preset["h2y"] = float(p.get("h2y", _prop_default("tlfc_h2y")))
            presets.append(preset)
        _PRESET_CACHE = presets
        _PRESET_MTIME = mtime
        return presets
    except Exception:
        _PRESET_CACHE = []
        _PRESET_MTIME = mtime
        return []


def _save_presets(presets):
    path = os.path.join(bpy.utils.extension_path_user(ADDON_MODULE_KEY, create=True), PRESET_FILE)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(presets, f, indent=2)
    except Exception:
        return False
    _load_presets(force=True)
    return True


def _add_current_preset(wm):
    presets = list(_load_presets())
    _mode_val = getattr(wm, 'tlfc_sidebar_mode', _prop_default("tlfc_sidebar_mode"))
    preset = {
        "name": "Curve",
        "type": _mode_val,  # 'BEZIER' or 'ELASTIC'
    }
    if _mode_val == 'ELASTIC':
        preset["amplitude"] = float(getattr(wm, 'tlfc_elastic_amplitude', _prop_default("tlfc_elastic_amplitude")))
        preset["period"] = float(getattr(wm, 'tlfc_elastic_period', _prop_default("tlfc_elastic_period")))
    else:
        preset["h1x"] = float(wm.tlfc_h1x)
        preset["h1y"] = float(wm.tlfc_h1y)
        preset["h2x"] = float(wm.tlfc_h2x)
        preset["h2y"] = float(wm.tlfc_h2y)
    presets.append(preset)
    return _save_presets(presets)


def _apply_preset_index(wm, idx):
    presets = _load_presets()
    if idx < 0 or idx >= len(presets):
        return False
    p = presets[idx]

    # Switch to appropriate mode
    preset_type = p.get("type", _prop_default("tlfc_sidebar_mode"))
    wm.tlfc_sidebar_mode = preset_type

    if preset_type == "ELASTIC":
        wm.tlfc_elastic_amplitude = p.get("amplitude", _prop_default("tlfc_elastic_amplitude"))
        wm.tlfc_elastic_period = p.get("period", _prop_default("tlfc_elastic_period"))
    else:
        wm.tlfc_h1x = p.get("h1x", _prop_default("tlfc_h1x"))
        wm.tlfc_h1y = p.get("h1y", _prop_default("tlfc_h1y"))
        wm.tlfc_h2x = p.get("h2x", _prop_default("tlfc_h2x"))
        wm.tlfc_h2y = p.get("h2y", _prop_default("tlfc_h2y"))
    return True


def _delete_preset_index(idx):
    presets = list(_load_presets())
    if idx < 0 or idx >= len(presets):
        return False
    presets.pop(idx)
    return _save_presets(presets)


def _draw_preset_tile(x0, y0, x1, y1, preset, size_scale, colors=None):
    C = colors if colors is not None else TLFC_COLORS
    _draw_rect(x0, y0, x1, y1, C["preset_tile_bg"])
    _draw_aa_line_strip([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], C["preset_tile_border"], width=2.0)
    pad = max(4, int(5 * size_scale))
    ix0 = x0 + pad
    iy0 = y0 + pad + 10
    ix1 = x1 - pad
    iy1 = y1 - pad
    _draw_rect(ix0, iy0, ix1, iy1, C["preset_preview_bg"])
    _draw_aa_line_strip([(ix0, iy0), (ix1, iy0), (ix1, iy1), (ix0, iy1), (ix0, iy0)], C["preset_preview_border"], width=2.0)

    preset_type = preset.get("type", "BEZIER")
    pts = []
    preview_steps = 32

    if preset_type == "ELASTIC":
        amplitude = preset.get("amplitude", _prop_default("tlfc_elastic_amplitude"))
        period = preset.get("period", _prop_default("tlfc_elastic_period"))
        for i in range(preview_steps):
            t = i / (preview_steps - 1.0)
            bx = t
            by = _elastic_ease_out_normalized(t, amplitude, period)
            by_clamped = max(0.0, min(1.0, by / 2.0))
            pts.append((ix0 + bx * (ix1 - ix0), iy0 + by_clamped * (iy1 - iy0)))
    else:
        h1x = max(0.0, float(preset.get("h1x", _prop_default("tlfc_h1x"))))
        h1y = float(preset.get("h1y", _prop_default("tlfc_h1y")))
        h2x = min(1.0, float(preset.get("h2x", _prop_default("tlfc_h2x"))))
        h2y = float(preset.get("h2y", _prop_default("tlfc_h2y")))
        p0 = (0.0, 0.0)
        p1 = (h1x, h1y)
        p2 = (h2x, h2y)
        p3 = (1.0, 1.0)
        for i in range(preview_steps):
            t = i / (preview_steps - 1.0)
            bx, by = _bezier_point(t, p0, p1, p2, p3)
            pts.append((ix0 + bx * (ix1 - ix0), iy0 + by * (iy1 - iy0)))

    _draw_aa_line_strip(pts, C["curve_orange"], width=2.0)
    _draw_text_centered(x0, y0, x1, y0 + 12, preset.get("name", "P"), size=max(8, int(9 * size_scale)), color=C["preset_title_text"], truncate=True, pad=4)

# ---------- Drawing helpers ----------
def _draw_rect(x0, y0, x1, y1, color):
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    verts = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
    batch = batch_for_shader(shader, 'TRI_STRIP', {"pos": verts})
    shader.bind()
    shader.uniform_float("color", color)
    gpu.state.blend_set('ALPHA')
    batch.draw(shader)
    gpu.state.blend_set('NONE')


def _draw_aa_line_strip(points, color, width=1.0):
    if len(points) < 2:
        return
    try:
        vp = gpu.state.viewport_get()
        if len(vp) >= 4:
            viewport_size = (float(vp[2]), float(vp[3]))
        else:
            viewport_size = (1920.0, 1080.0)
    except Exception:
        viewport_size = (1920.0, 1080.0)

    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    batch = batch_for_shader(shader, 'LINE_STRIP', {"pos": points})
    shader.bind()
    shader.uniform_float("viewportSize", viewport_size)
    shader.uniform_float("lineWidth", max(1.0, float(width)))
    shader.uniform_float("color", color)
    gpu.state.blend_set('ALPHA')
    batch.draw(shader)
    gpu.state.blend_set('NONE')


def _draw_filled_circle(x, y, r, color, steps=24):
    ns = bpy.app.driver_namespace
    cos_fn = ns.get('_tlfc_cos', math.cos)
    sin_fn = ns.get('_tlfc_sin', math.sin)
    verts = [(x, y)]
    for i in range(steps + 1):
        t = (i / steps) * 6.28318530718
        verts.append((
            x + r * cos_fn(t),
            y + r * sin_fn(t),
        ))
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    batch = batch_for_shader(shader, 'TRI_FAN', {"pos": verts})
    shader.bind()
    shader.uniform_float("color", color)
    gpu.state.blend_set('ALPHA')
    batch.draw(shader)
    gpu.state.blend_set('NONE')


def _draw_circle(x, y, r, color, steps=24, width=1.0):
    ns = bpy.app.driver_namespace
    cos_fn = ns.get('_tlfc_cos', math.cos)
    sin_fn = ns.get('_tlfc_sin', math.sin)
    pts = []
    for i in range(steps + 1):
        t = (i / steps) * 6.28318530718
        pts.append((
            x + r * cos_fn(t),
            y + r * sin_fn(t),
        ))
    _draw_aa_line_strip(pts, color, width=width)


def _draw_aa_circle(x, y, r, fill_color, outline_color, steps=28):
    # Keep original perceived thickness while improving edge quality.
    step_count = max(24, int(steps))
    _draw_filled_circle(x, y, max(1.0, r - 0.8), fill_color, steps=step_count)
    _draw_circle(x, y, r, outline_color, steps=step_count, width=2.0)


def _adjust_rgba(color, delta):
    return (
        _clamp01(color[0] + delta),
        _clamp01(color[1] + delta),
        _clamp01(color[2] + delta),
        color[3],
    )


def _button_state_colors(kind, state, colors=None):
    C = colors if colors is not None else TLFC_COLORS
    if kind == "apply":
        base = C["button_apply_base"]
        border = C["button_apply_border"]
    elif kind == "auto_on":
        base = C["button_auto_on_base"]
        border = C["button_auto_on_border"]
    elif kind == "auto_off":
        base = C["button_auto_off_base"]
        border = C["button_auto_off_border"]
    elif kind == "preset":
        base = C["button_preset_base"]
        border = C["button_preset_border"]
    else:
        base = C["button_default_base"]
        border = C["button_default_border"]

    if state == "hover":
        return _adjust_rgba(base, 0.08), _adjust_rgba(border, 0.07), C["white"]
    if state == "pressed":
        return _adjust_rgba(base, -0.08), _adjust_rgba(border, -0.05), C["text_pressed"]
    return base, border, C["text_default"]


def _draw_text_clipped_left(x, y, max_width, text, size=10, color=TLFC_COLORS["white"], pad=0):
    clipped = _truncate_text_to_width(str(text), max(1.0, max_width - pad), size=size)
    if not clipped:
        return
    _draw_text(x, y, clipped, size=size, color=color)


def _button_token(op, kwargs):
    if op == "zoom":
        return f"zoom:{kwargs.get('mode', 'CENTER')}"
    if op == "interp":
        return f"interp:{kwargs.get('mode', 'LINEAR')}"
    if op == "preset_apply":
        return f"preset:{int(kwargs.get('idx', -1))}"
    if op == "set_mode":
        return f"set_mode:{kwargs.get('mode', 'BEZIER')}"
    return op


def _theme_colors():
    """Read colors from the active Blender theme, returning a dict with the same
    keys as TLFC_COLORS. Falls back to TLFC_COLORS values for any slot that
    cannot be read from the theme."""
    colors = dict(TLFC_COLORS)

    try:
        theme = bpy.context.preferences.themes[0]
        ui = theme.user_interface
    except Exception as e:
        print(f"[TLFC] Failed to access theme: {e}")
        return colors

    def _rgba(value, alpha=1.0):
        if value is None:
            return None
        try:
            seq = tuple(float(v) for v in value)
        except Exception:
            try:
                seq = (float(value.r), float(value.g), float(value.b))
            except Exception:
                return None
        if len(seq) < 3:
            return None
        a = float(seq[3]) if len(seq) > 3 else float(alpha)
        return (seq[0], seq[1], seq[2], a)

    def _alpha(c, a):
        return (c[0], c[1], c[2], float(a)) if c else None

    def _shade(c, f):
        return (c[0] * f, c[1] * f, c[2] * f, c[3]) if c else None

    def _pick(path, alpha=1.0):
        obj = theme
        for part in path.split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                return None
        return _rgba(obj, alpha)
    
    reg_inner = _pick("user_interface.wcol_regular.inner")
    reg_inner_sel = _pick("user_interface.wcol_regular.inner_sel")
    reg_text = _pick("user_interface.wcol_regular.text")
    reg_text_sel = _pick("user_interface.wcol_regular.text_sel")
    reg_outline = _pick("user_interface.wcol_regular.outline")
    reg_outline_sel = _pick("user_interface.wcol_regular.outline_sel") 
    
    tab_inner = _pick("user_interface.wcol_tab.inner")
    tab_inner_sel = _pick("user_interface.wcol_tab.inner_sel")
    tab_text = _pick("user_interface.wcol_tab.text")
    tab_text_sel = _pick("user_interface.wcol_tab.text_sel")
    tab_outline = _pick("user_interface.wcol_tab.outline")
    
    state_overridden = _pick("user_interface.wcol_state.inner_overridden")
    state_changed = _pick("user_interface.wcol_state.inner_changed")
    
    menubg_inner = _pick("user_interface.wcol_menu_back.inner")    
    menu_inner = _pick("user_interface.wcol_menu.inner")
    menu_outline = _pick("user_interface.wcol_menu.outline")
    menu_text = _pick("user_interface.wcol_menu.text")

    curve_selected = _pick("common.anim.keyframe_selected")

    palette = {
        "white": _alpha(reg_text_sel, 1.0),
        "text_default": _alpha(reg_text, 1.0),
        "text_pressed": _alpha(reg_text_sel, 0.9),
        "preset_tile_bg": _alpha(reg_inner, 0.92),
        "preset_tile_border": _alpha(reg_outline, 0.95),
        "preset_preview_bg": _alpha(tab_inner, 0.8),
        "preset_preview_border": _alpha(tab_outline, 0.8),
        "curve_orange": _alpha(curve_selected, 1.0),
        "preset_title_text": _alpha(reg_text, 0.95),
        "button_apply_base": _alpha(reg_inner_sel, 0.95),
        "button_apply_border": _alpha(reg_outline_sel, 0.98),
        "button_auto_on_base": _alpha(reg_inner_sel, 0.95),
        "button_auto_on_border": _alpha(reg_outline_sel, 0.98),
        "button_auto_off_base": _alpha(reg_inner, 0.92),
        "button_auto_off_border": _alpha(reg_outline, 0.95),
        "button_preset_base": _alpha(tab_inner or reg_inner, 0.9),
        "button_preset_border": _alpha(tab_outline or reg_outline, 0.95),
        "button_default_base": _alpha(reg_inner, 0.88),
        "button_default_border": _alpha(reg_outline, 0.95),
        "graph_bg": _alpha(menubg_inner, 0.85),
        "tab_active_bg": _alpha(tab_inner_sel, 0.95),
        "tab_inactive_bg": _alpha(tab_inner, 0.88),
        "tab_active_text": _alpha(tab_text_sel, 1.0),
        "tab_inactive_text": _alpha(tab_text, 1.0),
        "tab_inactive_hover_text": _alpha(tab_text_sel, 1.0),
        "tab_active_outline": _alpha(tab_outline, 0.9),
        "grid_axis": _alpha(menu_outline, 1.0),
        "grid_boundary": _alpha(menu_outline, 0.9),
        "grid_regular": _alpha(menu_outline, 0.65),
        "elastic_curve": _alpha(curve_selected, 1.0),
        "endpoint_fill": _alpha(reg_inner, 1.0),
        "endpoint_outline": _alpha(reg_text, 0.6),
        "elastic_amp_guide": _alpha(state_overridden, 0.18),
        "elastic_per_guide": _alpha(state_changed, 0.18),
        "handle_amp_fill": _alpha(state_overridden, 1.0),
        "handle_per_fill": _alpha(state_changed, 1.0),
        "handle_outline": _alpha(reg_text, 1.0),
        "elastic_amp_label": _alpha(state_overridden, 0.9),
        "elastic_per_label": _alpha(state_changed, 0.9),
        "handle_line": _alpha(reg_text, 0.85),
        "bezier_h1_label": _alpha(state_overridden, 0.75),
        "bezier_h2_label": _alpha(state_changed, 0.75),
        "panel_bg": _alpha(menu_inner, 1.0),
        "panel_border": _alpha(menu_inner, 1.0),
        "panel_border_hover": _alpha(menu_outline, 1.0),
        "info_text": _alpha(menu_text, 1.0),
        "info_footer_text": _alpha(menu_text, 0.85),
        "info_empty_text": _alpha(menu_text, 0.8),
    }

    for key in TLFC_COLORS.keys():
        val = palette.get(key)
        if val is not None:
            colors[key] = val
    return colors


def _elastic_ease_out_normalized(t, amplitude=1.0, period=0.3):
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    per = max(0.001, float(period))
    amp_in = max(0.0, float(amplitude))

    time = -float(t)
    f = 1.0

    if amp_in < 1.0:
        s = per / 4.0
        blend_t = abs(s)
        if amp_in > 0.0:
            f *= amp_in
        else:
            f = 0.0
        if blend_t > 1e-8 and abs(time) < blend_t:
            l = abs(time) / blend_t
            f = (f * l) + (1.0 - l)
        amp = 1.0
    else:
        amp = amp_in
        asin_arg = max(-1.0, min(1.0, 1.0 / amp))
        s = per / (2.0 * math.pi) * math.asin(asin_arg)

    return (f * (amp * math.pow(2.0, 10.0 * time) * math.sin((time - s) * (2.0 * math.pi) / per))) + 1.0


def _apply_elastic_to_segment(fc, k0, k1, amplitude, period):
    """Apply elastic ease out to keyframe segment using ELASTIC interpolation."""
    f0, v0 = k0.co[0], k0.co[1]
    f1, v1 = k1.co[0], k1.co[1]
    df = f1 - f0
    dv = v1 - v0
    if abs(df) < 1e-8:
        return False

    # Scale amplitude by value delta so large value changes can overshoot more.
    value_scale = max(1.0, abs(dv))
    amp_scaled = max(1.0, float(amplitude) * value_scale)

    # Remove existing keys strictly between k0 and k1
    existing = [kp for kp in fc.keyframe_points if f0 < kp.co[0] < f1]
    for kp in reversed(existing):
        try:
            fc.keyframe_points.remove(kp)
        except Exception:
            pass

    # Set keyframe interpolation to ELASTIC and apply parameters
    try:
        k0.interpolation = 'ELASTIC'
        k0.easing = 'EASE_OUT'
        k0.amplitude = amp_scaled
        k0.period = period * df
        k1.interpolation = 'ELASTIC'
    except Exception:
        # Fallback if ELASTIC is not supported
        return False

    return True


def _draw_text(x, y, text, size=12, color=TLFC_COLORS["white"]):
    font_id = 0
    blf.size(font_id, size)
    blf.color(font_id, *color)
    blf.position(font_id, x, y, 0)
    blf.draw(font_id, text)


def _clip_line_to_rect(p1, p2, x0, y0, x1, y1):
    """Cohen-Sutherland line clipping. Returns clipped [(x,y),(x,y)] or None."""
    lx1, ly1 = p1
    lx2, ly2 = p2
    # Trivial reject
    if lx1 < x0 and lx2 < x0:
        return None
    if lx1 > x1 and lx2 > x1:
        return None
    if ly1 < y0 and ly2 < y0:
        return None
    if ly1 > y1 and ly2 > y1:
        return None
    # Clip each endpoint
    dx = lx2 - lx1
    dy = ly2 - ly1
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, lx1 - x0), (dx, x1 - lx1), (-dy, ly1 - y0), (dy, y1 - ly1)):
        if p == 0:
            if q < 0:
                return None
        elif p < 0:
            r = q / p
            if r > t1:
                return None
            if r > t0:
                t0 = r
        else:
            r = q / p
            if r < t0:
                return None
            if r < t1:
                t1 = r
    return [(lx1 + t0 * dx, ly1 + t0 * dy), (lx1 + t1 * dx, ly1 + t1 * dy)]


# ---------- Data gathering ----------
def _collect_action_fcurves(anim_data):
    action = getattr(anim_data, "action", None) if anim_data else None
    if action is None:
        return []

    # Blender versions with classic Action API expose action.fcurves directly.
    direct = getattr(action, "fcurves", None)
    if direct is not None:
        try:
            return list(direct)
        except Exception:
            return []

    # Layered action fallback (newer APIs): try channel bags per strip/slot.
    out = []
    seen = set()
    layers = getattr(action, "layers", None)
    if not layers:
        return out

    slot_candidates = []
    active_slot = getattr(anim_data, "action_slot", None)
    if active_slot is not None:
        slot_candidates.append(active_slot)
    slots = getattr(action, "slots", None)
    if slots:
        try:
            slot_candidates.extend(list(slots))
        except Exception:
            pass

    for layer in layers:
        strips = getattr(layer, "strips", None)
        if not strips:
            continue
        for strip in strips:
            channelbag_fn = getattr(strip, "channelbag", None)
            if not callable(channelbag_fn):
                continue
            for slot in slot_candidates:
                try:
                    bag = channelbag_fn(slot)
                except Exception:
                    continue
                if not bag:
                    continue
                bag_fcurves = getattr(bag, "fcurves", None)
                if not bag_fcurves:
                    continue
                try:
                    for fc in bag_fcurves:
                        fc_id = id(fc)
                        if fc_id in seen:
                            continue
                        seen.add(fc_id)
                        out.append(fc)
                except Exception:
                    continue
    return out


def _selected_fcurves_with_selected_keys(context):
    candidates = []
    for attr in ("selected_editable_fcurves", "selected_visible_fcurves", "visible_fcurves"):
        fcurves = getattr(context, attr, None)
        if fcurves:
            candidates = list(fcurves)
            break
    if not candidates:
        obj = context.active_object
        if obj and obj.animation_data:
            candidates = _collect_action_fcurves(obj.animation_data)

    out = []
    seen = set()
    for fc in candidates:
        fc_id = id(fc)
        if fc_id in seen:
            continue
        seen.add(fc_id)

        all_keys = list(fc.keyframe_points)
        sel_keys = []
        for kp in all_keys:
            if kp.select_control_point or kp.select_left_handle or kp.select_right_handle:
                sel_keys.append(kp)

        # In Timeline/Dopesheet it is common to select channels without key points.
        if sel_keys or getattr(fc, "select", False):
            out.append((fc, sel_keys, all_keys))
    return out

def _clamp01(x):
    return max(0.0, min(1.0, x))

def _focused_curve_item(context, selected_items):
    if not selected_items:
        return None
    active_fc = getattr(context, "active_editable_fcurve", None)
    if active_fc is not None:
        for item in selected_items:
            if item[0] == active_fc:
                return item
    return selected_items[0]


def _focused_segment(context, selected_items):
    item = _focused_curve_item(context, selected_items)
    if item is None:
        return None

    fc, sel_keys, all_keys = item
    key_source = list(sel_keys) if len(sel_keys) >= 2 else list(all_keys)
    if len(key_source) < 2:
        return None

    key_source.sort(key=lambda kp: kp.co[0])
    frame_now = context.scene.frame_current
    pair = None
    for i in range(len(key_source) - 1):
        a = key_source[i]
        b = key_source[i + 1]
        if a.co[0] <= frame_now <= b.co[0]:
            pair = (a, b)
            break
    if pair is None:
        pair = (key_source[0], key_source[1])

    k0, k1 = pair
    f0, v0 = k0.co[0], k0.co[1]
    f1, v1 = k1.co[0], k1.co[1]
    df = f1 - f0
    dv = v1 - v0
    if abs(df) < 1e-8:
        return None
    if abs(dv) < 1e-8:
        dv = 1.0

    def to_norm(pt):
        return ((pt[0] - f0) / df, (pt[1] - v0) / dv)

    c1 = to_norm(k0.handle_right)
    c2 = to_norm(k1.handle_left)
    return {
        "fc": fc,
        "k0": k0,
        "k1": k1,
        "f0": f0,
        "v0": v0,
        "df": df,
        "dv": dv,
        "c1": c1,
        "c2": c2,
    }


def _segment_from_selected_key(context, selected_items):
    item = _focused_curve_item(context, selected_items)
    if item is None:
        return None

    fc, sel_keys, all_keys = item
    if not sel_keys:
        return None

    key_sorted = sorted(all_keys, key=lambda kp: kp.co[0])
    key_pos = {id(kp): i for i, kp in enumerate(key_sorted)}
    frame_now = context.scene.frame_current

    # If exactly two keys are selected, use them as the segment.
    if len(sel_keys) == 2:
        k0, k1 = sorted(sel_keys, key=lambda kp: kp.co[0])
    else:
        # Otherwise, pick the selected key closest to current frame and its successor.
        cur = min(sel_keys, key=lambda kp: abs(kp.co[0] - frame_now))
        idx = key_pos.get(id(cur), -1)
        if idx < 0 or idx >= len(key_sorted) - 1:
            return None
        k0, k1 = cur, key_sorted[idx + 1]

    if k1.co[0] <= k0.co[0]:
        return None

    f0, v0 = k0.co[0], k0.co[1]
    f1, v1 = k1.co[0], k1.co[1]
    df = f1 - f0
    dv = v1 - v0
    if abs(df) < 1e-8:
        return None
    if abs(dv) < 1e-8:
        dv = 1.0

    def to_norm(pt):
        return ((pt[0] - f0) / df, (pt[1] - v0) / dv)

    return {
        "fc": fc,
        "k0": k0,
        "k1": k1,
        "f0": f0,
        "v0": v0,
        "df": df,
        "dv": dv,
        "c1": to_norm(k0.handle_right),
        "c2": to_norm(k1.handle_left),
    }

def _bezier_point(t, p0, p1, p2, p3):
    u = 1.0 - t
    b0 = u * u * u
    b1 = 3.0 * u * u * t
    b2 = 3.0 * u * t * t
    b3 = t * t * t
    return (
        b0 * p0[0] + b1 * p1[0] + b2 * p2[0] + b3 * p3[0],
        b0 * p0[1] + b1 * p1[1] + b2 * p2[1] + b3 * p3[1],
    )

def _editor_to_screen(nx, ny, sx0, sy0, sx1, sy1, zoom, pan_x, pan_y):
    vx = (nx - 0.5) * zoom + 0.5 + pan_x
    vy = (ny - 0.5) * zoom + 0.5 + pan_y
    return (
        sx0 + vx * (sx1 - sx0),
        sy0 + vy * (sy1 - sy0),
    )


def _screen_to_editor(px, py, sx0, sy0, sx1, sy1, zoom, pan_x, pan_y):
    vx = (px - sx0) / max(1e-8, (sx1 - sx0))
    vy = (py - sy0) / max(1e-8, (sy1 - sy0))
    nx = ((vx - 0.5 - pan_x) / max(1e-8, zoom)) + 0.5
    ny = ((vy - 0.5 - pan_y) / max(1e-8, zoom)) + 0.5
    return nx, ny


def _constrain_handle(which, x, y):
    # Asymmetric x limits per request; y is intentionally unbounded.
    # if which == "h1":
    #     return max(0.0, x), y
    # return min(1.0, x), y
    return _clamp01(x), y


def _snap_edge(x, y, threshold):
    sx, sy = x, y
    for edge in (0.0, 1.0):
        if abs(sx - edge) <= threshold:
            sx = edge
        if abs(sy - edge) <= threshold:
            sy = edge
    return sx, sy


def _snap_grid(x, y, subdiv):
    if subdiv <= 0:
        return x, y
    step = 1.0 / float(subdiv)
    return round(x / step) * step, round(y / step) * step


def _point_in_rect(px, py, rect):
    x0, y0, x1, y1 = rect
    return x0 <= px <= x1 and y0 <= py <= y1


def _overlay_buttons(_wm):
    auto_label = _t(_wm, "button.auto_on", "Auto: ON") if getattr(_wm, "tlfc_auto_apply", False) else _t(_wm, "button.auto_off", "Auto: OFF")
    return [
        [
            # {"label": _t(_wm, "button.zoom_in", "Zoom +"), "op": "zoom", "kwargs": {"mode": "IN"}},
            # {"label": _t(_wm, "button.zoom_out", "Zoom -"), "op": "zoom", "kwargs": {"mode": "OUT"}},
            {"label": _t(_wm, "button.center", "Center"), "op": "zoom", "kwargs": {"mode": "CENTER"}},
        ],
        [
            {"label": _t(_wm, "button.mirror", "Mirror"), "op": "mirror", "kwargs": {}},
            {"label": _t(_wm, "button.copy_selected", "Copy Sel."), "op": "read", "kwargs": {}},
            {"label": _t(_wm, "button.reset", "Reset"), "op": "reset", "kwargs": {}},
        ],
        [
            {"label": _t(_wm, "button.linear", "Linear"), "op": "interp", "kwargs": {"mode": "LINEAR"}},
            {"label": _t(_wm, "button.constant", "Constant"), "op": "interp", "kwargs": {"mode": "CONSTANT"}},
            {"label": auto_label, "op": "toggle_auto", "kwargs": {}},
            {"label": _t(_wm, "button.save", "Save"), "op": "preset_save", "kwargs": {}},
        ],
    ]


def _invoke_overlay_button(context, op, kwargs, shift=False):
    try:
        if op == "zoom":
            bpy.ops.tlfc.editor_zoom(mode=kwargs.get("mode", "CENTER"))
        elif op == "preset_save":
            bpy.ops.tlfc.save_preset()
        elif op == "preset_apply":
            idx = int(kwargs.get("idx", -1))
            if shift:
                _delete_preset_index(idx)
            elif _apply_preset_index(context.window_manager, idx):
                if context.window_manager.tlfc_auto_apply:
                    bpy.ops.tlfc.apply_curve()
        elif op == "toggle_auto":
            context.window_manager.tlfc_auto_apply = not context.window_manager.tlfc_auto_apply
        elif op == "apply":
            bpy.ops.tlfc.apply_curve()
        elif op == "interp":
            bpy.ops.tlfc.set_interpolation(mode=kwargs.get("mode", "LINEAR"))
        elif op == "mirror":
            bpy.ops.tlfc.mirror_curve()
        elif op == "reset":
            bpy.ops.tlfc.reset_curve()
        elif op == "read":
            bpy.ops.tlfc.read_curve()
        elif op == "set_mode":
            context.window_manager.tlfc_sidebar_mode = kwargs.get("mode", "BEZIER")
    except Exception:
        pass


def _iter_selected_segments(context):
    selected_items = _selected_fcurves_with_selected_keys(context)
    for fc, sel_keys, all_keys in selected_items:
        if not sel_keys:
            continue
        key_sorted = sorted(all_keys, key=lambda kp: kp.co[0])
        selected_ids = {id(kp) for kp in sel_keys}
        for i, kp in enumerate(key_sorted[:-1]):
            if id(kp) in selected_ids:
                nxt = key_sorted[i + 1]
                if nxt.co[0] > kp.co[0]:
                    yield fc, kp, nxt


def _apply_editor_curve_to_segment(k0, k1, h1x, h1y, h2x, h2y):
    f0, v0 = k0.co[0], k0.co[1]
    f1, v1 = k1.co[0], k1.co[1]
    df = f1 - f0
    dv = v1 - v0
    if abs(df) < 1e-8:
        return False

    k0.interpolation = 'BEZIER'
    k1.interpolation = 'BEZIER'
    k0.handle_right_type = 'FREE'
    k1.handle_left_type = 'FREE'
    h1x, h1y = _constrain_handle("h1", h1x, h1y)
    h2x, h2y = _constrain_handle("h2", h2x, h2y)
    k0.handle_right = (f0 + h1x * df, v0 + h1y * dv)
    k1.handle_left = (f0 + h2x * df, v0 + h2y * dv)
    return True


def _focused_curve_info(context, selected_items):
    item = _focused_curve_item(context, selected_items)
    if item is None:
        return None

    fc, sel_keys, all_keys = item
    key_source = sel_keys if sel_keys else all_keys
    frames = [kp.co[0] for kp in key_source]
    values = [kp.co[1] for kp in key_source]
    frame_now = context.scene.frame_current
    try:
        eval_now = fc.evaluate(frame_now)
    except Exception:
        eval_now = None

    return {
        "name": f"{fc.data_path}[{fc.array_index}]",
        "group": fc.group.name if fc.group else "None",
        "keys_total": len(all_keys),
        "keys_selected": len(sel_keys),
        "frame_span": (min(frames), max(frames)) if frames else None,
        "value_span": (min(values), max(values)) if values else None,
        "eval_now": eval_now,
        "modifiers": len(fc.modifiers),
        "extrapolation": fc.extrapolation,
    }
# ---------- Draw callback ----------
def draw_editor_sidebar():
    ctx = bpy.context
    region = ctx.region
    wm = ctx.window_manager
    space = ctx.space_data
    if not _supports_editor_space(space):
        return
    if region is None or region.type != 'WINDOW':
        return
    if not _is_editor_enabled(ctx.area):
        return
    is_info_space = (space.type == 'INFO')
    size_scale = wm.tlfc_display_size
    if is_info_space:
        # Info editor variant: keep sidebar full-width whenever active.
        x0 = 0
        x1 = region.width
        alpha = 1
        pad_outer = 0
    else:
        sidebar_w = int(region.width * (wm.tlfc_sidebar_width / 100.0))
        sidebar_w = max(int(TLFC_UI_NUMBERS["sidebar_min_px"]), min(region.width, sidebar_w))
        alpha = wm.tlfc_alpha
        pad_outer = wm.tlfc_outer_pad
        x1 = region.width - pad_outer
        x0 = max(0, x1 - sidebar_w)
    y0 = pad_outer
    y1 = region.height - pad_outer
    # Resolve theme colors once per draw call (auto-follows active Blender theme).
    C = _theme_colors()
    # Panel background and border
    bg = (C["panel_bg"][0], C["panel_bg"][1], C["panel_bg"][2], alpha)
    border = (C["panel_border"][0], C["panel_border"][1], C["panel_border"][2], min(1.0, alpha + 0.2))
    if getattr(wm, "tlfc_hover_sidebar_edge", False) or getattr(wm, "tlfc_dragging_sidebar", False):
        border = (C["panel_border_hover"][0], C["panel_border_hover"][1], C["panel_border_hover"][2], min(1.0, alpha + 0.28))
    _draw_rect(x0, y0, x1, y1, bg)
    full_width_panel = (x0 <= 0 and x1 >= region.width)
    if pad_outer == 0:
        # Keep left edge invisible when panel spans entire editor width.
        if not full_width_panel:
            _draw_aa_line_strip([(x0, y0), (x0, y1)], border, width=2.0)
    else:
        if full_width_panel:
            _draw_aa_line_strip([(x0, y0), (x0, y1)], border, width=2.0)
        else:
            _draw_aa_line_strip([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], border, width=2.0)
    title_top_pad = max(6, int(8 * size_scale))
    selected_items = _selected_fcurves_with_selected_keys(ctx)
    ns = bpy.app.driver_namespace

    sidebar_mode = getattr(wm, 'tlfc_sidebar_mode', _prop_default("tlfc_sidebar_mode"))
    hover_token = getattr(wm, "tlfc_hover_button", "")
    pressed_token = getattr(wm, "tlfc_pressed_button", "")
    _tab_btn_font = max(9, int(10 * size_scale))

    # Tab bar layout (sits below top padding)
    tab_h_val = max(14, int(16 * size_scale))
    tab_gap_below = max(3, int(4 * size_scale))
    tab_y1 = y1 - title_top_pad
    tab_y0 = tab_y1 - tab_h_val

    # Easing editor graph — height is ratio-driven for proportional scaling.
    # The curve widget is the most important UI element: guarantee a minimum
    # size so that it is never "crushed" by surrounding buttons.
    pad = 12
    sx0 = x0 + pad
    sx1_limit = x1 - pad
    sy1 = tab_y0 - tab_gap_below
    extra_tab_space = tab_h_val + tab_gap_below
    available_h = max(80, (y1 - y0) - extra_tab_space - 10)
    # Read graph ratio from addon preferences (persistent) with WM fallback.
    graph_ratio = _pref_float("tlfc_graph_height_ratio", getattr(wm, 'tlfc_graph_height_ratio', _prop_default("tlfc_graph_height_ratio")))
    # Guarantee at least 80px for the curve widget so it stays functional
    # even when the window is very short vertically.
    graph_h = max(80, int(available_h * graph_ratio))
    graph_w = min(graph_h, sx1_limit - sx0)
    # Allow the graph to be taller than wide (rectangular) so it
    # fills available vertical space instead of wasting it.
    graph_actual_h = graph_h
    # Clamp so the graph never extends below the panel's bottom reserve
    # (apply button height + padding).
    min_bottom = y0 + 50
    if sy1 - graph_actual_h < min_bottom:
        graph_actual_h = max(40, int(sy1 - min_bottom))
    sx1 = sx0 + graph_w
    sy0 = sy1 - graph_actual_h

    _draw_rect(sx0, sy0, sx1, sy1, C["graph_bg"])
    _draw_aa_line_strip([(sx0, sy0), (sx1, sy0), (sx1, sy1), (sx0, sy1), (sx0, sy0)], C["preset_preview_border"], width=1.0)
    # Separator bar below the graph for manual resize dragging.
    _sep_hit = int(TLFC_UI_NUMBERS.get("graph_sep_hit_px", 6))
    _sep_hover = getattr(wm, 'tlfc_hover_graph_sep', False)
    _sep_col = C["panel_border_hover"] if _sep_hover else C["panel_border"]

    # Draw tab buttons (Bezier | Elastic) and collect rects for hit-testing
    _pending_tab_buttons = []
    _tab_defs = [
        ("tab.bezier", "Bezier", "BEZIER"),
        ("tab.elastic", "Elastic", "ELASTIC"),
    ]
    _tab_total_w = sx1 - sx0
    _tab_w = (_tab_total_w - 1) / 2.0
    for _ti, (_tkey, _tdefault, _tmode) in enumerate(_tab_defs):
        _tx0 = sx0 + _ti * (_tab_w + 1)
        _tx1 = _tx0 + _tab_w
        _is_active = (sidebar_mode == _tmode)
        _token = _button_token("set_mode", {"mode": _tmode})
        _tstate = "pressed" if pressed_token == _token else ("hover" if hover_token == _token else "normal")
        if _is_active:
            _tab_bg = C["tab_active_bg"]
            _tab_tc = C["tab_active_text"]
        else:
            _tab_bg = C["tab_inactive_bg"]
            _tab_tc = C["tab_inactive_text"]
        if _tstate == "hover":
            _tab_bg = _adjust_rgba(_tab_bg, 0.08)
            if not _is_active:
                _tab_tc = C["tab_inactive_hover_text"]
        elif _tstate == "pressed":
            _tab_bg = _adjust_rgba(_tab_bg, -0.06)
        _draw_rect(_tx0, tab_y0, _tx1, tab_y1, _tab_bg)
        if _is_active:
            _draw_aa_line_strip([(_tx0, tab_y0), (_tx1, tab_y0), (_tx1, tab_y1), (_tx0, tab_y1), (_tx0, tab_y0)], C["tab_active_outline"], width=1.0)
        _draw_text_centered(_tx0, tab_y0, _tx1, tab_y1, _t(wm, _tkey, _tdefault), size=_tab_btn_font, color=_tab_tc, truncate=True, pad=4)
        _pending_tab_buttons.append({
            "rect_local": (_tx0, tab_y0, _tx1, tab_y1),
            "op": "set_mode",
            "kwargs": {"mode": _tmode},
            "id": _token,
        })

    zoom = wm.tlfc_view_zoom
    pan_x = wm.tlfc_view_pan_x
    pan_y = wm.tlfc_view_pan_y

    # Grid for readability in normalized space with pan/zoom.
    # Extended grid range to show lines outside (0,0)-(1,1) area
    subdiv = max(1, wm.tlfc_grid_subdiv)

    # Calculate extended grid range based on zoom and pan
    # We want to draw grid lines that are visible in the viewport
    grid_extend = 2.0  # Draw grid lines from -2 to 3 in normalized space
    grid_min = -grid_extend
    grid_max = 1.0 + grid_extend
    grid_total_divs = int((grid_max - grid_min) * subdiv)

    # Determine line width based on zoom level
    base_width = 2.0
    grid_line_width = base_width * (0.8 + 0.2 * min(zoom, 3.0))
    axis_line_width = grid_line_width * 1.8

    for i in range(grid_total_divs + 1):
        g = grid_min + i / float(subdiv)
        va = _editor_to_screen(g, grid_min, sx0, sy0, sx1, sy1, zoom, pan_x, pan_y)
        vb = _editor_to_screen(g, grid_max, sx0, sy0, sx1, sy1, zoom, pan_x, pan_y)
        ha = _editor_to_screen(grid_min, g, sx0, sy0, sx1, sy1, zoom, pan_x, pan_y)
        hb = _editor_to_screen(grid_max, g, sx0, sy0, sx1, sy1, zoom, pan_x, pan_y)

        # Highlight x=0 and y=0 axes
        is_x_axis = abs(g) < 1e-6  # g ≈ 0
        is_boundary = (abs(g) < 1e-6 or abs(g - 1.0) < 1e-6)  # g = 0 or g = 1

        if is_x_axis:
            col = C["grid_axis"]
            width = axis_line_width
        elif is_boundary:
            col = C["grid_boundary"]
            width = grid_line_width
        else:
            col = C["grid_regular"]
            width = grid_line_width * 0.8

        # Draw vertical line (clipped)
        v_clipped = _clip_line_to_rect(va, vb, sx0, sy0, sx1, sy1)
        if v_clipped:
            _draw_aa_line_strip(v_clipped, col, width=width)

        # Draw horizontal line (clipped)
        h_clipped = _clip_line_to_rect(ha, hb, sx0, sy0, sx1, sy1)
        if h_clipped:
            _draw_aa_line_strip(h_clipped, col, width=width)

    # Helper: clamp a screen-space point to inside the graph view rect.
    def _clamp_to_view(px, py):
        return (max(sx0, min(sx1, px)), max(sy0, min(sy1, py)))

    # Helper: compute smart text offset so a label near handle (hx,hy) stays inside view.
    def _label_pos(hx, hy, lbl_w, lbl_h, off_x, off_y):
        tx = hx + off_x
        ty = hy + off_y
        tx = max(sx0 + 2, min(sx1 - lbl_w - 2, tx))
        ty = max(sy0 + 2, min(sy1 - lbl_h - 2, ty))
        return tx, ty

    if sidebar_mode == 'ELASTIC':
        # --- Elastic mode ---
        # Editor ranges: amplitude 0.0–1.0, period 0.05–1.0
        _el_amp = _clamp_prop("tlfc_elastic_amplitude", getattr(wm, 'tlfc_elastic_amplitude', _prop_default("tlfc_elastic_amplitude")))
        _el_per = _clamp_prop("tlfc_elastic_period", getattr(wm, 'tlfc_elastic_period', _prop_default("tlfc_elastic_period")))

        # --- Draw elastic curve, clipped to the graph box via scissor ---
        _el_n = max(96, wm.tlfc_samples * 3)
        _el_pts = []
        for _si in range(_el_n):
            _st = _si / (_el_n - 1.0)
            _sv = _elastic_ease_out_normalized(_st, _el_amp, _el_per) * 0.5
            _el_pts.append(_editor_to_screen(_st, _sv, sx0, sy0, sx1, sy1, zoom, pan_x, pan_y))

        # Proper segment-level clip using Cohen-Sutherland (matches grid behavior).
        for _i in range(len(_el_pts) - 1):
            _seg = _clip_line_to_rect(_el_pts[_i], _el_pts[_i + 1], sx0, sy0, sx1, sy1)
            if _seg:
                _draw_aa_line_strip(_seg, C["elastic_curve"], width=4.0)

        # Handle positions in normalized editor space mapped from configured ranges.
        _amp_norm = _prop_to_unit("tlfc_elastic_amplitude", _el_amp)
        _per_norm = _prop_to_unit("tlfc_elastic_period", _el_per)
        p1s_raw = _editor_to_screen(TLFC_UI_NUMBERS["elastic_handle_x"], _amp_norm, sx0, sy0, sx1, sy1, zoom, pan_x, pan_y)
        p2s_raw = _editor_to_screen(_per_norm, TLFC_UI_NUMBERS["elastic_handle_y"], sx0, sy0, sx1, sy1, zoom, pan_x, pan_y)
        # Clamp handle positions to view rect for drawing and hit-testing.
        p1s = _clamp_to_view(*p1s_raw)
        p2s = _clamp_to_view(*p2s_raw)

        # --- Start / end point markers (only when inside view) ---
        _p_start = _editor_to_screen(0.0, 0.0, sx0, sy0, sx1, sy1, zoom, pan_x, pan_y)
        _p_end   = _editor_to_screen(1.0, 0.5, sx0, sy0, sx1, sy1, zoom, pan_x, pan_y)
        _ep_r = 4.0 * size_scale
        if sx0 <= _p_start[0] <= sx1 and sy0 <= _p_start[1] <= sy1:
            _draw_aa_circle(_p_start[0], _p_start[1], _ep_r, C["endpoint_fill"], C["endpoint_outline"])
        if sx0 <= _p_end[0] <= sx1 and sy0 <= _p_end[1] <= sy1:
            _draw_aa_circle(_p_end[0], _p_end[1], _ep_r, C["endpoint_fill"], C["endpoint_outline"])

        # --- Axis guide lines (clipped to view) ---
        _g_amp_bot = _editor_to_screen(TLFC_UI_NUMBERS["elastic_handle_x"], 0.0, sx0, sy0, sx1, sy1, zoom, pan_x, pan_y)
        _g_amp_top = _editor_to_screen(TLFC_UI_NUMBERS["elastic_handle_x"], 1.0, sx0, sy0, sx1, sy1, zoom, pan_x, pan_y)
        _gc = _clip_line_to_rect(_g_amp_bot, _g_amp_top, sx0, sy0, sx1, sy1)
        if _gc:
            _draw_aa_line_strip(_gc, C["elastic_amp_guide"], width=4.0)
        _g_per_l = _editor_to_screen(0.0, TLFC_UI_NUMBERS["elastic_handle_y"], sx0, sy0, sx1, sy1, zoom, pan_x, pan_y)
        _g_per_r = _editor_to_screen(1.0, TLFC_UI_NUMBERS["elastic_handle_y"], sx0, sy0, sx1, sy1, zoom, pan_x, pan_y)
        _gp = _clip_line_to_rect(_g_per_l, _g_per_r, sx0, sy0, sx1, sy1)
        if _gp:
            _draw_aa_line_strip(_gp, C["elastic_per_guide"], width=4.0)
        
        _draw_aa_line_strip([(sx0, sy0), (sx1, sy0)], _sep_col, width=3.0 if _sep_hover else 1.5)

        # --- Handle circles (drawn at clamped positions, always inside view) ---
        hover_handle = getattr(wm, 'tlfc_hover_handle', '')
        h1_radius = 7.0 * size_scale if hover_handle == 'h1' else 5.4 * size_scale
        h2_radius = 7.0 * size_scale if hover_handle == 'h2' else 5.4 * size_scale
        h1_alpha  = 1.0 if hover_handle == 'h1' else 0.92
        h2_alpha  = 1.0 if hover_handle == 'h2' else 0.92
        _draw_aa_circle(p1s[0], p1s[1], h1_radius, TLFC_COLORS["handle_amp_fill"], (TLFC_COLORS["handle_outline"][0], TLFC_COLORS["handle_outline"][1], TLFC_COLORS["handle_outline"][2], h1_alpha))
        _draw_aa_circle(p2s[0], p2s[1], h2_radius, TLFC_COLORS["handle_per_fill"], (TLFC_COLORS["handle_outline"][0], TLFC_COLORS["handle_outline"][1], TLFC_COLORS["handle_outline"][2], h2_alpha))

        # --- Labels next to handles, kept inside view ---
        _lbl_sz = max(8, int(9 * size_scale))
        _lbl_off = int(7 * size_scale)
        _lbl_h   = _lbl_sz + 2
        _a_text = "Amp: {:.2f}".format(_el_amp)
        _p_text = "Per: {:.3f}".format(_el_per)
        blf.size(0, _lbl_sz)
        _a_w = blf.dimensions(0, _a_text)[0]
        _p_w = blf.dimensions(0, _p_text)[0]
        _ax, _ay = _label_pos(p1s[0], p1s[1], _a_w, _lbl_h, _lbl_off, -4)
        _px, _py = _label_pos(p2s[0], p2s[1], _p_w, _lbl_h, -4, _lbl_off)
        _draw_text(_ax, _ay, _a_text, size=_lbl_sz, color=C["elastic_amp_label"])
        _draw_text(_px, _py, _p_text, size=_lbl_sz, color=C["elastic_per_label"])

    else:
        # --- Bezier mode ---
        p0 = (0.0, 0.0)
        p1 = (wm.tlfc_h1x, wm.tlfc_h1y)
        p2 = (wm.tlfc_h2x, wm.tlfc_h2y)
        p3 = (1.0, 1.0)
        p0s = _editor_to_screen(p0[0], p0[1], sx0, sy0, sx1, sy1, zoom, pan_x, pan_y)
        p3s = _editor_to_screen(p3[0], p3[1], sx0, sy0, sx1, sy1, zoom, pan_x, pan_y)
        p1s_raw = _editor_to_screen(p1[0], p1[1], sx0, sy0, sx1, sy1, zoom, pan_x, pan_y)
        p2s_raw = _editor_to_screen(p2[0], p2[1], sx0, sy0, sx1, sy1, zoom, pan_x, pan_y)
        # Clamp handle screen positions so they and their UI stay inside the view box.
        p1s = _clamp_to_view(*p1s_raw)
        p2s = _clamp_to_view(*p2s_raw)

        # Handle lines: clip from anchor circle to (clamped) handle – always stays inside.
        _p0_in = sx0 <= p0s[0] <= sx1 and sy0 <= p0s[1] <= sy1
        _p3_in = sx0 <= p3s[0] <= sx1 and sy0 <= p3s[1] <= sy1
        _hl1 = _clip_line_to_rect(p0s, p1s, sx0, sy0, sx1, sy1)
        if _hl1:
            _draw_aa_line_strip(_hl1, C["handle_line"], width=2.0)
        _hl2 = _clip_line_to_rect(p3s, p2s, sx0, sy0, sx1, sy1)
        if _hl2:
            _draw_aa_line_strip(_hl2, C["handle_line"], width=2.0)

        # Bezier curve – scissor-clip to view rect so it never bleeds outside.
        curve_pts = []
        samples = wm.tlfc_samples
        n = max(48, samples * 2)
        for i in range(n):
            t = i / (n - 1)
            bx, by = _bezier_point(t, p0, p1, p2, p3)
            curve_pts.append(_editor_to_screen(bx, by, sx0, sy0, sx1, sy1, zoom, pan_x, pan_y))
        # Segment-level clip using Cohen-Sutherland (matches grid behavior).
        for _i in range(len(curve_pts) - 1):
            _cs = _clip_line_to_rect(curve_pts[_i], curve_pts[_i + 1], sx0, sy0, sx1, sy1)
            if _cs:
                _draw_aa_line_strip(_cs, C["curve_orange"], width=4.0)
        
        _draw_aa_line_strip([(sx0, sy0), (sx1, sy0)], _sep_col, width=3.0 if _sep_hover else 1.5)

        # --- Start / end point markers (only when anchor is inside view) ---
        _ep_r = 4.0 * size_scale
        if sx0 <= p0s[0] <= sx1 and sy0 <= p0s[1] <= sy1:
            _draw_aa_circle(p0s[0], p0s[1], _ep_r, C["endpoint_fill"], C["endpoint_outline"])
        if sx0 <= p3s[0] <= sx1 and sy0 <= p3s[1] <= sy1:
            _draw_aa_circle(p3s[0], p3s[1], _ep_r, C["endpoint_fill"], C["endpoint_outline"])

        # --- Handle circles (always drawn at clamped positions, always inside view) ---
        hover_handle = getattr(wm, 'tlfc_hover_handle', '')
        h1_radius = 7.0 * size_scale if hover_handle == 'h1' else 5.4 * size_scale
        h2_radius = 7.0 * size_scale if hover_handle == 'h2' else 5.4 * size_scale
        h1_alpha  = 1.0 if hover_handle == 'h1' else 0.92
        h2_alpha  = 1.0 if hover_handle == 'h2' else 0.92
        _draw_aa_circle(p1s[0], p1s[1], h1_radius, TLFC_COLORS["handle_amp_fill"], (TLFC_COLORS["handle_outline"][0], TLFC_COLORS["handle_outline"][1], TLFC_COLORS["handle_outline"][2], h1_alpha))
        _draw_aa_circle(p2s[0], p2s[1], h2_radius, TLFC_COLORS["handle_per_fill"], (TLFC_COLORS["handle_outline"][0], TLFC_COLORS["handle_outline"][1], TLFC_COLORS["handle_outline"][2], h2_alpha))

        # --- Labels for handle values, kept inside view ---
        _lbl_sz  = max(8, int(9 * size_scale))
        _lbl_off = int(7 * size_scale)
        _lbl_h   = _lbl_sz + 2
        _h1_text = f"({wm.tlfc_h1x:.2f}, {wm.tlfc_h1y:.2f})"
        _h2_text = f"({wm.tlfc_h2x:.2f}, {wm.tlfc_h2y:.2f})"
        blf.size(0, _lbl_sz)
        _h1_w = blf.dimensions(0, _h1_text)[0]
        _h2_w = blf.dimensions(0, _h2_text)[0]
        _h1x, _h1y = _label_pos(p1s[0], p1s[1], _h1_w, _lbl_h, _lbl_off, -4)
        _h2x, _h2y = _label_pos(p2s[0], p2s[1], _h2_w, _lbl_h, -_h2_w - _lbl_off, _lbl_off)
        _draw_text(_h1x, _h1y, _h1_text, size=_lbl_sz, color=C["bezier_h1_label"])
        _draw_text(_h2x, _h2y, _h2_text, size=_lbl_sz, color=C["bezier_h2_label"])
    
    area_ptr = ctx.area.as_pointer() if ctx.area else 0
    ui_map = ns.get(EDITOR_UI_KEY)
    if not isinstance(ui_map, dict):
        ui_map = {}
        ns[EDITOR_UI_KEY] = ui_map

    ui_map[area_ptr] = {
        "area": ctx.area.as_pointer() if ctx.area else 0,
        "space_type": space.type,
        "region": region.as_pointer(),
        "region_w": region.width,
        "panel_rect_abs": (region.x + x0, region.y + y0, region.x + x1, region.y + y1),
        "panel_x1_abs": (region.x + x1),
        "sidebar_edge_abs": (region.x + x0),
        "sidebar_edge_hit_px": SIDEBAR_EDGE_HIT_PX,
        "rect": (sx0, sy0, sx1, sy1),
        "rect_abs": (region.x + sx0, region.y + sy0, region.x + sx1, region.y + sy1),
        "h1": p1s,
        "h2": p2s,
        "h1_abs": (region.x + p1s[0], region.y + p1s[1]),
        "h2_abs": (region.x + p2s[0], region.y + p2s[1]),
        "graph_sep_abs": (region.x + sx0, region.y + sy0 - _sep_hit,
                          region.x + sx1, region.y + sy0 + _sep_hit),
        "graph_sep_sy1": region.y + sy1,
        "graph_sep_available_h": available_h,
        "buttons_abs": [],
    }
    # Register tab buttons for click hit-testing (must happen after ui_map is initialised)
    for _tb in _pending_tab_buttons:
        _rx0, _ry0, _rx1, _ry1 = _tb["rect_local"]
        ui_map[area_ptr]["buttons_abs"].append({
            "rect": (region.x + _rx0, region.y + _ry0, region.x + _rx1, region.y + _ry1),
            "op": _tb["op"],
            "kwargs": _tb["kwargs"],
            "id": _tb["id"],
        })

    # Draw interactive overlay buttons with responsive placement.
    bx0 = sx1 + 12
    bx1 = x1 - 10
    gap = max(3, int(4 * size_scale))
    row_h = max(16, int(20 * size_scale))
    btn_font = max(9, int(10 * size_scale))
    apply_h = max(22, int(26 * size_scale))
    tile = max(38, int(46 * size_scale))
    presets = _load_presets()
    side_has_space = (bx1 - bx0) > 80
    if side_has_space:
        # If side buttons would truncate text, switch to a single-column layout for more width.
        force_column = False
        blf.size(0, btn_font)
        for row in _overlay_buttons(wm):
            cols = len(row)
            cell_w = (bx1 - bx0 - gap * (cols - 1)) / max(1, cols)
            for b in row:
                if blf.dimensions(0, str(b.get("label", "")))[0] > (cell_w - 12):
                    force_column = True
                    break
            if force_column:
                break

        # Apply button pinned to panel bottom at all times.
        ay0 = y0 + 10
        ay1 = ay0 + apply_h
        apply_token = _button_token("apply", {})
        apply_state = "pressed" if pressed_token == apply_token else ("hover" if hover_token == apply_token else "normal")
        apply_fill, apply_border, apply_text = _button_state_colors("apply", apply_state, colors=C)
        _draw_rect(bx0, ay0, bx1, ay1, apply_fill)
        # _draw_aa_line_strip([(bx0, ay0), (bx1, ay0), (bx1, ay1), (bx0, ay1), (bx0, ay0)], apply_border, width=1.0)
        _draw_text_centered(bx0, ay0, bx1, ay1, _t(wm, "button.apply_curve", "APPLY CURVE"), size=btn_font, color=apply_text, truncate=True, pad=8)
        ui_map[area_ptr]["buttons_abs"].append({
            "rect": (region.x + bx0, region.y + ay0, region.x + bx1, region.y + ay1),
            "op": "apply",
            "kwargs": {},
            "id": apply_token,
        })

        by = y1 - 42
        if force_column:
            for b in [btn for row in _overlay_buttons(wm) for btn in row]:
                rx0 = bx0
                rx1 = bx1
                ry1 = by
                ry0 = ry1 - row_h
                # Skip buttons that would overlap the APPLY button.
                if ry0 < ay1 + gap:
                    break
                token = _button_token(b["op"], b["kwargs"])
                state = "pressed" if pressed_token == token else ("hover" if hover_token == token else "normal")
                kind = "auto_on" if (b["op"] == "toggle_auto" and wm.tlfc_auto_apply) else ("auto_off" if b["op"] == "toggle_auto" else "default")
                fill, border_col, text_col = _button_state_colors(kind, state, colors=C)
                _draw_rect(rx0, ry0, rx1, ry1, fill)
                # _draw_aa_line_strip([(rx0, ry0), (rx1, ry0), (rx1, ry1), (rx0, ry1), (rx0, ry0)], border_col, width=1.0)
                _draw_text_centered(rx0, ry0, rx1, ry1, b["label"], size=btn_font, color=text_col, truncate=True, pad=6)
                ui_map[area_ptr]["buttons_abs"].append({
                    "rect": (region.x + rx0, region.y + ry0, region.x + rx1, region.y + ry1),
                    "op": b["op"],
                    "kwargs": b["kwargs"],
                    "id": token,
                })
                by -= (row_h + gap)
        else:
            for row in _overlay_buttons(wm):
                cols = len(row)
                cell_w = (bx1 - bx0 - gap * (cols - 1)) / max(1, cols)
                ry1 = by
                ry0 = ry1 - row_h
                # Skip row if it would overlap the APPLY button.
                if ry0 < ay1 + gap:
                    break
                for i, b in enumerate(row):
                    rx0 = bx0 + i * (cell_w + gap)
                    rx1 = rx0 + cell_w
                    token = _button_token(b["op"], b["kwargs"])
                    state = "pressed" if pressed_token == token else ("hover" if hover_token == token else "normal")
                    kind = "auto_on" if (b["op"] == "toggle_auto" and wm.tlfc_auto_apply) else ("auto_off" if b["op"] == "toggle_auto" else "default")
                    fill, border_col, text_col = _button_state_colors(kind, state, colors=C)
                    _draw_rect(rx0, ry0, rx1, ry1, fill)
                    # _draw_aa_line_strip([(rx0, ry0), (rx1, ry0), (rx1, ry1), (rx0, ry1), (rx0, ry0)], border_col, width=1.0)
                    _draw_text_centered(rx0, ry0, rx1, ry1, b["label"], size=btn_font, color=text_col, truncate=True, pad=6)
                    ui_map[area_ptr]["buttons_abs"].append({
                        "rect": (region.x + rx0, region.y + ry0, region.x + rx1, region.y + ry1),
                        "op": b["op"],
                        "kwargs": b["kwargs"],
                        "id": token,
                    })
                by -= (row_h + gap)

        # Preset square buttons under existing buttons.
        if presets:
            cols = max(1, int((bx1 - bx0 + gap) // (tile + gap)))
            for idx, p in enumerate(presets):
                c = idx % cols
                r = idx // cols
                tx0 = bx0 + c * (tile + gap)
                tx1 = min(tx0 + tile, bx1)
                ty1 = by - r * (tile + gap)
                ty0 = ty1 - tile
                if ty0 < y0 + apply_h + 20:
                    break
                _draw_preset_tile(tx0, ty0, tx1, ty1, p, size_scale, colors=C)
                token = _button_token("preset_apply", {"idx": idx})
                if pressed_token == token:
                    outline = _button_state_colors("preset", "pressed", colors=C)[1]
                    _draw_aa_line_strip([(tx0, ty0), (tx1, ty0), (tx1, ty1), (tx0, ty1), (tx0, ty0)], outline, width=4.0)
                elif hover_token == token:
                    outline = _button_state_colors("preset", "hover", colors=C)[1]
                    _draw_aa_line_strip([(tx0, ty0), (tx1, ty0), (tx1, ty1), (tx0, ty1), (tx0, ty0)], outline, width=3.2)
                ui_map[area_ptr]["buttons_abs"].append({
                    "rect": (region.x + tx0, region.y + ty0, region.x + tx1, region.y + ty1),
                    "op": "preset_apply",
                    "kwargs": {"idx": idx},
                    "id": token,
                })
    else:
        # Not enough side space: move buttons under the grid and hide info text.
        ux0 = x0 + 10
        ux1 = x1 - 10
        by = sy0 - 10
        for row in _overlay_buttons(wm):
            cols = len(row)
            cell_w = (ux1 - ux0 - gap * (cols - 1)) / max(1, cols)
            ry1 = by
            ry0 = ry1 - row_h
            # Drop button rows that would overlap the APPLY button area.
            if ry0 < y0 + apply_h + 20:
                break
            for i, b in enumerate(row):
                rx0 = ux0 + i * (cell_w + gap)
                rx1 = rx0 + cell_w
                token = _button_token(b["op"], b["kwargs"])
                state = "pressed" if pressed_token == token else ("hover" if hover_token == token else "normal")
                kind = "auto_on" if (b["op"] == "toggle_auto" and wm.tlfc_auto_apply) else ("auto_off" if b["op"] == "toggle_auto" else "default")
                fill, border_col, text_col = _button_state_colors(kind, state, colors=C)
                _draw_rect(rx0, ry0, rx1, ry1, fill)
                # _draw_aa_line_strip([(rx0, ry0), (rx1, ry0), (rx1, ry1), (rx0, ry1), (rx0, ry0)], border_col, width=1.0)
                _draw_text_centered(rx0, ry0, rx1, ry1, b["label"], size=btn_font, color=text_col, truncate=True, pad=6)
                ui_map[area_ptr]["buttons_abs"].append({
                    "rect": (region.x + rx0, region.y + ry0, region.x + rx1, region.y + ry1),
                    "op": b["op"],
                    "kwargs": b["kwargs"],
                    "id": token,
                })
            by -= (row_h + gap)

        # Presets under existing buttons in compact layout.
        if presets:
            cols = max(2, int((ux1 - ux0 + gap) // (tile + gap)))
            for idx, p in enumerate(presets):
                c = idx % cols
                r = idx // cols
                tx0 = ux0 + c * (tile + gap)
                tx1 = min(tx0 + tile, ux1)
                ty1 = by - r * (tile + gap)
                ty0 = ty1 - tile
                if ty0 < y0 + apply_h + 20:
                    break
                _draw_preset_tile(tx0, ty0, tx1, ty1, p, size_scale, colors=C)
                token = _button_token("preset_apply", {"idx": idx})
                if pressed_token == token:
                    outline = _button_state_colors("preset", "pressed")[1]
                    _draw_aa_line_strip([(tx0, ty0), (tx1, ty0), (tx1, ty1), (tx0, ty1), (tx0, ty0)], outline, width=2.0)
                elif hover_token == token:
                    outline = _button_state_colors("preset", "hover")[1]
                    _draw_aa_line_strip([(tx0, ty0), (tx1, ty0), (tx1, ty1), (tx0, ty1), (tx0, ty0)], outline, width=1.6)
                ui_map[area_ptr]["buttons_abs"].append({
                    "rect": (region.x + tx0, region.y + ty0, region.x + tx1, region.y + ty1),
                    "op": "preset_apply",
                    "kwargs": {"idx": idx},
                    "id": token,
                })

        ay1 = y0 + 10 + apply_h
        ay0 = y0 + 10
        apply_token = _button_token("apply", {})
        apply_state = "pressed" if pressed_token == apply_token else ("hover" if hover_token == apply_token else "normal")
        apply_fill, apply_border, apply_text = _button_state_colors("apply", apply_state, colors=C)
        _draw_rect(ux0, ay0, ux1, ay1, apply_fill)
        # _draw_aa_line_strip([(ux0, ay0), (ux1, ay0), (ux1, ay1), (ux0, ay1), (ux0, ay0)], apply_border, width=1.0)
        _draw_text_centered(ux0, ay0, ux1, ay1, _t(wm, "button.apply_curve", "APPLY CURVE"), size=btn_font, color=apply_text, truncate=True, pad=8)
        ui_map[area_ptr]["buttons_abs"].append({
            "rect": (region.x + ux0, region.y + ay0, region.x + ux1, region.y + ay1),
            "op": "apply",
            "kwargs": {},
            "id": apply_token,
        })

    # Focused curve details (active selected curve if available).
    info = _focused_curve_info(ctx, selected_items)
    if wm.tlfc_show_info and side_has_space and info:
        info_size = 10
        info_step = 14
        info_lines = [
            "{}: {}".format(_t(wm, 'info.selected', 'Selected'), info['name']),
            "{}: {} | {}: {} | {}: {}".format(
                _t(wm, 'info.group', 'Group'),
                info['group'],
                _t(wm, 'info.mods', 'Mods'),
                info['modifiers'],
                _t(wm, 'info.extrap', 'Extrap'),
                info['extrapolation'],
            ),
            "{}: {} {} / {} {}".format(
                _t(wm, 'info.keys', 'Keys'),
                info['keys_selected'],
                _t(wm, 'info.selected_short', 'selected'),
                info['keys_total'],
                _t(wm, 'info.total', 'total'),
            ),
        ]
        if info["frame_span"]:
            fs0, fs1 = info["frame_span"]
            info_lines.append("{}: {:.2f} -> {:.2f}".format(_t(wm, 'info.frame_span', 'Frame span'), fs0, fs1))
        if info["value_span"]:
            vs0, vs1 = info["value_span"]
            info_lines.append("{}: {:.4f} -> {:.4f}".format(_t(wm, 'info.value_span', 'Value span'), vs0, vs1))
        if info["eval_now"] is not None:
            info_lines.append("{}: {:.4f}".format(_t(wm, 'info.value_now', 'Value @ current frame'), info['eval_now']))

        y = sy0 - 16
        info_x = x0 + 10
        info_w = max(60.0, sx1 - info_x - 8)
        for line in info_lines:
            _draw_text_clipped_left(info_x, y, info_w, line, size=info_size, color=C["info_text"], pad=2)
            y -= info_step
        _draw_text_clipped_left(info_x, y0 + 4, info_w, "{}: {}".format(_t(wm, 'info.curves', 'Curves'), len(selected_items)), size=info_size, color=C["info_footer_text"], pad=2)
    elif wm.tlfc_show_info and side_has_space:
        info_x = x0 + 10
        info_w = max(60.0, sx1 - info_x - 8)
        info_size = 14
        _draw_text_clipped_left(info_x, sy0 - 16, info_w, _t(wm, "info.no_selected_keys", "No selected keys."), size=info_size, color=C["info_empty_text"], pad=2)

    gpu.state.blend_set('NONE')
# ---------- Redraw timer ----------
def redraw_timer():
    ns = bpy.app.driver_namespace
    now = time.perf_counter()
    prev_tick = ns.get(REDRAW_LAST_TICK_KEY)
    expected_interval = float(ns.get(REDRAW_LAST_INTERVAL_KEY, 0.2))
    load_ewma = float(ns.get(REDRAW_LOAD_EWMA_KEY, 1.0))

    if isinstance(prev_tick, (int, float)):
        dt = max(0.0, now - float(prev_tick))
        if expected_interval > 1e-5:
            # Runtime interval inflation is a useful proxy for render/GPU pressure.
            ratio = dt / expected_interval
            load_ewma = (load_ewma * 0.85) + (ratio * 0.15)
    ns[REDRAW_LAST_TICK_KEY] = now

    wm = bpy.context.window_manager
    changed_enabled = _prune_invalid_enabled_areas()
    if changed_enabled and not _any_timeline_editor_enabled() and wm is not None:
        wm.tlfc_mouse_editing = False
        _reset_ui_state(wm)

    if wm is None or not _any_timeline_editor_enabled():
        _disable_runtime_handlers(clear_ui=True)
        ns[REDRAW_LAST_INTERVAL_KEY] = 0.25
        ns[REDRAW_LOAD_EWMA_KEY] = load_ewma
        return 0.25

    heavy_threshold = max(
        TLFC_PROPERTY_RANGES["tlfc_redraw_load_threshold"][0],
        _pref_float("tlfc_redraw_load_threshold", TLFC_PROPERTY_DEFAULTS["tlfc_redraw_load_threshold"]),
    )
    heavy_load = load_ewma >= heavy_threshold
    try:
        for w in bpy.data.window_managers:
            for win in w.windows:
                scr = win.screen
                if not scr:
                    continue
                for area in scr.areas:
                    if area.type in {'DOPESHEET_EDITOR', 'INFO', 'GRAPH_EDITOR'}:
                        area.tag_redraw()
    except Exception:
        pass

    if getattr(wm, "tlfc_hover_sidebar_edge", False) or getattr(wm, "tlfc_hover_sidebar", False):
        interval = 0.04 if heavy_load else 0.016
    else:
        interval = 0.35 if heavy_load else 0.2
    ns[REDRAW_LAST_INTERVAL_KEY] = interval
    ns[REDRAW_LOAD_EWMA_KEY] = load_ewma
    return interval

class TLFC_PT_editor_header_dropdown(bpy.types.Panel):
    bl_space_type = 'DOPESHEET_EDITOR'
    bl_region_type = 'HEADER'
    bl_label = 'Bezier Editor Settings'

    @classmethod
    def poll(cls, context):
        sp = context.space_data
        return _supports_editor_space(sp)

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        sp = context.space_data
        is_info_space = bool(sp and sp.type == 'INFO')

        col = layout.column(align=True)
        col.label(text=_t(wm, "panel.display", "Display"))
        if not is_info_space:
            col.prop(wm, "tlfc_sidebar_width", text=_t(wm, "panel.sidebar_width", "Sidebar Width"))
            col.prop(wm, "tlfc_outer_pad", text=_t(wm, "panel.outer_padding", "Outer Padding"))
            col.prop(wm, "tlfc_alpha", text=_t(wm, "panel.background_alpha", "Background Alpha"))
        col.prop(wm, "tlfc_graph_height_ratio", text=_t(wm, "panel.graph_height_ratio", "Graph Height Ratio"))
        col.prop(wm, "tlfc_samples", text=_t(wm, "panel.curve_samples", "Curve Samples"))
        col.prop(wm, "tlfc_display_size", text=_t(wm, "panel.display_size", "Display Size"))
        tog = col.row(align=True)
        tog.prop(wm, "tlfc_show_info", text=_t(wm, "panel.show_info", "Show Info"))
        tog.prop(wm, "tlfc_auto_apply", text=_t(wm, "panel.auto_apply", "Auto Apply"))
        col.prop(wm, "tlfc_grid_subdiv", text=_t(wm, "panel.grid_subdivisions", "Grid Subdivisions"))

        col.separator()
        col.label(text=_t(wm, "panel.editor", "Editor"))
        h1 = col.row(align=True)
        h1.prop(wm, "tlfc_h1x")
        h1.prop(wm, "tlfc_h1y")
        h2 = col.row(align=True)
        h2.prop(wm, "tlfc_h2x")
        h2.prop(wm, "tlfc_h2y")

        col.separator()
        nav = col.row(align=True)
        nav.operator("tlfc.editor_zoom", text=_t(wm, "button.zoom_in", "Zoom +")).mode = 'IN'
        nav.operator("tlfc.editor_zoom", text=_t(wm, "button.zoom_out", "Zoom -")).mode = 'OUT'
        nav.operator("tlfc.editor_zoom", text=_t(wm, "button.center", "Center")).mode = 'CENTER'

        ease = col.row(align=True)
        ease.operator("tlfc.set_interpolation", text=_t(wm, "button.linear", "Linear")).mode = 'LINEAR'
        ease.operator("tlfc.set_interpolation", text=_t(wm, "button.constant", "Constant")).mode = 'CONSTANT'
        ease.operator("tlfc.mirror_curve", text=_t(wm, "button.mirror", "Mirror"))
        ease.operator("tlfc.reset_curve", text=_t(wm, "button.reset", "Reset"))

        col.operator("tlfc.read_curve", text=_t(wm, "panel.read_curve", "Read curve from Keyframe"))
        col.operator("tlfc.save_preset", text=_t(wm, "button.save_preset", "Save Preset"))
        col.operator("tlfc.open_preset_file", text=_t(wm, "panel.open_preset_file", "Open Preset File"))

        col.separator()
        apply_row = col.row()
        apply_row.scale_y = 1.5
        apply_row.operator("tlfc.apply_curve", text=_t(wm, "button.apply_curve", "APPLY CURVE"))


def draw_tlfc_timeline_header(self, context):
    _prune_invalid_enabled_areas()
    sp = context.space_data
    if not sp or sp.type != 'DOPESHEET_EDITOR':
        return
    if not _pref_bool("tlfc_show_timeline_header_button", _prop_default("tlfc_show_timeline_header_button")):
        return
    wm = context.window_manager
    row = self.layout.row(align=True)
    is_on = _is_editor_enabled(context.area) and wm.tlfc_mouse_editing
    icon = 'IPO_BEZIER'
    row.operator("tlfc.toggle_editor_mode", text="", icon=icon, depress=is_on)
    row.popover(panel="TLFC_PT_editor_header_dropdown", text="")


def draw_tlfc_info_header(self, context):
    _prune_invalid_enabled_areas()
    sp = context.space_data
    if not sp or sp.type != 'INFO':
        return
    if not _pref_bool("tlfc_show_info_header_button", _prop_default("tlfc_show_info_header_button")):
        return
    wm = context.window_manager
    self.layout.separator_spacer()
    row = self.layout.row(align=True)
    row.alignment = 'RIGHT'
    is_on = _is_editor_enabled(context.area) and wm.tlfc_mouse_editing
    icon = 'IPO_BEZIER'
    row.operator("tlfc.toggle_editor_mode", text="", icon=icon, depress=is_on)
    row.popover(panel="TLFC_PT_editor_header_dropdown", text="")


class TLFC_PT_graph_editor_header_dropdown(bpy.types.Panel):
    bl_space_type = 'GRAPH_EDITOR'
    bl_region_type = 'HEADER'
    bl_label = 'Bezier Editor Settings'

    @classmethod
    def poll(cls, context):
        sp = context.space_data
        return _supports_editor_space(sp)

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        sp = context.space_data

        col = layout.column(align=True)
        col.label(text=_t(wm, "panel.display", "Display"))
        col.prop(wm, "tlfc_sidebar_width", text=_t(wm, "panel.sidebar_width", "Sidebar Width"))
        col.prop(wm, "tlfc_outer_pad", text=_t(wm, "panel.outer_padding", "Outer Padding"))
        col.prop(wm, "tlfc_alpha", text=_t(wm, "panel.background_alpha", "Background Alpha"))
        col.prop(wm, "tlfc_graph_height_ratio", text=_t(wm, "panel.graph_height_ratio", "Graph Height Ratio"))
        col.prop(wm, "tlfc_samples", text=_t(wm, "panel.curve_samples", "Curve Samples"))
        col.prop(wm, "tlfc_display_size", text=_t(wm, "panel.display_size", "Display Size"))
        tog = col.row(align=True)
        tog.prop(wm, "tlfc_show_info", text=_t(wm, "panel.show_info", "Show Info"))
        tog.prop(wm, "tlfc_auto_apply", text=_t(wm, "panel.auto_apply", "Auto Apply"))
        col.prop(wm, "tlfc_grid_subdiv", text=_t(wm, "panel.grid_subdivisions", "Grid Subdivisions"))

        col.separator()
        col.label(text=_t(wm, "panel.editor", "Editor"))
        h1 = col.row(align=True)
        h1.prop(wm, "tlfc_h1x")
        h1.prop(wm, "tlfc_h1y")
        h2 = col.row(align=True)
        h2.prop(wm, "tlfc_h2x")
        h2.prop(wm, "tlfc_h2y")

        col.separator()
        nav = col.row(align=True)
        nav.operator("tlfc.editor_zoom", text=_t(wm, "button.zoom_in", "Zoom +")).mode = 'IN'
        nav.operator("tlfc.editor_zoom", text=_t(wm, "button.zoom_out", "Zoom -")).mode = 'OUT'
        nav.operator("tlfc.editor_zoom", text=_t(wm, "button.center", "Center")).mode = 'CENTER'

        ease = col.row(align=True)
        ease.operator("tlfc.set_interpolation", text=_t(wm, "button.linear", "Linear")).mode = 'LINEAR'
        ease.operator("tlfc.set_interpolation", text=_t(wm, "button.constant", "Constant")).mode = 'CONSTANT'
        ease.operator("tlfc.mirror_curve", text=_t(wm, "button.mirror", "Mirror"))
        ease.operator("tlfc.reset_curve", text=_t(wm, "button.reset", "Reset"))

        col.operator("tlfc.read_curve", text=_t(wm, "panel.read_curve", "Read curve from Keyframe"))
        col.operator("tlfc.save_preset", text=_t(wm, "button.save_preset", "Save Preset"))
        col.operator("tlfc.open_preset_file", text=_t(wm, "panel.open_preset_file", "Open Preset File"))

        col.separator()
        apply_row = col.row()
        apply_row.scale_y = 1.5
        apply_row.operator("tlfc.apply_curve", text=_t(wm, "button.apply_curve", "APPLY CURVE"))


def draw_tlfc_graph_header(self, context):
    _prune_invalid_enabled_areas()
    sp = context.space_data
    if not sp or sp.type != 'GRAPH_EDITOR':
        return
    if not _pref_bool("tlfc_show_graph_header_button", _prop_default("tlfc_show_graph_header_button")):
        return
    wm = context.window_manager
    row = self.layout.row(align=True)
    is_on = _is_editor_enabled(context.area) and wm.tlfc_mouse_editing
    icon = 'IPO_BEZIER'
    row.operator("tlfc.toggle_editor_mode", text="", icon=icon, depress=is_on)
    row.popover(panel="TLFC_PT_graph_editor_header_dropdown", text="")


class TLFC_OT_toggle_editor_mode(bpy.types.Operator):
    bl_idname = "tlfc.toggle_editor_mode"
    bl_label = "Toggle Editor Mode"
    bl_description = "Enable or disable the curve editor overlay in this area"

    def execute(self, context):
        wm = context.window_manager
        sp = context.space_data
        area = context.area
        if not area or not _supports_editor_space(sp):
            return {'CANCELLED'}

        if _is_editor_enabled(area):
            _set_editor_enabled(area, False)
            if not _any_timeline_editor_enabled():
                wm.tlfc_mouse_editing = False
                _reset_ui_state(wm)
                _disable_runtime_handlers(clear_ui=True)
        else:
            _set_editor_enabled_exclusive(area)
            _reset_ui_state(wm)
            _ensure_runtime_handlers()

            ns = bpy.app.driver_namespace
            ns[SWITCH_BLOCK_UNTIL_KEY] = time.perf_counter() + (1.0 / 30.0)

            ui_map = ns.get(EDITOR_UI_KEY)
            area_key = _area_key(area)
            if isinstance(ui_map, dict):
                for k in list(ui_map.keys()):
                    if k != area_key:
                        ui_map.pop(k, None)

            # Restart modal so stale handlers from another area are invalidated.
            if wm.tlfc_mouse_editing:
                wm.tlfc_mouse_editing = False
            try:
                bpy.ops.tlfc.mouse_edit_curve('INVOKE_DEFAULT')
            except Exception:
                wm.tlfc_mouse_editing = True

        if not _any_timeline_editor_enabled():
            _reset_ui_state(wm)
            _disable_runtime_handlers(clear_ui=True)
        _tag_redraw_dopesheet()
        return {'FINISHED'}


class TLFC_OT_mouse_edit_curve(bpy.types.Operator):
    bl_idname = "tlfc.mouse_edit_curve"
    bl_label = "Toggle Mouse Edit"
    bl_description = "Left drag handles, middle drag to pan the editor"

    _drag = None
    _cursor = None
    _session_id = 0

    def _set_modal_cursor(self, context, cursor):
        if self._cursor == cursor:
            return
        try:
            if cursor:
                context.window.cursor_modal_set(cursor)
            else:
                context.window.cursor_modal_restore()
            self._cursor = cursor
        except Exception:
            self._cursor = None

    def _clear_modal_cursor(self, context):
        if self._cursor is None:
            return
        try:
            context.window.cursor_modal_restore()
        except Exception:
            pass
        self._cursor = None

    def invoke(self, context, event):
        wm = context.window_manager
        if wm.tlfc_mouse_editing:
            wm.tlfc_mouse_editing = False
            _reset_ui_state(wm)
            self._clear_modal_cursor(context)
            return {'FINISHED'}
        sp = context.space_data
        if not _supports_editor_space(sp) or not _is_editor_enabled(context.area):
            self.report({'WARNING'}, _t(wm, "report.enable_overlay_first", "Enable Overlay first"))
            return {'CANCELLED'}
        _ensure_runtime_handlers()
        wm.tlfc_mouse_editing = True
        _reset_ui_state(wm)
        self._drag = None
        self._cursor = None
        ns = bpy.app.driver_namespace
        self._session_id = int(ns.get(MODAL_SESSION_KEY, 0)) + 1
        ns[MODAL_SESSION_KEY] = self._session_id
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        wm = context.window_manager
        ns = bpy.app.driver_namespace
        if self._session_id != int(ns.get(MODAL_SESSION_KEY, self._session_id)):
            self._drag = None
            self._clear_modal_cursor(context)
            return {'CANCELLED'}

        if not wm.tlfc_mouse_editing:
            _reset_ui_state(wm, clear_buttons=False, clear_handle=False)
            self._clear_modal_cursor(context)
            return {'CANCELLED'}

        if event.type == 'ESC' and event.value == 'PRESS':
            wm.tlfc_mouse_editing = False
            self._drag = None
            _reset_ui_state(wm)
            self._clear_modal_cursor(context)
            return {'CANCELLED'}

        if event.type == 'RIGHTMOUSE' and event.value == 'PRESS':
            return {'PASS_THROUGH'}

        ui_map = bpy.app.driver_namespace.get(EDITOR_UI_KEY)
        area_ptr = context.area.as_pointer() if context.area else 0
        ui = ui_map.get(area_ptr) if isinstance(ui_map, dict) else None
        if not ui:
            _reset_ui_state(wm)
            self._clear_modal_cursor(context)
            return {'PASS_THROUGH'}

        ui_area_ptr = int(ui.get("area", area_ptr))
        ui_area = _find_area_by_ptr(ui_area_ptr)

        # If this area changed editor type while overlay was open, close it cleanly.
        cur_space = ui_area.spaces.active if ui_area and ui_area.spaces else None
        cur_space_type = getattr(cur_space, "type", "") if cur_space else ""
        if (cur_space_type != ui.get("space_type", cur_space_type)) or (not _supports_editor_space(cur_space)):
            if ui_area is not None:
                _set_editor_enabled(ui_area, False)
            else:
                _enabled_areas_map().pop(ui_area_ptr, None)
            wm.tlfc_mouse_editing = False
            self._drag = None
            _reset_ui_state(wm)
            if not _any_timeline_editor_enabled():
                _disable_runtime_handlers(clear_ui=True)
            self._clear_modal_cursor(context)
            _tag_redraw_dopesheet()
            return {'CANCELLED'}

        block_until = float(ns.get(SWITCH_BLOCK_UNTIL_KEY, 0.0))
        if time.perf_counter() < block_until:
            self._drag = None
            _reset_ui_state(wm)
            self._set_modal_cursor(context, None)
            return {'RUNNING_MODAL'}

        mx_abs = event.mouse_x
        my_abs = event.mouse_y
        is_info_ui = (ui.get("space_type") == 'INFO')
        panel_rect = ui.get("panel_rect_abs")
        wm.tlfc_hover_sidebar = _point_in_rect(mx_abs, my_abs, panel_rect) if panel_rect else False
        edge_hit = float(ui.get("sidebar_edge_hit_px", SIDEBAR_EDGE_HIT_PX))
        edge_x = float(ui.get("sidebar_edge_abs", panel_rect[0] if panel_rect else 0.0))
        if panel_rect:
            _, py0, _, py1 = panel_rect
            wm.tlfc_hover_sidebar_edge = (not is_info_ui) and (py0 <= my_abs <= py1) and (abs(mx_abs - edge_x) <= edge_hit)
        else:
            wm.tlfc_hover_sidebar_edge = False
        rx0, ry0, rx1, ry1 = ui["rect_abs"]
        inside = (rx0 <= mx_abs <= rx1 and ry0 <= my_abs <= ry1)
        sx0, sy0, sx1, sy1 = ui["rect"]
        # Convert absolute window coords to WINDOW-region local editor coords.
        mx = mx_abs - rx0 + sx0
        my = my_abs - ry0 + sy0

        # Check handle hover state
        h1 = ui["h1_abs"]
        h2 = ui["h2_abs"]
        d1 = (mx_abs - h1[0]) * (mx_abs - h1[0]) + (my_abs - h1[1]) * (my_abs - h1[1])
        d2 = (mx_abs - h2[0]) * (mx_abs - h2[0]) + (my_abs - h2[1]) * (my_abs - h2[1])
        handle_hit_sq = float(HANDLE_HIT_RADIUS_PX * HANDLE_HIT_RADIUS_PX)

        # Store which handle is being hovered (if any)
        if d1 <= handle_hit_sq and not self._drag:
            wm.tlfc_hover_handle = "h1"
        elif d2 <= handle_hit_sq and not self._drag:
            wm.tlfc_hover_handle = "h2"
        else:
            wm.tlfc_hover_handle = ""

        hovered_token = ""
        for btn in ui.get("buttons_abs", []):
            if _point_in_rect(mx_abs, my_abs, btn["rect"]):
                hovered_token = btn.get("id", "")
                break
        wm.tlfc_hover_button = hovered_token

        # Graph separator hover detection — suppress when a handle is hovered
        # so the user can grab handles near the separator without accidentally resizing.
        sep_rect = ui.get("graph_sep_abs")
        handle_nearby = (wm.tlfc_hover_handle != "")
        wm.tlfc_hover_graph_sep = (not handle_nearby) and (_point_in_rect(mx_abs, my_abs, sep_rect) if sep_rect else False)

        if self._drag == "sidebar" or getattr(wm, "tlfc_dragging_sidebar", False):
            self._set_modal_cursor(context, 'MOVE_X')
        elif self._drag == "graph_sep" or wm.tlfc_hover_graph_sep:
            self._set_modal_cursor(context, 'MOVE_Y')
        elif hovered_token:
            self._set_modal_cursor(context, 'HAND_POINT')
        elif wm.tlfc_hover_sidebar_edge:
            self._set_modal_cursor(context, 'MOVE_X')
        else:
            self._set_modal_cursor(context, None)

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if wm.tlfc_hover_sidebar_edge:
                self._drag = "sidebar"
                wm.tlfc_dragging_sidebar = True
                wm.tlfc_pressed_button = ""
                return {'RUNNING_MODAL'}

            # Handles take priority over the separator to prevent accidental
            # resize when grabbing a handle near the bottom of the graph.
            h1 = ui["h1_abs"]
            h2 = ui["h2_abs"]
            d1 = (mx_abs - h1[0]) * (mx_abs - h1[0]) + (my_abs - h1[1]) * (my_abs - h1[1])
            d2 = (mx_abs - h2[0]) * (mx_abs - h2[0]) + (my_abs - h2[1]) * (my_abs - h2[1])
            if d1 <= handle_hit_sq:
                self._drag = "h1"
                wm.tlfc_pressed_button = ""
                return {'RUNNING_MODAL'}
            if d2 <= handle_hit_sq:
                self._drag = "h2"
                wm.tlfc_pressed_button = ""
                return {'RUNNING_MODAL'}

            if wm.tlfc_hover_graph_sep:
                self._drag = "graph_sep"
                wm.tlfc_pressed_button = ""
                return {'RUNNING_MODAL'}

            for btn in ui.get("buttons_abs", []):
                if _point_in_rect(mx_abs, my_abs, btn["rect"]):
                    wm.tlfc_pressed_button = btn.get("id", "")
                    _invoke_overlay_button(context, btn["op"], btn.get("kwargs", {}), shift=event.shift)
                    if context.area:
                        context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
            wm.tlfc_pressed_button = ""
            return {'PASS_THROUGH'}

        if event.type == 'MIDDLEMOUSE' and event.value == 'PRESS':
            if not inside:
                return {'PASS_THROUGH'}
            self._drag = "pan"
            wm.tlfc_pan_start_x = mx_abs
            wm.tlfc_pan_start_y = my_abs
            wm.tlfc_pan_origin_x = wm.tlfc_view_pan_x
            wm.tlfc_pan_origin_y = wm.tlfc_view_pan_y
            return {'RUNNING_MODAL'}

        # Mouse wheel zoom when hovering over the editor
        if event.type == 'WHEELUPMOUSE' and inside:
            # Zoom in
            old_zoom = wm.tlfc_view_zoom
            new_zoom = min(_prop_range("tlfc_view_zoom")[1], old_zoom * TLFC_UI_NUMBERS["zoom_wheel_factor"])

            # Zoom towards mouse cursor position
            if old_zoom > 0:
                # Get mouse position in editor space
                sx0, sy0, sx1, sy1 = ui["rect"]
                mx_editor, my_editor = _screen_to_editor(mx, my, sx0, sy0, sx1, sy1, old_zoom, wm.tlfc_view_pan_x, wm.tlfc_view_pan_y)

                # Adjust pan to keep the mouse point fixed
                zoom_factor = new_zoom / old_zoom
                wm.tlfc_view_pan_x = wm.tlfc_view_pan_x * zoom_factor + (mx_editor - 0.5) * (new_zoom - old_zoom)
                wm.tlfc_view_pan_y = wm.tlfc_view_pan_y * zoom_factor + (my_editor - 0.5) * (new_zoom - old_zoom)

                # Apply pan limits
                max_pan = TLFC_UI_NUMBERS["pan_limit_base"] / max(TLFC_UI_NUMBERS["pan_zoom_floor"], new_zoom)
                wm.tlfc_view_pan_x = max(-max_pan, min(max_pan, wm.tlfc_view_pan_x))
                wm.tlfc_view_pan_y = max(-max_pan, min(max_pan, wm.tlfc_view_pan_y))

            wm.tlfc_view_zoom = new_zoom
            if context.area:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'WHEELDOWNMOUSE' and inside:
            # Zoom out
            old_zoom = wm.tlfc_view_zoom
            new_zoom = max(_prop_range("tlfc_view_zoom")[0], old_zoom / TLFC_UI_NUMBERS["zoom_wheel_factor"])

            # Zoom towards mouse cursor position
            if old_zoom > 0:
                # Get mouse position in editor space
                sx0, sy0, sx1, sy1 = ui["rect"]
                mx_editor, my_editor = _screen_to_editor(mx, my, sx0, sy0, sx1, sy1, old_zoom, wm.tlfc_view_pan_x, wm.tlfc_view_pan_y)

                # Adjust pan to keep the mouse point fixed
                zoom_factor = new_zoom / old_zoom
                wm.tlfc_view_pan_x = wm.tlfc_view_pan_x * zoom_factor + (mx_editor - 0.5) * (new_zoom - old_zoom)
                wm.tlfc_view_pan_y = wm.tlfc_view_pan_y * zoom_factor + (my_editor - 0.5) * (new_zoom - old_zoom)

                # Apply pan limits
                max_pan = TLFC_UI_NUMBERS["pan_limit_base"] / max(TLFC_UI_NUMBERS["pan_zoom_floor"], new_zoom)
                wm.tlfc_view_pan_x = max(-max_pan, min(max_pan, wm.tlfc_view_pan_x))
                wm.tlfc_view_pan_y = max(-max_pan, min(max_pan, wm.tlfc_view_pan_y))

            wm.tlfc_view_zoom = new_zoom
            if context.area:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type in {'LEFTMOUSE', 'MIDDLEMOUSE'} and event.value == 'RELEASE':
            had_drag = self._drag is not None
            had_pressed = bool(wm.tlfc_pressed_button)
            was_sidebar_drag = self._drag == "sidebar"

            if event.type == 'LEFTMOUSE':
                wm.tlfc_pressed_button = ""
            if was_sidebar_drag:
                wm.tlfc_dragging_sidebar = False

            # Only capture release if this modal was actively handling interaction.
            if had_drag or had_pressed:
                self._drag = None
                return {'RUNNING_MODAL'}

            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE' and self._drag == "sidebar":
            if is_info_ui:
                self._drag = None
                wm.tlfc_dragging_sidebar = False
                return {'PASS_THROUGH'}
            panel_x1_abs = float(ui.get("panel_x1_abs", panel_rect[2] if panel_rect else mx_abs))
            region_w = max(1.0, float(ui.get("region_w", context.region.width if context.region else 1.0)))
            sidebar_w_px = max(TLFC_UI_NUMBERS["sidebar_min_px"], min(region_w, panel_x1_abs - mx_abs))
            new_pct = (sidebar_w_px / region_w) * 100.0
            wm.tlfc_sidebar_width = _clamp_prop("tlfc_sidebar_width", new_pct)
            if context.area:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE' and self._drag in {"h1", "h2"}:
            nx, ny = _screen_to_editor(mx, my, sx0, sy0, sx1, sy1, wm.tlfc_view_zoom, wm.tlfc_view_pan_x, wm.tlfc_view_pan_y)
            _mode_val = getattr(wm, 'tlfc_sidebar_mode', _prop_default("tlfc_sidebar_mode"))
            if _mode_val == 'ELASTIC':
                nx = _clamp01(nx)
                ny = _clamp01(ny)
                if event.ctrl:
                    nx, ny = _snap_grid(nx, ny, wm.tlfc_grid_subdiv)
                    nx = _clamp01(nx)
                    ny = _clamp01(ny)
                if self._drag == "h1":
                    wm.tlfc_elastic_amplitude = _unit_to_prop("tlfc_elastic_amplitude", ny)
                else:
                    wm.tlfc_elastic_period = _unit_to_prop("tlfc_elastic_period", nx)
            else:
                nx, ny = _constrain_handle(self._drag, nx, ny)
                if event.ctrl:
                    nx, ny = _snap_grid(nx, ny, wm.tlfc_grid_subdiv)
                    nx, ny = _constrain_handle(self._drag, nx, ny)
                if event.shift:
                    nx, ny = _snap_edge(nx, ny, wm.tlfc_snap_threshold)
                    nx, ny = _constrain_handle(self._drag, nx, ny)
                if self._drag == "h1":
                    wm.tlfc_h1x = nx
                    wm.tlfc_h1y = ny
                else:
                    wm.tlfc_h2x = nx
                    wm.tlfc_h2y = ny

            if wm.tlfc_auto_apply:
                bpy.ops.tlfc.apply_curve()

            # Redraw all dopesheet areas for lower perceived drag latency.
            try:
                for w in bpy.data.window_managers:
                    for win in w.windows:
                        scr = win.screen
                        if not scr:
                            continue
                        for area in scr.areas:
                            if area.type in {'DOPESHEET_EDITOR', 'INFO', 'GRAPH_EDITOR'}:
                                area.tag_redraw()
            except Exception:
                if context.area:
                    context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE' and self._drag == "graph_sep":
            region_y = context.region.y if context.region else 0
            sy1_abs = float(ui.get("graph_sep_sy1", my_abs))
            available_h = max(1.0, float(ui.get("graph_sep_available_h", 1.0)))
            # sy1_abs is the top of the graph area in window space.
            # (sy1_abs - my_abs) is the current height of the graph.
            new_ratio = (sy1_abs - my_abs) / available_h
            clamped = _clamp_prop("tlfc_graph_height_ratio", new_ratio)
            wm.tlfc_graph_height_ratio = clamped
            # Persist to addon preferences so it survives restarts.
            prefs = _addon_prefs()
            if prefs is not None:
                prefs.tlfc_graph_height_ratio = clamped
            if context.area:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE' and self._drag == "pan":
            dx = mx_abs - wm.tlfc_pan_start_x
            dy = my_abs - wm.tlfc_pan_start_y
            rw = max(1.0, sx1 - sx0)
            rh = max(1.0, sy1 - sy0)
            new_pan_x = wm.tlfc_pan_origin_x + (dx / rw)
            new_pan_y = wm.tlfc_pan_origin_y + (dy / rh)

            # Clamp pan to reasonable limits based on zoom
            # Allow panning to show content within (-1, -1) to (2, 2) in normalized space
            zoom = wm.tlfc_view_zoom
            max_pan = TLFC_UI_NUMBERS["pan_limit_base"] / max(TLFC_UI_NUMBERS["pan_zoom_floor"], zoom)  # More pan allowed when zoomed in
            new_pan_x = max(-max_pan, min(max_pan, new_pan_x))
            new_pan_y = max(-max_pan, min(max_pan, new_pan_y))

            wm.tlfc_view_pan_x = new_pan_x
            wm.tlfc_view_pan_y = new_pan_y
            if context.area:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        return {'PASS_THROUGH'}


class TLFC_OT_editor_zoom(bpy.types.Operator):
    bl_idname = "tlfc.editor_zoom"
    bl_label = "Bezier Editor Zoom"
    bl_description = "Zoom the editor view in, out, or reset to center"
    mode: bpy.props.EnumProperty(
        items=[
            ('IN', 'In', ''),
            ('OUT', 'Out', ''),
            ('CENTER', 'Center', ''),
        ]
    )

    def execute(self, context):
        wm = context.window_manager
        if self.mode == 'IN':
            wm.tlfc_view_zoom = min(_prop_range("tlfc_view_zoom")[1], wm.tlfc_view_zoom * TLFC_UI_NUMBERS["zoom_button_factor"])
        elif self.mode == 'OUT':
            wm.tlfc_view_zoom = max(_prop_range("tlfc_view_zoom")[0], wm.tlfc_view_zoom / TLFC_UI_NUMBERS["zoom_button_factor"])
        else:
            wm.tlfc_view_zoom = _prop_default("tlfc_view_zoom")
            wm.tlfc_view_pan_x = _prop_default("tlfc_view_pan_x")
            wm.tlfc_view_pan_y = _prop_default("tlfc_view_pan_y")
        return {'FINISHED'}


class TLFC_OT_apply_curve(bpy.types.Operator):
    bl_idname = "tlfc.apply_curve"
    bl_label = "Apply Curve"
    bl_description = "Apply the current editor curve to selected keyframe segments"

    def execute(self, context):
        wm = context.window_manager
        _mode_val = getattr(wm, 'tlfc_sidebar_mode', _prop_default("tlfc_sidebar_mode"))
        if _mode_val == 'ELASTIC':
            amplitude = _clamp_prop("tlfc_elastic_amplitude", getattr(wm, 'tlfc_elastic_amplitude', _prop_default("tlfc_elastic_amplitude")))
            period = _clamp_prop("tlfc_elastic_period", getattr(wm, 'tlfc_elastic_period', _prop_default("tlfc_elastic_period")))
            pairs = 0
            curves = set()
            for fc, k0, k1 in _iter_selected_segments(context):
                if _apply_elastic_to_segment(fc, k0, k1, amplitude, period):
                    fc.update()
                    pairs += 1
                    curves.add(id(fc))
            if pairs == 0:
                self.report({'WARNING'}, _t(wm, "report.no_selected_pairs", "No selected keyframe pairs found to apply"))
                return {'CANCELLED'}
            self.report({'INFO'}, _t(wm, "report.baked_elastic", "Baked elastic curve to {pairs} segment(s) on {curves} F-curve(s)").format(pairs=pairs, curves=len(curves)))
            return {'FINISHED'}
        h1x, h1y = wm.tlfc_h1x, wm.tlfc_h1y
        h2x, h2y = wm.tlfc_h2x, wm.tlfc_h2y
        pairs = 0
        curves = set()
        for fc, k0, k1 in _iter_selected_segments(context):
            if _apply_editor_curve_to_segment(k0, k1, h1x, h1y, h2x, h2y):
                fc.update()
                pairs += 1
                curves.add(id(fc))
        if pairs == 0:
            self.report({'WARNING'}, _t(wm, "report.no_selected_pairs", "No selected keyframe pairs found to apply"))
            return {'CANCELLED'}
        self.report({'INFO'}, _t(wm, "report.applied_curve", "Applied curve to {pairs} segment(s) on {curves} F-curve(s)").format(pairs=pairs, curves=len(curves)))
        return {'FINISHED'}


class TLFC_OT_set_interpolation(bpy.types.Operator):
    bl_idname = "tlfc.set_interpolation"
    bl_label = "Set Interpolation"
    bl_description = "Set interpolation mode for selected keyframes"
    mode: bpy.props.EnumProperty(
        items=[
            ('LINEAR', 'Linear', ''),
            ('CONSTANT', 'Constant', ''),
        ]
    ) # type: ignore

    def execute(self, context):
        changed = 0
        for _fc, sel_keys, _all_keys in _selected_fcurves_with_selected_keys(context):
            for kp in sel_keys:
                kp.interpolation = self.mode
                changed += 1
        if changed == 0:
            self.report({'WARNING'}, _t(context.window_manager, "report.no_selected_keyframes", "No selected keyframes"))
            return {'CANCELLED'}
        self.report({'INFO'}, _t(context.window_manager, "report.set_interpolation", "Set {count} keyframe(s) to {mode}").format(count=changed, mode=self.mode.title()))
        return {'FINISHED'}


class TLFC_OT_mirror_curve(bpy.types.Operator):
    bl_idname = "tlfc.mirror_curve"
    bl_label = "Mirror Handle"
    bl_description = "Mirror Bezier handles across the center"

    def execute(self, context):
        wm = context.window_manager
        h1x, h1y = wm.tlfc_h1x, wm.tlfc_h1y
        h2x, h2y = wm.tlfc_h2x, wm.tlfc_h2y
        nh1x, nh1y = 1.0 - h2x, 1.0 - h2y
        nh2x, nh2y = 1.0 - h1x, 1.0 - h1y
        wm.tlfc_h1x, wm.tlfc_h1y = _constrain_handle("h1", nh1x, nh1y)
        wm.tlfc_h2x, wm.tlfc_h2y = _constrain_handle("h2", nh2x, nh2y)
        return {'FINISHED'}

class TLFC_OT_reset_curve(bpy.types.Operator):
    bl_idname = "tlfc.reset_curve"
    bl_label = "Reset Curve"
    bl_description = "Reset Bezier handles to default values"

    def execute(self, context):
        wm = context.window_manager
        wm.tlfc_h1x, wm.tlfc_h1y = _prop_default("tlfc_h1x"), _prop_default("tlfc_h1y")
        wm.tlfc_h2x, wm.tlfc_h2y = _prop_default("tlfc_h2x"), _prop_default("tlfc_h2y")
        return {'FINISHED'}


class TLFC_OT_read_curve(bpy.types.Operator):
    bl_idname = "tlfc.read_curve"
    bl_label = "Read Curve from Keyframe"
    bl_description = "Read curve settings from the selected keyframe segment"

    def execute(self, context):
        seg = _segment_from_selected_key(context, _selected_fcurves_with_selected_keys(context))
        if seg is None:
            self.report({'WARNING'}, _t(context.window_manager, "report.need_selected_key_with_next", "Need a selected key with a next keyframe"))
            return {'CANCELLED'}
        wm = context.window_manager
        k0 = seg.get("k0")
        k1 = seg.get("k1")
        is_elastic = False
        try:
            if k0 and getattr(k0, "interpolation", "") == 'ELASTIC':
                is_elastic = True
            elif k1 and getattr(k1, "interpolation", "") == 'ELASTIC':
                is_elastic = True
        except Exception:
            is_elastic = False

        if is_elastic and k0 is not None:
            wm.tlfc_sidebar_mode = 'ELASTIC'
            amp = float(getattr(k0, "amplitude", _prop_default("tlfc_elastic_amplitude")))
            per = float(getattr(k0, "period", _prop_default("tlfc_elastic_period")))
            df = float(seg.get("df", 1.0))
            if abs(df) > 1e-8:
                per = per / df
            wm.tlfc_elastic_amplitude = _clamp_prop("tlfc_elastic_amplitude", amp)
            wm.tlfc_elastic_period = _clamp_prop("tlfc_elastic_period", per)
            self.report({'INFO'}, _t(wm, "report.elastic_loaded", "Elastic curve loaded from selected key segment"))
        else:
            wm.tlfc_sidebar_mode = 'BEZIER'
            interp = getattr(k0, "interpolation", "BEZIER") if k0 else "BEZIER"

            if interp == 'LINEAR':
                # Linear is a straight diagonal — handles at 1/3 and 2/3 along the diagonal.
                wm.tlfc_h1x, wm.tlfc_h1y = 1/3, 1/3
                wm.tlfc_h2x, wm.tlfc_h2y = 2/3, 2/3
                self.report({'INFO'}, _t(wm, "report.linear_loaded", "Linear curve loaded from selected key segment"))
            elif interp == 'CONSTANT':
                # Constant holds value then steps — flat handles at the bottom.
                wm.tlfc_h1x, wm.tlfc_h1y = 1/3, 0.0
                wm.tlfc_h2x, wm.tlfc_h2y = 2/3, 0.0
                self.report({'INFO'}, _t(wm, "report.constant_loaded", "Constant curve loaded from selected key segment"))
            else:
                # BEZIER: only trust stored handle positions when handle types are
                # FREE or ALIGNED — those actually define the curve shape.
                # AUTO/VECTOR handles are auto-computed; raw positions don't match
                # what the curve looks like, so fall back to the linear shape.
                trusted_types = {'FREE', 'ALIGNED'}
                h0_type = getattr(k0, "handle_right_type", "FREE") if k0 else "FREE"
                h1_type = getattr(k1, "handle_left_type", "FREE") if k1 else "FREE"
                if h0_type in trusted_types and h1_type in trusted_types:
                    wm.tlfc_h1x, wm.tlfc_h1y = seg["c1"]
                    wm.tlfc_h2x, wm.tlfc_h2y = seg["c2"]
                else:
                    wm.tlfc_h1x, wm.tlfc_h1y = 1/3, 1/3
                    wm.tlfc_h2x, wm.tlfc_h2y = 2/3, 2/3
                self.report({'INFO'}, _t(wm, "report.bezier_loaded", "Bezier curve loaded from selected key segment"))
        return {'FINISHED'}


class TLFC_OT_save_preset(bpy.types.Operator):
    bl_idname = "tlfc.save_preset"
    bl_label = "Save Preset"
    bl_description = "Save the current curve (Bezier or Elastic) as a preset"

    def execute(self, context):
        ok = _add_current_preset(context.window_manager)
        if not ok:
            self.report({'WARNING'}, _t(context.window_manager, "report.preset_save_failed", "Failed to save preset file"))
            return {'CANCELLED'}
        self.report({'INFO'}, _t(context.window_manager, "report.preset_saved", "Preset saved"))
        return {'FINISHED'}


class TLFC_OT_open_preset_file(bpy.types.Operator):
    bl_idname = "tlfc.open_preset_file"
    bl_label = "Open Preset File"
    bl_description = "Open the preset file location on disk"

    def execute(self, context):
        path = os.path.join(bpy.utils.extension_path_user(ADDON_MODULE_KEY, create=True), PRESET_FILE)
        # Ensure file exists so opening does not fail on first use.
        if not os.path.exists(path):
            _save_presets(list(_load_presets()))

        try:
            bpy.ops.wm.path_open(filepath=path)
            self.report({'INFO'}, _t(context.window_manager, "report.preset_opened", "Opened preset file"))
            return {'FINISHED'}
        except Exception:
            self.report({'WARNING'}, _t(context.window_manager, "report.preset_path", "Preset file: {path}").format(path=path))
            return {'CANCELLED'}


class TLFC_AP_addon_preferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_MODULE_KEY
    tlfc_redraw_load_threshold: bpy.props.FloatProperty(
        name="Redraw Load Threshold",
        description="Increase redraw interval when measured redraw load exceeds this threshold",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_redraw_load_threshold"],
        min=TLFC_PROPERTY_RANGES["tlfc_redraw_load_threshold"][0],
        max=TLFC_PROPERTY_RANGES["tlfc_redraw_load_threshold"][1],
    )
    tlfc_show_timeline_header_button: bpy.props.BoolProperty(
        name="Show Timeline Header Button",
        description="Show Bezier editor toggle and dropdown in Timeline header",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_show_timeline_header_button"],
    )
    tlfc_show_info_header_button: bpy.props.BoolProperty(
        name="Show Info Header Button",
        description="Show Bezier editor toggle and dropdown in Info header",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_show_info_header_button"],
    )
    tlfc_show_graph_header_button: bpy.props.BoolProperty(
        name="Show Graph Header Button",
        description="Show Bezier editor toggle and dropdown in Graph Editor header",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_show_graph_header_button"],
    )
    tlfc_graph_height_ratio: bpy.props.FloatProperty(
        name="Graph Height Ratio",
        description="Height of the editor graph as a ratio of available panel height (persistent)",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_graph_height_ratio"],
        min=TLFC_PROPERTY_RANGES["tlfc_graph_height_ratio"][0],
        max=TLFC_PROPERTY_RANGES["tlfc_graph_height_ratio"][1],
        subtype='FACTOR',
    )

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager if context and context.window_manager else bpy.context.window_manager
        col = layout.column(align=True)
        col.label(text=_t(wm, "prefs.title", "Bezier Curve Editor Overlay"))
        col.prop(self, "tlfc_redraw_load_threshold", text=_t(wm, "prefs.redraw_threshold", "Redraw Load Threshold"))
        rowB = col.row(align=True)
        rowB.prop(self, "tlfc_show_timeline_header_button", text=_t(wm, "prefs.show_timeline_button", "Show Timeline Header Button"))
        rowB.prop(self, "tlfc_show_info_header_button", text=_t(wm, "prefs.show_info_button", "Show Info Header Button"))
        rowB.prop(self, "tlfc_show_graph_header_button", text=_t(wm, "prefs.show_graph_button", "Show Graph Header Button"))


classes = (
    TLFC_AP_addon_preferences,
    TLFC_OT_toggle_editor_mode,
    TLFC_OT_mouse_edit_curve,
    TLFC_OT_editor_zoom,
    TLFC_OT_apply_curve,
    TLFC_OT_set_interpolation,
    TLFC_OT_mirror_curve,
    TLFC_OT_reset_curve,
    TLFC_OT_read_curve,
    TLFC_OT_save_preset,
    TLFC_OT_open_preset_file,
    TLFC_PT_editor_header_dropdown,
    TLFC_PT_graph_editor_header_dropdown,
)
def _cleanup_previous():
    ns = bpy.app.driver_namespace
    old_handle = ns.get(ADDON_KEY)
    if old_handle is not None:
        try:
            bpy.types.SpaceDopeSheetEditor.draw_handler_remove(old_handle, 'WINDOW')
        except Exception:
            pass
        ns.pop(ADDON_KEY, None)
    old_info_handle = ns.get(INFO_ADDON_KEY)
    if old_info_handle is not None:
        try:
            bpy.types.SpaceInfo.draw_handler_remove(old_info_handle, 'WINDOW')
        except Exception:
            pass
        ns.pop(INFO_ADDON_KEY, None)
    old_graph_handle = ns.get(GRAPH_ADDON_KEY)
    if old_graph_handle is not None:
        try:
            bpy.types.SpaceGraphEditor.draw_handler_remove(old_graph_handle, 'WINDOW')
        except Exception:
            pass
        ns.pop(GRAPH_ADDON_KEY, None)
    if bpy.app.timers.is_registered(redraw_timer):
        try:
            bpy.app.timers.unregister(redraw_timer)
        except Exception:
            pass
    ns.pop(ENABLED_AREAS_KEY, None)


def _disable_runtime_handlers(clear_ui=True):
    ns = bpy.app.driver_namespace
    old_handle = ns.get(ADDON_KEY)
    if old_handle is not None:
        try:
            bpy.types.SpaceDopeSheetEditor.draw_handler_remove(old_handle, 'WINDOW')
        except Exception:
            pass
        ns.pop(ADDON_KEY, None)

    old_info_handle = ns.get(INFO_ADDON_KEY)
    if old_info_handle is not None:
        try:
            bpy.types.SpaceInfo.draw_handler_remove(old_info_handle, 'WINDOW')
        except Exception:
            pass
        ns.pop(INFO_ADDON_KEY, None)

    old_graph_handle = ns.get(GRAPH_ADDON_KEY)
    if old_graph_handle is not None:
        try:
            bpy.types.SpaceGraphEditor.draw_handler_remove(old_graph_handle, 'WINDOW')
        except Exception:
            pass
        ns.pop(GRAPH_ADDON_KEY, None)

    if bpy.app.timers.is_registered(redraw_timer):
        try:
            bpy.app.timers.unregister(redraw_timer)
        except Exception:
            pass

    if clear_ui:
        ns.pop(EDITOR_UI_KEY, None)


def _ensure_runtime_handlers():
    ns = bpy.app.driver_namespace
    if ns.get(ADDON_KEY) is None:
        handle = bpy.types.SpaceDopeSheetEditor.draw_handler_add(
            draw_editor_sidebar, (), 'WINDOW', 'POST_PIXEL'
        )
        ns[ADDON_KEY] = handle

    if ns.get(INFO_ADDON_KEY) is None:
        info_handle = bpy.types.SpaceInfo.draw_handler_add(
            draw_editor_sidebar, (), 'WINDOW', 'POST_PIXEL'
        )
        ns[INFO_ADDON_KEY] = info_handle

    if ns.get(GRAPH_ADDON_KEY) is None:
        graph_handle = bpy.types.SpaceGraphEditor.draw_handler_add(
            draw_editor_sidebar, (), 'WINDOW', 'POST_PIXEL'
        )
        ns[GRAPH_ADDON_KEY] = graph_handle

    if not bpy.app.timers.is_registered(redraw_timer):
        bpy.app.timers.register(redraw_timer, first_interval=0.1)


def register():
    import math
    bpy.app.driver_namespace["_tlfc_sin"] = math.sin
    bpy.app.driver_namespace["_tlfc_cos"] = math.cos
    bpy.app.translations.register(ADDON_MODULE_KEY, translations_dict)
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.WindowManager.tlfc_show_display_settings = bpy.props.BoolProperty(
        name="Show Display Settings",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_show_display_settings"],
    )
    bpy.types.WindowManager.tlfc_sidebar_width = bpy.props.FloatProperty(
        name="Sidebar Width",
        description="Sidebar width as percentage of timeline panel width",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_sidebar_width"],
        min=TLFC_PROPERTY_RANGES["tlfc_sidebar_width"][0],
        max=TLFC_PROPERTY_RANGES["tlfc_sidebar_width"][1],
        subtype='PERCENTAGE',
    )
    bpy.types.WindowManager.tlfc_outer_pad = bpy.props.IntProperty(
        name="Outer Padding",
        description="Padding in pixels between the timeline region edges and the overlay panel",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_outer_pad"],
        min=TLFC_PROPERTY_RANGES["tlfc_outer_pad"][0],
        max=TLFC_PROPERTY_RANGES["tlfc_outer_pad"][1],
    )
    bpy.types.WindowManager.tlfc_alpha = bpy.props.FloatProperty(
        name="Background Alpha",
        description="Opacity of the Bezier editor sidebar background",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_alpha"],
        min=TLFC_PROPERTY_RANGES["tlfc_alpha"][0],
        max=TLFC_PROPERTY_RANGES["tlfc_alpha"][1],
    )
    bpy.types.WindowManager.tlfc_samples = bpy.props.IntProperty(
        name="Curve Samples",
        description="Number of curve samples used for drawing the Bezier preview",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_samples"],
        min=TLFC_PROPERTY_RANGES["tlfc_samples"][0],
        max=TLFC_PROPERTY_RANGES["tlfc_samples"][1],
    )
    bpy.types.WindowManager.tlfc_show_info = bpy.props.BoolProperty(
        name="Toggle Info",
        description="Show selected curve diagnostics under the graph area",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_show_info"],
    )
    bpy.types.WindowManager.tlfc_display_size = bpy.props.FloatProperty(
        name="Display Size",
        description="Global size multiplier for overlay UI elements",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_display_size"],
        min=TLFC_PROPERTY_RANGES["tlfc_display_size"][0],
        max=TLFC_PROPERTY_RANGES["tlfc_display_size"][1],
    )
    bpy.types.WindowManager.tlfc_h1x = bpy.props.FloatProperty(
        name="H1 X",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_h1x"],
        min=TLFC_PROPERTY_RANGES["tlfc_h1x"][0],
    )
    bpy.types.WindowManager.tlfc_h1y = bpy.props.FloatProperty(
        name="H1 Y",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_h1y"],
    )
    bpy.types.WindowManager.tlfc_h2x = bpy.props.FloatProperty(
        name="H2 X",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_h2x"],
        max=TLFC_PROPERTY_RANGES["tlfc_h2x"][1],
    )
    bpy.types.WindowManager.tlfc_h2y = bpy.props.FloatProperty(
        name="H2 Y",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_h2y"],
    )
    bpy.types.WindowManager.tlfc_sidebar_mode = bpy.props.EnumProperty(
        name="Sidebar Mode",
        description="Active easing editor mode",
        items=[
            ('BEZIER',  'Bezier',  'Bezier curve editor'),
            ('ELASTIC', 'Elastic', 'Elastic ease-out editor'),
        ],
        default=TLFC_PROPERTY_DEFAULTS["tlfc_sidebar_mode"],
    )
    bpy.types.WindowManager.tlfc_graph_height_ratio = bpy.props.FloatProperty(
        name="Graph Height Ratio",
        description="Height of the editor graph as a ratio of available panel height",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_graph_height_ratio"],
        min=TLFC_PROPERTY_RANGES["tlfc_graph_height_ratio"][0],
        max=TLFC_PROPERTY_RANGES["tlfc_graph_height_ratio"][1],
        subtype='FACTOR',
        update=_sync_graph_height_to_prefs,
    )
    bpy.types.WindowManager.tlfc_hover_graph_sep = bpy.props.BoolProperty(
        name="Hover Graph Separator",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_hover_graph_sep"],
        options={'HIDDEN'},
    )
    bpy.types.WindowManager.tlfc_elastic_amplitude = bpy.props.FloatProperty(
        name="Elastic Amplitude",
        description="Overshoot factor",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_elastic_amplitude"],
        min=TLFC_PROPERTY_RANGES["tlfc_elastic_amplitude"][0],
        max=TLFC_PROPERTY_RANGES["tlfc_elastic_amplitude"][1],
    )
    bpy.types.WindowManager.tlfc_elastic_period = bpy.props.FloatProperty(
        name="Elastic Period",
        description="Oscillation period",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_elastic_period"],
        min=TLFC_PROPERTY_RANGES["tlfc_elastic_period"][0],
        max=TLFC_PROPERTY_RANGES["tlfc_elastic_period"][1],
    )
    bpy.types.WindowManager.tlfc_grid_subdiv = bpy.props.IntProperty(
        name="Grid Subdivisions",
        description="Grid density for the normalized editor view",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_grid_subdiv"],
        min=TLFC_PROPERTY_RANGES["tlfc_grid_subdiv"][0],
        max=TLFC_PROPERTY_RANGES["tlfc_grid_subdiv"][1],
    )
    bpy.types.WindowManager.tlfc_auto_apply = bpy.props.BoolProperty(
        name="Auto Apply",
        description="Automatically apply handle edits to selected keyframe segments while dragging",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_auto_apply"],
    )
    bpy.types.WindowManager.tlfc_snap_threshold = bpy.props.FloatProperty(
        name="Edge Snap Threshold",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_snap_threshold"],
        min=TLFC_PROPERTY_RANGES["tlfc_snap_threshold"][0],
        max=TLFC_PROPERTY_RANGES["tlfc_snap_threshold"][1],
        options={'HIDDEN'},
    )
    bpy.types.WindowManager.tlfc_view_zoom = bpy.props.FloatProperty(
        name="View Zoom",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_view_zoom"],
        min=TLFC_PROPERTY_RANGES["tlfc_view_zoom"][0],
        max=TLFC_PROPERTY_RANGES["tlfc_view_zoom"][1],
    )
    bpy.types.WindowManager.tlfc_view_pan_x = bpy.props.FloatProperty(
        name="View Pan X",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_view_pan_x"],
    )
    bpy.types.WindowManager.tlfc_view_pan_y = bpy.props.FloatProperty(
        name="View Pan Y",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_view_pan_y"],
    )
    bpy.types.WindowManager.tlfc_pan_start_x = bpy.props.FloatProperty(
        name="Pan Start X",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_pan_start_x"],
        options={'HIDDEN'},
    )
    bpy.types.WindowManager.tlfc_pan_start_y = bpy.props.FloatProperty(
        name="Pan Start Y",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_pan_start_y"],
        options={'HIDDEN'},
    )
    bpy.types.WindowManager.tlfc_pan_origin_x = bpy.props.FloatProperty(
        name="Pan Origin X",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_pan_origin_x"],
        options={'HIDDEN'},
    )
    bpy.types.WindowManager.tlfc_pan_origin_y = bpy.props.FloatProperty(
        name="Pan Origin Y",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_pan_origin_y"],
        options={'HIDDEN'},
    )
    bpy.types.WindowManager.tlfc_mouse_editing = bpy.props.BoolProperty(
        name="Mouse Edit",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_mouse_editing"],
        options={'HIDDEN'},
    )
    bpy.types.WindowManager.tlfc_hover_sidebar = bpy.props.BoolProperty(
        name="Hover Sidebar",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_hover_sidebar"],
        options={'HIDDEN'},
    )
    bpy.types.WindowManager.tlfc_hover_sidebar_edge = bpy.props.BoolProperty(
        name="Hover Sidebar Edge",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_hover_sidebar_edge"],
        options={'HIDDEN'},
    )
    bpy.types.WindowManager.tlfc_dragging_sidebar = bpy.props.BoolProperty(
        name="Dragging Sidebar",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_dragging_sidebar"],
        options={'HIDDEN'},
    )
    bpy.types.WindowManager.tlfc_hover_button = bpy.props.StringProperty(
        name="Hover Button",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_hover_button"],
        options={'HIDDEN'},
    )
    bpy.types.WindowManager.tlfc_pressed_button = bpy.props.StringProperty(
        name="Pressed Button",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_pressed_button"],
        options={'HIDDEN'},
    )
    bpy.types.WindowManager.tlfc_hover_handle = bpy.props.StringProperty(
        name="Hover Handle",
        default=TLFC_PROPERTY_DEFAULTS["tlfc_hover_handle"],
        options={'HIDDEN'},
    )
    bpy.types.DOPESHEET_HT_header.append(draw_tlfc_timeline_header)
    bpy.types.INFO_HT_header.append(draw_tlfc_info_header)
    bpy.types.GRAPH_HT_header.append(draw_tlfc_graph_header)

    # Restore persistent graph height ratio from addon preferences.
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)
    # Also initialize on register (first-time enable) via a deferred timer
    # since WindowManager instance isn't fully ready at register time.
    if not bpy.app.timers.is_registered(_init_persistent_props):
        bpy.app.timers.register(_init_persistent_props, first_interval=0.1)

def _init_persistent_props():
    """Deferred one-shot: copy persistent addon-pref values to the WM runtime properties."""
    try:
        prefs = _addon_prefs()
        if prefs is not None:
            wm = bpy.context.window_manager
            wm.tlfc_graph_height_ratio = prefs.tlfc_graph_height_ratio
    except Exception:
        pass
    return None  # None = don't repeat

@bpy.app.handlers.persistent
def _on_load_post(dummy):
    """Restore persistent properties after a blend file is loaded."""
    try:
        prefs = _addon_prefs()
        if prefs is not None:
            wm = bpy.context.window_manager
            wm.tlfc_graph_height_ratio = prefs.tlfc_graph_height_ratio
    except Exception:
        pass

def unregister():
    bpy.app.translations.unregister(ADDON_MODULE_KEY)
    _cleanup_previous()
    # Remove the persistent load handler.
    try:
        bpy.app.handlers.load_post.remove(_on_load_post)
    except (ValueError, Exception):
        pass
    # Remove the deferred init timer if it's still pending.
    if bpy.app.timers.is_registered(_init_persistent_props):
        try:
            bpy.app.timers.unregister(_init_persistent_props)
        except Exception:
            pass
    try:
        bpy.types.DOPESHEET_HT_header.remove(draw_tlfc_timeline_header)
    except Exception:
        pass
    try:
        bpy.types.INFO_HT_header.remove(draw_tlfc_info_header)
    except Exception:
        pass
    try:
        bpy.types.GRAPH_HT_header.remove(draw_tlfc_graph_header)
    except Exception:
        pass
    try:
        bpy.types.WindowManager.tlfc_mouse_editing = False
    except Exception:
        pass
    for prop in (
        "tlfc_show_display_settings",
        "tlfc_sidebar_width",
        "tlfc_outer_pad",
        "tlfc_alpha",
        "tlfc_samples",
        "tlfc_show_info",
        "tlfc_display_size",
        "tlfc_h1x",
        "tlfc_h1y",
        "tlfc_h2x",
        "tlfc_h2y",
        "tlfc_grid_subdiv",
        "tlfc_auto_apply",
        "tlfc_snap_threshold",
        "tlfc_view_zoom",
        "tlfc_view_pan_x",
        "tlfc_view_pan_y",
        "tlfc_pan_start_x",
        "tlfc_pan_start_y",
        "tlfc_pan_origin_x",
        "tlfc_pan_origin_y",
        "tlfc_mouse_editing",
        "tlfc_hover_sidebar",
        "tlfc_hover_sidebar_edge",
        "tlfc_dragging_sidebar",
        "tlfc_hover_button",
        "tlfc_pressed_button",
        "tlfc_hover_handle",
        "tlfc_sidebar_mode",
        "tlfc_graph_height_ratio",
        "tlfc_hover_graph_sep",
        "tlfc_elastic_amplitude",
        "tlfc_elastic_period",
    ):
        if hasattr(bpy.types.WindowManager, prop):
            delattr(bpy.types.WindowManager, prop)
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass
