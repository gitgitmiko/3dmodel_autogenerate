"""Buat objek uji multi-view dan render 4 "foto" (depan, belakang, kiri, kanan) dengan Cycles CPU.

Pakai: blender -b --factory-startup -P scripts/make_test_views.py -- out_dir
Warna: depan kuning + mata hitam, belakang biru + kotak merah di KIRI-ATAS punggung (sisi kiri karakter),
samping kiri hijau, samping kanan oranye. Dipakai untuk memeriksa arah & cermin proyeksi multi-view.
"""

import math
import os
import sys

import bpy
from mathutils import Vector

out = os.path.abspath(sys.argv[sys.argv.index("--") + 1])
os.makedirs(out, exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)

# --- bentuk: badan kapsul + kepala + lengan + kaki
parts = []
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.45, location=(0, 0, 0.95), segments=48, ring_count=24)
body = bpy.context.active_object
body.scale = (1.0, 0.7, 1.25)
parts.append(body)
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.33, location=(0, 0, 1.75), segments=48, ring_count=24)
parts.append(bpy.context.active_object)
for sx in (-1, 1):
    bpy.ops.mesh.primitive_cylinder_add(radius=0.11, depth=0.75, location=(sx * 0.55, 0, 1.05), rotation=(0, sx * 0.35, 0))
    parts.append(bpy.context.active_object)
    bpy.ops.mesh.primitive_cylinder_add(radius=0.14, depth=0.6, location=(sx * 0.2, 0, 0.3))
    parts.append(bpy.context.active_object)
for p in parts:
    p.select_set(True)
bpy.context.view_layer.objects.active = body
bpy.ops.object.join()
obj = bpy.context.active_object
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
mod = obj.modifiers.new("remesh", "REMESH")
mod.mode, mod.voxel_size = "VOXEL", 0.02
bpy.ops.object.modifier_apply(modifier="remesh")
bpy.ops.object.shade_smooth()

# --- material prosedural berdasarkan arah normal & posisi (koordinat objek)
mat = bpy.data.materials.new("m")
mat.use_nodes = True
nt = mat.node_tree
N, L = nt.nodes, nt.links
bsdf = N["Principled BSDF"]
geo = N.new("ShaderNodeNewGeometry")
tc = N.new("ShaderNodeTexCoord")
sep_n = N.new("ShaderNodeSeparateXYZ")
L.new(geo.outputs["Normal"], sep_n.inputs[0])
sep_p = N.new("ShaderNodeSeparateXYZ")
L.new(tc.outputs["Object"], sep_p.inputs[0])


def mix(fac_socket, a, b):
    m = N.new("ShaderNodeMix")
    m.data_type = "RGBA"
    L.new(fac_socket, m.inputs["Factor"])
    if isinstance(a, tuple):
        m.inputs["A"].default_value = (*a, 1)
    else:
        L.new(a, m.inputs["A"])
    if isinstance(b, tuple):
        m.inputs["B"].default_value = (*b, 1)
    else:
        L.new(b, m.inputs["B"])
    return m.outputs["Result"]


def step(sock, edge, invert=False):
    m = N.new("ShaderNodeMath")
    m.operation = "LESS_THAN" if invert else "GREATER_THAN"
    L.new(sock, m.inputs[0])
    m.inputs[1].default_value = edge
    return m.outputs[0]


def mul(a, b):
    m = N.new("ShaderNodeMath")
    m.operation = "MULTIPLY"
    L.new(a, m.inputs[0])
    L.new(b, m.inputs[1])
    return m.outputs[0]


# depan Blender = -Y. Kiri karakter = +X.
c = mix(step(sep_n.outputs["X"], 0.5), (0.85, 0.35, 0.05), (0.1, 0.7, 0.2))      # samping: oranye(kanan) / hijau(kiri)
c = mix(step(sep_n.outputs["X"], -0.5, True), c, (0.85, 0.35, 0.05))
c = mix(step(sep_n.outputs["X"], 0.5), c, (0.1, 0.7, 0.2))
c = mix(step(sep_n.outputs["Y"], 0.45), c, (0.1, 0.25, 0.85))                     # belakang biru
c = mix(step(sep_n.outputs["Y"], -0.45, True), c, (0.95, 0.8, 0.1))               # depan kuning
# kotak merah di punggung kiri-atas: y>0 (belakang), x in (0.08, 0.32), z in (1.05, 1.35)
red = mul(mul(step(sep_p.outputs["Y"], 0.1), mul(step(sep_p.outputs["X"], 0.08), step(sep_p.outputs["X"], 0.32, True))),
          mul(step(sep_p.outputs["Z"], 1.05), step(sep_p.outputs["Z"], 1.35, True)))
c = mix(red, c, (0.9, 0.05, 0.05))
# mata: dua bulatan hitam di wajah (depan kepala)
for ex in (-0.12, 0.12):
    v = N.new("ShaderNodeVectorMath")
    v.operation = "DISTANCE"
    L.new(tc.outputs["Object"], v.inputs[0])
    v.inputs[1].default_value = (ex, -0.31, 1.8)
    c = mix(step(v.outputs["Value"], 0.07, True), c, (0.02, 0.02, 0.02))
L.new(c, bsdf.inputs["Base Color"])
bsdf.inputs["Roughness"].default_value = 0.6
obj.data.materials.append(mat)

# --- render
scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.cycles.samples = 24
scene.render.resolution_x = scene.render.resolution_y = 512
scene.render.film_transparent = False
scene.world = bpy.data.worlds.new("w")
scene.world.use_nodes = True
scene.world.node_tree.nodes["Background"].inputs[0].default_value = (0.9, 0.9, 0.9, 1)
scene.world.node_tree.nodes["Background"].inputs[1].default_value = 1.2
sun = bpy.data.objects.new("sun", bpy.data.lights.new("sun", "SUN"))
sun.data.energy = 2.5
sun.rotation_euler = (0.6, 0.3, 0.4)
scene.collection.objects.link(sun)
cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
cam.data.angle = math.radians(40)
scene.collection.objects.link(cam)
scene.camera = cam
center = Vector((0, 0, 1.05))
for name, d in {"front": (0, -1, 0), "back": (0, 1, 0), "left": (1, 0, 0), "right": (-1, 0, 0)}.items():
    cam.location = center + Vector(d) * 3.6
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
    scene.render.filepath = os.path.join(out, f"{name}.png")
    bpy.ops.render.render(write_still=True)
print("OK", out)
