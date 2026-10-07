"""Assistente dell'analisi: risponde alle domande del Data Analyst sul Process Explorer.

Esempi: «cos'è un Accounting Document?», «perché Customer è trasversale?», «quali
passaggi sono più lenti?». Conosce il dataset (tipi di oggetto, attività, da quali
tabelle e colonne sorgente vengono), il contesto del processo (assessment) e la vista
che l'utente sta guardando (tipi scelti, collegamenti visibili con numeri e tempi).

Può anche proporre un nome personale per un tipo di oggetto o un'attività (es.
«chiamalo Invoice»): lo propone con lo strumento rename_label e l'utente conferma
nella pagina. Il nome vale solo nella sua analisi (tabella AnalysisAlias), il dataset
non cambia.

La conversazione vive nel browser: a ogni domanda arrivano gli ultimi messaggi.
Prima dell'invio l'utente vede un costo indicativo.
"""
from __future__ import annotations

import json

from app.config import EXPLAIN_MODEL
from app.services.ai_mapping import _CHARS_PER_TOKEN, _FALLBACK_PRICE, _PRICES_USD_PER_MTOK

MAX_OUTPUT_TOKENS = 2000
TYPICAL_OUTPUT_TOKENS = 450
MAX_TURNS = 12          # ultimi messaggi della conversazione inviati a Claude
MAX_EDGES = 40          # collegamenti della vista passati come contesto

SYSTEM_PROMPT = """\
Sei l'assistente di analisi di un'app di process mining object-centric (OCEL 2.0). L'utente è un Data Analyst,
non necessariamente esperto di process mining né del sistema sorgente, e sta guardando il Process Explorer.

Come funziona il Process Explorer (per spiegare e indicare azioni concrete):
- È una mappa a linee di metropolitana: ogni tipo di oggetto (documento, riga, cliente...) è una linea colorata
  che parte in alto (il tratteggio iniziale indica che l'oggetto non è ancora entrato nel processo) e finisce
  in un cerchio pieno. Le fermate sono le attività (i tipi di evento dell'OCEL): un punto per ogni tipo che ci passa.
- Il numero su una linea è quante volte un oggetto passa da un'attività alla successiva; in modalità «Tempo»
  è il tempo mediano del passaggio. Il dettaglio di un collegamento dà anche media, minimo, massimo.
- Pannello «Controllo del grafo»: tipi di oggetto da spuntare; scheda Attività (spunte o cursore «le più
  frequenti»; le attività tolte vengono saltate e i passaggi ricollegati); scheda Collegamenti (cursore della
  quota più frequente o spunte); ricerca; toccando il nome di una linea la si vede da sola.
- Tipi «trasversali» (es. cliente, fornitore): pochi oggetti in moltissimi eventi di documenti diversi. Di base
  la loro linea passa solo dagli eventi propri (di cui sono l'oggetto di casa o che non appartengono alla storia
  di un documento); negli altri eventi compaiono come numero nella fermata. Un interruttore mostra la linea su
  tutti gli eventi collegati.
- Due linee si incrociano in una fermata solo se lo stesso evento è collegato a entrambi i tipi di oggetto.
- Il dataset si prepara nel modulo Ingestion (Data Engineer): lì si cambiano oggetti, eventi e collegamenti.

Cosa fare:
- Le etichette dell'app tra «» in queste istruzioni sono in italiano. Quando nomini un pulsante, un pannello,
  una scheda o un passo dell'app usa SEMPRE il testo corrispondente in "ui_labels": è quello che l'utente vede
  nella sua lingua. Non usare mai le etichette italiane se "language" è en.
- Rispondi alla domanda usando i DATI qui sotto: provenienza dai dati sorgente (tabelle, colonne chiave, colonne
  data/attività, collegamenti), numeri della vista, contesto del processo. Per spiegare cos'è un oggetto o
  un'attività unisci la provenienza alle tue conoscenze del sistema sorgente (es. tabelle SAP), dicendo cosa è
  certo dai dati e cosa è interpretazione.
- Usa solo i numeri forniti; se un numero non c'è, dillo e suggerisci come vederlo nell'app. Non inventare funzioni.
- Rinomina: se l'utente chiede di dare un altro nome a un tipo di oggetto o a un'attività, usa lo strumento
  rename_label con il nome originale esatto (quello del dataset) e il nuovo nome, e scrivi in una frase che il
  nome cambierà solo nella sua analisi dopo che avrà premuto il pulsante ui_labels.confirm (il dataset non cambia). Per tornare al
  nome originale usa new_name vuoto. Se il nome originale non è chiaro, chiedi prima quale intende.
- Quando parli di tipi e attività usa i nomi personali dell'utente (aliases), con l'originale tra parentesi la
  prima volta se aiuta.
- Rispondi nella lingua indicata da "language" (it = italiano, en = inglese), in modo semplice e concreto,
  al massimo 200 parole. Testo semplice: niente titoli markdown, al massimo elenchi con "-".
"""

RENAME_TOOL = {
    "name": "rename_label",
    "description": "Propone un nome personale per un tipo di oggetto o un'attività, solo nell'analisi di questo "
                   "utente (il dataset non cambia). L'utente lo conferma nella pagina.",
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["object_type", "activity"]},
            "original": {"type": "string", "description": "Nome esatto nel dataset."},
            "new_name": {"type": "string", "description": "Nuovo nome; vuoto per tornare all'originale."},
        },
        "required": ["kind", "original", "new_name"],
    },
}


def _price() -> tuple[float, float]:
    return _PRICES_USD_PER_MTOK.get(EXPLAIN_MODEL, _FALLBACK_PRICE)


def _round_days(sec):
    return None if sec is None else round(sec / 86400, 2)


def mapping_summary(rows) -> dict:
    """Da quali tabelle e colonne sorgente vengono oggetti, eventi e collegamenti (righe di mapping confermate)."""
    objects: dict[str, dict] = {}
    events: dict[str, dict] = {}
    for r in rows:
        if r.status not in ("confirmed", "overridden"):
            continue
        col = f"{r.source_table}.{r.source_column}" if r.source_column else r.source_table
        if r.ocel_element == "object_type.key" and r.object_type:
            objects.setdefault(r.object_type, {"tables": set(), "key": []})
            objects[r.object_type]["tables"].add(r.source_table)
            objects[r.object_type]["key"].append(col)
        elif r.ocel_element.startswith("event_type") or r.ocel_element == "e2o_relationship":
            name = r.event_type or "?"
            ev = events.setdefault(name, {"table": r.source_table, "time": [], "activity": None, "links": []})
            if r.ocel_element == "event_type.timestamp":
                ev["time"].append(col)
            elif r.ocel_element == "event_type.activity":
                ev["activity"] = {"column": col, "values": r.activity_values}
            elif r.ocel_element == "e2o_relationship":
                ev["links"].append({"object_type": r.related_object_type, "via": col, "qualifier": r.qualifier})
    return {
        "object_types": {k: {"tables": sorted(v["tables"]), "key": v["key"]} for k, v in objects.items()},
        "event_types": events,
    }


# Etichette dell'interfaccia che l'assistente puo' citare (testi italiani = chiavi del catalogo)
UI_LABELS = {
    "confirm": "Conferma", "graph_control": "Controllo del grafo", "object_types": "Tipi di oggetto",
    "activities_tab": "Attività", "connections_tab": "Collegamenti", "most_frequent": "Le più frequenti",
    "search": "Cerca", "frequency": "Frequenza", "time": "Tempo", "legend": "Legenda", "fit": "Adatta alla finestra",
    "cross_cutting_switch": "Tipi trasversali: mostra la linea su tutti gli eventi collegati",
    "cross_cutting_tag": "trasversale", "restore_original_name_button": "↺", "assistant": "Assistente",
    "ask_assistant": "Chiedi all'assistente", "mapping_review": "Revisione mapping", "add_link": "Aggiungi collegamento",
}


def build_context(*, language: str, process_name: str, assessment: dict, dataset: dict, model, graph: dict,
                  mapping: dict, aliases: dict, focus: str | None, ui_labels: dict | None = None) -> str:
    """Contesto in JSON per Claude: processo, dataset, provenienza, vista corrente."""
    types = [{
        "name": t["name"], "objects": t["objects"], "objects_with_events": t["with_events"],
        "related_events": t["events"], "cross_cutting": t["hub"], "avg_events_per_object": t["avg"],
        "own_activities": model.own_activities(t["name"]) if t["hub"] else None,
    } for t in model.type_summary()]
    edges = sorted((e for e in graph["edges"] if e["kept"]), key=lambda e: -e["count"])[:MAX_EDGES]
    view = {
        "selected_types": graph["types"],
        "cross_cutting_mode": graph.get("hub_mode"),
        "activities": graph["activity_list"],
        "activities_by_type": [{"activity": n["label"], "per_type": [
            {"type": x["type"], "events": x["events"], "objects": x["objects"]} for x in n["types"]]}
            for n in graph["nodes"] if n["kind"] == "activity"],
        "visible_connections": [{
            "type": e["type"], "from": e["source"].replace("__start__|", "START of "),
            "to": e["target"].replace("__end__|", "END of "), "transitions": e["count"], "objects": e["objects"],
            "median_days": _round_days((e["duration"] or {}).get("median")),
            "mean_days": _round_days((e["duration"] or {}).get("mean")),
        } for e in edges],
        "connections_total": graph["paths"]["total"],
        "user_is_looking_at": focus or None,
    }
    payload = {
        "language": language,
        "ui_labels": ui_labels or {},
        "process": {"name": process_name, "assessment": assessment},
        "dataset": {**dataset, "object_types": types},
        "source_mapping": mapping,
        "aliases": aliases,
        "view": view,
    }
    text = json.dumps(payload, ensure_ascii=False, default=list, separators=(",", ":"))
    return text[:60000]


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
    """Costo indicativo (risposta tipica) e massimo, in dollari."""
    price_in, price_out = _price()
    chars = len(SYSTEM_PROMPT) + len(context) + len(json.dumps(RENAME_TOOL)) + sum(
        len(m["content"]) for m in _messages(history))
    tokens_in = int(chars / _CHARS_PER_TOKEN) + 80
    typical = (tokens_in * price_in + TYPICAL_OUTPUT_TOKENS * price_out) / 1_000_000
    maximum = (tokens_in * price_in + MAX_OUTPUT_TOKENS * price_out) / 1_000_000
    return {"cost_usd": round(typical, 4), "max_usd": round(maximum, 4), "model": EXPLAIN_MODEL}


def ask(context: str, history: list[dict]) -> dict:
    """Chiama Claude. Ritorna {"answer", "actions", "cost_usd", "truncated"}."""
    import anthropic

    messages = _messages(history)
    if not messages:
        raise ValueError("nessuna domanda")
    client = anthropic.Anthropic()
    request = dict(model=EXPLAIN_MODEL, max_tokens=MAX_OUTPUT_TOKENS,
                   system=SYSTEM_PROMPT + "\nDATI:\n" + context, tools=[RENAME_TOOL], messages=messages)
    try:
        response = client.messages.create(**request, output_config={"effort": "low"})
    except (TypeError, anthropic.BadRequestError):
        response = client.messages.create(**request)
    price_in, price_out = _price()
    usage = response.usage
    cost = ((usage.input_tokens or 0) * price_in + (usage.output_tokens or 0) * price_out) / 1_000_000
    answer = "".join(b.text for b in response.content if getattr(b, "type", "") == "text").strip()
    actions = [{"type": "rename", **b.input} for b in response.content
               if getattr(b, "type", "") == "tool_use" and b.name == "rename_label"]
    return {"answer": answer, "actions": actions, "cost_usd": round(cost, 4),
            "truncated": response.stop_reason == "max_tokens"}
