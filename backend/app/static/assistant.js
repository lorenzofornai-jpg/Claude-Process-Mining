/* Assistente dell'analisi, comune a Process Explorer e Process Overview.
   initAssistant({ws, config, page, lang, T, getView, suggestions, endpoint, renderAction, persistKey}) collega
   il pannello di _assistant.html: costo indicativo prima di ogni domanda, conversazione nel browser, domande
   suggerite. renderAction(azione, contenitore, assistente) disegna le azioni proposte dall'assistente (es. nella
   revisione del mapping: accetta, rifiuta, rinomina) che l'utente conferma; con persistKey la conversazione
   sopravvive al ricaricamento della pagina (dopo un'azione applicata).
   Ritorna {open(prefill, focus), note(testo), save()} per usarlo da altri punti della pagina. */
function initAssistant(opts) {
  var T = opts.T;
  var panel = document.getElementById("assistant"), msgs = document.getElementById("as-messages");
  var text = document.getElementById("as-text"), costEl = document.getElementById("as-cost");
  var send = document.getElementById("as-send");
  var history = [], spent = 0, busy = false, focus = null, pending = [];
  var store = null;
  try { store = opts.persistKey ? window.sessionStorage : null; } catch (e) { store = null; }
  function fmt(s, p) { return String(s).replace(/\{(\w+)\}/g, function (_, k) { return p[k] !== undefined ? p[k] : "{" + k + "}"; }); }
  function el(tag, cls, t) { var e = document.createElement(tag); if (cls) e.className = cls; if (t !== undefined) e.textContent = t; return e; }
  function usd(x) { return Number(x).toLocaleString(opts.lang, { style: "currency", currency: "USD", minimumFractionDigits: 3, maximumFractionDigits: 3 }); }
  function post(messages, estimateOnly) {
    return fetch(opts.endpoint || "/analysis/assistant", {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspace_id: opts.ws, config_id: opts.config, page: opts.page, messages: messages,
                             view: opts.getView(), focus: focus, estimate: !!estimateOnly })
    }).then(function (r) { return r.json(); });
  }
  function updateEstimate() {
    post(history.concat([{ role: "user", content: text.value || "?" }]), true).then(function (j) {
      if (j.available === false) { costEl.textContent = j.error; send.disabled = true; text.disabled = true; return; }
      if (j.cost_usd === undefined) return;
      costEl.textContent = fmt(T.asCost, { c: usd(j.cost_usd), m: usd(j.max_usd) }) + (spent ? " · " + fmt(T.asSpent, { s: usd(spent) }) : "");
    }).catch(function () {});
  }
  function renderSuggestions() {
    var box = document.getElementById("as-suggest"); box.textContent = "";
    if (history.length) return;
    (opts.suggestions() || []).forEach(function (q) {
      var b = el("button", "as-chip", q); b.type = "button";
      b.addEventListener("click", function () { text.value = q; ask(); });
      box.appendChild(b);
    });
  }
  function bubble(role, t, cost) {
    var b = el("div", "as-msg as-" + role, t);
    if (cost) b.appendChild(el("div", "as-msg-cost", usd(cost)));
    msgs.appendChild(b);
    msgs.parentNode.scrollTop = msgs.parentNode.scrollHeight;
    return b;
  }
  function ask() {
    var q = text.value.trim();
    if (!q || busy) return;
    busy = true; send.disabled = true;
    history.push({ role: "user", content: q });
    text.value = ""; renderSuggestions();
    bubble("user", q);
    var wait = bubble("assistant", T.asThinking); wait.classList.add("as-wait");
    post(history, false).then(function (j) {
      wait.remove();
      if (j.available === false || j.error) { bubble("error", j.error || T.asError); history.pop(); return; }
      spent += j.cost_usd || 0;
      history.push({ role: "assistant", content: j.answer || "" });
      if (j.answer || !(j.actions || []).length) bubble("assistant", j.answer || "…", j.cost_usd);
      (j.actions || []).forEach(function (a) { renderAction(a); });
      save();
    }).catch(function () { wait.remove(); bubble("error", T.asError); history.pop(); })
      .finally(function () { busy = false; send.disabled = false; updateEstimate(); });
  }
  function renderAction(a) {
    if (!opts.renderAction) return;
    if (!a._k) { a._k = String(Date.now()) + Math.random().toString(36).slice(2); pending.push(a); }
    var box = el("div", "as-action");
    msgs.appendChild(box);
    opts.renderAction(a, box, api);
    msgs.parentNode.scrollTop = msgs.parentNode.scrollHeight;
  }
  // conversazione salvata nella scheda del browser (solo testi): dopo un'azione la pagina si ricarica
  function save() {
    if (!store) return;
    try { store.setItem(opts.persistKey, JSON.stringify({ history: history, spent: spent, open: !panel.hidden, pending: pending })); } catch (e) {}
  }
  function restore() {
    if (!store) return;
    var s = null;
    try { s = JSON.parse(store.getItem(opts.persistKey) || "null"); } catch (e) { s = null; }
    if (!s) return;
    history = s.history || []; spent = s.spent || 0;
    history.forEach(function (m) { if (m.content) bubble(m.role === "assistant" ? "assistant" : "user", m.content); });
    pending = s.pending || [];
    pending.forEach(function (a) { renderAction(a); });   // modifiche proposte non ancora applicate ne' annullate
    if (s.open) { panel.hidden = false; renderSuggestions(); updateEstimate(); }
  }
  function resolve(a) { pending = pending.filter(function (x) { return x._k !== a._k; }); save(); }
  function note(t) {   // esito di un'azione: resta nella conversazione (anche per Claude)
    history.push({ role: "user", content: t });
    bubble("user", t); save();
  }
  function open(prefill, f) {
    panel.hidden = false; focus = f || null;
    if (prefill) text.value = prefill;
    renderSuggestions(); updateEstimate(); save();
    text.focus();
  }
  document.querySelectorAll(".as-open-btn").forEach(function (b) { b.addEventListener("click", function () { open(); }); });
  document.getElementById("as-close").addEventListener("click", function () { panel.hidden = true; save(); });
  document.getElementById("as-new").addEventListener("click", function () {
    history = []; pending = []; msgs.textContent = ""; focus = null; renderSuggestions(); updateEstimate(); save();
  });
  document.getElementById("as-form").addEventListener("submit", function (e) { e.preventDefault(); ask(); });
  text.addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(); } });
  var api = { open: open, note: note, save: save, resolve: resolve, el: el, fmt: fmt,
              refresh: function () { if (!panel.hidden) { renderSuggestions(); updateEstimate(); } } };
  restore();
  return api;
}
