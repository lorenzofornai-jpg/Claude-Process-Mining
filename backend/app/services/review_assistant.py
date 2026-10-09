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
from app.services import data_query
from app.services.ai_mapping import _CHARS_PER_TOKEN, _FALLBACK_PRICE, _PRICES_USD_PER_MTOK

MAX_OUTPUT_TOKENS = 2500
TYPICAL_OUTPUT_TOKENS = 500
MAX_TURNS = 12
MAX_ROWS = 400
MAX_DATA_ROUNDS = 4   # interrogazioni dei dati prima della risposta

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
  add_link (collegare un evento a un oggetto tramite una colonna), add_computed (colonna calcolata: "change" per un
  log di modifiche, attività = Imposta/Rimuovi/Modifica + campo da campo, valore vecchio e nuovo; "slice" per
  estrarre una parte di un valore composto, es. il numero del documento dentro un OBJECTID, e collegare gli eventi a
  un oggetto). Usa solo row_id, nomi di gruppo, tabelle e colonne
  presenti nei DATI. Ogni modifica viene mostrata all'utente, che la conferma con il pulsante ui_labels.apply:
  dillo in una frase, senza ripetere l'elenco delle modifiche.
- Hai accesso ai dati caricati in sola lettura con query_rows (righe con filtri, conteggi per colonne) e
  compare_columns (quanti valori di una colonna si trovano in un'altra, anche solo una parte del valore). Usali
  quando la risposta dipende dai valori (es. quali documenti hanno un blocco, se un collegamento trova gli oggetti,
  che valori ha una colonna con molte modalità): verifica e rispondi con i numeri, senza dire che non vedi i dati.
  Fai poche interrogazioni mirate.
- Prima di dire che qualcosa si può o non si può ottenere, guarda "data_facts" (valori presenti nelle colonne,
  log di modifiche con quante impostazioni/rimozioni/modifiche per campo): se nei file un caso non c'è (es. nessuna
  rimozione di un blocco), dillo con i numeri invece di proporre soluzioni che non lo farebbero comparire.
- I nomi di oggetti, eventi e attività devono essere nomi di business corretti nella lingua indicata da "language": proponi
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

TOOLS.append({
    "name": "add_computed",
    "description": "Propone una colonna calcolata. rule=change: per un log di modifiche (field, old, new), la colonna "
                   "vale SET/REMOVE/CHANGE + campo e diventa la colonna attività di event_type, con un nome per valore. "
                   "rule=slice: la parte (start da 0, length) di una colonna con un valore composto, usata per collegare "
                   "event_type a link_object_type.",
    "input_schema": {
        "type": "object",
        "properties": {
            "rule": {"type": "string", "enum": ["change", "slice"]}, "table": {"type": "string"},
            "field": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
            "column": {"type": "string"}, "start": {"type": "integer"}, "length": {"type": "integer"},
            "event_type": {"type": "string"}, "link_object_type": {"type": "string"},
            "names": {"type": "object", "additionalProperties": {"type": "string"},
                      "description": "Per rule=change: nome dell'attività per ogni valore (es. \"SET MANSP\": \"Set Dunning Block\")."},
            "why": {"type": "string"},
        },
        "required": ["rule", "table", "event_type", "why"],
    },
})

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


def data_facts(tables: list, tables_data: dict[str, list[dict]], profile: dict | None) -> dict:
    """Cosa c'e' davvero nei file: per ogni tabella i valori delle colonne con poche modalita' (con quante righe),
    i log di modifiche (per campo: impostazioni, rimozioni, modifiche) e le chiavi dentro valori composti."""
    from collections import Counter
    out = {}
    for t in tables:
        data = tables_data.get(t.name) or []
        cols = {}
        for c in t.columns:
            counts = Counter(str(r.get(c.name) if r.get(c.name) is not None else "").strip() for r in data)
            if 1 <= len(counts) <= 25:
                cols[c.name] = {k or "(vuoto)": n for k, n in counts.most_common(12)}
        out[t.name] = {"rows": len(data), "low_cardinality_values": cols}
    for t in (profile or {}).get("tables", []):
        if t.get("change_log") and t["name"] in out:
            cl = t["change_log"]
            out[t["name"]]["change_log"] = {"field": cl["field"], "old": cl["old"], "new": cl["new"],
                                            "by_field": cl["by_field"], "suspicious_values": cl["foreign"]}
    for r in (profile or {}).get("relationships", []):
        if r.get("slice") and r["child_table"] in out:
            out[r["child_table"]].setdefault("embedded_keys", []).append(
                {"column": r["slice"]["column"], "start": r["slice"]["start"], "length": r["slice"]["length"],
                 "is_key_of": f"{r['parent_table']}.{r['parent_column']}", "share": r["coverage_pct"]})
    return out


def build_context(*, language: str, process_name: str, assessment: dict, rows: list[dict], model: dict,
                  business_objects: list | None, profile_issues: list, tables: list, link_options: dict,
                  ui_labels: dict, facts: dict | None = None) -> str:
    payload = {
        "data_facts": facts or {},
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
    chars += len(json.dumps(data_query.TOOLS))
    tokens_in = int(chars / _CHARS_PER_TOKEN) + 80
    # le interrogazioni dei dati rileggono il contesto dalla cache (un decimo del prezzo) piu' i risultati
    extra = MAX_DATA_ROUNDS * (tokens_in * 0.1 * price_in + 2500 * price_in + 300 * price_out)
    return {"cost_usd": round((tokens_in * 1.25 * price_in + TYPICAL_OUTPUT_TOKENS * price_out) / 1_000_000, 4),
            "max_usd": round((tokens_in * 1.25 * price_in + MAX_OUTPUT_TOKENS * price_out + extra) / 1_000_000, 4),
            "model": EXPLAIN_MODEL}


def ask(context: str, history: list[dict], tables: dict[str, list[dict]] | None = None) -> dict:
    """Chiama Claude; se chiede di leggere i dati (data_query) esegue le interrogazioni e continua, fino a
    MAX_DATA_ROUNDS volte. Ritorna {"answer", "actions", "cost_usd", "truncated", "queries"}."""
    import anthropic

    messages = _messages(history)
    if not messages:
        raise ValueError("nessuna domanda")
    client = anthropic.Anthropic()
    # il contesto e' lo stesso a ogni giro: in cache costa un decimo
    system = [{"type": "text", "text": SYSTEM_PROMPT + "\nDATI:\n" + context, "cache_control": {"type": "ephemeral"}}]
    tools = TOOLS + (data_query.TOOLS if tables is not None else [])
    price_in, price_out = _price()
    cost, answer, actions, queries, truncated = 0.0, [], [], 0, False
    for round_no in range(MAX_DATA_ROUNDS + 1):
        request = dict(model=EXPLAIN_MODEL, max_tokens=MAX_OUTPUT_TOKENS, system=system, messages=messages,
                       tools=tools if round_no < MAX_DATA_ROUNDS else TOOLS)
        try:
            response = client.messages.create(**request, output_config={"effort": "low"})
        except (TypeError, anthropic.BadRequestError):
            response = client.messages.create(**request)
        u = response.usage
        cost += ((u.input_tokens or 0) * price_in + (getattr(u, "cache_creation_input_tokens", 0) or 0) * price_in * 1.25
                 + (getattr(u, "cache_read_input_tokens", 0) or 0) * price_in * 0.1
                 + (u.output_tokens or 0) * price_out) / 1_000_000
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text").strip()
        if text:
            answer.append(text)
        uses = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
        actions += [{"type": b.name, **(b.input or {})} for b in uses if b.name not in data_query.NAMES]
        truncated = response.stop_reason == "max_tokens"
        data_uses = [b for b in uses if b.name in data_query.NAMES]
        if not data_uses or tables is None:
            break
        # ogni tool_use vuole il suo risultato: i dati per le interrogazioni, una conferma per le proposte
        results = []
        for b in uses:
            if b.name in data_query.NAMES:
                queries += 1
                out = data_query.run(b.name, b.input or {}, tables)
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": json.dumps(out, ensure_ascii=False, default=str)[:12000]})
            else:
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": "Proposta mostrata all'utente, che la confermerà."})
        messages = messages + [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]
    return {"answer": "\n\n".join(answer), "actions": actions, "cost_usd": round(cost, 4), "truncated": truncated,
            "queries": queries}


# ---------- azioni: controllo e applicazione ----------

def _group_of(r: dict):
    el = r["ocel_element"]
    if el.startswith("object_type") and r.get("object_type"):
        return ("obj", r["object_type"])
    if (el.startswith("event_type") or el == "e2o_relationship") and r.get("event_type"):
        return ("evt", r["event_type"])
    return None


def describe(action: dict, rows: list[dict], columns: dict[str, list[str]] | None = None) -> dict | None:
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
    elif kind == "add_computed":
        cols = (columns or {}).get(action.get("table"))
        rule = action.get("rule")
        events = {r.get("event_type") for r in rows if r["ocel_element"] == "event_type.timestamp"
                  and r["source_table"] == action.get("table")}
        if cols is None or action.get("event_type") not in events:
            return None
        if rule == "change":
            if not (action.get("old") in cols and action.get("new") in cols and (not action.get("field") or action["field"] in cols)):
                return None
            lines.append(msg("colonna calcolata in {t}: Imposta / Rimuovi / Modifica + {f} (da {o} → {n}), attività di «{e}»",
                             t=action["table"], f=action.get("field") or "—", o=action["old"], n=action["new"], e=action["event_type"]))
            names = action.get("names") or {}
            if names:
                lines.append(msg("nomi: {v}", v=", ".join(f"{k} → {v}" for k, v in list(names.items())[:8])))
        elif rule == "slice":
            start, length = action.get("start"), action.get("length")
            objs = {r.get("object_type") for r in rows if r["ocel_element"] == "object_type.key"} | {
                v for r in rows if r["ocel_element"] == "object_type.split" for v in (r.get("activity_values") or {}).values() if v}
            if action.get("column") not in cols or not isinstance(start, int) or not isinstance(length, int) or length < 1 \
                    or action.get("link_object_type") not in objs:
                return None
            lines.append(msg("colonna calcolata in {t}: posizioni {a}–{b} di {c}, per collegare «{e}» a {o}", t=action["table"],
                             a=start + 1, b=start + length, c=action["column"], e=action["event_type"], o=action["link_object_type"]))
        else:
            return None
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
    if kind == "add_computed":
        table, event = action["table"], action["event_type"]
        next_id = max((r["row_id"] for r in rows), default=-1) + 1
        why = msg("Colonna calcolata aggiunta con l'assistente.")
        base = {"source_table": table, "object_type": None, "event_type": None, "attribute_name": None, "qualifier": None,
                "related_object_type": None, "confidence": 1.0, "based_on_template": None, "rationale": why,
                "status": "overridden", "original_ai_proposal": None}
        if action["rule"] == "change":
            column = "CHANGE_KIND"
            spec = {"rule": "change", "field": action.get("field") or "", "old": action["old"], "new": action["new"]}
        else:
            column = f"{action['column']}_PART"
            spec = {"rule": "slice", "column": action["column"], "start": str(action["start"]), "length": str(action["length"])}
        rows[:] = [r for r in rows if not (r["ocel_element"] == "table.computed" and r["source_table"] == table
                                          and r["source_column"] == column)]
        rows.append({**base, "row_id": next_id, "source_column": column, "ocel_element": "table.computed", "activity_values": spec})
        if action["rule"] == "change":
            names = {str(k): str(v) for k, v in (action.get("names") or {}).items()} or None
            act = next((r for r in rows if r["ocel_element"] == "event_type.activity" and r["source_table"] == table
                        and r.get("event_type") == event), None)
            if act:
                act.update(source_column=column, activity_values=names, status="overridden", rationale=why)
            else:
                rows.append({**base, "row_id": next_id + 1, "source_column": column, "ocel_element": "event_type.activity",
                             "event_type": event, "activity_values": names})
        else:
            obj = action["link_object_type"]
            rows.append({**base, "row_id": next_id + 1, "source_column": column, "ocel_element": "e2o_relationship",
                         "event_type": event, "related_object_type": obj, "qualifier": f"for {obj.lower()}",
                         "activity_values": None})
        return True
    if kind == "add_link" and add_link:
        before = len(rows)
        add_link(rows, str(action.get("event_type") or ""), str(action.get("object_type") or ""),
                 str(action.get("column") or ""))
        return len(rows) > before or any(
            r["ocel_element"] == "e2o_relationship" and r.get("event_type") == action.get("event_type")
            and r.get("related_object_type") == action.get("object_type") for r in rows)
    return False
