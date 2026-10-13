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
      var add = el("button", "secondary pm-tool", "+ " + T.add); add.type = "button";
      add.addEventListener("click", function () { togglePanel("add"); });
      var views = el("button", "secondary pm-tool", T.views + " ▾"); views.type = "button";
      views.addEventListener("click", function () { togglePanel("views"); });
      tools.appendChild(add); tools.appendChild(views);
    }
    bar.appendChild(tools);
    if (filters.length) bar.appendChild(el("span", "tiny muted pm-filters-note", T.note));
    if (panel) bar.appendChild(panel);
  }

  // ---------- pannelli «+ Filtro» e «Viste» ----------
  var panel = null, panelKind = null, catalog = null;
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
    if (kind === "add") (catalog ? Promise.resolve(catalog) : call("/analysis/filters/catalog?" + q()).then(function (j) { catalog = j; return j; }))
      .then(addPanel).catch(function (e) { panel.textContent = e.message; });
    else call("/analysis/views?" + q()).then(viewsPanel).catch(function (e) { panel.textContent = e.message; });
  }
  function addPanel(cat) {
    if (panelKind !== "add") return;
    panel.textContent = "";
    var hubs = {}; (cat.types || []).forEach(function (t) { hubs[t.name] = t.hub; });
    var owners = [];
    (cat.attributes || []).forEach(function (a) { var k = a.kind === "event" ? "" : a.type; if (owners.indexOf(k) < 0) owners.push(k); });
    var row = el("div", "pm-panel-row");
    var who = el("select"); who.setAttribute("aria-label", T.what);
    owners.forEach(function (o) { var op = el("option", "", o ? plural(o) : T.events); op.value = o; who.appendChild(op); });
    var attr = el("select"); attr.setAttribute("aria-label", T.attribute);
    var search = el("input"); search.type = "search"; search.placeholder = T.search;
    row.appendChild(field(T.what, who)); row.appendChild(field(T.attribute, attr)); row.appendChild(field(T.searchLabel, search));
    panel.appendChild(row);
    var help = el("p", "tiny muted"); panel.appendChild(help);
    var list = el("div", "pm-values"); panel.appendChild(list);
    var chosen = [];
    function current() { var o = who.value; return (cat.attributes || []).filter(function (a) { return (a.kind === "event" ? "" : a.type) === o && a.attribute === attr.value; })[0]; }
    function fillAttrs() {
      attr.textContent = "";
      (cat.attributes || []).filter(function (a) { return (a.kind === "event" ? "" : a.type) === who.value; }).forEach(function (a) {
        var op = el("option", "", fmt(T.attrOption, { a: a.attribute, n: a.distinct.toLocaleString(LANG) })); op.value = a.attribute; attr.appendChild(op);
      });
      fillValues(true);
    }
    function fillValues(reset) {
      var a = current(); list.textContent = "";
      if (!a) return;
      if (reset) {
        var f = filters.filter(function (x) { return x.kind === a.kind && (x.type || "") === (a.type || "") && x.attribute === a.attribute; })[0];
        chosen = f ? f.values.slice() : [];
      }
      help.textContent = a.kind === "event" ? T.eventHelp : (hubs[a.type] ? fmt(T.hubHelp, { tp: plural(a.type) }) : fmt(T.objectHelp, { tp: plural(a.type) }));
      var term = search.value.trim().toLowerCase(), shown = 0;
      a.values.forEach(function (v) {
        if (term && String(v.value).toLowerCase().indexOf(term) < 0) return;
        if (shown >= 200) return; shown++;
        var lab = el("label", "pm-value"); var cb = el("input"); cb.type = "checkbox"; cb.checked = chosen.indexOf(v.value) >= 0;
        cb.addEventListener("change", function () { chosen = cb.checked ? chosen.concat([v.value]) : chosen.filter(function (x) { return x !== v.value; }); });
        lab.appendChild(cb); lab.appendChild(el("span", "", v.value)); lab.appendChild(el("span", "tiny muted", " " + v.count.toLocaleString(LANG)));
        list.appendChild(lab);
      });
      if (!shown) list.appendChild(el("p", "tiny muted", T.noValues));
    }
    who.addEventListener("change", fillAttrs); attr.addEventListener("change", function () { fillValues(true); });
    search.addEventListener("input", function () { fillValues(false); });
    var acts = el("div", "pm-panel-actions");
    var ok = el("button", "primary", T.apply); ok.type = "button";
    ok.addEventListener("click", function () {
      var a = current(); if (!a) return;
      var f = { kind: a.kind, attribute: a.attribute, values: chosen.slice() };
      if (a.kind !== "event") f.type = a.type;
      panel = null; panelKind = null; api_put(f);
    });
    var no = el("button", "secondary", T.cancel); no.type = "button"; no.addEventListener("click", closePanel);
    acts.appendChild(no); acts.appendChild(ok); panel.appendChild(acts);
    fillAttrs();
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
  function api_put(f) { api_obj.put(f); }

  function changed() { write(filters); render(); listeners.forEach(function (fn) { fn(filters.slice()); }); }

  var api_obj = null;
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
  api_obj = api;
  render();
  window.CASE_FILTERS = api;   // per l'assistente (static/assistant.js)
  return api;
}

/* Selezione su una serie di barre o colonne: tocco = aggiunge o toglie un valore, trascinamento = intervallo.
 * items: elementi DOM nell'ordine delle barre; isOn(i): selezionata?; onPick(indices, mode): mode = "toggle" | "range". */
function attachBarSelection(items, onPick) {
  var start = null, last = null, moved = false;
  function idxOf(target) { for (var i = 0; i < items.length; i++) if (items[i] === target || items[i].contains(target)) return i; return -1; }
  function mark(a, b) { items.forEach(function (it, i) { it.classList.toggle("pm-brush", a !== null && i >= Math.min(a, b) && i <= Math.max(a, b)); }); }
  items.forEach(function (it, i) {
    it.style.touchAction = "pan-y";
    it.addEventListener("pointerdown", function (e) { if (e.button) return; start = last = i; moved = false; mark(i, i); });
    it.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick([i], "toggle"); } });
  });
  var host = items.length ? items[0].parentNode : null;
  if (!host) return;
  host.addEventListener("pointermove", function (e) {
    if (start === null) return;
    var t = document.elementFromPoint(e.clientX, e.clientY), i = idxOf(t);
    if (i >= 0 && i !== last) { last = i; moved = moved || i !== start; mark(start, i); }
  });
  function end() {
    if (start === null) return;
    var a = Math.min(start, last), b = Math.max(start, last), list = [];
    for (var i = a; i <= b; i++) list.push(i);
    var mode = moved ? "range" : "toggle";
    start = last = null; mark(null, null);
    onPick(list, mode);
  }
  host.addEventListener("pointerup", end);
  host.addEventListener("pointerleave", function () { if (start !== null) end(); });
}

/* JSON con le chiavi in ordine (stesso testo di json.dumps(sort_keys=True, separators=(",", ":")) in Python):
 * serve a costruire la chiave che lega un filtro al grafico da cui parte. */
function stableKey(v) {
  if (v === null || typeof v !== "object") return JSON.stringify(v);
  if (Array.isArray(v)) return "[" + v.map(stableKey).join(",") + "]";
  return "{" + Object.keys(v).sort().map(function (k) { return JSON.stringify(k) + ":" + stableKey(v[k]); }).join(",") + "}";
}
