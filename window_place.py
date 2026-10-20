"""
窓まわりの共通の道具(レイを飛ばす・定数)。
"""
import bpy
from mathutils import Vector

MAX_WALL_THICKNESS = 0.35  # 住宅の壁厚の上限。これより遠い裏面は「反対側の壁」とみなし、壁厚には使わない[m]
MARGIN = 0.02             # カッターを壁の両側へ突き出す量[m]
CUTTER_TAG = "is_window_cutter"


def _ray_obj(obj, origin, direction, depsgraph):
    """1つのオブジェクト(ブーリアン適用後)にレイを飛ばす。(距離, 位置, 法線)をワールドで返す"""
    ev = obj.evaluated_get(depsgraph)
    mw = obj.matrix_world
    inv = mw.inverted()
    ok, loc, n, _idx = ev.ray_cast(inv @ origin, (inv.to_3x3() @ direction).normalized())
    if not ok:
        return None
    loc_w = mw @ loc
    n_w = (mw.to_3x3().inverted().transposed() @ n).normalized()
    return (loc_w - origin).length, loc_w, n_w


def register():
    pass


def unregister():
    pass
