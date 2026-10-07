"""Copertura degli obiettivi: i dati caricati bastano per le domande dell'assessment?

Dopo il caricamento delle tabelle, prima del mapping. Due livelli:

1. Senza costi (sempre): si cercano nelle colonne caricate i «dati concetto» che servono
   alle analisi tipiche (data di chiusura/pagamento, scadenza, importo, utente, canale o
   transazione, storico modifiche, motivo, condizioni di pagamento, cliente/fornitore,
   dispute). Ogni obiettivo spuntato nell'assessment («Cosa vuoi analizzare?») risulta
   coperto, coperto in parte o non coperto, con le colonne trovate e cosa manca.
   I riconoscimenti sono generici (nomi in piu' lingue e sistemi, profilo delle date); i
   nomi SAP sono solo una parte degli esempi.

2. Con Claude (su richiesta, con costo indicato prima): le domande di business scritte
   liberamente diventano obiettivi distinti; per ognuno cosa si puo' misurare e quali dati
   o tabelle mancano, nei termini del sistema sorgente dichiarato nell'assessment.
"""
from __future__ import annotations

import hashlib
import json
import re

from app.config import EXPLAIN_MODEL
from app.connectors.base import TableSchema
from app.i18n import msg
from app.services import sap_dictionary
from app.services.ai_mapping import _CHARS_PER_TOKEN, _FALLBACK_PRICE, _PRICES_USD_PER_MTOK

MAX_OUTPUT_TOKENS = 3500
TYPICAL_OUTPUT_TOKENS = 1600

# ---------- concetti e loro riconoscimento ----------

def _n(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


CONCEPTS: dict[str, dict] = {
    "settlement_date": {"label": msg("data di chiusura o pagamento"), "types": {"date"},
                        "names": {"augdt", "clearingdate", "paymentdate", "paidat", "paiddate", "settledat", "closedat",
                                  "datapagamento", "dataincasso", "datachiusura"},
                        "parts": ("clear", "paid", "payment", "settle", "incass", "pagament", "chiusur", "closed")},
    "due_date": {"label": msg("data di scadenza"), "types": {"date"},
                 "names": {"zfbdt", "netdt", "duedate", "scadenza", "datascadenza"}, "parts": ("due", "scaden")},
    "amount": {"label": msg("importo"), "types": {"float", "integer", "string"},
               "names": {"dmbtr", "wrbtr", "netwr", "amount", "importo", "value", "valore", "total", "totale"},
               "parts": ("amount", "importo", "amt")},
    "user": {"label": msg("utente che ha eseguito"), "types": None,
             "names": {"usnam", "ernam", "uname", "user", "username", "userid", "createdby", "changedby", "utente", "operatore"},
             "parts": ("user", "utente", "createdby", "changedby")},
    "channel": {"label": msg("transazione o canale"), "types": None,
                "names": {"tcode", "transaction", "channel", "source", "origin", "canale", "origine"},
                "parts": ("tcode", "channel", "canale")},
    "reason": {"label": msg("motivo o causale"), "types": None,
               "names": {"reasoncode", "reason", "grund", "motivo", "causale", "rstgr"}, "parts": ("reason", "motiv", "causal")},
    "payment_terms": {"label": msg("condizioni di pagamento"), "types": None,
                      "names": {"zterm", "paymentterms", "terms", "condizionipagamento", "termspayment"},
                      "parts": ("zterm", "paymentterm", "condizionipag")},
    "dunning": {"label": msg("blocco o livello di sollecito"), "types": None,
                "names": {"mansp", "manst", "dunningblock", "dunninglevel"}, "parts": ("dunning", "sollecit")},
    "customer": {"label": msg("cliente"), "types": None,
                 "names": {"kunnr", "customer", "customerid", "cliente", "kunag", "kunrg"}, "parts": ("customer", "client")},
    "vendor": {"label": msg("fornitore"), "types": None,
               "names": {"lifnr", "vendor", "supplier", "fornitore"}, "parts": ("vendor", "supplier", "fornitor")},
}

# Concetti di tabella: storico modifiche e dispute
CHANGE_TABLE = re.compile(r"(cdhdr|cdpos|audit|history|hist|changelog|_log$|log_|modific)", re.I)
# storico «generale» (tutti i campi dei documenti); gli altri log registrano solo alcuni campi (es. i blocchi)
GENERAL_CHANGE = re.compile(r"(cdhdr|cdpos|audit|changelog|change_log|history)", re.I)
OLD_NEW = ({"oldval", "valueold", "oldvalue", "valold", "valoreprecedente"}, {"newval", "valuenew", "newvalue", "valnew", "valorenuovo"})
DISPUTE = re.compile(r"(dispute|disput|contestaz|udmcase|scmg)", re.I)


def detect(tables: list[TableSchema]) -> dict[str, list[str]]:
    """{concetto: ["TABELLA.COLONNA", ...]} trovati nei dati caricati."""
    found: dict[str, list[str]] = {k: [] for k in CONCEPTS}
    found["change_history"] = []
    found["dispute"] = []
    found["dated_facts"] = []
    for t in tables:
        cols = {_n(c.name): c for c in t.columns}
        for c in t.columns:
            n = _n(c.name)
            if c.inferred_type == "date" and not c.planned_reason:
                found["dated_facts"].append(f"{t.name}.{c.name}")
            for key, spec in CONCEPTS.items():
                if spec["types"] and c.inferred_type not in spec["types"]:
                    continue
                if key == "due_date" and c.planned_reason:
                    found[key].append(f"{t.name}.{c.name}")
                    continue
                if n in spec["names"] or any(p in n for p in spec["parts"]):
                    if key == "amount" and c.inferred_type == "string" and n not in spec["names"]:
                        continue
                    found[key].append(f"{t.name}.{c.name}")
        if CHANGE_TABLE.search(t.name) or (set(cols) & OLD_NEW[0] and set(cols) & OLD_NEW[1]):
            found["change_history"].append(t.name)
        if DISPUTE.search(t.name) or any(DISPUTE.search(c.name) for c in t.columns):
            found["dispute"].append(t.name)
        for c in t.columns:  # valori di motivo che parlano di dispute (es. PRICE_DISPUTE)
            if _n(c.name) in CONCEPTS["reason"]["names"] and any(DISPUTE.search(str(v)) for v in c.sample_values):
                found["dispute"].append(f"{t.name}.{c.name}")
    found["dispute"] = sorted(set(found["dispute"]))
    return found


# ---------- obiettivi dell'assessment (opzioni di «Cosa vuoi analizzare?») ----------

def _req(*items):
    return list(items)


# Per ogni opzione: concetti necessari (tutti) e utili (rendono la risposta piu' completa); cosa chiedere se mancano
OBJECTIVE_NEEDS: dict[str, dict] = {
    "Tempi di attraversamento e colli di bottiglia": {
        "needs": _req("two_dated_facts"), "useful": _req("amount")},
    "Conformità al processo standard (varianti, deviazioni)": {
        "needs": _req("three_dated_facts"), "useful": _req("change_history")},
    "Rilavorazioni e modifiche (prezzi, quantità, date, blocchi)": {
        "needs": _req("change_history"), "useful": _req("user")},
    "Automazione e attività manuali": {
        "needs": _req("user"), "useful": _req("channel", "change_history")},
    "Compliance e segregazione dei compiti": {
        "needs": _req("user_on_two_tables"), "useful": _req("change_history")},
    "Performance per fornitore / cliente / reparto": {
        "needs": _req("partner"), "useful": _req("two_dated_facts")},
    "Puntualità di pagamenti o incassi": {
        "needs": _req("due_date", "settlement_date"), "useful": _req("amount", "dunning")},
}

MISSING_HINTS = {
    "two_dated_facts": msg("almeno due date di fatti diversi (es. creazione e chiusura) sugli stessi documenti"),
    "three_dated_facts": msg("più passaggi datati dello stesso processo (almeno tre date di fatti)"),
    "change_history": msg("lo storico delle modifiche (in SAP CDHDR/CDPOS; altrove audit trail o tabelle di log con valore vecchio e nuovo)"),
    "user": msg("l'utente che esegue le operazioni (in SAP USNAM/ERNAM)"),
    "user_on_two_tables": msg("l'utente su almeno due passaggi diversi del processo, per confrontare chi fa cosa"),
    "partner": msg("il cliente o il fornitore sui documenti"),
    "due_date": msg("la data di scadenza o la data base con i giorni di pagamento (in SAP ZFBDT con ZBD1T)"),
    "settlement_date": msg("la data di pagamento o di chiusura del documento (in SAP AUGDT nelle partite pareggiate)"),
    "amount": msg("gli importi"),
    "channel": msg("la transazione o il canale (in SAP TCODE), per distinguere automatico e manuale"),
    "dunning": msg("blocchi e livelli di sollecito (in SAP MANSP/MANST)"),
}


def _have(found: dict, need: str) -> list[str]:
    if need == "two_dated_facts":
        return found["dated_facts"] if len(found["dated_facts"]) >= 2 else []
    if need == "three_dated_facts":
        return found["dated_facts"] if len(found["dated_facts"]) >= 3 else []
    if need == "user_on_two_tables":
        return found["user"] if len({x.split(".")[0] for x in found["user"]}) >= 2 else []
    if need == "partner":
        return found["customer"] + found["vendor"]
    return found.get(need, [])


def quick_coverage(answers: dict, tables: list[TableSchema]) -> dict:
    """Livello senza costi: per ogni obiettivo spuntato, stato e colonne trovate/mancanti."""
    found = detect(tables)
    rows = []
    for option in answers.get("objectives") or []:
        spec = OBJECTIVE_NEEDS.get(option)
        if not spec:
            continue
        have, missing = [], []
        for need in spec["needs"]:
            cols = _have(found, need)
            (have if cols else missing).append({"need": need, "hint": MISSING_HINTS[need], "columns": cols[:8]})
        extra_missing = [{"need": u, "hint": MISSING_HINTS[u]} for u in spec["useful"] if not _have(found, u)]
        # un log che registra solo alcuni campi copre in parte: serve anche lo storico generale
        logs = found["change_history"]
        if "change_history" in spec["needs"] + spec["useful"] and logs and not any(GENERAL_CHANGE.search(x) for x in logs):
            extra_missing.append({"need": "change_history_general", "hint": msg(
                "uno storico generale delle modifiche: {t} registra solo alcuni campi (in SAP CDHDR/CDPOS registra prezzi, date, condizioni, blocchi)",
                t=", ".join(logs))})
        status = "covered" if not missing and not extra_missing else ("partial" if not missing or have else "missing")
        rows.append({"objective": option, "status": status, "have": have, "missing": missing, "useful_missing": extra_missing})
    return {"objectives": rows, "found": {k: v[:8] for k, v in found.items() if v}}


# ---------- livello con Claude ----------

SYSTEM_PROMPT = """\
Sei un esperto di process mining e dei sistemi gestionali (ERP, CRM, workflow). Un utente ha descritto in un
assessment gli obiettivi della sua analisi e ha caricato alcune tabelle. Devi dire, per ogni obiettivo, se con
queste tabelle si può misurare, cosa si può misurare e quali dati mancano.

Regole:
- Scomponi le domande di business, i KPI e gli obiettivi spuntati in obiettivi distinti e misurabili (di solito 3-6),
  con un titolo breve e la misura concreta (es. "giorni da emissione fattura a incasso, pesati sull'importo").
- Per ognuno: status "covered" (si misura con i dati caricati), "partial" (si misura solo in parte o con
  un'approssimazione) o "missing" (non si misura). In "available" elenca le colonne utili come TABELLA.COLONNA
  (solo colonne che esistono nei dati forniti). In "missing" elenca i dati che mancano: cosa serve in parole
  semplici, perché, e dove si trova di solito nel sistema sorgente dichiarato (nomi di tabelle o report reali e
  noti; se il sistema non è noto, descrivi il tipo di dato). Non inventare tabelle che non esistono.
- Usa le "evidenze" calcolate dall'app (concetti già trovati nelle colonne) come punto di partenza, ma verifica tu.
- Scrivi nella lingua indicata da "language" (it = italiano, en = inglese), in modo semplice e concreto.
- Rispondi SOLO con JSON valido, senza testo prima o dopo, con questa forma:
{"objectives":[{"title":"...","measure":"...","status":"covered|partial|missing","available":["TAB.COL - a cosa serve"],
"missing":[{"data":"...","why":"...","where":"..."}]}],"summary":"due o tre frasi di sintesi"}
"""


def _price():
    return _PRICES_USD_PER_MTOK.get(EXPLAIN_MODEL, _FALLBACK_PRICE)


def build_payload(*, language: str, process_name: str, answers: dict, tables: list[TableSchema], found: dict) -> str:
    keep = ("objectives", "key_questions", "kpis", "main_object", "other_objects", "start_event", "end_event", "systems",
            "known_tables", "custom_fields", "change_log")
    compact_tables = []
    for t in tables:
        hit = sap_dictionary.lookup(t.name, [c.name for c in t.columns])
        desc = hit[1].get("label") if hit else None
        compact_tables.append({
            "name": t.name, "rows": t.row_count, "description": desc,
            "columns": [{"name": c.name, "type": c.inferred_type, "examples": [str(v)[:24] for v in c.sample_values[:2]],
                         **({"planned_date": True} if c.planned_reason else {})} for c in t.columns[:80]],
        })
    payload = {"language": language, "process": process_name,
               "assessment": {k: answers[k] for k in keep if answers.get(k)},
               "tables": compact_tables, "evidence": {k: v for k, v in found.items() if v}}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def estimate(payload: str) -> dict:
    price_in, price_out = _price()
    tokens_in = int(len(SYSTEM_PROMPT + payload) / _CHARS_PER_TOKEN) + 50
    return {"cost_usd": round((tokens_in * price_in + TYPICAL_OUTPUT_TOKENS * price_out) / 1_000_000, 4),
            "max_usd": round((tokens_in * price_in + MAX_OUTPUT_TOKENS * price_out) / 1_000_000, 4),
            "model": EXPLAIN_MODEL}


def _parse(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start:end + 1])
    objs = []
    for o in data.get("objectives") or []:
        status = o.get("status") if o.get("status") in ("covered", "partial", "missing") else "partial"
        objs.append({"title": str(o.get("title") or "")[:200], "measure": str(o.get("measure") or "")[:400],
                     "status": status, "available": [str(x)[:200] for x in o.get("available") or []][:12],
                     "missing": [{k: str(m.get(k) or "")[:300] for k in ("data", "why", "where")}
                                 for m in o.get("missing") or [] if isinstance(m, dict)][:10]})
    return {"objectives": objs, "summary": str(data.get("summary") or "")[:1200]}


def ask(payload: str) -> dict:
    """Chiama Claude; ritorna {"result", "cost_usd", "truncated"}."""
    import anthropic

    client = anthropic.Anthropic()
    request = dict(model=EXPLAIN_MODEL, max_tokens=MAX_OUTPUT_TOKENS, system=SYSTEM_PROMPT,
                   messages=[{"role": "user", "content": payload}])
    try:
        response = client.messages.create(**request, output_config={"effort": "low"})
    except (TypeError, anthropic.BadRequestError):
        response = client.messages.create(**request)
    price_in, price_out = _price()
    cost = ((response.usage.input_tokens or 0) * price_in + (response.usage.output_tokens or 0) * price_out) / 1_000_000
    text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
    return {"result": _parse(text), "cost_usd": round(cost, 4), "truncated": response.stop_reason == "max_tokens"}


def tables_signature(tables: list[TableSchema]) -> str:
    """Impronta delle tabelle caricate: una valutazione vale per questo caricamento."""
    raw = "|".join(f"{t.name}:{t.row_count}:{','.join(c.name for c in t.columns)}" for t in sorted(tables, key=lambda x: x.name))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]
