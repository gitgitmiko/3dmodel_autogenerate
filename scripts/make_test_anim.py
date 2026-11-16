"""Buat animasi uji retarget: rig pose-T bernama mixamorig:* (proporsi berbeda dari rig kita).

Frame 0: lengan lurus ke bawah, frame 15: lengan lurus ke atas, frame 30: ke bawah lagi;
kaki kiri melangkah maju di frame 15. Diekspor ke out_dir/test_anim.bvh dan test_anim.fbx.
Pakai: blender -b --factory-startup -P scripts/make_test_anim.py -- out_dir
"""

import math
import os
import sys

import bpy
from mathutils import Quaternion, Vector

out = os.path.abspath(sys.argv[sys.argv.index("--") + 1])
os.makedirs(out, exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
arm = bpy.data.armatures.new("A")
obj = bpy.data.objects.new("Armature", arm)
bpy.context.scene.collection.objects.link(obj)
bpy.context.view_layer.objects.active = obj
bpy.ops.object.mode_set(mode="EDIT")
P = "mixamorig:"
J = {  # pose T, satuan meter, Z ke atas, depan -Y
    "Hips": ((0, 0, 1.0), (0, 0, 1.1), None),
    "Spine": ((0, 0, 1.1), (0, 0, 1.25), "Hips"),
    "Spine1": ((0, 0, 1.25), (0, 0, 1.38), "Spine"),
    "Spine2": ((0, 0, 1.38), (0, 0, 1.5), "Spine1"),
    "Neck": ((0, 0, 1.5), (0, 0, 1.6), "Spine2"),
    "Head": ((0, 0, 1.6), (0, 0, 1.8), "Neck"),
}
for side, sx in (("Left", 1), ("Right", -1)):
    J.update({
        f"{side}Shoulder": ((sx * 0.05, 0, 1.47), (sx * 0.18, 0, 1.47), "Spine2"),
        f"{side}Arm": ((sx * 0.18, 0, 1.47), (sx * 0.46, 0, 1.47), f"{side}Shoulder"),
        f"{side}ForeArm": ((sx * 0.46, 0, 1.47), (sx * 0.72, 0, 1.47), f"{side}Arm"),
        f"{side}Hand": ((sx * 0.72, 0, 1.47), (sx * 0.82, 0, 1.47), f"{side}ForeArm"),
        f"{side}UpLeg": ((sx * 0.1, 0, 0.98), (sx * 0.1, 0, 0.55), "Hips"),
        f"{side}Leg": ((sx * 0.1, 0, 0.55), (sx * 0.1, 0, 0.1), f"{side}UpLeg"),
        f"{side}Foot": ((sx * 0.1, 0, 0.1), (sx * 0.1, -0.12, 0.02), f"{side}Leg"),
        f"{side}ToeBase": ((sx * 0.1, -0.12, 0.02), (sx * 0.1, -0.2, 0.02), f"{side}Foot"),
    })
for n, (h, t, par) in J.items():
    b = arm.edit_bones.new(P + n)
    b.head, b.tail = Vector(h), Vector(t)
    if par:
        b.parent = arm.edit_bones[P + par]
        b.use_connect = (Vector(arm.edit_bones[P + par].tail) - Vector(h)).length < 1e-4
bpy.ops.object.mode_set(mode="POSE")
rest = {b.name: b.matrix_local.to_quaternion() for b in arm.bones}


def set_rot(name, axis, deg, frame):
    pbn = obj.pose.bones[P + name]
    pbn.rotation_mode = "QUATERNION"
    r = rest[P + name]
    pbn.rotation_quaternion = r.inverted() @ Quaternion(Vector(axis), math.radians(deg)) @ r
    pbn.keyframe_insert("rotation_quaternion", frame=frame)


for frame, arms_deg, leg in ((0, -88, 0), (15, 88, -35), (30, -88, 0)):
    set_rot("LeftArm", (0, 1, 0), -arms_deg, frame)   # sumbu Y: positif menurunkan lengan kiri
    set_rot("RightArm", (0, 1, 0), arms_deg, frame)
    set_rot("LeftUpLeg", (1, 0, 0), leg, frame)        # negatif = paha maju
    obj.pose.bones[P + "Hips"].location = (0, 0, 0)
    obj.pose.bones[P + "Hips"].keyframe_insert("location", frame=frame)
bpy.context.scene.frame_start, bpy.context.scene.frame_end = 0, 30
bpy.context.scene.render.fps = 30
bpy.ops.object.mode_set(mode="OBJECT")
obj.select_set(True)
bpy.ops.export_anim.bvh(filepath=os.path.join(out, "test_anim.bvh"), frame_start=0, frame_end=30, rotate_mode="NATIVE")
bpy.ops.export_scene.fbx(filepath=os.path.join(out, "test_anim.fbx"), object_types={"ARMATURE"}, add_leaf_bones=False,
                         bake_anim=True, bake_anim_use_all_actions=True, bake_anim_use_nla_strips=False,
                         bake_anim_force_startend_keying=True)
print("OK", out)
