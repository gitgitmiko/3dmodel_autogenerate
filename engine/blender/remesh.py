"""Remesh + transfer tekstur, dijalankan di Blender headless.

Pakai: blender -b --factory-startup -P remesh.py -- in.glb out_dir mode target_faces tex_res
  mode: quad (QuadriFlow, topologi quad rapi) | tri (decimate, segitiga)
Hasil: out_dir/model.glb (+ model.fbx, model.obj/.mtl/texture.png, remesh.json)

Tekstur model asli di-bake ke mesh baru dengan Cycles "selected to active" (warna dasar saja),
sehingga mesh baru punya UV & tekstur sendiri yang bersih.
"""

import json
import math
import sys
import time
from pathlib import Path

import bpy

argv = sys.argv[sys.argv.index("--") + 1:]
SRC, OUT, MODE = argv[0], Path(argv[1]).resolve(), argv[2]
TARGET = int(argv[3]) if len(argv) > 3 else 8000
TEX = int(argv[4]) if len(argv) > 4 else 1024
OUT.mkdir(parents=True, exist_ok=True)
t0 = time.time()
report = {"mode": MODE, "target_faces": TARGET, "warnings": []}

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=str(Path(SRC).resolve()))
meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
for o in bpy.context.scene.objects:
    o.select_set(o.type == "MESH")
bpy.context.view_layer.objects.active = meshes[0]
if len(meshes) > 1:
    bpy.ops.object.join()
src = bpy.context.view_layer.objects.active
mw = src.matrix_world.copy()
src.parent = None
src.matrix_world = mw
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
for o in list(bpy.context.scene.objects):
    if o is not src:
        bpy.data.objects.remove(o, do_unlink=True)
src.name = "Source"
report["source_faces"] = len(src.data.polygons)

# Sumber tanpa tekstur (vertex color): pastikan materialnya memakai color attribute agar ikut ter-bake.
has_tex = any(n.type == "TEX_IMAGE" for m in src.data.materials if m and m.node_tree for n in m.node_tree.nodes)
if not has_tex and src.data.color_attributes:
    mat = bpy.data.materials.new("SrcVC")
    mat.use_nodes = True
    nt = mat.node_tree
    vc = nt.nodes.new("ShaderNodeVertexColor")
    vc.layer_name = src.data.color_attributes[0].name
    nt.links.new(vc.outputs["Color"], nt.nodes["Principled BSDF"].inputs["Base Color"])
    src.data.materials.clear()
    src.data.materials.append(mat)

dims = src.dimensions
size = max(dims)

# ---------------------------------------------------------------- mesh baru
bpy.ops.object.select_all(action="DESELECT")
src.select_set(True)
bpy.context.view_layer.objects.active = src
bpy.ops.object.duplicate()
new = bpy.context.view_layer.objects.active
new.name = "Remeshed"
new.data.materials.clear()
for attr in list(new.data.color_attributes):
    new.data.color_attributes.remove(attr)
for uv in list(new.data.uv_layers):
    new.data.uv_layers.remove(uv)

if MODE == "quad":
    # Vertex terpecah di sambungan UV membuat mesh "terbuka"; satukan dulu, lalu voxel remesh
    # (hasil TripoSR + decimation sering tidak manifold, padahal QuadriFlow butuh manifold).
    import bmesh

    bm = bmesh.new()
    bm.from_mesh(new.data)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=size * 1e-5)
    bm.to_mesh(new.data)
    bm.free()
    vox = new.modifiers.new("vox", "REMESH")
    vox.mode = "VOXEL"
    vox.voxel_size = size / 220
    vox.adaptivity = 0.0
    bpy.ops.object.modifier_apply(modifier="vox")
    # Pemeriksaan QuadriFlow di Blender 5.x menolak keluaran voxel mentah (face sejajar grid);
    # smooth ringan 2 iterasi membuatnya diterima tanpa mengubah bentuk berarti.
    sm = new.modifiers.new("sm", "SMOOTH")
    sm.iterations, sm.factor = 2, 0.5
    bpy.ops.object.modifier_apply(modifier="sm")
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    ok = False
    for attempt in range(2):
        try:
            res = bpy.ops.object.quadriflow_remesh(target_faces=TARGET, use_preserve_sharp=False,
                                                   use_preserve_boundary=False, smooth_normals=True,
                                                   use_mesh_symmetry=False, seed=attempt)
            ok = "FINISHED" in res and len(new.data.polygons) > 0
        except RuntimeError as e:
            report["warnings"].append(f"QuadriFlow percobaan {attempt + 1} gagal: {e}")
        if ok:
            break
    if not ok:
        raise SystemExit("QuadriFlow gagal membuat mesh quad")
else:
    cur = len(new.data.polygons)
    if TARGET < cur:
        dec = new.modifiers.new("dec", "DECIMATE")
        dec.decimate_type = "COLLAPSE"
        dec.ratio = TARGET / cur
        bpy.ops.object.modifier_apply(modifier="dec")

bpy.ops.object.shade_smooth()
polys = new.data.polygons
report["faces"] = len(polys)
report["quads"] = sum(1 for p in polys if p.loop_total == 4)
report["vertices"] = len(new.data.vertices)

# ---------------------------------------------------------------- UV
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.select_all(action="SELECT")
bpy.ops.uv.smart_project(angle_limit=math.radians(66), island_margin=0.004, area_weight=0.0)
bpy.ops.object.mode_set(mode="OBJECT")

# ---------------------------------------------------------------- bake warna dari sumber
img = bpy.data.images.new("texture", TEX, TEX, alpha=False)
mat = bpy.data.materials.new("RemeshMat")
mat.use_nodes = True
nt = mat.node_tree
tex = nt.nodes.new("ShaderNodeTexImage")
tex.image = img
nt.links.new(tex.outputs["Color"], nt.nodes["Principled BSDF"].inputs["Base Color"])
nt.nodes["Principled BSDF"].inputs["Roughness"].default_value = 0.85
nt.nodes.active = tex  # target bake
new.data.materials.append(mat)

scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.cycles.samples = 1
scene.render.bake.use_selected_to_active = True
scene.render.bake.cage_extrusion = size * 0.015
scene.render.bake.max_ray_distance = size * 0.04
scene.render.bake.margin = max(4, TEX // 128)
bpy.ops.object.select_all(action="DESELECT")
src.select_set(True)
new.select_set(True)
bpy.context.view_layer.objects.active = new
tb = time.time()
bpy.ops.object.bake(type="DIFFUSE", pass_filter={"COLOR"}, use_selected_to_active=True)
report["bake_seconds"] = round(time.time() - tb, 1)
img.filepath_raw = str(OUT / "texture.png")
img.file_format = "PNG"
img.save()

bpy.data.objects.remove(src, do_unlink=True)
scene.render.engine = "BLENDER_EEVEE" if "BLENDER_EEVEE" in {e.identifier for e in
                                                            bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items} else scene.render.engine

# ---------------------------------------------------------------- ekspor
bpy.ops.object.select_all(action="SELECT")
bpy.ops.export_scene.gltf(filepath=str(OUT / "model.glb"), export_format="GLB", export_vertex_color="NONE")
bpy.ops.export_scene.fbx(filepath=str(OUT / "model.fbx"), path_mode="COPY", embed_textures=True)
bpy.ops.wm.obj_export(filepath=str(OUT / "model.obj"), export_materials=True, path_mode="RELATIVE",
                      export_triangulated_mesh=False, export_uv=True, export_normals=True)
report["seconds"] = round(time.time() - t0, 1)
(OUT / "remesh.json").write_text(json.dumps(report, indent=2))
print("[remesh] OK", json.dumps(report))
