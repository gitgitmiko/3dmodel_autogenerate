"""Tulis BVH uji standar (Y ke atas, satuan cm, pose T, karakter menghadap +Z) seperti file Mixamo/CMU.

Frame 0: lengan lurus ke bawah, frame 15: lengan ke atas + paha kiri maju, frame 30: kembali.
Pakai: python3 scripts/make_test_bvh.py out.bvh
"""

import sys

# nama, offset (x, y, z) relatif induk, anak
SK = ("Hips", (0, 100, 0), [
    ("Spine", (0, 10, 0), [("Spine1", (0, 15, 0), [("Spine2", (0, 13, 0), [
        ("Neck", (0, 12, 0), [("Head", (0, 10, 0), [])]),
        ("LeftShoulder", (5, 9, 0), [("LeftArm", (13, 0, 0), [("LeftForeArm", (28, 0, 0), [("LeftHand", (26, 0, 0), [])])])]),
        ("RightShoulder", (-5, 9, 0), [("RightArm", (-13, 0, 0), [("RightForeArm", (-28, 0, 0), [("RightHand", (-26, 0, 0), [])])])]),
    ])])]),
    ("LeftUpLeg", (10, -2, 0), [("LeftLeg", (0, -43, 0), [("LeftFoot", (0, -45, 0), [("LeftToeBase", (0, -8, 12), [])])])]),
    ("RightUpLeg", (-10, -2, 0), [("RightLeg", (0, -43, 0), [("RightFoot", (0, -45, 0), [("RightToeBase", (0, -8, 12), [])])])]),
])

order = []


def write(node, depth, out):
    name, off, kids = node
    pad = "  " * depth
    out.append(f"{pad}{'ROOT' if depth == 0 else 'JOINT'} {name}")
    out.append(f"{pad}{{")
    out.append(f"{pad}  OFFSET {off[0]:.2f} {off[1]:.2f} {off[2]:.2f}")
    if depth == 0:
        out.append(f"{pad}  CHANNELS 6 Xposition Yposition Zposition Zrotation Xrotation Yrotation")
    else:
        out.append(f"{pad}  CHANNELS 3 Zrotation Xrotation Yrotation")
    order.append(name)
    if kids:
        for k in kids:
            write(k, depth + 1, out)
    else:
        out.append(f"{pad}  End Site")
        out.append(f"{pad}  {{")
        out.append(f"{pad}    OFFSET 0.00 5.00 0.00")
        out.append(f"{pad}  }}")
    out.append(f"{pad}}}")


lines = ["HIERARCHY"]
write(SK, 0, lines)
frames = []
for f in range(31):
    t = f / 15 if f <= 15 else (30 - f) / 15  # 0 -> 1 -> 0
    arm = -88 + 176 * t                       # sudut Z lengan kiri: -88 (bawah) .. +88 (atas)
    vals = []
    for n in order:
        if n == "Hips":
            vals += [0, 100, 0, 0, 0, 0]
        elif n == "LeftArm":
            vals += [arm, 0, 0]
        elif n == "RightArm":
            vals += [-arm, 0, 0]
        elif n == "LeftUpLeg":
            vals += [0, -35 * t, 0]  # rotasi X negatif: paha maju (+Z) untuk karakter menghadap +Z
        else:
            vals += [0, 0, 0]
    frames.append(" ".join(f"{v:.3f}" for v in vals))
lines += ["MOTION", f"Frames: {len(frames)}", "Frame Time: 0.0333333", *frames]
open(sys.argv[1], "w").write("\n".join(lines) + "\n")
print("OK", sys.argv[1])
