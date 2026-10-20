"""
テンプレート窓を、壁をクリックして置く(内法の幅・高さ・壁厚に合わせて伸ばす)

テンプレートの約束(引違い窓のモデルで確かめた約束):
  - 原点 = 外壁仕上げ面・内法の下端・左右の中央。屋外側が +Y
  - 子オブジェクト: 伸ばす部材(窓枠など)、開口ボックス(is_window_cutter=True)、
    金物(カスタムプロパティ「高さ追従率」を持つもの。伸ばさず位置だけ動かす。
    「幅追従率」があれば、横方向はその割合で動かす)
  - 切断位置・内法・前提の壁厚は、テンプレートのカスタムプロパティで上書きできる
      dwg_cut_x(幅の切断線 ±, m) / dwg_cut_z(高さの切断線, m) / dwg_cut_y(奥行きの切断線, m)
      dwg_cut_z2(高さの2本目の切断線, m。あれば高さの増分を2本に半分ずつ振り分ける。上げ下げ窓用)
      dwg_cut_x2(幅の外側の切断線 ±, m。あれば幅の増分を4本の切断線に1/4ずつ振り分ける。
          4枚建て用: dwg_cut_x を内側の障子の中ほど、dwg_cut_x2 を外側の障子の中ほどに置く)
      dwg_inner_w / dwg_inner_h(モデルの内法, m) / dwg_wall_t0(モデルが前提にしている壁厚, m)
      dwg_lock_side("右" / "左"): モデルの錠(カムラッチ等)が室内から見てどちらにあるか。
          これがあるテンプレートだけ「錠: 右/左」を選べる。逆を選ぶと窓をまるごと左右反転して作る
  - オプション(シャッター・面格子): 子のカスタムプロパティ dwg_option = "シャッター" など。
      窓ごとに付ける/付けないを選べる。オプションの子で dwg_repeat_z(ピッチ, m)を持つもの
      (シャッターのスラット)は上から内法下端(dwg_repeat_to, 既定0)まで並べる。開閉で並べる長さが変わる
"""
import os
import bpy
from mathutils import Vector, Matrix
from bpy_extras import view3d_utils
from . import window_place as wp

DEFAULTS = {"cut_x": 0.145, "cut_z": 0.149, "cut_y": -0.130,
            "inner_w": 0.600, "inner_h": 0.300, "wall_t0": 0.160}
FOLLOW_KEY = "高さ追従率"
WIDTH_FOLLOW_KEY = "幅追従率"     # 金物を、幅の増分の何割だけ横に動かすか(-0.5〜0.5)
COLLECTION_NAME = "建具"
LOCK_KEY = "dwg_lock_side"
LOCK_ITEMS = [('R', "右", "室内から見て右に錠"), ('L', "左", "室内から見て左に錠")]
_SWAP_SIDE = {"左": "右", "右": "左"}
TATEGU_DIR_NAME = "TateguSet"           # テンプレートの.blendを置くフォルダ(アドオンと同じ場所)
MULLION_COLLECTION = "連窓・段窓_部材"    # 連窓・段窓の部材(外周の窓枠・方立・無目)が入ったコレクション名
HOLDER_NAME = "建具テンプレート(Link)"   # Linkしたテンプレートをまとめるローカルのコレクション(非表示・レンダー無効)
MAT_MAP_KEY = "dwg_mat_map"             # 置いた窓に記録する「元のマテリアル名 → 置き換えたマテリアル名」


# ---------- 建具セット(TateguSet フォルダの .blend)からの読み込み ----------

def tategu_folder():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), TATEGU_DIR_NAME)


def list_tategu_files():
    folder = tategu_folder()
    if not os.path.isdir(folder):
        return []
    return sorted(f for f in os.listdir(folder) if f.lower().endswith(".blend"))


_file_items_cache = []   # EnumPropertyの動的アイテムはPython側で参照を保持しないと文字化けする


def _file_items(self, context):
    global _file_items_cache
    files = list_tategu_files()
    _file_items_cache = [(f, f, "") for f in files] or [("NONE", "(ファイルが見つかりません)", "")]
    return _file_items_cache


def template_root(col):
    """コレクションの中のテンプレート窓の親オブジェクト(開口ボックスを子に持つメッシュ)を返す"""
    if col is None:
        return None
    for o in col.objects:
        if "dwg_template" in o or o.get("dwg_composite"):
            continue                               # 置いた窓はテンプレートではない
        if o.parent is None and o.type == 'MESH' and any(ch.get(wp.CUTTER_TAG) for ch in o.children):
            return o
    return None


def _tpl_col_poll(self, col):
    return template_root(col) is not None


def _lib_path(idb):
    return os.path.normcase(os.path.abspath(bpy.path.abspath(idb.library.filepath))) if idb.library else ""


def find_object(name, lib=""):
    """名前とライブラリのパスでオブジェクトを探す(ローカルとLinkで同名があっても取り違えない)"""
    for o in bpy.data.objects:
        if o.name == name and _lib_path(o) == lib:
            return o
    return bpy.data.objects.get(name)


def _find_layer_col(lc, col):
    if lc.collection == col:
        return lc
    for ch in lc.children:
        r = _find_layer_col(ch, col)
        if r is not None:
            return r
    return None


def template_holder(context):
    """Linkしたテンプレートを入れるローカルのコレクション。
    Linkしたコレクション自体は編集できない(非表示・レンダー無効を設定しても効かない)ので、
    ローカルの親コレクションを作り、そちらで非表示(目のアイコン)・レンダー無効・選択不可にする"""
    h = bpy.data.collections.get(HOLDER_NAME)
    if h is None or h.library is not None:
        h = bpy.data.collections.new(HOLDER_NAME)
    if h not in list(context.scene.collection.children):
        context.scene.collection.children.link(h)
    h.hide_render = True
    h.hide_select = True
    lc = _find_layer_col(context.view_layer.layer_collection, h)
    if lc is not None:
        lc.hide_viewport = True     # 目のアイコン(データは評価されるので、テンプレートの寸法は読める)
    return h


def _is_linked_template_col(col):
    return col.library is not None and (template_root(col) is not None or col.name == MULLION_COLLECTION)


def tidy_linked_templates(context):
    """シーン直下に置かれたLinkのテンプレート(以前の版で読み込んだもの)を、まとめ用コレクションに移す"""
    scol = context.scene.collection
    stray = [c for c in scol.children if _is_linked_template_col(c)]
    if not stray and bpy.data.collections.get(HOLDER_NAME) is None:
        return
    h = template_holder(context)
    for c in stray:
        if c not in list(h.children):
            h.children.link(c)
        scol.children.unlink(c)


def load_tategu_file(context, fname, only=None):
    """TateguSet の .blend から、テンプレート窓のコレクション(と方立無目)をLinkで読み込む。
    only: コレクション名の集合を渡すと、それだけを読み込む。読み込んだコレクション名のリストを返す"""
    path = os.path.join(tategu_folder(), fname)
    if not os.path.isfile(path):
        return []
    norm = os.path.normcase(os.path.abspath(path))
    if os.path.normcase(os.path.abspath(bpy.data.filepath or "-")) == norm:
        return []                                  # 開いているファイル自身は読み込めない
    already = set()
    for c in bpy.data.collections:
        if c.library and _lib_path(c) == norm:
            already.add(c.name)
    loaded = []
    with bpy.data.libraries.load(path, link=True) as (src, dst):
        skip = {COLLECTION_NAME, HOLDER_NAME}      # 置いた窓のコレクションなどは読み込まない
        names = [n for n in src.collections if n not in already and n.split(".")[0] not in skip]
        if only is not None:
            names = [n for n in names if n in only]
        dst.collections = names
    holder = None
    for col in dst.collections:
        if col is None:
            continue
        if template_root(col) is None and col.name != MULLION_COLLECTION:
            continue                               # 自動で作られる空の "Collection" などはシーンに繋がない
        if holder is None:
            holder = template_holder(context)      # テンプレートそのものは見せない・レンダーしない
        if col not in list(holder.children):
            holder.children.link(col)
        loaded.append(col.name)
    tidy_linked_templates(context)
    return loaded


class OBJECT_OT_load_tategu(bpy.types.Operator):
    """選んだ .blend から、まだ読み込んでいない建具テンプレートをLinkで読み込む"""
    bl_idname = "object.dwg_load_tategu"
    bl_label = "選択したファイルを読み込む"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.dwgt_file != "NONE"

    def execute(self, context):
        fname = context.scene.dwgt_file
        if os.path.normcase(os.path.abspath(bpy.data.filepath or "-")) == \
                os.path.normcase(os.path.abspath(os.path.join(tategu_folder(), fname))):
            self.report({'WARNING'}, "開いているファイル自身は読み込めません(テンプレートはそのまま使えます)")
            return {'CANCELLED'}
        loaded = load_tategu_file(context, fname)
        tidy_linked_templates(context)
        if not loaded:
            self.report({'INFO'}, "新しく読み込むテンプレートはありませんでした(読み込み済み)")
        else:
            self.report({'INFO'}, "読み込みました: " + " / ".join(loaded))
        return {'FINISHED'}


def tval(tpl, key):
    return float(tpl.get("dwg_" + key, DEFAULTS[key]))


def _cuts_z(tpl):
    """高さの切断線のリスト(下から順)。dwg_cut_z2 があれば2本"""
    zs = [tval(tpl, "cut_z")]
    if "dwg_cut_z2" in tpl:
        zs.append(float(tpl["dwg_cut_z2"]))
    return tuple(sorted(zs))


def _params(tpl, W, H, T):
    cx2 = float(tpl["dwg_cut_x2"]) if "dwg_cut_x2" in tpl else None
    c = (tval(tpl, "cut_x"), _cuts_z(tpl), tval(tpl, "cut_y"), cx2)
    d = (W - tval(tpl, "inner_w"), H - tval(tpl, "inner_h"), -(T - tval(tpl, "wall_t0")))
    return d, c


def _shift_x(px, dx, cx, cx2=None):
    """幅方向の移動量。cx2 があれば ±cx で 1/4、±cx2 でさらに 1/4 (4本の切断線で均等に伸ばす)"""
    a = abs(px)
    if cx2 is not None and cx2 > cx:
        s = 0.0 if a <= cx else (dx / 4 if a <= cx2 else dx / 2)
    else:
        s = 0.0 if a <= cx else dx / 2
    return -s if px < 0 else s


def _shift(p, d, c):
    dx, dz, dy = d
    cx, cz, cy = c[0], c[1], c[2]
    cx2 = c[3] if len(c) > 3 else None
    x = _shift_x(p.x, dx, cx, cx2)
    z = dz * sum(1 for k in cz if p.z > k) / len(cz)   # 切断線が2本なら、間は半分だけ上がる
    y = dy if p.y < cy else 0.0
    return Vector((x, y, z))


def _to_tpl(src, tpl):
    """src の座標をテンプレートの原点の座標系に直す行列。
    リンクしたばかりのテンプレート(非表示のコレクション)は matrix_world がまだ計算されていないことがあるので、
    親子関係(親の逆行列 × 位置・回転・拡大)をたどって自分で計算する"""
    m = Matrix.Identity(4)
    o = src
    while o is not None and o is not tpl:
        m = o.matrix_parent_inverse @ o.matrix_basis @ m
        o = o.parent
    if o is None:                                    # tpl の子孫でなければ、ワールド同士で
        return world_of(tpl).inverted() @ world_of(src)
    return m


def world_of(src):
    """matrix_world を使わずに、親子関係から計算したワールド行列(リンクしたばかりのデータ用)"""
    m = Matrix.Identity(4)
    o = src
    while o is not None:
        m = o.matrix_parent_inverse @ o.matrix_basis @ m if o.parent else o.matrix_basis @ m
        o = o.parent
    return m


def _stretched_mesh(src, tpl, d, c, name):
    """src のメッシュを、テンプレートの原点の座標系に直してから伸ばした、新しいメッシュ"""
    to_root = _to_tpl(src, tpl)
    me = src.data.copy()
    me.name = name
    for v in me.vertices:
        p = to_root @ v.co
        v.co = p + _shift(p, d, c)
    me.update()
    return me


def _hardware_local(src, tpl, d, c):
    """金物: 伸ばさず、幅は 増分 × 幅追従率(なければ切断線の帯)、高さは 増分 × 高さ追従率 だけ動かした行列"""
    to_root = _to_tpl(src, tpl)
    pts = [to_root @ Vector(b) for b in src.bound_box]
    cen = sum(pts, Vector()) / 8
    if WIDTH_FOLLOW_KEY in src:
        sx = d[0] * float(src[WIDTH_FOLLOW_KEY])          # 幅追従率があればそれに従う
    else:
        sx = _shift(cen, (d[0], 0.0, 0.0), c).x            # なければ切断線のどちら側にあるかで決める
    follow = float(src.get(FOLLOW_KEY, 0.5))
    return Matrix.Translation((sx, 0.0, d[1] * follow)) @ to_root


def has_lock(tpl):
    """錠の左右を選べるテンプレートか"""
    return tpl is not None and LOCK_KEY in tpl


def lock_mirror(tpl, lock):
    """lock('R'/'L')にするために左右反転が要るか"""
    if not has_lock(tpl) or lock not in ('R', 'L'):
        return False
    return {'R': "右", 'L': "左"}[lock] != str(tpl[LOCK_KEY])


def _mirror_mesh(me):
    """メッシュを X=0 で左右反転する(面の向きも直す)"""
    for v in me.vertices:
        v.co.x = -v.co.x
    me.flip_normals()
    me.update()


_MIRROR = Matrix.Scale(-1.0, 4, (1.0, 0.0, 0.0))


def _collection(context):
    """置いた窓を入れる、このファイルの「建具」コレクション(Linkで同じ名前のものがあっても、ローカルの方だけを使う)"""
    col = next((c for c in bpy.data.collections if c.name == COLLECTION_NAME and c.library is None), None)
    if col is None:
        col = bpy.data.collections.new(COLLECTION_NAME)
    scol = context.scene.collection
    if col not in list(scol.children_recursive):
        scol.children.link(col)
    return col


# ---------- オプション(シャッター・面格子など) ----------
# テンプレートの子で、カスタムプロパティ dwg_option = "シャッター" などを持つものはオプション。
# 窓ごとに付ける/付けないを選べる(親の窓に dwg_opt_<名前> = True/False で記録)。
# オプションの子で dwg_repeat_z (ピッチ, m) を持つもの(シャッターのスラットなど)は、
# 上から下へ並べる。並べる下端は dwg_repeat_to(内法下端からの高さ, m。既定 0)。
# どこまで並べるかは窓の dwg_shutter_open(0=閉 / 1=全開)で決まる。
# オプションの子で dwg_shutter_bottom を持つもの(シャッターの座板)は、モデルの位置 = 「開」の位置として、
# 開き具合に合わせて内法下端(dwg_repeat_to)まで下ろす。座板があるとき、スラットは座板の上端まで並べる。
OPTION_KEY = "dwg_option"
REPEAT_KEY = "dwg_repeat_z"
REPEAT_TO_KEY = "dwg_repeat_to"
BOTTOM_BAR_KEY = "dwg_shutter_bottom"
OPEN_KEY = "dwg_shutter_open"
SHUTTER_STATES = [('CLOSE', "閉", "シャッターを閉めた状態"),
                  ('HALF', "半開", "途中まで下ろした状態"),
                  ('OPEN', "開", "シャッターを開けた状態(スラットはボックスの中)")]


def template_options(tpl):
    """テンプレートが持っているオプションの名前(出てきた順)"""
    out = []
    if tpl is None:
        return out
    for ch in tpl.children:
        n = ch.get(OPTION_KEY)
        if n and n not in out:
            out.append(str(n))
    return out


def opt_prop(name):
    return "dwg_opt_" + name


def _clamp01(x):
    return max(0.0, min(1.0, float(x)))


def _bottom_bar_mesh(src, tpl, d, c, name, open_ratio):
    """座板: 伸ばしてから、開き具合に合わせて下ろしたメッシュ。(メッシュ, 下ろしたあとの上端) を返す"""
    me = _stretched_mesh(src, tpl, d, c, name)
    if not me.vertices:
        return me, None
    zs = [v.co.z for v in me.vertices]
    travel = min(zs) - float(src.get(REPEAT_TO_KEY, 0.0))     # 開の位置から内法下端まで
    dz = -max(0.0, travel) * (1.0 - _clamp01(open_ratio))
    for v in me.vertices:
        v.co.z += dz
    me.update()
    return me, max(zs) + dz


def _repeat_mesh(src, tpl, d, c, name, open_ratio, bottom=None):
    """src(スラット1枚)を伸ばしてから、上から下へ並べたメッシュ。並べるものが無ければ None
    bottom: 並べる下端(座板の上端など)。None なら 内法下端(dwg_repeat_to)× 開き具合"""
    import math
    import bmesh
    me0 = _stretched_mesh(src, tpl, d, c, name)
    if not me0.vertices:
        return me0
    zs = [v.co.z for v in me0.vertices]
    top, h = max(zs), max(zs) - min(zs)
    pitch = float(src.get(REPEAT_KEY, 0.0)) or h
    if pitch > 1.0:                                  # 1より大きければ mm で入れたとみなす(50 → 0.05m)
        pitch /= 1000.0
    if bottom is None:
        b0 = float(src.get(REPEAT_TO_KEY, 0.0))
        span = (top - b0) * (1.0 - _clamp01(open_ratio))
    else:
        span = top - bottom
    if pitch <= 0 or span < h * 0.5:
        bpy.data.meshes.remove(me0)
        return None
    # 一番下のスラットの下端が、ちょうど下端に来るように(ピッチは少しだけ詰める)
    n = int(math.ceil(max(0.0, span - h) / pitch - 1e-6)) + 1
    pe = (span - h) / (n - 1) if n > 1 else pitch
    bm = bmesh.new()
    bm.from_mesh(me0)
    geom = list(bm.verts) + list(bm.edges) + list(bm.faces)
    for k in range(1, n):
        ret = bmesh.ops.duplicate(bm, geom=geom)
        vs = [g for g in ret["geom"] if isinstance(g, bmesh.types.BMVert)]
        bmesh.ops.translate(bm, verts=vs, vec=(0.0, 0.0, -k * pe))
    bm.to_mesh(me0)
    bm.free()
    me0.update()
    return me0


# ---- 面格子: 格子を並べる ----
# 面格子の枠(オプションの子)には dwg_option_type = "縦" / "横" / "菱"。窓の dwg_grille_type と同じものだけ付ける。
# 枠の子の格子には、並べ方の目印を付ける:
#   dwg_repeat_x  (ピッチ) 縦格子: 内法の幅に、ほぼこのピッチで均等に並べる(左右の端には置かない)
#   dwg_repeat_up (ピッチ) 横格子: モデルの位置(一番下)から内法上端の手前まで、ほぼこのピッチで均等に並べる
#   dwg_repeat_tile        菱クロス: 菱形1つ分を伸ばさずに縦横に敷き詰めて、枠の内側で切り取る
GRILLE_TYPE_KEY = "dwg_option_type"
GRILLE_ROOT_KEY = "dwg_grille_type"
GRILLE_OPT = "面格子"
GRILLE_TYPES = [('V', "縦", "縦格子"), ('H', "横", "横格子"), ('X', "菱", "菱クロス格子")]
GRILLE_NAME = {k: n for k, n, _d in GRILLE_TYPES}


def _pitch(src, key):
    p = float(src.get(key, 0.0) or 0.0)
    return p / 1000.0 if p > 1.0 else p              # 1より大きければ mm とみなす


def _copies(me, offsets):
    """me を offsets の各位置にコピーして1つのメッシュにする(最初の offset は元の分)"""
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(me)
    geom = list(bm.verts) + list(bm.edges) + list(bm.faces)
    orig = list(bm.verts)
    for off in offsets[1:]:
        ret = bmesh.ops.duplicate(bm, geom=geom)
        vs = [g for g in ret["geom"] if isinstance(g, bmesh.types.BMVert)]
        bmesh.ops.translate(bm, verts=vs, vec=off)
    bmesh.ops.translate(bm, verts=orig, vec=offsets[0])
    bm.to_mesh(me)
    bm.free()
    me.update()
    return me


def _bbox(me):
    xs = [v.co.x for v in me.vertices]
    zs = [v.co.z for v in me.vertices]
    return min(xs), max(xs), min(zs), max(zs)


def _spread_x(me, W, pitch):
    x0 = sum(_bbox(me)[:2]) / 2
    n = max(2, int(round(W / pitch)))
    pos = [-W / 2 + i * W / n for i in range(1, n)]
    return _copies(me, [(p - x0, 0.0, 0.0) for p in pos])


def _spread_up(me, H, pitch):
    b = _bbox(me)
    z0 = (b[2] + b[3]) / 2
    span = H - z0
    n = max(1, int(round(span / pitch)))
    return _copies(me, [(0.0, 0.0, i * span / n) for i in range(n)])


def _tile(me, W, H):
    """菱形1つ分を縦横に敷き詰めて、内法(+はみ出し分)で切り取る"""
    import math
    import bmesh
    x0, x1, z0, z1 = _bbox(me)
    pw, ph = x1 - x0, z1 - z0
    if pw <= 0 or ph <= 0:
        return me
    m = max(0.0, -z0)                                # 下端からのはみ出し(枠に食い込む分)を上と左右にも取る
    L = W / 2 + m
    K = int(math.ceil(L / pw)) + 1
    J = int(math.ceil((H + m - z0) / ph)) + 1
    offs = [(0.0, 0.0, 0.0)] + [(k * pw, 0.0, j * ph) for j in range(J) for k in range(-K, K + 1)
                                 if not (j == 0 and k == 0)]
    _copies(me, offs)
    bm = bmesh.new()
    bm.from_mesh(me)
    for co, no in (((-L, 0, 0), (-1, 0, 0)), ((L, 0, 0), (1, 0, 0)),
                   ((0, 0, z0), (0, 0, -1)), ((0, 0, H + m), (0, 0, 1))):
        geom = list(bm.verts) + list(bm.edges) + list(bm.faces)
        bmesh.ops.bisect_plane(bm, geom=geom, plane_co=co, plane_no=no, clear_outer=True)
    bm.to_mesh(me)
    bm.free()
    me.update()
    return me


def _grille_mesh(src, tpl, d, c, name):
    """面格子の格子(並べ方の目印つき)のメッシュ。目印が無ければ None"""
    W = tval(tpl, "inner_w") + d[0]
    H = tval(tpl, "inner_h") + d[1]
    if "dwg_repeat_tile" in src:
        me = _stretched_mesh(src, tpl, (0.0, 0.0, d[2]), c, name)   # 菱形は伸ばさない
        return _tile(me, W, H) if me.vertices else me
    if "dwg_repeat_x" in src:
        me = _stretched_mesh(src, tpl, (0.0, d[1], d[2]), c, name)   # 格子1本は太らせない(高さだけ伸ばす)
        p = _pitch(src, "dwg_repeat_x")
        return _spread_x(me, W, p) if me.vertices and p > 0 else me
    if "dwg_repeat_up" in src:
        me = _stretched_mesh(src, tpl, (d[0], 0.0, d[2]), c, name)   # 幅だけ伸ばす
        p = _pitch(src, "dwg_repeat_up")
        return _spread_up(me, H, p) if me.vertices and p > 0 else me
    return None


def _remove_tree(ob):
    for o in reversed(_descendants(ob)):
        me = o.data if o.type == 'MESH' else None
        bpy.data.objects.remove(o, do_unlink=True)
        if me is not None and me.users == 0:
            bpy.data.meshes.remove(me)


def _new_part(src, me, col, parent):
    ob = bpy.data.objects.new(src.name, me)
    ob.display_type = src.display_type
    ob.hide_render = src.hide_render
    for k in src.keys():
        if not k.startswith("_"):
            ob[k] = src[k]
    ob["dwg_src"] = src.name
    col.objects.link(ob)
    ob.parent = parent
    ob.matrix_parent_inverse = Matrix.Identity(4)
    ob.matrix_basis = Matrix.Identity(4)
    return ob


def _add_options(root, tpl, d, c, mirror, col):
    """root の dwg_opt_* の設定どおりに、オプションの部材を作って付ける"""
    open_ratio = float(root.get(OPEN_KEY, 0.0))
    for ch in tpl.children:
        name = ch.get(OPTION_KEY)
        if not name or ch.type != 'MESH' or not root.get(opt_prop(str(name)), False):
            continue
        gtype = ch.get(GRILLE_TYPE_KEY)
        if gtype and str(gtype) != str(root.get(GRILLE_ROOT_KEY, "縦")):
            continue                                 # 選んでいない種類の面格子
        me = _stretched_mesh(ch, tpl, d, c, ch.name)
        if mirror:
            _mirror_mesh(me)
        ob = _new_part(ch, me, col, root)
        kids = [gc for gc in ch.children if gc.type == 'MESH']
        bar_top = None
        built = {}
        for gc in kids:                              # 先に座板を下ろして、スラットを並べる下端を決める
            if BOTTOM_BAR_KEY in gc:
                gme, top = _bottom_bar_mesh(gc, tpl, d, c, gc.name, open_ratio)
                built[gc.name] = gme
                if top is not None:
                    bar_top = top if bar_top is None else max(bar_top, top)
        for gc in kids:
            if gc.name in built:
                gme = built[gc.name]
            elif REPEAT_KEY in gc:
                gme = _repeat_mesh(gc, tpl, d, c, gc.name, open_ratio, bottom=bar_top)
                if gme is None:
                    continue
            elif any(k in gc for k in ("dwg_repeat_x", "dwg_repeat_up", "dwg_repeat_tile")):
                gme = _grille_mesh(gc, tpl, d, c, gc.name)
            else:
                gme = _stretched_mesh(gc, tpl, d, c, gc.name)
            if mirror:
                _mirror_mesh(gme)
            g = _new_part(gc, gme, col, ob)
            for md in gc.modifiers:                  # 「Smooth by Angle」などのジオメトリノードはそのまま付ける
                if md.type == 'NODES' and md.node_group is not None:
                    nm = g.modifiers.new(md.name, 'NODES')
                    nm.node_group = md.node_group


def _set_options(root, tpl, options, shutter_open):
    """窓に、オプションの付ける/付けない と シャッターの開き具合を記録する(None の項目はそのまま)"""
    for name in template_options(tpl):
        if options is not None and name in options:
            root[opt_prop(name)] = bool(options[name])
        elif opt_prop(name) not in root:
            root[opt_prop(name)] = False
    if root.get(opt_prop("シャッター")) and root.get(opt_prop(GRILLE_OPT)):
        root[opt_prop(GRILLE_OPT)] = False         # 両方は付けない(シャッターを優先)
    if shutter_open is not None:
        root[OPEN_KEY] = float(shutter_open)
    elif OPEN_KEY not in root:
        root[OPEN_KEY] = 0.0
    if options is not None and options.get("面格子の種類"):
        root[GRILLE_ROOT_KEY] = str(options["面格子の種類"])
    elif GRILLE_ROOT_KEY not in root:
        root[GRILLE_ROOT_KEY] = "縦"


def shutter_open_value(state, ratio):
    return {'CLOSE': 0.0, 'OPEN': 1.0}.get(state, float(ratio))


def build_window(context, tpl, place_mw, W, H, T, skip_sides=None, mirror=False,
                 options=None, shutter_open=None):
    """テンプレートを複製・伸長して置く。(窓の親オブジェクト, 開口ボックス) を返す

    skip_sides: {"上","下","左","右"} の部分集合。連窓・段窓などで隣のセルに接する側の
                枠部材(カスタムプロパティ dwg_side を持つもの)を省きたいときに指定する。
    mirror: True なら窓をまるごと左右反転する(錠の左右を変える)。
    options: {"シャッター": True, ...} 付けるオプション(省略時はどれも付けない)
    shutter_open: シャッターの開き具合(0=閉 / 1=全開)
    """
    d, c = _params(tpl, W, H, T)
    col = _collection(context)
    rme = _stretched_mesh(tpl, tpl, d, c, tpl.name)
    if mirror:
        _mirror_mesh(rme)
    root = bpy.data.objects.new(tpl.name, rme)
    col.objects.link(root)
    root.matrix_world = place_mw
    root["dwg_template"] = tpl.name
    root["dwg_template_lib"] = _lib_path(tpl)
    root["dwg_W"], root["dwg_H"], root["dwg_T"] = W, H, T
    root["dwg_mirror"] = bool(mirror)
    cutter = None
    for ch in tpl.children:
        if ch.type != 'MESH' or ch.get(OPTION_KEY):
            continue
        side = ch.get("dwg_side")
        if mirror and side in _SWAP_SIDE:
            side = _SWAP_SIDE[side]
        if skip_sides and side in skip_sides:
            continue
        if FOLLOW_KEY in ch:
            ob = ch.copy()
            ob.data = ch.data.copy()
            local = _hardware_local(ch, tpl, d, c)
            if mirror:
                _mirror_mesh(ob.data)
                local = _MIRROR @ local @ _MIRROR
        else:
            me = _stretched_mesh(ch, tpl, d, c, ch.name)
            if mirror:
                _mirror_mesh(me)
            ob = bpy.data.objects.new(ch.name, me)
            ob.display_type = ch.display_type
            ob.hide_render = ch.hide_render
            for k in ch.keys():
                if not k.startswith("_"):
                    ob[k] = ch[k]
            if side:
                ob["dwg_side"] = side
            local = Matrix.Identity(4)
        ob["dwg_src"] = ch.name
        col.objects.link(ob)
        ob.parent = root
        ob.matrix_parent_inverse = Matrix.Identity(4)
        ob.matrix_basis = local
        if ob.get(wp.CUTTER_TAG):
            cutter = ob
    _set_options(root, tpl, options or {}, shutter_open)
    _add_options(root, tpl, d, c, mirror, col)
    localize_materials(root)
    return root, cutter


def update_window(root, W, H, T, lock=None, options=None, shutter_open=None):
    """置いた窓を、テンプレートから作り直す(オブジェクトはそのまま、メッシュと金物の位置だけ更新)
    lock: 'R'/'L' を渡すと錠の左右もそれに合わせる(None なら置いたときのまま)
    options / shutter_open: オプションの付ける/付けないと開き具合(None なら今のまま)"""
    tpl = find_object(root.get("dwg_template", ""), root.get("dwg_template_lib", ""))
    if tpl is None:
        raise ValueError("テンプレート「%s」が見つかりません" % root.get("dwg_template", ""))
    mirror = lock_mirror(tpl, lock) if lock is not None else bool(root.get("dwg_mirror", False))
    d, c = _params(tpl, W, H, T)
    old = root.data
    root.data = _stretched_mesh(tpl, tpl, d, c, old.name)
    if mirror:
        _mirror_mesh(root.data)
    if old.users == 0:
        bpy.data.meshes.remove(old)
    for ob in list(root.children):
        if ob.get(OPTION_KEY):
            _remove_tree(ob)                         # オプションは下でまとめて作り直す
            continue
        src = find_object(ob.get("dwg_src", ""), _lib_path(tpl))
        if src is None:
            continue
        if FOLLOW_KEY in src:
            old = ob.data
            ob.data = src.data.copy()
            local = _hardware_local(src, tpl, d, c)
            if mirror:
                _mirror_mesh(ob.data)
                local = _MIRROR @ local @ _MIRROR
            ob.matrix_basis = local
            if old.users == 0:
                bpy.data.meshes.remove(old)
        else:
            old = ob.data
            ob.data = _stretched_mesh(src, tpl, d, c, old.name)
            if mirror:
                _mirror_mesh(ob.data)
            side = src.get("dwg_side")
            if side:
                ob["dwg_side"] = _SWAP_SIDE.get(side, side) if mirror else side
            if old.users == 0:
                bpy.data.meshes.remove(old)
    root["dwg_W"], root["dwg_H"], root["dwg_T"] = W, H, T
    root["dwg_mirror"] = bool(mirror)
    _set_options(root, tpl, options, shutter_open)
    col = root.users_collection[0] if root.users_collection else bpy.context.scene.collection
    _add_options(root, tpl, d, c, mirror, col)
    localize_materials(root)
    apply_mat_map(top_root(root))


# ---------- マテリアル ----------
# 置いた窓のメッシュはテンプレート(Link)の複製なので、マテリアルもLinkのまま(編集できない)。
#  1) ローカル化: Linkのマテリアル1つにつき、このファイルにローカルのコピーを1つだけ作って全部の窓で使い回す。
#     → アルミの色などを一度変えれば、ファイル内の窓が全部変わる。形状はLinkのまま。
#  2) 置き換え(例外): 選択中の窓だけ別のマテリアルにする。結果は一番上の親(単体の窓 or 連窓・段窓)に
#     dwg_mat_map として記録し、「更新」でメッシュを作り直したあとにも掛け直す。
SRC_MAT_KEY = "dwg_src_mat"      # ローカルのコピーに付ける「元のLinkのマテリアル」の印(ライブラリのパス|名前)


def _mat_src_key(m):
    # ライブラリはファイル名だけで見る(別のPCでアドオンの場所が違っても、同じコピーを使い回せるように)
    return os.path.basename(_lib_path(m)) + "|" + m.name


def local_copy_of(m):
    """Linkのマテリアル m の、このファイルでの共通のローカルコピー(無ければ作る)"""
    key = _mat_src_key(m)
    for lm in bpy.data.materials:
        if lm.library is None and lm.get(SRC_MAT_KEY) == key:
            return lm
    lm = m.copy()
    lm.name = m.name                 # Linkと同じ名前(ローカル側で重なれば .001 が付く)
    lm[SRC_MAT_KEY] = key
    return lm


def localize_materials(root):
    """root 以下の部材に付いたLinkのマテリアルを、共通のローカルコピーに付け替える"""
    if root is None:
        return
    for o in _descendants(root):
        if o.type != 'MESH' or o.get(wp.CUTTER_TAG):
            continue
        for sl in o.material_slots:
            m = sl.material
            if m is not None and m.library is not None:
                sl.material = local_copy_of(m)


def localize_all_windows():
    """ファイル内の置いた窓すべてを、ローカルのマテリアルにする(以前の版で置いた窓用)"""
    for o in bpy.data.objects:
        if o.library is None and o.parent is None and _is_root(o):
            localize_materials(o)
            apply_mat_map(o)

def _is_root(o):
    return o is not None and ("dwg_template" in o or bool(o.get("dwg_composite")))


def top_root(o):
    """o(窓の部材でもよい)の、一番上の窓の親(連窓・段窓のセルならその連窓・段窓)"""
    found = None
    while o is not None:
        if _is_root(o):
            found = o
        o = o.parent
    return found


def selected_roots(context):
    out = []
    for o in context.selected_objects:
        r = top_root(o)
        if r is not None and r not in out:
            out.append(r)
    return out


def _descendants(root):
    stack, out = [root], []
    while stack:
        o = stack.pop()
        out.append(o)
        stack.extend(o.children)
    return out


def window_materials(roots):
    """選択中の窓に使われているマテリアル(開口ボックスは除く)を、出てきた順に"""
    mats = []
    for r in roots:
        for o in _descendants(r):
            if o.type != 'MESH' or o.get(wp.CUTTER_TAG):
                continue
            for sl in o.material_slots:
                if sl.material is not None and sl.material not in mats:
                    mats.append(sl.material)
    return mats


def mat_ident(m):
    return ("L:" if m.library else "") + m.name


def find_material(ident):
    linked = ident.startswith("L:")
    name = ident[2:] if linked else ident
    for m in bpy.data.materials:
        if m.name == name and (m.library is not None) == linked:
            return m
    return None


def _local_material(name):
    for m in bpy.data.materials:
        if m.name == name and m.library is None:
            return m
    return bpy.data.materials.get(name)


def _get_map(root):
    mp = root.get(MAT_MAP_KEY)
    return dict(mp.to_dict()) if hasattr(mp, "to_dict") else {}


def replace_material(root, old, new):
    """root 以下の old を new に置き換えて、記録する(元のマテリアル名 → 新しい名前)"""
    mp = _get_map(root)
    chained = False
    for k, v in list(mp.items()):
        if v == old.name:                 # 一度置き換えたものをさらに置き換えた
            mp[k] = new.name
            chained = True
    if not chained:
        mp[old.name] = new.name
    mp = {k: v for k, v in mp.items() if k != v}
    root[MAT_MAP_KEY] = mp
    n = 0
    for o in _descendants(root):
        if o.type != 'MESH' or o.get(wp.CUTTER_TAG):
            continue
        for sl in o.material_slots:
            if sl.material == old:
                sl.material = new
                n += 1
    return n


def apply_mat_map(root):
    """記録してある置き換えを、root 以下に掛け直す(メッシュを作り直したあとに呼ぶ)"""
    if root is None:
        return
    mp = _get_map(root)
    if not mp:
        return
    for o in _descendants(root):
        if o.type != 'MESH' or o.get(wp.CUTTER_TAG):
            continue
        for sl in o.material_slots:
            m = sl.material
            if m is None:
                continue
            key = m.name if m.name in mp else None
            if key is None and m.get(SRC_MAT_KEY):         # 以前の版の記録(Linkの名前)にも合わせる
                src_name = str(m[SRC_MAT_KEY]).split("|", 1)[-1]
                key = src_name if src_name in mp else None
            if key is not None:
                new = _local_material(mp[key])
                if new is not None and new != m:
                    sl.material = new


_mat_items_cache = []


def _mat_items(self, context):
    global _mat_items_cache
    mats = window_materials(selected_roots(context)) if context else []
    _mat_items_cache = [(mat_ident(m), m.name + ("(リンク)" if m.library else ""), "") for m in mats] \
        or [("NONE", "(置いた窓を選択してください)", "")]
    return _mat_items_cache


def _focus_material(context, roots, mat):
    """マテリアルのタブで、すぐ mat を編集できるようにする(それを使う部材をアクティブに)"""
    for r in roots:
        for o in _descendants(r):
            for i, sl in enumerate(o.material_slots):
                if sl.material == mat:
                    try:
                        context.view_layer.objects.active = o
                        o.active_material_index = i
                    except RuntimeError:
                        pass
                    return


class OBJECT_OT_dwg_mat_replace(bpy.types.Operator):
    """選択中の窓の「元のマテリアル」を「置き換え先」に置き換える(更新しても保たれる)"""
    bl_idname = "object.dwg_mat_replace"
    bl_label = "置き換え"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.dwgt_mat_to is not None

    def execute(self, context):
        s = context.scene
        old, new = find_material(s.dwgt_mat_from), s.dwgt_mat_to
        if old is None:
            self.report({'WARNING'}, "置き換えるマテリアルを選んでください")
            return {'CANCELLED'}
        if old == new:
            return {'CANCELLED'}
        roots = selected_roots(context)
        n = sum(replace_material(r, old, new) for r in roots)
        try:
            s.dwgt_mat_from = mat_ident(new)
        except TypeError:
            pass
        self.report({'INFO'}, "%s → %s(%d箇所)" % (old.name, new.name, n))
        return {'FINISHED'}


class OBJECT_OT_dwg_mat_make_local(bpy.types.Operator):
    """選んだマテリアルをコピーして、選択中の窓だけに割り当てる(この窓だけ色を変えたいとき)。
    コピーはマテリアルのタブでそのまま編集できる"""
    bl_idname = "object.dwg_mat_make_local"
    bl_label = "コピーして編集"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene
        old = find_material(s.dwgt_mat_from)
        if old is None:
            self.report({'WARNING'}, "マテリアルを選んでください")
            return {'CANCELLED'}
        new = old.copy()
        if SRC_MAT_KEY in new:
            del new[SRC_MAT_KEY]         # 共通のコピーと間違えないように印は外す
        base = old.name.split("_カスタム")[0]
        new.name = base + "_カスタム"
        roots = selected_roots(context)
        for r in roots:
            replace_material(r, old, new)
        s.dwgt_mat_to = new
        try:
            s.dwgt_mat_from = mat_ident(new)
        except TypeError:
            pass
        _focus_material(context, roots, new)
        self.report({'INFO'}, "「%s」を作りました。マテリアルのタブで編集できます" % new.name)
        return {'FINISHED'}


class VIEW3D_PT_dwg_material(bpy.types.Panel):
    """選択中の窓のマテリアルを置き換える(単体・連窓・段窓共通)"""
    bl_label = "マテリアル(選択中の窓)"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "建具"
    bl_order = 3

    @classmethod
    def poll(cls, context):
        return bool(selected_roots(context))

    def draw(self, context):
        s = context.scene
        col = self.layout.column()
        col.prop(s, "dwgt_mat_from", text="元")
        col.prop(s, "dwgt_mat_to", text="置換先")
        row = col.row(align=True)
        row.operator(OBJECT_OT_dwg_mat_replace.bl_idname, icon='MATERIAL')
        row.operator(OBJECT_OT_dwg_mat_make_local.bl_idname, icon='DUPLICATE')
        m = find_material(s.dwgt_mat_from) if s.dwgt_mat_from != "NONE" else None
        if m is not None and m.library is not None:
            col.label(text="リンクのマテリアルは編集できません→「コピーして編集」", icon='INFO')


# ---------- 床の高さ(FL)をスポイトで拾う ----------

class OBJECT_OT_dwg_pick_floor(bpy.types.Operator):
    """床の面をクリックして、その高さを「床の高さ(FL)」にする"""
    bl_idname = "object.dwg_pick_floor"
    bl_label = "床面から拾う"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "3Dビューで実行してください")
            return {'CANCELLED'}
        context.window_manager.modal_handler_add(self)
        context.window.cursor_modal_set('EYEDROPPER')
        context.workspace.status_text_set("床の面をクリックしてFLを拾う / 右クリック・Escで中止")
        return {'RUNNING_MODAL'}

    def _end(self, context):
        context.window.cursor_modal_restore()
        context.workspace.status_text_set(None)

    def modal(self, context, event):
        if event.type in {'RIGHTMOUSE', 'ESC'}:
            self._end(context)
            return {'CANCELLED'}
        if is_undo_key(event):
            self._end(context)
            return {'CANCELLED'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            z = pick_floor_z(context, (event.mouse_region_x, event.mouse_region_y))
            if z is None:
                self.report({'WARNING'}, "水平な面(床)をクリックしてください")
                return {'RUNNING_MODAL'}
            context.scene.dwgt_FL = z
            self.report({'INFO'}, "床の高さ FL = %.0fmm" % (z * 1000))
            self._end(context)
            return {'FINISHED'}
        return {'RUNNING_MODAL'} if event.type in {'LEFTMOUSE'} else {'PASS_THROUGH'}


def pick_floor_z(context, coord):
    """画面上の座標で一番手前の水平な面の高さ。置いた窓・開口ボックスは無視。見つからなければ None"""
    region, rv3d = context.region, context.region_data
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
    dg = context.evaluated_depsgraph_get()
    best = None
    for obj in context.visible_objects:
        if obj.type != 'MESH' or _is_ours(obj):
            continue
        r = wp._ray_obj(obj, origin, direction, dg)
        if r and (best is None or r[0] < best[0]):
            best = r
    if best is None or abs(best[2].z) < 0.7:     # 一番手前が壁など(水平でない)なら拾わない
        return None
    return best[1].z


def fl_row(layout, s):
    """「床の高さ(FL)」の入力欄と、スポイトのボタン"""
    r = layout.row(align=True)
    r.prop(s, "dwgt_FL")
    r.operator(OBJECT_OT_dwg_pick_floor.bl_idname, text="", icon='EYEDROPPER')


def placement_from_hit(wall, loc, n, side, FL, sill, T_param):
    """クリックした点と面から、窓の置き場所の行列を作る。(行列, 使った壁厚, 測れたか) を返す"""
    dg = bpy.context.evaluated_depsgraph_get()
    back = wp._ray_obj(wall, loc - n * 0.001, -n, dg)
    T = (back[1] - loc).length if back else None
    if T is not None and T > wp.MAX_WALL_THICKNESS:
        T = None
    measured = T is not None
    if T is None:
        T = T_param
    if side == 'OUTSIDE':
        e = n.copy()
        ext = loc.copy()
    else:
        e = -n
        ext = loc - n * T
    e.z = 0.0
    if e.length < 1e-6:
        return None
    e.normalize()
    Z = Vector((0.0, 0.0, 1.0))
    X = e.cross(Z)                       # X = Y × Z (右手系)
    rot = Matrix((X, e, Z)).transposed().to_4x4()
    P = Vector((ext.x, ext.y, FL + sill))
    return Matrix.Translation(P) @ rot, T, measured


def is_undo_key(event):
    """Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y(置くコマンドの途中でUndoされるとBlenderが落ちるので止める)"""
    return event.value == 'PRESS' and (event.ctrl or event.oskey) and event.type in {'Z', 'Y'}


def end_placing(context, op, event):
    """置くコマンドの途中で Undo キーが押されたら、Undo はさせずにコマンドだけ終える"""
    context.workspace.status_text_set(None)
    op.report({'INFO'}, "置くコマンドを終了しました。もう一度 Ctrl+Z で元に戻せます")
    return {'FINISHED'}


def placed_continue(context, label):
    """Shift+クリックで続けて置くとき、1つずつ元に戻せるように Undo の区切りを入れる"""
    try:
        bpy.ops.ed.undo_push(message=label)
    except RuntimeError:
        pass


def select_only(context, obj):
    """置いたものだけを選択してアクティブにする(すぐ「更新」できるように)"""
    for o in context.selected_objects:
        o.select_set(False)
    try:
        obj.select_set(True)
        context.view_layer.objects.active = obj
    except RuntimeError:
        pass


def clean_dead_cuts(wall):
    """窓を消したあとに壁に残った、相手(開口ボックス)が空の「開口_」ブーリアンを消す"""
    for md in list(wall.modifiers):
        if md.type == 'BOOLEAN' and md.name.startswith("開口_") and md.object is None:
            wall.modifiers.remove(md)


def add_cut(wall, cutter, name):
    clean_dead_cuts(wall)
    md = wall.modifiers.new("開口_" + name, 'BOOLEAN')
    md.operation = 'DIFFERENCE'
    md.object = cutter
    md.solver = 'EXACT'
    md.use_hole_tolerant = True   # 面1枚の壁(閉じていない)でも穴を開ける
    return md


def _is_ours(o):
    return bool(o.get(wp.CUTTER_TAG) or ("dwg_template" in o) or ("dwg_src" in o))


def pick_surface(context, coord):
    """画面上の座標から、一番手前の壁を探す。開口ボックスと、置いた窓は無視する"""
    region, rv3d = context.region, context.region_data
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
    dg = context.evaluated_depsgraph_get()
    best = None
    for obj in context.visible_objects:
        if obj.type != 'MESH' or _is_ours(obj) or obj.library is not None:
            continue
        r = wp._ray_obj(obj, origin, direction, dg)
        if r and (best is None or r[0] < best[0]):
            best = (r[0], obj, r[1], r[2])
    if best is None:
        return None
    _, wall, loc, n = best
    if n.dot(direction) > 0:
        n = -n
    return wall, loc, n


def set_sill(root, z):
    """置いた窓(または連窓・段窓)の原点の高さ = 内法下端を z にする。位置の X・Y と向きはそのまま"""
    m = root.matrix_world.copy()
    m.translation.z = z
    root.matrix_world = m


def ensure_object_mode(context):
    """編集モードなどのときは、オブジェクトモードに切り替える(窓は編集モードでは置けないため)"""
    if context.mode == 'OBJECT':
        return True
    try:
        bpy.ops.object.mode_set(mode='OBJECT')
    except Exception:
        pass
    return context.mode == 'OBJECT'


class OBJECT_OT_place_template_window(bpy.types.Operator):
    bl_idname = "object.dwg_place_template_window"
    bl_label = "窓を置く(壁をクリック)"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "3Dビューで実行してください")
            return {'CANCELLED'}
        if template_root(context.scene.dwgt_template) is None:
            self.report({'WARNING'}, "テンプレートの窓(コレクション)を選んでください")
            return {'CANCELLED'}
        if not ensure_object_mode(context):
            self.report({'WARNING'}, "オブジェクトモードに切り替えてから実行してください")
            return {'CANCELLED'}
        tidy_linked_templates(context)
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set("壁をクリックで窓を置く(Shift+クリックで続けて置く) / 右クリック・Escで中止")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'RIGHTMOUSE', 'ESC'}:
            context.workspace.status_text_set(None)
            return {'FINISHED'}
        if is_undo_key(event):
            return end_placing(context, self, event)
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            hit = pick_surface(context, (event.mouse_region_x, event.mouse_region_y))
            if hit:
                s = context.scene
                wall, loc, n = hit
                pl = placement_from_hit(wall, loc, n, s.dwgt_side, s.dwgt_FL, s.dwgt_sill, s.dwgt_T)
                if pl is None:
                    self.report({'WARNING'}, "水平な面には置けません")
                else:
                    mw, T, measured = pl
                    tpl = template_root(s.dwgt_template)
                    root, cutter = build_window(context, tpl, mw, s.dwgt_W, s.dwgt_H, T,
                                                mirror=lock_mirror(tpl, s.dwgt_lock),
                                                options=scene_options(s), shutter_open=scene_shutter_open(s))
                    if cutter is not None:
                        add_cut(wall, cutter, root.name)
                    self.report({'INFO'}, "壁厚 %.0fmm(%s)" % (T * 1000, "測定" if measured else "設定値"))
                    select_only(context, root)
                    if not event.shift:
                        context.workspace.status_text_set(None)
                        return {'FINISHED'}
                    placed_continue(context, "窓を置く")
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}


class OBJECT_OT_update_template_window(bpy.types.Operator):
    bl_idname = "object.dwg_update_template_window"
    bl_label = "選択中の窓を更新"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene
        done = 0
        skipped = 0
        for o in context.selected_objects:
            root = o if "dwg_template" in o else (o.parent if o.parent and "dwg_template" in o.parent else None)
            if root is None:
                continue
            if top_root(root) is not root:           # 連窓・段窓のセルは単体として更新しない(寸法やオプションが崩れる)
                skipped += 1
                continue
            update_window(root, s.dwgt_W, s.dwgt_H, float(root.get("dwg_T", s.dwgt_T)), lock=s.dwgt_lock,
                          options=scene_options(s), shutter_open=scene_shutter_open(s))
            if root.parent is None:                  # 内法下端の高さ(FL + 内法下端)も合わせる。連窓・段窓のセルは動かさない
                set_sill(root, s.dwgt_FL + s.dwgt_sill)
            done += 1
        if done == 0:
            if skipped:
                self.report({'WARNING'}, "連窓・段窓は「選択中の連窓・段窓を更新」で更新してください")
            else:
                self.report({'WARNING'}, "置いた窓を選択してください")
            return {'CANCELLED'}
        if skipped:
            self.report({'WARNING'}, "連窓・段窓の窓(%d個)は更新しませんでした" % skipped)
        return {'FINISHED'}


def template_collection(tpl):
    """テンプレート窓の親オブジェクト tpl が入っているコレクション(パネルのテンプレート欄に入れるもの)"""
    for col in tpl.users_collection:
        if template_root(col) is tpl:
            return col
    for col in bpy.data.collections:
        if template_root(col) is tpl:
            return col
    return None


class OBJECT_OT_read_template_window(bpy.types.Operator):
    """選択中の(単体サッシの)窓の設定を、パネルに読み込む"""
    bl_idname = "object.dwg_read_template_window"
    bl_label = "選択中の窓の設定を読む"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene
        o = context.active_object
        if o is None or not o.select_get():
            o = context.selected_objects[0] if context.selected_objects else None
        top = top_root(o) if o is not None else None
        if top is None:
            self.report({'WARNING'}, "置いた窓を選択してください")
            return {'CANCELLED'}
        if "dwg_template" not in top or top.parent is not None:
            self.report({'WARNING'}, "連窓・段窓の設定は、まだ読めません(単体サッシのみ)")
            return {'CANCELLED'}
        root = top
        tpl = find_object(root.get("dwg_template", ""), root.get("dwg_template_lib", ""))
        col = template_collection(tpl) if tpl is not None else None
        if col is None:
            self.report({'WARNING'}, "テンプレート「%s」が見つかりません(建具セットを読み込んでください)"
                        % root.get("dwg_template", ""))
            return {'CANCELLED'}
        s.dwgt_template = col
        s.dwgt_W = float(root.get("dwg_W", s.dwgt_W))
        s.dwgt_H = float(root.get("dwg_H", s.dwgt_H))
        s.dwgt_T = max(0.085, float(root.get("dwg_T", s.dwgt_T)))
        s.dwgt_sill = root.matrix_world.translation.z - s.dwgt_FL
        if has_lock(tpl):
            base = 'R' if str(tpl[LOCK_KEY]) == "右" else 'L'
            s.dwgt_lock = ({'R': 'L', 'L': 'R'}[base]) if root.get("dwg_mirror") else base
        # オプション(シャッターと面格子は同時に付かないので、片方ずつ入れても打ち消し合わない)
        s.dwgt_opt_shutter = bool(root.get(opt_prop("シャッター"), False))
        s.dwgt_opt_grille = bool(root.get(opt_prop(GRILLE_OPT), False))
        g = str(root.get(GRILLE_ROOT_KEY, "縦"))
        s.dwgt_grille_type = next((k for k, v in GRILLE_NAME.items() if v == g), 'V')
        op = float(root.get(OPEN_KEY, 0.0))
        if op <= 0.001:
            s.dwgt_shutter_state = 'CLOSE'
        elif op >= 0.999:
            s.dwgt_shutter_state = 'OPEN'
        else:
            s.dwgt_shutter_state = 'HALF'
            s.dwgt_shutter_ratio = min(0.95, max(0.05, op))
        if hasattr(s, "dwg_tab"):
            s.dwg_tab = 'SINGLE'
        self.report({'INFO'}, "「%s」の設定を読み込みました" % root.name)
        return {'FINISHED'}


class VIEW3D_PT_dwg_tabs(bpy.types.Panel):
    """建具タブの一番上: 単体サッシ/連窓/段窓の切り替えと、建具セットの読み込み"""
    bl_label = "建具"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "建具"
    bl_options = {'HIDE_HEADER'}
    bl_order = 0

    def draw(self, context):
        s = context.scene
        if _has_composite():
            self.layout.row().prop(s, "dwg_tab", expand=True)
        box = self.layout.box()
        box.prop(s, "dwgt_file", text="")
        box.operator(OBJECT_OT_load_tategu.bl_idname, icon='IMPORT')


def lock_row(layout, tpl, data, prop):
    """錠の左右を選べるテンプレートなら「錠(室内から見て): 右/左」を出す"""
    if has_lock(tpl):
        r = layout.row(align=True)
        r.label(text="錠(室内から見て)")
        r.prop(data, prop, expand=True)


# パネルで選ぶオプション(テンプレートの dwg_option の名前 → シーンのプロパティ)
OPT_SCENE = {"シャッター": "dwgt_opt_shutter", "面格子": "dwgt_opt_grille"}


def _only_shutter(self, _context):
    """シャッターと面格子は同時に付けない(片方を付けたら、もう片方は外す)"""
    if self.dwgt_opt_shutter and self.dwgt_opt_grille:
        self.dwgt_opt_grille = False


def _only_grille(self, _context):
    if self.dwgt_opt_grille and self.dwgt_opt_shutter:
        self.dwgt_opt_shutter = False


def scene_options(s):
    opts = {name: bool(getattr(s, p, False)) for name, p in OPT_SCENE.items()}
    opts["面格子の種類"] = GRILLE_NAME.get(getattr(s, "dwgt_grille_type", 'V'), "縦")
    return opts


def scene_shutter_open(s):
    return shutter_open_value(s.dwgt_shutter_state, s.dwgt_shutter_ratio)


def options_rows(layout, tpl, s):
    """テンプレートが持っているオプションだけ、付ける/付けないの切り替えを出す"""
    names = [n for n in template_options(tpl) if n in OPT_SCENE]
    if not names:
        return
    box = layout.box()
    for name in names:
        box.prop(s, OPT_SCENE[name], text=name + "を付ける")
        if name == "シャッター" and s.dwgt_opt_shutter:
            box.row().prop(s, "dwgt_shutter_state", expand=True)
            if s.dwgt_shutter_state == 'HALF':
                box.prop(s, "dwgt_shutter_ratio", slider=True)
        if name == GRILLE_OPT and s.dwgt_opt_grille:
            box.row().prop(s, "dwgt_grille_type", expand=True)


def _has_composite():
    """連窓・段窓(window_composite.py)が入っているか。無料版では入っていない"""
    return hasattr(bpy.types.Scene, "dwgs_tiers")


def tab_is(context, name):
    if name == 'SINGLE' and not _has_composite():
        return True
    return getattr(context.scene, "dwg_tab", 'SINGLE') == name


class VIEW3D_PT_template_window(bpy.types.Panel):
    bl_label = "単体サッシ"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "建具"
    bl_options = {'HIDE_HEADER'}
    bl_order = 1

    @classmethod
    def poll(cls, context):
        return tab_is(context, 'SINGLE')

    def draw(self, context):
        s = context.scene
        col = self.layout.column()
        r = col.row(align=True)
        r.prop(s, "dwgt_template", text="")
        r.operator(OBJECT_OT_read_template_window.bl_idname, text="", icon='EYEDROPPER')
        lock_row(col, template_root(s.dwgt_template), s, "dwgt_lock")
        options_rows(col, template_root(s.dwgt_template), s)
        col.separator()
        col.prop(s, "dwgt_W")
        col.prop(s, "dwgt_H")
        fl_row(col, s)
        col.prop(s, "dwgt_sill")
        col.prop(s, "dwgt_T")
        col.prop(s, "dwgt_side", expand=True)
        col.separator()
        col.operator(OBJECT_OT_place_template_window.bl_idname, icon='MOD_BOOLEAN')
        col.operator(OBJECT_OT_update_template_window.bl_idname, icon='FILE_REFRESH')


_classes = (OBJECT_OT_load_tategu, OBJECT_OT_place_template_window, OBJECT_OT_update_template_window,
            OBJECT_OT_read_template_window, OBJECT_OT_dwg_mat_replace, OBJECT_OT_dwg_mat_make_local, OBJECT_OT_dwg_pick_floor,
            VIEW3D_PT_dwg_tabs, VIEW3D_PT_template_window, VIEW3D_PT_dwg_material)


@bpy.app.handlers.persistent
def _on_load(_dummy):
    """ファイルを開いたとき: 以前の版で読み込んだLinkのテンプレートを非表示・レンダー無効のコレクションへ移し、
    置いてある窓のマテリアルをローカルにする"""
    try:
        tidy_linked_templates(bpy.context)
    except Exception as e:
        print("建具テンプレートの整理に失敗:", e)
    try:
        localize_all_windows()
    except Exception as e:
        print("建具マテリアルのローカル化に失敗:", e)


def register():
    S = bpy.types.Scene
    S.dwg_tab = bpy.props.EnumProperty(name="建具の種類",
                                       items=[('SINGLE', "単体サッシ", "連・段なしの単体サッシを置く"),
                                              ('REN', "連窓", "横に並べた連窓を作って置く"),
                                              ('DAN', "段窓", "単体サッシ・連窓を縦に積んだ段窓を作って置く")],
                                       default='SINGLE')
    S.dwgt_file = bpy.props.EnumProperty(name="建具セットのファイル", items=_file_items,
                                         description="TateguSetフォルダの、読み込みたい.blendファイル")
    S.dwgt_template = bpy.props.PointerProperty(name="テンプレート", type=bpy.types.Collection, poll=_tpl_col_poll,
                                                description="置く窓のテンプレート(コレクション)")
    S.dwgt_lock = bpy.props.EnumProperty(name="錠", items=LOCK_ITEMS, default='R')
    S.dwgt_W = bpy.props.FloatProperty(name="内法の幅", default=1.65, min=0.1, unit='LENGTH', subtype='DISTANCE')
    S.dwgt_H = bpy.props.FloatProperty(name="内法の高さ", default=0.9, min=0.1, unit='LENGTH', subtype='DISTANCE')
    S.dwgt_FL = bpy.props.FloatProperty(name="床の高さ(FL) Z=", default=0.0, unit='LENGTH', subtype='DISTANCE')
    S.dwgt_sill = bpy.props.FloatProperty(name="内法下端の高さ(FLから)", default=1.0, unit='LENGTH', subtype='DISTANCE')
    S.dwgt_T = bpy.props.FloatProperty(name="壁厚(面1枚の壁のとき)", description="壁の厚みが測れないとき(面1枚の壁など)に使う壁厚。厚みのある壁は自動で測る", default=0.16, min=0.085, unit='LENGTH', subtype='DISTANCE')
    S.dwgt_side = bpy.props.EnumProperty(name="クリックする側",
                                         items=[('OUTSIDE', "外側", "外壁の面をクリックする"),
                                                ('INSIDE', "内側", "内壁の面をクリックする")], default='OUTSIDE')
    S.dwgt_opt_shutter = bpy.props.BoolProperty(name="シャッター", default=False, update=_only_shutter,
                                                description="シャッターのあるテンプレートで、シャッターを付ける(面格子は外れる)")
    S.dwgt_opt_grille = bpy.props.BoolProperty(name="面格子", default=False, update=_only_grille,
                                               description="面格子のあるテンプレートで、面格子を付ける(シャッターは外れる)")
    S.dwgt_grille_type = bpy.props.EnumProperty(name="面格子の種類", items=GRILLE_TYPES, default='V')
    S.dwgt_shutter_state = bpy.props.EnumProperty(name="シャッター", items=SHUTTER_STATES, default='CLOSE')
    S.dwgt_shutter_ratio = bpy.props.FloatProperty(name="開き具合", default=0.5, min=0.05, max=0.95,
                                                   subtype='FACTOR',
                                                   description="半開のとき、どれだけ開いているか(0=閉に近い / 1=開に近い)")
    S.dwgt_mat_from = bpy.props.EnumProperty(name="元のマテリアル", items=_mat_items,
                                             description="選択中の窓で使われているマテリアル")
    S.dwgt_mat_to = bpy.props.PointerProperty(name="置き換え先", type=bpy.types.Material,
                                              description="置き換えるマテリアル")
    for c in _classes:
        bpy.utils.register_class(c)
    if _on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load)


def unregister():
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
    S = bpy.types.Scene
    for p in ("dwgt_lock", "dwg_tab", "dwgt_file", "dwgt_template", "dwgt_W", "dwgt_H", "dwgt_FL", "dwgt_sill", "dwgt_T", "dwgt_side",
              "dwgt_mat_from", "dwgt_mat_to", "dwgt_opt_shutter", "dwgt_opt_grille",
              "dwgt_shutter_state", "dwgt_shutter_ratio", "dwgt_grille_type"):
        if hasattr(S, p):
            delattr(S, p)
