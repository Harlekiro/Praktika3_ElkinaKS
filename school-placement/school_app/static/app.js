"use strict";

/* Оценка вариантов размещения школ — клиентская часть.
 * Все расчёты выполняет сервер (BlocksNet). Здесь только отображение и управление.
 */

const STORAGE_PREFIX = "school-placement.saved.v2:";
const storageKey = () => STORAGE_PREFIX + (S.meta ? S.meta.files.data_dir : "");
const SVG_NS = "http://www.w3.org/2000/svg";

const S = {
  meta: null,
  baseRows: [],          // исходные показатели по кварталам
  byId: new Map(),       // геометрия и исходные показатели
  vbFull: null,
  vb: null,
  mode: "base",
  selected: null,
  draft: [],             // школы, которые пользователь задаёт в редакторе
  view: null,            // показанный сценарий: {label, kind, placements, result}
  sort: { key: "id", dir: 1 },
  auto: { jobId: null, timer: null, result: null, waiters: [], selectedBlock: null },
  variants: { A: null, B: null, C: null },
  bManual: false,       // вместимость школ варианта B изменена вручную
  saved: [],
};

const $ = (id) => document.getElementById(id);
const nf0 = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 1, maximumFractionDigits: 1 });

const fmtInt = (v) => (v === null || v === undefined || Number.isNaN(v) ? "—" : nf0.format(v));
const fmtPct = (p) => (p === null || p === undefined ? "—" : nf1.format(p * 100));
const fmtPP = (v) => (v === null || v === undefined ? "—" : (v > 0.05 ? "+" : "") + nf1.format(v));
const fmtSigned = (v) => (v === null || v === undefined ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "") + nf0.format(Math.abs(v)));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
/** Согласование: 1 место, 2 места, 5 мест. */
const placesWord = (n) => {
  const a = Math.abs(n) % 100, b = a % 10;
  if (b === 1 && a !== 11) return "место";
  if (b >= 2 && b <= 4 && (a < 12 || a > 14)) return "места";
  return "мест";
};
const fmtPlaces = (n) => `${fmtInt(n)} ${placesWord(n)}`;
const placementsText = (pl) => (pl.length ? pl.map((p) => `№${p.block_id}: ${fmtPlaces(p.capacity)}`).join("; ") : "без новых школ");
const placementsKey = (pl) => JSON.stringify(pl.map((p) => [p.block_id, p.capacity]));

function toast(text, isError = false) {
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " error" : "");
  el.textContent = text;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), isError ? 7000 : 3500);
}

function show(el, text, cls) {
  el.textContent = text;
  el.className = el.className.replace(/\b(ok|warn)\b/g, "").trim();
  if (cls) el.classList.add(cls);
  el.classList.toggle("hidden", !text);
}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    throw new Error("Сервер недоступен. Проверьте, что приложение запущено (run.py).");
  }
  let payload = null;
  try { payload = await response.json(); } catch (error) { /* пусто */ }
  if (!response.ok) throw new Error((payload && payload.error) || `Ошибка сервера (${response.status}).`);
  return payload;
}

/** Положительное целое число мест или текст ошибки. */
function parseCapacity(text) {
  const value = String(text).trim();
  const max = S.meta ? S.meta.config.max_capacity : 100000;
  if (!/^\d+$/.test(value) || Number(value) <= 0) return { error: "Вместимость школы — положительное целое число мест." };
  if (Number(value) > max) return { error: `Вместимость школы — не больше ${fmtInt(max)} мест.` };
  return { value: Number(value) };
}

// ---------------------------------------------------------------------------
// Загрузка
// ---------------------------------------------------------------------------

async function init() {
  try {
    const [meta, blocks] = await Promise.all([api("/api/meta"), api("/api/blocks")]);
    S.meta = meta;
    S.baseRows = blocks.blocks.map(({ path, centroid, ...row }) => row);
    blocks.blocks.forEach((b) => S.byId.set(b.id, b));
    S.vbFull = blocks.view_box.slice();
    S.vb = blocks.view_box.slice();
  } catch (error) {
    $("loading").classList.add("hidden");
    show($("load-error"), "Не удалось загрузить данные приложения.\n" + error.message);
    return;
  }
  $("loading").classList.add("hidden");
  S.saved = loadSaved();
  renderMeta();
  fillSelects();
  buildMap();
  bindEvents();
  renderAll();
  renderVariantHint();
  renderVariants();
  renderSaved();
}

function renderMeta() {
  const m = S.meta;
  const v = m.validation;
  $("territory").textContent = m.territory || "";
  $("dataset-name").textContent = m.dataset_name;
  $("m-acc").textContent = m.config.accessibility_min;
  $("m-dem").textContent = m.config.demand_per_1000;
  const text =
    `Набор данных: ${m.dataset_name}. Исходный расчёт воспроизведён: ${v.method}. Сверено ${v.compared_blocks} из ` +
    `${v.populated_blocks} населённых кварталов (максимальное расхождение обеспеченности ${v.max_abs_diff.toExponential(1)}), ` +
    `итоговая обеспеченность ${v.computed_total.toFixed(3)} (сохранённый результат — ${v.reported_total}), ` +
    `связи «квартал → школа» ${v.links_equal ? "совпадают" : "не сверены"} (${v.links_count}). ` +
    `Матрица ${v.matrix_shape.join(" × ")}, идентификаторы строк и столбцов совпадают с кварталами.` +
    (v.warnings.length ? "\nПредупреждения: " + v.warnings.join("; ") : "");
  const banner = $("validation");
  banner.textContent = text;
  banner.className = "alert " + (v.status === "ok" ? "alert-ok" : "alert-warn");
  $("method-check").innerHTML = `<div class="alert ${v.status === "ok" ? "alert-ok" : "alert-warn"}">${esc(text)}</div>`;
  $("sources").innerHTML = m.sources.map((s) => `<li>${esc(s)}</li>`).join("");
  $("assumptions").innerHTML = m.assumptions.map((s) => `<li>${esc(s)}</li>`).join("");
  const f = m.files;
  $("files").innerHTML =
    `<p>Данные приложения: ${esc(f.data_dir)} (подготовлены ${esc(m.data_prepared_at)})</p>` +
    (m.dataset_log.length ? `<p>Журнал сборки набора:<br>${m.dataset_log.map(esc).join("<br>")}</p>` : "") +
    Object.entries(f.source_files).map(([k, p]) => `<p>${esc(k)}: ${esc(p)}<br>sha256 ${esc(f.source_hashes[k] || "—")}</p>`).join("");
  $("blocks-note").textContent =
    `Потребность — ceil(население / 1000 × ${m.config.demand_per_1000}) мест. ` +
    `Обеспеченность — доля потребности, закрытая местами в пределах ${m.config.accessibility_min} минут пешком.`;
}

function blockOptionLabel(row) {
  const parts = [`№${row.id}`];
  if (row.population > 0) parts.push(`${fmtInt(row.population)} чел.`);
  if (row.base_capacity > 0) parts.push(`школа ${fmtPlaces(row.base_capacity)}`);
  parts.push(row.land_use_label);
  return parts.join(" · ");
}

function fillSelects() {
  const options = S.baseRows.map((r) => `<option value="${r.id}">${esc(blockOptionLabel(r))}</option>`).join("");
  ["draft-block", "va-block", "vb-block1", "vb-block2"].forEach((id) => { $(id).innerHTML = options; });
  // Значения по умолчанию — кварталы с наибольшим исходным дефицитом (пользователь может изменить).
  const byDeficit = S.baseRows.slice().sort((a, b) => b.base_deficit_places - a.base_deficit_places || a.id - b.id);
  $("va-block").value = byDeficit[0].id;
  $("vb-block1").value = byDeficit[0].id;
  $("vb-block2").value = byDeficit[1].id;
  $("draft-block").value = byDeficit[0].id;
}

// ---------------------------------------------------------------------------
// Текущее отображение
// ---------------------------------------------------------------------------

function currentRows() {
  return S.view ? S.view.result.blocks : S.baseRows;
}

function rowById(id) {
  return currentRows().find((r) => r.id === id);
}

function setView(view) {
  S.view = view;
  if (view && S.mode === "base") setMode("scenario", false);
  renderAll();
}

function renderAll() {
  renderSummary();
  renderViewLabel();
  renderDraft();
  updateMapColors();
  renderMarkers();
  renderLegend();
  renderCard();
  renderBlocksTable();
}

function renderViewLabel() {
  const el = $("view-label");
  if (!S.view) {
    el.innerHTML = "Показано <b>исходное состояние</b>. Задайте школы в редакторе сценария и нажмите «Рассчитать сценарий».";
    return;
  }
  el.innerHTML = `Показан сценарий: <b>${esc(S.view.label)}</b> — ${esc(placementsText(S.view.placements))}. ` +
    "Сравнение — относительно исходного состояния.";
}

function stat(label, value, unit, sub) {
  return `<div class="stat"><div class="stat-label">${label}</div>` +
    `<div class="stat-value">${value} <span class="stat-unit">${unit}</span></div>` +
    `<div class="stat-sub">${sub || "&nbsp;"}</div></div>`;
}

function renderSummary() {
  const b = S.meta.base;
  const r = S.view ? S.view.result : null;
  const s = r ? r.scenario : b;
  const deltaCls = (v, goodPositive = true) => (v === 0 ? "" : (v > 0) === goodPositive ? "delta-good" : "delta-bad");
  const html = [
    stat("Население", fmtInt(b.population), "чел.", "численность не меняется в сценариях"),
    stat("Расчётная потребность", fmtInt(b.demand_places), "мест", `${S.meta.config.demand_per_1000} мест на 1000 жителей`),
    stat("Вместимость сети школ", fmtInt(s.capacity_places), "мест",
      r ? `исходно ${fmtInt(b.capacity_places)}; прирост <span class="${deltaCls(r.added_capacity)}">${fmtSigned(r.added_capacity)} мест</span>` : "существующие школы"),
    stat("Удовлетворённый спрос", fmtInt(s.served_places), "мест", r ? `исходно ${fmtInt(b.served_places)}` : `в пределах ${S.meta.config.accessibility_min} мин пешком`),
    stat("Остаточный дефицит мест", fmtInt(s.deficit_places), "мест",
      r ? `исходно ${fmtInt(b.deficit_places)}; <span class="${deltaCls(r.deficit_reduction_places)}">сокращение ${fmtSigned(r.deficit_reduction_places)}</span>` : "исходное состояние"),
    stat("Обеспеченность", fmtPct(s.provision), "%",
      r ? `исходно ${fmtPct(b.provision)}%; <span class="${deltaCls(r.provision_change_pp)}">${fmtPP(r.provision_change_pp)} п.п.</span>` : "доля потребности"),
  ];
  $("summary").innerHTML = html.join("");
}

// ---------------------------------------------------------------------------
// Карта
// ---------------------------------------------------------------------------

const PROVISION_CLASSES = [
  { test: (p) => p < 0.25, color: "#d7301f", label: "0–25 %" },
  { test: (p) => p < 0.5, color: "#fc8d59", label: "25–50 %" },
  { test: (p) => p < 0.75, color: "#fdcc8a", label: "50–75 %" },
  { test: (p) => p < 0.9999, color: "#a6d96a", label: "75–99 %" },
  { test: () => true, color: "#1a9641", label: "100 %" },
];
const NO_POP = "#e4e7ea";
const CHANGE_CLASSES = [
  { test: (d) => d < 0, color: "#d7301f", label: "дефицит вырос" },
  { test: (d) => d === 0, color: "#f4f4f2", label: "без изменений" },
  { test: (d) => d <= 2, color: "#c7e9c0", label: "сокращение 1–2 места" },
  { test: (d) => d <= 10, color: "#74c476", label: "3–10 мест" },
  { test: () => true, color: "#238b45", label: "более 10 мест" },
];

function blockColor(row) {
  if (S.mode === "change") {
    if (!S.view || row.demand_places === 0) return NO_POP;
    return CHANGE_CLASSES.find((c) => c.test(row.deficit_reduction_places)).color;
  }
  const p = S.mode === "scenario" ? row.scenario_provision : row.base_provision;
  if (p === null) return NO_POP;
  return PROVISION_CLASSES.find((c) => c.test(p)).color;
}

function buildMap() {
  const svg = $("map");
  svg.innerHTML = "";
  const gBlocks = document.createElementNS(SVG_NS, "g");
  gBlocks.id = "g-blocks";
  for (const block of S.byId.values()) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", block.path);
    path.setAttribute("class", "block");
    path.dataset.id = block.id;
    gBlocks.appendChild(path);
  }
  const gMarkers = document.createElementNS(SVG_NS, "g");
  gMarkers.id = "g-markers";
  svg.append(gBlocks, gMarkers);
  S.vb = populatedExtent() || S.vbFull.slice();
  applyViewBox();
}

/** Охват кварталов с населением или школами — начальный масштаб карты. */
function populatedExtent() {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const b of S.byId.values()) {
    if (!(b.population > 0 || b.base_capacity > 0)) continue;
    const nums = b.path.match(/-?\d+(\.\d+)?/g).map(Number);
    for (let i = 0; i + 1 < nums.length; i += 2) {
      x0 = Math.min(x0, nums[i]); x1 = Math.max(x1, nums[i]);
      y0 = Math.min(y0, nums[i + 1]); y1 = Math.max(y1, nums[i + 1]);
    }
  }
  if (!Number.isFinite(x0)) return null;
  const pad = Math.max(x1 - x0, y1 - y0) * 0.15;
  return [x0 - pad, y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad];
}

function applyViewBox() {
  $("map").setAttribute("viewBox", S.vb.join(" "));
  renderMarkers();
}

function updateMapColors() {
  const rows = new Map(currentRows().map((r) => [r.id, r]));
  document.querySelectorAll("#g-blocks path").forEach((path) => {
    const id = Number(path.dataset.id);
    path.setAttribute("fill", blockColor(rows.get(id)));
    path.classList.toggle("selected", id === S.selected);
  });
  const hints = {
    base: "Обеспеченность населения местами в школах в исходном состоянии.",
    scenario: S.view ? "Обеспеченность в показанном сценарии." : "Сценарий не рассчитан — показано исходное состояние.",
    change: S.view ? "Сокращение дефицита мест по кварталам относительно исходного состояния." : "Сценарий не рассчитан — изменений нет.",
  };
  $("mode-hint").textContent = hints[S.mode];
}

function renderMarkers() {
  const g = $("g-markers");
  if (!g) return;
  g.innerHTML = "";
  const r = S.vb[2] / 90;
  const showNew = S.view && S.mode !== "base";
  const added = new Map();
  if (showNew) S.view.placements.forEach((p) => added.set(p.block_id, (added.get(p.block_id) || 0) + p.capacity));
  for (const block of S.byId.values()) {
    const [x, y] = block.centroid;
    const existing = block.base_capacity > 0;
    const isNew = added.has(block.id);
    if (!existing && !isNew) continue;
    const shift = existing && isNew ? r * 1.1 : 0;
    if (existing) drawMarker(g, x - shift, y, r, "marker-existing", "Ш", `${fmtInt(block.base_capacity)}`);
    if (isNew) drawMarker(g, x + shift, y, r, "marker-new", "+", `+${fmtInt(added.get(block.id))}`);
  }
  if (S.selected !== null) {
    const b = S.byId.get(S.selected);
    if (b) {
      const label = document.createElementNS(SVG_NS, "text");
      label.setAttribute("x", b.centroid[0]);
      label.setAttribute("y", b.centroid[1] - r * 1.6);
      label.setAttribute("class", "marker-label");
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("font-size", r * 1.2);
      label.textContent = `№${b.id}`;
      g.appendChild(label);
    }
  }
}

function drawMarker(g, x, y, r, cls, symbol, caption) {
  const c = document.createElementNS(SVG_NS, "circle");
  c.setAttribute("cx", x); c.setAttribute("cy", y); c.setAttribute("r", r); c.setAttribute("class", cls);
  const t = document.createElementNS(SVG_NS, "text");
  t.setAttribute("x", x); t.setAttribute("y", y); t.setAttribute("class", "marker-text"); t.setAttribute("font-size", r * 1.1);
  t.textContent = symbol;
  const cap = document.createElementNS(SVG_NS, "text");
  cap.setAttribute("x", x + r * 1.25); cap.setAttribute("y", y + r * 0.4); cap.setAttribute("class", "marker-label");
  cap.setAttribute("font-size", r * 1.05);
  cap.textContent = caption;
  g.append(c, t, cap);
}

function renderLegend() {
  const items = [];
  if (S.mode === "change") {
    items.push(`<span class="legend-title">Сокращение дефицита мест в квартале, мест</span>`);
    CHANGE_CLASSES.forEach((c) => items.push(`<span class="legend-item"><span class="swatch" style="background:${c.color}"></span>${c.label}</span>`));
  } else {
    items.push(`<span class="legend-title">Обеспеченность местами в школах, % потребности (${S.mode === "base" ? "исходное состояние" : "сценарий"})</span>`);
    PROVISION_CLASSES.forEach((c) => items.push(`<span class="legend-item"><span class="swatch" style="background:${c.color}"></span>${c.label}</span>`));
  }
  items.push(`<span class="legend-item"><span class="swatch" style="background:${NO_POP}"></span>нет населения</span>`);
  items.push(`<span class="legend-item"><span class="dot" style="background:#1d4ed8"></span>существующая школа (число мест)</span>`);
  items.push(`<span class="legend-item"><span class="dot" style="background:#e8590c"></span>новая школа сценария (+ мест)</span>`);
  $("legend").innerHTML = items.join("");
}

function setMode(mode, render = true) {
  S.mode = mode;
  document.querySelectorAll("#map-modes button").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  if (render) { updateMapColors(); renderMarkers(); renderLegend(); }
}

function selectBlock(id, scroll = false) {
  S.selected = id;
  $("draft-block").value = id;
  updateMapColors();
  renderMarkers();
  renderCard();
  document.querySelectorAll("#blocks-table tbody tr").forEach((tr) => tr.classList.toggle("selected", Number(tr.dataset.id) === id));
  if (scroll) $("map-wrap").scrollIntoView({ behavior: "smooth", block: "center" });
}

function tooltipHtml(row) {
  const lines = [`<b>Квартал №${row.id}</b> · ${esc(row.land_use_label)}`, `Население: ${fmtInt(row.population)} чел.`];
  if (row.demand_places > 0) {
    lines.push(`Потребность: ${fmtPlaces(row.demand_places)}`);
    lines.push(`Обеспеченность: ${fmtPct(row.base_provision)}%` + (S.view ? ` → ${fmtPct(row.scenario_provision)}%` : ""));
    lines.push(`Дефицит: ${fmtInt(row.base_deficit_places)}` + (S.view ? ` → ${fmtInt(row.scenario_deficit_places)}` : "") + " мест");
  }
  const cap = S.view ? row.scenario_capacity : row.base_capacity;
  if (cap > 0) lines.push(`Вместимость школ: ${fmtPlaces(cap)}`);
  return lines.join("<br>");
}

function bindMap() {
  const svg = $("map");
  const wrap = $("map-wrap");
  const tip = $("tooltip");
  let drag = null;

  const scale = () => {
    const rect = svg.getBoundingClientRect();
    return Math.max(S.vb[2] / rect.width, S.vb[3] / rect.height);
  };
  const toSvg = (clientX, clientY) => {
    const pt = svg.createSVGPoint();
    pt.x = clientX; pt.y = clientY;
    return pt.matrixTransform(svg.getScreenCTM().inverse());
  };
  const zoom = (factor, cx, cy) => {
    const [x, y, w, h] = S.vb;
    const nw = Math.min(Math.max(w * factor, S.vbFull[2] / 40), S.vbFull[2] * 1.5);
    const k = nw / w;
    S.vb = [cx - (cx - x) * k, cy - (cy - y) * k, w * k, h * k];
    applyViewBox();
  };
  const center = () => [S.vb[0] + S.vb[2] / 2, S.vb[1] + S.vb[3] / 2];

  svg.addEventListener("wheel", (e) => {
    e.preventDefault();
    const p = toSvg(e.clientX, e.clientY);
    zoom(e.deltaY < 0 ? 1 / 1.25 : 1.25, p.x, p.y);
  }, { passive: false });
  svg.addEventListener("pointerdown", (e) => {
    drag = { x: e.clientX, y: e.clientY, vb: S.vb.slice(), moved: false, target: e.target };
  });
  window.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 4) return;
    if (!drag.moved) { drag.moved = true; svg.classList.add("dragging"); tip.classList.add("hidden"); }
    const k = scale();
    S.vb = [drag.vb[0] - dx * k, drag.vb[1] - dy * k, drag.vb[2], drag.vb[3]];
    svg.setAttribute("viewBox", S.vb.join(" "));
  });
  window.addEventListener("pointerup", () => {
    if (!drag) return;
    svg.classList.remove("dragging");
    if (drag.moved) renderMarkers();
    else if (drag.target.dataset && drag.target.dataset.id) selectBlock(Number(drag.target.dataset.id));
    drag = null;
  });
  svg.addEventListener("mousemove", (e) => {
    if (drag && drag.moved) return;
    const id = e.target.dataset ? Number(e.target.dataset.id) : NaN;
    if (Number.isNaN(id)) { tip.classList.add("hidden"); return; }
    tip.innerHTML = tooltipHtml(rowById(id));
    const rect = wrap.getBoundingClientRect();
    let left = e.clientX - rect.left + 14, top = e.clientY - rect.top + 14;
    if (left > rect.width - 290) left -= 300;
    tip.style.left = left + "px"; tip.style.top = top + "px";
    tip.classList.remove("hidden");
  });
  svg.addEventListener("mouseleave", () => tip.classList.add("hidden"));
  $("zoom-in").onclick = () => zoom(1 / 1.4, ...center());
  $("zoom-out").onclick = () => zoom(1.4, ...center());
  $("zoom-reset").onclick = () => { S.vb = S.vbFull.slice(); applyViewBox(); };
  document.querySelectorAll("#map-modes button").forEach((b) => { b.onclick = () => setMode(b.dataset.mode); });
}

// ---------------------------------------------------------------------------
// Карточка квартала
// ---------------------------------------------------------------------------

function renderCard() {
  const body = $("card-body");
  if (S.selected === null) { body.className = "muted"; body.textContent = "Выберите квартал на карте или в таблице."; return; }
  const r = rowById(S.selected);
  body.className = "";
  const sc = !!S.view;
  const dd = (base, scen, unit = "") => (sc && scen !== base ? `${base}${unit} → ${scen}${unit}` : `${base}${unit}`);
  body.innerHTML = `
    <div class="card-title">Квартал №${r.id}</div>
    <dl class="card-grid">
      <dt>Землепользование</dt><dd>${esc(r.land_use_label)}</dd>
      <dt>Площадь</dt><dd>${nf1.format(r.area_ha)} га</dd>
      <dt>Численность населения</dt><dd>${fmtInt(r.population)} чел.</dd>
      <dt>Расчётная потребность</dt><dd>${fmtInt(r.demand_places)} мест</dd>
      <dt>Суммарная вместимость школ</dt><dd>${dd(fmtInt(r.base_capacity), fmtInt(r.scenario_capacity), " мест")}</dd>
      <dt>Обеспеченность</dt><dd>${r.demand_places ? dd(fmtPct(r.base_provision), fmtPct(r.scenario_provision), "%") : "нет населения"}</dd>
      <dt>Дефицит мест</dt><dd>${r.demand_places ? dd(fmtInt(r.base_deficit_places), fmtInt(r.scenario_deficit_places), " мест") : "—"}</dd>
    </dl>
    <div class="button-row">
      <button type="button" class="btn btn-small btn-primary" data-act="draft">Добавить школу в сценарий</button>
      <button type="button" class="btn btn-small" data-act="A">Для варианта A</button>
      <button type="button" class="btn btn-small" data-act="B1">B: квартал 1</button>
      <button type="button" class="btn btn-small" data-act="B2">B: квартал 2</button>
    </div>`;
  body.querySelectorAll("button").forEach((btn) => {
    btn.onclick = () => {
      const act = btn.dataset.act;
      if (act === "draft") addDraft(r.id);
      if (act === "A") { $("va-block").value = r.id; toast(`Квартал №${r.id} выбран для варианта A.`); }
      if (act === "B1") { $("vb-block1").value = r.id; toast(`Квартал №${r.id} — первая школа варианта B.`); }
      if (act === "B2") { $("vb-block2").value = r.id; toast(`Квартал №${r.id} — вторая школа варианта B.`); }
    };
  });
}

// ---------------------------------------------------------------------------
// Редактор сценария
// ---------------------------------------------------------------------------

function addDraft(blockId) {
  const input = $("draft-capacity");
  const parsed = parseCapacity(input.value);
  input.classList.toggle("invalid", !!parsed.error);
  if (parsed.error) { show($("draft-error"), parsed.error); input.focus(); return; }
  show($("draft-error"), "");
  const id = blockId ?? Number($("draft-block").value);
  S.draft.push({ block_id: id, capacity: parsed.value });
  renderDraft();
  toast(`Добавлена школа на ${fmtPlaces(parsed.value)} в квартале №${id}. Нажмите «Рассчитать сценарий».`);
}

function draftIsCalculated() {
  return S.view && S.view.kind === "draft" && placementsKey(S.view.placements) === placementsKey(S.draft);
}

function renderDraft() {
  const list = $("draft-list");
  list.innerHTML = S.draft.map((p, i) =>
    `<li><span><span class="num">Квартал №${p.block_id}</span> — ${fmtPlaces(p.capacity)}</span>` +
    `<button type="button" class="btn-link" data-i="${i}">удалить</button></li>`).join("");
  list.querySelectorAll("button").forEach((b) => {
    b.onclick = () => { S.draft.splice(Number(b.dataset.i), 1); renderDraft(); };
  });
  const added = S.draft.reduce((s, p) => s + p.capacity, 0);
  const badge = $("draft-status");
  if (!S.draft.length) { badge.textContent = "нет школ"; badge.className = "badge"; }
  else {
    const calc = draftIsCalculated();
    badge.textContent = `${S.draft.length} шк., +${fmtInt(added)} мест · ${calc ? "рассчитан" : "не рассчитан"}`;
    badge.className = "badge " + (calc ? "badge-ok" : "badge-warn");
  }
}

async function calcDraft() {
  const btn = $("calc");
  btn.disabled = true;
  show($("draft-message"), "Расчёт…");
  try {
    const placements = S.draft.slice();
    const result = await api("/api/scenario", { placements });
    const label = placements.length ? "сценарий пользователя" : "без новых школ";
    setView({ label, kind: "draft", placements: result.placements, result });
    if (!placements.length) {
      const same = result.scenario.deficit_places === S.meta.base.deficit_places && result.scenario.provision === S.meta.base.provision;
      show($("draft-message"), same ? "Без новых школ расчёт совпадает с исходным состоянием." : "Внимание: расчёт без новых школ отличается от исходного.", same ? "ok" : "warn");
    } else {
      show($("draft-message"),
        `Прирост вместимости сети: +${fmtInt(result.added_capacity)} мест. Остаточный дефицит: ${fmtInt(result.scenario.deficit_places)} мест ` +
        `(сокращение ${fmtSigned(result.deficit_reduction_places)}).`, "ok");
    }
  } catch (error) {
    show($("draft-message"), error.message, "warn");
    toast(error.message, true);
  } finally {
    btn.disabled = false;
  }
}

function resetAll() {
  S.draft = [];
  S.view = null;
  S.auto.selectedBlock = null;
  show($("draft-message"), "");
  show($("draft-error"), "");
  $("draft-capacity").value = "600";
  $("draft-capacity").classList.remove("invalid");
  setMode("base", false);
  renderAll();
  renderAutoTable();
  toast("Изменения сброшены: показано исходное состояние.");
}

// ---------------------------------------------------------------------------
// Автоподбор
// ---------------------------------------------------------------------------

async function startAuto(capacityText) {
  const parsed = parseCapacity(capacityText ?? $("auto-capacity").value);
  $("auto-capacity").classList.toggle("invalid", !!parsed.error);
  if (parsed.error) { show($("auto-error"), parsed.error); return false; }
  show($("auto-error"), "");
  try {
    const snap = await api("/api/auto", { capacity: parsed.value });
    S.auto.jobId = snap.id;
    S.auto.result = null;
    S.auto.selectedBlock = null;
    renderAutoTable();
    renderAutoProgress(snap);
    clearInterval(S.auto.timer);
    S.auto.timer = setInterval(pollAuto, 400);
    return true;
  } catch (error) {
    show($("auto-error"), error.message);
    return false;
  }
}

async function pollAuto() {
  const id = S.auto.jobId;
  try {
    const snap = await api(`/api/auto/${id}`);
    if (id !== S.auto.jobId) return;
    renderAutoProgress(snap);
    if (snap.state === "running" || snap.state === "cancelling") return;
    clearInterval(S.auto.timer);
    if (snap.state === "failed") {
      show($("auto-error"), "Перебор прерван ошибкой: " + snap.failure);
      S.auto.waiters.splice(0).forEach((fn) => fn(null));
      return;
    }
    S.auto.result = snap.result;
    renderAutoResult();
    S.auto.waiters.splice(0).forEach((fn) => fn(snap.result));
  } catch (error) {
    clearInterval(S.auto.timer);
    show($("auto-error"), error.message);
    S.auto.waiters.splice(0).forEach((fn) => fn(null));
  }
}

function renderAutoProgress(snap) {
  const running = snap.state === "running" || snap.state === "cancelling";
  $("auto-progress").classList.toggle("hidden", !running);
  $("auto-start").disabled = running;
  $("auto-cancel").disabled = snap.state === "cancelling";
  $("auto-progress-text").textContent =
    `Проверено ${snap.checked} из ${snap.total} кварталов` + (snap.current_block !== null ? ` · квартал №${snap.current_block}` : "") +
    (snap.errors_so_far ? ` · ошибок: ${snap.errors_so_far}` : "");
  $("auto-elapsed").textContent = `${nf1.format(snap.elapsed_s)} с`;
  $("auto-bar").style.width = `${(100 * snap.checked) / snap.total}%`;
  const badge = $("auto-status");
  const states = { running: ["идёт перебор", "badge-run"], cancelling: ["остановка…", "badge-run"], failed: ["ошибка", "badge-bad"], cancelled: ["остановлен", "badge-warn"] };
  if (states[snap.state]) { badge.textContent = states[snap.state][0]; badge.className = "badge " + states[snap.state][1]; }
  if (running) show($("auto-message"), `Перебор для школы на ${fmtPlaces(snap.capacity)}: каждый квартал считается от исходной сети школ.`);
}

function renderAutoResult() {
  const res = S.auto.result;
  const badge = $("auto-status");
  if (res.complete) { badge.textContent = `${res.checked} из ${res.total} проверено`; badge.className = "badge badge-ok"; }
  else { badge.textContent = `неполный: ${res.evaluated} из ${res.total}`; badge.className = "badge badge-warn"; }
  show($("auto-message"), res.message, res.complete ? "ok" : "warn");
  const errBox = $("auto-errors");
  if (res.errors.length) {
    show(errBox, `Ошибки расчёта (${res.errors.length}):\n` + res.errors.map((e) => `квартал №${e.block_id}: ${e.error}`).join("\n"));
  } else show(errBox, "");
  $("auto-export").disabled = !res.candidates.length;
  renderAutoTable();
}

function renderAutoTable() {
  const body = $("auto-table").querySelector("tbody");
  const res = S.auto.result;
  if (!res) {
    body.innerHTML = `<tr><td colspan="10" class="muted">${S.auto.jobId ? "Идёт перебор…" : "Автоподбор ещё не запускался."}</td></tr>`;
    return;
  }
  const rows = res.candidates.map((c) => {
    const status = c.is_best ? (res.complete ? (res.ties > 1 ? "равноценный лучший" : "лучший") : "лучший среди рассчитанных") : "";
    return `<tr data-id="${c.block_id}" class="${c.is_best ? "best" : ""} ${c.block_id === S.auto.selectedBlock ? "selected" : ""}">` +
      `<td>${c.rank}</td><td>№${c.block_id}</td><td class="text">${esc(c.land_use_label)}</td><td>${fmtInt(c.population)}</td>` +
      `<td>${fmtInt(c.base_capacity)}</td><td>${fmtInt(c.block_base_deficit)}</td><td>${fmtInt(c.scenario_deficit)}</td>` +
      `<td>${fmtSigned(c.deficit_reduction)}</td><td>${fmtPct(c.scenario_provision)}</td><td class="text">${status}</td></tr>`;
  });
  res.errors.forEach((e) => rows.push(`<tr class="error-row"><td>—</td><td>№${e.block_id}</td><td class="text" colspan="8">Ошибка расчёта: ${esc(e.error)}</td></tr>`));
  body.innerHTML = rows.join("");
  body.querySelectorAll("tr[data-id]").forEach((tr) => { tr.onclick = () => showCandidate(Number(tr.dataset.id)); });
}

async function showCandidate(blockId) {
  const res = S.auto.result;
  const cand = res.candidates.find((c) => c.block_id === blockId);
  S.auto.selectedBlock = blockId;
  renderAutoTable();
  try {
    const placements = [{ block_id: blockId, capacity: res.capacity }];
    const result = await api("/api/scenario", { placements });
    setView({ label: `автоподбор, квартал №${blockId} (место ${cand.rank})`, kind: "auto", placements, result });
    selectBlock(blockId);
    const same = result.scenario.deficit_places === cand.scenario_deficit && result.deficit_reduction_places === cand.deficit_reduction;
    show($("auto-message"),
      same
        ? `Квартал №${blockId}: ручной расчёт сценария совпадает с результатом перебора — остаточный дефицит ${fmtInt(cand.scenario_deficit)} мест, сокращение ${fmtSigned(cand.deficit_reduction)}.`
        : `Квартал №${blockId}: ручной расчёт (${result.scenario.deficit_places}) не совпал с перебором (${cand.scenario_deficit}).`,
      same ? "ok" : "warn");
  } catch (error) {
    toast(error.message, true);
  }
}

// ---------------------------------------------------------------------------
// Три варианта
// ---------------------------------------------------------------------------

/** Вместимость вариантов: общая (A и C) и две школы варианта B. */
function readVariantCaps() {
  const total = parseCapacity($("vtotal").value);
  if (total.error) return { error: "Вместимость варианта: " + total.error.charAt(0).toLowerCase() + total.error.slice(1) };
  const b1 = parseCapacity($("vb-cap1").value), b2 = parseCapacity($("vb-cap2").value);
  let errorB = null;
  if (b1.error || b2.error) errorB = "Вариант B: вместимость каждой школы — положительное целое число мест.";
  else if (b1.value + b2.value !== total.value) {
    errorB = `Вариант B: сумма вместимости школ (${fmtInt(b1.value + b2.value)}) должна быть равна вместимости варианта (${fmtPlaces(total.value)}).`;
  }
  return { total: total.value, b1: b1.value, b2: b2.value, errorB };
}

function variantTitle(L, placements) {
  const caps = placements.map((p) => fmtInt(p.capacity));
  const last = placesWord(placements[placements.length - 1].capacity);
  if (L === "A") return `A: одна школа ${caps[0]} ${last} (выбор пользователя)`;
  if (L === "B") return `B: две школы ${caps.join(" + ")} ${last}`;
  return `C: одна школа ${caps[0]} ${last} (автоподбор)`;
}

function plannedTitle(L) {
  const c = readVariantCaps();
  if (c.error) return { A: "A: одна школа", B: "B: две школы", C: "C: одна школа (автоподбор)" }[L];
  if (L === "B") return variantTitle("B", [{ capacity: c.b1 }, { capacity: c.b2 }]);
  return variantTitle(L, [{ capacity: c.total }]);
}

function onVariantCapsChange(fromTotal) {
  const total = parseCapacity($("vtotal").value);
  $("vtotal").classList.toggle("invalid", !!total.error);
  if (fromTotal && !total.error && !S.bManual) {
    $("vb-cap1").value = Math.ceil(total.value / 2);
    $("vb-cap2").value = Math.floor(total.value / 2);
  }
  const c = readVariantCaps();
  ["vb-cap1", "vb-cap2"].forEach((id) => $(id).classList.toggle("invalid", !!(c.errorB && !c.error)));
  show($("vtotal-error"), c.error || c.errorB || "");
  if (!c.error) {
    document.querySelectorAll(".va-cap").forEach((el) => { el.textContent = fmtPlaces(c.total); });
    document.querySelector(".vb-cap").textContent = `${fmtInt(c.b1 || 0)} + ${fmtPlaces(c.b2 || 0)}`;
  }
  renderVariants();
}

function renderVariantHint() {
  const b = S.meta.base;
  $("vtotal-hint").textContent =
    `Расчётная потребность территории — ${fmtPlaces(b.demand_places)}, вместимость существующих школ — ` +
    `${fmtPlaces(b.capacity_places)}, исходный дефицит — ${fmtPlaces(b.deficit_places)}. ` +
    "Новые школы не могут сократить дефицит больше, чем на исходный дефицит, поэтому вместимость стоит выбирать " +
    "соразмерно потребности населённого пункта. Для варианта B нужно не меньше 2 мест.";
  $("vtotal-deficit").textContent = `По исходному дефициту (${fmtPlaces(b.deficit_places)})`;
  $("vtotal-deficit").disabled = b.deficit_places < 2;
}

async function calcVariant(letter, placements, extraNote = "") {
  const box = $(`v${letter.toLowerCase()}-result`);
  box.textContent = "Расчёт…";
  box.classList.remove("stale");
  try {
    const result = await api("/api/scenario", { placements });
    const total = result.placements.reduce((sum, p) => sum + p.capacity, 0);
    S.variants[letter] = { placements: result.placements, result, note: extraNote, total, title: variantTitle(letter, result.placements) };
    renderVariants();
    showVariant(letter);
  } catch (error) {
    S.variants[letter] = null;
    box.textContent = error.message;
    renderVariants();
  }
}

function showVariant(letter) {
  const v = S.variants[letter];
  if (!v) return;
  setView({ label: `вариант ${v.title}`, kind: "variant", placements: v.placements, result: v.result });
}

async function calcVariantA() {
  const c = readVariantCaps();
  if (c.error) { $("va-result").textContent = c.error; return; }
  await calcVariant("A", [{ block_id: Number($("va-block").value), capacity: c.total }]);
}

async function calcVariantB() {
  const c = readVariantCaps();
  if (c.error || c.errorB) { $("vb-result").textContent = c.error || c.errorB; return; }
  const b1 = Number($("vb-block1").value), b2 = Number($("vb-block2").value);
  if (b1 === b2) { $("vb-result").textContent = "Выберите два разных квартала."; return; }
  await calcVariant("B", [{ block_id: b1, capacity: c.b1 }, { block_id: b2, capacity: c.b2 }]);
}

async function calcVariantC() {
  const box = $("vc-result");
  const c = readVariantCaps();
  if (c.error) { box.textContent = c.error; return; }
  const capacity = c.total;
  const res = S.auto.result;
  const usable = res && res.capacity === capacity && res.complete;
  if (!usable) {
    box.textContent = `Запущен перебор всех кварталов для школы на ${fmtPlaces(capacity)}…`;
    box.classList.remove("stale");
    $("auto-capacity").value = String(capacity);
    const waiting = new Promise((resolve) => S.auto.waiters.push(resolve));
    if (!(await startAuto(String(capacity)))) { box.textContent = "Не удалось запустить перебор."; return; }
    const finished = await waiting;
    if (!finished || finished.capacity !== capacity) { box.textContent = "Перебор не завершён — вариант C не определён."; return; }
  }
  const result = S.auto.result;
  if (!result.complete) {
    S.variants.C = null;
    renderVariants();
    box.textContent = "Перебор неполный — квартал для варианта C среди всех кварталов не определён. Запустите перебор заново.";
    return;
  }
  const select = $("vc-block");
  select.innerHTML = result.best.map((x) => `<option value="${x.block_id}">№${x.block_id} · сокращение ${fmtSigned(x.deficit_reduction)} мест</option>`).join("");
  select.disabled = result.best.length < 2;
  const chosen = Number(select.value);
  const note = result.best.length > 1
    ? `Равноценных лучших кварталов: ${result.best.length} (${result.best.map((x) => "№" + x.block_id).join(", ")}). Выбран №${chosen}; другой можно выбрать в списке.`
    : `Лучший квартал по результатам перебора всех ${result.total} кварталов.`;
  await calcVariant("C", [{ block_id: chosen, capacity }], note);
}

async function calcAllVariants() {
  const btn = $("variants-calc-all");
  btn.disabled = true;
  try {
    await calcVariantA();
    await calcVariantB();
    await calcVariantC();
  } finally {
    btn.disabled = false;
  }
}

function variantSummary(v) {
  const r = v.result;
  return `${esc(placementsText(v.placements))}<br>Прирост вместимости: +${fmtPlaces(r.added_capacity)} · ` +
    `остаточный дефицит: <b>${fmtInt(r.scenario.deficit_places)}</b> ${placesWord(r.scenario.deficit_places)} · сокращение: ${fmtSigned(r.deficit_reduction_places)} · ` +
    `обеспеченность: ${fmtPct(r.scenario.provision)}%` + (v.note ? `<br><span class="muted">${esc(v.note)}</span>` : "");
}

function renderVariants() {
  const caps = readVariantCaps();
  ["A", "B", "C"].forEach((L) => {
    const v = S.variants[L];
    const l = L.toLowerCase();
    const box = $(`v${l}-result`);
    $(`v${l}-show`).disabled = !v;
    if (!v) return;
    const stale = !caps.error && v.total !== caps.total;
    box.innerHTML = variantSummary(v) + (stale
      ? `<br><b>Рассчитан для ${fmtPlaces(v.total)} — пересчитайте для ${fmtPlaces(caps.total)}.</b>` : "");
    box.classList.remove("muted");
    box.classList.toggle("stale", stale);
  });
  const b = S.meta.base;
  const rows = [`<tr class="base-row"><td class="text">Исходное состояние</td><td class="text">существующие школы</td><td>0</td>` +
    `<td>${fmtInt(b.capacity_places)}</td><td>${fmtInt(b.deficit_places)}</td><td>0</td><td>${fmtPct(b.provision)}</td></tr>`];
  ["A", "B", "C"].forEach((L) => {
    const v = S.variants[L];
    if (!v) { rows.push(`<tr><td class="text">${esc(plannedTitle(L))}</td><td class="text muted" colspan="6">не рассчитан</td></tr>`); return; }
    const r = v.result;
    rows.push(`<tr><td class="text">${esc(v.title)}</td><td class="text">${esc(placementsText(v.placements))}</td>` +
      `<td>+${fmtInt(r.added_capacity)}</td><td>${fmtInt(r.scenario.capacity_places)}</td><td>${fmtInt(r.scenario.deficit_places)}</td>` +
      `<td>${fmtSigned(r.deficit_reduction_places)}</td><td>${fmtPct(r.scenario.provision)}</td></tr>`);
  });
  $("variants-table").querySelector("tbody").innerHTML = rows.join("");
}

// ---------------------------------------------------------------------------
// Сохранённые сценарии
// ---------------------------------------------------------------------------

function loadSaved() {
  try {
    const items = JSON.parse(localStorage.getItem(storageKey()) || "[]");
    return Array.isArray(items) ? items : [];
  } catch (error) { return []; }
}

function persistSaved() {
  try { localStorage.setItem(storageKey(), JSON.stringify(S.saved)); }
  catch (error) { toast("Не удалось сохранить в браузере: " + error.message, true); }
}

function savedEntry(name, placements, r) {
  return {
    id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
    name, placements,
    added: r.added_capacity,
    capacity: r.scenario.capacity_places,
    deficit: r.scenario.deficit_places,
    reduction: r.deficit_reduction_places,
    provision: r.scenario.provision,
    change_pp: r.provision_change_pp,
    data_prepared_at: S.meta.data_prepared_at,
    saved_at: new Date().toISOString(),
  };
}

function saveView() {
  if (!S.view) { toast("Сначала рассчитайте сценарий.", true); return; }
  if (S.view.kind === "draft" && !draftIsCalculated()) { toast("Сценарий изменён после расчёта — нажмите «Рассчитать сценарий».", true); return; }
  const name = $("save-name").value.trim() || `${S.view.label} (${placementsText(S.view.placements)})`;
  S.saved.push(savedEntry(name, S.view.placements, S.view.result));
  persistSaved();
  $("save-name").value = "";
  renderSaved();
  toast(`Сценарий «${name}» сохранён для сравнения.`);
}

function saveVariants() {
  const done = ["A", "B", "C"].filter((L) => S.variants[L]);
  if (!done.length) { toast("Сначала рассчитайте варианты A, B и C.", true); return; }
  done.forEach((L) => S.saved.push(savedEntry(`Вариант ${S.variants[L].title}`, S.variants[L].placements, S.variants[L].result)));
  persistSaved();
  renderSaved();
  toast(`Сохранено вариантов: ${done.length}.` + (done.length < 3 ? " Не рассчитаны: " + ["A", "B", "C"].filter((L) => !S.variants[L]).join(", ") : ""));
}

function renderSaved() {
  const b = S.meta.base;
  const rows = [`<tr class="base-row"><td class="text">Исходное состояние</td><td class="text">существующие школы</td><td>0</td>` +
    `<td>${fmtInt(b.deficit_places)}</td><td>0</td><td>${fmtPct(b.provision)}</td><td>0,0</td><td></td></tr>`];
  const best = S.saved.length ? Math.min(...S.saved.map((s) => s.deficit)) : null;
  S.saved.forEach((s) => {
    const stale = s.data_prepared_at !== S.meta.data_prepared_at ? ' <span class="badge badge-warn">данные обновлены</span>' : "";
    rows.push(`<tr class="${s.deficit === best ? "best" : ""}"><td class="text">${esc(s.name)}${stale}</td><td class="text">${esc(placementsText(s.placements))}</td>` +
      `<td>+${fmtInt(s.added)}</td><td>${fmtInt(s.deficit)}</td><td>${fmtSigned(s.reduction)}</td><td>${fmtPct(s.provision)}</td>` +
      `<td>${fmtPP(s.change_pp)}</td><td><button class="btn-link" data-show="${s.id}">на карте</button> · ` +
      `<button class="btn-link" data-del="${s.id}">удалить</button></td></tr>`);
  });
  const body = $("saved-table").querySelector("tbody");
  body.innerHTML = rows.join("");
  body.querySelectorAll("[data-del]").forEach((btn) => {
    btn.onclick = () => { S.saved = S.saved.filter((s) => s.id !== btn.dataset.del); persistSaved(); renderSaved(); };
  });
  body.querySelectorAll("[data-show]").forEach((btn) => {
    btn.onclick = async () => {
      const s = S.saved.find((x) => x.id === btn.dataset.show);
      try {
        const result = await api("/api/scenario", { placements: s.placements });
        setView({ label: `сохранённый: ${s.name}`, kind: "saved", placements: result.placements, result });
        $("map-wrap").scrollIntoView({ behavior: "smooth", block: "center" });
      } catch (error) { toast(error.message, true); }
    };
  });
  $("saved-export").disabled = !S.saved.length;
  $("saved-clear").disabled = !S.saved.length;
}

// ---------------------------------------------------------------------------
// Таблица кварталов
// ---------------------------------------------------------------------------

function renderBlocksTable() {
  const filter = $("blocks-filter").value.trim().toLowerCase();
  const relevantOnly = $("blocks-relevant").checked;
  let rows = currentRows().filter((r) => {
    if (relevantOnly && !(r.population > 0 || r.base_capacity > 0 || r.added_capacity > 0 || r.deficit_reduction_places !== 0)) return false;
    if (!filter) return true;
    return String(r.id) === filter.replace("№", "") || r.land_use_label.toLowerCase().includes(filter);
  });
  const { key, dir } = S.sort;
  rows = rows.slice().sort((a, b) => {
    const x = a[key], y = b[key];
    if (x === null && y === null) return a.id - b.id;
    if (x === null) return 1;
    if (y === null) return -1;
    return (typeof x === "string" ? x.localeCompare(y, "ru") : x - y) * dir || a.id - b.id;
  });
  document.querySelectorAll("#blocks-table th[data-key]").forEach((th) => {
    th.classList.toggle("sorted-asc", th.dataset.key === key && dir === 1);
    th.classList.toggle("sorted-desc", th.dataset.key === key && dir === -1);
  });
  const body = $("blocks-table").querySelector("tbody");
  body.innerHTML = rows.map((r) =>
    `<tr data-id="${r.id}" class="${r.id === S.selected ? "selected" : ""}"><td>№${r.id}</td><td class="text">${esc(r.land_use_label)}</td>` +
    `<td>${fmtInt(r.population)}</td><td>${fmtInt(r.demand_places)}</td><td>${fmtInt(r.base_capacity)}</td>` +
    `<td>${r.added_capacity ? "+" + fmtInt(r.added_capacity) : ""}</td><td>${fmtInt(r.scenario_capacity)}</td>` +
    `<td>${fmtPct(r.base_provision)}</td><td>${fmtPct(r.scenario_provision)}</td><td>${fmtPP(r.provision_change_pp)}</td>` +
    `<td>${fmtInt(r.base_deficit_places)}</td><td>${fmtInt(r.scenario_deficit_places)}</td><td>${fmtSigned(r.deficit_reduction_places)}</td></tr>`).join("");
  body.querySelectorAll("tr").forEach((tr) => { tr.onclick = () => selectBlock(Number(tr.dataset.id), true); });
  $("blocks-count").textContent = `Показано ${rows.length} из ${currentRows().length} кварталов · ${S.view ? "сценарий: " + S.view.label : "исходное состояние"}`;
}

// ---------------------------------------------------------------------------
// CSV
// ---------------------------------------------------------------------------

function csvValue(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "number") return String(Math.round(v * 1e6) / 1e6).replace(".", ",");
  const s = String(v);
  return /[;"\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function downloadCsv(filename, columns, rows) {
  const lines = [columns.map((c) => csvValue(c[1])).join(";")];
  rows.forEach((row) => lines.push(columns.map((c) => csvValue(typeof c[0] === "function" ? c[0](row) : row[c[0]])).join(";")));
  const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

const pct = (k) => (r) => (r[k] === null ? null : r[k] * 100);

function exportBlocks() {
  if (S.view && S.view.kind === "draft" && !draftIsCalculated()) {
    toast("Сценарий изменён после расчёта. Экспортируется последний рассчитанный сценарий.", true);
  }
  const scenario = S.view ? placementsText(S.view.placements) : "исходное состояние";
  const cols = [
    [() => scenario, "Сценарий"], ["id", "Квартал"], ["land_use_label", "Землепользование"], ["population", "Население, чел."],
    ["demand_places", "Потребность, мест"], ["base_capacity", "Вместимость исходная, мест"], ["added_capacity", "Новые места"],
    ["scenario_capacity", "Вместимость в сценарии, мест"], [pct("base_provision"), "Обеспеченность исходная, %"],
    [pct("scenario_provision"), "Обеспеченность в сценарии, %"], ["provision_change_pp", "Изменение обеспеченности, п.п."],
    ["base_served_places", "Удовлетворено исходно, мест"], ["scenario_served_places", "Удовлетворено в сценарии, мест"],
    ["base_deficit_places", "Дефицит исходный, мест"], ["scenario_deficit_places", "Дефицит в сценарии, мест"],
    ["deficit_reduction_places", "Сокращение дефицита, мест"],
  ];
  const suffix = S.view ? S.view.placements.map((p) => `${p.block_id}-${p.capacity}`).join("_") || "bez-shkol" : "ishodnoe";
  downloadCsv(`kvartaly_${suffix}.csv`, cols, currentRows());
}

function exportAuto() {
  const res = S.auto.result;
  if (!res) return;
  const rows = res.candidates.map((c) => ({ ...c, status: c.is_best ? (res.complete ? "лучший" : "лучший среди рассчитанных") : "" }));
  res.errors.forEach((e) => rows.push({ block_id: e.block_id, status: "ошибка: " + e.error }));
  downloadCsv(`avtopodbor_${res.capacity}.csv`, [
    ["rank", "Место"], ["block_id", "Квартал"], ["capacity", "Вместимость школы, мест"], ["population", "Население квартала, чел."],
    ["base_capacity", "Вместимость школ квартала, мест"], ["block_base_deficit", "Дефицит квартала исходный, мест"],
    ["scenario_deficit", "Остаточный дефицит на территории, мест"], ["deficit_reduction", "Сокращение дефицита, мест"],
    [pct("scenario_provision"), "Обеспеченность, %"], ["status", "Статус"],
  ], rows);
}

function exportSaved() {
  downloadCsv("sravnenie_scenariev.csv", [
    ["name", "Сценарий"], [(s) => placementsText(s.placements), "Размещение"], ["added", "Прирост вместимости, мест"],
    ["capacity", "Вместимость сети, мест"], ["deficit", "Остаточный дефицит, мест"], ["reduction", "Сокращение дефицита, мест"],
    [pct("provision"), "Обеспеченность, %"], ["change_pp", "Изменение, п.п."], ["saved_at", "Сохранён"],
  ], S.saved);
}

// ---------------------------------------------------------------------------
// События
// ---------------------------------------------------------------------------

function bindEvents() {
  bindMap();
  $("draft-form").addEventListener("submit", (e) => { e.preventDefault(); addDraft(); });
  $("draft-capacity").addEventListener("input", (e) => e.target.classList.remove("invalid"));
  $("draft-block").addEventListener("change", (e) => selectBlock(Number(e.target.value)));
  $("calc").onclick = calcDraft;
  $("reset").onclick = resetAll;
  $("save").onclick = saveView;

  $("auto-form").addEventListener("submit", (e) => { e.preventDefault(); startAuto(); });
  $("auto-capacity").addEventListener("input", (e) => e.target.classList.remove("invalid"));
  $("auto-cancel").onclick = async () => {
    try { await api(`/api/auto/${S.auto.jobId}/cancel`, {}); } catch (error) { toast(error.message, true); }
  };
  $("auto-export").onclick = exportAuto;

  $("va-calc").onclick = calcVariantA;
  $("vb-calc").onclick = calcVariantB;
  $("vc-calc").onclick = calcVariantC;
  $("variants-calc-all").onclick = calcAllVariants;
  $("vtotal-form").addEventListener("submit", (e) => e.preventDefault());
  $("vtotal").addEventListener("input", () => onVariantCapsChange(true));
  ["vb-cap1", "vb-cap2"].forEach((id) => $(id).addEventListener("input", () => { S.bManual = true; onVariantCapsChange(false); }));
  $("vtotal-task").onclick = () => { $("vtotal").value = "600"; S.bManual = false; onVariantCapsChange(true); };
  $("vtotal-deficit").onclick = () => { $("vtotal").value = String(S.meta.base.deficit_places); S.bManual = false; onVariantCapsChange(true); };
  $("vc-block").addEventListener("change", (e) => {
    const res = S.auto.result;
    calcVariant("C", [{ block_id: Number(e.target.value), capacity: res.capacity }],
      `Выбран один из ${res.best.length} равноценных лучших кварталов (${res.best.map((c) => "№" + c.block_id).join(", ")}).`);
  });
  ["a", "b", "c"].forEach((l) => { $(`v${l}-show`).onclick = () => showVariant(l.toUpperCase()); });
  $("variants-save").onclick = saveVariants;

  $("saved-export").onclick = exportSaved;
  $("saved-clear").onclick = () => {
    if (!confirm("Удалить все сохранённые сценарии из этого браузера?")) return;
    S.saved = []; persistSaved(); renderSaved();
  };

  $("blocks-filter").addEventListener("input", renderBlocksTable);
  $("blocks-relevant").addEventListener("change", renderBlocksTable);
  $("blocks-export").onclick = exportBlocks;
  document.querySelectorAll("#blocks-table th[data-key]").forEach((th) => {
    th.onclick = () => {
      const key = th.dataset.key;
      S.sort = { key, dir: S.sort.key === key ? -S.sort.dir : (key === "id" || key === "land_use_label" ? 1 : -1) };
      renderBlocksTable();
    };
  });
}

init();
