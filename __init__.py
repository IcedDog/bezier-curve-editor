bl_info = {
    "name": "Bezier Curve Editor Overlay",
    "author": "IcedDog",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "Dope Sheet / Graph Editor > Sidebar",
    "description": "Bezier curve overlay editor for Timeline keyframe easing",
    "category": "Animation",
}

from .main import register, unregister

__all__ = ("register", "unregister")
