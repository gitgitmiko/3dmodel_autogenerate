"""Auto-rig humanoid + animasi dasar, dijalankan di Blender headless.

Dua tahap (seperti Meshy/Mixamo):
  prepare: blender -b --factory-startup -P autorig.py -- prepare in.glb out_dir [height_m]
           -> front.png (tampak depan) + prep.json (tebakan 8 penanda + pemetaan piksel<->dunia)
  rig:     blender -b --factory-startup -P autorig.py -- rig in.glb out_dir [height_m] [markers.json] [anims]
           -> rigged.glb (mesh + armature + animasi), rigged.fbx, rig.json

Penanda (koordinat tampak depan, satuan meter setelah normalisasi; x = kiri karakter positif, z = atas):
  chin, groin, elbow_l, elbow_r, wrist_l, wrist_r, knee_l, knee_r
Kedalaman (sumbu depan-belakang) setiap sendi dihitung otomatis dari bentuk mesh.

Tanpa GPU/ML:
1. Normalisasi: kaki di lantai, pusat di sumbu Z, tinggi = height_m. Potongan kecil yang melayang dibuang.
2. Tebakan penanda dari irisan horizontal mesh.
3. Kerangka 22 tulang bernama standar Mixamo (Hips, Spine, LeftArm, ...), kompatibel dengan retargeting.
4. Skinning "bone heat" Blender; vertex yang gagal diberi bobot dari tulang terdekat.
5. Animasi prosedural (rotasi didefinisikan di ruang armature lalu dikonversi ke ruang tulang).
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
MODE = argv[0]
SRC = argv[1]
OUT = Path(argv[2])
HEIGHT = float(argv[3]) if len(argv) > 3 and argv[3] else 1.75
MARKERS_FILE = argv[4] if len(argv) > 4 and argv[4] else None
ANIMS = argv[5].split(",") if len(argv) > 5 and argv[5] else ["idle", "walk", "run", "wave", "jump"]
FPS = 30
MARKER_KEYS = ["chin", "groin", "elbow_l", "elbow_r", "wrist_l", "wrist_r", "knee_l", "knee_r"]
OUT.mkdir(parents=True, exist_ok=True)
report = {"warnings": []}


def warn(msg):
    print("[autorig] WARN:", msg)
    report["warnings"].append(msg)


# ---------------------------------------------------------------- 1. impor & normalisasi
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=str(Path(SRC).resolve()))
meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
if not meshes:
    raise SystemExit("GLB tidak berisi mesh")
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

# Buang potongan lepas yang kecil (mis. ujung ekor melayang) agar tidak merusak deteksi & bobot.
bm = bmesh.new()
bm.from_mesh(body.data)
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
        print(f"[autorig] membuang {len(drop)} vertex dari potongan lepas")
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

# Material yang menampilkan vertex color (TripoSR menyimpan warna per vertex).
if body.data.color_attributes:
    mat = bpy.data.materials.new("BodyMat")
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    ca = nt.nodes.new("ShaderNodeVertexColor")
    ca.layer_name = body.data.color_attributes[0].name
    nt.links.new(ca.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.8
    body.data.materials.clear()
    body.data.materials.append(mat)


# ---------------------------------------------------------------- 2. alat bantu geometri
def slice_pts(z, half=0.006):
    return V[np.abs(V[:, 2] - z) < half * H]


def clusters_1d(pts, gap=0.012):
    """Kelompokkan titik berdasarkan X; pisah bila ada celah > gap*H."""
    if len(pts) == 0:
        return []
    p = pts[np.argsort(pts[:, 0])]
    cut = np.where(np.diff(p[:, 0]) > gap * H)[0]
    return [g for g in np.split(p, cut + 1) if len(g) >= 3]


def nearest_cluster(cls, x):
    def dist(g):
        a, b = g[:, 0].min(), g[:, 0].max()
        return 0 if a <= x <= b else min(abs(a - x), abs(b - x))

    return min(cls, key=dist) if cls else None


def center_cluster(z):
    return nearest_cluster(clusters_1d(slice_pts(z)), 0.0)


def width_at(z):
    g = center_cluster(z)
    return (g[:, 0].max() - g[:, 0].min()) if g is not None else 0.0


def depth_at(x, z, r=0.035):
    """Kedalaman (y) tengah penampang mesh di sekitar titik tampak depan (x, z)."""
    for rr in (r, r * 2, r * 4):
        m = (np.abs(V[:, 0] - x) < rr * H) & (np.abs(V[:, 2] - z) < rr * H)
        if m.sum() >= 4:
            ys = V[m, 1]
            return float((ys.min() + ys.max()) / 2)
    return 0.0


# ---------------------------------------------------------------- 3. tebakan penanda
def estimate_markers():
    m = {}
    # Kaki: cari irisan terendah yang memuat dua kelompok di kiri & kanan pusat, lacak ke atas sampai menyatu.
    start, cur = None, None
    for z in np.arange(0.02 * H, 0.35 * H, 0.01 * H):
        cs = [g for g in clusters_1d(slice_pts(z)) if abs(g[:, 0].mean()) < 0.25 * H]
        L = [g for g in cs if g[:, 0].mean() > 0]
        R = [g for g in cs if g[:, 0].mean() < 0]
        if L and R:
            start = z
            cur = {"l": min(L, key=lambda g: g[:, 0].mean())[:, 0].mean(),
                   "r": max(R, key=lambda g: g[:, 0].mean())[:, 0].mean()}
            break
    crotch = None
    track = {"l": [], "r": []}
    if start is not None:
        for z in np.arange(start, 0.65 * H, 0.01 * H):
            cs = clusters_1d(slice_pts(z))
            if not cs:
                continue
            gl, gr = nearest_cluster(cs, cur["l"]), nearest_cluster(cs, cur["r"])
            if gl is gr:
                crotch = z
                break
            for s, g in (("l", gl), ("r", gr)):
                cur[s] = g[:, 0].mean()
                track[s].append((z, cur[s]))
    if crotch is None or crotch < 0.2 * H:
        warn("Selangkangan tidak terdeteksi otomatis — periksa penanda")
        crotch = 0.47 * H
    m["groin"] = [0.0, crotch]

    # Leher = bagian tersempit di atas dada; dagu sedikit di atasnya.
    span = H - crotch
    zs = np.arange(crotch + 0.45 * span, crotch + 0.9 * span, 0.005 * H)
    ws = np.array([width_at(z) for z in zs])
    ok = ws > 0.02 * H
    neck = float(zs[ok][np.argmin(ws[ok])]) if ok.any() else crotch + 0.72 * span
    torso = neck - crotch
    m["chin"] = [0.0, neck + 0.06 * torso]

    # Lutut: posisi x kaki di tengah antara telapak dan selangkangan.
    for s, sign in (("l", 1), ("r", -1)):
        kz = 0.05 * H + 0.5 * (crotch - 0.05 * H)
        tr = track[s]
        kx = tr[int(np.argmin([abs(t[0] - kz) for t in tr]))][1] if tr else sign * 0.08 * H
        m[f"knee_{s}"] = [kx, kz]

    # Lengan: titik terjauh dari bahu di sisi badan, dibatasi panjang lengan wajar (~1.2x panjang badan).
    waist_g = center_cluster(crotch + 0.35 * torso)
    waist_half = np.abs(waist_g[:, 0]).max() if waist_g is not None else 0.1 * H
    sh_z = neck - 0.10 * torso
    sh_x = max(min(width_at(crotch + 0.72 * torso) / 2, waist_half * 1.6) * 0.85, 0.06 * H)
    max_len = 1.25 * torso
    for s, sign in (("l", 1), ("r", -1)):
        sh = np.array([sign * sh_x, sh_z])
        mask = (sign * V[:, 0] > waist_half * 1.08) & (V[:, 2] > crotch - 0.35 * torso) & (V[:, 2] < neck)
        pts = V[mask][:, [0, 2]]
        d = np.linalg.norm(pts - sh, axis=1) if len(pts) else np.array([])
        pts, d = (pts[d <= max_len], d[d <= max_len]) if len(d) else (pts, d)
        if len(pts) < 30:
            warn(f"Lengan {'kiri' if s == 'l' else 'kanan'} menempel ke badan — periksa penanda")
            tip = np.array([sign * (sh_x + 0.06 * H), crotch])
        else:
            tip = pts[np.argmax(d)]
        vec = tip - sh
        m[f"elbow_{s}"] = (sh + vec * 0.47).tolist()
        m[f"wrist_{s}"] = (sh + vec * 0.85).tolist()
    return {k: [round(float(a), 4) for a in v] for k, v in m.items()}


# ---------------------------------------------------------------- kamera tampak depan (pemetaan piksel)
FRAME_PX = 768
frame_size = max(H, float(V[:, 0].max() - V[:, 0].min())) * 1.08
frame = {"cx": 0.0, "cz": H / 2, "size": frame_size, "px": FRAME_PX}

if MODE == "prepare":
    markers = estimate_markers()
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    sh = scene.display.shading
    sh.light, sh.color_type, sh.show_cavity = "STUDIO", "VERTEX", True
    scene.world = bpy.data.worlds.new("w")
    scene.world.color = (0.12, 0.12, 0.14)
    scene.render.resolution_x = scene.render.resolution_y = FRAME_PX
    cam_data = bpy.data.cameras.new("cam")
    cam_data.type, cam_data.ortho_scale = "ORTHO", frame_size
    cam = bpy.data.objects.new("cam", cam_data)
    scene.collection.objects.link(cam)
    cam.location = (0, -H * 4, H / 2)
    cam.rotation_euler = (math.pi / 2, 0, 0)
    scene.camera = cam
    scene.render.filepath = str((OUT / "front.png").resolve())
    bpy.ops.render.render(write_still=True)
    (OUT / "prep.json").write_text(json.dumps({
        "height_m": H, "markers": markers, "frame": frame, "warnings": report["warnings"],
    }, indent=2))
    print("[autorig] PREPARE OK", json.dumps(markers))
    raise SystemExit(0)

# ---------------------------------------------------------------- 4. sendi dari penanda
mk = estimate_markers()
if MARKERS_FILE:
    given = json.loads(Path(MARKERS_FILE).read_text())
    given = given.get("markers", given)
    for k in MARKER_KEYS:
        if k in given:
            mk[k] = [float(given[k][0]), float(given[k][1])]
report["markers"] = mk

chin_z, groin_z = mk["chin"][1], mk["groin"][1]
torso = chin_z - groin_z
if torso <= 0.1 * H:
    raise SystemExit("Penanda dagu harus jauh di atas selangkangan")
neck_z = chin_z - 0.075 * torso
shoulder_z = chin_z - 0.16 * torso
hip_z = groin_z + 0.05 * torso
spine_z = [groin_z + f * torso for f in (0.25, 0.50, 0.72)]
top = H
cy = depth_at(0, spine_z[1], 0.06)


def J(x, z, r=0.035):
    return Vector((x, depth_at(x, z, r), z))


chest_half = width_at(spine_z[2]) / 2 or 0.12 * H
arms, legs = {}, {}
for s, sign in (("l", 1), ("r", -1)):
    el, wr = np.array(mk[f"elbow_{s}"]), np.array(mk[f"wrist_{s}"])
    sh_x = sign * min(chest_half * 0.75, abs(el[0]) * 0.95 if abs(el[0]) > 0.04 * H else chest_half * 0.75)
    sh_x = sign * max(abs(sh_x), 0.05 * H)
    tip = wr + (wr - el) * 0.45
    arms[s] = {
        "shoulder": J(sh_x, shoulder_z, 0.05),
        "elbow": J(*el),
        "wrist": J(*wr),
        "tip": J(*tip, 0.025),
    }
    kn = np.array(mk[f"knee_{s}"])
    hip_x = sign * max(abs(kn[0]) * 0.9, 0.04 * H)
    ankle_z = 0.055 * H
    ankle_x = kn[0] + (kn[0] - hip_x) * ((kn[1] - ankle_z) / max(hip_z - kn[1], 1e-3)) * 0.3
    ankle = J(ankle_x, ankle_z, 0.03)
    foot = V[(V[:, 2] < 0.05 * H) & (np.abs(V[:, 0] - ankle_x) < 0.07 * H)]
    toe_y = float(foot[:, 1].min()) if len(foot) else ankle.y - 0.1 * H
    toe_end = Vector((ankle_x, toe_y, 0.02 * H))
    toe = ankle.lerp(toe_end, 0.65)
    toe.z = 0.025 * H
    knee = J(*kn)
    knee.y -= 0.01 * H  # sedikit ke depan agar lutut menekuk ke arah yang benar
    legs[s] = {"hip": Vector((hip_x, cy, hip_z)), "knee": knee, "ankle": ankle, "toe": toe, "toe_end": toe_end}
for s in arms:
    arms[s]["elbow"].y += 0.01 * H  # siku sedikit ke belakang

# Pisahkan kaki yang menempel: face di bawah selangkangan yang melintasi garis tengah (x=0) akan tertarik
# jadi lembaran saat melangkah, jadi dipotong lalu lubangnya ditutup. Kaki yang sudah terpisah tidak terpengaruh.
cut_z = groin_z - 0.025 * H
bm = bmesh.new()
bm.from_mesh(body.data)
cross = [f for f in bm.faces
         if max(v.co.z for v in f.verts) < cut_z
         and min(v.co.x for v in f.verts) < 0 < max(v.co.x for v in f.verts)]
if cross:
    bmesh.ops.delete(bm, geom=cross, context="FACES_ONLY")
    loose = [v for v in bm.verts if not v.link_faces]
    bmesh.ops.delete(bm, geom=loose, context="VERTS")
    boundary = [e for e in bm.edges if e.is_boundary and max(v.co.z for v in e.verts) < cut_z + 0.01 * H]
    filled = bmesh.ops.holes_fill(bm, edges=boundary, sides=0)
    bmesh.ops.triangulate(bm, faces=filled["faces"])
    warn(f"Kaki menempel: {len(cross)} face di antara kaki dipotong agar bisa melangkah") if len(cross) > 200 else None
    report["legs_split_faces"] = len(cross)
bm.to_mesh(body.data)
bm.free()
body.data.update()
V = np.array([v.co[:] for v in body.data.vertices])

# ---------------------------------------------------------------- 5. armature
arm_data = bpy.data.armatures.new("Armature")
rig = bpy.data.objects.new("Armature", arm_data)
bpy.context.scene.collection.objects.link(rig)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode="EDIT")
eb = arm_data.edit_bones


def bone(name, head, tail, parent=None, connect=False):
    b = eb.new(name)
    b.head, b.tail = Vector(head), Vector(tail)
    if parent:
        b.parent = eb[parent]
        b.use_connect = connect
    return b


head_y = depth_at(0, (chin_z + top) / 2, 0.05)
P = lambda z, y=cy: Vector((0, y, z))
bone("Hips", P(hip_z), P(spine_z[0]))
bone("Spine", P(spine_z[0]), P(spine_z[1]), "Hips", True)
bone("Spine1", P(spine_z[1]), P(spine_z[2]), "Spine", True)
bone("Spine2", P(spine_z[2]), P(neck_z), "Spine1", True)
bone("Neck", P(neck_z), P(chin_z, head_y), "Spine2", True)
bone("Head", P(chin_z, head_y), P(top, head_y), "Neck", True)
for s, name in (("l", "Left"), ("r", "Right")):
    a, l = arms[s], legs[s]
    sign = 1 if s == "l" else -1
    clav = Vector((sign * 0.025 * H, a["shoulder"].y, shoulder_z + 0.01 * H))
    bone(f"{name}Shoulder", clav, a["shoulder"], "Spine2")
    bone(f"{name}Arm", a["shoulder"], a["elbow"], f"{name}Shoulder", True)
    bone(f"{name}ForeArm", a["elbow"], a["wrist"], f"{name}Arm", True)
    bone(f"{name}Hand", a["wrist"], a["tip"], f"{name}ForeArm", True)
    bone(f"{name}UpLeg", l["hip"], l["knee"], "Hips")
    bone(f"{name}Leg", l["knee"], l["ankle"], f"{name}UpLeg", True)
    bone(f"{name}Foot", l["ankle"], l["toe"], f"{name}Leg", True)
    bone(f"{name}ToeBase", l["toe"], l["toe_end"], f"{name}Foot", True)
for b in eb:
    b.align_roll(Vector((0, -1, 0)) if abs(b.vector.normalized().y) < 0.9 else Vector((0, 0, 1)))
bpy.ops.object.mode_set(mode="OBJECT")

# ---------------------------------------------------------------- 6. skinning
bpy.ops.object.select_all(action="DESELECT")
body.select_set(True)
rig.select_set(True)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.parent_set(type="ARMATURE_AUTO")

bone_names = [b.name for b in arm_data.bones]
segs = {b.name: (np.array(b.head_local[:]), np.array(b.tail_local[:])) for b in arm_data.bones}


def seg_dist(p, a, b):
    ab = b - a
    t = np.clip(((p - a) @ ab) / max(ab @ ab, 1e-9), 0, 1)
    return np.linalg.norm(p - (a[None] + t[:, None] * ab[None]), axis=1)


# Filter sisi: bone heat sering mencampur kaki kiri-kanan bila kaki/sepatu saling menempel,
# sehingga mesh di antara kaki "tertarik" saat melangkah. Vertex di x>0 tidak boleh ikut tulang Right*, dst.
left_idx = np.where(V[:, 0] > 0)[0].tolist()
right_idx = np.where(V[:, 0] < 0)[0].tolist()
side_cleaned = 0
for vg in body.vertex_groups:
    if vg.name.startswith("Right") and left_idx:
        vg.remove(left_idx)
    elif vg.name.startswith("Left") and right_idx:
        vg.remove(right_idx)
# Normalisasi ulang bobot yang tersisa.
for v in body.data.vertices:
    s = sum(g.weight for g in v.groups)
    if 0 < s < 0.999:
        side_cleaned += 1
        for g in v.groups:
            g.weight /= s
report["side_renormalized"] = side_cleaned

groups = {vg.index: vg.name for vg in body.vertex_groups}
total = np.zeros(len(V))
for v in body.data.vertices:
    total[v.index] = sum(g.weight for g in v.groups if groups.get(g.group) in segs)
missing = np.where(total < 1e-3)[0]
if len(missing):
    pct = 100 * len(missing) / len(V)
    msg = f"{len(missing)} vertex ({pct:.1f}%) tanpa bobot -> bobot dari tulang terdekat"
    warn(msg) if pct > 5 else print("[autorig]", msg)
    usable = [n for n in bone_names if "ToeBase" not in n]
    D = np.stack([seg_dist(V[missing], *segs[n]) for n in usable], axis=1)
    for j, n in enumerate(usable):  # vertex sisi kiri tidak boleh ikut tulang kanan, dan sebaliknya
        if n.startswith("Left"):
            D[V[missing, 0] < -0.01 * H, j] = np.inf
        elif n.startswith("Right"):
            D[V[missing, 0] > 0.01 * H, j] = np.inf
    near = np.argsort(D, axis=1)[:, :2]
    for row, vi in enumerate(missing):
        d = D[row, near[row]] + 1e-4
        w = (1 / d**2) / (1 / d**2).sum()
        for j, wt in zip(near[row], w):
            vg = body.vertex_groups.get(usable[j]) or body.vertex_groups.new(name=usable[j])
            vg.add([int(vi)], float(wt), "REPLACE")
report["unweighted_fixed"] = int(len(missing))

# ---------------------------------------------------------------- 7. animasi
pb = rig.pose.bones
for p in pb:
    p.rotation_mode = "QUATERNION"
rest_q = {b.name: b.matrix_local.to_quaternion() for b in arm_data.bones}
D2R = math.pi / 180
AX = {"x": Vector((1, 0, 0)), "y": Vector((0, 1, 0)), "z": Vector((0, 0, 1))}


def arm_space(*rots):
    """rots: (sumbu, derajat) dalam ruang armature, diterapkan berurutan."""
    q = Quaternion()
    for ax, deg in rots:
        q = Quaternion(AX[ax], deg * D2R) @ q
    return q


def to_local(name, q_arm):
    r = rest_q[name]
    return r.inverted() @ q_arm @ r


def arm_target(side, target_deg):
    """Rotasi sumbu Y armature yang membawa lengan ke sudut target (−90 = lurus ke bawah, 0 = datar, 90 = ke atas)."""
    b = arm_data.bones[("Left" if side == "l" else "Right") + "Arm"]
    v = b.tail_local - b.head_local
    delta = math.degrees(math.atan2(v.z, abs(v.x))) - target_deg  # positif = perlu turun
    return delta if side == "l" else -delta


def relaxed_arms(extra_l=(), extra_r=(), down=-72):
    return {
        "LeftArm": [("y", arm_target("l", down)), *extra_l],
        "RightArm": [("y", arm_target("r", down)), *extra_r],
    }


# Konvensi sumbu X armature: sudut positif memutar bagian bawah tulang ke belakang (+Y),
# jadi paha maju = negatif, lutut menekuk = positif, siku menekuk = negatif, badan condong ke depan = positif.
def pose_idle(t):
    p = 2 * math.pi * t / 2.4
    b = math.sin(p)
    r = relaxed_arms([("x", 2 * b)], [("x", -2 * b)])
    r.update({
        "Spine1": [("x", 1.5 * b)], "Spine2": [("x", 1.5 * b)],
        "Neck": [("x", -1 * b)], "Head": [("z", 3 * math.sin(p * 0.5))],
        "LeftForeArm": [("x", -8)], "RightForeArm": [("x", -8)],
    })
    return r, Vector((0, 0, 0.003 * H * b))


def pose_walk(t, run=False):
    T = 0.7 if run else 1.1
    p = 2 * math.pi * t / T
    s, c = math.sin(p), math.cos(p)
    leg = 38 if run else 26
    armsw = 35 if run else 20
    r = relaxed_arms([("x", armsw * s)], [("x", -armsw * s)], down=-65 if run else -72)
    knee_l = (20 if run else 5) + (75 if run else 45) * max(0.0, c) ** 1.4
    knee_r = (20 if run else 5) + (75 if run else 45) * max(0.0, -c) ** 1.4
    elbow = -85 if run else -12
    r.update({
        "LeftUpLeg": [("x", -leg * s)], "RightUpLeg": [("x", leg * s)],
        "LeftLeg": [("x", knee_l)], "RightLeg": [("x", knee_r)],
        "LeftFoot": [("x", -0.3 * knee_l + 8 * s)], "RightFoot": [("x", -0.3 * knee_r - 8 * s)],
        "LeftForeArm": [("x", elbow)], "RightForeArm": [("x", elbow)],
        "Spine": [("x", 10 if run else 2), ("z", -6 * s)],
        "Spine2": [("z", 8 * s)],
        "Head": [("z", -2 * s), ("x", -6 if run else 0)],
    })
    bob = (0.035 if run else 0.015) * H * abs(math.cos(p))
    return r, Vector((0, 0, bob - (0.03 if run else 0.01) * H))


def pose_wave(t):
    p = 2 * math.pi * t / 1.6
    r = relaxed_arms()
    r["RightArm"] = [("y", arm_target("r", 55))]
    r.update({
        "RightForeArm": [("y", -70 + 25 * math.sin(p * 2))],
        "LeftForeArm": [("x", -8)],
        "Spine2": [("z", 3 * math.sin(p))], "Head": [("z", 6), ("y", -4)],
    })
    return r, Vector()


def pose_jump(t):
    u = (t % 1.4) / 1.4

    def ease(a, b, x):
        x = min(max((x - a) / (b - a), 0), 1)
        return x * x * (3 - 2 * x)

    crouch = ease(0.0, 0.25, u) * (1 - ease(0.25, 0.35, u)) + ease(0.72, 0.8, u) * (1 - ease(0.8, 1.0, u))
    air = ease(0.3, 0.5, u) * (1 - ease(0.5, 0.75, u))
    up = math.sin(math.pi * min(max((u - 0.32) / 0.42, 0), 1))
    r = relaxed_arms([("x", -60 * air + 25 * crouch)], [("x", -60 * air + 25 * crouch)], down=-72 + 40 * air)
    r.update({
        "LeftUpLeg": [("x", -55 * crouch - 15 * air)], "RightUpLeg": [("x", -55 * crouch - 15 * air)],
        "LeftLeg": [("x", 100 * crouch + 25 * air)], "RightLeg": [("x", 100 * crouch + 25 * air)],
        "LeftFoot": [("x", -45 * crouch + 20 * air)], "RightFoot": [("x", -45 * crouch + 20 * air)],
        "Spine": [("x", 20 * crouch)], "Head": [("x", -10 * crouch)],
    })
    return r, Vector((0, 0, -0.18 * H * crouch + 0.28 * H * up))


LIB = {
    "idle": (pose_idle, 2.4, True),
    "walk": (pose_walk, 1.1, True),
    "run": (lambda t: pose_walk(t, run=True), 0.7, True),
    "wave": (pose_wave, 1.6, True),
    "jump": (pose_jump, 1.4, False),
}

rig.animation_data_create()
hips_rest = arm_data.bones["Hips"].matrix_local.to_3x3()
made = []
for anim in ANIMS:
    if anim not in LIB:
        warn(f"Animasi tidak dikenal: {anim}")
        continue
    fn, dur, loop = LIB[anim]
    act = bpy.data.actions.new(anim)
    act.use_fake_user = True
    rig.animation_data.action = act
    n = round(dur * FPS)
    for f in range(0, n + 1):
        rots, hips_off = fn(f / FPS)  # fungsi pose periodik, jadi frame terakhir = frame pertama
        for p in pb:
            p.rotation_quaternion = to_local(p.name, arm_space(*rots.get(p.name, [])))
            p.keyframe_insert("rotation_quaternion", frame=f, group=p.name)
        pb["Hips"].location = hips_rest.inverted() @ hips_off
        pb["Hips"].keyframe_insert("location", frame=f, group="Hips")
    act.frame_range = (0, n)
    track = rig.animation_data.nla_tracks.new()
    track.name = anim
    track.strips.new(anim, 0, act)
    rig.animation_data.action = None
    made.append({"name": anim, "frames": n, "seconds": round(n / FPS, 2), "loop": loop})

for p in pb:
    p.rotation_quaternion = Quaternion()
    p.location = Vector()
bpy.context.scene.render.fps = FPS

# ---------------------------------------------------------------- 8. ekspor
bpy.ops.object.select_all(action="SELECT")
bpy.ops.export_scene.gltf(
    filepath=str((OUT / "rigged.glb").resolve()),
    export_format="GLB",
    export_animations=True,
    export_animation_mode="ACTIONS",
    export_vertex_color="ACTIVE",
    export_skins=True,
    export_force_sampling=True,
    export_optimize_animation_size=True,
)
try:
    bpy.ops.export_scene.fbx(
        filepath=str((OUT / "rigged.fbx").resolve()),
        add_leaf_bones=False,
        bake_anim=True,
        bake_anim_use_all_actions=True,
        bake_anim_use_nla_strips=False,
        path_mode="COPY",
    )
except Exception as e:  # FBX opsional
    warn(f"Ekspor FBX gagal: {e}")


def vec(v):
    return [round(x, 4) for x in v]


report.update({
    "height_m": HEIGHT,
    "joints": {
        "arms": {s: {k: vec(v) for k, v in a.items()} for s, a in arms.items()},
        "legs": {s: {k: vec(v) for k, v in l.items()} for s, l in legs.items()},
    },
    "bones": bone_names,
    "animations": made,
    "vertices": len(V),
})
(OUT / "rig.json").write_text(json.dumps(report, indent=2))
print("[autorig] OK", json.dumps({"animations": [m["name"] for m in made], "warnings": report["warnings"]}))
