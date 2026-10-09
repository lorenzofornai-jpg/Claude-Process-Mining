"""Assistente della revisione del mapping: risponde alle domande del Data Engineer e propone modifiche.

Esempi: «perché BSEG non ha una chiave?», «accetta tutto quello che riguarda la Fattura», «rifiuta
l'oggetto Bkpf (altro)», «chiama l'evento Post Document "Registrazione documento"», «collega Clear Invoice
all'Incasso tramite AUGBL». Conosce il processo (assessment, oggetti di business confermati), il profilo dei
dati e tutte le righe del mapping con la loro decisione.

Le modifiche sono proposte con strumenti (decisioni, nomi, valori, collegamenti): la pagina le mostra come
schede da confermare; solo dopo la conferma vengono applicate (apply). Nulla cambia senza l'utente.
"""
from __future__ import annotations

import json

from app.config import EXPLAIN_MODEL
from app.i18n import msg, render
from app.services.ai_mapping import _CHARS_PER_TOKEN, _FALLBACK_PRICE, _PRICES_USD_PER_MTOK

MAX_OUTPUT_TOKENS = 2500
TYPICAL_OUTPUT_TOKENS = 500
MAX_TURNS = 12
MAX_ROWS = 400

SYSTEM_PROMPT = """\
Sei l'assistente della revisione del mapping in un'app di process mining object-centric (OCEL 2.0). L'utente è
il Data Engineer: sta decidendo quali proposte di mapping tenere prima di generare il dataset per l'analisi.

Come funziona la pagina «Revisione del mapping»:
- In alto il «Modello proposto»: gruppi di oggetti (chiave e attributi) e di eventi (data/ora, attività,
  attributi, collegamenti agli oggetti). Ogni gruppo si accetta o rifiuta in blocco.
- Sotto, il dettaglio per tabella: una riga per ogni proposta (colonna sorgente -> elemento OCEL) con confidenza,
  motivazione e decisione (accetta / rifiuta); «Modifica» cambia il tipo di riga e i nomi.
- Elementi: object_type.key (chiave di un oggetto), object_type.attribute, object_type.split (una colonna divide
  la tabella in più tipi di oggetto: valore -> tipo, "" = escluso), event_type.timestamp (data di un evento),
  event_type.time (ora), event_type.activity (colonna che dice quale attività: valore -> nome attività, "" =
  esclusa), event_type.attribute, e2o_relationship (collegamento evento -> oggetto).
- Le date previste o di scadenza non sono eventi; i log sono eventi di altri oggetti; le anagrafiche sono di
  solito dimensioni. Gli oggetti di business confermati (se ci sono) sono quelli scelti prima del mapping.
- Quando ogni riga ha una decisione si genera il dataset.

Cosa fare:
- Rispondi usando i DATI: righe del mapping (con row_id, stato, confidenza, motivazione), gruppi del modello,
  profilo dei dati, oggetti di business, assessment. Spiega con parole semplici cosa cambia nel dataset.
- Se l'utente chiede di cambiare qualcosa, o se una modifica è chiaramente utile e l'utente è d'accordo, usa gli
  strumenti: set_decisions (accettare, rifiutare o rimettere da decidere gruppi o righe), rename (nome di un tipo di
  oggetto o di evento in tutte le righe), set_values (traduzione valore -> nome per una riga activity o split),
  add_link (collegare un evento a un oggetto tramite una colonna). Usa solo row_id, nomi di gruppo, tabelle e colonne
  presenti nei DATI. Ogni modifica viene mostrata all'utente, che la conferma con il pulsante ui_labels.apply:
  dillo in una frase, senza ripetere l'elenco delle modifiche.
- I nomi di oggetti, eventi e attività devono essere nomi di business corretti nella lingua del processo: proponi
  correzioni se un nome è tecnico (es. «Bkpf event»), con grammatica corretta.
- Etichette dell'app: usa SEMPRE i testi di "ui_labels" (sono nella lingua dell'utente).
- Rispondi nella lingua indicata da "language" (it = italiano, en = inglese), al massimo 200 parole, testo semplice
  (niente titoli markdown, al massimo elenchi con "-").
"""

TOOLS = [
    {
        "name": "set_decisions",
        "description": "Propone decisioni su gruppi del modello o su singole righe del mapping (l'utente conferma).",
        "input_schema": {
            "type": "object",
            "properties": {
                "changes": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "group_kind": {"type": "string", "enum": ["obj", "evt"],
                                       "description": "Per un gruppo: obj (tipo di oggetto) o evt (tipo di evento)."},
                        "group_name": {"type": "string", "description": "Nome esatto del gruppo."},
                        "row_id": {"type": "integer", "description": "Per una singola riga, al posto del gruppo."},
                        "decision": {"type": "string", "enum": ["confirmed", "rejected", "proposed"]},
                    },
                    "required": ["decision"],
                }},
                "why": {"type": "string", "description": "Perché, in una frase."},
            },
            "required": ["changes", "why"],
        },
    },
    {
        "name": "rename",
        "description": "Propone un nuovo nome per un tipo di oggetto o di evento, in tutte le righe che lo usano.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["object_type", "event_type"]},
                "old": {"type": "string"}, "new": {"type": "string"}, "why": {"type": "string"},
            },
            "required": ["kind", "old", "new", "why"],
        },
    },
    {
        "name": "set_values",
        "description": "Propone la traduzione valore -> nome di una riga activity (nomi di attività) o split (tipi di "
                       "oggetto); \"\" esclude il valore.",
        "input_schema": {
            "type": "object",
            "properties": {"row_id": {"type": "integer"}, "values": {"type": "object", "additionalProperties": {"type": "string"}},
                           "why": {"type": "string"}},
            "required": ["row_id", "values", "why"],
        },
    },
    {
        "name": "add_link",
        "description": "Propone di collegare gli eventi di un tipo a un tipo di oggetto tramite una colonna della "
                       "tabella dell'evento che contiene la chiave (o il numero) di quell'oggetto.",
        "input_schema": {
            "type": "object",
            "properties": {"event_type": {"type": "string"}, "object_type": {"type": "string"},
                           "column": {"type": "string"}, "why": {"type": "string"}},
            "required": ["event_type", "object_type", "column", "why"],
        },
    },
]

UI_LABELS = {
    "apply": "Applica", "model": "Modello proposto", "accept_all": "Accetta tutto", "reject": "Rifiuta",
    "edit": "Modifica", "add_link": "Aggiungi collegamento", "generate": "Conferma e genera il dataset",
    "business_objects_step": "Oggetti di business", "review": "Revisione del mapping",
}


def _price():
    return _PRICES_USD_PER_MTOK.get(EXPLAIN_MODEL, _FALLBACK_PRICE)


def _row(r: dict, lang: str) -> dict:
    out = {"row_id": r["row_id"], "table": r["source_table"], "column": r.get("source_column"),
           "element": r["ocel_element"], "status": r["status"], "confidence": round(float(r.get("confidence") or 0), 2)}
    for k in ("object_type", "event_type", "attribute_name", "related_object_type", "qualifier"):
        if r.get(k):
            out[k] = r[k]
    if r.get("activity_values"):
        out["values"] = dict(list(r["activity_values"].items())[:30])
    why = render(lang, r.get("rationale"))
    if why:
        out["why"] = why[:160]
    return out


def build_context(*, language: str, process_name: str, assessment: dict, rows: list[dict], model: dict,
                  business_objects: list | None, profile_issues: list, tables: list, link_options: dict,
                  ui_labels: dict) -> str:
    payload = {
        "language": language, "ui_labels": ui_labels,
        "process": {"name": process_name, "assessment": assessment},
        "business_objects": [{"name": o["name"], "table": o["table"], "key": o.get("key"), "role": o.get("role"),
                              "included": o.get("include"), **({"rows_where": o["filter"]} if o.get("filter") else {})}
                             for o in business_objects or []],
        "tables": [{"name": t.name, "rows": t.row_count, "columns": [c.name for c in t.columns][:60]} for t in tables],
        "data_profile_issues": profile_issues[:30],
        "model_groups": [{"kind": g["kind"], "name": g["name"], "tables": g["tables"], "keys": g.get("keys"),
                          "timestamps": g.get("timestamps"), "links": g.get("links"), "total_rows": g["total"],
                          "to_decide": g["pending"], "rejected": g["rejected"]}
                         for g in model.get("objects", []) + model.get("events", [])],
        "link_options": link_options,
        "mapping_rows": [_row(r, language) for r in rows[:MAX_ROWS]],
    }
    return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))[:90000]


def _messages(history: list[dict]) -> list[dict]:
    msgs = []
    for m in history[-MAX_TURNS:]:
        role = "assistant" if m.get("role") == "assistant" else "user"
        content = str(m.get("content") or "").strip()[:4000]
        if not content:
            continue
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n\n" + content
        else:
            msgs.append({"role": role, "content": content})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def estimate(context: str, history: list[dict]) -> dict:
    price_in, price_out = _price()
    chars = len(SYSTEM_PROMPT) + len(context) + len(json.dumps(TOOLS)) + sum(len(m["content"]) for m in _messages(history))
    tokens_in = int(chars / _CHARS_PER_TOKEN) + 80
    return {"cost_usd": round((tokens_in * price_in + TYPICAL_OUTPUT_TOKENS * price_out) / 1_000_000, 4),
            "max_usd": round((tokens_in * price_in + MAX_OUTPUT_TOKENS * price_out) / 1_000_000, 4), "model": EXPLAIN_MODEL}


def ask(context: str, history: list[dict]) -> dict:
    """Chiama Claude. Ritorna {"answer", "actions", "cost_usd", "truncated"}."""
    import anthropic

    messages = _messages(history)
    if not messages:
        raise ValueError("nessuna domanda")
    client = anthropic.Anthropic()
    request = dict(model=EXPLAIN_MODEL, max_tokens=MAX_OUTPUT_TOKENS, system=SYSTEM_PROMPT + "\nDATI:\n" + context,
                   tools=TOOLS, messages=messages)
    try:
        response = client.messages.create(**request, output_config={"effort": "low"})
    except (TypeError, anthropic.BadRequestError):
        response = client.messages.create(**request)
    price_in, price_out = _price()
    cost = ((response.usage.input_tokens or 0) * price_in + (response.usage.output_tokens or 0) * price_out) / 1_000_000
    answer = "".join(b.text for b in response.content if getattr(b, "type", "") == "text").strip()
    actions = [{"type": b.name, **(b.input or {})} for b in response.content if getattr(b, "type", "") == "tool_use"]
    return {"answer": answer, "actions": actions, "cost_usd": round(cost, 4), "truncated": response.stop_reason == "max_tokens"}


# ---------- azioni: controllo e applicazione ----------

def _group_of(r: dict):
    el = r["ocel_element"]
    if el.startswith("object_type") and r.get("object_type"):
        return ("obj", r["object_type"])
    if (el.startswith("event_type") or el == "e2o_relationship") and r.get("event_type"):
        return ("evt", r["event_type"])
    return None


def describe(action: dict, rows: list[dict]) -> dict | None:
    """Controlla un'azione proposta e la descrive per la scheda di conferma: None se non e' applicabile
    (riga o gruppo inesistente, tipo non previsto). Ritorna {"action", "lines": [msg...], "why"}."""
    by_id = {r["row_id"]: r for r in rows}
    groups = {_group_of(r) for r in rows} - {None}
    kind = action.get("type")
    lines = []
    if kind == "set_decisions":
        labels = {"confirmed": msg("accetta"), "rejected": msg("rifiuta"), "proposed": msg("rimetti da decidere")}
        clean = []
        for c in action.get("changes") or []:
            d = c.get("decision")
            if d not in labels:
                continue
            if c.get("row_id") is not None and int(c["row_id"]) in by_id:
                r = by_id[int(c["row_id"])]
                clean.append({"row_id": int(c["row_id"]), "decision": d})
                lines.append(msg("{d}: riga {t}.{c} ({e})", d=labels[d], t=r["source_table"], c=r.get("source_column") or "—",
                                 e=r["ocel_element"]))
            elif (c.get("group_kind"), c.get("group_name")) in groups:
                clean.append({"group_kind": c["group_kind"], "group_name": c["group_name"], "decision": d})
                lines.append(msg("{d}: gruppo «{g}»", d=labels[d], g=c["group_name"]))
        if not clean:
            return None
        action = {**action, "changes": clean}
    elif kind == "rename":
        field = action.get("kind")
        old, new = str(action.get("old") or "").strip(), " ".join(str(action.get("new") or "").split())[:100]
        used = {r.get(field) for r in rows}
        if field not in ("object_type", "event_type") or old not in used or not new or new == old:
            return None
        action = {**action, "old": old, "new": new}
        lines.append(msg("rinomina «{o}» in «{n}»", o=old, n=new))
    elif kind == "set_values":
        r = by_id.get(int(action.get("row_id", -1)))
        values = action.get("values")
        if not r or r["ocel_element"] not in ("event_type.activity", "object_type.split") or not isinstance(values, dict):
            return None
        values = {str(k): str(v) for k, v in values.items()}
        action = {**action, "row_id": r["row_id"], "values": values}
        shown = ", ".join(f"{k} → {v or '∅'}" for k, v in list(values.items())[:8])
        lines.append(msg("nomi per valore di {t}.{c}: {v}", t=r["source_table"], c=r.get("source_column"), v=shown))
    elif kind == "add_link":
        lines.append(msg("collega «{e}» a {o} tramite {c}", e=action.get("event_type"), o=action.get("object_type"),
                         c=action.get("column")))
    else:
        return None
    return {"action": action, "lines": lines, "why": str(action.get("why") or "")[:300]}


def apply(action: dict, rows: list[dict], add_link=None) -> bool:
    """Applica un'azione gia' controllata da describe alle righe della revisione (in sessione)."""
    kind = action.get("type")
    by_id = {r["row_id"]: r for r in rows}
    if kind == "set_decisions":
        for c in action.get("changes") or []:
            targets = ([by_id[c["row_id"]]] if "row_id" in c and c["row_id"] in by_id else
                       [r for r in rows if _group_of(r) == (c.get("group_kind"), c.get("group_name"))])
            for r in targets:
                r["status"] = c["decision"]
        return True
    if kind == "rename":
        field, old, new = action["kind"], action["old"], action["new"]
        for r in rows:
            touched = False
            if r.get(field) == old:
                r[field] = new
                touched = True
            if field == "object_type":
                if r.get("related_object_type") == old:
                    r["related_object_type"] = new
                    touched = True
                if r["ocel_element"] == "object_type.split" and r.get("activity_values"):
                    vals = {k: (new if v == old else v) for k, v in r["activity_values"].items()}
                    touched = touched or vals != r["activity_values"]
                    r["activity_values"] = vals
            if touched and r["status"] == "confirmed":
                r["status"] = "overridden"
        return True
    if kind == "set_values":
        r = by_id.get(action["row_id"])
        if not r:
            return False
        r["activity_values"] = {**(r.get("activity_values") or {}), **action["values"]}
        if r["status"] != "rejected":
            r["status"] = "overridden"
        return True
    if kind == "add_link" and add_link:
        before = len(rows)
        add_link(rows, str(action.get("event_type") or ""), str(action.get("object_type") or ""),
                 str(action.get("column") or ""))
        return len(rows) > before or any(
            r["ocel_element"] == "e2o_relationship" and r.get("event_type") == action.get("event_type")
            and r.get("related_object_type") == action.get("object_type") for r in rows)
    return False
