"use strict";

const $ = (id) => document.getElementById(id);
const state = { me: null, members: [], playlists: [], currentId: null, pl: null, keys: new Set() };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const thumb = (key) => `/api/thumb/${encodeURIComponent(key)}`;
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
function dur(ms) {
  const s = Math.round((ms || 0) / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
function longDur(ms) {
  const m = Math.round((ms || 0) / 60000);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}
function ago(t) {
  if (!t) return "never";
  const s = Date.now() / 1000 - t;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  return new Date(t * 1000).toLocaleString();
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (r.status === 401) { showLogin(); throw new Error("signed out"); }
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    toast(esc(msg));
    throw new Error(msg);
  }
  return r.json();
}

function toast(html) {
  const el = document.createElement("div");
  el.className = "toast";
  el.innerHTML = html;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

/* ---------------- sign in ---------------- */

const LOGIN_ERRORS = {
  noaccess: "That Plex account doesn't have access to this server.",
  notapproved: "Plex sign-in wasn't completed. Try again.",
  expired: "Sign-in took too long. Try again.",
};

async function showLogin() {
  $("app").hidden = true;
  $("login").hidden = false;
  const err = new URLSearchParams(location.search).get("error");
  if (err) { $("login-error").textContent = LOGIN_ERRORS[err] || "Sign-in failed."; $("login-error").hidden = false; }
  const cfg = await fetch("/api/config").then((r) => r.json());
  if (cfg.demo) {
    $("plex-login").hidden = true;
    $("demo-login").hidden = false;
    $("demo-members").innerHTML = cfg.members
      .map((m) => `<button class="btn" data-id="${esc(m.id)}">${esc(m.title)}</button>`).join("");
    $("demo-members").onclick = async (e) => {
      const id = e.target.dataset.id;
      if (!id) return;
      await fetch("/auth/demo", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ member: id }) });
      location.href = "/";
    };
  }
}

/* ---------------- playlists ---------------- */

async function loadPlaylists() {
  state.playlists = await api("/api/playlists");
  renderSidebar();
  if (!state.playlists.length) {
    state.currentId = null;
    $("playlist").hidden = true;
    $("empty").hidden = false;
    return;
  }
  $("empty").hidden = true;
  if (!state.playlists.some((p) => p.id === state.currentId)) {
    const fromHash = Number(new URLSearchParams(location.hash.slice(1)).get("p"));
    state.currentId = state.playlists.some((p) => p.id === fromHash) ? fromHash : state.playlists[0].id;
    await loadPlaylist();
  }
}

function renderSidebar() {
  $("pl-list").innerHTML = state.playlists.map((p) => `
    <a class="pl-item ${p.id === state.currentId ? "active" : ""}" href="#p=${p.id}" data-id="${p.id}">
      ${p.first_key ? `<img class="art" src="${thumb(p.first_key)}" alt="" loading="lazy">` : `<div class="art" style="background:var(--panel-2)"></div>`}
      <div><div class="t">${esc(p.name)}</div><div class="s">${plural(p.count, "song")}${p.waiting ? ` (+${p.waiting} waiting)` : ""} · ${p.people === 1 ? "just you" : plural(p.people, "person").replace("persons", "people")}</div></div>
    </a>`).join("");
}

async function loadPlaylist(flashKeys) {
  if (state.currentId == null) return;
  let pl;
  try { pl = await api(`/api/playlists/${state.currentId}`); }
  catch (e) { state.currentId = null; return loadPlaylists(); }
  state.pl = pl;
  state.keys = new Set(pl.items.map((t) => t.key));
  history.replaceState(null, "", `#p=${pl.id}`);
  renderPlaylist(flashKeys);
  renderSidebar();
}

function renderPlaylist(flashKeys = new Set()) {
  const pl = state.pl;
  $("playlist").hidden = false;
  $("empty").hidden = true;
  if ($("pl-name").contentEditable !== "true") $("pl-name").textContent = pl.name;
  const total = pl.items.reduce((s, t) => s + (t.duration || 0), 0);
  $("pl-meta").textContent = `${plural(pl.items.length, "song")} · ${longDur(total)} · started by ${pl.created_by}`;

  const albums = [];
  for (const t of pl.items) {
    if (!albums.some((a) => a.album === t.album)) albums.push(t);
    if (albums.length === 4) break;
  }
  const cover = albums.length >= 4 ? albums : albums.slice(0, 1);
  $("pl-cover").className = "pl-cover" + (cover.length === 1 ? " one" : "");
  $("pl-cover").innerHTML = cover.map((t) => `<img src="${thumb(t.key)}" alt="">`).join("");

  $("pl-mirrors").innerHTML = pl.mirrors.map((m) => {
    let tip = m.state === "synced" ? `In ${m.title}'s Plex · synced ${ago(m.synced_at)}` : m.state === "error" ? m.error : "Syncing…";
    if (m.missing) tip += ` · ${plural(m.missing, "song")} not in their libraries`;
    const name = m.id === state.me.id ? `${m.title} (you)` : m.title;
    const x = pl.mirrors.length > 1
      ? `<button class="chip-x" data-unshare="${esc(m.id)}" title="Remove ${esc(m.title)}" aria-label="Remove ${esc(m.title)}">×</button>` : "";
    return `<span class="chip ${m.state}" title="${esc(tip)}">${esc(name)}${x}</span>`;
  }).join("") + `<button id="add-people" class="chip add">+ Add people</button>`;
  if (!$("picker").hidden) renderPicker();

  renderWaiting(pl);
  $("no-tracks").hidden = pl.items.length > 0 || pl.waiting.length > 0;
  $("tracks").innerHTML = pl.items.map((t, i) => `
    <li class="row ${flashKeys.has(t.key) ? "flash" : ""}" draggable="true" data-key="${esc(t.key)}" data-i="${i}">
      <span class="num" title="Drag to reorder">${i + 1}</span>
      <img class="art" src="${thumb(t.key)}" alt="" loading="lazy">
      <div style="min-width:0"><div class="t">${esc(t.title)}</div><div class="a">${esc(t.artist)}</div></div>
      <span class="al">${esc(t.album)}</span>
      <span class="by" title="Added ${esc(new Date(t.added_at * 1000).toLocaleString())}">${esc(t.added_by)}</span>
      <span class="dur">${dur(t.duration)}</span>
      <button class="btn ghost icon x" data-remove="${esc(t.key)}" title="Remove" aria-label="Remove ${esc(t.title)}">×</button>
    </li>`).join("");
}

const WAIT_STATE = { missing: "Not in Plex", requested: "Downloading", failed: "Download failed" };
function renderWaiting(pl) {
  const w = pl.waiting || [];
  $("waiting").hidden = !w.length;
  if (!w.length) return;
  $("waiting-title").textContent = `Waiting for ${plural(w.length, "song")}`;
  const dl = state.me.can_download;
  $("download-all").hidden = !dl || !w.some((x) => x.status !== "requested");
  $("download-all").textContent = `Download all with ${state.me.downloader || ""}`;
  $("waiting-list").innerHTML = w.map((x) => `
    <li class="wait ${x.status}">
      <div style="min-width:0"><div class="t">${esc(x.title)}</div>
        <div class="a">${esc(x.artist)}${x.album ? " · " + esc(x.album) : ""}</div></div>
      <span class="wstate" title="${esc(x.note || "")}">${WAIT_STATE[x.status] || esc(x.status)}${x.note && x.status === "failed" ? ": " + esc(x.note) : ""}</span>
      <span class="cat-acts">
        ${dl && x.status !== "requested" ? `<button class="btn" data-dl="${x.id}">${x.status === "failed" ? "Retry" : "Download"}</button>` : ""}
        <button class="btn ghost icon x" data-forget="${x.id}" title="Stop waiting for it" aria-label="Stop waiting for ${esc(x.title)}">×</button>
      </span>
    </li>`).join("");
}

$("waiting-list").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b || !state.pl) return;
  b.disabled = true;
  try {
    if (b.dataset.dl) {
      const r = await api(`/api/playlists/${state.pl.id}/waiting/${b.dataset.dl}/download`, { method: "POST" });
      toast(esc(r.note));
    } else if (b.dataset.forget) {
      await api(`/api/playlists/${state.pl.id}/waiting/${b.dataset.forget}`, { method: "DELETE" });
    }
  } catch (_) {}
  loadPlaylist();
});
$("download-all").onclick = async () => {
  if (!confirm(`Ask ${state.me.downloader} to download ${plural(state.pl.waiting.filter((x) => x.status !== "requested").length, "song")}?`)) return;
  $("download-all").disabled = true;
  try {
    const r = await api(`/api/playlists/${state.pl.id}/waiting/download-all`, { method: "POST" });
    toast(`Sent ${plural(r.sent, "song")} to ${esc(state.me.downloader)}${r.failed ? `; ${r.failed} couldn't be found` : ""}.`);
  } catch (_) {}
  $("download-all").disabled = false;
  loadPlaylist();
};
$("check-waiting").onclick = async () => {
  $("check-waiting").disabled = true;
  try {
    const r = await api(`/api/playlists/${state.pl.id}/waiting/check`, { method: "POST" });
    toast(r.added ? `${plural(r.added, "song")} found in Plex and added` : "None of them are in Plex yet.");
  } catch (_) {}
  $("check-waiting").disabled = false;
  loadPlaylist();
};

/* ---------------- importing from Spotify / Apple Music / Deezer ---------------- */

$("import-pl").onclick = () => {
  $("ext-box").hidden = !$("ext-box").hidden;
  $("new-box").hidden = true;
  if (!$("ext-box").hidden) $("ext-url").focus();
};
$("empty-import").onclick = () => { $("ext-box").hidden = true; $("import-pl").click(); };
let extJob = null;
$("ext-form").onsubmit = async (e) => {
  e.preventDefault();
  const url = $("ext-url").value.trim();
  if (!url) return;
  $("ext-result").hidden = true;
  $("ext-go").disabled = true;
  $("ext-status").textContent = "Reading the playlist…";
  try {
    extJob = (await api("/api/import", { method: "POST", body: { url } })).job;
    pollImport(extJob);
  } catch (err) {
    $("ext-status").textContent = err.message;
    $("ext-go").disabled = false;
  }
};
async function pollImport(job) {
  let j;
  try { j = await api(`/api/import/${job}`); }
  catch (err) { $("ext-status").textContent = err.message; $("ext-go").disabled = false; return; }
  if (job !== extJob) return;
  if (j.state === "running") {
    $("ext-status").textContent = j.total ? `Looking for the songs in your Plex… ${j.done} of ${j.total}` : "Reading the playlist…";
    return setTimeout(() => pollImport(job), 700);
  }
  $("ext-go").disabled = false;
  if (j.state === "error") { $("ext-status").textContent = j.error; return; }
  const r = j.result, found = r.items.filter((i) => i.match).length, missing = r.items.filter((i) => !i.match);
  $("ext-status").textContent = r.note || "";
  $("ext-name").value = r.name;
  $("ext-summary").textContent = `${r.source}: ${found} of ${plural(r.items.length, "song")} are in your Plex.` +
    (missing.length ? " Not found:" : "");
  $("ext-missing").innerHTML = missing.map((i) => `<div class="mini"><b>${esc(i.title)}</b> · ${esc(i.artist)}</div>`).join("");
  $("ext-missing").hidden = !missing.length;
  $("ext-keep").parentElement.hidden = !missing.length;
  $("ext-keep-label").textContent = `Keep the ${plural(missing.length, "missing song")} waiting` +
    (state.me.can_download ? ` (you can then download them with ${state.me.downloader})` : " (added when they show up in Plex)");
  $("ext-result").hidden = false;
}
$("ext-create").onclick = async () => {
  $("ext-create").disabled = true;
  try {
    const r = await api(`/api/import/${extJob}/create`, {
      method: "POST", body: { name: $("ext-name").value, keep_missing: $("ext-keep").checked },
    });
    toast(`Created with ${plural(r.added, "song")}${r.waiting ? `, ${r.waiting} waiting` : ""}. Now pick who it's for.`);
    $("ext-box").hidden = true;
    $("ext-result").hidden = true;
    $("ext-url").value = "";
    $("ext-status").textContent = "";
    extJob = null;
    openNew(r.id);
  } catch (_) {}
  $("ext-create").disabled = false;
};

$("pl-list").addEventListener("click", (e) => {
  const a = e.target.closest(".pl-item");
  if (!a) return;
  e.preventDefault();
  state.currentId = Number(a.dataset.id);
  closeResults();
  loadPlaylist();
});

$("empty-new").onclick = () => $("new-pl").click();
$("new-pl").onclick = () => {
  $("new-box").hidden = !$("new-box").hidden;
  $("import-box").hidden = true;
  $("leftover-box").hidden = true;
  $("ext-box").hidden = true;
  if (!$("new-box").hidden) $("new-name").focus();
};
async function openNew(id) {
  $("new-name").value = "";
  $("new-box").hidden = true;
  state.currentId = id;
  await loadPlaylists();
  await loadPlaylist();
  openPicker(); // new playlists start with just you: pick who it's for
}
$("new-form").onsubmit = async (e) => {
  e.preventDefault();
  const name = $("new-name").value.trim();
  if (!name) return;
  const { id } = await api("/api/playlists", { method: "POST", body: { name } });
  openNew(id);
};

/* ---------------- sharing an existing Plex playlist ---------------- */

const KEEP_HINT = {
  false: "Your playlist becomes the shared one. It's renamed with 👥 and changes made by others show up in it.",
  true: "Your playlist stays as it is, and the shared one is a separate copy.",
};
function updateKeepHint() { $("keep-hint").textContent = KEEP_HINT[$("keep-original").checked]; }
$("keep-original").onchange = updateKeepHint;

$("show-import").onclick = async () => {
  const box = $("import-box");
  box.hidden = !box.hidden;
  if (box.hidden) return;
  updateKeepHint();
  $("import-list").innerHTML = `<div class="none">Loading your playlists…</div>`;
  let pls;
  try { pls = await api("/api/plex-playlists"); }
  catch (_) { $("import-list").innerHTML = `<div class="none">Couldn't load your playlists.</div>`; return; }
  $("import-list").innerHTML = pls.length ? pls.map((p) => `
    <button class="import-item" data-import="${esc(p.key)}" data-smart="${p.smart ? 1 : 0}" title="Share “${esc(p.title)}”">
      <div><div class="t">${esc(p.title)}</div>
      <div class="s">${plural(p.count, "song")}${p.smart ? " · smart playlist, shared as a copy" : ""}</div></div>
      <span class="go">Share</span>
    </button>`).join("")
    : `<div class="none">You don't have any music playlists in Plex yet.</div>`;
};

async function loadLeftovers() {
  const list = $("leftover-list");
  list.innerHTML = `<div class="none">Looking through everyone's Plex…</div>`;
  let items;
  try { items = await api("/api/leftovers"); }
  catch (_) { list.innerHTML = `<div class="none">Couldn't check everyone's Plex.</div>`; return; }
  list.innerHTML = items.length ? items.map((l) => {
    const who = l.people.length > 3 ? `${l.people.slice(0, 3).join(", ")} +${l.people.length - 3}` : l.people.join(", ");
    return `
    <div class="import-item leftover" title="In the Plex of: ${esc(l.people.join(", "))}">
      <div><div class="t">👥 ${esc(l.name)}</div>
      <div class="s">${plural(l.people.length, "person").replace("persons", "people")} (${esc(who)}) · ${plural(l.songs, "song")}${l.in_jukebox ? " · extra copies of a playlist that's here" : ""}</div></div>
      <span class="acts">
        ${l.in_jukebox || !l.songs ? "" : `<button class="go" data-restore="${esc(l.name)}">Bring back</button>`}
        <button class="go danger" data-del="${esc(l.name)}" data-n="${l.copies}">Delete</button>
      </span>
    </div>`;
  }).join("") : `<div class="none">No leftovers: everyone's 👥 playlists are in sync.</div>`;
}

async function loadOthers() {
  let items = [];
  try { items = await api("/api/others"); } catch (_) {}
  $("others-wrap").hidden = !items.length;
  $("others-list").innerHTML = items.map((p) => `
    <div class="import-item leftover" title="Shared with: ${esc(p.people.join(", "))}">
      <div><div class="t">${esc(p.name)}</div>
      <div class="s">${plural(p.people.length, "person").replace("persons", "people")} · ${plural(p.count, "song")}</div></div>
      <span class="acts">
        <button class="go" data-join="${p.id}">Join</button>
        <button class="go danger" data-delpl="${p.id}" data-name="${esc(p.name)}">Delete</button>
      </span>
    </div>`).join("");
}

$("show-leftovers").onclick = () => {
  const box = $("leftover-box");
  box.hidden = !box.hidden;
  if (!box.hidden) { loadLeftovers(); loadOthers(); }
};

$("others-list").addEventListener("click", async (e) => {
  const join = e.target.closest("[data-join]");
  const del = e.target.closest("[data-delpl]");
  if (!join && !del) return;
  if (del && !confirm(`Delete “${del.dataset.name}” for everyone? It'll be removed from everyone's Plex too.`)) return;
  $("others-list").querySelectorAll("button").forEach((x) => (x.disabled = true));
  try {
    if (join) {
      await api(`/api/others/${join.dataset.join}/join`, { method: "POST" });
      state.currentId = Number(join.dataset.join);
      await loadPlaylists();
      await loadPlaylist();
      toast("You're in. It'll show up in your Plex too.");
    } else {
      await api(`/api/others/${del.dataset.delpl}`, { method: "DELETE" });
      toast(`Deleted “${esc(del.dataset.name)}”. It's being removed from everyone's Plex.`);
    }
  } catch (_) {}
  loadOthers();
});

$("leftover-list").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del]");
  const res = e.target.closest("[data-restore]");
  if (!del && !res) return;
  if (del && !confirm(`Delete “👥 ${del.dataset.del}” from everyone's Plex (${plural(Number(del.dataset.n), "copy").replace("copys", "copies")})? This can't be undone.`)) return;
  $("leftover-list").querySelectorAll("button").forEach((x) => (x.disabled = true));
  try {
    if (del) {
      const r = await api("/api/leftovers/delete", { method: "POST", body: { name: del.dataset.del } });
      toast(`Deleted from ${plural(r.deleted, "person's Plex").replace("person's Plexs", "people's Plex")}` +
        (r.queued ? `. ${plural(r.queued, "person")}'s Plex didn't answer; try again later.` : ""));
    } else {
      const r = await api("/api/leftovers/restore", { method: "POST", body: { name: res.dataset.restore } });
      toast(`Brought back “${esc(r.names[0])}”`);
      await loadPlaylists();
    }
  } catch (_) { /* api() already showed the error */ }
  loadLeftovers();
});

$("import-list").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-import]");
  if (!b) return;
  $("import-list").querySelectorAll("button").forEach((x) => (x.disabled = true));
  b.querySelector(".go").textContent = "Sharing…";
  try {
    const r = await api("/api/playlists/import", {
      method: "POST", body: { key: b.dataset.import, keep_original: $("keep-original").checked },
    });
    toast(`Shared ${plural(r.count, "song")}. Now pick who it's for.`);
    $("import-box").hidden = true;
    openNew(r.id);
  } catch (_) {
    $("import-list").querySelectorAll("button").forEach((x) => (x.disabled = false));
    b.querySelector(".go").textContent = "Share";
  }
});

/* ---------------- sharing ---------------- */

function openPicker() {
  $("picker").hidden = false;
  $("picker-q").value = "";
  renderPicker();
  $("picker-q").focus();
}
function closePicker() { $("picker").hidden = true; }

function renderPicker() {
  const inIt = new Set(state.pl.mirrors.map((m) => m.id));
  const q = $("picker-q").value.trim().toLowerCase();
  const people = state.members.filter((m) => !inIt.has(m.id) && m.title.toLowerCase().includes(q));
  $("picker-list").innerHTML = people.length
    ? people.map((m) => `<button class="picker-item" data-share="${esc(m.id)}"><span>${esc(m.title)}</span><span class="plus">+</span></button>`).join("")
    : `<div class="none">${state.members.length === inIt.size ? "Everyone on your server is already in it." : "Nobody matches."}</div>`;
}

$("picker-q").addEventListener("input", renderPicker);
$("picker-list").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-share]");
  if (!b) return;
  b.disabled = true;
  await api(`/api/playlists/${state.pl.id}/members`, { method: "POST", body: { member: b.dataset.share } });
  await loadPlaylist();
  $("picker-q").focus();
});

$("pl-mirrors").addEventListener("click", async (e) => {
  if (e.target.id === "add-people") {
    e.stopPropagation();
    return $("picker").hidden ? openPicker() : closePicker();
  }
  const id = e.target.dataset.unshare;
  if (!id) return;
  const m = state.pl.mirrors.find((x) => x.id === id);
  const msg = id === state.me.id
    ? `Leave “${state.pl.name}”? It'll be removed from your Plex and you won't see it here any more.`
    : `Remove ${m.title} from “${state.pl.name}”? It'll be removed from their Plex.`;
  if (!confirm(msg)) return;
  await api(`/api/playlists/${state.pl.id}/members/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (id === state.me.id) { state.currentId = null; loadPlaylists(); } else loadPlaylist();
});

document.addEventListener("click", (e) => { if (!e.target.closest(".picker")) closePicker(); });

$("pl-name").onclick = () => {
  const el = $("pl-name");
  if (el.contentEditable === "true") return;
  el.contentEditable = "true";
  el.focus();
  document.getSelection().selectAllChildren(el);
};
$("pl-name").onkeydown = (e) => {
  if (e.key === "Enter") { e.preventDefault(); e.target.blur(); }
  if (e.key === "Escape") { e.target.textContent = state.pl.name; e.target.blur(); }
};
$("pl-name").onblur = async () => {
  const el = $("pl-name");
  el.contentEditable = "false";
  const name = el.textContent.trim();
  if (!name || name === state.pl.name) { el.textContent = state.pl.name; return; }
  await api(`/api/playlists/${state.pl.id}`, { method: "PATCH", body: { name } });
};

$("pl-delete").onclick = async () => {
  if (!confirm(`Delete “${state.pl.name}” for everyone? It'll be removed from everyone's Plex too.`)) return;
  await api(`/api/playlists/${state.pl.id}`, { method: "DELETE" });
  state.currentId = null;
  loadPlaylists();
};

$("tracks").addEventListener("click", async (e) => {
  const key = e.target.dataset.remove;
  if (!key) return;
  e.target.closest(".row").style.opacity = ".3";
  await api(`/api/playlists/${state.pl.id}/tracks/${encodeURIComponent(key)}`, { method: "DELETE" });
});

/* ---------------- drag to reorder ---------------- */

let dragKey = null;
const tracksEl = $("tracks");
const clearDrop = () => tracksEl.querySelectorAll(".drop-before,.drop-after").forEach((r) => r.classList.remove("drop-before", "drop-after"));
tracksEl.addEventListener("dragstart", (e) => {
  const row = e.target.closest(".row");
  if (!row) return;
  dragKey = row.dataset.key;
  row.classList.add("dragging");
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", dragKey);
});
tracksEl.addEventListener("dragend", (e) => {
  e.target.closest(".row")?.classList.remove("dragging");
  clearDrop();
  dragKey = null;
});
tracksEl.addEventListener("dragover", (e) => {
  const row = e.target.closest(".row");
  if (!row || dragKey == null) return;
  e.preventDefault();
  clearDrop();
  const r = row.getBoundingClientRect();
  row.classList.add(e.clientY < r.top + r.height / 2 ? "drop-before" : "drop-after");
});
tracksEl.addEventListener("drop", async (e) => {
  const row = e.target.closest(".row");
  if (!row || dragKey == null) return;
  e.preventDefault();
  const after = row.classList.contains("drop-after");
  clearDrop();
  const items = state.pl.items;
  const from = items.findIndex((t) => t.key === dragKey);
  let before = Number(row.dataset.i) + (after ? 1 : 0); // insert before this index of the old list
  const index = before > from ? before - 1 : before;
  if (from < 0 || index === from) return;
  const [moved] = items.splice(from, 1); // show it right away; the server confirms
  items.splice(index, 0, moved);
  renderPlaylist();
  await api(`/api/playlists/${state.pl.id}/move`, { method: "POST", body: { key: moved.key, index } });
});

/* ---------------- search ---------------- */

let searchTimer = null, searchSeq = 0;
$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 220);
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeResults(); closePicker(); } });
$("search").addEventListener("focus", () => { if ($("results").innerHTML && $("search").value.trim().length > 1) $("results").hidden = false; });
document.addEventListener("click", (e) => { if (!e.target.closest(".search")) closeResults(); });
function closeResults() { $("results").hidden = true; }

async function runSearch() {
  const q = $("search").value.trim();
  if (q.length < 2) return closeResults();
  const seq = ++searchSeq;
  const res = await api(`/api/search?q=${encodeURIComponent(q)}`);
  if (seq !== searchSeq) return;
  renderResults(res);
}

function renderResults(res) {
  let html = "";
  if (res.albums.length) {
    html += `<h3>Albums</h3>` + res.albums.map((a) => `
      <div class="row result">
        <img class="art" src="${thumb(a.key)}" alt="" loading="lazy">
        <div style="min-width:0"><div class="t">${esc(a.title)}</div><div class="a">${esc(a.artist)}${a.year ? " · " + a.year : ""} · ${plural(a.count || 0, "song")}</div></div>
        <span></span>
        <button class="btn icon" data-album="${esc(a.key)}" title="Add whole album" aria-label="Add album ${esc(a.title)}">+</button>
      </div>`).join("");
  }
  if (res.tracks.length) {
    html += `<h3>Songs</h3>` + res.tracks.map((t) => `
      <div class="row result">
        <img class="art" src="${thumb(t.key)}" alt="" loading="lazy">
        <div style="min-width:0"><div class="t">${esc(t.title)}</div><div class="a">${esc(t.artist)} · ${esc(t.album)}</div></div>
        <span class="dur">${dur(t.duration)}</span>
        ${state.keys.has(t.key)
          ? `<span class="in" title="Already in the playlist">✓</span>`
          : `<button class="btn icon" data-track="${esc(t.key)}" title="Add" aria-label="Add ${esc(t.title)}">+</button>`}
      </div>`).join("");
  }
  html = html || `<div class="none">Nothing in your Plex.</div>`;
  html += `<div class="catalog-wrap"><button class="link-btn" data-catalog="1">Not in Plex? Search Deezer for it…</button></div>`;
  $("results").innerHTML = html;
  $("results").hidden = false;
}

async function searchCatalog(wrap) {
  const q = $("search").value.trim();
  wrap.innerHTML = `<div class="none">Searching Deezer…</div>`;
  let res;
  try { res = await api(`/api/catalog?q=${encodeURIComponent(q)}`); }
  catch (_) { wrap.innerHTML = `<div class="none">Couldn't search Deezer.</div>`; return; }
  state.catalog = res;
  const dl = state.me.can_download;
  wrap.innerHTML = `<h3>From Deezer (not necessarily in Plex)</h3>` + (res.length ? res.map((t, i) => `
    <div class="row result cat">
      <span class="art blank">♫</span>
      <div style="min-width:0"><div class="t">${esc(t.title)}</div><div class="a">${esc(t.artist)} · ${esc(t.album)}</div></div>
      <span class="dur">${dur(t.duration)}</span>
      <span class="cat-acts">
        ${dl ? `<button class="btn" data-get="${i}" title="Download it with ${esc(state.me.downloader)}, and add it to this playlist when it's in Plex">Get</button>` : ""}
        <button class="btn icon" data-want="${i}" title="Add it (now if it's in Plex, otherwise when it shows up)" aria-label="Add ${esc(t.title)}">+</button>
      </span>
    </div>`).join("") : `<div class="none">Deezer doesn't have it either.</div>`);
}

$("results").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b || !state.pl) return;
  if (b.dataset.catalog) { e.stopPropagation(); return searchCatalog(b.parentElement); }
  if (b.dataset.want || b.dataset.get) {
    const t = state.catalog[Number(b.dataset.want || b.dataset.get)];
    b.disabled = true;
    try {
      const r = await api(`/api/playlists/${state.pl.id}/want`, { method: "POST", body: { ...t, download: !!b.dataset.get } });
      toast(r.added ? `Added “${esc(t.title)}”: it was already in Plex`
        : r.download ? `${esc(r.download.note)}. It'll be added when it's in Plex.`
        : `“${esc(t.title)}” isn't in Plex yet. It'll be added when it shows up.`);
      b.closest(".cat-acts").innerHTML = `<span class="in">✓</span>`;
      loadPlaylist();
    } catch (_) { b.disabled = false; }
    return;
  }
  b.disabled = true;
  if (b.dataset.track) {
    await api(`/api/playlists/${state.pl.id}/tracks`, { method: "POST", body: { keys: [b.dataset.track] } });
    b.outerHTML = `<span class="in">✓</span>`;
  } else if (b.dataset.album) {
    const r = await api(`/api/playlists/${state.pl.id}/album`, { method: "POST", body: { album: b.dataset.album } });
    toast(r.added ? `Added ${plural(r.added, "song")}` : "Those songs are already in the playlist");
    b.disabled = false;
  }
});

/* ---------------- live updates ---------------- */

let ws, wsRetry = 1000, refreshTimer = null;
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { $("live").classList.add("on"); wsRetry = 1000; };
  ws.onclose = () => {
    $("live").classList.remove("on");
    setTimeout(connect, wsRetry);
    wsRetry = Math.min(wsRetry * 2, 15000);
  };
  ws.onmessage = (e) => onLive(JSON.parse(e.data));
}

function onLive(msg) {
  if (msg.type === "playlist" && msg.who && msg.who !== state.me.title) {
    const p = state.playlists.find((x) => x.id === msg.id);
    toast(`<b>${esc(msg.who)}</b> ${esc(msg.action)}${p && msg.id !== state.currentId ? ` in ${esc(p.name)}` : ""}`);
  }
  // Coalesce bursts of messages into one refresh.
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(async () => {
    const before = new Set(state.keys);
    await loadPlaylists();
    if (state.currentId != null && (msg.id === state.currentId || msg.type === "sync")) {
      await loadPlaylist();
      const fresh = new Set([...state.keys].filter((k) => !before.has(k)));
      if (fresh.size && msg.who !== state.me.title) {
        tracksEl.querySelectorAll(".row").forEach((r) => { if (fresh.has(r.dataset.key)) r.classList.add("flash"); });
      }
    }
  }, 150);
}

// When the tab comes back, catch up on anything missed.
document.addEventListener("visibilitychange", () => { if (!document.hidden && state.me) loadPlaylists().then(() => loadPlaylist()); });

/* ---------------- start ---------------- */

(async function init() {
  try { state.me = await api("/api/me"); } catch (_) { return; }
  const cfg = await api("/api/config");
  $("demo-badge").hidden = !cfg.demo;
  $("me-name").textContent = state.me.title;
  $("show-leftovers").hidden = !state.me.is_owner;
  $("search").value = ""; // browsers restore old input on reload
  $("login").hidden = true;
  $("app").hidden = false;
  state.members = await api("/api/members");
  await loadPlaylists();
  connect();
})();
