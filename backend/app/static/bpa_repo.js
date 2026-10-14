/* BPA: archivio dei simboli nell'editor BPMN (bpa_editor.js).
 * - Nome gia' usato da un simbolo dello stesso tipo: «Riutilizza» (stesso simbolo) o «Rinomina».
 * - Rinominare un simbolo che compare in altri BPMN: si conferma vedendo dove cambiera' (cambia ovunque).
 * - Pannello delle proprieta' a destra: Attributi (modificabili), Occorrenze, Relazioni del simbolo selezionato.
 * - Copia/incolla anche tra schede (clipboard nel browser): a ogni incolla si sceglie se usare gli stessi simboli
 *   o crearne di nuovi. */
function initBpaRepo(c) {
  var modeler = c.modeler, T = c.T, ws = c.ws;
  var meta = null, map = {}, links = {}, suppress = 0, queue = Promise.resolve();
  var CLIP = "pm-bpa-clipboard";
  function fmt(s, p) { return String(s).replace(/\{(\w+)\}/g, function (_, k) { return p[k] !== undefined ? p[k] : "{" + k + "}"; }); }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined && text !== null) e.textContent = text; return e; }
  function q(p) { return new URLSearchParams(p).toString(); }
  function getJSON(url) {
    return fetch(url, { credentials: "same-origin" }).then(function (r) { return r.json().then(function (j) { if (!r.ok || j.error) throw new Error(j.error || r.status); return j; }); });
  }
  function postJSON(url, body) {
    return fetch(url, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok || j.error) throw new Error(j.error || r.status); return j; }); });
  }
  function norm(s) { return String(s || "").trim().replace(/\s+/g, " ").toLowerCase(); }
  function quiet(fn) { suppress++; try { return fn(); } finally { suppress--; } }

  // ---------- tipi, simboli degli elementi ----------
  function target(e) { return e && (e.labelTarget || e); }
  function typeOf(e) {
    e = target(e);
    if (!meta || !e || !e.businessObject || !e.businessObject.$type) return null;
    var t = e.businessObject.$type.split(":")[1] || "";
    return meta.tags[t.charAt(0).toLowerCase() + t.slice(1)] || null;
  }
  function nameOf(e) { return String((target(e).businessObject || {}).name || "").trim(); }
  function generic(name) { return !name || meta.generic.indexOf(norm(name)) >= 0; }
  function symbolOf(e) {
    e = target(e);
    var l = links[e.id];
    if (l && l.symbol) return l.symbol;
    if (l && l.copy_of) return null;
    var m = map[e.id];
    return m && m.type === typeOf(e) ? m.symbol : null;
  }
  function where(s) {
    var w = (s.models || []).map(function (m) { return m.process + " · " + m.label; });
    if (s.hidden_models) w.push(fmt(T.hiddenModels, { n: s.hidden_models }));
    return w.length ? w.join("; ") : T.noModels;
  }
  var reg = function () { return modeler.get("elementRegistry"); };

  function reload() {
    return Promise.all([
      meta ? Promise.resolve(meta) : getJSON("/analysis/bpa/meta?" + q({ workspace_id: ws })).then(function (j) { meta = j; return j; }),
      getJSON("/analysis/bpa/" + encodeURIComponent(c.modelId) + "/symbols?" + q({ workspace_id: ws }))
    ]).then(function (r) { map = r[1].elements || {}; refreshPanel(); }).catch(function () {});
  }

  // ---------- nomi: gia' usati / cambiano ovunque ----------
  function setLabel(e, name) { quiet(function () { modeler.get("modeling").updateLabel(target(e), name); }); }

  function askDuplicate(e, s) {
    return pmConfirm(fmt(T.dupAsk, { t: s.type_label, n: s.name, w: where(s) }),
                     { title: T.dupTitle, ok: T.reuse, cancel: T.rename, safe: true }).then(function (yes) {
      if (yes) {
        links[target(e).id] = { symbol: s.id };
        if (nameOf(e) !== s.name) setLabel(e, s.name);   // stesso simbolo: stesso nome (maiuscole comprese)
        c.changed();
        refreshPanel();
        return true;
      }
      setTimeout(function () { try { modeler.get("directEditing").activate(target(e)); } catch (err) {} }, 50);
      return false;
    });
  }
  function askRenameEverywhere(e, sid) {
    return getJSON("/analysis/bpa/symbols/" + encodeURIComponent(sid) + "?" + q({ workspace_id: ws, lang: c.lang(), model_id: c.modelId }))
      .then(function (d) {
        var others = d.occurrences.filter(function (o) { return !o.current; });
        if (!others.length && !d.hidden_occurrences) return true;
        var w = others.map(function (o) { return o.process + " · " + o.label; });
        if (d.hidden_occurrences) w.push(fmt(T.hiddenModels, { n: d.hidden_occurrences }));
        return pmConfirm(fmt(T.renameAsk, { n: nameOf(e), w: w.join("; ") }),
                         { title: T.renameTitle, ok: T.renameAll, cancel: T.cancel, safe: true }).then(function (yes) {
          if (!yes) { quiet(function () { modeler.get("commandStack").undo(); }); return false; }
          // le altre occorrenze dello stesso simbolo in questo BPMN prendono subito il nome nuovo
          var nm = nameOf(e);
          reg().filter(function (x) { return x !== e && !x.labelTarget && symbolOf(x) === sid && nameOf(x) !== nm; })
            .forEach(function (x) { setLabel(x, nm); });
          return true;
        });
      });
  }
  function checkName(e) {
    e = target(e);
    var type = typeOf(e), name = nameOf(e);
    if (!type || generic(name)) return Promise.resolve();
    var sid = symbolOf(e);
    return getJSON("/analysis/bpa/symbols/lookup?" + q({ workspace_id: ws, type: type, name: name, lang: c.lang(), exclude: sid || "" }))
      .then(function (j) {
        if (j.symbol) return askDuplicate(e, j.symbol);
        var m = map[e.id];
        if (sid && m && m.symbol === sid && m.elsewhere && !(links[e.id] && links[e.id].symbol)) return askRenameEverywhere(e, sid);
        if (sid) {   // stesso simbolo piu' volte in questo BPMN: stesso nome
          var nm = nameOf(e);
          reg().filter(function (x) { return x !== e && !x.labelTarget && symbolOf(x) === sid && nameOf(x) !== nm; })
            .forEach(function (x) { setLabel(x, nm); });
        }
      }).catch(function () {});
  }
  function enqueue(fn) { queue = queue.then(fn, fn); return queue; }
  modeler.on("commandStack.element.updateLabel.postExecuted", function (ev) {
    if (suppress || c.isLoading()) return;
    var e = ev.context.element;
    enqueue(function () { return checkName(e); }).then(refreshPanel);
  });

  // conflitti segnalati dal server al salvataggio (nomi gia' usati non decisi): stessa domanda, uno alla volta
  function resolveConflicts(list) {
    var all = true, first = null;
    var p = Promise.resolve();
    list.forEach(function (cf) {
      p = p.then(function () {
        var e = reg().get(cf.element_id);
        if (!e || !cf.existing) { all = false; return; }
        if (first) return;   // dopo un «Rinomina» si rinomina prima quello
        return askDuplicate(e, cf.existing).then(function (yes) { if (!yes) { all = false; first = e; } });
      });
    });
    return p.then(function () { return all; });
  }

  // ---------- pannello delle proprieta' ----------
  var panel = document.getElementById("bpa-panel"), current = null, tab = "attrs", detail = null;
  function openPanel(open) {
    var was = !panel.hidden;
    panel.hidden = !open;
    if (was !== open) setTimeout(function () { modeler.get("canvas").resized(); }, 0);
  }
  modeler.on("selection.changed", function (ev) {
    var s = ev.newSelection || [];
    var e = s.length === 1 ? target(s[0]) : null;
    if (!e || !typeOf(e)) { current = null; openPanel(false); return; }
    if (current !== e) detail = null;
    current = e;
    openPanel(true);
    refreshPanel();
  });
  document.getElementById("bpa-panel-close").addEventListener("click", function () {
    modeler.get("selection").select(null);
  });
  panel.querySelectorAll("[data-ptab]").forEach(function (b) {
    b.addEventListener("click", function () { tab = b.dataset.ptab; draw(); });
  });

  var loadSeq = 0;
  function refreshPanel() {
    if (!current || panel.hidden || !meta) return;
    if (!reg().get(current.id)) { current = null; openPanel(false); return; }
    var sid = symbolOf(current), seq = ++loadSeq;
    if (!sid) { detail = null; draw(); return; }
    getJSON("/analysis/bpa/symbols/" + encodeURIComponent(sid) + "?" + q({ workspace_id: ws, lang: c.lang(), model_id: c.modelId }))
      .then(function (d) { if (seq === loadSeq) { detail = d; draw(); } })
      .catch(function () { if (seq === loadSeq) { detail = null; draw(); } });
  }

  function draw() {
    var e = current; if (!e) return;
    var type = typeOf(e), name = nameOf(e), sid = symbolOf(e);
    panel.querySelectorAll("[data-ptab]").forEach(function (b) {
      b.classList.toggle("active", b.dataset.ptab === tab); b.setAttribute("aria-selected", b.dataset.ptab === tab ? "true" : "false");
    });
    document.getElementById("bpa-panel-type").textContent = meta.types[type] || "";
    document.getElementById("bpa-panel-name").textContent = name || T.noName;
    var body = document.getElementById("bpa-panel-body"); body.textContent = "";
    var counts = document.querySelectorAll("[data-pcount]");
    counts.forEach(function (x) {
      var n = !detail ? "" : x.dataset.pcount === "occ" ? detail.occurrences.reduce(function (a, o) { return a + o.elements.length; }, 0)
            : x.dataset.pcount === "rel" ? detail.relations.length : "";
      x.textContent = n === "" ? "" : String(n);
    });
    if (tab === "attrs") drawAttrs(body, e, type, name, sid);
    else if (tab === "occ") drawOcc(body, sid);
    else drawRel(body, sid);
  }

  function field(label, control, help, req) {
    var l = el("label", "bpa-field");
    var t = el("span", "bpa-field-label", label + (req ? " *" : "")); l.appendChild(t);
    l.appendChild(control);
    if (help) l.appendChild(el("span", "tiny muted", help));
    var st = el("span", "tiny bpa-field-status"); l.appendChild(st);
    return l;
  }
  function drawAttrs(body, e, type, name, sid) {
    // nome: cambia l'etichetta nel diagramma (passa dal controllo dei nomi gia' usati)
    var ni = el("input"); ni.type = "text"; ni.value = name; ni.maxLength = 500;
    ni.addEventListener("change", function () { modeler.get("modeling").updateLabel(e, ni.value.trim()); });
    body.appendChild(field(T.name + " (" + c.lang().toUpperCase() + ")", ni, detail && detail.symbol.name_other ? fmt(T.otherName, { l: c.lang() === "it" ? "EN" : "IT", n: detail.symbol.name_other }) : "", true));
    if (generic(name)) { body.appendChild(el("p", "tiny bpa-note", name ? T.genericName : T.needName)); }
    else if (!sid) { body.appendChild(el("p", "tiny bpa-note", T.notYetSaved)); }
    var defs = meta.attributes[type] || [];
    if (!defs.length) { body.appendChild(el("p", "tiny muted", T.noAttrs)); return; }
    var values = {};
    (detail && detail.attributes || []).forEach(function (a) { values[a.key] = a.value; });
    defs.forEach(function (d) {
      var v = values[d.key];
      if (d.translatable && v && typeof v === "object") v = v[c.lang()];
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
          d.options.forEach(function (o) { var op = el("option", "", o.label); op.value = o.value; if (o.value === v) op.selected = true; ctl.appendChild(op); });
          read = function () { return ctl.value; }; break;
        case "multi_choice":
          ctl = el("div", "bpa-multi");
          d.options.forEach(function (o) {
            var lab = el("label", "checkbox"), cb = el("input"); cb.type = "checkbox"; cb.value = o.value;
            cb.checked = Array.isArray(v) && v.indexOf(o.value) >= 0; lab.appendChild(cb); lab.appendChild(document.createTextNode(" " + o.label)); ctl.appendChild(lab);
          });
          read = function () { return Array.prototype.filter.call(ctl.querySelectorAll("input"), function (x) { return x.checked; }).map(function (x) { return x.value; }); };
          break;
        default:
          ctl = el("input"); ctl.type = d.type === "url" ? "url" : d.type === "email" ? "email" : "text"; ctl.value = v || ""; read = function () { return ctl.value; };
      }
      var f = field(d.name + (d.translatable ? " (" + c.lang().toUpperCase() + ")" : ""), ctl, d.help, d.required);
      if (!sid) Array.prototype.forEach.call(f.querySelectorAll("input,select,textarea"), function (x) { x.disabled = true; });
      var st = f.querySelector(".bpa-field-status");
      ctl.addEventListener("change", function () {
        var vals = {}; vals[d.key] = read();
        st.textContent = T.attrSaving; st.className = "tiny bpa-field-status";
        postJSON("/analysis/bpa/symbols/" + encodeURIComponent(sid) + "/attrs", { workspace_id: ws, lang: c.lang(), values: vals })
          .then(function (j) {
            if (j.errors && j.errors[d.key]) { st.textContent = j.errors[d.key]; st.className = "tiny bpa-field-status bad"; return; }
            st.textContent = "✓ " + T.attrSaved; st.className = "tiny bpa-field-status ok";
            if (detail) detail.attributes.forEach(function (a) { if (a.key === d.key) a.value = j.values[d.key]; });
          })
          .catch(function (err) { st.textContent = err.message; st.className = "tiny bpa-field-status bad"; });
      });
      body.appendChild(f);
    });
    if (sid && detail && detail.updated_by) body.appendChild(el("p", "tiny muted", fmt(T.lastChange, { u: detail.updated_by, d: window.pmLocalTime(detail.updated_at) })));
  }
  function drawOcc(body, sid) {
    if (!sid || !detail) { body.appendChild(el("p", "tiny muted", T.notYetSaved)); return; }
    var ul = el("ul", "bpa-plist");
    detail.occurrences.forEach(function (o) {
      var li = el("li", o.current ? "current" : "");
      var t = el("div", "bpa-pmain");
      if (o.current) t.appendChild(el("strong", "", T.thisModel));
      else { var a = el("a", "", o.label); a.href = o.url + "&focus=" + encodeURIComponent(o.elements[0]); a.target = "_blank"; a.rel = "noopener"; t.appendChild(a); }
      t.appendChild(el("span", "tiny muted", o.process + (o.elements.length > 1 ? " · " + fmt(T.timesIn, { n: o.elements.length }) : "")));
      li.appendChild(t);
      ul.appendChild(li);
    });
    body.appendChild(ul);
    if (detail.hidden_occurrences) body.appendChild(el("p", "tiny muted", fmt(T.hiddenModels, { n: detail.hidden_occurrences })));
  }
  function drawRel(body, sid) {
    if (!sid || !detail) { body.appendChild(el("p", "tiny muted", T.notYetSaved)); return; }
    if (!detail.relations.length) { body.appendChild(el("p", "tiny muted", T.noRelations)); return; }
    var groups = {};
    detail.relations.forEach(function (r) { (groups[r.label] = groups[r.label] || []).push(r); });
    Object.keys(groups).forEach(function (label) {
      body.appendChild(el("h4", "bpa-rel-h", label));
      var ul = el("ul", "bpa-plist");
      groups[label].forEach(function (r) {
        var li = el("li", r.current ? "current" : "");
        var t = el("div", "bpa-pmain");
        t.appendChild(el("span", "", r.other.name));
        t.appendChild(el("span", "tiny muted", r.other.type + " · " + (r.current ? T.thisModel : "")
          + r.models.filter(function (m) { return m.id !== c.modelId; }).map(function (m) { return (r.current ? ", " : "") + m.process + " · " + m.label; }).join(", ")));
        li.appendChild(t); ul.appendChild(li);
      });
      body.appendChild(ul);
    });
  }

  // ---------- copia / incolla (anche tra schede) ----------
  function copy() {
    var sel = modeler.get("selection").get().map(target).filter(function (e, i, a) { return a.indexOf(e) === i; });
    var shapes = sel.filter(function (e) {
      var t = e.businessObject && e.businessObject.$type;
      return e.waypoints === undefined && t && !/Participant|Lane$|Process|Collaboration/.test(t);
    });
    if (!shapes.length) { c.say(T.copyNone, "warn"); return; }
    var ids = shapes.map(function (s) { return s.id; });
    var items = shapes.map(function (s) {
      var bo = s.businessObject, ed = bo.eventDefinitions && bo.eventDefinitions[0], lc = bo.loopCharacteristics;
      return { id: s.id, type: bo.$type, name: bo.name || "", x: s.x, y: s.y, w: s.width, h: s.height,
               symbol: symbolOf(s), stype: typeOf(s), eventDef: ed ? ed.$type : null,
               loop: lc ? lc.$type : null, sequential: !!(lc && lc.isSequential),
               expanded: /SubProcess|Transaction/.test(bo.$type) ? !!(s.di && s.di.isExpanded) : undefined,
               attachedTo: bo.attachedToRef && ids.indexOf(s.host && s.host.id) >= 0 ? s.host.id : null,
               text: bo.$type === "bpmn:TextAnnotation" ? bo.text || "" : undefined };
    });
    var conns = reg().filter(function (e) {
      return e.waypoints && e.source && e.target && ids.indexOf(e.source.id) >= 0 && ids.indexOf(e.target.id) >= 0;
    }).map(function (e) { return { type: e.businessObject.$type, source: e.source.id, target: e.target.id, name: e.businessObject.name || "" }; });
    try { localStorage.setItem(CLIP, JSON.stringify({ at: Date.now(), lang: c.lang(), model: c.modelId, items: items, conns: conns })); }
    catch (err) { c.say(err.message, "bad"); return; }
    c.say(fmt(T.copied, { n: items.length }), "ok");
  }

  function parentAt(p) {
    var parts = reg().filter(function (e) { return e.businessObject && e.businessObject.$type === "bpmn:Participant"; });
    var hit = parts.filter(function (e) { return p.x >= e.x && p.x <= e.x + e.width && p.y >= e.y && p.y <= e.y + e.height; })[0];
    return hit || parts[0] || modeler.get("canvas").getRootElement();
  }
  function uniqueName(type, name) {
    var base = name.replace(/\s*\(\d+\)$/, ""), n = 2;
    function next() {
      var cand = base + " (" + n + ")";
      return getJSON("/analysis/bpa/symbols/lookup?" + q({ workspace_id: ws, type: type, name: cand, lang: c.lang() }))
        .then(function (j) {
          var local = reg().filter(function (e) { return typeOf(e) === type && norm(nameOf(e)) === norm(cand); }).length;
          if (!j.symbol && !local) return cand;
          n++; return n > 50 ? cand : next();
        });
    }
    return next();
  }
  function paste() {
    var clip;
    try { clip = JSON.parse(localStorage.getItem(CLIP) || "null"); } catch (err) { clip = null; }
    if (!clip || !clip.items || !clip.items.length) { c.say(T.pasteNone, "warn"); return; }
    var ef = modeler.get("elementFactory"), modeling = modeler.get("modeling"), bf = modeler.get("bpmnFactory"), canvas = modeler.get("canvas");
    var vb = canvas.viewbox(), center = { x: vb.x + vb.width / 2, y: vb.y + vb.height / 2 };
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    clip.items.forEach(function (i) { minX = Math.min(minX, i.x); minY = Math.min(minY, i.y); maxX = Math.max(maxX, i.x + i.w); maxY = Math.max(maxY, i.y + i.h); });
    var dx = center.x - (minX + maxX) / 2, dy = center.y - (minY + maxY) / 2;
    if (clip.model === c.modelId) { dx += 40; dy += 40; }
    var parent = parentAt(center), made = {}, created = [];
    quiet(function () {
      clip.items.slice().sort(function (a, b) { return (a.attachedTo ? 1 : 0) - (b.attachedTo ? 1 : 0); }).forEach(function (i) {
        var attrs = { type: i.type };
        if (i.eventDef) attrs.eventDefinitionType = i.eventDef;
        if (i.expanded !== undefined) attrs.isExpanded = i.expanded;
        if (i.expanded) { attrs.width = i.w; attrs.height = i.h; }
        var s;
        try { s = ef.createShape(attrs); } catch (err) { return; }
        if (i.name) s.businessObject.name = i.name;
        if (i.text !== undefined) s.businessObject.text = i.text;
        if (i.loop) { try { s.businessObject.loopCharacteristics = bf.create(i.loop, i.sequential ? { isSequential: true } : {}); } catch (err) {} }
        var pos = { x: i.x + i.w / 2 + dx, y: i.y + i.h / 2 + dy };
        try {
          if (i.attachedTo && made[i.attachedTo]) made[i.id] = modeling.createShape(s, pos, made[i.attachedTo], { attach: true });
          else made[i.id] = modeling.createShape(s, pos, parent);
          created.push({ item: i, shape: made[i.id] });
        } catch (err) {}
      });
      clip.conns.forEach(function (k) {
        if (!made[k.source] || !made[k.target]) return;
        try { var cn = modeling.connect(made[k.source], made[k.target], { type: k.type }); if (k.name) modeling.updateLabel(cn, k.name); } catch (err) {}
      });
    });
    if (!created.length) { c.say(T.pasteFailed, "bad"); return; }
    modeler.get("selection").select(created.map(function (x) { return x.shape; }));
    var withSym = created.filter(function (x) { return x.item.symbol && x.item.stype === typeOf(x.shape); });
    var others = created.filter(function (x) { return withSym.indexOf(x) < 0 && x.item.name; });
    var p = Promise.resolve();
    if (withSym.length) {
      p = pmConfirm(fmt(T.pasteAsk, { n: withSym.length }), { title: T.pasteTitle, ok: T.pasteSame, cancel: T.pasteNew, safe: true })
        .then(function (same) {
          var seq = Promise.resolve();
          withSym.forEach(function (x) {
            seq = seq.then(function () {
              if (same) {
                links[x.shape.id] = { symbol: x.item.symbol };
                if (clip.lang !== c.lang())   // incollato in un modello aperto nell'altra lingua: il nome del simbolo in questa
                  return getJSON("/analysis/bpa/symbols/" + encodeURIComponent(x.item.symbol) + "?" + q({ workspace_id: ws, lang: c.lang() }))
                    .then(function (d) { if (d.symbol.name) setLabel(x.shape, d.symbol.name); }).catch(function () {});
                return;
              }
              links[x.shape.id] = { copy_of: x.item.symbol };
              return uniqueName(x.item.stype, nameOf(x.shape)).then(function (nm) { setLabel(x.shape, nm); });
            });
          });
          return seq;
        });
    }
    // elementi con nome ma senza simbolo (non ancora salvati): il solito controllo dei nomi
    others.forEach(function (x) { p = p.then(function () { return checkName(x.shape); }); });
    p.then(function () { c.changed(); c.say(fmt(T.pasted, { n: created.length }), "ok"); refreshPanel(); });
  }
  try {
    modeler.get("keyboard").addListener(3000, function (ctx) {
      var e = ctx.keyEvent;
      if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
      var k = (e.key || "").toLowerCase();
      if (k === "c") { copy(); return true; }
      if (k === "v") { paste(); return true; }
    });
  } catch (err) {}
  document.getElementById("bpa-copy").addEventListener("click", copy);
  document.getElementById("bpa-paste").addEventListener("click", paste);

  // apertura da un'occorrenza: l'elemento indicato viene selezionato e messo al centro
  function focus(id) {
    var e = id && reg().get(id);
    if (!e) return;
    var canvas = modeler.get("canvas"), vb = canvas.viewbox();
    canvas.viewbox({ x: e.x + e.width / 2 - vb.width / 2, y: e.y + e.height / 2 - vb.height / 2, width: vb.width, height: vb.height });
    modeler.get("selection").select(e);
  }

  return {
    reload: reload, links: function () { return links; }, setLinks: function (l) { links = l || {}; },
    afterSave: function () { links = {}; return reload(); }, resolveConflicts: resolveConflicts, focus: focus,
    refresh: refreshPanel
  };
}
