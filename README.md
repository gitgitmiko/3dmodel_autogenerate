# gitgit3d

Engine 3D generatif self-hosted ala Meshy, berjalan **tanpa GPU** di server rumah.
Live: **https://3d.gitgitmiko.my.id** (login password).

Tahap 1 (sudah jalan):

- **Image → 3D**: satu gambar menjadi model 3D (GLB / OBJ / STL).
- **Texture bake**: UV atlas + tekstur 1K/2K; sisi depan memakai gambar asli sehingga detailnya tajam.
- **Multi-view**: foto belakang / samping kiri / samping kanan (opsional) ikut ditempel ke tekstur,
  bisa ditambahkan saat unggah atau belakangan (rig diperbarui otomatis).
- **Text → 3D**: deskripsi teks → gambar oleh Google Flow (Nano Banana) lewat
  [gflow-cli](https://github.com/ffroliva/gflow-cli) → 3D bertekstur semua sisi.
- **Auto-rig humanoid**: kerangka 22 tulang (nama standar Mixamo), skinning otomatis, 9 animasi
  (*idle, walk, run, wave, jump, dance, attack, sit, cheer*) + **impor animasi Mixamo FBX / BVH**
  (di-retarget otomatis), diekspor ke GLB dan FBX (Unity/Unreal/Godot/Blender).
- **Auto-rig hewan berkaki empat**: tulang punggung, leher, kepala, ekor, 4 kaki; animasi *idle, walk, run, jump*.
- **Remesh**: topologi quad rapi (QuadriFlow) atau segitiga dengan target jumlah face, tekstur di-bake ulang.

## Cara kerja

```
gambar ──rembg──► potong background ──TripoSR (CPU)──► triplane NeRF ──marching cubes──► mesh
      ──bersihkan + Taubin smoothing + decimation──► UV unwrap (xatlas)
      ──bake tekstur: warna NeRF per texel + proyeksi gambar asli──► model.glb (+ OBJ zip, STL, thumbnail)

model.glb ──Blender headless (autorig.py prepare)──► tebakan 8 penanda sendi + gambar tampak depan
          ──pengguna cek/geser penanda di UI──► (autorig.py rig) kerangka + bone heat skinning
          ──animasi prosedural──► rigged.glb + rigged.fbx
```

| Bagian | Teknologi |
| --- | --- |
| Rekonstruksi 3D | [TripoSR](https://github.com/VAST-AI-Research/TripoSR) (MIT), di-vendor di `engine/tsr/`, `torchmcubes` diganti `PyMCubes` agar tanpa compile/CUDA |
| Hapus background | rembg (`isnet-general-use`) |
| Rigging & animasi | Blender 5.2 headless (`engine/blender/autorig.py`) |
| API & antrean | FastAPI + SQLite, satu worker (satu job per waktu) |
| UI | HTML/JS polos + `<model-viewer>` (di-vendor) |

Performa di i5-1135G7 (4 core, tanpa GPU): image→3D + tekstur 2K ±55–75 detik, RAM puncak ±3,8 GB;
rigging + 5 animasi ±3–5 detik. Model TripoSR dilepas dari RAM setelah 15 menit menganggur.

### Texture bake (`engine/texture.py`)

1. Mesh akhir di-unwrap dengan xatlas (±4–12 detik untuk 60 ribu face).
2. Segitiga dirasterisasi di ruang UV untuk mendapat posisi 3D setiap texel, lalu warnanya ditanyakan
   langsung ke medan warna TripoSR (jauh lebih rapat daripada vertex color).
3. **Proyeksi gambar asli**: kamera input TripoSR diketahui (sumbu +X, jarak 1,9, FOV 40°), jadi texel
   yang menghadap kamera dan tidak terhalang (uji depth buffer) diganti warna dari gambar asli resolusi
   penuh, dengan transisi halus ke warna NeRF di sisi samping/belakang. Dilewati otomatis bila siluet
   mesh tidak cocok dengan gambar (IoU < 0,6).
4. Texel kosong diisi warna tetangga terdekat agar tidak ada garis di sambungan UV.

Tanpa foto tambahan, sisi belakang/samping memakai warna tebakan NeRF (buram).

### Multi-view

Foto tambahan memakai kamera virtual yang diputar mengelilingi sumbu vertikal (kiri 90°, belakang 180°,
kanan 270°; "kiri" = difoto dari sisi kiri karakter). Karena framing foto pengguna tidak sama persis
dengan gambar depan, siluet mesh dari kamera itu dicocokkan ke alpha foto (skala dari tinggi bbox, lalu
pencarian kisi skala ±8% & geser ±3% yang memaksimalkan IoU). Foto dengan IoU < 0,6 dilewati dan
ditandai "dilewati" di UI. Setiap texel memakai foto yang paling tegak lurus terhadap permukaannya
(bobot^4 agar sambungan tidak lebar), sisanya warna NeRF.

Geometri tetap dari gambar depan saja (TripoSR model satu-gambar), jadi foto samping/belakang
memperbaiki warna, bukan bentuk. Uji arah & cermin: `scripts/make_test_views.py` membuat objek
dengan kotak merah di punggung kiri.
Tanpa tekstur (opsi "Vertex color"), warna vertex dikonversi sRGB→linear agar tidak pucat di glTF.

### Text → 3D (`app/textgen.py`, `engine/sheet.py`)

1. Deskripsi pengguna dibungkus prompt yang ramah 3D (satu objek, seluruh badan, pose A untuk karakter,
   latar putih polos, tanpa bayangan) + preset gaya (3D kartun, realistis, chibi, anime, low poly).
2. Mode **turnaround** (default): satu gambar 16:9 berisi 4 tampak berjajar (depan, samping, belakang,
   samping). Karena dibuat dalam satu gambar, desainnya konsisten antar tampak. Gambar dipotong per figur
   (rembg + komponen terhubung); figur 1 = depan (masukan TripoSR), figur 3 = belakang, figur 2 & 4 =
   samping. Model gambar sering tertukar kiri/kanan, jadi saat bake kedua penempatan dicoba dan dipilih
   yang siluetnya paling cocok dengan mesh.
3. gflow mengendalikan Chrome sungguhan pada akun Google server (Xvfb `:99`, mode `classic` agar memakai
   kuota gambar harian paket, bukan kuota agen). Profil Chrome dipakai bersama otomasi lain, jadi semua
   pemanggil antre lewat `flock /tmp/gflow-profile.lock`. Gambar Flow tidak memakai kredit Veo.
4. `image i2i` gflow belum mendukung frontend Flow baru (flow.google.com) untuk akun ini, sehingga
   tampak tambahan dibuat lewat turnaround, bukan edit gambar.

Bila sesi Flow kedaluwarsa, job gagal dengan pesan berisi perintah login ulang
(`cd ~/github-repos/gflow-cli && DISPLAY=:99 .venv/bin/gflow auth login`).

### Rigging: kenapa ada penanda?

Seperti Meshy/Mixamo, posisi sendi ditebak otomatis dari irisan mesh (kaki dilacak dari lantai sampai
menyatu di selangkangan, leher = bagian tersempit, tangan = titik lengan terjauh), tapi karakter hasil AI
sering punya aksesori (ekor, senjata, jubah) yang mengecoh. Karena itu pengguna bisa menggeser 8 penanda
(dagu, selangkangan, siku, pergelangan, lutut) di tampak depan sebelum rig dibuat. Kedalaman tiap sendi
dihitung otomatis dari penampang mesh.

Perlakuan khusus mesh hasil AI:
- vertex yang terpecah di sambungan UV disatukan lagi (UV tetap utuh karena disimpan per sudut face);
- potongan kecil yang melayang dibuang;
- kaki/paha yang menempel dipotong di garis tengah (di bawah selangkangan) lalu lubangnya ditutup,
  supaya kaki bisa melangkah tanpa mesh "tertarik";
- bobot tulang kiri tidak boleh mempengaruhi sisi kanan (dan sebaliknya); vertex yang gagal di-skin
  diberi bobot dari tulang terdekat.

### Animasi dari file (retarget)

Rig humanoid memakai nama tulang Mixamo, jadi animasi Mixamo (Download → FBX, *Without Skin*) dan BVH
(mis. CMU mocap) bisa diunggah per aset. Nama tulang dipetakan (prefix `mixamorig:` dibuang, alias umum
seperti `LeftUpperArm`/`LeftThigh`/`Pelvis`), lalu untuk tiap tulang dihitung rotasi dunia relatif terhadap
pose istirahat sumber. Karena rig sumber umumnya pose T sedangkan rig kita pose A, arah istirahat tiap
tulang diselaraskan dulu (`rotation_difference`), baru gerakan sumber diterapkan dan dikonversi ke rotasi
lokal mengikuti hierarki. Root motion diskalakan menurut tinggi pinggul dan dibuat *in place*.

### Remesh (`engine/blender/remesh.py`)

Vertex sambungan UV disatukan → voxel remesh (menjamin manifold) → smooth ringan 2 iterasi (pemeriksa
QuadriFlow di Blender 5.x menolak keluaran voxel mentah) → QuadriFlow (atau decimate untuk mode segitiga)
→ smart UV → bake warna dari model asli dengan Cycles *selected to active* → GLB + FBX + OBJ (quad tetap
quad di OBJ/FBX; GLB selalu segitiga karena format glTF). Hasil bisa dijadikan model utama (rig dibuat ulang
otomatis dan remesh diterapkan ulang setelah tekstur dibuat ulang) atau dikembalikan ke model asli.

### Hewan berkaki empat (`engine/blender/autorig_quad.py`)

Model hewan sebaiknya dibuat dari tampak **samping**; text → 3D jenis *Hewan* otomatis meminta turnaround
`[samping, depan, samping lain, belakang]` dan memakai tampak samping sebagai masukan TripoSR. Penanda:
kepala, pundak, pinggul, ujung ekor, telapak depan & belakang. Kepala ditentukan dari ujung badan yang lebih
"gemuk" (bukan yang lebih tinggi — ekor sering terangkat di atas kepala); pundak/pinggul dibatasi dekat garis
punggung agar tidak tertukar dengan kepala/telinga. Kaki kiri/kanan dipisah dari kedalaman mesh. Langkah
jalan memakai urutan lateral (belakang-kiri, depan-kiri, belakang-kanan, depan-kanan); lari berupa
*bound* dengan lenturan tulang punggung.

Tampak depan vs belakang hewan sering punya siluet hampir sama, sehingga penempatan foto samping yang
seri diputuskan dari kecocokan warna foto dengan warna tebakan NeRF.

## Menjalankan

```bash
uv sync                      # torch CPU + dependensi (pyproject.toml)
cp .env.example .env         # isi APP_PASSWORD & SECRET_KEY
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8800
```

Butuh Blender ≥ 4.2 (default `~/game-toolchain/bin/blender`, ubah via `BLENDER_BIN`).
Bobot TripoSR (±1,7 GB) diunduh otomatis dari HuggingFace saat pertama dipakai.

Produksi di server ini: `systemctl --user {status,restart} gitgit3d` (unit: `deploy/gitgit3d.service`,
Nice=10 & MemoryMax=8G agar CCTV/Frigate tidak terganggu), dipublikasikan lewat Cloudflare Tunnel
(`~/.cloudflared/config.yml`, host `3d.gitgitmiko.my.id` → `127.0.0.1:8800`).

Catatan: Cloudflare meng-cache `.js/.css` di edge. Setelah mengubah `web/app.js` atau `web/style.css`,
naikkan angka `?v=` di `web/index.html`.

Uji cepat tanpa UI:

```bash
.venv/bin/python -m engine.image_to_3d gambar.png out/
blender -b --factory-startup -P engine/blender/autorig.py -- prepare out/model.glb out/rig
blender -b --factory-startup -P engine/blender/autorig.py -- rig out/model.glb out/rig 1.75 [markers.json]
blender -b --factory-startup -P engine/blender/render_preview.py -- out/rig/rigged.glb prev.png --anim walk --frame 8
```

## API

Semua endpoint (kecuali login) butuh cookie sesi.

| Method | Path | Keterangan |
| --- | --- | --- |
| POST | `/api/login` `{password}` | masuk |
| GET | `/api/assets` | daftar aset |
| POST | `/api/assets` (multipart: `image`, opsional `image_back`/`image_left`/`image_right`, `name`, `resolution` 128–320, `target_faces`, `texture_res` 0/1024/2048, `remove_bg`, `auto_rig`) | buat model dari gambar |
| GET/PATCH/DELETE | `/api/assets/{id}` | detail (+ job, penanda) / ganti nama / hapus |
| POST | `/api/assets/text` `{prompt, kind: character\|animal\|object, style, turnaround, resolution, target_faces, texture_res, auto_rig}` | teks → gambar (Google Flow) → 3D |
| POST | `/api/assets/{id}/views` (multipart `image_back`/`image_left`/`image_right`, `remove`=daftar dipisah koma) | ganti foto sisi lain, buat ulang model + rig |
| POST | `/api/assets/{id}/rig/prepare` `{height, rig_type: humanoid\|quadruped}` | tebak penanda + render tampak depan |
| POST | `/api/assets/{id}/rig` `{markers, height, animations, rig_type}` | buat rig + animasi |
| POST/DELETE | `/api/assets/{id}/rig/animations` (multipart `files`) / `.../{nama}` | animasi Mixamo FBX / BVH |
| POST | `/api/assets/{id}/remesh` `{mode: quad\|tri, target_faces, texture_res, apply}` · `/remesh/revert` | remesh |
| GET | `/api/jobs/{id}`, `/api/queue` | status job / antrean |
| GET | `/files/{id}/{path}?download=1` | unduh file hasil |

## Roadmap (fitur Meshy lain)

- [x] **Texture bake** (UV atlas + tekstur dari medan warna + proyeksi gambar asli).
- [ ] Peta normal/roughness (PBR penuh) dan perbaikan sisi belakang tekstur.
- [x] **Multi-view tekstur** (foto belakang/samping ditempel ke tekstur).
- [ ] Multi-view untuk **geometri** (carving siluet / model multi-view) agar bentuk samping lebih akurat.
- [x] **Text → 3D** lewat Google Flow (gflow-cli) dengan turnaround 4 tampak.
- [ ] **AI texturing / retexture** untuk mesh yang sudah ada (butuh GPU agar praktis).
- [x] **Remesh**: quad (QuadriFlow) / segitiga + bake ulang tekstur.
- [x] Animasi tambahan (dance, attack, sit, cheer) + retarget dari file Mixamo FBX / BVH.
- [x] Rig hewan berkaki empat + jenis *Hewan* di text → 3D.
- [ ] Rig burung / hewan bersayap, ular; animasi wajah (blendshape).
- [ ] Retarget animasi untuk rig hewan.
- [ ] Engine lebih tajam (Hunyuan3D-2mini / TRELLIS / SF3D) bila server mendapat GPU.

## Lisensi pihak ketiga

- TripoSR — MIT, © Tripo AI & Stability AI (`engine/tsr/LICENSE-TripoSR`). Bobot model: `stabilityai/TripoSR` (MIT).
- `@google/model-viewer` — BSD-3-Clause (`web/vendor/model-viewer.min.js`).
- rembg — MIT; model `isnet-general-use` — Apache-2.0.
