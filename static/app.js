/* 山峰可视域计算 - 前端逻辑 */
"use strict";

const state = {
  observer: null,          // {lon, lat, elevation, radius}
  inputMode: "text",       // 'text' | 'map'（坐标输入方式，校验逻辑由其决定）
  picked: null,            // {lon, lat} 地图点选采集的观测点坐标
  map: null,
  tileLayer: null,
  overlayGroup: null,      // 曲率圆 + 可视域 + 观测点标记
  clickMarker: null,
  liftLine: null,          // 最近一次测高的测地线连线（仅保留最新一条）
  lastLiftDisplay: null,   // 最近一次测高的剖面显示数据（单位切换时重绘用）
  busyCount: 0,
};

const $ = (id) => document.getElementById(id);
const log = (...args) => console.log("[pva]", ...args);
const warn = (...args) => console.warn("[pva]", ...args);
const FT_PER_M = 3.280839895;
const MIN_DISPLAY_M = 5000;
const R_EARTH_M = 6371000;

function setBusy(on) {
  state.busyCount = Math.max(0, state.busyCount + (on ? 1 : -1));
  $("busy").classList.toggle("hidden", state.busyCount === 0);
}

async function api(path, body) {
  const t0 = performance.now();
  log("→", body ? "POST" : "GET", path, body ?? "");
  const resp = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    warn("←", path, `HTTP ${resp.status} (${(performance.now() - t0).toFixed(0)}ms)`,
         data.detail || "");
    throw new Error(data.detail || `请求失败（HTTP ${resp.status}）`);
  }
  log("←", path, `+${(performance.now() - t0).toFixed(0)}ms`);
  return data;
}

function fmtLength(m) {
  if (m >= 10000) return `${(m / 1000).toFixed(1)} km`;
  return `${m.toFixed(0)} m`;
}

function fmtHeight(m) {
  return $("unit").value === "ft"
    ? `${(m * FT_PER_M).toLocaleString("en-US", { maximumFractionDigits: 1 })} ft`
    : `${m.toLocaleString("en-US", { maximumFractionDigits: 1 })} m`;
}

function fmtCoord(lon, lat) {
  const hemi = (v, pos, neg) => (v >= 0 ? pos : neg);
  return `${Math.abs(lat).toFixed(5)}°${hemi(lat, "N", "S")} ` +
         `${Math.abs(lon).toFixed(5)}°${hemi(lon, "E", "W")}`;
}

function demSourceConfig() {
  const sel = $("dem");
  if (sel.value === "offline-geotiff") {
    const path = $("dem-file").value.trim();
    if (!path) throw new Error("请在高级设置中填写离线 GeoTIFF 高程文件路径");
    return { type: "geotiff", path };
  }
  return { type: "terrarium" };
}

function refraction() { return $("refraction").value; }

/** 有效地球半径（与折射设置联动：纯几何 R / 标准大气折射 7/6 R）。 */
function effEarthRadiusM() {
  return refraction() === "standard" ? R_EARTH_M * 7 / 6 : R_EARTH_M;
}

/* ---------------- 地图 ---------------- */

function initMap() {
  state.map = L.map("map", { zoomControl: true, worldCopyJump: true }).setView([35, 105], 4);
  state.overlayGroup = L.layerGroup().addTo(state.map);
  setBasemapOnline();
  state.map.on("click", onMapClick);
}

function setBasemapOnline() {
  if (state.tileLayer) { state.map.removeLayer(state.tileLayer); state.tileLayer = null; }
  state.tileLayer = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: '&copy; OpenStreetMap contributors',
  }).addTo(state.map);
}

async function setBasemapOffline() {
  if (state.tileLayer) { state.map.removeLayer(state.tileLayer); state.tileLayer = null; }
  const path = $("basemap-file").value.trim();
  if (!path) return;
  setBusy(true);
  try {
    const data = await api("/api/basemap", { path });
    const b = data.bounds;
    state.overlayGroup.addLayer(L.imageOverlay(data.image,
      [[b[1], b[0]], [b[3], b[2]]], { opacity: 0.85 }));
    state.map.fitBounds([[b[1], b[0]], [b[3], b[2]]]);
  } catch (err) {
    showFeedback(`底图加载失败：${err.message}`, false);
  } finally {
    setBusy(false);
  }
}

function maxZoomForMinSpan(lat) {
  // 最小显示范围兜底：视口较短边对应地面跨度不小于 5 km
  const size = state.map.getSize();
  const minPx = Math.min(size.x, size.y) || 600;
  const earthCirc = 40075016.686;
  const z = Math.log2(earthCirc * Math.cos(lat * Math.PI / 180) * minPx / (256 * MIN_DISPLAY_M));
  return Math.max(2, Math.floor(z));
}

function applyZoomConstraints() {
  const { observer, map } = state;
  if (!observer) return;
  // 显示范围上限：曲率圆包围盒外扩 10%（限制平移/缩小的范围）
  const [sw, ne] = radiusBounds(observer.lat, observer.lon, observer.radius);
  const dLat = (ne.lat - sw.lat) * 0.1 + 0.2;
  const dLng = (ne.lng - sw.lng) * 0.1 + 0.2;
  map.setMaxBounds(L.latLngBounds(
    [[sw.lat - dLat, sw.lng - dLng], [ne.lat + dLat, ne.lng + dLng]],
  ));
  map.setMinZoom(2);
  map.setMaxZoom(maxZoomForMinSpan(observer.lat));
}

function radiusBounds(lat, lon, radiusM) {
  const dLat = radiusM / 111320.0;
  const dLon = radiusM / (111320.0 * Math.cos(lat * Math.PI / 180));
  return [L.latLng(lat - dLat, lon - dLon), L.latLng(lat + dLat, lon + dLon)];
}

/* ---------------- 输入模式切换（文本框输入 / 地图点选） ---------------- */

function updatePhaseBar() {
  const badge = $("phase-badge");
  const toggle = $("phase-toggle");
  if (state.inputMode === "map") {
    badge.textContent = "选点中";
    badge.className = "phase-badge picking";
    toggle.textContent = "退出选点";
  } else if (state.observer) {
    badge.textContent = "可测高";
    badge.className = "phase-badge ready";
    toggle.textContent = "进入地图选点";
  } else {
    badge.textContent = "未设定观测点";
    badge.className = "phase-badge unset";
    toggle.textContent = "进入地图选点";
  }
}

function setInputMode(mode, options = {}) {
  const prevMode = state.inputMode;
  if (state.inputMode === mode) {
    updatePhaseBar();
    return;
  }
  log(`输入模式切换: ${prevMode} → ${mode}`);
  const leavingPick = state.inputMode === "map";
  state.inputMode = mode;
  document.querySelectorAll(".seg-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.mode === mode);
  });
  const isText = mode === "text";
  $("fmt-row").classList.toggle("hidden", !isText);
  $("text-input-row").classList.toggle("hidden", !isText);
  $("pick-row").classList.toggle("hidden", isText);
  $("pick-hint").classList.toggle("hidden", isText);
  if (leavingPick) {
    // 退出点选模式：按当前观测点恢复平移/缩放约束
    if (state.observer) applyZoomConstraints();
    else { state.map.setMinZoom(2); state.map.setMaxZoom(18); state.map.setMaxBounds(null); }
    if (options.clearFeedback !== false) showFeedback("", true);
  } else {
    // 进入点选模式：临时解除平移/缩放约束
    state.map.setMaxBounds(null);
  }
  updatePhaseBar();
}

function onSegBtnClick(e) {
  const btn = e.currentTarget;
  if (btn.classList.contains("active")) return;
  setInputMode(btn.dataset.mode);
}

/** 点选确认成功后的自动退出：清空点选状态并切回文本框输入模式（保留成功反馈）。 */
function exitPickAfterConfirm() {
  state.picked = null;
  $("picked-coord").value = "";
  setInputMode("text", { clearFeedback: false });
}

function normalizeLon(lon) {
  return ((lon + 180) % 360 + 360) % 360 - 180;
}

/* ---------------- 观测点确认流程 ---------------- */

function showFeedback(msg, ok) {
  const el = $("feedback");
  el.textContent = msg;
  el.className = "feedback " + (ok ? "ok" : "err");
}

async function onConfirm() {
  const btn = $("confirm");
  btn.disabled = true;
  try {
    let payload;
    if (state.inputMode === "map") {
      if (!state.picked) {
        showFeedback("请先在地图上点选观测点坐标", false);
        return;
      }
      // 点选坐标天然合法：仍以十进制度走统一校验入口，预期直接“校验成功”
      payload = {
        text: `${state.picked.lon.toFixed(6)}, ${state.picked.lat.toFixed(6)}`,
        format: "decimal",
      };
    } else {
      payload = { text: $("coord").value.trim(), format: $("fmt").value };
    }
    const v = await api("/api/validate", payload);
    if (!v.ok) {
      showFeedback(v.message, false);
      return;
    }
    log("校验通过:", v.lon, v.lat, `模式=${state.inputMode}`);
    showFeedback("校验成功，正在计算可视域…", true);
    await loadObserver(v.lon, v.lat);
    if (state.inputMode === "map" && state.observer) {
      // 确认生效后自动切回文本框输入模式，并清空点选状态
      exitPickAfterConfirm();
    }
  } catch (err) {
    showFeedback(err.message, false);
  } finally {
    btn.disabled = false;
  }
}

async function loadObserver(lon, lat) {
  setBusy(true);
  // 自动替换：清除旧结果（含点选测高）
  state.overlayGroup.clearLayers();
  resetLiftPanel();

  let source;
  try { source = demSourceConfig(); } catch (err) { showFeedback(err.message, false); return; }

  try {
    const hz = await api("/api/horizon",
      { lon, lat, refraction: refraction(), dem_source: source });
    state.observer = { lon, lat, elevation: hz.elevation_m, radius: hz.display_radius_m };
    log("地平线结果: 高程=%.1fm 半径=%.1fkm 兜底=%s DEM=%s",
        hz.elevation_m, hz.display_radius_m / 1000, hz.fallback, hz.dem_source);

    const vs = await api("/api/viewshed", {
      lon, lat, refraction: refraction(), engine: $("engine").value,
      radius_m: hz.display_radius_m, dem_source: source,
    });

    // 曲率上限圆（虚线）
    L.circle([lat, lon], {
      radius: hz.display_radius_m,
      color: "#2469ce", weight: 1.5, dashArray: "6 6", fill: false,
    }).addTo(state.overlayGroup);
    // 可视域叠加（半透明填充）
    const b = vs.bounds;
    L.imageOverlay(vs.image, [[b[1], b[0]], [b[3], b[2]]], { opacity: 1.0 })
      .addTo(state.overlayGroup);
    // 观测点标记
    L.circleMarker([lat, lon], { radius: 6, color: "#c0392b", weight: 2, fillOpacity: 0.9 })
      .bindTooltip(`观测点 ${fmtCoord(lon, lat)}`).addTo(state.overlayGroup);

    state.map.fitBounds(radiusBounds(lat, lon, hz.display_radius_m));
    // 地图点选模式期间保持解除约束（退出该模式时再按当前观测点恢复）
    if (state.inputMode !== "map") applyZoomConstraints();
    log("可视域结果: 可见=%s 格网=%s 引擎=%s 耗时=%ss 图层=%dKB",
        vs.visible_cells, vs.grid_shape.join("×"), vs.engine, vs.elapsed_s,
        Math.round(vs.image.length / 1024));

    const fb = hz.fallback
      ? `校验成功。地平线半径过小，已兜底为 ${MIN_DISPLAY_M / 1000} km`
      : "校验成功";
    showFeedback(fb, true);
    $("obs-info").innerHTML =
      `观测点：${fmtCoord(lon, lat)}<br>` +
      `高程：${fmtHeight(hz.elevation_m)}（DEM：${hz.dem_source}）<br>` +
      `曲率上限半径：${fmtLength(hz.display_radius_m)}` +
      (hz.fallback ? "（最小显示半径兜底）" : "") + `<br>` +
      `可视域：${vs.visible_cells.toLocaleString("en-US")} 格可见 / ` +
      `格网 ${vs.grid_shape.join("×")}，引擎 ${vs.engine}，耗时 ${vs.elapsed_s}s`;
    updatePhaseBar();
  } catch (err) {
    state.observer = null;
    warn("观测点加载失败:", err.message);
    showFeedback(err.message, false);
    updatePhaseBar();
  } finally {
    setBusy(false);
  }
}

/* ---------------- 点选测高 ---------------- */

function resetLiftPanel() {
  $("lift-result").classList.add("hidden");
  $("lift-hint").classList.remove("hidden");
  $("profile-box").classList.add("hidden");
  $("profile-chart").textContent = "";
  $("profile-error").classList.add("hidden");
  state.lastLiftDisplay = null;
  clearLiftLine();
  if (state.clickMarker) { state.map.removeLayer(state.clickMarker); state.clickMarker = null; }
}

/** 曲率修正（弦线基准）显示值：z(d) − d(D−d)/(2·R_eff)，平地呈两端高中间低的弧线。 */
function computeProfileDisplay(profile, rEffM) {
  const d = profile.dist_m, e = profile.elev_m;
  const total = d[d.length - 1];
  return d.map((di, i) => e[i] - (di * (total - di)) / (2 * rEffM));
}

function profileSvgEl(tag, attrs = {}) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
}

/** 渲染剖面缩略图：display 为 {profile, feasible, lift_m}；剖面缺失时显示错误占位。 */
function renderProfileThumbnail(display) {
  const chart = $("profile-chart"), err = $("profile-error");
  chart.textContent = "";
  if (!display || !display.profile) {
    err.classList.remove("hidden");
    err.textContent = "剖面取数失败（DEM 不可用或超出覆盖范围）";
    return;
  }
  err.classList.add("hidden");

  const d = display.profile.dist_m;
  const z = computeProfileDisplay(display.profile, effEarthRadiusM());
  const total = d[d.length - 1];
  const lift = display.feasible ? Math.max(0, display.lift_m || 0) : 0;
  const sight0 = z[0], sight1 = z[z.length - 1] + lift; // 抬升后点击点 → 观测点

  let lo = Math.min(sight0, sight1), hi = Math.max(sight0, sight1);
  for (const v of z) { if (v < lo) lo = v; if (v > hi) hi = v; }
  const pad = Math.max((hi - lo) * 0.08, 1.0);
  lo -= pad; hi += pad;

  const W = 480, H = 190, ML = 52, MR = 12, MT = 14, MB = 30;
  const px = (di) => ML + (di / total) * (W - ML - MR);
  const py = (v) => MT + ((hi - v) / (hi - lo)) * (H - MT - MB);

  const svg = profileSvgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%" });

  const inFt = $("unit").value === "ft";
  const fmtTick = (m) => Math.round(inFt ? m * FT_PER_M : m).toLocaleString("en-US");
  const fmtKm = (m) => (m / 1000).toLocaleString("en-US", { maximumFractionDigits: 1 });
  for (let i = 0; i <= 4; i++) {
    const v = lo + ((hi - lo) * i) / 4;
    const y = py(v);
    svg.appendChild(profileSvgEl("line", {
      x1: ML, y1: y, x2: W - MR, y2: y, stroke: "#e2e4e8", "stroke-width": 1,
    }));
    const t = profileSvgEl("text", { x: ML - 6, y: y + 4, "text-anchor": "end", class: "tick" });
    t.textContent = fmtTick(v);
    svg.appendChild(t);
  }
  for (let i = 0; i <= 4; i++) {
    const di = (total * i) / 4;
    const t = profileSvgEl("text", { x: px(di), y: H - 8, "text-anchor": "middle", class: "tick" });
    t.textContent = fmtKm(di);
    svg.appendChild(t);
  }
  const yTitle = profileSvgEl("text", { x: ML - 6, y: MT - 3, "text-anchor": "end", class: "axis-title" });
  yTitle.textContent = `高程 (${inFt ? "ft" : "m"})`;
  svg.appendChild(yTitle);
  const xTitle = profileSvgEl("text", { x: W - MR, y: H - 8, "text-anchor": "end", class: "axis-title" });
  xTitle.textContent = "距离 (km)";
  svg.appendChild(xTitle);

  const pts = d.map((di, i) => `${px(di).toFixed(1)},${py(z[i]).toFixed(1)}`).join(" ");
  svg.appendChild(profileSvgEl("polygon", {
    points: `${ML},${H - MB} ${pts} ${W - MR},${H - MB}`,
    fill: "rgba(160,99,42,0.14)", stroke: "none",
  }));
  svg.appendChild(profileSvgEl("polyline", {
    points: pts, fill: "none", stroke: "#a0632a", "stroke-width": 1.5,
  }));

  svg.appendChild(profileSvgEl("line", {
    x1: px(0), y1: py(sight0), x2: px(total), y2: py(sight1),
    stroke: "#2469ce", "stroke-width": 1.2, "stroke-dasharray": "6 4",
  }));
  for (const [di, v] of [[0, sight0], [total, sight1]]) {
    svg.appendChild(profileSvgEl("circle", { cx: px(di), cy: py(v), r: 2.5, fill: "#2469ce" }));
  }

  if (!display.feasible) {
    const t = profileSvgEl("text", {
      x: (ML + W - MR) / 2, y: (MT + H - MB) / 2, "text-anchor": "middle", class: "nosolve",
    });
    t.textContent = "无解";
    svg.appendChild(t);
  }
  chart.appendChild(svg);
}

function clearLiftLine() {
  if (state.liftLine) { state.map.removeLayer(state.liftLine); state.liftLine = null; }
}

/** 渲染观测点↔点击点测地线连线（与剖面采样同一条）；新连线覆盖旧连线。 */
function updateLiftLine(geodesic) {
  clearLiftLine();
  if (!Array.isArray(geodesic) || geodesic.length < 2) return;
  const latlngs = geodesic.map(([lon, lat]) => [lat, normalizeLon(lon)]);
  state.liftLine = L.polyline(latlngs, {
    color: "#c0392b", weight: 2.5, opacity: 0.9, dashArray: "8 6",
  }).addTo(state.map);
}

async function onMapClick(e) {
  if (state.inputMode === "map") {
    // 地图点选模式：点击即采集观测点坐标（覆盖旧值），点选测高暂不可用
    const lon = normalizeLon(e.latlng.lng);
    const lat = Math.max(-90, Math.min(90, e.latlng.lat));
    state.picked = { lon, lat };
    $("picked-coord").value = `${lon.toFixed(6)}, ${lat.toFixed(6)}`;
    log("点选观测点:", lon.toFixed(6), lat.toFixed(6));
    showFeedback(`已点选观测点：${fmtCoord(lon, lat)}，点击【确认输入】生效`, true);
    return;
  }
  if (!state.observer) {
    $("lift-hint").innerHTML = "请先在左侧确认观测点，再点击地图查询。";
    return;
  }
  const lon = e.latlng.lng, lat = e.latlng.lat;
  setBusy(true);
  try {
    const data = await api("/api/lift", {
      obs_lon: state.observer.lon, obs_lat: state.observer.lat,
      click_lon: lon, click_lat: lat,
      refraction: refraction(), dem_source: demSourceConfig(),
    });
    $("lift-hint").classList.add("hidden");
    $("lift-result").classList.remove("hidden");
    $("click-pos").textContent = fmtCoord(lon, lat);
    $("click-dist").textContent = fmtLength(data.distance_m);
    if (data.feasible) {
      $("click-elev").textContent = fmtHeight(data.clicked_elev_m);
      const v = data.lift_m;
      $("lift-value").textContent = v <= 0.05 ? `无需抬升（已通视）` : fmtHeight(v);
      $("lift-value").style.color = "";
    } else {
      $("click-elev").textContent = "—";
      $("lift-value").textContent = data.message;
      $("lift-value").style.color = "#c0392b";
    }
    if (state.clickMarker) state.map.removeLayer(state.clickMarker);
    state.clickMarker = L.circleMarker([lat, lon],
      { radius: 5, color: "#2b2f36", weight: 2, fillOpacity: 0.6 })
      .bindTooltip(`点击点 ${fmtCoord(lon, lat)}`).addTo(state.map);
    // 剖面缩略图 + 测地线连线（几何不可行同样渲染；剖面缺失显示错误占位）
    state.lastLiftDisplay = { profile: data.profile, feasible: data.feasible, lift_m: data.lift_m };
    $("profile-box").classList.remove("hidden");
    renderProfileThumbnail(state.lastLiftDisplay);
    updateLiftLine(data.geodesic);
    log("测高结果: 可行=%s 抬升=%s 距离=%.0fm 剖面=%s 连线=%d 点",
        data.feasible, data.lift_m !== null ? `${data.lift_m.toFixed(1)}m` : data.message,
        data.distance_m, data.profile ? `${data.profile.dist_m.length} 点` : "不可用",
        Array.isArray(data.geodesic) ? data.geodesic.length : 0);
  } catch (err) {
    warn("测高查询失败:", err.message);
    $("lift-hint").classList.remove("hidden");
    $("lift-hint").textContent = err.message;
    $("lift-hint").classList.add("err");
    // 测高未完成：新连线不渲染；旧连线清除，旧剖面转错误占位
    clearLiftLine();
    if (!$("profile-box").classList.contains("hidden")) {
      state.lastLiftDisplay = null;
      renderProfileThumbnail(null);
    }
  } finally {
    setBusy(false);
  }
}

/* ---------------- 初始化 ---------------- */

async function loadOptions() {
  const engines = await api("/api/engines");
  const engineSel = $("engine");
  for (const e of engines) {
    const opt = new Option(e.name, e.id);
    engineSel.add(opt);
  }
  engineSel.value = "angular-ray-sweep";
  $("engine-desc").textContent = engines[0].description;

  const sources = await api("/api/dem-sources");
  const demSel = $("dem");
  for (const s of sources) demSel.add(new Option(s.name, s.id));
  demSel.value = "aws-terrain-tiles";
  $("dem-desc").textContent = sources[0].description;
}

function bindEvents() {
  $("confirm").addEventListener("click", onConfirm);
  $("coord").addEventListener("keydown", (e) => { if (e.key === "Enter") onConfirm(); });
  document.querySelectorAll(".seg-btn").forEach((btn) => {
    btn.addEventListener("click", onSegBtnClick);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.inputMode === "map") setInputMode("text");
  });
  $("phase-toggle").addEventListener("click", () => {
    setInputMode(state.inputMode === "map" ? "text" : "map");
  });

  $("engine").addEventListener("change", async () => {
    const engines = await api("/api/engines");
    const cur = engines.find((e) => e.id === $("engine").value);
    $("engine-desc").textContent = cur ? cur.description : "";
    if (state.observer) loadObserver(state.observer.lon, state.observer.lat);
  });

  $("dem").addEventListener("change", async () => {
    const sources = await api("/api/dem-sources");
    const cur = sources.find((s) => s.id === $("dem").value);
    $("dem-desc").textContent = cur ? cur.description : "";
    $("dem-file-row").classList.toggle("hidden", $("dem").value !== "offline-geotiff");
  });

  $("basemap").addEventListener("change", () => {
    const offline = $("basemap").value === "offline";
    $("basemap-file-row").classList.toggle("hidden", !offline);
    if (!offline) setBasemapOnline();
  });
  $("basemap-apply").addEventListener("click", setBasemapOffline);

  $("refraction").addEventListener("change", () => {
    if (state.observer) loadObserver(state.observer.lon, state.observer.lat);
  });
  $("unit").addEventListener("change", () => {
    if (state.lastLiftDisplay) renderProfileThumbnail(state.lastLiftDisplay);
    if (state.observer) {
      $("obs-info").innerHTML =
        `观测点：${fmtCoord(state.observer.lon, state.observer.lat)}<br>` +
        `高程：${fmtHeight(state.observer.elevation)}<br>` +
        `曲率上限半径：${fmtLength(state.observer.radius)}`;
    }
  });
}

function waitForLeaflet(timeoutMs = 5000) {
  if (typeof L !== "undefined") return Promise.resolve();
  return new Promise((resolve) => {
    const t0 = Date.now();
    const timer = setInterval(() => {
      if (typeof L !== "undefined" || Date.now() - t0 > timeoutMs) {
        clearInterval(timer);
        resolve();
      }
    }, 50);
  });
}

/** 轻量页面自检（?selftest=1）：验证输入模式切换/阶段指示/自动退出链路，结果写入 DOM。 */
async function runSelfTest() {
  const results = [];
  const check = (name, cond) => results.push(`${cond ? "PASS" : "FAIL"} ${name}`);
  const segActive = (mode) =>
    document.querySelector(`.seg-btn[data-mode="${mode}"]`).classList.contains("active");

  check("初始阶段=未设定观测点", $("phase-badge").textContent === "未设定观测点");
  check("初始按钮=进入地图选点", $("phase-toggle").textContent === "进入地图选点");

  $("phase-toggle").click();
  check("切换后阶段=选点中", $("phase-badge").textContent === "选点中");
  check("按钮=退出选点", $("phase-toggle").textContent === "退出选点");
  check("分段控件联动=地图点选激活", segActive("map"));
  check("只读坐标框可见", !$("pick-row").classList.contains("hidden"));
  check("文本输入框隐藏", $("text-input-row").classList.contains("hidden"));

  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
  check("Esc 后阶段=未设定观测点", $("phase-badge").textContent === "未设定观测点");
  check("Esc 后分段=文本框激活", segActive("text"));
  check("Esc 后只读框隐藏", $("pick-row").classList.contains("hidden"));

  $("phase-toggle").click();
  state.picked = { lon: 116.397, lat: 39.909 };
  $("picked-coord").value = "116.397000, 39.909000";
  state.observer = { lon: 116.397, lat: 39.909, elevation: 50, radius: 25240 };
  exitPickAfterConfirm();
  check("确认后自动退出=可测高", $("phase-badge").textContent === "可测高");
  check("点选状态已清空", state.picked === null && $("picked-coord").value === "");
  check("确认后分段=文本框激活", segActive("text"));
  check("确认后按钮=进入地图选点", $("phase-toggle").textContent === "进入地图选点");

  // —— 剖面缩略图与测地线连线 ——
  const synth = {
    dist_m: Array.from({ length: 41 }, (_, i) => i * 500),
    elev_m: Array(41).fill(100),
  };
  const zDisp = computeProfileDisplay(synth, R_EARTH_M);
  const sag = zDisp[0] - zDisp[20];
  check("曲率修正中点凹陷≈D²/8R", Math.abs(sag - 20000 * 20000 / (8 * R_EARTH_M)) < 2.0);
  state.lastLiftDisplay = { profile: synth, feasible: true, lift_m: 31.4 };
  $("profile-box").classList.remove("hidden");
  renderProfileThumbnail(state.lastLiftDisplay);
  check("剖面SVG已渲染", $("profile-chart").querySelector("svg") !== null);
  check("可行结果无无解标注", !$("profile-chart").textContent.includes("无解"));
  check("高程轴单位=m", $("profile-chart").textContent.includes("高程 (m)"));
  renderProfileThumbnail({ profile: synth, feasible: false, lift_m: null });
  check("几何不可行叠加无解标注", $("profile-chart").textContent.includes("无解"));
  $("unit").value = "ft";
  renderProfileThumbnail({ profile: synth, feasible: true, lift_m: 31.4 });
  check("高程轴单位联动=ft", $("profile-chart").textContent.includes("高程 (ft)"));
  $("unit").value = "m";
  renderProfileThumbnail(null);
  check("剖面缺失显示错误占位", !$("profile-error").classList.contains("hidden"));
  updateLiftLine([[116.0, 39.5], [116.5, 39.8], [117.0, 40.1]]);
  check("测地线连线已渲染", state.liftLine !== null && state.map.hasLayer(state.liftLine));
  updateLiftLine([[116.2, 39.6], [116.9, 40.0]]);
  check("新连线覆盖旧连线", state.liftLine !== null
    && state.liftLine.getLatLngs().length === 2 && state.map.hasLayer(state.liftLine));
  resetLiftPanel();
  check("重置时连线与剖面随旧结果清除",
        state.liftLine === null && $("profile-box").classList.contains("hidden"));

  state.observer = null;
  updatePhaseBar();
  const pre = document.createElement("pre");
  pre.id = "selftest-results";
  pre.textContent = results.join("\n");
  document.body.appendChild(pre);
}

window.addEventListener("DOMContentLoaded", async () => {
  // CDN 失败时，index.html 的 onerror 会补挂本地 Leaflet；此处等待其就绪
  await waitForLeaflet();
  initMap();
  try {
    await loadOptions();
    bindEvents();
    updatePhaseBar();
    if (new URLSearchParams(location.search).has("selftest")) await runSelfTest();
  } catch (err) {
    showFeedback(`初始化失败：${err.message}`, false);
  }
});
