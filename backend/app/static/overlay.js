/* Process Overlay: il BPMN di riferimento (bpmn-js, solo lettura) con sopra i dati del dataset.
 * Blu = nel modello e nei dati, grigio = nel modello e non nei dati, rosso = nei dati e non nel modello
 * (attivita' disegnate sotto la pool e passaggi come curve). Varianti a destra come filtro. */
function initOverlay(o) {
  var T = o.T, picked = null, data = null, timer = null, firstFit = true;
  var viewer = new BpmnJS({ container: "#ovl-canvas" });
  var SVGNS = "http://www.w3.org/2000/svg";
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined && text !== null) e.textContent = text; return e; }
  function sv(tag, attrs, text) { var e = document.createElementNS(SVGNS, tag); for (var k in attrs) e.setAttribute(k, attrs[k]); if (text !== undefined) e.textContent = text; return e; }
  function fmt(s, p) { return String(s).replace(/\{(\w+)\}/g, function (_, k) { return p[k] !== undefined ? p[k] : "{" + k + "}"; }); }
  function num(n) {
    if (n >= 1e6) return (n / 1e6).toLocaleString(o.lang, { maximumFractionDigits: 2 }) + "M";
    if (n >= 1e4) return (n / 1e3).toLocaleString(o.lang, { maximumFractionDigits: 2 }) + "k";
    return Number(n).toLocaleString(o.lang);
  }
  function pct(x) { return (x * 100).toLocaleString(o.lang, { maximumFractionDigits: 1 }) + "%"; }
  function dur(d) {
    if (d === null || d === undefined) return "—";
    var f = function (v) { return v.toLocaleString(o.lang, { maximumFractionDigits: 1 }); };
    if (d < 1) return fmt(T.hours, { n: f(d * 24) });
    if (d < 14) return fmt(T.days, { n: f(d) });
    if (d < 60) return fmt(T.weeks, { n: f(d / 7) });
    return fmt(T.months, { n: f(d / 30.4) });
  }
  var status = document.getElementById("ovl-status");

  // ---------- dati ----------
  function load() {
    status.textContent = T.loading;
    var q = new URLSearchParams({ workspace_id: o.ws, config_id: o.config });
    if (o.CF && o.CF.param()) q.set("filters", o.CF.param());
    if (picked) { q.set("vsel", "1"); picked.forEach(function (v) { q.append("var", v); }); }
    return fetch("/analysis/overlay/data?" + q.toString(), { credentials: "same-origin" })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok || j.error) throw new Error(j.error || r.status); return j; }); })
      .then(function (j) {
        document.getElementById("ovl-work").hidden = !j.reference;
        document.getElementById("ovl-empty").hidden = !!j.reference;
        if (!j.reference) { status.textContent = ""; return; }
        data = j; drawVariants(); return draw();
      })
      .catch(function (e) { status.textContent = T.error + " (" + (e.message || e) + ")"; });
  }
  function reload() { clearTimeout(timer); timer = setTimeout(load, 200); }
  if (o.CF) o.CF.onChange(function () { picked = null; load(); });

  // ---------- varianti (filtro) ----------
  function isOn(id) { return !picked || picked.indexOf(id) >= 0; }
  function drawVariants() {
    var list = document.getElementById("ovl-variants"); list.textContent = "";
    (data.variants || []).forEach(function (v) {
      var li = el("li", "ovl-variant" + (isOn(v.id) ? " on" : ""));
      var cb = el("input"); cb.type = "checkbox"; cb.checked = isOn(v.id); cb.setAttribute("aria-label", "#" + v.id);
      cb.title = v.steps.map(function (s) { return s.activity + (s.repeated ? " ×" : ""); }).join(" → ");
      cb.addEventListener("change", function () {
        var cur = picked ? picked.slice() : data.variants.map(function (x) { return x.id; });
        picked = cb.checked ? cur.concat([v.id]) : cur.filter(function (x) { return x !== v.id; });
        if (picked.length >= data.variants_total) picked = null;
        reload();
      });
      li.appendChild(cb);
      li.appendChild(el("span", "ovl-vid", "#" + v.id));
      var p = el("span", "ovl-vpct"); p.appendChild(el("strong", "", pct(v.share)));
      p.appendChild(el("span", "tiny muted", v.count === 1 ? T.case1 : fmt(T.cases, { n: num(v.count) }))); li.appendChild(p);
      li.appendChild(el("span", "ovl-vdur", dur(v.median)));
      li.title = cb.title;
      list.appendChild(li);
    });
    var nv = picked ? picked.length : data.variants_total;
    document.getElementById("ovl-vcount").textContent = fmt(T.of, { a: num(nv), b: num(data.variants_total) });
    document.getElementById("ovl-vbar").style.width = (data.variants_total ? 100 * nv / data.variants_total : 0) + "%";
    var share = data.cases_total ? data.cases_selected / data.cases_total : 0;
    document.getElementById("ovl-ccount").textContent = pct(share);
    document.getElementById("ovl-cbar").style.width = (100 * share) + "%";
  }
  document.querySelectorAll("[data-pick]").forEach(function (a) {
    a.addEventListener("click", function (e) {
      e.preventDefault();
      var k = a.dataset.pick;
      picked = k === "all" ? null : k === "none" ? [] : [1];
      load();
    });
  });

  // ---------- disegno ----------
  var markers = [];
  function draw() {
    var canvas = viewer.get("canvas"), keep = firstFit ? null : canvas.viewbox();
    return viewer.importXML(data.xml).then(function () {
      var reg = viewer.get("elementRegistry"), overlays = viewer.get("overlays");
      Object.keys(data.elements).forEach(function (id) {
        var e = reg.get(id), x = data.elements[id];
        if (!e) return;
        canvas.addMarker(id, x.status === "ok" ? "pm-ov-ok" : "pm-ov-miss");
        if (x.count && /Task|SubProcess|CallActivity|Transaction/.test(e.type))
          overlays.add(id, { position: { top: -22, left: 0 }, html: '<div class="pm-ov-badge">' + num(x.count) + "</div>" });
      });
      Object.keys(data.flows).forEach(function (id) {
        var e = reg.get(id), x = data.flows[id];
        if (!e) return;
        canvas.addMarker(id, x.status === "ok" ? "pm-ov-ok" : "pm-ov-miss");
        if (x.count && e.waypoints && e.waypoints.length > 1) {
          var w = e.waypoints, i = Math.floor((w.length - 1) / 2), a = w[i], b = w[i + 1];
          overlays.add(id, { position: { left: (a.x + b.x) / 2 - e.x - 18, top: (a.y + b.y) / 2 - e.y - 18 },
                             html: '<div class="pm-ov-flow">' + num(x.count) + "</div>" });
        }
      });
      drawExtras(canvas, reg, overlays);
      if (keep && !firstFit) canvas.viewbox(keep); else fit();
      firstFit = false;
      var notes = [];
      if (data.empty) notes.push(T.empty);
      else if (picked && !picked.length) notes.push(T.none);
      if (data.extra_hidden) notes.push(fmt(T.extraHidden, { n: data.extra_hidden }));
      if (data.unmatched_tasks && data.unmatched_tasks.length) notes.push(fmt(T.unmatched, { n: data.unmatched_tasks.length }));
      document.getElementById("ovl-note").textContent = notes.join(" ");
      status.textContent = data.empty ? "" : fmt(T.cases, { n: num(data.cases_selected) });
    });
  }

  // attivita' in rosso sotto la pool, passaggi in rosso come curve
  var extraBox = {};
  function drawExtras(canvas, reg, overlays) {
    var layer = canvas.getLayer("pm-overlay", 2);
    while (layer.firstChild) layer.removeChild(layer.firstChild);
    extraBox = {};
    var parts = reg.filter(function (e) { return e.type === "bpmn:Participant"; });
    var all = reg.filter(function (e) { return !e.waypoints && e.type !== "label" && e.parent; });
    var minX = Math.min.apply(null, all.map(function (e) { return e.x; }).concat([0]));
    var bottom = Math.max.apply(null, (parts.length ? parts : all).map(function (e) { return e.y + e.height; }).concat([0]));
    var W = 120, H = 70, GAP = 50, x0 = minX + 140, y0 = bottom + 110;
    (data.extra || []).forEach(function (x, i) {
      var bx = x0 + i * (W + GAP), by = y0;
      extraBox[x.id] = { x: bx, y: by, width: W, height: H };
      layer.appendChild(sv("rect", { x: bx, y: by, width: W, height: H, rx: 8, class: "pm-ov-extra" }));
      var words = x.name.split(/\s+/), lines = [], cur = "";
      words.forEach(function (w) { if ((cur + " " + w).trim().length > 16 && cur) { lines.push(cur); cur = w; } else cur = (cur + " " + w).trim(); });
      if (cur) lines.push(cur);
      lines = lines.slice(0, 3);
      lines.forEach(function (ln, k) {
        layer.appendChild(sv("text", { x: bx + W / 2, y: by + H / 2 + (k - (lines.length - 1) / 2) * 13 + 4, class: "pm-ov-extra-text" }, ln));
      });
      layer.appendChild(sv("rect", { x: bx, y: by - 20, width: 46, height: 16, rx: 2, class: "pm-ov-extra-badge" }));
      layer.appendChild(sv("text", { x: bx + 23, y: by - 8, class: "pm-ov-extra-badge-text" }, num(x.count)));
    });
    function box(id) { return extraBox[id] || reg.get(id); }
    (data.red_edges || []).forEach(function (r, idx) {
      var a = box(r.source), b = box(r.target);
      if (!a || !b) return;
      var ax = a.x + a.width / 2, ay = a.y + a.height / 2, bx = b.x + b.width / 2, by = b.y + b.height / 2;
      // dal bordo dei riquadri (alto o basso secondo la posizione)
      ay = by > ay ? a.y + a.height : (by < ay ? a.y : ay);
      by = ay > by ? b.y + b.height : b.y;
      var bend = Math.max(30, Math.abs(bx - ax) * 0.15);
      var d = "M" + ax + " " + ay + " C" + ax + " " + (ay + (by > ay ? bend : -bend)) + " " + bx + " " + (by + (by > ay ? -bend : bend)) + " " + bx + " " + by;
      layer.appendChild(sv("path", { d: d, class: "pm-ov-red", "marker-end": "url(#pm-ov-arrow)" }));
      // etichetta lungo la curva (a 1/3, 1/2 o 2/3 alternati: le etichette vicine non si sovrappongono)
      var tt = [0.5, 0.35, 0.65][idx % 3], u = 1 - tt;
      var c1y = ay + (by > ay ? bend : -bend), c2y = by + (by > ay ? -bend : bend);
      var lx = u * u * u * ax + 3 * u * u * tt * ax + 3 * u * tt * tt * bx + tt * tt * tt * bx;
      var ly = u * u * u * ay + 3 * u * u * tt * c1y + 3 * u * tt * tt * c2y + tt * tt * tt * by;
      layer.appendChild(sv("text", { x: lx, y: ly - 4, class: "pm-ov-red-text" }, num(r.count)));
    });
    // freccia rossa (una volta)
    var svg = canvas._svg;
    if (svg && !svg.querySelector("#pm-ov-arrow")) {
      var defs = svg.querySelector("defs") || svg.insertBefore(sv("defs", {}), svg.firstChild);
      var m = sv("marker", { id: "pm-ov-arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" });
      m.appendChild(sv("path", { d: "M0 0 L10 5 L0 10 z", fill: "#d23f5b" }));
      defs.appendChild(m);
    }
  }

  function fit() {
    var canvas = viewer.get("canvas");
    canvas.zoom("fit-viewport", "auto");
    var vb = canvas.viewbox(), inner = vb.inner, outer = vb.outer;
    var xs = [inner.x, inner.x + inner.width], ys = [inner.y, inner.y + inner.height];
    Object.keys(extraBox).forEach(function (k) { var b = extraBox[k]; xs.push(b.x, b.x + b.width); ys.push(b.y - 24, b.y + b.height); });
    var x0 = Math.min.apply(null, xs) - 30, x1 = Math.max.apply(null, xs) + 30, y0 = Math.min.apply(null, ys) - 40, y1 = Math.max.apply(null, ys) + 30;
    var scale = Math.min(1.4, outer.width / (x1 - x0), outer.height / (y1 - y0));
    canvas.viewbox({ x: x0, y: y0, width: outer.width / scale, height: outer.height / scale });
  }
  document.getElementById("ovl-fit").addEventListener("click", fit);
  document.getElementById("ovl-zoom-in").addEventListener("click", function () { viewer.get("zoomScroll").stepZoom(1); });
  document.getElementById("ovl-zoom-out").addEventListener("click", function () { viewer.get("zoomScroll").stepZoom(-1); });
  if (typeof touchToMouse === "function") touchToMouse(document.getElementById("ovl-canvas"), viewer);

  // BPMN di riferimento (uno solo per dataset)
  var sel = document.getElementById("ovl-ref"), open = document.getElementById("ovl-open");
  sel.addEventListener("change", function () {
    if (!sel.value) return;
    fetch("/analysis/bpa/" + encodeURIComponent(sel.value) + "/reference", {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_id: o.ws })
    }).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function () {
        open.href = "/analysis/bpa/" + encodeURIComponent(sel.value) + "/edit?workspace_id=" + encodeURIComponent(o.ws); open.hidden = false;
        var blank = sel.querySelector('option[value=""]'); if (blank) blank.remove();
        picked = null; firstFit = true; load();
      })
      .catch(function (e) { alert(e.message); });
  });
  load();
}
