import bpy
import importlib

from . import window_place
from . import window_template

importlib.reload(window_place)
importlib.reload(window_template)

# 連窓・段窓(有料版のみ)。window_composite.py が無ければ単体サッシだけで動く
try:
    _composite = importlib.reload(importlib.import_module(".window_composite", __name__))
except ImportError:
    _composite = None

HAS_COMPOSITE = _composite is not None


def register():
    window_place.register()
    window_template.register()
    if _composite is not None:
        _composite.register()


def unregister():
    if _composite is not None:
        _composite.unregister()
    window_template.unregister()
    window_place.unregister()
