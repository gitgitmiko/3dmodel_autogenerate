"""Render pratinjau GLB dengan Workbench (warna dari tekstur, atau vertex color bila tanpa tekstur).

Pakai: blender -b --factory-startup -P render_preview.py -- model.glb out.png [--views front,side,34]
       [--anim walk --frame 8] [--joints] [--size 512]
Setiap view disimpan sebagai out_<view>.png (view pertama juga ke out.png).
"""

import argparse
import os
import sys

import bpy
from mathutils import Vector

ap = argparse.ArgumentParser()
ap.add_argument("src")
ap.add_argument("out")
ap.add_argument("--views", default="front,side,34")
ap.add_argument("--anim")
ap.add_argument("--frame", type=int, default=0)
ap.add_argument("--joints", action="store_true", help="tampilkan titik sendi armature")
ap.add_argument("--size", type=int, default=512)
ap.add_argument("--wire", action="store_true", help="tampilkan wireframe topologi")
args = ap.parse_args(sys.argv[sys.argv.index("--") + 1:])
args.src, args.out = os.path.abspath(args.src), os.path.abspath(args.out)

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=args.src)
scene = bpy.context.scene
rig = next((o for o in scene.objects if o.type == "ARMATURE"), None)
if rig and args.anim:
    act = bpy.data.actions.get(args.anim)
    if act is None:
        raise SystemExit(f"Animasi {args.anim} tidak ada. Tersedia: {[a.name for a in bpy.data.actions]}")
    rig.animation_data_create()
    if rig.animation_data.nla_tracks:
        for tr in rig.animation_data.nla_tracks:
            tr.mute = True
    rig.animation_data.action = act
    if hasattr(rig.animation_data, "action_suitable_slots") and rig.animation_data.action_suitable_slots:
        rig.animation_data.action_slot = rig.animation_data.action_suitable_slots[0]
scene.frame_set(args.frame)

meshes = [o for o in scene.objects if o.type == "MESH"]
deps = bpy.context.evaluated_depsgraph_get()
pts = []
for o in meshes:
    ev = o.evaluated_get(deps)
    m = ev.to_mesh()
    pts += [ev.matrix_world @ v.co for v in m.vertices]
    ev.to_mesh_clear()
lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
center = (lo + hi) / 2
size = max(hi - lo)

if rig and args.joints:
    mat = bpy.data.materials.new("joint")
    mat.diffuse_color = (1, 0.15, 0.1, 1)
    for pbone in rig.pose.bones:
        for p in (pbone.head, pbone.tail):
            bpy.ops.mesh.primitive_uv_sphere_add(radius=size * 0.012, location=rig.matrix_world @ p)
            s = bpy.context.active_object
            s.data.materials.append(mat)
            s.show_in_front = True

if args.wire:
    wmat = bpy.data.materials.new("wire")
    wmat.diffuse_color = (0.05, 0.05, 0.08, 1)
    for o in meshes:
        w = o.copy()
        w.data = o.data.copy()
        scene.collection.objects.link(w)
        mod = w.modifiers.new("w", "WIREFRAME")
        mod.thickness = size * 0.0015
        mod.use_replace = True
        w.data.materials.clear()
        w.data.materials.append(wmat)
        w.color = (0.05, 0.05, 0.08, 1)

scene.render.engine = "BLENDER_WORKBENCH"
shading = scene.display.shading
shading.light = "STUDIO"
textured = any(n.type == "TEX_IMAGE" for m in bpy.data.materials if m.node_tree for n in m.node_tree.nodes)
shading.color_type = "TEXTURE" if textured else "VERTEX"
shading.show_cavity = True
if args.joints:
    shading.show_xray = True
    shading.xray_alpha = 0.55
scene.world = bpy.data.worlds.new("w")
scene.world.color = (0.12, 0.12, 0.14)
scene.render.resolution_x = scene.render.resolution_y = args.size

DIRS = {"front": Vector((0, -1, 0)), "side": Vector((1, 0, 0)), "34": Vector((1, -1, 0.6)), "back": Vector((0, 1, 0)), "left": Vector((1, 0, 0)), "right": Vector((-1, 0, 0))}
base = args.out[:-4] if args.out.endswith(".png") else args.out
for i, view in enumerate(args.views.split(",")):
    cam_data = bpy.data.cameras.new("cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = size * 1.15
    cam = bpy.data.objects.new("cam", cam_data)
    scene.collection.objects.link(cam)
    d = DIRS[view].normalized()
    cam.location = center + d * size * 3
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
    scene.camera = cam
    for path in ([base + ".png"] if i == 0 else []) + [f"{base}_{view}.png"]:
        scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
