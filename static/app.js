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
  busyCount: 0,
};

const $ = (id) => document.getElementById(id);
const FT_PER_M = 3.280839895;
const MIN_DISPLAY_M = 5000;

function setBusy(on) {
  state.busyCount = Math.max(0, state.busyCount + (on ? 1 : -1));
  $("busy").classList.toggle("hidden", state.busyCount === 0);
}

async function api(path, body) {
  const resp = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `请求失败（HTTP ${resp.status}）`);
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

function setInputMode(mode) {
  if (state.inputMode === mode) return;
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
    showFeedback("", true);
  } else {
    // 进入点选模式：临时解除平移/缩放约束
    state.map.setMaxBounds(null);
  }
}

function onSegBtnClick(e) {
  const btn = e.currentTarget;
  if (btn.classList.contains("active")) return;
  setInputMode(btn.dataset.mode);
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
    showFeedback("校验成功，正在计算可视域…", true);
    await loadObserver(v.lon, v.lat);
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
  } catch (err) {
    state.observer = null;
    showFeedback(err.message, false);
  } finally {
    setBusy(false);
  }
}

/* ---------------- 点选测高 ---------------- */

function resetLiftPanel() {
  $("lift-result").classList.add("hidden");
  $("lift-hint").classList.remove("hidden");
  if (state.clickMarker) { state.map.removeLayer(state.clickMarker); state.clickMarker = null; }
}

async function onMapClick(e) {
  if (state.inputMode === "map") {
    // 地图点选模式：点击即采集观测点坐标（覆盖旧值），点选测高暂不可用
    const lon = normalizeLon(e.latlng.lng);
    const lat = Math.max(-90, Math.min(90, e.latlng.lat));
    state.picked = { lon, lat };
    $("picked-coord").value = `${lon.toFixed(6)}, ${lat.toFixed(6)}`;
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
  } catch (err) {
    $("lift-hint").classList.remove("hidden");
    $("lift-hint").textContent = err.message;
    $("lift-hint").classList.add("err");
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

window.addEventListener("DOMContentLoaded", async () => {
  // CDN 失败时，index.html 的 onerror 会补挂本地 Leaflet；此处等待其就绪
  await waitForLeaflet();
  initMap();
  try {
    await loadOptions();
    bindEvents();
  } catch (err) {
    showFeedback(`初始化失败：${err.message}`, false);
  }
});
