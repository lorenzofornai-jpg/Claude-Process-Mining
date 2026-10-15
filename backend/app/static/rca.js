/* Root Cause Analysis: terzo livello della Control Tower (services/rca.py, rotta /analysis/control-tower/rca).
   initRca(h) riceve dalla pagina le utilita' (h.el, h.button, h.fmt, h.tx, h.tp, h.value, h.num, h.pct, h.days,
   h.api, h.kpiById, h.values(), h.filters(), h.filterLabels(), h.period(), h.go, h.T) e ritorna
   {view(section, kpiId)}: l'elemento da mostrare. Ogni RCA e' salvata con la sua impronta: riaprirla non ricalcola. */
function initRca(h) {
  var T = h.T, el = h.el, fmt = h.fmt;

  var shown = null;   // l'analisi mostrata, per l'assistente

  function view(sec, kpiId) {
    shown = null;
    var wrap = el("div", "rca");
    var nav = el("div", "ct-crumbs");
    nav.appendChild(h.button("← " + T.back, "secondary", function () { h.go(sec.id); }));
    if (h.assistant) nav.appendChild(h.button("✦ " + T.assistant, "secondary", function () { h.assistant(); }));
    wrap.appendChild(nav);
    var head = el("div", "rca-head");
    head.appendChild(el("p", "eyebrow", T.title));
    head.appendChild(el("h2", "ct-h ct-h-big", h.tx(sec.question)));
    head.appendChild(el("p", "tiny muted", T.intro));
    wrap.appendChild(head);

    // il sintomo: uno dei KPI della domanda
    var pick = el("div", "rca-kpis");
    pick.appendChild(el("span", "ct-field-label", T.symptom));
    var list = el("div", "rca-kpi-list");
    sec.kpis.forEach(function (id) {
      var k = h.kpiById(id); if (!k) return;
      var r = (h.values().kpis || {})[id] || {};
      var b = h.button("", "rca-kpi" + (id === kpiId ? " on" : "") + (r.status ? " ct-" + r.status : ""), function () { h.go(sec.id, id); });
      b.setAttribute("aria-pressed", id === kpiId ? "true" : "false");
      b.appendChild(el("span", "rca-kpi-name", h.tx(k.name)));
      b.appendChild(el("strong", "rca-kpi-val", r.issues ? "—" : h.value(k, r.value, r.currency)));
      b.disabled = !!r.issues;
      list.appendChild(b);
    });
    pick.appendChild(list);
    wrap.appendChild(pick);
    var labels = h.filterLabels();
    wrap.appendChild(el("p", "tiny muted rca-scope", labels.length ? fmt(T.scopeFiltered, { f: labels.join(" · ") }) : T.scopeAll));

    var body = el("div", "rca-body");
    wrap.appendChild(body);
    var savedBox = el("section", "rca-saved");
    wrap.appendChild(savedBox);
    var k = h.kpiById(kpiId);
    if (!k) { body.appendChild(el("p", "muted", T.chooseKpi)); savedList(savedBox, sec, null); return wrap; }
    body.appendChild(el("p", "muted", T.loading));
    request(false).then(function (j) { show(body, k, j); }).catch(function (e) { fail(body, e); savedList(savedBox, sec, null); });
    return wrap;

    function request(run) {
      return h.api("/analysis/control-tower/rca", {
        workspace_id: h.ws, config_id: h.config, kpi_id: kpiId, section_id: sec.id, period: h.period(),
        filters: h.filters(), filter_labels: labels, run: run
      });
    }
    function start(box, k) {
      box.textContent = "";
      box.appendChild(el("p", "muted rca-working", T.working));
      request(true).then(function (j) { show(box, k, j); }).catch(function (e) { fail(box, e); });
    }
    function show(box, k, j) {
      box.textContent = "";
      var a = j.analysis;
      savedList(savedBox, sec, a && !a.stale ? a.id : null);
      if (!a || a.stale) {
        var card = el("div", "rca-start");
        if (a && a.stale) card.appendChild(el("p", "rca-stale", fmt(T.stale, { v: a.version, d: when(a.created_at) })));
        else card.appendChild(el("p", "", T.notYet));
        var est = j.llm && j.estimate ? fmt(T.cost, { c: usd(j.estimate.cost_usd), m: usd(j.estimate.max_usd) }) : T.noLlm;
        card.appendChild(el("p", "tiny muted", est));
        card.appendChild(h.button(a && a.stale ? T.update : T.run, "primary", function () { start(box, k); }));
        box.appendChild(card);
        if (!a) return;
      }
      shown = { kpi: k, analysis: a };
      if (h.onShown) h.onShown();
      box.appendChild(result(k, a));
    }
    function fail(box, e) { box.textContent = ""; box.appendChild(el("p", "as-error", e.message || String(e))); }
  }

  // ---------- RCA salvate della domanda: si riaprono con i loro filtri e il loro periodo ----------
  function savedList(box, sec, currentId) {
    h.api("/analysis/control-tower/rca/list", { workspace_id: h.ws, config_id: h.config }).then(function (j) {
      box.textContent = "";
      var rows = (j.analyses || []).filter(function (r) { return r.section_id === sec.id || sec.kpis.indexOf(r.kpi_id) >= 0; });
      var det = el("details", "rca-saved-box"); det.open = rows.length > 0 && rows.length <= 6;
      det.appendChild(el("summary", "", fmt(T.savedTitle, { n: rows.length })));
      if (!rows.length) { det.appendChild(el("p", "tiny muted", T.savedNone)); box.appendChild(det); return; }
      det.appendChild(el("p", "tiny muted", T.savedHint));
      var ul = el("ul", "rca-saved-list");
      rows.forEach(function (r) {
        var k = h.kpiById(r.kpi_id);
        var li = el("li", "rca-saved-row" + (r.id === currentId ? " on" : ""));
        var main = el("div", "rca-saved-main");
        main.appendChild(el("strong", "", k ? h.tx(k.name) : r.kpi_id));
        var fl = r.filter_labels.length ? r.filter_labels.join(" · ") : T.savedNoFilters, per = months(r.months);
        var scope = fl.indexOf(per) >= 0 ? fl : fl + " · " + per;   // il periodo e' gia' nei filtri (mese di inizio)
        main.appendChild(el("span", "tiny", scope));
        var res = r.status === "ok" ? fmt(r.causes === 1 ? T.savedCause1 : T.savedCauses, { n: r.causes }) +
                    (r.top ? ": " + (h.tx(r.top.title) || factor(r.top.parts || [])) : "")
                : r.status === "insufficient" ? T.insufficient : T.noCauses;
        main.appendChild(el("span", "tiny rca-saved-res" + (r.status === "ok" ? " ok" : ""), res));
        main.appendChild(el("span", "tiny muted", fmt(T.savedBy, { u: r.created_by || "—", d: when(r.created_at), v: r.version }) +
                                                (r.stale ? " · " + T.savedStale : "")));
        li.appendChild(main);
        var acts = el("div", "rca-saved-acts");
        if (r.id === currentId) acts.appendChild(el("span", "tiny muted", T.savedShown));
        else {
          var canOpen = (r.filters !== null && r.filters !== undefined) || !r.filter_labels.length;
          var ob = h.button(T.savedOpen, "secondary", function () { h.open(sec.id, r.kpi_id, r.filters || []); });
          if (!canOpen) { ob.disabled = true; ob.title = T.savedNoRestore; }
          acts.appendChild(ob);
        }
        acts.appendChild(h.button(T.savedDelete, "secondary", function () {
          pmConfirm(T.savedDeleteQ, { ok: T.savedDelete }).then(function (yes) {
            if (!yes) return;
            h.api("/analysis/control-tower/rca/delete", { workspace_id: h.ws, config_id: h.config, id: r.id })
              .then(function () { if (r.id === currentId) h.go(sec.id, r.kpi_id); else savedList(box, sec, currentId); })
              .catch(function (e) { alert(e.message || e); });
          });
        }));
        li.appendChild(acts);
        ul.appendChild(li);
      });
      det.appendChild(ul);
      box.appendChild(det);
    }).catch(function () { box.textContent = ""; });
  }
  function months(m) {
    if (!m || !m.length) return T.savedAllPeriod;
    return m.length === 1 ? h.monthLabel(m[0]) : h.monthLabel(m[0]) + " – " + h.monthLabel(m[m.length - 1]);
  }

  function when(iso) { if (!iso) return "—"; try { return new Date(iso).toLocaleString(h.lang, { dateStyle: "medium", timeStyle: "short" }); } catch (e) { return iso; } }
  function usd(x) { return Number(x || 0).toLocaleString(h.lang, { style: "currency", currency: "USD", minimumFractionDigits: 3, maximumFractionDigits: 3 }); }
  function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }
  function typeName(t) { return cap(h.tp(t, 1)); }
  function n1(x) { return h.num(x, 1); }

  // ---------- il fattore in parole ----------
  function partLabel(p) {
    var where = p.attr ? (p.src === "rel" ? fmt(T.relAttr, { a: p.attr, t: h.tp(p.type, 1) }) : p.attr) : typeName(p.type);
    switch (p.k) {
      case "value": return fmt(T.pValue, { w: where, v: p.value });
      case "range":
        if (p.lo === null || p.lo === undefined) return fmt(T.pUpTo, { w: where, b: n1(p.hi) });
        if (p.hi === null || p.hi === undefined) return fmt(T.pOver, { w: where, a: n1(p.lo) });
        return fmt(T.pRange, { w: where, a: n1(p.lo), b: n1(p.hi) });
      case "empty": return fmt(T.pEmpty, { w: where });
      case "object": return fmt(T.pObject, { t: typeName(p.type), v: p.value });
      case "activity": return fmt(p.change ? T.pChange : T.pActivity, { a: p.activity });
      case "repeated": return fmt(T.pRepeated, { a: p.activity });
      case "manual": return fmt(T.pManual, { a: p.activity });
      case "weekday":
        var d = new Date(Date.UTC(2024, 0, 1 + (p.value || 0)));   // 1 gennaio 2024 = lunedi'
        return fmt(T.pWeekday, { d: d.toLocaleDateString(h.lang, { weekday: "long", timeZone: "UTC" }) });
      case "quarter_end": return T.pQuarterEnd;
      case "workload": return T.pWorkload;
    }
    return "";
  }
  function factor(parts) { return parts.map(partLabel).join(" + "); }

  // ---------- risultato ----------
  function result(k, a) {
    var r = a.result, box = el("div", "rca-result");
    var binary = r.symptom && r.symptom.kind === "binary";
    var pop = r.population || {};
    box.appendChild(el("p", "tiny muted", binary
      ? fmt(T.popBinary, { b: h.num(pop.bad || 0), n: h.num(pop.n || 0), p: h.pct(pop.rate || 0), tp: h.tp(r.lead, pop.n) })
      : fmt(T.popDuration, { n: h.num(pop.n || 0), tp: h.tp(r.lead, pop.n), m: h.days(pop.median || 0) })));
    if (r.status !== "ok") {
      var ban = el("div", "rca-none");
      ban.appendChild(el("strong", "", r.status === "insufficient" ? T.insufficient : T.noCauses));
      if (r.notes && r.notes.length) ban.appendChild(el("p", "", r.notes[0]));
      box.appendChild(ban);
    }
    if (r.segment) box.appendChild(segment(k, r));
    if (r.findings && r.findings.length) {
      box.appendChild(el("h3", "rca-h", T.causes));
      var ol = el("ol", "rca-findings");
      r.findings.forEach(function (f) { ol.appendChild(finding(f, binary, r)); });
      box.appendChild(ol);
    }
    if (r.dismissed && r.dismissed.length) {
      var det = el("details", "rca-dismissed");
      det.appendChild(el("summary", "tiny", fmt(T.dismissed, { n: r.dismissed.length })));
      r.dismissed.forEach(function (f) {
        var p = el("p", "tiny"); p.appendChild(el("strong", "", h.tx(f.title) || factor(f.parts)));
        if (h.tx(f.explanation)) p.appendChild(document.createTextNode(" — " + h.tx(f.explanation)));
        det.appendChild(p);
      });
      box.appendChild(det);
    }
    var rest = (r.notes || []).slice(r.status !== "ok" ? 1 : 0);
    if (rest.length) { var ul = el("ul", "tiny muted rca-notes"); rest.forEach(function (n) { ul.appendChild(el("li", "", n)); }); box.appendChild(ul); }
    var rj = r.rejected || {};
    if (rj.candidates) box.appendChild(el("p", "tiny muted", fmt(T.examined, {
      c: rj.candidates, e: rj.effect || 0, s: rj.significance || 0, u: rj.stability || 0, f: rj.confounded || 0, d: rj.duplicate || 0 })));
    var foot = fmt(T.savedBy, { u: a.created_by || "—", d: when(a.created_at), v: a.version });
    if (a.cost_usd) foot += " · " + fmt(T.spent, { c: usd(a.cost_usd) });
    box.appendChild(el("p", "tiny muted rca-foot", foot));
    return box;
  }

  function finding(f, binary, r) {
    var li = el("li", "rca-finding");
    var head = el("div", "rca-fhead");
    head.appendChild(el("strong", "rca-ftitle", h.tx(f.title) || factor(f.parts)));
    var chips = el("span", "rca-chips");
    chips.appendChild(el("span", "rca-chip fam-" + f.family, T["fam_" + f.family] || f.family));
    chips.appendChild(el("span", "rca-chip conf-" + f.confidence, f.confidence === "high" ? T.confHigh : T.confMedium));
    if (f.verdict === "uncertain") chips.appendChild(el("span", "rca-chip", T.uncertain));
    head.appendChild(chips);
    li.appendChild(head);
    if (h.tx(f.title)) li.appendChild(el("p", "tiny muted rca-factor", factor(f.parts)));
    var nums = el("div", "rca-nums");
    function stat(label, val) { var d = el("div", "rca-stat"); d.appendChild(el("span", "tiny muted", label)); d.appendChild(el("strong", "", val)); nums.appendChild(d); }
    if (binary) {
      stat(T.sHere, h.pct(f.m_g));
      stat(T.sElsewhere, h.pct(f.m_r));
      if (f.lift) stat(T.sTimes, fmt(T.times, { n: n1(f.lift) }));
    } else {
      stat(T.sHereMed, h.days(Math.abs(f.med_g)));
      stat(T.sElsewhereMed, h.days(Math.abs(f.med_r)));
    }
    stat(T.sCases, fmt(T.casesShare, { n: h.num(f.n), p: h.pct(f.share_pop, 0) }));
    if (f.excess_share) stat(binary ? T.sExplainsCases : T.sExplainsDays, h.pct(f.excess_share, 0));
    li.appendChild(nums);
    if (h.tx(f.explanation)) li.appendChild(el("p", "rca-expl", h.tx(f.explanation)));
    if (h.tx(f.check)) { var c = el("p", "tiny rca-check"); c.appendChild(el("strong", "", T.check + ": ")); c.appendChild(document.createTextNode(h.tx(f.check))); li.appendChild(c); }
    var manual = f.parts.filter(function (p) { return p.k === "manual"; })[0];
    if (manual) li.appendChild(el("p", "tiny muted", fmt(manual.basis === "time" ? T.howManualTime : T.howManualUser, { p: h.pct(manual.auto_share || 0, 0), a: manual.activity })));
    if (f.same_cases && f.same_cases.length) li.appendChild(el("p", "tiny muted", fmt(T.sameCases, { x: f.same_cases.map(factor).join("; ") })));
    return li;
  }

  function segment(k, r) {
    var s = r.segment, binary = r.symptom.kind === "binary", box = el("div", "rca-segment");
    box.appendChild(el("h3", "rca-h", T.segTitle));
    var show = function (x) { return binary ? h.pct(x) : h.days(Math.abs(x)); };
    var line = fmt(T.segLine, { a: show(s.m_seg), b: show(s.m_rest), n: h.num(s.n_seg), m: h.num(s.n_rest) });
    box.appendChild(el("p", "", line));
    if (!s.worse) { box.appendChild(el("p", "tiny muted", T.segNotWorse)); return box; }
    if (s.mix) {
      var what = s.mix.attr ? fmt(T.relAttr, { a: s.mix.attr, t: h.tp(s.mix.type, 1) }) : typeName(s.mix.type);
      box.appendChild(el("p", "", fmt(T.segMix, { p: h.pct(Math.min(1, Math.max(0, s.mix.mix_share)), 0), w: what })));
      var ul = el("ul", "rca-mix");
      (s.mix.top || []).forEach(function (c) {
        ul.appendChild(el("li", "", fmt(T.segMixRow, { v: c.value === null ? T.other : c.value, a: h.pct(c.w_seg, 0), b: h.pct(c.w_rest, 0), m: show(c.m_rest) })));
      });
      box.appendChild(ul);
      var perf = s.mix.perf;
      box.appendChild(el("p", "tiny muted", perf > 0 ? fmt(T.segPerf, { x: binary ? fmt(T.pp, { n: n1(perf * 100) }) : h.days(perf) }) : T.segPerfNone));
    } else box.appendChild(el("p", "tiny muted", T.segNoMix));
    return box;
  }

  // la RCA che l'utente sta guardando, compatta, nella lingua attiva (contesto dell'assistente)
  function context() {
    if (!shown) return null;
    var a = shown.analysis, r = a.result || {}, binary = r.symptom && r.symptom.kind === "binary";
    var rd = function (x) { return typeof x === "number" ? Math.round(x * 1000) / 1000 : x; };
    function f(x) {
      var o = { factor: factor(x.parts), family: x.family, confidence: x.confidence, cases: x.n, share_of_cases: rd(x.share_pop),
                explains_share: rd(x.excess_share), verdict: x.verdict || null,
                title: h.tx(x.title) || null, explanation: h.tx(x.explanation) || null, check: h.tx(x.check) || null };
      if (binary) { o.symptom_rate_here = rd(x.m_g); o.symptom_rate_others = rd(x.m_r); o.times = rd(x.lift); }
      else { o.median_days_here = rd(Math.abs(x.med_g)); o.median_days_others = rd(Math.abs(x.med_r)); }
      if (x.same_cases && x.same_cases.length) o.same_cases_as = x.same_cases.map(factor);
      return o;
    }
    return {
      kpi_id: shown.kpi.id, symptom: r.symptom, status: r.status, population: r.population,
      causes: (r.findings || []).map(f), dismissed_as_consequences: (r.dismissed || []).map(f),
      segment_vs_rest: r.segment || null, notes: r.notes || [], rejected_counts: r.rejected || {},
      behaviour_timing: r.timing || null, saved_at: a.created_at, saved_by: a.created_by,
      dataset_version: a.version, stale: !!a.stale, case_filters: a.filter_labels || []
    };
  }

  return { view: view, context: context };
}
