ADDON_KEY = "TLFC_EDITOR_HANDLE"
INFO_ADDON_KEY = "TLFC_INFO_EDITOR_HANDLE"
EDITOR_UI_KEY = "TLFC_EDITOR_UI"
ENABLED_AREAS_KEY = "TLFC_ENABLED_AREAS"
PRESET_FILE = "curve_presets.txt"

HANDLE_HIT_RADIUS_PX = 20
SIDEBAR_EDGE_HIT_PX = 6

REDRAW_LOAD_EWMA_KEY = "TLFC_REDRAW_LOAD_EWMA"
REDRAW_LAST_TICK_KEY = "TLFC_REDRAW_LAST_TICK"
REDRAW_LAST_INTERVAL_KEY = "TLFC_REDRAW_LAST_INTERVAL"
MODAL_SESSION_KEY = "TLFC_MODAL_SESSION"
SWITCH_BLOCK_UNTIL_KEY = "TLFC_SWITCH_BLOCK_UNTIL"

TLFC_PROPERTY_DEFAULTS = {
	"tlfc_redraw_load_threshold": 1.65,
	"tlfc_show_timeline_header_button": True,
	"tlfc_show_info_header_button": True,
	"tlfc_show_display_settings": False,
	"tlfc_sidebar_width": 34.0,
	"tlfc_outer_pad": 0,
	"tlfc_alpha": 1.0,
	"tlfc_samples": 32,
	"tlfc_show_info": True,
	"tlfc_display_size": 1.5,
	"tlfc_h1x": 0.333,
	"tlfc_h1y": 0.0,
	"tlfc_h2x": 0.667,
	"tlfc_h2y": 1.0,
	"tlfc_sidebar_mode": "BEZIER",
	"tlfc_elastic_amplitude": 1.0,
	"tlfc_elastic_period": 0.3,
	"tlfc_grid_subdiv": 4,
	"tlfc_auto_apply": False,
	"tlfc_snap_threshold": 0.1,
	"tlfc_view_zoom": 1.0,
	"tlfc_view_pan_x": 0.0,
	"tlfc_view_pan_y": 0.0,
	"tlfc_pan_start_x": 0.0,
	"tlfc_pan_start_y": 0.0,
	"tlfc_pan_origin_x": 0.0,
	"tlfc_pan_origin_y": 0.0,
	"tlfc_mouse_editing": False,
	"tlfc_hover_sidebar": False,
	"tlfc_hover_sidebar_edge": False,
	"tlfc_dragging_sidebar": False,
	"tlfc_hover_button": "",
	"tlfc_pressed_button": "",
	"tlfc_hover_handle": "",
}

TLFC_PROPERTY_RANGES = {
	"tlfc_redraw_load_threshold": (1.05, 5.0),
	"tlfc_sidebar_width": (10.0, 100.0),
	"tlfc_outer_pad": (0, 60),
	"tlfc_alpha": (0.0, 1.0),
	"tlfc_samples": (24, 400),
	"tlfc_display_size": (0.75, 3.0),
	"tlfc_h1x": (0.0, None),
	"tlfc_h1y": (None, None),
	"tlfc_h2x": (None, 1.0),
	"tlfc_h2y": (None, None),
	"tlfc_elastic_amplitude": (0.0, 1.0),
	"tlfc_elastic_period": (0.05, 1.0),
	"tlfc_grid_subdiv": (1, 64),
	"tlfc_snap_threshold": (0.0, 1.0),
	"tlfc_view_zoom": (0.2, 6.0),
}

TLFC_UI_NUMBERS = {
	"sidebar_min_px": 160.0,
	"zoom_wheel_factor": 1.15,
	"zoom_button_factor": 1.2,
	"pan_limit_base": 1.5,
	"pan_zoom_floor": 0.1,
	"elastic_handle_x": 0.10,
	"elastic_handle_y": 0.25,
}

TLFC_COLORS = {
	"white": (1.0, 1.0, 1.0, 1.0),
	"text_default": (0.96, 0.97, 0.99, 1.0),
	"text_pressed": (0.93, 0.94, 0.96, 1.0),
	"preset_tile_bg": (0.18, 0.20, 0.24, 0.92),
	"preset_tile_border": (0.70, 0.74, 0.82, 0.95),
	"preset_preview_bg": (0.08, 0.09, 0.11, 0.8),
	"preset_preview_border": (0.42, 0.45, 0.52, 0.95),
	"curve_orange": (0.95, 0.62, 0.18, 1.0),
	"preset_title_text": (0.92, 0.94, 0.98, 1.0),
	"button_apply_base": (0.72, 0.36, 0.15, 0.95),
	"button_apply_border": (0.84, 0.77, 0.66, 0.98),
	"button_auto_on_base": (0.14, 0.48, 0.24, 0.95),
	"button_auto_on_border": (0.62, 0.88, 0.66, 0.98),
	"button_auto_off_base": (0.34, 0.20, 0.20, 0.92),
	"button_auto_off_border": (0.84, 0.62, 0.62, 0.95),
	"button_preset_base": (0.22, 0.26, 0.32, 0.90),
	"button_preset_border": (0.66, 0.71, 0.81, 0.95),
	"button_default_base": (0.24, 0.28, 0.36, 0.88),
	"button_default_border": (0.70, 0.74, 0.82, 0.95),
	"graph_bg": (0.03, 0.035, 0.045, 0.7),
	"tab_active_bg": (0.26, 0.30, 0.40, 0.95),
	"tab_inactive_bg": (0.15, 0.17, 0.22, 0.88),
	"tab_active_text": (1.0, 1.0, 1.0, 1.0),
	"tab_inactive_text": (0.72, 0.76, 0.84, 1.0),
	"tab_inactive_hover_text": (0.92, 0.94, 0.98, 1.0),
	"tab_active_outline": (0.50, 0.58, 0.82, 0.90),
	"grid_axis": (0.45, 0.50, 0.65, 1.0),
	"grid_boundary": (0.30, 0.33, 0.39, 0.9),
	"grid_regular": (0.24, 0.26, 0.31, 0.65),
	"elastic_curve": (0.42, 0.88, 0.56, 1.0),
	"endpoint_fill": (0.18, 0.20, 0.26, 1.0),
	"endpoint_outline": (0.75, 0.78, 0.85, 0.95),
	"elastic_amp_guide": (0.22, 0.84, 0.96, 0.18),
	"elastic_per_guide": (0.96, 0.48, 0.24, 0.18),
	"handle_amp_fill": (0.22, 0.84, 0.96, 1.0),
	"handle_per_fill": (0.96, 0.48, 0.24, 1.0),
	"handle_outline": (1.0, 1.0, 1.0, 1.0),
	"elastic_amp_label": (0.22, 0.84, 0.96, 0.9),
	"elastic_per_label": (0.96, 0.48, 0.24, 0.9),
	"handle_line": (0.64, 0.66, 0.72, 0.85),
	"bezier_h1_label": (0.22, 0.84, 0.96, 0.75),
	"bezier_h2_label": (0.96, 0.48, 0.24, 0.75),
	"panel_bg": (0.08, 0.09, 0.11, 1.0),
	"panel_border": (0.65, 0.68, 0.74, 1.0),
	"panel_border_hover": (0.90, 0.74, 0.42, 1.0),
	"info_text": (0.82, 0.86, 0.92, 1.0),
	"info_footer_text": (0.75, 0.80, 0.87, 1.0),
	"info_empty_text": (0.76, 0.80, 0.86, 1.0),
}
