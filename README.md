# gitgit3d

Engine 3D generatif self-hosted ala Meshy, berjalan **tanpa GPU** di server rumah.
Live: **https://3d.gitgitmiko.my.id** (login password).

Tahap 1 (sudah jalan):

- **Image → 3D**: satu gambar menjadi model 3D (GLB / OBJ / STL).
- **Texture bake**: UV atlas + tekstur 1K/2K; sisi depan memakai gambar asli sehingga detailnya tajam.
- **Auto-rig humanoid + animasi dasar**: kerangka 22 tulang (nama standar Mixamo), skinning otomatis,
  animasi *idle, walk, run, wave, jump*, diekspor ke GLB dan FBX (Unity/Unreal/Godot/Blender).

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

Sisi belakang/samping tetap buram karena hanya ada satu gambar — itu batas pendekatan satu-gambar.
Tanpa tekstur (opsi "Vertex color"), warna vertex dikonversi sRGB→linear agar tidak pucat di glTF.

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
| POST | `/api/assets` (multipart: `image`, `name`, `resolution` 128–320, `target_faces`, `texture_res` 0/1024/2048, `remove_bg`, `auto_rig`) | buat model dari gambar |
| GET/PATCH/DELETE | `/api/assets/{id}` | detail (+ job, penanda) / ganti nama / hapus |
| POST | `/api/assets/{id}/rig/prepare` `{height}` | tebak penanda + render tampak depan |
| POST | `/api/assets/{id}/rig` `{markers, height, animations}` | buat rig + animasi |
| GET | `/api/jobs/{id}`, `/api/queue` | status job / antrean |
| GET | `/files/{id}/{path}?download=1` | unduh file hasil |

## Roadmap (fitur Meshy lain)

- [x] **Texture bake** (UV atlas + tekstur dari medan warna + proyeksi gambar asli).
- [ ] Peta normal/roughness (PBR penuh) dan perbaikan sisi belakang tekstur.
- [ ] **Multi-view image → 3D** (depan/samping/belakang) untuk geometri lebih akurat.
- [ ] **Text → 3D**: text → gambar (model difusi ringan / API eksternal) → pipeline image→3D.
- [ ] **AI texturing / retexture** untuk mesh yang sudah ada (butuh GPU agar praktis).
- [ ] **Remesh**: quad remesh & target poligon dari UI (Blender QuadriFlow / voxel remesh).
- [ ] Animasi tambahan (dance, attack, sit) + retarget dari file Mixamo/BVH.
- [ ] Rig untuk karakter non-humanoid (berkaki empat).
- [ ] Engine lebih tajam (Hunyuan3D-2mini / TRELLIS / SF3D) bila server mendapat GPU.

## Lisensi pihak ketiga

- TripoSR — MIT, © Tripo AI & Stability AI (`engine/tsr/LICENSE-TripoSR`). Bobot model: `stabilityai/TripoSR` (MIT).
- `@google/model-viewer` — BSD-3-Clause (`web/vendor/model-viewer.min.js`).
- rembg — MIT; model `isnet-general-use` — Apache-2.0.
