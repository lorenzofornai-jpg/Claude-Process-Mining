/* Pannello delle proprieta' di un simbolo dell'archivio BPMN: schede Attributi, Occorrenze, Relazioni.
 * Usato nell'editor (bpa_repo.js) e nell'Amministrazione (admin_bpa.html). */
var BpaPanel = (function () {
  function fmt(s, p) { return String(s).replace(/\{(\w+)\}/g, function (_, k) { return p[k] !== undefined ? p[k] : "{" + k + "}"; }); }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined && text !== null) e.textContent = text; return e; }

  function field(label, control, help, req) {
    var l = el("label", "bpa-field");
    l.appendChild(el("span", "bpa-field-label", label + (req ? " *" : "")));
    l.appendChild(control);
    if (help) l.appendChild(el("span", "tiny muted", help));
    l.appendChild(el("span", "tiny bpa-field-status"));
    return l;
  }

  /* defs: [{key, type, name, help, required, translatable, options:[{value,label}], value}]
   * o: {lang, T, postUrl (null = non modificabile), body: extra per la richiesta, onSaved(key, values)} */
  function attrs(body, defs, o) {
    defs.forEach(function (d) {
      var v = d.value;
      if (d.translatable && v && typeof v === "object") v = v[o.lang];
      var ctl, read;
      switch (d.type) {
        case "long_text": ctl = el("textarea"); ctl.rows = 4; ctl.value = v || ""; read = function () { return ctl.value; }; break;
        case "integer": case "number":
          ctl = el("input"); ctl.type = "number"; ctl.step = d.type === "integer" ? "1" : "any"; ctl.value = v === null || v === undefined ? "" : v;
          read = function () { return ctl.value; }; break;
        case "date": ctl = el("input"); ctl.type = "date"; ctl.value = v || ""; read = function () { return ctl.value; }; break;
        case "datetime": ctl = el("input"); ctl.type = "datetime-local"; ctl.value = v || ""; read = function () { return ctl.value; }; break;
        case "boolean": ctl = el("input"); ctl.type = "checkbox"; ctl.checked = !!v; read = function () { return ctl.checked; }; break;
        case "choice":
          ctl = el("select"); ctl.appendChild(el("option", "", "—")).value = "";
          d.options.forEach(function (op) { var x = el("option", "", op.label); x.value = op.value; if (op.value === v) x.selected = true; ctl.appendChild(x); });
          read = function () { return ctl.value; }; break;
        case "multi_choice":
          ctl = el("div", "bpa-multi");
          d.options.forEach(function (op) {
            var lab = el("label", "checkbox"), cb = el("input"); cb.type = "checkbox"; cb.value = op.value;
            cb.checked = Array.isArray(v) && v.indexOf(op.value) >= 0; lab.appendChild(cb); lab.appendChild(document.createTextNode(" " + op.label)); ctl.appendChild(lab);
          });
          read = function () { return Array.prototype.filter.call(ctl.querySelectorAll("input"), function (x) { return x.checked; }).map(function (x) { return x.value; }); };
          break;
        default:
          ctl = el("input"); ctl.type = d.type === "url" ? "url" : d.type === "email" ? "email" : "text"; ctl.value = v || ""; read = function () { return ctl.value; };
      }
      var f = field(d.name + (d.translatable ? " (" + o.lang.toUpperCase() + ")" : ""), ctl, d.help, d.required);
      if (!o.postUrl) Array.prototype.forEach.call(f.querySelectorAll("input,select,textarea"), function (x) { x.disabled = true; });
      var st = f.querySelector(".bpa-field-status");
      ctl.addEventListener("change", function () {
        if (!o.postUrl) return;
        var vals = {}; vals[d.key] = read();
        st.textContent = o.T.attrSaving; st.className = "tiny bpa-field-status";
        fetch(o.postUrl, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
                           body: JSON.stringify(Object.assign({ lang: o.lang, values: vals }, o.body || {})) })
          .then(function (r) { return r.json().then(function (j) { if (!r.ok || j.error) throw new Error(j.error || r.status); return j; }); })
          .then(function (j) {
            if (j.errors && j.errors[d.key]) { st.textContent = j.errors[d.key]; st.className = "tiny bpa-field-status bad"; return; }
            st.textContent = "✓ " + o.T.attrSaved; st.className = "tiny bpa-field-status ok";
            d.value = j.values[d.key];
            if (o.onSaved) o.onSaved(d.key, j.values);
          })
          .catch(function (err) { st.textContent = err.message; st.className = "tiny bpa-field-status bad"; });
      });
      body.appendChild(f);
    });
  }

  /* o: {T, links: true = link ai BPMN (si aprono in una nuova scheda)} */
  function occurrences(body, detail, o) {
    var ul = el("ul", "bpa-plist");
    detail.occurrences.forEach(function (oc) {
      var li = el("li", oc.current ? "current" : "");
      var t = el("div", "bpa-pmain");
      if (oc.current) t.appendChild(el("strong", "", o.T.thisModel));
      else if (o.links && oc.url) { var a = el("a", "", oc.label); a.href = oc.url + "&focus=" + encodeURIComponent(oc.elements[0]); a.target = "_blank"; a.rel = "noopener"; t.appendChild(a); }
      else t.appendChild(el("strong", "", oc.label));
      t.appendChild(el("span", "tiny muted", oc.process + (oc.elements.length > 1 ? " · " + fmt(o.T.timesIn, { n: oc.elements.length }) : "")));
      li.appendChild(t);
      ul.appendChild(li);
    });
    if (!detail.occurrences.length) body.appendChild(el("p", "tiny muted", o.T.noOccurrences));
    else body.appendChild(ul);
    if (detail.hidden_occurrences) body.appendChild(el("p", "tiny muted", fmt(o.T.hiddenModels, { n: detail.hidden_occurrences })));
  }

  function relations(body, detail, o) {
    if (!detail.relations.length) { body.appendChild(el("p", "tiny muted", o.T.noRelations)); return; }
    var groups = {};
    detail.relations.forEach(function (r) { (groups[r.label] = groups[r.label] || []).push(r); });
    Object.keys(groups).forEach(function (label) {
      body.appendChild(el("h4", "bpa-rel-h", label));
      var ul = el("ul", "bpa-plist");
      groups[label].forEach(function (r) {
        var li = el("li", r.current ? "current" : "");
        var t = el("div", "bpa-pmain");
        t.appendChild(el("span", "", r.other.name));
        var where = r.models.filter(function (m) { return m.id !== o.modelId; }).map(function (m) { return m.process + " · " + m.label; });
        if (r.current) where.unshift(o.T.thisModel);
        t.appendChild(el("span", "tiny muted", r.other.type + (where.length ? " · " + where.join(", ") : "")));
        li.appendChild(t); ul.appendChild(li);
      });
      body.appendChild(ul);
    });
  }

  return { attrs: attrs, occurrences: occurrences, relations: relations, field: field, el: el, fmt: fmt };
})();
