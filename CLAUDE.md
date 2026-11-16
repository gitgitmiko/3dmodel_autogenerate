# CLAUDE.md — gitgit3d

Engine 3D generatif self-hosted ala Meshy, jalan di server rumah **tanpa GPU**.
Live: https://3d.gitgitmiko.my.id (login password, ada di `.env` → `APP_PASSWORD`).
GitHub: `gitgitmiko/3dmodel_autogenerate` (branch `main`). Detail teknis lengkap: `README.md`.

**Balas pengguna dalam Bahasa Indonesia.**

## Batasan utama (menentukan semua keputusan)

- Server: i5-1135G7 (4 core/8 thread), RAM 15 GB, **tanpa GPU NVIDIA** (hanya Iris Xe). Model yang butuh
  CUDA (Hunyuan3D, TRELLIS, SF3D) tidak bisa dipakai. Rekonstruksi 3D = **TripoSR di CPU** (±55–75 dtk/gambar,
  RAM puncak ±3,8 GB).
- Server yang sama menjalankan CCTV/Frigate, Home Assistant, 9router, whatif-automation, dll. Job harus tetap
  **satu antrean** dan berprioritas rendah (unit systemd: `Nice=10`, `MemoryMax=8G`). Jangan paralelkan job berat.

## Struktur

```
app/        FastAPI: main.py (API + auth), worker.py (antrean 1 worker), store.py (SQLite), textgen.py (gflow), config.py
engine/     image_to_3d.py (rembg → TripoSR → mesh → tekstur), texture.py (UV xatlas + bake + proyeksi multi-view),
            sheet.py (potong turnaround), tsr/ (TripoSR vendored, torchmcubes → PyMCubes)
engine/blender/  skrip Blender headless: autorig.py (humanoid), autorig_quad.py (hewan 4 kaki),
                 remesh.py (QuadriFlow/decimate + bake), render_preview.py (render pratinjau/debug)
web/        UI HTML/JS polos (app.js, style.css, index.html) + vendor/model-viewer.min.js
scripts/    alat uji: make_test_views.py, make_test_anim.py (FBX), make_test_bvh.py (BVH Y-up), draw_markers.py
deploy/     gitgit3d.service (systemd user unit, di-symlink ke ~/.config/systemd/user/)
data/       (gitignored) assets/<id>/..., gitgit3d.sqlite, gflow-project.txt, test/ (hasil uji)
```

Job kinds: `text_to_image` → `image_to_3d` → `rig_prepare` → `rig`, plus `remesh`. Satu aset = satu folder
`data/assets/<id>/` (input.png, view_*.png, model.glb, rig/, remesh/, ...).

## Menjalankan & deploy

- Python via `uv` (`uv sync`; torch CPU dari index pytorch-cpu). Jalankan kode dengan `.venv/bin/python`.
- Blender: `~/game-toolchain/bin/blender` (5.2 LTS). Selalu `-b --factory-startup -P script.py -- args`.
- Service: `systemctl --user restart gitgit3d` (port 8800). **Restart wajib setelah mengubah `app/` atau
  `engine/`**; perubahan `web/` cukup refresh browser.
- **Cloudflare meng-cache `.js/.css` di edge** (TTL 4 jam, header no-cache ditimpa). Setelah mengubah
  `web/app.js` atau `web/style.css`, **naikkan angka `?v=N`** di `web/index.html`.
- Tunnel: unit user `cloudflared-webunime.service`, config `~/.cloudflared/config.yml`
  (`3d.gitgitmiko.my.id → 127.0.0.1:8800`). Restart tunnel memutus semua subdomain beberapa detik.
- Jangan `pkill -f` dengan pola yang juga cocok dengan baris perintah shell sendiri (pernah mematikan shell).

## Text → 3D lewat gflow-cli (Google Flow / Nano Banana)

- gflow ada di `~/github-repos/gflow-cli/.venv/bin/gflow`, memakai akun Google pemilik server (Chrome sungguhan
  di Xvfb `:99`). Gambar Flow tidak memakai kredit Veo; kuota gambar harian paket besar (±1.000/hari).
- **Selalu**: `DISPLAY=:99`, `GFLOW_CLI_UI_MODE=classic` (mode agen memakai kuota kecil), dan bungkus dengan
  `flock -w 1800 /tmp/gflow-profile.lock` — profil Chrome dipakai bersama whatif-automation & otomasi lain.
- `gflow image i2i` **tidak didukung** di frontend Flow baru untuk akun ini → tampak tambahan dibuat lewat
  satu gambar **turnaround 4 tampak** (16:9) lalu dipotong (`engine/sheet.py`).
- `gflow auth status` bisa keliru melapor "not signed in to Flow" padahal `t2i` berhasil; uji dengan generate.
- Bila sesi benar-benar kedaluwarsa, login ulang harus dilakukan pengguna sendiri di server
  (`cd ~/github-repos/gflow-cli && DISPLAY=:99 .venv/bin/gflow auth login`).

## Konvensi koordinat (sering jadi sumber bug)

- TripoSR: Z atas, kamera input di +X, kanan gambar = +Y. Diputar ke glTF: Y atas, **depan menghadap +Z**.
- Di Blender setelah impor glTF: Z atas, depan = **-Y**, sisi kiri karakter = **+X**.
- Rotasi animasi didefinisikan di ruang armature lalu `to_local()`; sudut positif sumbu X/L memutar ujung bawah
  tulang ke **belakang** (paha maju = negatif, lutut menekuk = positif).
- Multi-view: kamera "left" = difoto dari sisi KIRI karakter (wajah menghadap kiri layar). Turnaround AI sering
  tertukar kiri/kanan → `side_a/side_b` ditempatkan lewat skor siluet, seri diputus dengan kecocokan warna.

## Gotcha yang sudah ditemui

- Mesh bertekstur punya vertex terpecah di sambungan UV → **satukan dulu** (`remove_doubles`) sebelum
  deteksi pulau, skinning, atau voxel remesh.
- QuadriFlow di Blender 5.2 menolak keluaran voxel remesh mentah → beri smooth ringan 2 iterasi dulu.
- `to_track_quat("-Z", "Y")` untuk kamera (bukan `"Z"` sebagai up — membuat kamera terguling).
- Blender butuh path output absolut untuk render.
- Hewan: kepala = ujung badan yang lebih **gemuk** (ekor sering lebih tinggi dari kepala).
- Kaki/paha humanoid yang menempel dipotong di garis tengah di bawah selangkangan agar bisa melangkah.

## Cara menguji

- Tes engine cepat: `.venv/bin/python -m engine.image_to_3d gambar.png out/`
- Render pratinjau (verifikasi visual — **selalu lihat hasil render, jangan hanya percaya exit code**):
  `blender -b --factory-startup -P engine/blender/render_preview.py -- model.glb out.png --views front,side,back,left,right,34 [--anim walk --frame 8] [--joints] [--wire]`
- Objek uji arah/cermin multi-view: `scripts/make_test_views.py` (kotak merah di punggung kiri).
- Uji retarget: `scripts/make_test_anim.py` (FBX pose T, nama `mixamorig:`), `scripts/make_test_bvh.py`.
- E2E browser: Playwright tidak terpasang global; pakai `NODE_PATH=~/github-repos/WEBUNIME/node_modules node script.js`.
- Contoh gambar TripoSR ada di `data/test/` (police_woman, robot, tiger_girl).

## Status & roadmap

Sudah: image→3D, texture bake + proyeksi gambar asli, multi-view, text→3D (gflow), auto-rig humanoid (9 animasi +
retarget Mixamo FBX/BVH), rig hewan 4 kaki, remesh quad/segitiga.
Berikutnya (lihat README): rig burung/ular, animasi wajah (blendshape), retarget untuk rig hewan, multi-view untuk
geometri, peta normal/roughness.

## Git

- Identitas repo-lokal: `gitgitmiko <gitgitmiko@users.noreply.github.com>`. Push ke `origin main`.
- Commit hanya bila diminta / sesuai alur kerja pengguna; jangan commit `.env` atau `data/`.
