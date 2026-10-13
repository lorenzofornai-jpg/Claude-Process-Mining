/* Filtri sui casi condivisi tra le analisi dello stesso dataset (Process Overview, Process Explorer, Control Tower).
 *
 * Una selezione fatta su un grafico (mesi, intervalli di tempo, ...) diventa un filtro sui casi: gli oggetti di quel
 * tipo che la soddisfano, con gli oggetti collegati e tutta la loro storia (services/ocel_filter.py). I filtri restano
 * nella scheda del browser (sessionStorage) per dataset: aprendo un'altra analisi dello stesso dataset valgono anche li'.
 * Valori diversi nello stesso filtro: basta uno (OR); filtri diversi: valgono tutti (AND).
 */
function initCaseFilters(opts) {
  var KEY = "pm-filters-" + opts.config, T = opts.T, LANG = opts.lang || "it", listeners = [];
  var bar = document.getElementById(opts.barId || "pm-filters");

  function read() { try { var v = JSON.parse(window.sessionStorage.getItem(KEY) || "[]"); return Array.isArray(v) ? v : []; } catch (e) { return []; } }
  function write(list) { try { window.sessionStorage.setItem(KEY, JSON.stringify(list)); } catch (e) {} }
  var filters = read();

  function fmt(s, p) { return String(s).replace(/\{(\w+)\}/g, function (_, k) { return p[k] !== undefined ? p[k] : "{" + k + "}"; }); }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
  function plural(t) { return (opts.plurals && opts.plurals[t]) || t; }
  function monthText(m) {
    var p = m.split("-"), d = new Date(Date.UTC(+p[0], +p[1] - 1, 1));
    return d.toLocaleDateString(LANG, { month: "short", year: "2-digit", timeZone: "UTC" });
  }
  function nextMonth(m) { var y = +m.slice(0, 4), mo = +m.slice(5, 7) + 1; if (mo > 12) { y++; mo = 1; } return y + "-" + (mo < 10 ? "0" : "") + mo; }
  // mesi consecutivi raccolti in intervalli: «gen 26 – mar 26, mag 26»
  function monthsText(values) {
    var v = values.slice().sort(), out = [], i = 0;
    while (i < v.length) {
      var j = i; while (j + 1 < v.length && v[j + 1] === nextMonth(v[j])) j++;
      out.push(j > i ? monthText(v[i]) + " – " + monthText(v[j]) : monthText(v[i])); i = j + 1;
    }
    return out.join(", ");
  }
  function daysText(n) { return fmt(T.days, { n: Number(n).toLocaleString(LANG, { maximumFractionDigits: 1 }) }); }
  function rangesText(ranges) {
    return ranges.map(function (r) { return r[1] === null || r[1] === undefined ? "> " + daysText(r[0]) : daysText(r[0]) + " – " + daysText(r[1]); }).join(", ");
  }
  function text(v) { return v && typeof v === "object" ? (v[LANG] || v.it || v.en || "") : (v || ""); }
  function valueText(v) { return v === "" ? T.empty : v; }
  function label(f) {
    if (f.kind === "dimension") return fmt(T.chipAttr, { tp: plural(f.type), a: f.title, v: f.values.map(valueText).join(", ") });
    if (f.kind === "condition") return fmt(T.chipCond, { tp: plural(f.type), l: text(f.label) });
    if (f.kind === "start_month") return fmt(T.chipStart, { tp: plural(f.type), m: monthsText(f.values) });
    if (f.kind === "duration") return fmt(f.scope === "related" ? T.chipDurationRel : T.chipDuration, { tp: plural(f.type), r: rangesText(f.ranges) });
    if (f.kind === "event") return fmt(T.chipEvent, { a: f.attribute, v: f.values.join(", ") });
    return fmt(T.chipAttr, { tp: plural(f.type), a: f.attribute, v: f.values.join(", ") });
  }
  function same(a, b) {
    if (a.key || b.key) return (a.key || "") === (b.key || "");
    return a.kind === b.kind && (a.type || null) === (b.type || null) && (a.attribute || null) === (b.attribute || null) && (a.scope || null) === (b.scope || null); }

  function render() {
    if (!bar) return;
    bar.textContent = "";
    bar.hidden = false;
    bar.classList.toggle("pm-filters-empty", !filters.length);
    bar.appendChild(el("span", "pm-filters-title", filters.length ? T.title : T.none));
    filters.forEach(function (f, i) {
      var chip = el("span", "pm-chip"); chip.appendChild(el("span", "pm-chip-text", label(f)));
      var x = el("button", "pm-chip-x", "✕"); x.type = "button"; x.title = T.remove; x.setAttribute("aria-label", T.remove + ": " + label(f));
      x.addEventListener("click", function () { filters.splice(i, 1); changed(); });
      chip.appendChild(x); bar.appendChild(chip);
    });
    var tools = el("span", "pm-filters-tools");
    if (filters.length) {
      var all = el("button", "link-btn pm-chip-clear", T.clear); all.type = "button";
      all.addEventListener("click", function () { filters = []; changed(); });
      tools.appendChild(all);
    }
    if (opts.ws) {
      var views = el("button", "secondary pm-tool", T.views + " ▾"); views.type = "button";
      views.addEventListener("click", function () { togglePanel("views"); });
      tools.appendChild(views);
    }
    bar.appendChild(tools);
    if (filters.length) bar.appendChild(el("span", "tiny muted pm-filters-note", T.note));
    if (panel) bar.appendChild(panel);
  }

  // ---------- pannello «Viste» ----------
  var panel = null, panelKind = null;
  function call(url, body) {
    var o = body ? { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : { credentials: "same-origin" };
    return fetch(url, o).then(function (r) { return r.json().then(function (j) { if (!r.ok || j.error) throw new Error(j.error || r.status); return j; }); });
  }
  function q(extra) { var p = new URLSearchParams({ workspace_id: opts.ws, config_id: opts.config }); return p.toString() + (extra || ""); }
  function closePanel() { panel = null; panelKind = null; render(); }
  function togglePanel(kind) {
    if (panelKind === kind) { closePanel(); return; }
    panelKind = kind; panel = el("div", "pm-panel");
    panel.appendChild(el("p", "tiny muted", T.loading));
    render();
    call("/analysis/views?" + q()).then(viewsPanel).catch(function (e) { panel.textContent = e.message; });
  }
  function viewsPanel(j) {
    if (panelKind !== "views") return;
    panel.textContent = "";
    var views = j.views || [];
    if (!views.length) panel.appendChild(el("p", "tiny muted", T.noViews));
    var ul = el("ul", "pm-views");
    views.forEach(function (v) {
      var li = el("li");
      var open = el("button", "link-btn pm-view-open", v.name); open.type = "button";
      open.title = (v.filters || []).map(label).join(" · ");
      open.addEventListener("click", function () { panel = null; panelKind = null; filters = (v.filters || []).slice(); changed(); });
      li.appendChild(open);
      var nf = (v.filters || []).length;
      li.appendChild(el("span", "tiny muted", " " + fmt(nf === 1 ? T.viewBy1 : T.viewBy, { u: v.created_by || "", n: nf })));
      if (v.mine) {
        var del = el("button", "pm-chip-x pm-view-del", "✕"); del.type = "button"; del.title = T.deleteView;
        del.addEventListener("click", function () {
          if (!window.confirm(fmt(T.confirmDelete, { v: v.name }))) return;
          call("/analysis/views/" + encodeURIComponent(v.id) + "/delete", { workspace_id: opts.ws }).then(function () { panelKind = null; togglePanel("views"); })
            .catch(function (e) { alert(e.message); });
        });
        li.appendChild(del);
      }
      ul.appendChild(li);
    });
    panel.appendChild(ul);
    var row = el("div", "pm-panel-row");
    var name = el("input"); name.type = "text"; name.maxLength = 200; name.placeholder = T.viewName; name.disabled = !filters.length;
    var save = el("button", "primary", T.saveView); save.type = "button"; save.disabled = !filters.length;
    save.addEventListener("click", function () {
      if (!name.value.trim()) { name.focus(); return; }
      call("/analysis/views", { workspace_id: opts.ws, config_id: opts.config, name: name.value.trim(), filters: filters })
        .then(function () { panelKind = null; togglePanel("views"); }).catch(function (e) { alert(e.message); });
    });
    row.appendChild(field(filters.length ? T.saveAs : T.saveNeedsFilters, name)); row.appendChild(save);
    panel.appendChild(row);
  }
  function field(text, control) { var l = el("label", "pm-field"); l.appendChild(el("span", "tiny muted", text)); l.appendChild(control); return l; }

  function changed() { write(filters); render(); listeners.forEach(function (fn) { fn(filters.slice()); }); }

  var api = {
    get: function () { return filters.slice(); },
    param: function () { return filters.length ? JSON.stringify(filters) : ""; },
    find: function (proto) { return filters.filter(function (f) { return same(f, proto); })[0] || null; },
    // tutti i filtri che soddisfano una condizione (es. i mesi di inizio dei tipi dei KPI)
    where: function (fn) { return filters.filter(fn); },
    // toglie piu' filtri insieme (un solo ricalcolo)
    removeWhere: function (fn) { var n = filters.length; filters = filters.filter(function (f) { return !fn(f); }); if (filters.length !== n) changed(); },
    // sostituisce (o toglie, se values/ranges vuoti) il filtro dello stesso tipo
    put: function (f) {
      var empty = f.kind === "condition" ? !(f.picked && f.picked.length) : !(f.values && f.values.length) && !(f.ranges && f.ranges.length);
      var idx = -1; filters.forEach(function (x, i) { if (same(x, f)) idx = i; });
      if (empty) { if (idx >= 0) filters.splice(idx, 1); }
      else if (idx >= 0) filters[idx] = f; else filters.push(f);
      changed();
    },
    // piu' cambiamenti insieme, un solo ricalcolo: fn riceve una copia dei filtri e restituisce quelli nuovi
    edit: function (fn) { filters = fn(filters.slice()).filter(Boolean); changed(); },
    onChange: function (fn) { listeners.push(fn); },
    label: label
  };
  render();
  window.CASE_FILTERS = api;   // per l'assistente (static/assistant.js)
  return api;
}

/* Selezione su una serie di barre, colonne, punti o righe (in ordine).
 *  - tocco su un elemento: aggiunge o toglie quel valore, subito (onPick([i], "toggle"));
 *  - trascinamento: compare un pennello (rettangolo con due maniglie) come negli strumenti di process mining;
 *    si allarga o si stringe dalle maniglie, si sposta trascinandolo, e si conferma con ✓ (i valori dentro),
 *    ✕ (annulla) o ◐ (inverti: tutti i valori tranne quelli dentro) -> onPick(indici, "range").
 * Orientamento automatico: colonne affiancate (pennello orizzontale) o righe una sotto l'altra (verticale). */
function attachBarSelection(items, onPick) {
  items = (items || []).filter(Boolean);
  if (!items.length) return;
  // spesso il grafico viene inserito nella pagina subito dopo: si aspetta che ci sia (servono le misure)
  if (!items[0].isConnected) {
    if (!(arguments[2] > 5)) { var n = (arguments[2] || 0) + 1; requestAnimationFrame(function () { attachBarSelection(items, onPick, n); }); }
    return;
  }
  var T = window.CASE_FILTER_T || {};
  var host = items[0].ownerSVGElement ? items[0].ownerSVGElement.parentNode : items[0].parentNode;
  if (items[0].tagName === "TR") { var tb = items[0].closest("table"); host = tb && tb.parentNode; }   // non dentro la tabella
  if (!host) return;
  host.classList.add("pm-brush-host");   // position: relative (gli elementi possono non essere ancora nella pagina)
  function rect(e) { return e.getBoundingClientRect(); }
  var r0 = rect(items[0]), r1 = rect(items[items.length - 1]);
  var horizontal = items.length < 2 || Math.abs((r1.left + r1.right) - (r0.left + r0.right)) >= Math.abs((r1.top + r1.bottom) - (r0.top + r0.bottom));
  host.style.touchAction = horizontal ? "pan-y" : "auto";   // colonne: il dito in orizzontale disegna il pennello
  items.forEach(function (it) { it.classList.add("pm-pick"); });

  // coordinate nel contenuto del contenitore (che puo' scorrere)
  function pos(clientX, clientY) {
    var h = rect(host);
    return horizontal ? clientX - h.left + host.scrollLeft : clientY - h.top + host.scrollTop;
  }
  function span(it) {
    var h = rect(host), r = rect(it);
    return horizontal ? [r.left - h.left + host.scrollLeft, r.right - h.left + host.scrollLeft]
                      : [r.top - h.top + host.scrollTop, r.bottom - h.top + host.scrollTop];
  }
  function inside(a, b) {
    var lo = Math.min(a, b), hi = Math.max(a, b), out = [];
    items.forEach(function (it, i) { var s = span(it), c = (s[0] + s[1]) / 2; if (c >= lo && c <= hi) out.push(i); });
    return out;
  }
  function mark(list) { items.forEach(function (it, i) { it.classList.toggle("pm-brush", !!list && list.indexOf(i) >= 0); }); }

  var brush = null, tools = null, a = 0, b = 0, drag = null, snap = null;
  var INVERT = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M8 2a6 6 0 0 1 0 12z" fill="currentColor"/></svg>';
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; }
  function cross() { var h = rect(host); return horizontal ? [0, Math.max(host.scrollHeight, h.height)] : [0, Math.max(host.scrollWidth, h.width)]; }
  // «cella» di ogni elemento: da meta' strada dal precedente a meta' strada dal successivo, cosi' il riquadro
  // contiene sempre colonne o righe intere e scatta all'elemento successivo quando si supera la meta'
  function cells() {
    var cs = items.map(function (it, i) { var r = span(it); return { i: i, c: (r[0] + r[1]) / 2, s: r }; });
    var o = cs.slice().sort(function (x, y) { return x.c - y.c; }), out = [];
    o.forEach(function (x, k) {
      var prev = o[k - 1], next = o[k + 1];
      var lo = prev ? (prev.c + x.c) / 2 : Math.min(x.s[0], next ? x.c - (next.c - x.c) / 2 : x.s[0]);
      var hi = next ? (x.c + next.c) / 2 : Math.max(x.s[1], prev ? x.c + (x.c - prev.c) / 2 : x.s[1]);
      out[x.i] = [Math.max(0, lo), hi];
    });
    return out;
  }
  function draw() {
    var lo = Math.min(a, b), hi = Math.max(a, b), c = cross(), list = inside(a, b);
    if (list.length) {
      var cl = cells();
      lo = Math.min.apply(null, list.map(function (i) { return cl[i][0]; }));
      hi = Math.max.apply(null, list.map(function (i) { return cl[i][1]; }));
    }
    snap = list.length ? [lo, hi] : null;
    if (horizontal) { brush.style.left = lo + "px"; brush.style.width = (hi - lo) + "px"; brush.style.top = c[0] + "px"; brush.style.height = c[1] + "px"; }
    else {
      brush.style.top = lo + "px"; brush.style.height = (hi - lo) + "px"; brush.style.left = c[0] + "px"; brush.style.width = c[1] + "px";
      // tabelle larghe che scorrono: maniglie nella parte visibile
      var mid = host.scrollLeft + Math.min(host.clientWidth || c[1], c[1]) / 2;
      brush.querySelectorAll(".pm-brush-handle").forEach(function (hd) { hd.style.left = mid + "px"; });
    }
    mark(list);
    placeTools();
  }
  // pulsanti ✓ ✕ ◐ sempre visibili: dentro il riquadro se c'e' posto, altrimenti spostati nella parte visibile
  // del grafico e dello schermo (colonne ai bordi della pagina, grafici che scorrono)
  function placeTools() {
    var br = rect(brush), hr = rect(host), vv = window.visualViewport;
    var vx = vv ? vv.offsetLeft : 0, vy = vv ? vv.offsetTop : 0, vw = vv ? vv.width : window.innerWidth, vh = vv ? vv.height : window.innerHeight;
    var tw = tools.offsetWidth, th = tools.offsetHeight, m = 6;
    var L = Math.max(hr.left, vx) + m, R = Math.min(hr.right, vx + vw) - m, Tp = Math.max(hr.top, vy) + m, Bt = Math.min(hr.bottom, vy + vh) - m;
    var x = horizontal ? br.left + 14 : Math.min(br.right, R) - tw - 10;
    var y = horizontal ? br.top + 4 : br.top + (br.height - th) / 2;
    x = Math.max(L, Math.min(x, R - tw)); y = Math.max(Tp, Math.min(y, Bt - th));
    tools.style.left = (x - br.left) + "px"; tools.style.top = (y - br.top) + "px";
  }
  function onResize() { if (brush) draw(); }
  function onScroll() { if (brush) placeTools(); }
  host.addEventListener("scroll", function () { if (brush && !horizontal) draw(); });
  function remove() {
    if (brush) brush.remove(); brush = null; mark(null);
    document.removeEventListener("keydown", keys); window.removeEventListener("resize", onResize); window.removeEventListener("scroll", onScroll);
  }
  function finish(invert) {
    var list = inside(a, b);
    if (invert) list = items.map(function (_, i) { return i; }).filter(function (i) { return list.indexOf(i) < 0; });
    remove();
    if (list.length) onPick(list, "range");
  }
  function keys(e) { if (e.key === "Escape") remove(); else if (e.key === "Enter") { e.preventDefault(); finish(false); } }
  function make() {
    brush = el("div", "pm-brush-box" + (horizontal ? " pm-brush-h" : " pm-brush-v"));
    var h1 = el("span", "pm-brush-handle pm-brush-start"), h2 = el("span", "pm-brush-handle pm-brush-end");
    var bar = tools = el("span", "pm-brush-tools");
    [["✓", T.brushApply, function () { finish(false); }], ["✕", T.brushCancel, remove], ["◐", T.brushInvert, function () { finish(true); }]]
      .forEach(function (x, i) {
        var btn = el("button", "pm-brush-btn pm-brush-btn-" + i, i === 2 ? "" : x[0]); btn.type = "button";
        if (i === 2) btn.innerHTML = INVERT; btn.title = x[1] || ""; btn.setAttribute("aria-label", x[1] || x[0]);
        btn.addEventListener("pointerdown", function (e) { e.stopPropagation(); });
        btn.addEventListener("click", function (e) { e.stopPropagation(); x[2](); });
        bar.appendChild(btn);
      });
    brush.appendChild(h1); brush.appendChild(h2); brush.appendChild(bar);
    host.appendChild(brush);
    document.addEventListener("keydown", keys); window.addEventListener("resize", onResize); window.addEventListener("scroll", onScroll, { passive: true });
  }
  // Che cosa si prende premendo in un punto: una maniglia, il riquadro (per spostarlo) o un nuovo pennello
  // (su un elemento o nello spazio tra gli elementi). null = niente (fuori, o sui pulsanti ✓ ✕ ◐).
  function grab(target, x, y) {
    if (brush && brush.contains(target)) {
      if (target.closest(".pm-brush-tools")) return null;
      if (target.classList.contains("pm-brush-start")) return { what: Math.min(a, b) === a ? "a" : "b" };
      if (target.classList.contains("pm-brush-end")) return { what: Math.max(a, b) === b ? "b" : "a" };
      return { what: "move" };
    }
    var i = -1; items.forEach(function (it, k) { if (it === target || it.contains(target)) i = k; });
    if (i < 0) {                          // si puo' partire anche dallo spazio vuoto tra gli elementi (es. sopra i punti)
      var p0 = pos(x, y), s0 = span(items[0]), s1 = span(items[items.length - 1]);
      if (p0 < Math.min(s0[0], s1[0]) || p0 > Math.max(s0[1], s1[1])) return brush ? { what: "out" } : null;
    }
    return { what: "new", item: i };
  }
  function begin(g, x, y) {
    if (g.what === "out") { remove(); return; }
    if (g.what === "new" && brush) remove();
    drag = { what: g.what, item: g.item, p: pos(x, y), a: a, b: b, moved: false };
  }
  function moveTo(x, y) {
    var p = pos(x, y), d = p - drag.p;
    if (!drag.moved && Math.abs(d) < 6) return;
    drag.moved = true;
    if (drag.what === "new") { if (!brush) { make(); a = drag.p; } b = p; }
    else if (drag.what === "row") {           // dalla riga tenuta premuta, verso l'alto o verso il basso
      if (p >= drag.s0[0]) { a = drag.s0[0]; b = Math.max(p, drag.s0[1]); } else { a = drag.s0[1]; b = p; }
    }
    else if (drag.what === "a") a = drag.a + d;
    else if (drag.what === "b") b = drag.b + d;
    else { a = drag.a + d; b = drag.b + d; }
    draw();
  }
  function end() {
    if (!drag) return;
    var dr = drag; drag = null;
    if (dr.what === "new" && !dr.moved) { if (dr.item >= 0) onPick([dr.item], "toggle"); return; }
    if (brush && !inside(a, b).length) { remove(); return; }
    if (brush && snap) { a = snap[0]; b = snap[1]; draw(); }   // i bordi restano sulle colonne o righe intere
  }

  // mouse, penna, trackpad: eventi pointer (il dito si gestisce sotto, con gli eventi touch)
  host.addEventListener("pointerdown", function (e) {
    if (e.pointerType === "touch" || e.button) return;
    var g = grab(e.target, e.clientX, e.clientY); if (!g) return;
    e.preventDefault(); begin(g, e.clientX, e.clientY);
    try { host.setPointerCapture(e.pointerId); } catch (err) {}
  });
  host.addEventListener("pointermove", function (e) { if (e.pointerType !== "touch" && drag) moveTo(e.clientX, e.clientY); });
  host.addEventListener("pointerup", function (e) { if (e.pointerType !== "touch") end(); });
  host.addEventListener("pointercancel", function (e) { if (e.pointerType !== "touch") drag = null; });

  // dito (iPad, telefono): sul riquadro e sulle maniglie si trascina subito; altrove si decide dal primo movimento:
  // lungo i valori = pennello, di traverso = la pagina scorre. Righe di tabella: tieni premuto un attimo, poi trascina
  // (cosi' la pagina continua a scorrere con il dito sulla tabella).
  var touch = null;
  host.addEventListener("touchstart", function (e) {
    if (e.touches.length !== 1) { touch = null; drag = null; return; }
    var t = e.touches[0], g = grab(e.target, t.clientX, t.clientY);
    if (!g) { touch = null; return; }
    if (g.what === "out") { remove(); touch = null; return; }
    touch = { g: g, x: t.clientX, y: t.clientY, live: g.what !== "new", timer: null };
    if (touch.live) { e.preventDefault(); begin(g, t.clientX, t.clientY); return; }
    if (!horizontal) {
      touch.timer = setTimeout(function () {
        if (!touch) return;
        touch.live = true; begin(touch.g, touch.x, touch.y);
        var s0 = span(items[Math.max(0, touch.g.item)]); make(); a = s0[0]; b = s0[1];
        drag.what = "row"; drag.s0 = s0; drag.moved = true; draw();
        if (navigator.vibrate) try { navigator.vibrate(10); } catch (err) {}
      }, 380);
    }
  }, { passive: false });
  host.addEventListener("touchmove", function (e) {
    if (!touch || e.touches.length !== 1) return;
    var t = e.touches[0];
    if (!touch.live) {
      var dm = horizontal ? t.clientX - touch.x : t.clientY - touch.y, dc = horizontal ? t.clientY - touch.y : t.clientX - touch.x;
      if (Math.abs(dm) < 8 && Math.abs(dc) < 8) return;
      if (!horizontal || Math.abs(dc) >= Math.abs(dm)) { clearTimeout(touch.timer); touch = null; return; }   // scorre la pagina
      touch.live = true; begin(touch.g, touch.x, touch.y);
    }
    if (e.cancelable) e.preventDefault();
    if (drag) moveTo(t.clientX, t.clientY);
  }, { passive: false });
  host.addEventListener("touchend", function (e) {
    if (!touch) return;
    var tc = touch; touch = null; clearTimeout(tc.timer);
    if (tc.live) { if (e.cancelable) e.preventDefault(); end(); return; }
    if (tc.g.what === "new" && tc.g.item >= 0) { if (e.cancelable) e.preventDefault(); onPick([tc.g.item], "toggle"); }   // tocco
  }, { passive: false });
  host.addEventListener("touchcancel", function () { if (touch) clearTimeout(touch.timer); touch = null; drag = null; });
  items.forEach(function (it, i) {
    it.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick([i], "toggle"); } });
  });
}

/* JSON con le chiavi in ordine (stesso testo di json.dumps(sort_keys=True, separators=(",", ":")) in Python):
 * serve a costruire la chiave che lega un filtro al grafico da cui parte. */
function stableKey(v) {
  if (v === null || typeof v !== "object") return JSON.stringify(v);
  if (Array.isArray(v)) return "[" + v.map(stableKey).join(",") + "]";
  return "{" + Object.keys(v).sort().map(function (k) { return JSON.stringify(k) + ":" + stableKey(v[k]); }).join(",") + "}";
}
