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
  var extraBox = {}, bandBottom = 0;
  function drawExtras(canvas, reg, overlays) {
    var layer = canvas.getLayer("pm-overlay", 2);
    while (layer.firstChild) layer.removeChild(layer.firstChild);
    extraBox = {}; bandBottom = 0;
    // bordo inferiore: l'ultima lane (o pool) che contiene le attivita' e gli eventi del flusso, non pool vuote,
    // annotazioni o altri elementi lontani
    var nodes = reg.filter(function (e) {
      return !e.waypoints && e.type !== "label" && /Task|SubProcess|CallActivity|Event|Gateway/.test(e.type);
    });
    var lanes = [];
    nodes.forEach(function (n) {
      var p = n.parent;
      while (p && p.type !== "bpmn:Lane" && p.type !== "bpmn:Participant") p = p.parent;
      if (p) {  // la lane piu' interna che contiene il nodo (le lane del modello non sono figlie nel registro)
        var inner = reg.filter(function (l) {
          return l.type === "bpmn:Lane" && n.x >= l.x && n.x <= l.x + l.width && n.y >= l.y && n.y <= l.y + l.height;
        });
        lanes.push(inner.length ? inner[inner.length - 1] : p);
      }
    });
    var ref = lanes.length ? lanes : nodes;
    var minX = Math.min.apply(null, ref.map(function (e) { return e.x; }).concat(ref.length ? [] : [0]));
    var bottom = Math.max.apply(null, ref.map(function (e) { return e.y + e.height; }).concat(ref.length ? [] : [0]));
    // fascia sotto la pool: ogni passaggio rosso scende dall'attivita' a un suo livello, corre in orizzontale e
    // risale (o scende) nell'attivita' di arrivo; le attivita' rosse stanno ancora piu' in basso
    var edges = data.red_edges || [], STEP = 14, NLEV = Math.max(1, Math.min(edges.length, 10));
    var band0 = bottom + 36, bandEnd = band0 + (NLEV - 1) * STEP;
    var W = 120, H = 70, GAP = 50, x0 = minX + 140, y0 = bandEnd + 70;
    (data.extra || []).forEach(function (x, i) {
      var bx = x0 + i * (W + GAP), by = y0;
      extraBox[x.id] = { x: bx, y: by, width: W, height: H, extra: true };
      layer.appendChild(sv("rect", { x: bx, y: by, width: W, height: H, rx: 8, class: "pm-ov-extra" }));
      var words = x.name.split(/\s+/), lines = [], cur = "";
      words.forEach(function (w) { if ((cur + " " + w).trim().length > 16 && cur) { lines.push(cur); cur = w; } else cur = (cur + " " + w).trim(); });
      if (cur) lines.push(cur);
      lines = lines.slice(0, 3);
      lines.forEach(function (ln, k) {
        layer.appendChild(sv("text", { x: bx + W / 2, y: by + H / 2 + (k - (lines.length - 1) / 2) * 13 + 4, class: "pm-ov-extra-text" }, ln));
      });
      // numero di casi sotto, a sinistra (in alto arrivano le frecce, in basso a destra quelle tra attivita' rosse)
      layer.appendChild(sv("rect", { x: bx, y: by + H + 4, width: 46, height: 16, rx: 2, class: "pm-ov-extra-badge" }));
      layer.appendChild(sv("text", { x: bx + 23, y: by + H + 16, class: "pm-ov-extra-badge-text" }, num(x.count)));
    });
    function box(id) { return extraBox[id] || reg.get(id); }
    // percorso ortogonale con angoli arrotondati: (ax,ay) -> (ax,lv) -> (bx,lv) -> (bx,by)
    function route(ax, ay, bx, by, lv) {
      var r = Math.min(8, Math.abs(bx - ax) / 2, Math.abs(lv - ay), Math.abs(lv - by));
      var dx = bx > ax ? 1 : -1, d1 = lv > ay ? 1 : -1, d2 = by > lv ? 1 : -1;
      if (Math.abs(bx - ax) < 1) return "M" + ax + " " + ay + " L" + ax + " " + lv + " L" + bx + " " + by;
      return "M" + ax + " " + ay + " L" + ax + " " + (lv - d1 * r) + " Q" + ax + " " + lv + " " + (ax + dx * r) + " " + lv +
             " L" + (bx - dx * r) + " " + lv + " Q" + bx + " " + lv + " " + bx + " " + (lv + d2 * r) + " L" + bx + " " + by;
    }
    var nOut = {}, nIn = {}, lev = 0, low = 0;
    function slot(m, id, b, out) {   // punti di attacco diversi sullo stesso bordo
      var k = m[id] = (m[id] || 0) + 1, f = out ? [0.4, 0.3, 0.2, 0.5] : [0.6, 0.7, 0.8, 0.5];
      return b.x + b.width * f[(k - 1) % 4];
    }
    edges.forEach(function (r) {
      var a = box(r.source), b = box(r.target);
      if (!a || !b) return;
      var ax = slot(nOut, r.source, a, true), bx = slot(nIn, r.target, b, false), ay, by, lv;
      if (a.extra && b.extra) {   // tra due attivita' rosse: sotto la loro fila, a destra del numero di casi
        ax = a.x + a.width * 0.6; bx = b.x + b.width * 0.85; ay = a.y + a.height; by = b.y + b.height; lv = y0 + H + 34 + (low++ % 4) * 10;
      } else {
        ay = a.extra ? a.y : a.y + a.height;
        by = b.extra ? b.y : b.y + b.height;
        lv = band0 + (lev++ % NLEV) * STEP;
      }
      layer.appendChild(sv("path", { d: route(ax, ay, bx, by, lv), class: "pm-ov-red", "marker-end": "url(#pm-ov-arrow)" }));
      if (Math.abs(bx - ax) < 30)   // quasi verticale: numero accanto alla linea
        layer.appendChild(sv("text", { x: Math.max(ax, bx) + 6, y: (Math.min(ay, by) + lv) / 2, class: "pm-ov-red-text start" }, num(r.count)));
      else layer.appendChild(sv("text", { x: (ax + bx) / 2, y: lv - 3, class: "pm-ov-red-text" }, num(r.count)));
      if (!(a.extra && b.extra)) bandBottom = Math.max(bandBottom, lv);
      if (a.extra && b.extra) extraBox[r.source].low = Math.max(extraBox[r.source].low || 0, lv);
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
    Object.keys(extraBox).forEach(function (k) { var b = extraBox[k]; xs.push(b.x, b.x + b.width); ys.push(b.y, b.y + b.height + 24); if (b.low) ys.push(b.low + 8); });
    if (bandBottom) ys.push(bandBottom + 8);
    var x0 = Math.min.apply(null, xs) - 30, x1 = Math.max.apply(null, xs) + 30, y0 = Math.min.apply(null, ys) - 40, y1 = Math.max.apply(null, ys) + 30;
    var scale = Math.min(1.4, outer.width / (x1 - x0), outer.height / (y1 - y0));
    canvas.viewbox({ x: x0, y: y0, width: outer.width / scale, height: outer.height / scale });
  }
  document.getElementById("ovl-fit").addEventListener("click", fit);
  document.getElementById("ovl-zoom-in").addEventListener("click", function () { viewer.get("zoomScroll").stepZoom(1); });
  document.getElementById("ovl-zoom-out").addEventListener("click", function () { viewer.get("zoomScroll").stepZoom(-1); });
  if (typeof touchToMouse === "function") touchToMouse(document.getElementById("ovl-canvas"), viewer);

  // BPMN di riferimento (uno solo per dataset): una variante della BPA del dataset o un modello del repository BPA
  // (sola lettura; serve l'oggetto guida dei casi, proposto quello del modello se il dataset lo ha)
  var sel = document.getElementById("ovl-ref"), open = document.getElementById("ovl-open");
  var leadField = document.getElementById("ovl-lead-field"), leadSel = document.getElementById("ovl-lead");
  function choose() {
    var opt = sel.options[sel.selectedIndex];
    if (!opt || !opt.value) return;
    var repo = opt.dataset.repo === "1";
    leadField.hidden = !repo;
    if (repo && !leadSel.value) {
      var guess = opt.dataset.lead && leadSel.querySelector('option[value="' + CSS.escape(opt.dataset.lead) + '"]');
      if (guess) leadSel.value = opt.dataset.lead;
      else { status.textContent = T.chooseLead; leadSel.focus(); return; }
    }
    fetch("/analysis/bpa/" + encodeURIComponent(sel.value) + "/reference", {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspace_id: o.ws, config_id: o.config, lead: repo ? leadSel.value : "" })
    }).then(function (r) { return r.json().then(function (j) { if (!r.ok || j.error) throw new Error(j.error || r.status); return j; }); })
      .then(function () {
        open.href = repo ? "/bpa/models/" + encodeURIComponent(sel.value) + "/view"
                         : "/analysis/bpa/" + encodeURIComponent(sel.value) + "/edit?workspace_id=" + encodeURIComponent(o.ws);
        open.hidden = false;
        var blank = sel.querySelector('option[value=""]'); if (blank) blank.remove();
        picked = null; firstFit = true; load();
      })
      .catch(function (e) { status.textContent = e.message; });
  }
  sel.addEventListener("change", function () { leadSel.value = ""; choose(); });
  leadSel.addEventListener("change", function () { if (leadSel.value) choose(); });
  load();
  return { picked: function () { return picked; }, data: function () { return data; } };
}
