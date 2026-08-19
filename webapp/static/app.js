/* 旅程回忆 前端逻辑 — 步骤化单链路 */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
})[char]);

const state = {
  cities: [],
  gallery: null,       // 当前预设城市的画廊元数据
  gallerySlug: null,   // 画廊对应的服务端区域身份
  galleryBbox: null,   // 画廊生成时的原始 bbox；生成模型必须复用
  renderKind: "topdown",
  selectedStyle: null,
  generationProfile: "classic",
  authConfig: null,
  account: null,
  job: null,           // {id, mode}
  /* 当前目标：预设城市 or 自定义区域 */
  target: { kind: "area", city: null, title: "自定义区域" },
  cityGroupFilter: "",   // 国家分组筛选
  cityTextFilter: "",    // 文字筛选
  viewLocked: false,     // 确定取景后锁定，避免风格图与取景不匹配
};

/* ---------------- 会话持久化（localStorage）---------------- */
// 用户关掉页面再回来，自动恢复上次状态，不浪费已渲染的产物。
// 不存照片原图（隐私），只存 GPS 坐标 + 文件名 + city slug。

const SESSION_KEY = "jr_session";
let _sessionCache = null;   // 模块级缓存，避免递归

function getSession() {
  if (_sessionCache) return _sessionCache;
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (raw) { _sessionCache = JSON.parse(raw); return _sessionCache; }
  } catch (_) {}
  // 首次访问：生成匿名 session（id 去连字符，服务端校验 isalnum）
  _sessionCache = {
    id: (crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2)).replace(/-/g, ""),
    createdAt: Date.now(),
    target: null,          // {kind, city, title, prototype}
    selectedStyle: null,
    generationProfile: "classic",
    areaName: "",
    photoPoints: [],       // [{lat, lon, name}] 照片 GPS 坐标（不含原图）
    journeyClusters: null, // [{lat, lon, name, count}]
    lastCity: null,        // 最近一次生成/查看的 city slug
    lastBbox: null,        // 最近取景框
  };
  _writeSession();
  return _sessionCache;
}

function _writeSession() {
  try { localStorage.setItem(SESSION_KEY, JSON.stringify(_sessionCache)); } catch (_) {}
}

function saveSession(patch) {
  const s = getSession();
  if (patch) Object.assign(s, patch);
  _writeSession();
}

/** 从持久化状态恢复页面（页面加载时调用） */
async function restoreSession() {
  // 检查 URL ?s=xxx（跨设备恢复）
  const urlParams = new URLSearchParams(window.location.search);
  const cloudSid = urlParams.get("s");
  if (cloudSid && cloudSid.length <= 64 && /^[a-zA-Z0-9]+$/.test(cloudSid)) {
    try {
      const r = await fetch(`/api/session/${cloudSid}`);
      if (r.ok) {
        const cloudData = await r.json();
        // 云端数据覆盖本地
        _sessionCache = cloudData;
        _writeSession();
      }
    } catch (_) {}
  }

  const s = getSession();
  if (!s.lastCity && !s.target) return;  // 全新用户，无需恢复

  // 恢复 target
  if (s.target) {
    state.target = s.target;
  }
  if (s.selectedStyle) {
    state.selectedStyle = s.selectedStyle;
  }
  if (s.generationProfile) {
    state.generationProfile = s.generationProfile;
  }
  if (s.areaName) {
    const el = $("areaName");
    if (el && !el.value) el.value = s.areaName;
  }

  // 恢复地图位置
  if (s.lastBbox) {
    initMap();
    const b = s.lastBbox;
    map.state.map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: [16, 16] });
    setTierNear(kmFromBbox(b));
    syncSize();
    setTimeout(updateRect, 200);
  }

  // 恢复照片标记点
  if (s.photoPoints && s.photoPoints.length) {
    initMap();
    for (const p of s.photoPoints) {
      L.marker([p.lat, p.lon]).addTo(map.state.map)
        .bindPopup(`📷 ${p.name || "照片位置"}`);
    }
  }

  // 恢复旅程轨迹
  if (s.journeyClusters && s.journeyClusters.length) {
    initMap();
    map.journey = { clusters: s.journeyClusters, layerGroup: null };
    redrawJourney();
  }

  // 恢复产物展示
  if (s.lastCity) {
    try {
      const r = await fetchJSON(`/api/artifacts/${s.lastCity}`);
      lastArtifacts = r.artifacts;
      renderViewer();
      renderDownloads();
    } catch (_) {}
    // 恢复画廊
    try {
      state.gallery = await fetchJSON(`/api/gallery/${s.lastCity}`);
      renderStep3();
    } catch (_) {}
  }

  // 提示用户
  const hint = $("photoHint");
  if (hint && s.lastCity) {
    hint.textContent = `已恢复上次会话（${s.lastCity}），产物仍在云端 ✓`;
  }
}

/** 关键操作后自动保存（在 selectCity / confirmArea / pollJob done 等地方调用） */
function persistState() {
  const patch = {
    target: state.target,
    selectedStyle: state.selectedStyle,
    generationProfile: state.generationProfile,
    areaName: ($("areaName") || {}).value || "",
    lastCity: state.target.city || (lastArtifacts ? state.jobSlug : null),
    lastBbox: map.state.map ? currentBbox() : null,
    photoPoints: map.photoPoint ? [{ lat: map.photoPoint[0], lon: map.photoPoint[1], name: "" }] : [],
    journeyClusters: map.journey ? map.journey.clusters.map(c => ({
      lat: c.lat, lon: c.lon, name: c.name, count: c.count,
      dwell_minutes: c.dwell_minutes, manual: c.manual,
    })) : null,
  };
  saveSession(patch);
  // 同步到云端（跨设备恢复）
  const s = getSession();
  fetch("/api/session/save", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: s.id, data: { ...s, ...patch } }),
  }).catch(() => {});  // 静默失败，不阻断用户操作
}

/* ---------------- 数据加载 ---------------- */

async function fetchJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    if (r.status === 401) openAccountDialog();
    throw new Error(msg);
  }
  return r.json();
}

/* ---------------- 账号与我的任务 ---------------- */

function openAccountDialog() {
  const dialog = $("accountDialog");
  if (dialog.open) return;
  if (dialog.showModal) dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeAccountDialog() {
  const dialog = $("accountDialog");
  if (dialog.close) dialog.close();
  else dialog.removeAttribute("open");
}

function renderAccount() {
  const user = state.account;
  $("accountLoginView").hidden = Boolean(user);
  $("accountUserView").hidden = !user;
  $("myTasksCard").hidden = !user;
  if (!user) {
    $("accountLabel").textContent = "登录";
    $("accountQuota").textContent = "保存我的任务";
    return;
  }
  const shortEmail = user.email.length > 24
    ? user.email.slice(0, 21) + "…" : user.email;
  $("accountLabel").textContent = shortEmail;
  $("accountQuota").textContent = `本月剩余 ${user.quota_remaining}`;
  $("accountEmail").textContent = user.email;
  $("accountQuotaLarge").textContent = `${user.quota_remaining} / ${user.quota_limit}`;
  $("accountQuotaPeriod").textContent =
    `${user.quota_period} · 按计算量扣除，缓存结果不重复扣费`;
}

async function refreshAccount() {
  if (window.location.protocol === "file:") return;
  try {
    const config = await fetchJSON("/api/auth/config");
    state.authConfig = config;
    const result = await fetchJSON("/api/auth/me");
    state.account = result.user || null;
    renderAccount();
    if (state.account) await loadMyTasks();
    else if (config.required) openAccountDialog();
  } catch (_) {
    renderAccount();
  }
}

async function sendEmailCode() {
  const email = $("authEmail").value.trim();
  if (!email) { $("authMessage").textContent = "请先输入邮箱"; return; }
  const button = $("btnSendCode");
  button.disabled = true;
  $("authMessage").textContent = "正在发送…";
  try {
    const result = await fetchJSON("/api/auth/email/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    $("authMessage").textContent = result.message;
    if (result.dev_code) $("authCode").value = result.dev_code;
    let left = 60;
    button.textContent = `${left}s 后重发`;
    const timer = setInterval(() => {
      left -= 1;
      button.textContent = left > 0 ? `${left}s 后重发` : "获取验证码";
      if (left <= 0) { clearInterval(timer); button.disabled = false; }
    }, 1000);
  } catch (error) {
    $("authMessage").textContent = error.message;
    button.disabled = false;
  }
}

async function verifyEmailCode() {
  const email = $("authEmail").value.trim();
  const code = $("authCode").value.trim();
  if (!email || !/^\d{6}$/.test(code)) {
    $("authMessage").textContent = "请输入邮箱和 6 位验证码";
    return;
  }
  try {
    const result = await fetchJSON("/api/auth/email/verify", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, code }),
    });
    state.account = result.user;
    renderAccount();
    await loadMyTasks();
    closeAccountDialog();
  } catch (error) {
    $("authMessage").textContent = error.message;
  }
}

async function logoutAccount() {
  await fetchJSON("/api/auth/logout", { method: "POST" });
  state.account = null;
  renderAccount();
}

async function loadMyTasks() {
  if (!state.account) return;
  try {
    const result = await fetchJSON("/api/jobs?mine=true");
    const jobs = result.jobs || [];
    $("myTasksSummary").textContent = jobs.length
      ? `共 ${jobs.length} 个，点击即可继续` : "还没有任务";
    $("myTaskList").innerHTML = jobs.length ? jobs.map((job) => {
      const status = { pending: "排队中", running: "生成中", done: "已完成",
                       failed: "失败" }[job.status] || job.status;
      const queue = job.status === "pending" && job.queue_position
        ? ` · 队列第 ${job.queue_position} 位` : "";
      return `<button class="my-task" type="button" data-job-id="${esc(job.id)}">
        <span><strong>${esc(job.city_title || job.city)}</strong>
          <small>${esc(job.mode === "styles" ? "风格方案" :
            (job.mode === "draft" ? "快速预览" : "正式模型"))}</small></span>
        <span class="my-task-status ${esc(job.status)}">${esc(status + queue)}</span>
      </button>`;
    }).join("") : '<p class="my-task-empty">从选择一个地点开始，第一件作品会出现在这里。</p>';
  } catch (error) {
    $("myTaskList").innerHTML = `<p class="my-task-empty">${esc(error.message)}</p>`;
  }
}

$("accountTrigger").onclick = openAccountDialog;
$("accountClose").onclick = closeAccountDialog;
$("btnSendCode").onclick = sendEmailCode;
$("btnVerifyCode").onclick = verifyEmailCode;
$("authCode").onkeydown = (event) => {
  if (event.key === "Enter") verifyEmailCode();
};
$("btnLogout").onclick = logoutAccount;
$("btnRefreshTasks").onclick = loadMyTasks;
$("btnOpenTasks").onclick = () => {
  closeAccountDialog();
  $("myTasksCard").scrollIntoView({ behavior: "smooth", block: "start" });
};
$("myTaskList").onclick = (event) => {
  const item = event.target.closest("[data-job-id]");
  if (item) lookupJob(item.dataset.jobId);
};

/* ---------------- Step 1 Tab 切换 ---------------- */

function switchS1Tab(tab) {
  $("s1Tabs").querySelectorAll(".s1-tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  $("s1Search").hidden = tab !== "search";
  $("s1Photo").hidden = tab !== "photo";
  // 切到搜索时自动聚焦
  if (tab === "search") setTimeout(() => $("lmInput").focus(), 50);
}

$("s1Tabs").addEventListener("click", (e) => {
  const btn = e.target.closest(".s1-tab");
  if (btn) switchS1Tab(btn.dataset.tab);
});

async function loadCities() {
  const data = await fetchJSON("/api/cities");
  state.cities = data.cities;
  buildGroupSelect();
  renderTabs();
  renderStep3();
  renderViewer();
  renderDownloads();
}

const FALLBACK_HERO_SAMPLES = [
  {
    url: "assets/paris-15km-dense.jpg",
    kind: "真实 15 × 15 KM 输出",
    location: "PARIS / DENSE DETAIL",
    title: "塞纳河与巴黎城市肌理",
    alt: "真实生成的巴黎 15 公里乘 15 公里精细风格图",
  },
  {
    url: "assets/suzhou-15km-block-fill.jpg",
    kind: "真实 15 × 15 KM 输出",
    location: "SUZHOU / BLOCK FILL",
    title: "金鸡湖水岸与苏州城市纹理",
    alt: "真实生成的苏州金鸡湖 15 公里乘 15 公里街区填充风格图",
  },
];

let heroSamples = FALLBACK_HERO_SAMPLES;
let heroSampleIndex = 0;
let heroTouchStartX = null;
let heroAutoplayTimer = null;
const HERO_AUTOPLAY_MS = 5200;

function showHeroSample(index, immediate = false) {
  if (!heroSamples.length) return;
  heroSampleIndex = (index + heroSamples.length) % heroSamples.length;
  const sample = heroSamples[heroSampleIndex];
  const image = $("heroShowcaseImage");
  const apply = () => {
    image.src = sample.url;
    image.alt = sample.alt || `${sample.title}，15 公里乘 15 公里真实输出`;
    $("heroShowcaseKind").textContent = sample.kind || "真实 15 × 15 KM 输出";
    $("heroShowcaseLocation").textContent = sample.location || "15 KM × 15 KM";
    $("heroShowcaseTitle").textContent = sample.title;
    $("heroShowcaseCount").textContent =
      `${String(heroSampleIndex + 1).padStart(2, "0")} / ${String(heroSamples.length).padStart(2, "0")}`;
    image.classList.remove("is-changing");
  };
  if (immediate || image.getAttribute("src") === sample.url) {
    apply();
  } else {
    image.classList.add("is-changing");
    window.setTimeout(apply, 130);
  }
}

function restartHeroAutoplay() {
  window.clearInterval(heroAutoplayTimer);
  heroAutoplayTimer = null;
  if (heroSamples.length < 2) return;
  heroAutoplayTimer = window.setInterval(
    () => showHeroSample(heroSampleIndex + 1), HERO_AUTOPLAY_MS);
}

function initHeroShowcase() {
  showHeroSample(0, true);
  restartHeroAutoplay();
  $("heroShowcasePrev").onclick = () => {
    showHeroSample(heroSampleIndex - 1);
    restartHeroAutoplay();
  };
  $("heroShowcaseNext").onclick = () => {
    showHeroSample(heroSampleIndex + 1);
    restartHeroAutoplay();
  };
  const frame = $("heroShowcase");
  frame.addEventListener("touchstart", (event) => {
    heroTouchStartX = event.changedTouches[0]?.clientX ?? null;
  }, { passive: true });
  frame.addEventListener("touchend", (event) => {
    if (heroTouchStartX === null) return;
    const delta = (event.changedTouches[0]?.clientX ?? heroTouchStartX) - heroTouchStartX;
    heroTouchStartX = null;
    if (Math.abs(delta) >= 45) {
      showHeroSample(heroSampleIndex + (delta < 0 ? 1 : -1));
      restartHeroAutoplay();
    }
  }, { passive: true });
  FALLBACK_HERO_SAMPLES.slice(1).forEach((sample) => {
    const image = new Image();
    image.src = sample.url;
  });
}

async function loadShowcase() {
  try {
    const result = await fetchJSON("/api/showcase");
    const samples = (result.samples || []).filter(
      (sample) => sample.url && sample.title && sample.size_km === 15);
    if (!samples.length) return;
    heroSamples = samples;
    showHeroSample(0, true);
    restartHeroAutoplay();
    heroSamples.slice(1, 4).forEach((sample) => {
      const image = new Image();
      image.src = sample.url;
    });
  } catch (_) {
    // file:// previews and an unavailable API keep the bundled verified samples.
  }
}

/** 构建国家分组下拉（去重 + 精选置顶） */
function buildGroupSelect() {
  const sel = $("cityGroup");
  const groups = [];
  const seen = new Set();
  for (const c of state.cities) {
    const g = c.group || "";
    if (g && !seen.has(g)) { seen.add(g); groups.push(g); }
  }
  // 精选排前，其余按字母序
  groups.sort((a, b) => {
    if (a === "精选") return -1;
    if (b === "精选") return 1;
    return a.localeCompare(b, "zh");
  });
  sel.innerHTML = '<option value="">全部</option>';
  for (const g of groups) {
    const opt = document.createElement("option");
    opt.value = g;
    opt.textContent = g;
    sel.appendChild(opt);
  }
}

$("cityGroup").onchange = () => {
  state.cityGroupFilter = $("cityGroup").value;
  renderTabs();
};
$("cityFilter").oninput = () => {
  state.cityTextFilter = $("cityFilter").value.trim().toLowerCase();
  renderTabs();
};

function cityInfo() {
  if (state.target.kind !== "preset") return null;
  return state.cities.find((c) => c.name === state.target.city);
}

/* ---------------- Step 1：位置选择 ---------------- */

function renderTabs() {
  const nav = $("cityTabs");
  nav.innerHTML = "";
  const gf = state.cityGroupFilter;
  const tf = state.cityTextFilter;
  let list = state.cities;
  if (gf) list = list.filter((c) => (c.group || "") === gf);
  if (tf) list = list.filter((c) =>
    c.title.toLowerCase().includes(tf) ||
    (c.name || "").toLowerCase().includes(tf));
  // 最多显示 30 个，避免 DOM 过重
  const shown = list.slice(0, 30);
  for (const c of shown) {
    const btn = document.createElement("button");
    const on = state.target.kind === "preset" && c.name === state.target.city;
    btn.className = "city-tab" + (on ? " active" : "");
    btn.innerHTML =
      `<span>${c.title}</span><span class="proto">${c.prototype}</span>` +
      (c.running ? '<span class="dot-running"></span>' : "");
    btn.onclick = () => selectCity(c.name);
    nav.appendChild(btn);
  }
  if (list.length > 30) {
    const more = document.createElement("span");
    more.className = "city-tab more";
    more.textContent = `+${list.length - 30} 更多`;
    nav.appendChild(more);
  }
}

/** 选预设城市：切目标 + 载画廊 + 地图跟着跳（问题 6） */
async function selectCity(name) {
  const c = state.cities.find((x) => x.name === name);
  if (!c) return;
  state.target = { kind: "preset", city: name, title: c.title,
                   prototype: c.prototype };
  state.selectedStyle = null;
  state.gallery = null;
  // 照片位置是用户的锚，不因选城市/搜地名而消失（只有重新上传才换）
  $("lmInput").value = "";
  renderTabs();

  // 地图联动：跳到该城市 bbox 并对齐取景框
  initMap();
  const b = c.bbox;               // [south, west, north, east]
  setTierNear(kmFromBbox(b));
  syncSize();
  map.state.map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: [16, 16] });
  setTimeout(updateRect, 120);
  $("customHint").textContent =
    `已选预设城市：${c.title}（取景框已对齐官方范围，可自行微调）`;
  unlockView();

  if (c.has_gallery) {
    try { state.gallery = await fetchJSON(`/api/gallery/${name}`); }
    catch (_) { /* 无画廊则跳过 */ }
  }
  renderStep3();
  renderViewer();
  renderDownloads();
  persistState();
}

/* ---------------- 地图与取景 ---------------- */

const TIERS = [5, 10, 15];              // 取景三挡（km）

const map = {
  state: { map: null, rect: null },
  photoMarker: null,
  photoPoint: null,     // [lat, lon] 单张照片 EXIF
  journey: null,        // {clusters, layerGroup}
};

function sizeKm() {
  const b = $("tierSeg").querySelector("button.active");
  return b ? parseFloat(b.dataset.km) : 10;
}

/** 选中最接近给定尺寸的挡位 */
function setTierNear(km) {
  const pick = TIERS.reduce((a, b) =>
    Math.abs(b - km) < Math.abs(a - km) ? b : a);
  $("tierSeg").querySelectorAll("button").forEach((x) =>
    x.classList.toggle("active", parseFloat(x.dataset.km) === pick));
}

function syncSize() {
  const km = sizeKm();
  $("tierHint").textContent = `${km} km 见方 · 金色框即成品范围`;
}

function bboxFromCenter(lat, lon, km) {
  const halfLat = km / 2 / 110.574;
  const halfLon = km / 2 / (111.32 * Math.max(Math.cos(lat * Math.PI / 180), 0.2));
  return [lat - halfLat, lon - halfLon, lat + halfLat, lon + halfLon];
}

function currentBbox() {
  const c = map.state.map.getCenter();
  return bboxFromCenter(c.lat, c.lng, sizeKm()).map((v) => +v.toFixed(4));
}

function kmFromBbox(b) {
  const latKm = (b[2] - b[0]) * 110.574;
  const lonKm = (b[3] - b[1]) * 111.32
    * Math.cos(((b[0] + b[2]) / 2) * Math.PI / 180);
  return Math.max(latKm, lonKm);
}

function normalizeBbox(value) {
  if (!Array.isArray(value) || value.length !== 4) return null;
  const bbox = value.map(Number);
  if (!bbox.every(Number.isFinite)) return null;
  const [s, w, n, e] = bbox;
  return n > s && e > w ? bbox : null;
}

/** 用服务端任务记录恢复区域上下文，杜绝“巴黎画廊 + 西湖选框”。 */
function restoreJobArea(job) {
  const bbox = normalizeBbox(job.bbox);
  if (!bbox) return false;
  const title = job.city_title || "自定义区域";
  state.target = {
    kind: "area", city: null, title,
    prototype: job.prototype || "landscape",
  };
  state.gallerySlug = job.city;
  state.galleryBbox = bbox;
  $("areaName").value = title;
  initMap();
  map.userMoved = false;
  setTierNear(kmFromBbox(bbox));
  syncSize();
  map.state.map.fitBounds(
    [[bbox[0], bbox[1]], [bbox[2], bbox[3]]],
    { padding: [16, 16] },
  );
  setTimeout(updateRect, 120);
  renderTabs();
  lockView();
  saveSession({ target: state.target, areaName: title, lastBbox: bbox });
  return true;
}

function updateRect() {
  if (!map.state.map) return;
  const [s, w, n, e] = currentBbox();
  const bounds = [[s, w], [n, e]];
  if (map.state.rect) map.state.rect.setBounds(bounds);
  else map.state.rect = L.rectangle(bounds, {
    color: "#d18a5f", weight: 2, fillOpacity: 0.08, dashArray: "6 4",
    interactive: false,
  }).addTo(map.state.map);
}

function initMap() {
  if (map.state.map || typeof L === "undefined") return;
  map.state.map = L.map("leafletMap", { zoomControl: true })
    .setView([30.25, 120.15], 12);
  // 卫星底图（Esri World Imagery，免费无 key）
  L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 19,
      attribution: '&copy; Esri, Maxar, Earthstar Geographics' }
  ).addTo(map.state.map);
  // 地名标注叠加层（Esri Reference）
  L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 19, attribution: '&copy; Esri' }
  ).addTo(map.state.map);
  map.state.map.on("move", () => {
    updateRect();
    // 手动拖图即视为自定义区域
    if (state.target.kind === "preset" && map.userMoved) {
      state.target = { kind: "area", city: null, title: "自定义区域" };
      renderTabs();
      renderStep3();
    }
  });
  map.state.map.on("dragend zoomend", () => { map.userMoved = true; });
  updateRect();
  setTimeout(() => map.state.map.invalidateSize(), 60);
}

$("tierSeg").querySelectorAll("button").forEach((b) => {
  b.onclick = () => {
    $("tierSeg").querySelectorAll("button")
      .forEach((x) => x.classList.toggle("active", x === b));
    syncSize();
    updateRect();
    // 取景变了，已有风格图不再对应
    invalidateStyles();
  };
});

/* ---------------- Step 2 → 3：确定取景 → 生成风格图 ---------------- */

/** 取景/位置变了 → 旧风格图失效 */
function invalidateStyles() {
  if (state.target.kind === "preset") return;   // 预设城市画廊不变
  state.gallery = null;
  state.gallerySlug = null;
  state.galleryBbox = null;
  state.selectedStyle = null;
  renderStep3();
  renderViewer();
}

/** 锁定取景：确定看风格后调用，禁止拖图/改挡位 */
function lockView() {
  state.viewLocked = true;
  if (map.state.map) {
    map.state.map.dragging.disable();
    map.state.map.scrollWheelZoom.disable();
    map.state.map.doubleClickZoom.disable();
    map.state.map.touchZoom.disable();
    $("leafletMap").classList.add("locked");
  }
  $("tierSeg").querySelectorAll("button").forEach((b) => (b.disabled = true));
  $("lockRow").hidden = false;
}

/** 解锁取景：用户点“重新取景”，同时清除已生成的风格图 */
function unlockView() {
  state.viewLocked = false;
  if (map.state.map) {
    map.state.map.dragging.enable();
    map.state.map.scrollWheelZoom.enable();
    map.state.map.doubleClickZoom.enable();
    map.state.map.touchZoom.enable();
    $("leafletMap").classList.remove("locked");
  }
  $("tierSeg").querySelectorAll("button").forEach((b) => (b.disabled = false));
  $("lockRow").hidden = true;
  invalidateStyles();   // 框要变了，旧风格图作废
}
$("btnUnlock").onclick = unlockView;

async function confirmArea() {
  if (!map.state.map) { initMap(); return; }
  // 切新区域 → 2D 图回初始状态（不放旧图/坏图）
  lastArtifacts = null;
  $("preview2d").hidden = true;
  $("p2Topdown").hidden = true;
  $("p2Diag").hidden = true;
  // 预设城市（含景点目录）用存储的 bbox，自定义区域用地图中心
  let bbox, proto, name, slug;
  if (state.target.kind === "preset") {
    const c = state.cities.find((x) => x.name === state.target.city);
    if (!c) return;
    bbox = c.bbox;
    proto = c.prototype || "landscape";
    name = c.title;
    slug = c.name;   // 景点城市用 ID 作 slug，画廊存到 output/style_gallery/{id}/
  } else {
    bbox = currentBbox();
    proto = state.target.prototype || "landscape";
    name = $("areaName").value.trim();
    slug = "";
  }
  const btn = $("btnConfirmArea");
  btn.disabled = true;
  try {
    const r = await fetchJSON("/api/styles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bbox, name, prototype: proto, slug }),
    });
    state.job = { id: r.job_id, mode: "styles", city: r.slug, slug: r.slug };
    state.pendingArea = { bbox, slug: r.slug };
    state.gallerySlug = r.slug;
    state.galleryBbox = bbox;
    $("jobPanel").hidden = false;
    $("jobStatus").textContent = r.cached
      ? "已找到该区域的现成结果"
      : (r.reused ? "相同区域正在生成，已接入现有任务" : "生成 4 种风格的平面图中");
    $("jobStatus").className = "pill";
    $("galleryHint").textContent = r.cached
      ? "正在载入…"
      : (r.reused
        ? "已接入相同区域的生成任务，完成后会自动显示"
        : "正在分析地图并生成 4 种风格，预计 8–12 分钟，请耐心等待");
    renderJobProgress({
      progress_pct: r.cached ? 100 : 2,
      duration_hint: "首次生成通常需要 8–12 分钟；相同区域会直接复用",
    });
    showJobToken(r.job_id);
    refreshAccount();
    lockView();   // 任务已开始，锁定取景框
    pollJob();
  } catch (err) {
    btn.disabled = false;
    alert("生成失败: " + err.message);
  }
}

$("btnConfirmArea").onclick = confirmArea;

/* ---------------- Step 1：地名检索（目录 + geocode 兜底）---------------- */

let lmTimer = null;
let lmCatalogCache = null;    // 启动预取，聚焦时直接用

async function lmSearch(q, show = true) {
  let items = [];
  try {
    const r = await fetchJSON(`/api/landmarks?q=${encodeURIComponent(q)}`);
    items = r.landmarks.map((x) => ({ ...x, source: "catalog" }));
  } catch (_) { /* 目录失败继续兜底 */ }
  if (!q) lmCatalogCache = items;
  if (!items.length && q) {
    // 目录未命中 → 全球地理编码兜底（问题 1）
    if (show) renderLmResults([], "查询中…");
    try {
      const g = await fetchJSON(`/api/geocode?q=${encodeURIComponent(q)}`);
      items = g.results || [];
    } catch (err) {
      if (show) renderLmResults([], "✕ " + err.message);
      return;
    }
  }
  if (show) renderLmResults(items);
}

const SRC_LABEL = { amap: "高德", nominatim: "", catalog: "" };

/** 数据三态徽标：对用户只展示友好文案，不暴露内部区域名 */
function stateBadge(it) {
  if (it.data_state === "fetchable") {
    return '<span class="lm-badge fetch">即将开放</span>';
  }
  if (it.data_state === "none") return '<span class="lm-badge">即将开放</span>';
  return "";
}

function renderLmResults(list, msg) {
  const box = $("lmResults");
  if (msg) {
    box.innerHTML = `<div class="lm-empty">${msg}</div>`;
    box.hidden = false;
    return;
  }
  if (!list.length) {
    box.innerHTML = '<div class="lm-empty">没找到，换个说法试试</div>';
    box.hidden = false;
    return;
  }
  box.innerHTML = "";
  for (const lm of list) {
    const div = document.createElement("div");
    div.className = "lm-item" + (lm.available ? "" : " off");
    const where = [lm.city, lm.country].filter(Boolean).join(" · ");
    const src = SRC_LABEL[lm.source] || "";
    div.innerHTML =
      `<span class="lm-name">${lm.name}</span>` +
      `<span class="lm-meta">${where}</span>` +
      (src ? `<span class="lm-src">${src}</span>` : "") +
      stateBadge(lm);
    div.onclick = () => selectPlace(lm);
    box.appendChild(div);
  }
  box.hidden = false;
}

/** 选中一个地点（目录条目或 geocode 结果）→ 切自定义区域 + 摆框 */
function selectPlace(lm) {
  $("lmResults").hidden = true;
  $("lmInput").value = lm.name;
  state.target = { kind: "area", city: null, title: lm.name,
                   prototype: lm.style || "landscape" };
  state.selectedStyle = null;
  state.gallery = null;
  lastArtifacts = null;
  $("preview2d").hidden = true;
  renderTabs();

  initMap();
  const b = lm.bbox;
  setTierNear(kmFromBbox(b));
  syncSize();
  map.state.map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: [16, 16] });
  setTimeout(updateRect, 120);
  unlockView();
  // 数据三态：本地就绪 / 可拉取（给按钮）/ 不在覆盖范围
  const hint = $("customHint");
  state.pendingFetch = null;
  if (lm.data_state === "local" || lm.available) {
    hint.textContent =
      `已定位：${lm.name}${lm.city ? "（" + lm.city + "）" : ""}，可微调取景`;
  } else if (lm.data_state === "fetchable") {
    state.pendingFetch = lm.fetch;
    hint.textContent = `${lm.name}：数据正在准备中，敬请期待`;
  } else {
    hint.textContent = `${lm.name}：该区域即将开放，敬请期待`;
  }
  renderStep3();
  renderViewer();
  renderDownloads();
}

$("lmInput").oninput = () => {
  const q = $("lmInput").value.trim();
  clearTimeout(lmTimer);
  if (!q) { $("lmResults").hidden = true; return; }
  lmTimer = setTimeout(() => lmSearch(q), 300);
};
$("lmInput").onfocus = () => {
  // 聚焦且未输入时，展示预取的可生成目录（发现入口）
  if ($("lmInput").value.trim()) return;
  if (window.location.protocol === "file:") {
    renderLmResults([], "当前是静态文件预览，景点目录需要通过网页服务打开");
    return;
  }
  if (lmCatalogCache !== null) {
    renderLmResults(lmCatalogCache);
    return;
  }
  // 修复启动竞态：预取尚未完成时用户先聚焦，不能一直显示空白。
  renderLmResults([], "正在载入景点目录…");
  lmSearch("", true);
};
document.addEventListener("click", (e) => {
  if (!e.target.closest(".lm-search")) $("lmResults").hidden = true;
});

/* ---------------- Step 1：照片定位 / 旅程 ---------------- */

function clearPhotoPoint() {
  if (map.photoMarker) { map.photoMarker.remove(); map.photoMarker = null; }
  map.photoPoint = null;
}

function clearJourney() {
  if (map.journey && map.journey.layerGroup) map.journey.layerGroup.remove();
  map.journey = null;
  $("journeySummary").hidden = true;
  hideGapCard();
}

// 文件名 → ObjectURL 映射（用于追问时展示缩略图）
const photoUrls = new Map();

function cachePhotoUrls(files) {
  // 清除旧的（每次重新上传替换）
  for (const url of photoUrls.values()) URL.revokeObjectURL(url);
  photoUrls.clear();
  for (const f of files) {
    photoUrls.set(f.name, URL.createObjectURL(f));
  }
}

$("photoInput").onchange = async () => {
  let files = [...$("photoInput").files];
  if (!files.length) return;
  // 最多 10 张：同一地点传太多没意义，且增加解析时间
  if (files.length > 10) {
    $("photoHint").textContent =
      `最多 10 张照片（已自动取前 10 张，请精选不同地点的代表照）`;
    files = files.slice(0, 10);
  }
  cachePhotoUrls(files);  // 缓存缩略图 URL

  // 智能分流：1 张 = 单点定位，多张 = 旅程轨迹
  if (files.length === 1) {
    clearJourney();
    $("photoHint").textContent = "读取照片 GPS 中…";
    try {
      const fd = new FormData();
      fd.append("photo", files[0]);
      const r = await fetchJSON("/api/photo-location", { method: "POST", body: fd });
      map.photoPoint = [r.lat, r.lon];
      state.target = { kind: "area", city: null, title: "照片位置" };
      renderTabs();
      initMap();
      map.state.map.setView([r.lat, r.lon], 13);
      if (map.photoMarker) map.photoMarker.remove();
      map.photoMarker = L.marker([r.lat, r.lon]).addTo(map.state.map)
        .bindPopup("📷 照片拍摄点").openPopup();
      $("photoHint").textContent =
        `已定位 ${r.lat.toFixed(5)}, ${r.lon.toFixed(5)}` +
        (r.pbf ? "，该区域可生成 ✓" : "，该区域即将开放");
      renderStep3();
      persistState();
    } catch (err) {
      clearPhotoPoint();
      $("photoHint").textContent = "✕ " + err.message;
      if (/GPS/.test(err.message)) {
        startGapFlow([{
          type: "no_gps_all",
          question: "这张照片在哪拍的？",
          detail: { photo_names: [files[0].name] },
          optional: true,
        }]);
      }
    }
  } else {
    // 多张 → 旅程模式
    clearJourney();
    clearPhotoPoint();
    $("photoHint").textContent = `解析 ${files.length} 张照片中…`;
    try {
      const fd = new FormData();
      for (const f of files) fd.append("photos", f);
      const r = await fetchJSON("/api/journey", { method: "POST", body: fd });
      renderJourney(r, files.length);
    } catch (err) {
      $("photoHint").textContent = "✕ " + err.message;
    }
  }
  $("photoInput").value = "";
};

function renderJourney(r, total) {
  const clusters = r.clusters || [];
  if (!clusters.length) {
    $("photoHint").textContent =
      "没读到 GPS（微信/截图会抹掉 EXIF），可手动告诉我地点";
    startGapFlow(r.gaps || []);
    return;
  }
  state.target = { kind: "area", city: null, title: "旅程" };
  renderTabs();
  initMap();
  const lg = L.layerGroup().addTo(map.state.map);
  const path = clusters.map((c) => [c.lat, c.lon]);
  if (path.length >= 2) {
    L.polyline(path, { color: "#e0a458", weight: 3, dashArray: "1 6" }).addTo(lg);
  }
  clusters.forEach((c, i) => {
    const dwell = c.dwell_minutes >= 60
      ? `${(c.dwell_minutes / 60).toFixed(1)} 小时`
      : `${Math.round(c.dwell_minutes)} 分钟`;
    const title = c.name || `停留点 ${i + 1}`;
    L.marker([c.lat, c.lon], {
      icon: L.divIcon({
        className: "journey-pin", html: `<span>${i + 1}</span>`,
        iconSize: [24, 24], iconAnchor: [12, 12],
      }),
    }).bindPopup(`${title} · ${c.count} 张 · ${dwell}`).addTo(lg);
  });
  map.journey = { clusters, layerGroup: lg };

  if (r.suggested_bbox) {
    const b = r.suggested_bbox;
    setTierNear(kmFromBbox(b));
    syncSize();
    map.state.map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: [20, 20] });
    setTimeout(updateRect, 120);
  }
  const noGps = (r.photos || []).filter((p) => p.status === "no_gps").length;
  const suspect = (r.photos || []).filter((p) => p.status === "suspect").length;
  const days = (r.chapters || []).filter((ch) => ch.date !== "unknown").length;
  let html = `🧳 ${total} 张照片 → <b>${clusters.length} 个停留点</b>`
    + (days > 1 ? ` · 跨 ${days} 天` : "");
  if (noGps) html += ` · ${noGps} 张无 GPS`;
  if (suspect) html += ` · ${suspect} 张位置异常已剔除`;
  const named = clusters.filter((c) => c.name).map((c) => c.name);
  if (named.length) html += `<br>${named.join(" → ")}`;
  if (r.suggest_split) html += "<br>⚠ 跨度较大，建议按天缩小取景框逐段生成";
  if (!r.pbf) html += "<br>⚠ 该区域即将开放，敬请期待";
  $("journeySummary").innerHTML = html;
  $("journeySummary").hidden = false;
  $("photoHint").textContent = "轨迹已上图，生成时带编号针 + 金色轨迹线";
  renderStep3();
  startGapFlow(r.gaps || []);
  persistState();
}

/* ---------------- Step 1：缺口追问（只问照片里没有的）---------------- */

const gapFlow = { queue: [], idx: 0, timer: null };

function hideGapCard() {
  $("gapCard").hidden = true;
  $("gapResults").hidden = true;
  $("gapInput").value = "";
  $("gapThumbs").innerHTML = "";
  gapFlow.queue = [];
  gapFlow.idx = 0;
}

function startGapFlow(gaps) {
  if (!gaps.length) { hideGapCard(); return; }
  gapFlow.queue = gaps;
  gapFlow.idx = 0;
  showGap();
}

function showGap() {
  const g = gapFlow.queue[gapFlow.idx];
  if (!g) { hideGapCard(); return; }
  $("gapCard").hidden = false;
  $("gapQuestion").textContent = g.question;
  $("gapCount").textContent = gapFlow.queue.length > 1
    ? `${gapFlow.idx + 1}/${gapFlow.queue.length}` : "";
  $("gapSkip").textContent = g.optional ? "跳过" : "稍后再说";
  $("gapInput").value = "";
  $("gapResults").hidden = true;
  // 展示相关照片缩略图
  renderGapThumbs(g.detail && g.detail.photo_names);
  $("gapInput").focus();
}

function renderGapThumbs(names) {
  const box = $("gapThumbs");
  box.innerHTML = "";
  if (!names || !names.length) { box.hidden = true; return; }
  // 最多展示 6 张缩略图
  const show = names.slice(0, 6);
  for (const name of show) {
    const url = photoUrls.get(name);
    if (!url) continue;
    const img = document.createElement("img");
    img.src = url;
    img.alt = name;
    img.title = "点击查看大图 · " + name;
    img.className = "gap-thumb";
    img.onclick = () => openLightbox(url, name);
    box.appendChild(img);
  }
  box.hidden = !box.children.length;
}

function nextGap() {
  gapFlow.idx += 1;
  if (gapFlow.idx >= gapFlow.queue.length) hideGapCard();
  else showGap();
}

/** 把一句话拆成多个地名：支持“第N张在X”序号结构 或 顿号/逗号/分号/换行 列表 */
function parseGapNames(text) {
  const t = (text || "").trim();
  if (!t) return [];
  const clean = (s) =>
    s.replace(/^[，,、；;\s\/·]+|[，,、；;\s\/·]+$/g, "").trim();
  // 明确序号标记：“第N张/第N个/N张/N个”（无“第”时量词不可省，避免误伤“3号景点”）
  const seqRe = /(?:第\s*[一二三四五六七八九十百\d]+\s*[张个站处]|[一二三四五六七八九十百\d]+\s*[张个])\s*[在是为：:]?\s*/g;
  if (seqRe.test(t)) {
    seqRe.lastIndex = 0;
    const parts = t.split(seqRe).map(clean).filter(Boolean);
    if (parts.length) return parts;   // 序号命中：返回去前缀后的地名（哪怕 1 个）
  }
  // 分隔符列表
  const parts = t.split(/[，,、；;\n\/]+/).map(clean).filter(Boolean);
  return parts.length ? parts : [t];
}

/** 该缺口是否支持“一句话批量对应多张图” */
function gapSupportsBatch(g) {
  if (!g) return false;
  if (g.type !== "no_gps_all" && g.type !== "no_gps_partial") return false;
  const n = (g.detail && g.detail.photo_names) || [];
  return n.length >= 2;   // 至少两张图，序号对应才有意义
}

async function gapSearch(q) {
  const g = gapFlow.queue[gapFlow.idx];
  if (!g) return;
  const near = (g.detail && g.detail.near) || null;
  let url = `/api/geocode?q=${encodeURIComponent(q)}`;
  if (near) url += `&near_lat=${near[0]}&near_lon=${near[1]}`;
  const box = $("gapResults");
  try {
    const r = await fetchJSON(url);
    const list = r.results || [];
    if (!list.length) {
      box.innerHTML = '<div class="lm-empty">没找到，换个说法试试</div>';
      box.hidden = false;
      return;
    }
    box.innerHTML = "";
    for (const it of list.slice(0, 5)) {
      const div = document.createElement("div");
      div.className = "lm-item";
      const where = [it.city, it.country].filter(Boolean).join(" · ");
      const dist = it.dist_km !== undefined ? ` · ${it.dist_km}km` : "";
      div.innerHTML =
        `<span class="lm-name">${it.name}</span>` +
        `<span class="lm-meta">${where}${dist}</span>` +
        (SRC_LABEL[it.source] ? `<span class="lm-src">${SRC_LABEL[it.source]}</span>` : "");
      div.onclick = () => applyGapAnswer(it);
      box.appendChild(div);
    }
    box.hidden = false;
  } catch (err) {
    box.innerHTML = `<div class="lm-empty">✕ ${err.message}</div>`;
    box.hidden = false;
  }
}

/** 回答落地：根据缺口类型插针 / 追加轨迹点 / 补名，立即上图 */
function applyGapAnswer(place) {
  const g = gapFlow.queue[gapFlow.idx];
  if (!g) return;
  const [lat, lon] = place.center;
  initMap();

  if (g.type === "no_gps_all") {
    // 无任何定位 → 整个旅程落在这里
    state.target = { kind: "area", city: null, title: place.name };
    const b = place.bbox;
    setTierNear(kmFromBbox(b));
    syncSize();
    map.state.map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: [16, 16] });
    setTimeout(updateRect, 120);
  } else if (g.type === "unnamed_cluster") {
    // 补上停留点名称（不改坐标，保留照片的真实位置）
    const c = map.journey && map.journey.clusters[g.detail.cluster];
    if (c) {
      c.name = place.name;
      redrawJourney();
    }
  } else {
    // time_gap / no_gps_partial → 插入一个停留点
    const c = {
      lat, lon, count: 0, t_start: null, t_end: null,
      dwell_minutes: 0, name: place.name, manual: true,
    };
    if (!map.journey) map.journey = { clusters: [], layerGroup: null };
    const at = g.type === "time_gap" ? (g.detail.after_cluster + 1)
                                     : map.journey.clusters.length;
    map.journey.clusters.splice(at, 0, c);
    redrawJourney();
  }
  nextGap();
}

/** 用当前 clusters 重画地图图层与摘要 */
function redrawJourney() {
  if (!map.journey) return;
  if (map.journey.layerGroup) map.journey.layerGroup.remove();
  const lg = L.layerGroup().addTo(map.state.map);
  const clusters = map.journey.clusters;
  const path = clusters.map((c) => [c.lat, c.lon]);
  if (path.length >= 2) {
    L.polyline(path, { color: "#e0a458", weight: 3, dashArray: "1 6" }).addTo(lg);
  }
  clusters.forEach((c, i) => {
    const title = c.name || `停留点 ${i + 1}`;
    const meta = c.manual ? "手动补充"
      : `${c.count} 张 · ${Math.round(c.dwell_minutes)} 分钟`;
    L.marker([c.lat, c.lon], {
      icon: L.divIcon({
        className: "journey-pin" + (c.manual ? " manual" : ""),
        html: `<span>${i + 1}</span>`,
        iconSize: [24, 24], iconAnchor: [12, 12],
      }),
    }).bindPopup(`${title} · ${meta}`).addTo(lg);
  });
  map.journey.layerGroup = lg;

  const named = clusters.filter((c) => c.name).map((c) => c.name);
  const el = $("journeySummary");
  if (named.length) {
    el.innerHTML = `🧳 <b>${clusters.length} 个停留点</b><br>${named.join(" → ")}`;
    el.hidden = false;
  }
}

/** 批量落地：把按顺序解析出的多个地点，一一对应到该缺口的无 GPS 照片 */
async function batchApply(names) {
  const g = gapFlow.queue[gapFlow.idx];
  if (!g) return;
  const near = (g.detail && g.detail.near) || null;
  const box = $("gapResults");
  box.innerHTML = '<div class="lm-empty">按顺序解析中…</div>';
  box.hidden = false;
  const places = [];
  const fails = [];
  for (const nm of names) {
    let url = `/api/geocode?q=${encodeURIComponent(nm)}`;
    if (near) url += `&near_lat=${near[0]}&near_lon=${near[1]}`;
    try {
      const r = await fetchJSON(url);
      if (r.results && r.results.length) places.push(r.results[0]);
      else fails.push(nm);
    } catch (_) { fails.push(nm); }
  }
  if (!places.length) {
    box.innerHTML = '<div class="lm-empty">都没找到，检查地名或拆开逐个输入</div>';
    return;
  }
  applyGapBatch(g, places);
  if (fails.length) {
    $("photoHint").textContent =
      `已标记 ${places.length} 个；未识别：${fails.join("、")}（可单独补）`;
  }
}

/** 批量回答落地：为每个地点建一个手动停留点，上图 + 摆取景框 */
function applyGapBatch(g, places) {
  initMap();
  if (!map.journey) map.journey = { clusters: [], layerGroup: null };
  for (const p of places) {
    map.journey.clusters.push({
      lat: p.center[0], lon: p.center[1], count: 1,
      t_start: null, t_end: null, dwell_minutes: 0,
      name: p.name, manual: true,
    });
  }
  state.target = { kind: "area", city: null, title: places[0].name };
  const lats = places.map((p) => p.center[0]);
  const lons = places.map((p) => p.center[1]);
  const pad = 0.012;
  const b = [Math.min(...lats) - pad, Math.min(...lons) - pad,
             Math.max(...lats) + pad, Math.max(...lons) + pad];
  setTierNear(kmFromBbox(b));
  syncSize();
  map.state.map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: [20, 20] });
  setTimeout(updateRect, 120);
  redrawJourney();
  nextGap();
}

$("gapInput").oninput = () => {
  const q = $("gapInput").value.trim();
  clearTimeout(gapFlow.timer);
  if (q.length < 2) { $("gapResults").hidden = true; return; }
  const names = parseGapNames(q);
  const g = gapFlow.queue[gapFlow.idx];
  // 批量模式：一句话多个地名 → 预览对应关系，回车一次性标记
  if (names.length >= 2 && gapSupportsBatch(g)) {
    const box = $("gapResults");
    box.innerHTML =
      `<div class="lm-empty batch-hint">检测到 ${names.length} 个地点，`
      + `将按顺序对应照片。按 <b>回车</b> 标记：<br>`
      + names.map((n, i) => `${i + 1}. ${n}`).join("　") + `</div>`;
    box.hidden = false;
    return;
  }
  gapFlow.timer = setTimeout(() => gapSearch(q), 300);
};
$("gapInput").onkeydown = (e) => {
  if (e.key === "Enter") {
    const q = $("gapInput").value.trim();
    if (q.length < 2) return;
    clearTimeout(gapFlow.timer);
    const names = parseGapNames(q);
    const g = gapFlow.queue[gapFlow.idx];
    if (names.length >= 2 && gapSupportsBatch(g)) batchApply(names);
    else gapSearch(q);
  }
};
$("gapSkip").onclick = () => nextGap();

/* ---------------- Step 3：风格与画像 ---------------- */

const PROFILE_LABELS = {
  area_km2: ["覆盖面积", (v) => v.toFixed(0) + " km²"],
  elevation_range_m: ["高差", (v) => v.toFixed(0) + " m"],
  water_ratio: ["水体占比", (v) => (v * 100).toFixed(1) + "%"],
  building_density: ["建筑密度", (v) => v.toFixed(0) + " /km²"],
  vegetation_ratio: ["植被占比", (v) => (v * 100).toFixed(0) + "%"],
  road_density_km_per_km2: ["路网密度", (v) => v.toFixed(1) + " km/km²"],
  osm_quality: ["数据质量", (v) => v],
};

function renderStep3() {
  const has = !!(state.gallery && state.gallery.styles);
  const grid = $("galleryGrid");
  grid.innerHTML = "";
  $("renderToggle").style.display = has ? "" : "none";

  if (has) {
    if (!state.selectedStyle || !state.gallery.styles[state.selectedStyle]) {
      state.selectedStyle = state.gallery.styles.baseline
        ? "baseline" : Object.keys(state.gallery.styles)[0];
    }
    grid.style.display = "";
    for (const [key, s] of Object.entries(state.gallery.styles)) {
      const card = document.createElement("div");
      card.className = "style-card" + (key === state.selectedStyle ? " selected" : "");
      const img = s.images[state.renderKind] || s.images.topdown;
      card.innerHTML = `
        <img src="${img}" loading="lazy" alt="${s.label}">
        <div class="style-meta">
          <div class="row1">
            <span class="label">${s.label}</span>
            <span class="score">★ ${s.score.toFixed(2)}</span>
          </div>
          <div class="desc">${s.desc}</div>
        </div>`;
      card.querySelector("img").onclick = (e) => {
        e.stopPropagation();
        openLightbox(img, `${s.label} · ★${s.score.toFixed(2)}`);
      };
      card.onclick = () => {
        state.selectedStyle = key;
        renderStep3();
        renderViewer();      // 选中风格后，Step 4 上方的 2D 图跟着换
      };
      grid.appendChild(card);
    }
    $("galleryHint").textContent = "点图片放大，点卡片切换风格";
  } else {
    grid.style.display = "none";
    $("galleryHint").textContent = "自定义区域用自动参数（规则引擎按地貌适配）";
  }
  updateStyleHint();

  // 城市画像（仅预设城市有）
  const profile = state.gallery && state.gallery.profile;
  const box = $("profileChips");
  box.innerHTML = "";
  if (profile) {
    for (const [key, [label, fmt]] of Object.entries(PROFILE_LABELS)) {
      if (profile[key] === undefined) continue;
      const el = document.createElement("span");
      el.className = "chip";
      el.innerHTML = `${label}<b>${fmt(profile[key])}</b>`;
      box.appendChild(el);
    }
  }
  syncGenerationProfileAvailability();
}

function updateStyleHint() {
  const el = $("styleHint");
  if (state.selectedStyle && state.gallery) {
    const s = state.gallery.styles[state.selectedStyle];
    el.textContent = `快速预览和正式模型将使用：${s.label}`;
    el.classList.add("on");
  } else if (state.gallery) {
    el.textContent = "未选风格，将用自动参数生成；可先在上方选一个";
    el.classList.remove("on");
  } else {
    el.textContent = "";
    el.classList.remove("on");
  }
}

$("renderToggle").querySelectorAll("button").forEach((b) => {
  b.onclick = () => {
    state.renderKind = b.dataset.kind;
    $("renderToggle").querySelectorAll("button")
      .forEach((x) => x.classList.toggle("active", x === b));
    renderStep3();
  };
});

/* ---------------- Step 4/5：生成 ---------------- */

function qualityProfileAvailable() {
  return state.target.kind === "preset" && state.target.city === "westlake";
}

function syncGenerationProfileAvailability() {
  const available = qualityProfileAvailable();
  const qualityInputs = $("profilePicker").querySelectorAll(
    'input[value="quality_flat"], input[value="quality_textured"]');
  qualityInputs.forEach((input) => { input.disabled = !available; });

  if (!available && state.generationProfile !== "classic") {
    state.generationProfile = "classic";
  }
  const selected = $("profilePicker").querySelector(
    `input[value="${state.generationProfile}"]`);
  if (selected) selected.checked = true;
  $("profilePicker").querySelectorAll(".profile-option").forEach((label) => {
    const input = label.querySelector("input");
    label.classList.toggle("active", input.checked);
  });

  const quality = state.generationProfile !== "classic";
  $("profileNote").textContent = available
    ? "精细模型已开放：直接生成 25km 正式模型；两种街区表现会分别保存。"
    : "精细模型暂在“杭州 · 西湖”开放；其他区域使用标准生成。";
  $("profileNote").classList.toggle("available", available);
  $("btnDraft").disabled = !!state.job || quality;
  $("btnDraft").title = quality ? "精细模型直接生成正式文件" : "";
  $("fullModeHint").textContent = quality
    ? `${state.generationProfile === "quality_flat" ? "平整街区" : "地块起伏"} · 模型 + 预览图`
    : "标准生成 · 模型 + 预览图";
}

$("profilePicker").addEventListener("change", (event) => {
  const input = event.target.closest('input[name="generationProfile"]');
  if (!input || input.disabled) return;
  state.generationProfile = input.value;
  state.selectedStyle = state.generationProfile === "classic"
    ? state.selectedStyle : null;
  syncGenerationProfileAvailability();
  updateStyleHint();
  persistState();
});

/** 组装请求体：预设城市 or 自定义区域，统一入口 */
function buildRequest(mode) {
  const body = {
    mode,
    generation_profile: state.generationProfile,
  };
  if (mode === "draft" && state.generationProfile !== "classic") {
    throw new Error("精细模型直接生成正式打印文件，不提供快速预览");
  }
  if (state.target.kind === "preset") {
    body.city = state.target.city;
    if (state.selectedStyle && state.generationProfile === "classic") {
      body.style = state.selectedStyle;
    }
    return body;
  }
  if (!map.state.map) throw new Error("请先在上方选择位置");
  const bbox = state.gallery && state.galleryBbox
    ? [...state.galleryBbox]
    : currentBbox();
  const [s, w, n, e] = bbox;
  const inBox = (lat, lon) => lat >= s && lat <= n && lon >= w && lon <= e;
  body.area = { bbox, name: $("areaName").value.trim() };
  if (state.selectedStyle && state.gallery) {
    if (!state.gallerySlug) {
      throw new Error("风格图缺少区域身份，请重新找回任务或确认位置");
    }
    body.style = state.selectedStyle;
    body.gallery_slug = state.gallerySlug;
  }
  body.markers = [];
  if (map.journey) {
    const cs = map.journey.clusters.filter((c) => inBox(c.lat, c.lon));
    body.markers = cs.slice(0, 10).map((c) => [c.lat, c.lon]);
  } else if (map.photoPoint && inBox(map.photoPoint[0], map.photoPoint[1])) {
    body.markers = [map.photoPoint];
  }
  return body;
}

async function startJob(mode) {
  if (state.job) return;
  let body;
  try { body = buildRequest(mode); }
  catch (err) { alert(err.message); return; }
  try {
    const resp = await fetchJSON("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state.job = { id: resp.job_id, mode, city: resp.city,
                  generationProfile: body.generation_profile };
    $("jobPanel").hidden = false;
    const extra = body.markers && body.markers.length
      ? ` · ${body.markers.length} 处标注` : "";
    const styleLabel = body.style && state.gallery
      ? ` · ${state.gallery.styles[body.style].label}` : "";
    const baseStatus = resp.cached
      ? "已找到相同配置的现成模型"
      : (resp.reused
        ? "相同配置正在生成，已接入现有任务"
        : (mode === "draft" ? "生成 3D 预览中" : "生成打印模型中"));
    $("jobStatus").textContent = baseStatus + styleLabel + extra;
    $("jobStatus").className = "pill";
    $("jobStage").textContent = "正在准备地图与高程数据";
    renderJobProgress({
      progress_pct: resp.cached ? 100 : 2,
      duration_hint: mode === "draft"
        ? "通常需要 5–15 分钟"
        : "正式模型通常需要 15–40 分钟",
    });
    showJobToken(resp.job_id);
    refreshAccount();
    setBusy(true);
    pollJob();
  } catch (err) {
    alert("启动失败: " + err.message);
  }
}

function setBusy(busy) {
  $("btnDraft").disabled = busy || state.generationProfile !== "classic";
  $("btnFull").disabled = busy;
}

/** 从数据源服务器拉取区域 PBF（拉一次永久复用）*/
async function fetchPbf(region) {
  const btn = $("btnFetchPbf");
  if (btn) { btn.disabled = true; btn.textContent = "下载中…"; }
  try {
    const r = await fetchJSON("/api/fetch-pbf", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ region }),
    });
    if (r.state === "local") {
      $("customHint").textContent = `${region} 数据已就绪`;
      return;
    }
    state.job = { id: r.job_id, mode: "fetch", city: null, region };
    $("jobPanel").hidden = false;
    $("jobStatus").textContent = `下载 ${region} 数据中`;
    $("jobStatus").className = "pill";
    pollJob();
  } catch (err) {
    if (btn) { btn.disabled = false; btn.textContent = "↓ 下载数据"; }
    alert("下载失败: " + err.message);
  }
}

function renderJobProgress(job) {
  const pct = Math.max(0, Math.min(100, Math.round(job.progress_pct || 0)));
  const progress = $("jobProgress");
  progress.setAttribute("aria-valuenow", String(pct));
  $("jobProgressBar").style.width = `${pct}%`;
  $("jobProgressText").textContent = pct >= 100
    ? "已完成"
    : `阶段进度（估算）${pct}%`;
  $("jobEstimate").textContent = job.duration_hint
    ? `${job.duration_hint} · 可复制任务链接稍后回来`
    : "正在估算耗时，请耐心等待";
}

async function pollJob() {
  if (!state.job) return;
  try {
    const j = await fetchJSON(`/api/jobs/${state.job.id}`);
    $("jobElapsed").textContent = fmtDuration(j.elapsed_s);
    renderJobProgress(j);
    // 运行日志不外露；只展示友好状态，失败时显示归类后的异常提示
    const hint = $("jobHint");
    renderQualityChecks(j.quality_checks || []);
    // pending = 排队等 worker 拉取；running = 正在计算
    if (j.status === "pending") {
      const queueText = j.queue_position
        ? `当前队列第 ${j.queue_position} 位` : "等待计算节点接单";
      $("jobStatus").textContent = `⏳ 排队中 · ${queueText}`;
      $("jobStatus").className = "pill";
      $("jobStage").textContent = state.account
        ? "任务已保存到账号，可以关闭页面稍后回来"
        : "任务已保存，可以稍后凭令牌找回";
      setTimeout(pollJob, 3000);
      return;
    }
    if (j.status === "running") {
      $("jobStatus").textContent = "⏳ 正在生成，请耐心等待…";
      $("jobStatus").className = "pill";
      $("jobStage").textContent = j.stage_label || "正在构建地图图层与模型几何";
      setTimeout(pollJob, 2500);
      return;
    }
    const failed = j.status !== "done";
    $("jobStatus").textContent = failed ? "✕ 失败" : "✓ 完成";
    $("jobStatus").className = "pill " + j.status;
    $("jobStage").textContent = failed
      ? "生成未完成，请查看下方说明"
      : "模型与交付文件已经生成";
    if (failed) {
      hint.textContent = j.error_msg || "生成失败，请稍后重试";
      hint.hidden = false;
    } else if (j.quality_warnings && j.quality_warnings.length) {
      hint.textContent = `生成完成，但需要检查：${j.quality_warnings.join("；")}`;
      hint.hidden = false;
    } else {
      hint.hidden = true;
    }
    const { city, mode, region, slug } = state.job;
    state.jobSlug = slug;
    state.job = null;
    setBusy(false);
    refreshAccount();
    if (mode === "fetch") {
      // 数据到位 → 重查当前位置状态，清掉提示里的下载按钮
      if (j.status === "done") {
        state.pendingFetch = null;
        $("customHint").textContent = `${region} 数据已就绪，可生成了`;
        const q = $("lmInput").value.trim();
        if (q) lmSearch(q, false);
      }
      return;
    }
    if (mode === "styles") {
      $("btnConfirmArea").disabled = false;
      if (j.status === "done") {
        // 拉回刚生成的风格画廊，填到 Step 3
        try {
          state.gallery = await fetchJSON(`/api/gallery/${state.jobSlug}`);
          state.gallerySlug = state.jobSlug;
          if (state.pendingArea && state.pendingArea.slug === state.jobSlug) {
            state.galleryBbox = [...state.pendingArea.bbox];
          }
          state.selectedStyle = null;
          // 风格任务与模型任务共用区域 slug。找回已完成风格任务时，
          // 同时恢复该区域后来生成的最新 GLB/PNG；否则预览区会永久空白。
          await refreshArtifacts(state.jobSlug);
          renderStep3();
          renderViewer();
          $("step3").scrollIntoView({ behavior: "smooth", block: "start" });
        } catch (err) {
          $("galleryHint").textContent = "风格图加载失败: " + err.message;
        }
      } else {
        $("galleryHint").textContent = j.error_msg || "风格图生成失败，请稍后重试";
      }
      return;
    }
    if (j.status === "done") { await refreshArtifacts(city); persistState(); }
  } catch (err) {
    $("jobStatus").textContent = "轮询中断: " + err.message;
    state.job = null;
    setBusy(false);
  }
}

function renderQualityChecks(checks) {
  const el = $("qualityChecks");
  if (!checks.length) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.innerHTML = checks.map((check) => {
    const status = check.status === "pass" || check.status === "warning"
      ? check.status : "";
    return `
    <div class="quality-check ${status}">
      <strong>${esc(check.label)}</strong>
      <span>${esc(check.detail)}</span>
    </div>`;
  }).join("");
  el.hidden = false;
}

/* ---------------- 任务令牌：展示 + 找回 ---------------- */

/** 从令牌或完整链接中提取 job_id */
function parseJobToken(input) {
  const s = (input || "").trim();
  const m = s.match(/[?&]job=([a-zA-Z0-9]+)/);
  if (m) return m[1];
  if (/^[a-zA-Z0-9]{6,16}$/.test(s)) return s;   // 纯令牌
  return null;
}

/** 生成任务后展示令牌 + 复制链接 */
function showJobToken(job_id) {
  $("jobTokenRow").hidden = false;
  $("jobToken").textContent = job_id;
  $("jobLookup").value = job_id;
  const taskUrl = new URL(location.href);
  taskUrl.searchParams.set("job", job_id);
  history.replaceState({}, "", taskUrl);
  $("btnCopyJob").onclick = () => {
    const url = taskUrl.toString();
    const done = () => { $("btnCopyJob").textContent = "已复制✓";
      setTimeout(() => { $("btnCopyJob").textContent = "复制链接"; }, 1500); };
    const fallbackCopy = () => {
      const input = document.createElement("textarea");
      input.value = url;
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.appendChild(input);
      input.select();
      document.execCommand("copy");
      input.remove();
      done();
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(done).catch(fallbackCopy);
    } else { fallbackCopy(); }
  };
}

/** 用令牌/链接找回任务：查状态，完成则加载产物 */
async function lookupJob(input) {
  const id = parseJobToken(input);
  if (!id) { alert("令牌格式不对，请输入 6-16 位字母数字或完整链接"); return; }
  try {
    const j = await fetchJSON(`/api/jobs/${id}`);
    if (j.mode === "styles") restoreJobArea(j);
    state.job = { id, mode: j.mode, city: j.city, slug: j.city };
    $("jobPanel").hidden = false;
    showJobToken(id);
    $("jobStatus").textContent = "查询中…";
    $("jobStatus").className = "pill";
    setBusy(true);
    pollJob();
  } catch (err) {
    alert("找不到该任务: " + err.message);
  }
}

$("btnJobLookup").onclick = () => lookupJob($("jobLookup").value);
$("jobLookup").onkeydown = (e) => {
  if (e.key === "Enter") lookupJob($("jobLookup").value);
};

let lastArtifacts = null;

async function refreshArtifacts(city) {
  try {
    const r = await fetchJSON(`/api/artifacts/${city}`);
    lastArtifacts = r.artifacts;
  } catch (_) { return; }
  if (state.target.kind === "preset") await loadCities();
  renderViewer();
  renderDownloads();
}

function currentArtifacts() {
  const info = cityInfo();
  if (info) return info.artifacts;
  return lastArtifacts;
}

function renderViewer() {
  const art = currentArtifacts();
  // 2D 图（与 3D 同源图层）：旅程纪念平面图 + 轨迹标注图，上下排列
  const stamp = art && art.draft_glb
    ? encodeURIComponent(art.draft_glb.mtime) : Date.now();
  const pairs = [
    ["p2Topdown", "imgTopdown", art && art.topdown_png, "旅程纪念平面图"],
    ["p2Diag", "imgDiag", art && art.preview_png, "轨迹与标注"],
  ];
  let anyPng = false;
  for (const [figId, imgId, url, cap] of pairs) {
    const fig = $(figId);
    if (url) {
      const full = url + "?t=" + stamp;
      const img = $(imgId);
      img.src = full;
      img.onclick = () => openLightbox(full, cap);
      img.onerror = () => { fig.hidden = true; };  // 加载失败→隐藏，不放坏图
      fig.hidden = false;
      anyPng = true;
    } else {
      fig.hidden = true;
    }
  }
  $("preview2d").hidden = !anyPng;

  const glb = art && art.draft_glb;
  const wrap = $("viewerWrap");
  const old = wrap.querySelector("model-viewer");
  if (!glb) {
    if (old) old.remove();
    $("viewerEmpty").style.display = "flex";
    return;
  }
  $("glbInfo").textContent = `${glb.size_mb} MB · ${glb.mtime}`;
  $("viewerEmpty").style.display = "none";
  if (window.__mvFailed) {
    $("viewerEmpty").style.display = "flex";
    $("viewerEmpty").innerHTML =
      `3D 组件加载失败，可直接 <a href="${glb.url}" style="color:var(--accent)">下载 GLB</a>`;
    return;
  }
  const src = glb.url + "?" + glb.mtime;
  if (old && old.getAttribute("src") === src) return;
  if (old) old.remove();
  const mv = document.createElement("model-viewer");
  mv.setAttribute("src", src);
  mv.setAttribute("camera-controls", "");
  mv.setAttribute("shadow-intensity", "0.6");
  mv.setAttribute("exposure", "0.95");
  // 正视图：把平放的浮雕立起来（绕 X 轴 -90°），相机正对画面，
  // 像看一幅挂在墙上的浮雕；底座在背面，自然看不到
  mv.setAttribute("orientation", "-90deg 0deg 0deg");
  mv.setAttribute("camera-orbit", "0deg 90deg 105%");
  // 允许左右转、少量仰俯，但不能绕到背面看底座
  mv.setAttribute("min-camera-orbit", "-75deg 55deg auto");
  mv.setAttribute("max-camera-orbit", "75deg 115deg auto");
  wrap.appendChild(mv);
}

function renderDownloads() {
  const art = currentArtifacts();
  const dls = $("downloads");
  dls.innerHTML = "";
  if (!art) return;
  for (const m of art.models_3mf || []) {
    const a = document.createElement("a");
    a.className = "dl-item";
    a.href = m.url;
    a.download = m.name;
    a.innerHTML = `<span class="dl-name">📦 ${m.name}</span>
                   <span class="meta">${m.size_mb} MB · ${m.mtime}</span>`;
    dls.appendChild(a);
  }
  if (art.draft_glb) {
    const a = document.createElement("a");
    a.className = "dl-item";
    a.href = art.draft_glb.url;
    a.download = "draft.glb";
    a.innerHTML = `<span class="dl-name">🧊 Draft GLB（预览模型）</span>
                   <span class="meta">${art.draft_glb.size_mb} MB</span>`;
    dls.appendChild(a);
  }
  if (art.preview_png) {
    const a = document.createElement("a");
    a.className = "dl-item";
    a.href = art.preview_png;
    a.target = "_blank";
    a.innerHTML = '<span class="dl-name">🖼 轨迹标注图 PNG</span><span class="meta">查看</span>';
    dls.appendChild(a);
  }
  if (art.topdown_png) {
    const a = document.createElement("a");
    a.className = "dl-item";
    a.href = art.topdown_png;
    a.target = "_blank";
    a.innerHTML = '<span class="dl-name">🖼 旅程纪念平面图 PNG（高清）</span><span class="meta">查看</span>';
    dls.appendChild(a);
  }
  if (art.design_spec) {
    const a = document.createElement("a");
    a.className = "dl-item";
    a.href = art.design_spec.url;
    a.download = art.design_spec.name || "design_spec.json";
    a.innerHTML = '<span class="dl-name">⚙️ Design Spec（生成参数与验收证据）</span><span class="meta">JSON</span>';
    dls.appendChild(a);
  }
}

function fmtDuration(s) {
  return s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.round(s)}s`;
}

/* ---------------- 灯箱 ---------------- */

function openLightbox(src, cap) {
  $("lightboxImg").src = src;
  $("lightboxCap").textContent = cap;
  $("lightbox").hidden = false;
  document.body.style.overflow = "hidden";
}
function closeLightbox() {
  $("lightbox").hidden = true;
  document.body.style.overflow = "";
}

/* ---------------- 事件绑定与启动 ---------------- */

/** 复制“我的链接”到剪贴板（跨设备恢复用） */
function copyMyLink() {
  const s = getSession();
  const url = `${location.origin}${location.pathname}?s=${s.id}`;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(() => {
      alert("链接已复制！\n在任何设备打开这个链接即可恢复当前状态。\n\n" + url);
    });
  } else {
    // fallback
    const ta = document.createElement("textarea");
    ta.value = url;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
    alert("链接已复制！\n\n" + url);
  }
  // 确保云端有最新状态
  persistState();
}
window.copyMyLink = copyMyLink;  // 暴露给 onclick

window.__mvFallback = () => { window.__mvFailed = true; };

$("btnDraft").onclick = () => startJob("draft");
$("btnFull").onclick = () => startJob("full");
$("lightbox").onclick = closeLightbox;
$("lightboxClose").onclick = closeLightbox;

syncSize();
initHeroShowcase();
window.addEventListener("load", async () => {
  initMap();
  if (window.location.protocol !== "file:") {
    lmSearch("", false);   // 预取目录缓存，不弹下拉
  }
  await refreshAccount();
  await loadShowcase();
  await restoreSession();  // 恢复上次会话
  syncGenerationProfileAvailability();
  // 任务链接 ?job=xxx → 自动找回该任务
  const jobParam = new URLSearchParams(window.location.search).get("job");
  if (jobParam) { $("jobLookup").value = jobParam; lookupJob(jobParam); }
});

loadCities().catch((e) => {
  if (window.location.protocol === "file:") {
    const input = $("lmInput");
    input.placeholder = "静态预览不含景点数据，请通过网页服务打开";
    return;
  }
  document.body.insertAdjacentHTML("beforeend",
    `<div style="color:#d97762;text-align:center;padding:20px">景点目录加载失败: ${e.message}</div>`);
});
