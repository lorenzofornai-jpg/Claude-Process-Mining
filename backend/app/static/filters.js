/* Filtri per attributo dell'analisi, comuni a Process Explorer e Process Overview (markup in _filters.html).
   initFilters({ws, config, T, lang, onChange}) carica gli attributi del dataset (/analysis/attributes), mostra i
   filtri attivi come etichette rimovibili e permette di aggiungerne uno: attributo (di un tipo di oggetto o degli
   eventi), ricerca tra i valori, scelta di uno o piu' valori. I filtri restano nella scheda del browser per
   dataset, cosi' passando da Explorer a Overview valgono gli stessi. onChange(filters) ricalcola la pagina.
   Ritorna {get(), query()}: i filtri attivi e il parametro da aggiungere alle richieste. */
function initFilters(opts) {
  var T = opts.T, key = "pm-filters:" + opts.config;
  var box = document.getElementById("flt"), chips = document.getElementById("flt-chips");
  var attrSel = document.getElementById("flt-attr"), search = document.getElementById("flt-search");
  var list = document.getElementById("flt-values"), apply = document.getElementById("flt-apply");
  var hint = document.getElementById("flt-hint"), adder = document.getElementById("flt-add");
  var filters = [], catalog = [], chosen = {};
  try { filters = JSON.parse(window.sessionStorage.getItem(key) || "[]") || []; } catch (e) { filters = []; }
  function fmt(s, p) { return String(s).replace(/\{(\w+)\}/g, function (_, k) { return p[k] !== undefined ? p[k] : "{" + k + "}"; }); }
  function el(tag, cls, t) { var e = document.createElement(tag); if (cls) e.className = cls; if (t !== undefined) e.textContent = t; return e; }
  function num(n) { return Number(n).toLocaleString(opts.lang); }
  function save() { try { window.sessionStorage.setItem(key, JSON.stringify(filters)); } catch (e) {} }
  function label(f) { return (f.kind === "event" ? T.events : f.type) + " · " + f.attribute; }
  function same(a, b) { return a.kind === b.kind && (a.type || "") === (b.type || "") && a.attribute === b.attribute; }

  function renderChips() {
    chips.textContent = "";
    box.classList.toggle("flt-on", filters.length > 0);
    if (!filters.length) { chips.appendChild(el("span", "tiny muted", T.none)); return; }
    filters.forEach(function (f, i) {
      var c = el("span", "flt-chip");
      var shown = f.values.slice(0, 3).join(", ") + (f.values.length > 3 ? " " + fmt(T.more, { n: f.values.length - 3 }) : "");
      c.appendChild(el("span", "", label(f) + ": " + shown));
      var x = el("button", "flt-x", "×"); x.type = "button"; x.setAttribute("aria-label", T.remove);
      x.addEventListener("click", function () { filters.splice(i, 1); save(); renderChips(); opts.onChange(filters); });
      c.appendChild(x); chips.appendChild(c);
    });
    var all = el("button", "link-button tiny", T.clear); all.type = "button";
    all.addEventListener("click", function () { filters = []; save(); renderChips(); opts.onChange(filters); });
    chips.appendChild(all);
  }
  function current() { return catalog[Number(attrSel.value)]; }
  function renderValues() {
    var a = current(); list.textContent = "";
    if (!a) return;
    var q = (search.value || "").trim().toLowerCase();
    var hits = a.values.filter(function (v) { return !q || String(v.value).toLowerCase().indexOf(q) >= 0; });
    hits.slice(0, 60).forEach(function (v) {
      var li = el("li"), lab = el("label"), cb = el("input");
      cb.type = "checkbox"; cb.checked = !!chosen[v.value];
      cb.addEventListener("change", function () { if (cb.checked) chosen[v.value] = true; else delete chosen[v.value]; renderHint(); });
      lab.appendChild(cb); lab.appendChild(el("span", "flt-v", v.value)); lab.appendChild(el("span", "tiny muted", num(v.count)));
      li.appendChild(lab); list.appendChild(li);
    });
    if (hits.length > 60) list.appendChild(el("li", "tiny muted", fmt(T.narrow, { n: num(hits.length) })));
    if (!hits.length) list.appendChild(el("li", "tiny muted", T.noMatch));
    renderHint();
  }
  function renderHint() {
    var a = current(), n = Object.keys(chosen).length;
    apply.disabled = !n;
    hint.textContent = a ? fmt(a.kind === "event" ? T.hintEvent : T.hintObject, { t: a.type || "", a: a.attribute, d: num(a.distinct) })
                           + (n ? " · " + (n === 1 ? T.chosen1 : fmt(T.chosen, { n: n })) : "") : "";
  }
  attrSel.addEventListener("change", function () { chosen = {}; search.value = ""; renderValues(); });
  search.addEventListener("input", renderValues);
  apply.addEventListener("click", function () {
    var a = current(), values = Object.keys(chosen);
    if (!a || !values.length) return;
    var f = { kind: a.kind, type: a.type, attribute: a.attribute, values: values };
    var old = filters.filter(function (x) { return same(x, f); })[0];
    if (old) old.values = Array.from(new Set(old.values.concat(values))); else filters.push(f);
    chosen = {}; search.value = ""; save(); renderChips(); renderValues(); adder.open = false; opts.onChange(filters);
  });
  fetch("/analysis/attributes?" + new URLSearchParams({ workspace_id: opts.ws, config_id: opts.config }), { credentials: "same-origin" })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      catalog = j.attributes || [];
      attrSel.textContent = "";
      if (!catalog.length) { adder.hidden = true; return; }
      catalog.forEach(function (a, i) {
        var o = el("option", "", (a.kind === "event" ? T.events : a.type) + " · " + a.attribute + " (" + num(a.distinct) + ")");
        o.value = i; attrSel.appendChild(o);
      });
      // filtri salvati che non esistono piu' in questo dataset: via
      var known = filters.filter(function (f) { return catalog.some(function (a) { return same(a, f); }); });
      if (known.length !== filters.length) { filters = known; save(); renderChips(); opts.onChange(filters); }
      renderValues();
    }).catch(function () { adder.hidden = true; });
  renderChips();
  return {
    get: function () { return filters; },
    query: function () { return filters.length ? JSON.stringify(filters) : ""; }
  };
}
