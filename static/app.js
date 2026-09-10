/* 山峰可视域计算 - 前端逻辑 */
"use strict";

const state = {
  observer: null,          // {lon, lat, elevation, radius}
  inputMode: "text",       // 'text' | 'map'（坐标输入方式，校验逻辑由其决定）
  picked: null,            // {lon, lat} 地图点选采集的观测点坐标
  map: null,
  tileLayer: null,
  overlayGroup: null,      // 曲率圆 + 可视域 + 观测点标记
  peakGroup: null,         // 附近制高点候选 marker + 搜索范围圆（随最新结果替换）
  clickMarker: null,
  liftLine: null,          // 最近一次测高的测地线连线（仅保留最新一条）
  lastLiftDisplay: null,   // 最近一次测高的剖面显示数据（单位切换时重绘用）
  pendingText: null,       // 文本框当前内容经即时校验得到的待确认坐标 {lon, lat}
  peak: { pick: null, tune: null },      // 两工具各自最新一次搜索结果（含 center/radius_m）
  peakHintKind: { pick: "", tune: "" },  // 提示类型标记（err 时保留至下次搜索）
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
  state.peakGroup = L.layerGroup().addTo(state.map);
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
  updatePeakTools();
  if (isText) scheduleTextValidate();
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
  updatePeakTools();
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
  // 自动替换：清除旧结果（含点选测高与附近制高点搜索）
  state.overlayGroup.clearLayers();
  resetLiftPanel();
  resetPeakPanels();

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

/** 曲率修正（弦线基准）显示值：z(d) + d(D−d)/(2·R_eff)，平地呈两端低中间高的弧线（地球凸起）。 */
function computeProfileDisplay(profile, rEffM) {
  const d = profile.dist_m, e = profile.elev_m;
  const total = d[d.length - 1];
  return d.map((di, i) => e[i] + (di * (total - di)) / (2 * rEffM));
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
  const rEff = effEarthRadiusM();
  const z = computeProfileDisplay(display.profile, rEff);
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

  // 图注：说明曲率修正量与视线含义（单位随米/英尺联动）
  const cap = document.createElement("div");
  cap.className = "profile-caption";
  cap.textContent =
    `剖面含地球曲率修正（弦线基准，中点凸起≈${fmtHeight((total * total) / (8 * rEff))}）；` +
    "蓝色虚线为抬升后的视线（直线）。";
  chart.appendChild(cap);
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
    updatePeakTools();
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

/* ---------------- 附近制高点搜索（选点辅助 / 观测点微调辅助） ---------------- */

const PEAK_TOOLS = ["pick", "tune"];

/** 工具一（选点辅助）的搜索中心：点选模式=已点选坐标；文本模式=即时校验通过的文本坐标。 */
function selectionCenter() {
  if (state.inputMode === "map") return state.picked ? { ...state.picked } : null;
  return state.pendingText ? { ...state.pendingText } : null;
}

function showPeakHint(tool, msg, kind) {
  const el = $(`peak-hint-${tool}`);
  el.textContent = msg;
  el.className = "feedback" + (kind === "err" ? " err" : "");
  state.peakHintKind[tool] = kind || "";
}

/** 按阶段刷新两工具可用性：选点辅助=有待确认中心；微调辅助=已确认观测点。 */
function updatePeakTools() {
  const hasPickCenter = !!selectionCenter();
  $("peak-search-pick").disabled = !hasPickCenter;
  if (!hasPickCenter) {
    showPeakHint("pick", state.inputMode === "map"
      ? "先在地图上点选，再搜索附近制高点"
      : "文本框输入有效坐标后可搜索附近制高点", "");
  } else if (state.peakHintKind.pick !== "err") {
    showPeakHint("pick", "", "");
  }
  $("peak-search-tune").disabled = !state.observer;
  if (!state.observer) {
    showPeakHint("tune", "确认观测点后可用", "");
  } else if (state.peakHintKind.tune !== "err") {
    showPeakHint("tune", "", "");
  }
}

/** 即时校验文本框坐标（与【确认输入】同一校验入口），结果供选点辅助确定中心。 */
async function validateTextNow() {
  if (state.inputMode !== "text") { state.pendingText = null; updatePeakTools(); return; }
  const text = $("coord").value.trim();
  if (!text) { state.pendingText = null; updatePeakTools(); return; }
  try {
    const v = await api("/api/validate", { text, format: $("fmt").value });
    state.pendingText = v.ok ? { lon: v.lon, lat: v.lat } : null;
  } catch (err) {
    state.pendingText = null;
  }
  updatePeakTools();
}

let textValidateTimer = null;
function scheduleTextValidate() {
  clearTimeout(textValidateTimer);
  textValidateTimer = setTimeout(validateTextNow, 400);
}

async function runPeakSearch(tool) {
  const center = tool === "pick" ? selectionCenter()
    : (state.observer ? { lon: state.observer.lon, lat: state.observer.lat } : null);
  if (!center) return;
  const btn = $(`peak-search-${tool}`);
  btn.disabled = true;
  setBusy(true);
  resetPeakPanels(); // 再次搜索覆盖旧结果（仅保留最新一组，含另一工具的面板与图形）
  showPeakHint(tool, "", "");
  try {
    const radiusM = parseFloat($(`peak-radius-${tool}`).value);
    const res = await api("/api/peak-search", {
      lon: center.lon, lat: center.lat, radius_m: radiusM, dem_source: demSourceConfig(),
    });
    state.peak[tool] = { ...res, center, radius_m: radiusM };
    renderPeakResult(tool, state.peak[tool]);
    drawPeakOnMap(state.peak[tool]);
    log("制高点搜索[%s]: 中心(%.5f, %.5f) 半径=%.1fkm → 候选(%.5f, %.5f) 高程=%.1fm 距离=%.0fm 覆盖率=%.2f 中心即最高=%s",
        tool, center.lon, center.lat, radiusM / 1000, res.lon, res.lat,
        res.elevation_m, res.distance_m, res.coverage_ratio, res.is_center_highest);
  } catch (err) {
    warn(`制高点搜索[${tool}]失败:`, err.message);
    showPeakHint(tool, err.message, "err");
  } finally {
    setBusy(false);
    updatePeakTools();
  }
}

function renderPeakResult(tool, entry) {
  $(`peak-result-${tool}`).classList.remove("hidden");
  $(`peak-coord-${tool}`).textContent = `${entry.lon.toFixed(6)}, ${entry.lat.toFixed(6)}`;
  $(`peak-elev-${tool}`).textContent = fmtHeight(entry.elevation_m);
  $(`peak-dist-${tool}`).textContent = `${(entry.distance_m / 1000).toFixed(2)} km`;
  const note = $(`peak-note-${tool}`);
  const adopt = $(`peak-adopt-${tool}`);
  if (entry.is_center_highest) {
    note.textContent = "当前点已是附近制高点";
    note.className = "feedback ok";
    adopt.classList.add("hidden");
    return;
  }
  adopt.classList.remove("hidden");
  if (entry.coverage_ratio < 0.5) {
    note.textContent = `搜索范围内 DEM 覆盖不完全（约 ${Math.round(entry.coverage_ratio * 100)}%），` +
      "结果基于有效覆盖部分";
    note.className = "feedback warn";
  } else {
    note.textContent = "";
    note.className = "feedback";
  }
}

function drawPeakOnMap(entry) {
  // 虚线搜索范围圆 + 候选 marker；不自动平移地图（候选超出显示范围时侧栏结果仍完整）
  L.circle([entry.center.lat, entry.center.lon], {
    radius: entry.radius_m, color: "#e67e22", weight: 1, dashArray: "4 4", fill: false,
  }).addTo(state.peakGroup);
  L.circleMarker([entry.lat, entry.lon], {
    radius: 6, color: "#e67e22", weight: 2, fillOpacity: 0.9,
  }).bindTooltip(`制高点候选 ${fmtCoord(entry.lon, entry.lat)}`).addTo(state.peakGroup);
}

function refreshPeakElevations() {
  for (const tool of PEAK_TOOLS) {
    const entry = state.peak[tool];
    if (entry) $(`peak-elev-${tool}`).textContent = fmtHeight(entry.elevation_m);
  }
}

/** 清除两工具结果与地图图形（更换观测点 / 采用后由确认链路触发）。 */
function resetPeakPanels() {
  state.peak.pick = null;
  state.peak.tune = null;
  if (state.peakGroup) state.peakGroup.clearLayers();
  for (const tool of PEAK_TOOLS) {
    $(`peak-result-${tool}`).classList.add("hidden");
    $(`peak-note-${tool}`).textContent = "";
  }
  updatePeakTools();
}

/** 采用候选为观测点：以候选坐标走既有【确认输入】生效链路（不旁路直调）。 */
async function adoptPeak(tool) {
  const entry = state.peak[tool];
  if (!entry || entry.is_center_highest) return;
  const btn = $(`peak-adopt-${tool}`);
  btn.disabled = true;
  try {
    if (state.inputMode === "map") setInputMode("text"); // Esc 语义退出点选模式
    $("fmt").value = "decimal";
    $("coord").value = `${entry.lon.toFixed(6)}, ${entry.lat.toFixed(6)}`;
    state.pendingText = { lon: entry.lon, lat: entry.lat };
    await onConfirm();
  } finally {
    btn.disabled = false;
    updatePeakTools();
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
  $("coord").addEventListener("input", scheduleTextValidate);
  $("fmt").addEventListener("change", scheduleTextValidate);
  for (const tool of PEAK_TOOLS) {
    $(`peak-search-${tool}`).addEventListener("click", () => runPeakSearch(tool));
    $(`peak-adopt-${tool}`).addEventListener("click", () => adoptPeak(tool));
  }
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
    refreshPeakElevations();
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
  const bulge = zDisp[20] - zDisp[0];
  check("曲率修正中点凸起≈D²/8R", Math.abs(bulge - 20000 * 20000 / (8 * R_EARTH_M)) < 2.0);
  state.lastLiftDisplay = { profile: synth, feasible: true, lift_m: 31.4 };
  $("profile-box").classList.remove("hidden");
  renderProfileThumbnail(state.lastLiftDisplay);
  check("剖面SVG已渲染", $("profile-chart").querySelector("svg") !== null);
  check("图注含曲率修正说明", $("profile-chart").textContent.includes("曲率修正")
    && $("profile-chart").textContent.includes("视线"));
  const blueLine = $("profile-chart").querySelector('line[stroke="#2469ce"]');
  const blueDots = $("profile-chart").querySelectorAll('circle[fill="#2469ce"]');
  check("蓝线为直线段且端点锚定剖面两端", blueLine !== null && blueDots.length === 2
    && Math.abs(+blueLine.getAttribute("x1") - +blueDots[0].getAttribute("cx")) < 0.5
    && Math.abs(+blueLine.getAttribute("y1") - +blueDots[0].getAttribute("cy")) < 0.5
    && Math.abs(+blueLine.getAttribute("x2") - +blueDots[1].getAttribute("cx")) < 0.5
    && Math.abs(+blueLine.getAttribute("y2") - +blueDots[1].getAttribute("cy")) < 0.5);

  // 最小抬升时蓝线应与剖面相切：平坦地形+中央脊，抬升取解析解（excess=0 于脊顶）
  const ridgeElev = 110, dropMid = (10000 * 10000) / (2 * R_EARTH_M);
  const liftTang = 2 * (ridgeElev - 100 + dropMid);
  renderProfileThumbnail({
    profile: { dist_m: synth.dist_m,
               elev_m: synth.elev_m.map((v, i) => (i === 20 ? ridgeElev : v)) },
    feasible: true, lift_m: liftTang,
  });
  const svg2 = $("profile-chart").querySelector("svg");
  const line2 = svg2.querySelector('line[stroke="#2469ce"]');
  const ridgePt = svg2.querySelector('polyline[stroke="#a0632a"]')
    .getAttribute("points").split(" ")[20].split(",").map(Number);
  const { x1, y1, x2, y2 } = {
    x1: +line2.getAttribute("x1"), y1: +line2.getAttribute("y1"),
    x2: +line2.getAttribute("x2"), y2: +line2.getAttribute("y2"),
  };
  const sightY = y1 + (y2 - y1) * ((ridgePt[0] - x1) / (x2 - x1));
  check("最小抬升时蓝线与剖面相切", Math.abs(sightY - ridgePt[1]) < 0.6);
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

  // —— 附近制高点搜索（选点辅助 / 观测点微调辅助） ——
  state.observer = null;
  state.pendingText = null;
  updatePeakTools();
  const pickSel = $("peak-radius-pick"), tuneSel = $("peak-radius-tune");
  check("制高点档位=6档默认2km(选点辅助)",
        pickSel.options.length === 6 && pickSel.value === "2000");
  check("制高点档位=6档默认2km(微调辅助)",
        tuneSel.options.length === 6 && tuneSel.value === "2000");
  tuneSel.value = "5000";
  check("两工具下拉相互独立", pickSel.value === "2000");
  tuneSel.value = "2000";
  check("无待确认坐标时工具一禁用", $("peak-search-pick").disabled === true);
  check("未设定观测点时工具二禁用", $("peak-search-tune").disabled === true);

  $("coord").value = "116.397, 39.909";
  await validateTextNow();
  check("文本即时校验通过后工具一启用", $("peak-search-pick").disabled === false);
  state.observer = { lon: 116.397, lat: 39.909, elevation: 50, radius: 25240 };
  updatePeakTools();
  check("设定观测点后工具二启用", $("peak-search-tune").disabled === false);

  // 结果渲染分支（直接驱动渲染函数）
  state.peak.pick = { lon: 116.4, lat: 39.95, elevation_m: 100, distance_m: 0,
                      coverage_ratio: 1, is_center_highest: true,
                      center: { lon: 116.4, lat: 39.95 }, radius_m: 2000 };
  renderPeakResult("pick", state.peak.pick);
  check("中心即最高时提示并隐藏采用按钮",
        $("peak-note-pick").textContent.includes("已是附近制高点")
        && $("peak-adopt-pick").classList.contains("hidden"));
  state.peak.pick = { lon: 116.45, lat: 39.98, elevation_m: 100, distance_m: 900,
                      coverage_ratio: 0.3, is_center_highest: false,
                      center: { lon: 116.4, lat: 39.95 }, radius_m: 2000 };
  renderPeakResult("pick", state.peak.pick);
  check("覆盖率偏低时附提示且采用按钮可见",
        $("peak-note-pick").textContent.includes("覆盖")
        && !$("peak-adopt-pick").classList.contains("hidden"));
  state.peak.pick = null;
  $("peak-result-pick").classList.add("hidden");

  // 搜索与采用链路（网络桩化）
  const origFetch = window.fetch;
  const peakCalls = { last: null };
  const jsonResp = (obj) => new Response(JSON.stringify(obj),
    { status: 200, headers: { "Content-Type": "application/json" } });
  window.fetch = async (path, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    if (path === "/api/validate") {
      const [lonV, latV] = body.text.split(",").map((s) => parseFloat(s.trim()));
      return jsonResp({ ok: true, lon: lonV, lat: latV, message: "校验成功" });
    }
    if (path === "/api/horizon") {
      return jsonResp({ elevation_m: 43.5, horizon_radius_m: 23590, display_radius_m: 23590,
                        min_display_radius_m: 5000, fallback: false,
                        refraction: "geometric", dem_source: "stub" });
    }
    if (path === "/api/viewshed") {
      return jsonResp({ image: "data:image/png;base64,iVBORw0KGgo=",
                        bounds: [115.9, 39.6, 116.9, 40.2], cell_size_m: 90,
                        grid_shape: [64, 64], radius_m: 23590, elevation_m: 43.5,
                        engine: "angular-ray-sweep", refraction: "geometric",
                        visible_cells: 123, elapsed_s: 0.05, dem_source: "stub" });
    }
    if (path === "/api/peak-search") {
      peakCalls.last = body;
      return jsonResp({ lon: 116.5, lat: 40.0, elevation_m: 800.0, distance_m: 12900.0,
                        coverage_ratio: 1.0, is_center_highest: false, dem_source: "stub" });
    }
    return jsonResp({});
  };
  try {
    await runPeakSearch("pick");
    check("工具一请求中心=待确认坐标且半径随下拉",
          peakCalls.last && peakCalls.last.lon === 116.397 && peakCalls.last.radius_m === 2000);
    check("工具一结果渲染", !$("peak-result-pick").classList.contains("hidden")
      && $("peak-coord-pick").textContent === "116.500000, 40.000000"
      && $("peak-dist-pick").textContent === "12.90 km");
    check("工具一范围圆与候选marker已渲染", state.peakGroup.getLayers().length === 2);
    await runPeakSearch("tune");
    check("工具二请求中心=已确认观测点", peakCalls.last && peakCalls.last.lon === 116.397);
    check("再次搜索覆盖旧结果(仅保留最新一组)",
          $("peak-result-pick").classList.contains("hidden") && state.peak.pick === null
          && !$("peak-result-tune").classList.contains("hidden"));
    $("unit").value = "ft";
    refreshPeakElevations();
    check("候选高程随米/英尺联动", $("peak-elev-tune").textContent.includes("ft"));
    $("unit").value = "m";
    refreshPeakElevations();
    await adoptPeak("tune");
    check("采用后文本框=候选坐标(十进制)", $("coord").value === "116.500000, 40.000000");
    check("采用后观测点=候选坐标且阶段=可测高",
          state.observer && state.observer.lon === 116.5 && state.observer.lat === 40.0
          && $("phase-badge").textContent === "可测高");
    check("采用后候选marker与两工具结果清除",
          state.peak.pick === null && state.peak.tune === null
          && state.peakGroup.getLayers().length === 0
          && $("peak-result-tune").classList.contains("hidden"));
  } finally {
    window.fetch = origFetch;
  }

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
    updatePeakTools();
    if (new URLSearchParams(location.search).has("selftest")) await runSelfTest();
  } catch (err) {
    showFeedback(`初始化失败：${err.message}`, false);
  }
});
