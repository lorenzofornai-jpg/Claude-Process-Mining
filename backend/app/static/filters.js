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
  function label(f) {
    if (f.kind === "start_month") return fmt(T.chipStart, { tp: plural(f.type), m: monthsText(f.values) });
    if (f.kind === "duration") return fmt(f.scope === "related" ? T.chipDurationRel : T.chipDuration, { tp: plural(f.type), r: rangesText(f.ranges) });
    if (f.kind === "event") return fmt(T.chipEvent, { a: f.attribute, v: f.values.join(", ") });
    return fmt(T.chipAttr, { tp: plural(f.type), a: f.attribute, v: f.values.join(", ") });
  }
  function same(a, b) { return a.kind === b.kind && (a.type || null) === (b.type || null) && (a.attribute || null) === (b.attribute || null) && (a.scope || null) === (b.scope || null); }

  function render() {
    if (!bar) return;
    bar.textContent = "";
    bar.hidden = !filters.length;
    if (!filters.length) return;
    bar.appendChild(el("span", "pm-filters-title", T.title));
    filters.forEach(function (f, i) {
      var chip = el("span", "pm-chip"); chip.appendChild(el("span", "pm-chip-text", label(f)));
      var x = el("button", "pm-chip-x", "✕"); x.type = "button"; x.title = T.remove; x.setAttribute("aria-label", T.remove + ": " + label(f));
      x.addEventListener("click", function () { filters.splice(i, 1); changed(); });
      chip.appendChild(x); bar.appendChild(chip);
    });
    var all = el("button", "link-btn pm-chip-clear", T.clear); all.type = "button";
    all.addEventListener("click", function () { filters = []; changed(); });
    bar.appendChild(all);
    bar.appendChild(el("span", "tiny muted pm-filters-note", T.note));
  }
  function changed() { write(filters); render(); listeners.forEach(function (fn) { fn(filters.slice()); }); }

  var api = {
    get: function () { return filters.slice(); },
    param: function () { return filters.length ? JSON.stringify(filters) : ""; },
    find: function (proto) { return filters.filter(function (f) { return same(f, proto); })[0] || null; },
    // sostituisce (o toglie, se values/ranges vuoti) il filtro dello stesso tipo
    put: function (f) {
      var empty = !(f.values && f.values.length) && !(f.ranges && f.ranges.length);
      var idx = -1; filters.forEach(function (x, i) { if (same(x, f)) idx = i; });
      if (empty) { if (idx >= 0) filters.splice(idx, 1); }
      else if (idx >= 0) filters[idx] = f; else filters.push(f);
      changed();
    },
    onChange: function (fn) { listeners.push(fn); },
    label: label
  };
  render();
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
