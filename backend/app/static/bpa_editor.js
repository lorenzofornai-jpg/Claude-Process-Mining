/* Editor BPMN 2.0 della BPA (bpmn-js, https://bpmn.io): apre il modello di una variante nella lingua scelta, lo
 * salva (l'altra lingua si aggiorna sul server con la stessa struttura e i nomi tradotti), scarica .bpmn e .svg.
 * Il logo bpmn.io in basso a destra resta visibile (condizione della licenza di bpmn-js). */

// testi dei menu dell'editor in italiano (le chiavi sono quelli originali in inglese di bpmn-js)
var BPMN_IT = {
  "Activate create/remove space tool": "Strumento spazio (crea/togli spazio)",
  "Activate global connect tool": "Strumento collegamento",
  "Activate hand tool": "Strumento mano (sposta la vista)",
  "Activate lasso tool": "Strumento lazo (selezione)",
  "Add lane above": "Aggiungi una lane sopra",
  "Add lane below": "Aggiungi una lane sotto",
  "Add text annotation": "Aggiungi un'annotazione",
  "Align elements": "Allinea gli elementi",
  "Align elements left": "Allinea a sinistra", "Align elements right": "Allinea a destra",
  "Align elements center": "Allinea al centro", "Align elements top": "Allinea in alto",
  "Align elements bottom": "Allinea in basso", "Align elements middle": "Allinea al centro (verticale)",
  "Append compensation activity": "Aggiungi un'attività di compensazione",
  "Append conditional intermediate catch event": "Aggiungi un evento intermedio condizionale",
  "Append end event": "Aggiungi un evento di fine",
  "Append gateway": "Aggiungi un gateway",
  "Append intermediate/boundary event": "Aggiungi un evento intermedio/di confine",
  "Append message intermediate catch event": "Aggiungi un evento intermedio di messaggio",
  "Append receive task": "Aggiungi un task di ricezione",
  "Append signal intermediate catch event": "Aggiungi un evento intermedio di segnale",
  "Append task": "Aggiungi un task",
  "Append timer intermediate catch event": "Aggiungi un evento intermedio timer",
  "Change element": "Cambia tipo di elemento",
  "Collection": "Raccolta",
  "Connect to other element": "Collega a un altro elemento",
  "Connect using association": "Collega con un'associazione",
  "Connect using data input association": "Collega con un'associazione di dati",
  "Create data object reference": "Crea un oggetto dati",
  "Create data store reference": "Crea un archivio dati",
  "Create end event": "Crea un evento di fine",
  "Create expanded sub-process": "Crea un sottoprocesso espanso",
  "Create gateway": "Crea un gateway",
  "Create group": "Crea un gruppo",
  "Create intermediate/boundary event": "Crea un evento intermedio/di confine",
  "Create pool/participant": "Crea una pool/partecipante",
  "Create start event": "Crea un evento di inizio",
  "Create task": "Crea un task",
  "Delete": "Elimina",
  "Distribute elements horizontally": "Distribuisci in orizzontale",
  "Distribute elements vertically": "Distribuisci in verticale",
  "Divide into three lanes": "Dividi in tre lane",
  "Divide into two lanes": "Dividi in due lane",
  "Loop": "Ciclo",
  "Open {element}": "Apri {element}",
  "Parallel multi-instance": "Multi-istanza parallela",
  "Participant multiplicity": "Molteplicità del partecipante",
  "Search in diagram": "Cerca nel diagramma",
  "Sequential multi-instance": "Multi-istanza sequenziale",
  "Toggle non-interrupting": "Non interrompente sì/no",
  "Ad-hoc sub-process": "Sottoprocesso ad hoc",
  "Ad-hoc sub-process (collapsed)": "Sottoprocesso ad hoc (compresso)",
  "Ad-hoc sub-process (expanded)": "Sottoprocesso ad hoc (espanso)",
  "Business rule task": "Task di regola di business",
  "Call activity": "Attività di chiamata",
  "Cancel boundary event": "Evento di confine di annullamento",
  "Cancel end event": "Evento di fine di annullamento",
  "Compensation boundary event": "Evento di confine di compensazione",
  "Compensation end event": "Evento di fine di compensazione",
  "Compensation intermediate throw event": "Evento intermedio di compensazione (lancio)",
  "Compensation start event": "Evento di inizio di compensazione",
  "Complex gateway": "Gateway complesso",
  "Conditional boundary event": "Evento di confine condizionale",
  "Conditional boundary event (non-interrupting)": "Evento di confine condizionale (non interrompente)",
  "Conditional flow": "Flusso condizionale",
  "Conditional intermediate catch event": "Evento intermedio condizionale",
  "Conditional start event": "Evento di inizio condizionale",
  "Conditional start event (non-interrupting)": "Evento di inizio condizionale (non interrompente)",
  "Data object reference": "Oggetto dati",
  "Data store reference": "Archivio dati",
  "Default flow": "Flusso predefinito",
  "End event": "Evento di fine",
  "Error boundary event": "Evento di confine di errore",
  "Error end event": "Evento di fine di errore",
  "Error start event": "Evento di inizio di errore",
  "Escalation boundary event": "Evento di confine di escalation",
  "Escalation boundary event (non-interrupting)": "Evento di confine di escalation (non interrompente)",
  "Escalation end event": "Evento di fine di escalation",
  "Escalation intermediate throw event": "Evento intermedio di escalation (lancio)",
  "Escalation start event": "Evento di inizio di escalation",
  "Escalation start event (non-interrupting)": "Evento di inizio di escalation (non interrompente)",
  "Event based instantiating Gateway": "Gateway basato su eventi (istanziante)",
  "Event sub-process": "Sottoprocesso di evento",
  "Event-based gateway": "Gateway basato su eventi",
  "Exclusive gateway": "Gateway esclusivo",
  "Expanded pool/participant": "Pool/partecipante espanso",
  "Empty pool/participant": "Pool/partecipante vuoto",
  "Empty pool/participant (removes content)": "Pool/partecipante vuoto",
  "Inclusive gateway": "Gateway inclusivo",
  "Intermediate throw event": "Evento intermedio (lancio)",
  "Link intermediate catch event": "Evento intermedio di collegamento (ricezione)",
  "Link intermediate throw event": "Evento intermedio di collegamento (lancio)",
  "Manual task": "Task manuale",
  "Message boundary event": "Evento di confine di messaggio",
  "Message boundary event (non-interrupting)": "Evento di confine di messaggio (non interrompente)",
  "Message end event": "Evento di fine di messaggio",
  "Message intermediate catch event": "Evento intermedio di messaggio (ricezione)",
  "Message intermediate throw event": "Evento intermedio di messaggio (invio)",
  "Message start event": "Evento di inizio di messaggio",
  "Message start event (non-interrupting)": "Evento di inizio di messaggio (non interrompente)",
  "Parallel Event based instantiating Gateway": "Gateway parallelo basato su eventi (istanziante)",
  "Parallel gateway": "Gateway parallelo",
  "Receive task": "Task di ricezione",
  "Script task": "Task di script",
  "Send task": "Task di invio",
  "Sequence flow": "Flusso di sequenza",
  "Service task": "Task di servizio",
  "Signal boundary event": "Evento di confine di segnale",
  "Signal boundary event (non-interrupting)": "Evento di confine di segnale (non interrompente)",
  "Signal end event": "Evento di fine di segnale",
  "Signal intermediate catch event": "Evento intermedio di segnale (ricezione)",
  "Signal intermediate throw event": "Evento intermedio di segnale (lancio)",
  "Signal start event": "Evento di inizio di segnale",
  "Signal start event (non-interrupting)": "Evento di inizio di segnale (non interrompente)",
  "Start event": "Evento di inizio",
  "Sub-process": "Sottoprocesso",
  "Sub-process (collapsed)": "Sottoprocesso (compresso)",
  "Sub-process (expanded)": "Sottoprocesso (espanso)",
  "Task": "Task",
  "Terminate end event": "Evento di fine (termina)",
  "Timer boundary event": "Evento di confine timer",
  "Timer boundary event (non-interrupting)": "Evento di confine timer (non interrompente)",
  "Timer intermediate catch event": "Evento intermedio timer",
  "Timer start event": "Evento di inizio timer",
  "Timer start event (non-interrupting)": "Evento di inizio timer (non interrompente)",
  "Transaction": "Transazione",
  "User task": "Task utente",
  "Intermediate/boundary events": "Eventi intermedi/di confine",
  "Gateways": "Gateway", "Tasks": "Task", "Events": "Eventi", "Sub-processes": "Sottoprocessi",
  "Data objects": "Oggetti dati", "Participants": "Partecipanti", "Artifacts": "Artefatti",
  "Search": "Cerca", "No matching entries found.": "Nessun risultato."
};

// testi in inglese accorciati (nei menu non devono comparire tagliati)
var BPMN_EN = { "Empty pool/participant (removes content)": "Empty pool/participant" };

function initBpaEditor(o) {
  var T = o.T, uiLang = o.uiLang;
  var lang = o.lang;            // lingua del diagramma (nomi delle attivita'), separata da quella dell'interfaccia
  var dirty = false, busy = false;
  var status = document.getElementById("bpa-status");
  function say(text, cls) { status.textContent = text || ""; status.className = "bpa-status " + (cls || ""); }
  function fmt(s, p) { return String(s).replace(/\{(\w+)\}/g, function (_, k) { return p[k] !== undefined ? p[k] : "{" + k + "}"; }); }

  var translate = {
    translate: ["value", function (template, replacements) {
      replacements = replacements || {};
      var t = uiLang === "it" && BPMN_IT[template] ? BPMN_IT[template] : (BPMN_EN[template] || template);
      return t.replace(/{([^}]+)}/g, function (_, key) { return replacements[key] !== undefined ? replacements[key] : "{" + key + "}"; });
    }]
  };
  var modeler = new BpmnJS({ container: "#bpa-canvas", additionalModules: [translate] });
  var api = function (path) { return "/analysis/bpa/" + encodeURIComponent(o.id) + path; };

  function load(l) {
    busy = true; loading = true; say(T.loading);
    return fetch(api("/xml?workspace_id=" + encodeURIComponent(o.ws) + "&lang=" + l), { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then(function (xml) { return modeler.importXML(xml); })
      .then(function (res) {
        lang = l; dirty = false; busy = false; loading = false;
        modeler.get("canvas").zoom("fit-viewport", "auto");
        document.querySelectorAll("[data-dlang]").forEach(function (b) { b.classList.toggle("active", b.dataset.dlang === lang); b.setAttribute("aria-pressed", b.dataset.dlang === lang ? "true" : "false"); });
        say((res && res.warnings && res.warnings.length) ? fmt(T.warnings, { n: res.warnings.length }) : "");
      })
      .catch(function (e) { busy = false; loading = false; say(T.loadError + " (" + (e.message || e) + ")", "bad"); });
  }
  // Bozza nel browser (non sul server) delle modifiche non salvate: su iPad chiudendo la scheda il browser non chiede
  // nulla, quindi alla riapertura del modello si chiede se salvarle o scartarle.
  var DRAFT = "pm-bpa-draft-" + o.id, draftTimer = null;
  function readDraft() { try { var d = JSON.parse(localStorage.getItem(DRAFT) || "null"); return d && d.xml ? d : null; } catch (e) { return null; } }
  function clearDraft() { clearTimeout(draftTimer); try { localStorage.removeItem(DRAFT); } catch (e) {} }
  function storeDraft() {
    clearTimeout(draftTimer);
    if (!dirty) return;
    modeler.saveXML({ format: false }).then(function (r) {
      try { localStorage.setItem(DRAFT, JSON.stringify({ lang: lang, xml: r.xml, at: new Date().toISOString() })); } catch (e) {}
    });
  }
  function storeDraftSoon() { clearTimeout(draftTimer); draftTimer = setTimeout(storeDraft, 600); }
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "hidden") { commitText(); storeDraft(); } });
  window.addEventListener("pagehide", function () { commitText(); storeDraft(); });

  var loading = false;   // durante l'apertura le modifiche non contano; durante il salvataggio si'
  modeler.on("commandStack.changed", function () { if (!loading) { dirty = true; storeDraftSoon(); if (!busy) say(T.unsaved, "warn"); } });
  modeler.on("directEditing.activate", function () { if (!loading) say(T.unsaved, "warn"); });
  // un nome in corso di scrittura (casella di testo aperta) si conferma prima di salvare, cambiare lingua o uscire
  function commitText() { try { var de = modeler.get("directEditing"); if (de.isActive()) de.complete(); } catch (e) {} }

  function save() {
    if (busy) return Promise.resolve(false);
    commitText();
    busy = true; dirty = false; say(T.saving);
    return modeler.saveXML({ format: true }).then(function (r) {
      return fetch(api("/save"), { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace_id: o.ws, lang: lang, xml: r.xml }) });
    }).then(function (r) { return r.json().then(function (j) { if (!r.ok || j.error) throw new Error(j.error || r.status); return j; }); })
      .then(function (j) {
        busy = false;
        if (dirty) { say(T.unsaved, "warn"); storeDraftSoon(); return true; }   // cambiato ancora mentre si salvava
        clearDraft();
        var other = lang === "it" ? "EN" : "IT";
        if (j.untranslated) say(fmt(T.savedUntranslated, { n: j.untranslated, l: other }), "warn");
        else say(j.translated ? fmt(T.savedTranslated, { n: j.translated, l: other }) : T.saved, "ok");
        return true;
      })
      .catch(function (e) { busy = false; dirty = true; say(T.saveError + " " + (e.message || e), "bad"); return false; });
  }

  document.getElementById("bpa-save").addEventListener("click", save);
  document.querySelectorAll("[data-dlang]").forEach(function (b) {
    b.addEventListener("click", function () {
      var to = b.dataset.dlang;
      if (to === lang || busy) return;
      commitText();
      (dirty ? save() : Promise.resolve(true)).then(function (ok) { if (ok) load(to); });
    });
  });
  document.getElementById("bpa-fit").addEventListener("click", function () { modeler.get("canvas").zoom("fit-viewport", "auto"); });
  document.getElementById("bpa-zoom-in").addEventListener("click", function () { modeler.get("zoomScroll").stepZoom(1); });
  document.getElementById("bpa-zoom-out").addEventListener("click", function () { modeler.get("zoomScroll").stepZoom(-1); });
  document.getElementById("bpa-undo").addEventListener("click", function () { modeler.get("commandStack").undo(); });
  document.getElementById("bpa-redo").addEventListener("click", function () { modeler.get("commandStack").redo(); });

  function download(name, data, type) {
    var a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([data], { type: type }));
    a.download = name; document.body.appendChild(a); a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }
  document.getElementById("bpa-dl-bpmn").addEventListener("click", function () {
    modeler.saveXML({ format: true }).then(function (r) { download(o.fileName + "-" + lang + ".bpmn", r.xml, "application/xml"); });
  });
  document.getElementById("bpa-dl-svg").addEventListener("click", function () {
    modeler.saveSVG().then(function (r) { download(o.fileName + "-" + lang + ".svg", r.svg, "image/svg+xml"); });
  });
  document.getElementById("bpa-reset").addEventListener("click", function () {
    var menu = document.querySelector(".bpa-more"); if (menu) menu.open = false;
    pmConfirm(T.resetConfirm, { title: T.resetTitle, ok: T.resetOk }).then(function (yes) {
      if (!yes) return;
      busy = true; say(T.resetting);
      fetch(api("/reset"), { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace_id: o.ws }) })
        .then(function (r) { if (!r.ok) throw new Error(r.status); busy = false; dirty = false; clearDraft(); return load(lang); })
        .catch(function (e) { busy = false; say(T.saveError + " " + (e.message || e), "bad"); });
    });
  });

  // Ctrl/Cmd+S salva; uscendo con modifiche non salvate il browser chiede conferma
  document.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")) { e.preventDefault(); save(); }
  });
  // chiudendo la scheda (o ricaricandola) con modifiche non salvate il browser chiede se uscire: nessun salvataggio automatico
  window.addEventListener("beforeunload", function (e) { commitText(); if (dirty) { e.preventDefault(); e.returnValue = ""; } });

  touchToMouse(document.getElementById("bpa-canvas"), modeler);

  // a tutta pagina: l'editor occupa lo spazio sotto l'intestazione dell'app (che cambia altezza con lo schermo)
  var box = document.querySelector(".bpa-editor");
  function size() { box.style.height = Math.max(420, window.innerHeight - (box.getBoundingClientRect().top + window.scrollY)) + "px"; }
  size(); window.addEventListener("resize", size);

  load(lang).then(function () {
    var d = readDraft();
    if (!d) return;
    var when = d.at ? (window.pmLocalTime ? window.pmLocalTime(d.at) : d.at) : "";
    pmConfirm(fmt(T.draftAsk, { d: when }), { title: T.draftTitle, ok: T.save, cancel: T.discard, safe: true }).then(function (yes) {
      if (!yes) { clearDraft(); return; }
      busy = true; loading = true;
      modeler.importXML(d.xml).then(function () {
        lang = d.lang === "en" ? "en" : "it"; loading = false; busy = false; dirty = true;
        document.querySelectorAll("[data-dlang]").forEach(function (b) { b.classList.toggle("active", b.dataset.dlang === lang); b.setAttribute("aria-pressed", b.dataset.dlang === lang ? "true" : "false"); });
        save();
      }).catch(function (e) { loading = false; busy = false; clearDraft(); say(T.loadError + " (" + (e.message || e) + ")", "bad"); });
    });
  });
  return modeler;
}


/* bpmn-js lavora con il mouse: con il dito (iPad) i gesti diventano eventi del mouse sull'elemento toccato.
 * Un dito: trascina (elementi, tavolozza, maniglie, sfondo per spostare la vista); tocco = clic; doppio tocco =
 * doppio clic (rinomina). Due dita: zoom (pizzico) e spostamento. Il testo in modifica resta al tocco nativo. */
function touchToMouse(box, modeler) {
  if (!("ontouchstart" in window)) return;
  var last = null, lastTap = { t: 0, x: 0, y: 0 }, moved = false, pinch = null;
  function fire(type, t, target, extra) {
    var el = target || document.elementFromPoint(t.clientX, t.clientY) || box;
    var ev = new MouseEvent(type, Object.assign({ bubbles: true, cancelable: true, view: window, clientX: t.clientX, clientY: t.clientY,
      screenX: t.screenX, screenY: t.screenY, button: 0, buttons: type === "mouseup" || type === "click" ? 0 : 1 }, extra || {}));
    el.dispatchEvent(ev);
    return el;
  }
  var over = null;   // elemento sotto il dito: l'editor riconosce dove si posa un elemento da mouseover/mouseout
  function hover(t) {
    var el = document.elementFromPoint(t.clientX, t.clientY);
    if (!el || el === over) return;
    var init = { bubbles: true, cancelable: true, view: window, clientX: t.clientX, clientY: t.clientY };
    if (over) over.dispatchEvent(new MouseEvent("mouseout", Object.assign({ relatedTarget: el }, init)));
    el.dispatchEvent(new MouseEvent("mouseover", Object.assign({ relatedTarget: over }, init)));
    over = el;
  }
  function editing(e) { return e.target.closest && e.target.closest(".djs-direct-editing-parent, input, textarea, select, [contenteditable=true]"); }
  box.addEventListener("touchstart", function (e) {
    if (editing(e)) return;
    if (e.touches.length === 2) {   // pizzico: zoom attorno al centro delle dita
      if (last) { fire("mouseup", last.t); last = null; }
      var a = e.touches[0], b = e.touches[1];
      pinch = { d: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY), z: modeler.get("canvas").zoom(),
                cx: (a.clientX + b.clientX) / 2, cy: (a.clientY + b.clientY) / 2, vb: modeler.get("canvas").viewbox() };
      e.preventDefault(); return;
    }
    if (e.touches.length !== 1) return;
    e.preventDefault();
    var t = e.touches[0];
    moved = false;
    var entry = e.target.closest && e.target.closest(".djs-palette .entry");
    if (entry) {   // tavolozza: il clic prende l'elemento, che segue il dito e si posa dove lo si lascia
      fire("click", t, entry);
      last = { t: t, target: null, palette: true };
      return;
    }
    hover(t); fire("mousemove", t);   // come il mouse che arriva sull'elemento prima di premere
    last = { t: t, target: fire("mousedown", t) };
  }, { passive: false });
  box.addEventListener("touchmove", function (e) {
    if (editing(e)) return;
    e.preventDefault();   // niente scorrimento della pagina ne' «tira per aggiornare»
    if (pinch && e.touches.length === 2) {
      var a = e.touches[0], b = e.touches[1], canvas = modeler.get("canvas");
      var d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
      var r = box.getBoundingClientRect();
      canvas.zoom(Math.max(0.2, Math.min(4, pinch.z * d / pinch.d)), { x: pinch.cx - r.left, y: pinch.cy - r.top });
      return;
    }
    if (!last || e.touches.length !== 1) return;
    var t = e.touches[0];
    if (Math.abs(t.clientX - last.t.clientX) + Math.abs(t.clientY - last.t.clientY) > 4) moved = true;
    hover(t); fire("mousemove", t);
    last.t = t;
  }, { passive: false });
  box.addEventListener("touchend", function (e) {
    if (pinch) { if (e.touches.length < 2) pinch = null; e.preventDefault(); return; }
    if (!last || editing(e)) return;
    e.preventDefault();
    var t = e.changedTouches[0], target = last.target;
    if (last.palette) {
      if (moved) { hover(t); fire("mousemove", t); fire("mouseup", t); fire("click", t); }   // posato nel punto in cui si alza il dito
      last = null;   // senza trascinare: l'elemento resta da posare con un tocco sul diagramma
      return;
    }
    fire("mouseup", t);
    if (!moved) {
      fire("click", t, target);
      var now = Date.now();
      if (now - lastTap.t < 350 && Math.abs(t.clientX - lastTap.x) + Math.abs(t.clientY - lastTap.y) < 20) {
        fire("dblclick", t, target, { detail: 2 }); lastTap.t = 0;
      } else lastTap = { t: now, x: t.clientX, y: t.clientY };
    }
    last = null;
  }, { passive: false });
  box.addEventListener("touchcancel", function () { if (last) fire("mouseup", last.t); last = null; pinch = null; });
}
