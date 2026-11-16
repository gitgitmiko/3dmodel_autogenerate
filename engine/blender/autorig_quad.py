"""Auto-rig hewan berkaki empat + animasi dasar (Blender headless).

Model diasumsikan dibuat dari tampak SAMPING hewan, jadi di tampak depan render (kamera -Y Blender)
hewan terlihat dari samping: panjang badan sepanjang sumbu X, kepala di +X atau -X.

  prepare: blender -b --factory-startup -P autorig_quad.py -- prepare in.glb out_dir [height]
  rig:     blender -b --factory-startup -P autorig_quad.py -- rig in.glb out_dir [height] [markers.json] [anims]

Penanda (koordinat tampak depan x, z dalam meter): head, withers (pundak, di atas kaki depan),
hips (pinggul, di atas kaki belakang), tail_tip (= hips bila tanpa ekor), front_foot, back_foot.
Posisi kiri/kanan (kedalaman) tiap kaki dihitung otomatis dari mesh.
"""

import json
import math
import sys
from pathlib import Path

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector

argv = sys.argv[sys.argv.index("--") + 1:]
MODE, SRC, OUT = argv[0], argv[1], Path(argv[2]).resolve()
HEIGHT = float(argv[3]) if len(argv) > 3 and argv[3] else 0.6
MARKERS_FILE = argv[4] if len(argv) > 4 and argv[4] else None
ANIMS = argv[5].split(",") if len(argv) > 5 and argv[5] else ["idle", "walk", "run", "jump"]
FPS = 30
MARKER_KEYS = ["head", "withers", "hips", "tail_tip", "front_foot", "back_foot"]
OUT.mkdir(parents=True, exist_ok=True)
report = {"rig_type": "quadruped", "warnings": []}


def warn(msg):
    print("[autorig] WARN:", msg)
    report["warnings"].append(msg)


# ---------------------------------------------------------------- impor & normalisasi (sama seperti humanoid)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=str(Path(SRC).resolve()))
meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
for o in bpy.context.scene.objects:
    o.select_set(o.type == "MESH")
bpy.context.view_layer.objects.active = meshes[0]
if len(meshes) > 1:
    bpy.ops.object.join()
body = bpy.context.view_layer.objects.active
body.name = "Body"
mw = body.matrix_world.copy()
body.parent = None
body.matrix_world = mw
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
for o in list(bpy.context.scene.objects):
    if o is not body:
        bpy.data.objects.remove(o, do_unlink=True)

bm = bmesh.new()
bm.from_mesh(body.data)
bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)  # sambungan UV
bm.verts.ensure_lookup_table()
seen, islands = set(), []
for v in bm.verts:
    if v.index in seen:
        continue
    stack, isl = [v], []
    seen.add(v.index)
    while stack:
        cur = stack.pop()
        isl.append(cur)
        for e in cur.link_edges:
            o = e.other_vert(cur)
            if o.index not in seen:
                seen.add(o.index)
                stack.append(o)
    islands.append(isl)
if len(islands) > 1:
    biggest = max(len(i) for i in islands)
    drop = [v for isl in islands if len(isl) < biggest * 0.15 for v in isl]
    if drop:
        bmesh.ops.delete(bm, geom=drop, context="VERTS")
bm.to_mesh(body.data)
bm.free()

V = np.array([v.co[:] for v in body.data.vertices])
lo, hi = V.min(0), V.max(0)
scale = HEIGHT / (hi[2] - lo[2])
offset = np.array([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
body.data.transform(Matrix.Diagonal((scale, scale, scale, 1)) @ Matrix.Translation(Vector(offset)))
body.data.update()
V = np.array([v.co[:] for v in body.data.vertices])
H = HEIGHT
LEN = float(V[:, 0].max() - V[:, 0].min())
TEXTURED = any(n.type == "TEX_IMAGE" for m in body.data.materials if m and m.node_tree for n in m.node_tree.nodes)
if not TEXTURED and body.data.color_attributes:
    mat = bpy.data.materials.new("BodyMat")
    mat.use_nodes = True
    nt = mat.node_tree
    ca = nt.nodes.new("ShaderNodeVertexColor")
    ca.layer_name = body.data.color_attributes[0].name
    nt.links.new(ca.outputs["Color"], nt.nodes["Principled BSDF"].inputs["Base Color"])
    body.data.materials.clear()
    body.data.materials.append(mat)


# ---------------------------------------------------------------- geometri
def column(x, half=0.02):
    return V[np.abs(V[:, 0] - x) < half * LEN]


def top_at(x):
    c = column(x)
    return float(c[:, 2].max()) if len(c) else 0.0


def depth_center(x, zmin=None, zmax=None):
    c = column(x, 0.04)
    if zmin is not None:
        c = c[c[:, 2] >= zmin]
    if zmax is not None:
        c = c[c[:, 2] <= zmax]
    return float((c[:, 1].min() + c[:, 1].max()) / 2) if len(c) else 0.0


def clusters_x(pts, gap):
    if len(pts) == 0:
        return []
    p = pts[np.argsort(pts[:, 0])]
    cut = np.where(np.diff(p[:, 0]) > gap)[0]
    return [g for g in np.split(p, cut + 1) if len(g) >= 5]


def estimate_markers():
    m = {}
    xs = np.linspace(V[:, 0].min(), V[:, 0].max(), 80)
    tops = np.array([top_at(x) for x in xs])
    n = len(xs) // 4
    # Kepala = ujung yang lebih "gemuk". Ujung yang lebih tinggi tidak bisa dipakai: ekor kucing/anjing sering
    # terangkat lebih tinggi daripada kepala. Hitung massa (jumlah vertex) di atas tinggi perut pada tiap ujung.
    x0, x1 = V[:, 0].min(), V[:, 0].max()
    zone = 0.25 * (x1 - x0)
    above = V[V[:, 2] > 0.35 * H]
    mass_pos = int((above[:, 0] > x1 - zone).sum())
    mass_neg = int((above[:, 0] < x0 + zone).sum())
    d = 1.0 if mass_pos >= mass_neg else -1.0
    report["head_side"] = {"mass_+x": mass_pos, "mass_-x": mass_neg}
    feet = clusters_x(V[V[:, 2] < 0.06 * H], 0.04 * LEN)
    feet = sorted(feet, key=len, reverse=True)[:4]
    if len(feet) >= 2:
        fx = sorted(g[:, 0].mean() for g in feet)
        front_x, back_x = (fx[-1], fx[0]) if d > 0 else (fx[0], fx[-1])
    else:
        warn("Kaki depan/belakang tidak terpisah di lantai — periksa penanda")
        front_x = d * 0.28 * LEN
        back_x = -d * 0.28 * LEN
    m["front_foot"] = [front_x, 0.0]
    m["back_foot"] = [back_x, 0.0]
    # Garis punggung diukur di tengah badan; di atas kaki depan sering ada kepala/telinga yang lebih tinggi.
    back_top = top_at((front_x + back_x) / 2)
    m["withers"] = [front_x, min(top_at(front_x), back_top * 1.08)]
    m["hips"] = [back_x, min(top_at(back_x), back_top * 1.08)]
    head_zone = xs[-n:] if d > 0 else xs[:n]
    head_tops = tops[-n:] if d > 0 else tops[:n]
    hx = float(head_zone[int(np.argmax(head_tops))])
    col = column(hx)
    upper = col[col[:, 2] > back_top] if len(col) else col  # bagian kepala di atas garis punggung
    hz = float((upper[:, 2].max() + upper[:, 2].min()) / 2) if len(upper) else top_at(hx) * 0.85
    m["head"] = [hx, hz]
    tail_side = V[(d * V[:, 0] < d * back_x - 0.04 * LEN) & (V[:, 2] > 0.25 * m["hips"][1])]
    if len(tail_side):
        tip = tail_side[np.argmin(d * tail_side[:, 0])]
        m["tail_tip"] = [float(tip[0]), float(tip[2])]
    else:
        m["tail_tip"] = list(m["hips"])
    return {k: [round(float(a), 4) for a in v] for k, v in m.items()}


FRAME_PX = 768
frame_size = max(H, LEN) * 1.08
frame = {"cx": 0.0, "cz": H / 2, "size": frame_size, "px": FRAME_PX}

if MODE == "prepare":
    markers = estimate_markers()
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    sh = scene.display.shading
    sh.light, sh.color_type, sh.show_cavity = "STUDIO", ("TEXTURE" if TEXTURED else "VERTEX"), True
    scene.world = bpy.data.worlds.new("w")
    scene.world.color = (0.12, 0.12, 0.14)
    scene.render.resolution_x = scene.render.resolution_y = FRAME_PX
    cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
    cam.data.type, cam.data.ortho_scale = "ORTHO", frame_size
    scene.collection.objects.link(cam)
    cam.location = (0, -max(H, LEN) * 4, H / 2)
    cam.rotation_euler = (math.pi / 2, 0, 0)
    scene.camera = cam
    scene.render.filepath = str(OUT / "front.png")
    bpy.ops.render.render(write_still=True)
    (OUT / "prep.json").write_text(json.dumps({"rig_type": "quadruped", "height_m": H, "markers": markers,
                                                "frame": frame, "warnings": report["warnings"]}, indent=2))
    print("[autorig] PREPARE OK", json.dumps(markers))
    raise SystemExit(0)

# ---------------------------------------------------------------- sendi dari penanda
given = {}
if MARKERS_FILE:
    given = json.loads(Path(MARKERS_FILE).read_text())
    given = given.get("markers", given)
mk = {} if all(k in given for k in MARKER_KEYS) else estimate_markers()
for k in MARKER_KEYS:
    if k in given:
        mk[k] = [float(given[k][0]), float(given[k][1])]
report["markers"] = mk

wx, wz = mk["withers"]
hx_, hz_ = mk["hips"]
D = 1.0 if wx >= hx_ else -1.0          # arah hadap hewan sepanjang X
F = Vector((D, 0, 0))                   # depan
LV = Vector((0, D, 0))                  # kiri hewan = cross(Z, depan)
mid_x = (wx + hx_) / 2
mid_col = column(mid_x, 0.05)
belly = float(mid_col[:, 2].min()) if len(mid_col) else min(wz, hz_) * 0.5
belly = min(belly, 0.8 * min(wz, hz_))
cy = depth_center(mid_x, zmin=belly)


def spine_pt(x, top):
    return Vector((x, cy, belly + 0.6 * (top - belly)))


P0 = spine_pt(hx_, hz_)
C0 = spine_pt(wx, wz)
head = Vector((mk["head"][0], depth_center(mk["head"][0], zmin=belly), mk["head"][1]))
snout_pts = V[(D * V[:, 0] > D * head.x) & (V[:, 2] > belly)]
snout_x = float(snout_pts[:, 0][np.argmax(D * snout_pts[:, 0])]) if len(snout_pts) else head.x + D * 0.08 * LEN
head_base = C0.lerp(head, 0.72)
snout = Vector((snout_x, head.y, head.z - 0.02 * H))
tail_tip = Vector((mk["tail_tip"][0], cy, mk["tail_tip"][1]))
has_tail = (tail_tip - Vector((hx_, cy, hz_))).length > 0.06 * LEN
tail0 = Vector((hx_ - D * 0.04 * LEN, cy, belly + 0.85 * (hz_ - belly)))


def leg_y(foot_x):
    """Kedalaman kaki kiri & kanan dari titik mesh di bawah perut sekitar telapak."""
    pts = V[(np.abs(V[:, 0] - foot_x) < 0.07 * LEN) & (V[:, 2] < 0.6 * belly)]
    half = max(float(np.abs(V[:, 1] - cy).max()) * 0.45, 0.02 * H)
    if len(pts) < 10:
        return cy + D * half, cy - D * half
    left = pts[(pts[:, 1] - cy) * D > 0][:, 1]
    right = pts[(pts[:, 1] - cy) * D <= 0][:, 1]
    yl = float(np.median(left)) if len(left) > 5 else cy + D * half
    yr = float(np.median(right)) if len(right) > 5 else cy - D * half
    if abs(yl - yr) < 0.02 * H:  # kaki kiri-kanan tidak terpisah (foto samping menutupi)
        yl, yr = cy + D * half, cy - D * half
    return yl, yr


ffx, bfx = mk["front_foot"][0], mk["back_foot"][0]
fy = leg_y(ffx)
by = leg_y(bfx)
legs = {}
for side, i in (("Left", 0), ("Right", 1)):
    y = fy[i]
    legs[f"{side}Front"] = [Vector((wx, y, (wz + belly) / 2)), Vector((ffx + D * 0.01 * LEN, y, belly * 0.9)),
                            Vector((ffx, y, belly * 0.3)), Vector((ffx + D * 0.06 * LEN, y, 0.015 * H))]
    y = by[i]
    legs[f"{side}Back"] = [Vector((hx_, y, (hz_ + belly) / 2)), Vector((bfx + D * 0.04 * LEN, y, belly * 0.9)),
                           Vector((bfx - D * 0.03 * LEN, y, belly * 0.35)), Vector((bfx + D * 0.05 * LEN, y, 0.015 * H))]

# ---------------------------------------------------------------- armature
arm_data = bpy.data.armatures.new("Armature")
rig = bpy.data.objects.new("Armature", arm_data)
bpy.context.scene.collection.objects.link(rig)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode="EDIT")
eb = arm_data.edit_bones


def bone(name, h, t, parent=None, connect=False):
    b = eb.new(name)
    b.head, b.tail = Vector(h), Vector(t)
    if parent:
        b.parent = eb[parent]
        b.use_connect = connect
    return b


sp = [P0.lerp(C0, f) for f in (0.0, 0.25, 0.5, 0.75, 1.0)]
bone("Hips", sp[0], sp[1])
bone("Spine", sp[1], sp[2], "Hips", True)
bone("Spine1", sp[2], sp[3], "Spine", True)
bone("Chest", sp[3], sp[4], "Spine1", True)
bone("Neck", sp[4], head_base, "Chest", True)
bone("Head", head_base, snout, "Neck", True)
if has_tail:
    t = [tail0.lerp(tail_tip, f) for f in (0, 1 / 3, 2 / 3, 1)]
    bone("Tail1", t[0], t[1], "Hips")
    bone("Tail2", t[1], t[2], "Tail1", True)
    bone("Tail3", t[2], t[3], "Tail2", True)
for name, pts in legs.items():
    parent = "Chest" if "Front" in name else "Hips"
    bone(f"{name}Upper", pts[0], pts[1], parent)
    bone(f"{name}Lower", pts[1], pts[2], f"{name}Upper", True)
    bone(f"{name}Paw", pts[2], pts[3], f"{name}Lower", True)
for b in eb:
    b.align_roll(Vector((0, 0, 1)) if abs(b.vector.normalized().z) < 0.9 else F)
bpy.ops.object.mode_set(mode="OBJECT")

# ---------------------------------------------------------------- skinning
bpy.ops.object.select_all(action="DESELECT")
body.select_set(True)
rig.select_set(True)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.parent_set(type="ARMATURE_AUTO")
side = (V[:, 1] - cy) * D  # >0 = sisi kiri hewan
left_idx = np.where(side > 0)[0].tolist()
right_idx = np.where(side <= 0)[0].tolist()
for vg in body.vertex_groups:
    if vg.name.startswith("Right") and left_idx:
        vg.remove(left_idx)
    elif vg.name.startswith("Left") and right_idx:
        vg.remove(right_idx)
bone_names = [b.name for b in arm_data.bones]
segs = {b.name: (np.array(b.head_local[:]), np.array(b.tail_local[:])) for b in arm_data.bones}


def seg_dist(p, a, b):
    ab = b - a
    t_ = np.clip(((p - a) @ ab) / max(ab @ ab, 1e-9), 0, 1)
    return np.linalg.norm(p - (a[None] + t_[:, None] * ab[None]), axis=1)


groups = {vg.index: vg.name for vg in body.vertex_groups}
total = np.zeros(len(V))
for v in body.data.vertices:
    total[v.index] = sum(g.weight for g in v.groups if groups.get(g.group) in segs)
missing = np.where(total < 1e-3)[0]
if len(missing):
    Dm = np.stack([seg_dist(V[missing], *segs[n]) for n in bone_names], axis=1)
    for j, n in enumerate(bone_names):
        if n.startswith("Left"):
            Dm[side[missing] <= 0, j] = np.inf
        elif n.startswith("Right"):
            Dm[side[missing] > 0, j] = np.inf
    near = np.argsort(Dm, axis=1)[:, :2]
    for row, vi in enumerate(missing):
        dd = Dm[row, near[row]] + 1e-4
        w = (1 / dd**2) / (1 / dd**2).sum()
        for j, wt in zip(near[row], w):
            vg = body.vertex_groups.get(bone_names[j]) or body.vertex_groups.new(name=bone_names[j])
            vg.add([int(vi)], float(wt), "REPLACE")
    if len(missing) > 0.05 * len(V):
        warn(f"{len(missing)} vertex tanpa bobot diberi bobot dari tulang terdekat")

# ---------------------------------------------------------------- animasi
pb = rig.pose.bones
for p in pb:
    p.rotation_mode = "QUATERNION"
rest_q = {b.name: b.matrix_local.to_quaternion() for b in arm_data.bones}
AX = {"L": LV, "Z": Vector((0, 0, 1)), "F": F}
D2R = math.pi / 180


def arm_space(*rots):
    q = Quaternion()
    for ax, deg in rots:
        q = Quaternion(AX[ax], deg * D2R) @ q
    return q


def to_local(name, q):
    r = rest_q[name]
    return r.inverted() @ q @ r


# Sudut positif terhadap sumbu L memutar ujung bawah tulang ke BELAKANG, jadi kaki maju = negatif.
LEG_NAMES = ["LeftFront", "RightFront", "LeftBack", "RightBack"]


def leg_pose(name, swing, lift, crouch=0.0):
    front = "Front" in name
    return {
        f"{name}Upper": [("L", -swing + (12 if front else -18) * crouch)],
        f"{name}Lower": [("L", (35 if front else -40) * lift + (-25 if front else 45) * crouch)],
        f"{name}Paw": [("L", (-20 if front else 25) * lift + (12 if front else -25) * crouch)],
    }


def tail(r, t, speed=2.0, amp=18.0):
    if has_tail:
        for i, n in enumerate(("Tail1", "Tail2", "Tail3")):
            r[n] = [("Z", amp * (0.6 + 0.4 * i) * math.sin(2 * math.pi * (speed * t - 0.12 * i)))]


def pose_idle(t):
    b = math.sin(2 * math.pi * t / 2.4)
    r = {"Spine1": [("L", 1.5 * b)], "Chest": [("L", 1.5 * b)],
         "Head": [("Z", 10 * math.sin(2 * math.pi * t / 4.8)), ("L", -3 * b)]}
    tail(r, t, speed=1.25, amp=12)
    return r, Vector((0, 0, 0.004 * H * b))


def pose_walk(t, run=False):
    T = 0.55 if run else 1.0
    u = t / T
    phases = {"LeftBack": 0.0, "LeftFront": 0.25, "RightBack": 0.5, "RightFront": 0.75} if not run else \
        {"LeftFront": 0.0, "RightFront": 0.08, "LeftBack": 0.5, "RightBack": 0.58}
    amp = 38 if run else 24
    r = {}
    for n, ph in phases.items():
        a = 2 * math.pi * (u - ph)
        r.update(leg_pose(n, amp * math.sin(a), max(0.0, math.cos(a)) ** 1.3 * (1.3 if run else 1.0)))
    flex = math.sin(2 * math.pi * u)
    r["Spine"] = [("L", (9 if run else 2) * flex)]
    r["Chest"] = [("L", -(7 if run else 1.5) * flex), ("Z", 3 * math.sin(2 * math.pi * u))]
    r["Neck"] = [("L", (6 if run else 3) * math.sin(4 * math.pi * u))]
    tail(r, t, speed=2.0 if run else 1.5, amp=10 if run else 15)
    bob = (0.05 if run else 0.012) * H * (max(0.0, math.sin(2 * math.pi * u)) if run else abs(math.sin(2 * math.pi * u)))
    return r, Vector((0, 0, bob))


def pose_jump(t):
    u = (t % 1.3) / 1.3

    def ease(a, b, x):
        x = min(max((x - a) / (b - a), 0), 1)
        return x * x * (3 - 2 * x)

    crouch = ease(0.0, 0.25, u) * (1 - ease(0.25, 0.35, u)) + ease(0.75, 0.82, u) * (1 - ease(0.82, 1.0, u))
    air = ease(0.3, 0.45, u) * (1 - ease(0.6, 0.78, u))
    up = math.sin(math.pi * min(max((u - 0.3) / 0.45, 0), 1))
    r = {}
    for n in LEG_NAMES:
        front = "Front" in n
        r.update(leg_pose(n, (35 if front else -35) * air, 0.8 * air, crouch))
    r["Spine"] = [("L", -8 * air + 6 * crouch)]
    r["Neck"] = [("L", -12 * air)]
    tail(r, t, speed=1.0, amp=8)
    return r, Vector((0, 0, -0.12 * H * crouch + 0.35 * H * up))


LIB = {"idle": (pose_idle, 2.4, True), "walk": (pose_walk, 1.0, True),
       "run": (lambda t: pose_walk(t, run=True), 0.55, True), "jump": (pose_jump, 1.3, False)}
rig.animation_data_create()
bpy.context.scene.render.fps = FPS
hips_rest = arm_data.bones["Hips"].matrix_local.to_3x3()
made = []
for anim in ANIMS:
    if anim not in LIB:
        continue
    fn, dur, loop = LIB[anim]
    act = bpy.data.actions.new(anim)
    act.use_fake_user = True
    rig.animation_data.action = act
    n = round(dur * FPS)
    for f in range(0, n + 1):
        rots, off = fn(f / FPS)
        for p in pb:
            p.rotation_quaternion = to_local(p.name, arm_space(*rots.get(p.name, [])))
            p.keyframe_insert("rotation_quaternion", frame=f, group=p.name)
        pb["Hips"].location = hips_rest.inverted() @ off
        pb["Hips"].keyframe_insert("location", frame=f, group="Hips")
    act.frame_range = (0, n)
    tr = rig.animation_data.nla_tracks.new()
    tr.name = anim
    tr.strips.new(anim, 0, act)
    rig.animation_data.action = None
    made.append({"name": anim, "frames": n, "seconds": round(n / FPS, 2), "loop": loop})
for p in pb:
    p.rotation_quaternion = Quaternion()
    p.location = Vector()

# ---------------------------------------------------------------- ekspor
bpy.ops.object.select_all(action="SELECT")
bpy.ops.export_scene.gltf(filepath=str(OUT / "rigged.glb"), export_format="GLB", export_animations=True,
                          export_animation_mode="ACTIONS", export_vertex_color="NONE" if TEXTURED else "ACTIVE",
                          export_skins=True, export_force_sampling=True, export_optimize_animation_size=True)
try:
    bpy.ops.export_scene.fbx(filepath=str(OUT / "rigged.fbx"), add_leaf_bones=False, bake_anim=True,
                             bake_anim_use_all_actions=True, bake_anim_use_nla_strips=False, path_mode="COPY",
                             embed_textures=True)
except Exception as e:
    warn(f"Ekspor FBX gagal: {e}")
report.update({"height_m": HEIGHT, "bones": bone_names, "animations": made, "vertices": len(V),
               "facing": "+X" if D > 0 else "-X", "has_tail": bool(has_tail)})
(OUT / "rig.json").write_text(json.dumps(report, indent=2))
print("[autorig] OK", json.dumps({"animations": [m["name"] for m in made], "warnings": report["warnings"]}))
