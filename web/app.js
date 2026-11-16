// gitgit3d — UI (vanilla JS)
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const MARKERS = [
  { k: "chin", label: "Dagu", color: "#ffd84a" },
  { k: "groin", label: "Selangkangan", color: "#ff8a3d" },
  { k: "elbow_l", label: "Siku kiri", color: "#4ad6ff" },
  { k: "wrist_l", label: "Pergelangan kiri", color: "#3e8bff" },
  { k: "knee_l", label: "Lutut kiri", color: "#7c5cff" },
  { k: "elbow_r", label: "Siku kanan", color: "#5dff9d" },
  { k: "wrist_r", label: "Pergelangan kanan", color: "#2fbf6a" },
  { k: "knee_r", label: "Lutut kanan", color: "#ff5ca8" },
];
const MIRROR = { elbow_l: "elbow_r", elbow_r: "elbow_l", wrist_l: "wrist_r", wrist_r: "wrist_l", knee_l: "knee_r", knee_r: "knee_l" };
const ANIM_LABEL = { idle: "Diam (idle)", walk: "Jalan", run: "Lari", wave: "Melambai", jump: "Lompat", dance: "Joget", attack: "Menyerang", sit: "Duduk", cheer: "Sorak" };
const VIEWS = [
  { k: "back", label: "Belakang" },
  { k: "left", label: "Samping kiri" },
  { k: "right", label: "Samping kanan" },
];
const KIND_LABEL = { remesh: "Remesh", text_to_image: "Teks → gambar (Google Flow)", image_to_3d: "Gambar → 3D", rig_prepare: "Menyiapkan rig", rig: "Rigging & animasi" };

const state = { assets: [], current: null, tab: "model", pollTimer: null, markers: null, symmetric: true };

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: "same-origin", ...opts,
    headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json", ...(opts.headers || {}) } : opts.headers });
  let data = null;
  try { data = await r.json(); } catch { /* bukan JSON */ }
  if (r.status === 401 && !path.endsWith("/login")) { showLogin(); throw new Error("Belum masuk"); }
  if (!r.ok) throw new Error((data && (data.message || data.detail)) || `HTTP ${r.status}`);
  return data;
}

// ------------------------------------------------------------------ login
function showLogin() {
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  $("#pw").focus();
}
$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#loginErr").textContent = "";
  try {
    await api("api/login", { method: "POST", body: JSON.stringify({ password: $("#pw").value }) });
    $("#pw").value = "";
    boot();
  } catch (err) { $("#loginErr").textContent = err.message; }
});
$("#logoutBtn").addEventListener("click", async () => { await api("api/logout", { method: "POST" }); showLogin(); });
$("#newBtn").addEventListener("click", () => go(""));

// ------------------------------------------------------------------ routing (#/a/<id>/<tab>)
function go(id, tab) { location.hash = id ? `#/a/${id}${tab ? "/" + tab : ""}` : "#/"; }
window.addEventListener("hashchange", route);
function route() {
  const m = location.hash.match(/^#\/a\/([0-9a-f]+)(?:\/(\w+))?/);
  clearTimeout(state.pollTimer);
  if (m) { state.tab = m[2] || "model"; openAsset(m[1], true); }
  else { state.current = null; renderGallery(); renderNew(); }
}

// ------------------------------------------------------------------ galeri
function statusBadges(a) {
  const b = [];
  if (a.status === "ready") b.push('<span class="badge ok">3D</span>');
  else if (a.status === "error") b.push('<span class="badge err">gagal</span>');
  else b.push('<span class="badge run">proses</span>');
  if (a.rig_status === "rigged") b.push('<span class="badge rig">rig</span>');
  else if (["preparing", "rigging"].includes(a.rig_status)) b.push('<span class="badge run">rig…</span>');
  else if (a.rig_status === "prepared") b.push('<span class="badge">penanda</span>');
  return b.join("");
}
function renderGallery() {
  const g = $("#gallery");
  if (!state.assets.length) { g.innerHTML = '<p class="muted small" style="padding:8px">Belum ada model. Klik “＋ Baru”.</p>'; return; }
  g.innerHTML = state.assets.map((a) => {
    const thumb = a.files["rig/thumb.png"] || a.files["thumb.png"] || a.files["input.png"];
    return `<div class="item ${state.current?.id === a.id ? "on" : ""}" data-id="${a.id}">
      ${thumb ? `<img src="${thumb}?t=${a.rig_status}${a.status}" alt="" loading="lazy">` : '<div class="ph">◆</div>'}
      <div class="meta"><div class="nm">${esc(a.name)}</div><div>${statusBadges(a)}</div></div></div>`;
  }).join("");
  g.querySelectorAll(".item").forEach((el) => el.addEventListener("click", () => go(el.dataset.id)));
}
async function refreshList() {
  try { state.assets = await api("api/assets"); renderGallery(); } catch { /* abaikan */ }
}
async function refreshQueue() {
  try {
    const q = await api("api/queue");
    const run = q.active.find((j) => j.status === "running");
    const waiting = q.active.filter((j) => j.status === "queued").length;
    $("#queueInfo").textContent = run ? `⏳ ${KIND_LABEL[run.kind]} ${Math.round(run.progress * 100)}%${waiting ? ` · ${waiting} antre` : ""}` : "";
  } catch { /* abaikan */ }
}

// ------------------------------------------------------------------ slot foto sisi lain
// files: {back: File|null, ...}; existing: {back: url}; removed: Set; status: {back: {used, iou}}
function mountViewSlots(el, { existing = {}, status = {}, onChange = () => {} } = {}) {
  const st = { files: {}, removed: new Set() };
  const render = () => {
    el.innerHTML = VIEWS.map(({ k, label }) => {
      const f = st.files[k];
      const url = f ? URL.createObjectURL(f) : !st.removed.has(k) && existing[k];
      const s = !f && url && status[k];
      const badge = s ? (s.used ? `<span class="st">dipakai</span>` : `<span class="st" title="Siluet tidak cocok dengan model (IoU ${s.iou})">dilewati</span>`) : "";
      return `<label class="vslot ${f ? "new-file" : ""}" data-k="${k}">
        <input type="file" accept="image/png,image/jpeg,image/webp" hidden>
        ${url ? `<img src="${url}" alt="">${badge}<button type="button" class="x" title="Hapus">×</button>` : `<span>＋<br>tambah</span>`}
        <span class="lbl">${label}</span></label>`;
    }).join("");
    el.querySelectorAll(".vslot").forEach((slot) => {
      const k = slot.dataset.k, input = $("input", slot);
      const set = (file) => { if (file && file.type.startsWith("image/")) { st.files[k] = file; st.removed.delete(k); render(); onChange(st); } };
      input.addEventListener("change", () => set(input.files[0]));
      ["dragenter", "dragover"].forEach((ev) => slot.addEventListener(ev, (e) => { e.preventDefault(); slot.classList.add("over"); }));
      ["dragleave", "drop"].forEach((ev) => slot.addEventListener(ev, (e) => { e.preventDefault(); slot.classList.remove("over"); }));
      slot.addEventListener("drop", (e) => set(e.dataTransfer.files[0]));
      $(".x", slot)?.addEventListener("click", (e) => {
        e.preventDefault(); e.stopPropagation();
        if (st.files[k]) delete st.files[k]; else st.removed.add(k);
        render(); onChange(st);
      });
    });
  };
  render();
  return st;
}

// ------------------------------------------------------------------ animasi dari file (Mixamo FBX / BVH)
function customAnimCardHTML(a) {
  const list = a.custom_anims || [];
  return `<div class="card"><h3>Animasi dari file</h3>
    <p class="muted small">Mixamo: pilih animasi → Download → Format <b>FBX</b>, <b>Without Skin</b>, 30 fps. BVH (mis. CMU mocap) juga bisa. Gerakan di-retarget otomatis ke rig ini.</p>
    <div class="dl">${list.map((n) => `<div class="anim-file"><span>${esc(n)}</span><button class="btn ghost sm danger" data-del-anim="${esc(n)}">Hapus</button></div>`).join("")}</div>
    <label class="btn sm wide" style="margin-top:8px">＋ Unggah .fbx / .bvh<input type="file" id="animFiles" accept=".fbx,.bvh" multiple hidden></label>
    <p class="error" id="animErr"></p></div>`;
}
function wireCustomAnims(a) {
  $("#animFiles")?.addEventListener("change", async (e) => {
    const fd = new FormData();
    [...e.target.files].forEach((f) => fd.append("files", f));
    try { await api(`api/assets/${a.id}/rig/animations`, { method: "POST", body: fd }); openAsset(a.id, true); }
    catch (err) { $("#animErr").textContent = err.message; }
  });
  document.querySelectorAll("[data-del-anim]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(`Hapus animasi ${b.dataset.delAnim}?`)) return;
    try { await api(`api/assets/${a.id}/rig/animations/${encodeURIComponent(b.dataset.delAnim)}`, { method: "DELETE" }); openAsset(a.id, true); }
    catch (err) { $("#animErr").textContent = err.message; }
  }));
}

// ------------------------------------------------------------------ halaman baru
function renderNew() {
  const main = $("#main");
  main.replaceChildren($("#tplNew").content.cloneNode(true));
  let file = null;
  const views = mountViewSlots($("#newViews"));
  const drop = $("#drop"), fileIn = $("#file");
  const setFile = (f) => {
    if (!f || !f.type.startsWith("image/")) return;
    file = f;
    const img = $("#dropPreview");
    img.src = URL.createObjectURL(f);
    img.classList.remove("hidden");
    $("#dropHint").classList.add("hidden");
    $("#submitBtn").disabled = false;
    if (!$("#name").value) $("#name").placeholder = f.name.replace(/\.[^.]+$/, "");
  };
  fileIn.addEventListener("change", () => setFile(fileIn.files[0]));
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => setFile(e.dataTransfer.files[0]));
  window.onpaste = (e) => { const f = [...(e.clipboardData?.files || [])][0]; if (f && $("#drop")) setFile(f); };
  // tab gambar / teks
  const setMode = (mode) => {
    main.querySelectorAll("#newTabs button").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
    $("#newForm").classList.toggle("hidden", mode !== "image");
    $("#textForm").classList.toggle("hidden", mode !== "text");
    $("#newHint").textContent = mode === "text"
      ? "Tulis deskripsi objek atau karakter (bahasa Indonesia/Inggris). Google Flow membuat gambarnya, lalu gambar diproses jadi 3D bertekstur."
      : "Unggah satu gambar objek atau karakter. Hasil terbaik: satu objek, tampak depan, latar polos, karakter dengan lengan & kaki sedikit terbuka (pose A/T).";
    try { localStorage.setItem("g3d-mode", mode); } catch { /* abaikan */ }
  };
  main.querySelectorAll("#newTabs button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
  let saved = "image"; try { saved = localStorage.getItem("g3d-mode") || "image"; } catch { /* abaikan */ }
  setMode(saved);
  $("#tPrompt").addEventListener("input", () => { $("#tSubmit").disabled = $("#tPrompt").value.trim().length < 3; });
  $("#textForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const tf = $("#textForm");
    const val = (n) => $(`.seg[data-name=${n}] button.on`, tf).dataset.v;
    const body = { prompt: $("#tPrompt").value.trim(), name: $("#tName").value, kind: val("kind"), style: val("style"),
      turnaround: $("#tTurn").checked, resolution: +val("resolution"), target_faces: +val("target_faces"),
      texture_res: +val("texture_res"), auto_rig: $("#tAutoRig").checked };
    $("#tSubmit").disabled = true; $("#tSubmit").textContent = "Mengirim…";
    try { const r = await api("api/assets/text", { method: "POST", body: JSON.stringify(body) }); await refreshList(); go(r.asset.id); }
    catch (err) { $("#tErr").textContent = err.message; $("#tSubmit").disabled = false; $("#tSubmit").textContent = "Buat dari teks"; }
  });
  main.querySelectorAll(".seg").forEach((seg) => seg.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    seg.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  }));
  $("#newForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!file) return;
    const fd = new FormData();
    fd.append("image", file);
    for (const [k, f] of Object.entries(views.files)) fd.append(`image_${k}`, f);
    fd.append("name", $("#name").value);
    $("#newForm").querySelectorAll(".seg").forEach((seg) => fd.append(seg.dataset.name, $("button.on", seg).dataset.v));
    fd.append("remove_bg", $("#removeBg").checked);
    fd.append("auto_rig", $("#autoRig").checked);
    $("#submitBtn").disabled = true;
    $("#submitBtn").textContent = "Mengunggah…";
    try {
      const r = await api("api/assets", { method: "POST", body: fd });
      await refreshList();
      go(r.asset.id);
    } catch (err) {
      $("#newErr").textContent = err.message;
      $("#submitBtn").disabled = false;
      $("#submitBtn").textContent = "Buat model 3D";
    }
  });
}

// ------------------------------------------------------------------ halaman aset
async function openAsset(id, force = false) {
  let a;
  try { a = await api(`api/assets/${id}`); }
  catch (err) { $("#main").innerHTML = `<div class="empty"><p class="error">${esc(err.message)}</p></div>`; return; }
  const first = state.current?.id !== id;
  const prevSig = state.current && sig(state.current);
  state.current = a;
  const i = state.assets.findIndex((x) => x.id === a.id);
  if (i >= 0) state.assets[i] = a; else state.assets.unshift(a);
  renderGallery();
  if (first || !$("#tabBody")) { state.markers = null; renderAssetShell(); renderTab(); }
  else if (force || prevSig !== sig(a)) renderTab();
  else updateProgress();
  const busy = a.jobs.some((j) => ["queued", "running"].includes(j.status));
  if (busy) state.pollTimer = setTimeout(() => openAsset(id), 1500);
}
// Tanda tangan status: render ulang tab hanya bila status berubah (agar viewer 3D tidak berkedip).
const sig = (a) => [a.status, a.rig_status, a.jobs.map((j) => j.id + j.status).join()].join("|");

function renderAssetShell() {
  const a = state.current;
  $("#main").replaceChildren($("#tplAsset").content.cloneNode(true));
  $("#aName").textContent = a.name;
  $("#aMeta").textContent = new Date(a.created * 1000).toLocaleString("id-ID");
  $("#aName").addEventListener("click", async () => {
    const n = prompt("Nama baru", a.name);
    if (!n || n === a.name) return;
    await api(`api/assets/${a.id}`, { method: "PATCH", body: JSON.stringify({ name: n }) });
    $("#aName").textContent = n; a.name = n; refreshList();
  });
  $("#delBtn").addEventListener("click", async () => {
    if (!confirm(`Hapus “${a.name}” beserta semua filenya?`)) return;
    try { await api(`api/assets/${a.id}`, { method: "DELETE" }); await refreshList(); go(""); }
    catch (err) { alert(err.message); }
  });
  document.querySelectorAll(".tabs button").forEach((b) => {
    b.classList.toggle("on", b.dataset.tab === state.tab);
    b.addEventListener("click", () => go(a.id, b.dataset.tab));
  });
}

function latest(kind) { return state.current.jobs.find((j) => j.kind === kind); }
function activeJob(kinds) {
  const act = state.current.jobs.filter((j) => kinds.includes(j.kind) && ["queued", "running"].includes(j.status));
  return act.find((j) => j.status === "running") || act[act.length - 1]; // yang berjalan, atau antrean terdepan
}

function progressHTML(job) {
  const pct = Math.round((job.progress || 0) * 100);
  const msg = job.status === "queued" ? (job.queue_position ? `Menunggu antrean (${job.queue_position} job di depan)` : "Menunggu antrean") : job.message;
  return `<div class="progress" data-job="${job.id}"><div class="spinner"></div><strong>${KIND_LABEL[job.kind]}</strong>
    <div class="bar"><i style="width:${pct}%"></i></div><span class="muted small job-msg">${esc(msg)} · ${pct}%</span></div>`;
}
function updateProgress() {
  document.querySelectorAll(".progress[data-job]").forEach((el) => {
    const j = state.current.jobs.find((x) => x.id === el.dataset.job);
    if (!j) return;
    const pct = Math.round((j.progress || 0) * 100);
    $(".bar > i", el).style.width = pct + "%";
    const msg = j.status === "queued" ? (j.queue_position ? `Menunggu antrean (${j.queue_position} job di depan)` : "Menunggu antrean") : j.message;
    $(".job-msg", el).textContent = `${msg} · ${pct}%`;
  });
}
function warningsHTML(list) {
  return list?.length ? `<div class="warn-box"><strong>Catatan</strong><ul>${list.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : "";
}
function downloadsHTML(items) {
  return items.length ? `<div class="card"><h3>Unduh</h3><div class="dl">${items.map((d) =>
    `<a class="btn sm" href="${d.url}?download=1" download="${esc(d.name)}">⬇ ${esc(d.label)}</a>`).join("")}</div></div>` : "";
}

function renderTab() {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === state.tab));
  (state.tab === "rig" ? renderRigTab : renderModelTab)();
}

function renderModelTab() {
  const a = state.current, body = $("#tabBody");
  const job = activeJob(["text_to_image", "image_to_3d"])
    || [latest("text_to_image"), latest("image_to_3d")].find((j) => j?.status === "error" && !j.message.startsWith("Dibatalkan"))
    || latest("image_to_3d");
  const f = a.files;
  const promptCard = a.params.prompt ? `<div class="card"><h3>Deskripsi</h3><div class="prompt-box">${esc(a.params.prompt)}</div>
      ${f["sheet.png"] ? `<p></p><a href="${f["sheet.png"]}" target="_blank"><img src="${f["sheet.png"]}" alt="Turnaround" style="width:100%;border-radius:8px"></a>` : ""}</div>` : "";
  const thumbs = promptCard + `<div class="card"><h3>Gambar sumber</h3><div class="thumbs">
      ${f["input.png"] ? `<figure><img src="${f["input.png"]}" alt=""><figcaption>Asli</figcaption></figure>` : ""}
      ${f["cutout.png"] ? `<figure><img src="${f["cutout.png"]}" alt=""><figcaption>Tanpa background</figcaption></figure>` : ""}
    </div></div>`;
  let viewer;
  if (a.status === "ready") {
    viewer = `<model-viewer src="${f["model.glb"]}?v=${a.created}" camera-controls auto-rotate shadow-intensity="1" exposure="1.05"
      environment-image="neutral" touch-action="pan-y" camera-orbit="20deg 75deg auto" alt="Model 3D"></model-viewer>`;
  } else if (a.status === "error") {
    viewer = `<div class="progress"><div class="err-box">${esc(job?.message || "Gagal")}</div></div>`;
  } else {
    viewer = job ? progressHTML(job) : '<div class="progress"><div class="spinner"></div></div>';
  }
  const s = a.stats || {};
  const stats = a.status === "ready" ? `<div class="card"><h3>Info</h3><dl class="stats">
      <dt>Vertex</dt><dd>${(s.vertices || 0).toLocaleString("id-ID")}</dd>
      <dt>Face</dt><dd>${(s.faces || 0).toLocaleString("id-ID")}</dd>
      <dt>Resolusi</dt><dd>${a.params.resolution}³</dd>
      <dt>Tekstur</dt><dd>${s.texture ? `${s.texture}px` : "vertex color"}</dd>
      <dt>Foto dipakai</dt><dd>${s.texture_views ? Object.entries(s.texture_views).filter(([, v]) => v.used).length + " dari " + Object.keys(s.texture_views).length : (s.texture_projected ? "1" : "—")}</dd>
      <dt>Waktu proses</dt><dd>${s.seconds ?? "—"} dtk</dd></dl></div>` : "";
  const rigCta = a.status === "ready" ? `<div class="card"><h3>Karakter humanoid?</h3>
      <p class="muted small">Buat kerangka (rig) otomatis + animasi jalan, lari, lompat, melambai, idle.</p>
      <button class="btn primary wide" id="toRig">Lanjut ke rigging →</button></div>` : "";
  const rm = s.remesh, rmJob = activeJob(["remesh"]);
  const remeshCard = a.status === "ready" ? `<div class="card"><h3>Remesh</h3>
      ${s.remesh_applied ? `<p class="small"><span class="badge rig">model utama = hasil remesh</span></p>` : ""}
      ${rmJob ? progressHTML(rmJob) : `
      <div class="field">Topologi<div class="seg" data-rm="mode"><button type="button" data-v="quad" class="on">Quad</button><button type="button" data-v="tri">Segitiga</button></div></div><p></p>
      <div class="field">Target face<div class="seg" data-rm="target_faces"><button type="button" data-v="2000">2rb</button><button type="button" data-v="5000" class="on">5rb</button><button type="button" data-v="10000">10rb</button><button type="button" data-v="20000">20rb</button></div></div><p></p>
      <div class="field">Tekstur<div class="seg" data-rm="texture_res"><button type="button" data-v="1024" class="on">1K</button><button type="button" data-v="2048">2K</button></div></div><p></p>
      <label class="check"><input type="checkbox" id="rmApply"> Jadikan model utama (dipakai rig & unduhan utama)</label><p></p>
      <button class="btn primary wide" id="rmBtn">Remesh</button>`}
      ${rm && !rmJob ? `<p></p><dl class="stats"><dt>Hasil</dt><dd>${rm.faces.toLocaleString("id-ID")} face${rm.quads ? ` (${Math.round(100 * rm.quads / rm.faces)}% quad)` : ""}</dd><dt>Waktu</dt><dd>${rm.seconds} dtk</dd></dl>
        <p></p><button class="btn sm wide" id="rmView">Lihat hasil remesh</button>
        <div class="dl" style="margin-top:6px">${a.downloads.filter((d) => d.url.includes("/remesh/")).map((d) => `<a class="btn sm" href="${d.url}?download=1" download="${esc(d.name)}">⬇ ${esc(d.label)}</a>`).join("")}</div>` : ""}
      ${s.remesh_applied && !rmJob ? `<p></p><button class="btn ghost sm wide" id="rmRevert">Kembalikan model asli</button>` : ""}
      <p class="error" id="rmErr"></p></div>` : "";
  const viewsCard = a.status === "ready" || a.status === "error" ? `<div class="card"><h3>Foto sisi lain</h3>
      <p class="muted small">Tambahkan foto belakang/samping agar tekstur sisi itu tajam. Model dibuat ulang (±1–2 menit)${a.rig_status === "rigged" ? ", rig ikut diperbarui otomatis" : ""}.</p>
      <div class="view-slots" id="assetViews"></div><p></p>
      <button class="btn primary wide hidden" id="applyViews">Terapkan & buat ulang tekstur</button><p class="error" id="viewsErr"></p></div>` : "";
  body.innerHTML = `<div class="view-grid"><div class="viewer">${viewer}</div>
    <div class="side-col">${stats}${downloadsHTML(a.downloads.filter((d) => !d.url.includes("/rig/") && !d.url.includes("/remesh/")))}${remeshCard}${rigCta}${viewsCard}${thumbs}</div></div>`;
  $("#toRig")?.addEventListener("click", () => go(a.id, "rig"));
  body.querySelectorAll("[data-rm]").forEach((seg) => seg.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    seg.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  }));
  $("#rmBtn")?.addEventListener("click", async () => {
    const v = (n) => $(`[data-rm=${n}] button.on`, body).dataset.v;
    $("#rmBtn").disabled = true;
    try {
      await api(`api/assets/${a.id}/remesh`, { method: "POST", body: JSON.stringify({ mode: v("mode"), target_faces: +v("target_faces"), texture_res: +v("texture_res"), apply: $("#rmApply").checked }) });
      openAsset(a.id, true);
    } catch (err) { $("#rmErr").textContent = err.message; $("#rmBtn").disabled = false; }
  });
  $("#rmView")?.addEventListener("click", () => {
    const mv = $(".viewer model-viewer");
    if (!mv) return;
    const showing = mv.dataset.rm === "1";
    mv.src = (showing ? f["model.glb"] : f["remesh/model.glb"]) + "?v=" + Date.now();
    mv.dataset.rm = showing ? "" : "1";
    $("#rmView").textContent = showing ? "Lihat hasil remesh" : "Lihat model utama";
  });
  $("#rmRevert")?.addEventListener("click", async () => {
    if (!confirm("Kembalikan model asli sebagai model utama?")) return;
    try { await api(`api/assets/${a.id}/remesh/revert`, { method: "POST" }); openAsset(a.id, true); }
    catch (err) { $("#rmErr").textContent = err.message; }
  });
  if ($("#assetViews")) {
    const existing = Object.fromEntries(VIEWS.filter(({ k }) => f[`view_${k}.png`]).map(({ k }) => [k, f[`view_${k}.png`] + "?v=" + a.created]));
    const asg = s.side_assignment?.side_a; // tampak samping dari turnaround AI
    if (asg) {
      const other = asg === "left" ? "right" : "left";
      if (!existing[asg] && f["view_side_a.png"]) existing[asg] = f["view_side_a.png"];
      if (!existing[other] && f["view_side_b.png"]) existing[other] = f["view_side_b.png"];
    }
    const vs = mountViewSlots($("#assetViews"), { existing, status: s.texture_views || {},
      onChange: (st) => $("#applyViews").classList.toggle("hidden", !Object.keys(st.files).length && !st.removed.size) });
    $("#applyViews").addEventListener("click", async () => {
      const fd = new FormData();
      for (const [k, file] of Object.entries(vs.files)) fd.append(`image_${k}`, file);
      fd.append("remove", [...vs.removed].join(","));
      $("#applyViews").disabled = true;
      try { await api(`api/assets/${a.id}/views`, { method: "POST", body: fd }); openAsset(a.id, true); }
      catch (err) { $("#viewsErr").textContent = err.message; $("#applyViews").disabled = false; }
    });
  }
}

function renderRigTab() {
  const a = state.current, body = $("#tabBody");
  if (a.status !== "ready") {
    body.innerHTML = `<div class="empty muted">${a.status === "error" ? "Model 3D gagal dibuat, rigging tidak bisa dilakukan." : "Tunggu model 3D selesai dibuat dulu."}</div>`;
    return;
  }
  const job = activeJob(["rig_prepare", "rig"]);
  const lastPrep = latest("rig_prepare"), lastRig = latest("rig");
  if (job) {
    body.innerHTML = `<div class="view-grid"><div class="viewer">${progressHTML(job)}</div><div class="side-col">
      <div class="card muted small">Rigging berjalan di Blender (headless). Biasanya selesai dalam hitungan detik setelah giliran tiba.</div></div></div>`;
    return;
  }
  if (a.rig_status === "rigged" && !state.editMarkers) return renderRigged(lastRig);
  if (a.rig_prep) return renderMarkerEditor(lastRig?.status === "error" ? lastRig : lastPrep?.status === "error" ? lastPrep : null);
  // belum disiapkan
  const err = lastPrep?.status === "error" ? `<div class="err-box">${esc(lastPrep.message)}</div>` : "";
  body.innerHTML = `<div class="card" style="max-width:640px">
    <h3>Rigging otomatis</h3>
    <p class="muted">Langkah 1: sistem menebak posisi sendi dari bentuk model. Langkah 2: kamu cek & geser penandanya (dagu, siku, pergelangan, lutut, selangkangan) lalu rig + animasi dibuat.</p>
    <p class="muted small">Cocok untuk karakter humanoid berdiri tegak dengan lengan sedikit terbuka. Hewan berkaki empat / objek belum didukung.</p>
    <label class="field" style="max-width:220px">Tinggi karakter (meter)<input type="number" id="height" value="1.75" min="0.2" max="10" step="0.05"></label>
    <p></p><button class="btn primary" id="prepBtn">Tebak posisi sendi</button> ${err}</div>`;
  $("#prepBtn").addEventListener("click", async () => {
    $("#prepBtn").disabled = true;
    try {
      await api(`api/assets/${a.id}/rig/prepare`, { method: "POST", body: JSON.stringify({ height: +$("#height").value || 1.75 }) });
      openAsset(a.id);
    } catch (err) { alert(err.message); $("#prepBtn").disabled = false; }
  });
}

function renderMarkerEditor(errJob) {
  const a = state.current, body = $("#tabBody"), prep = a.rig_prep;
  if (!state.markers) state.markers = structuredClone(a.rig_markers || prep.markers);
  const anims = Object.keys(ANIM_LABEL);
  const chosen = new Set(a.rig_report?.animations?.map((x) => x.name) || anims);
  body.innerHTML = `<div class="view-grid">
    <div><div class="marker-wrap" id="mw"><img src="${a.files["rig/front.png"]}?v=${Date.now()}" alt="Tampak depan"><svg id="mkSvg"></svg></div></div>
    <div class="side-col">
      ${errJob ? `<div class="err-box">${esc(errJob.message)}</div>` : ""}
      ${warningsHTML(prep.warnings)}
      <div class="card"><h3>Atur penanda sendi</h3>
        <p class="muted small">Geser titik ke posisi yang benar pada model. Ini tampak <b>depan</b>: sisi kiri karakter ada di kanan layar.</p>
        <div class="legend">${MARKERS.map((m) => `<span><i style="background:${m.color}"></i>${m.label}</span>`).join("")}</div>
        <p></p><label class="check"><input type="checkbox" id="sym" ${state.symmetric ? "checked" : ""}> Simetris (geser satu sisi, sisi lain ikut)</label>
        <p></p><button class="btn sm" id="resetMk">Kembalikan tebakan otomatis</button>
      </div>
      <div class="card"><h3>Animasi</h3><div class="anim-grid">${anims.map((n) =>
        `<label class="check"><input type="checkbox" class="anim" value="${n}" ${chosen.has(n) ? "checked" : ""}> ${ANIM_LABEL[n]}</label>`).join("")}</div>
        <p></p><label class="field">Tinggi karakter (meter)<input type="number" id="height" value="${prep.height_m || 1.75}" min="0.2" max="10" step="0.05"></label>
      </div>
      ${customAnimCardHTML(a)}
      <button class="btn primary wide" id="rigBtn">Buat rig + animasi</button>
      ${a.rig_status === "rigged" ? '<button class="btn ghost wide" id="backRig">← Kembali ke hasil rig</button>' : ""}
    </div></div>`;
  drawMarkers();
  wireCustomAnims(a);
  $("#sym").addEventListener("change", (e) => { state.symmetric = e.target.checked; });
  $("#resetMk").addEventListener("click", () => { state.markers = structuredClone(prep.markers); drawMarkers(); });
  $("#backRig")?.addEventListener("click", () => { state.editMarkers = false; renderTab(); });
  $("#rigBtn").addEventListener("click", async () => {
    $("#rigBtn").disabled = true;
    const animations = [...document.querySelectorAll(".anim:checked")].map((x) => x.value);
    try {
      await api(`api/assets/${a.id}/rig`, { method: "POST", body: JSON.stringify({ markers: state.markers, animations, height: +$("#height").value || 1.75 }) });
      state.editMarkers = false;
      openAsset(a.id);
    } catch (err) { alert(err.message); $("#rigBtn").disabled = false; }
  });
}

function drawMarkers() {
  const f = state.current.rig_prep.frame, svg = $("#mkSvg");
  svg.setAttribute("viewBox", "0 0 1000 1000");
  const toPx = ([x, z]) => [((x - f.cx) / f.size + 0.5) * 1000, (0.5 - (z - f.cz) / f.size) * 1000];
  const toWorld = (u, v) => [+(f.cx + (u / 1000 - 0.5) * f.size).toFixed(4), +(f.cz + (0.5 - v / 1000) * f.size).toFixed(4)];
  const bone = (a, b) => { const [x1, y1] = toPx(state.markers[a]), [x2, y2] = toPx(state.markers[b]);
    return `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="#fff" stroke-opacity=".5" stroke-width="3" stroke-dasharray="6 6"/>`; };
  svg.innerHTML = [["chin", "groin"], ["elbow_l", "wrist_l"], ["elbow_r", "wrist_r"], ["groin", "knee_l"], ["groin", "knee_r"]].map(([a, b]) => bone(a, b)).join("") +
    MARKERS.map((m) => {
      const [u, v] = toPx(state.markers[m.k]);
      const left = u > 600;
      return `<g class="mk" data-k="${m.k}" transform="translate(${u} ${v})"><circle class="hit" r="26"/><circle class="dot" r="11" fill="${m.color}"/>
        <text x="${left ? -16 : 16}" y="5" text-anchor="${left ? "end" : "start"}">${m.label}</text></g>`;
    }).join("");
  svg.querySelectorAll(".mk").forEach((g) => g.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    const k = g.dataset.k;
    g.classList.add("drag");
    const move = (ev) => {
      const r = svg.getBoundingClientRect();
      const u = Math.min(1000, Math.max(0, ((ev.clientX - r.left) / r.width) * 1000));
      const v = Math.min(1000, Math.max(0, ((ev.clientY - r.top) / r.height) * 1000));
      let [x, z] = toWorld(u, v);
      if (k === "chin" || k === "groin") x = 0; // titik tengah selalu di sumbu tengah
      state.markers[k] = [x, z];
      if (state.symmetric && MIRROR[k]) state.markers[MIRROR[k]] = [-x, z];
      drawMarkers();
    };
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); drawMarkers(); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }));
}

function renderRigged(job) {
  const a = state.current, body = $("#tabBody");
  const rep = a.rig_report || {};
  const anims = (rep.animations || []).map((x) => x.name);
  body.innerHTML = `<div class="view-grid">
    <div class="viewer"><model-viewer id="rv" src="${a.files["rig/rigged.glb"]}?v=${job?.finished || 0}" camera-controls autoplay shadow-intensity="1"
      environment-image="neutral" touch-action="pan-y" camera-orbit="25deg 78deg auto" alt="Model ter-rig"></model-viewer>
      <div class="anim-btns">${anims.map((n, i) => `<button class="btn sm ${i === 0 ? "on" : ""}" data-anim="${esc(n)}">${esc(ANIM_LABEL[n] || n)}</button>`).join("")}</div></div>
    <div class="side-col">
      ${warningsHTML(rep.warnings)}
      <div class="card"><h3>Rig</h3><dl class="stats">
        <dt>Tulang</dt><dd>${rep.bones?.length || 0} (nama standar Mixamo)</dd>
        <dt>Tinggi</dt><dd>${rep.height_m} m</dd>
        <dt>Animasi</dt><dd>${anims.length}</dd></dl></div>
      ${downloadsHTML(a.downloads.filter((d) => d.url.includes("/rig/")))}
      ${customAnimCardHTML(a)}
      <button class="btn wide" id="editMk">Ubah penanda & rig ulang</button>
    </div></div>`;
  wireCustomAnims(a);
  const mv = $("#rv");
  mv.addEventListener("load", () => { if (anims[0]) { mv.animationName = anims[0]; mv.play(); } });
  body.querySelectorAll("[data-anim]").forEach((b) => b.addEventListener("click", () => {
    body.querySelectorAll("[data-anim]").forEach((x) => x.classList.toggle("on", x === b));
    mv.animationName = b.dataset.anim;
    mv.currentTime = 0;
    mv.play();
  }));
  $("#editMk").addEventListener("click", () => { state.editMarkers = true; state.markers = null; renderTab(); });
}

// ------------------------------------------------------------------ mulai
async function boot() {
  const me = await api("api/me");
  if (!me.authed) return showLogin();
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  await refreshList();
  route();
  refreshQueue();
  setInterval(() => { refreshQueue(); if (!state.current) refreshList(); }, 5000);
}
boot();
