"""Assistente dell'analisi: risponde alle domande del Data Analyst sul Process Explorer e sulla Process Overview.

Esempi: «cos'è un Accounting Document?», «perché Customer è trasversale?», «quali passaggi sono piu'
lenti?», «perche' il tempo di attraversamento e' 31 giorni?», «cosa sono le pratiche aperte?». Conosce il
dataset (tipi di oggetto, attivita', da quali tabelle e colonne sorgente vengono, perche' quegli oggetti di
business), il contesto del processo (assessment, obiettivo misurabile) e quello che l'utente sta guardando
(nel Process Explorer i tipi e i collegamenti visibili; nella Process Overview oggetto guida, tempi, varianti).

Non rinomina nulla: i nomi arrivano corretti dall'ingestion. Se un nome e' poco chiaro lo spiega e dice dove
correggerlo (oggetti di business, revisione del mapping).

La conversazione vive nel browser: a ogni domanda arrivano gli ultimi messaggi. Prima dell'invio l'utente
vede un costo indicativo.
"""
from __future__ import annotations

import json

from app.config import EXPLAIN_MODEL
from app.services import data_query
from app.services.ai_mapping import _CHARS_PER_TOKEN, _FALLBACK_PRICE, _PRICES_USD_PER_MTOK

MAX_OUTPUT_TOKENS = 2000
TYPICAL_OUTPUT_TOKENS = 450
MAX_TURNS = 12          # ultimi messaggi della conversazione inviati a Claude
MAX_EDGES = 40          # collegamenti della vista passati come contesto

SYSTEM_PROMPT = """\
Sei l'assistente di analisi di un'app di process mining object-centric (OCEL 2.0). L'utente è un Data Analyst,
non necessariamente esperto di process mining né del sistema sorgente. "page" dice quale pagina sta guardando.

Process Overview (page = overview): volumi, tempi e varianti visti da un «Oggetto guida» (un tipo di oggetto
scelto dall'utente, es. la fattura). Il «Tempo di attraversamento» si misura in tre modi: «Da inizio a fine»
(l'obiettivo misurabile: dalla prima attività di inizio alla prima di fine successiva; chi non ha ancora la fine è
tra gli «Aperti», con la sua età all'ultimo evento del dataset), «Solo l'oggetto» (dal primo all'ultimo evento
dell'oggetto, aperti compresi), «Con gli oggetti collegati» (fino all'ultimo evento anche degli oggetti collegati,
esclusi i tipi trasversali). Le varianti sono le sequenze di attività dell'oggetto guida (ripetizioni consecutive
raggruppate); l'happy path è la più frequente; «Altre attività frequenti» sono quelle fuori dall'happy path.
L'obiettivo misurabile si imposta nel riquadro «Obiettivo misurabile».

Process Explorer (page = explorer): mappa a linee di metropolitana. Ogni tipo di oggetto è una linea colorata che
parte in alto (tratteggio iniziale: l'oggetto non è ancora entrato nel processo) e finisce in un cerchio pieno;
le fermate sono le attività, un punto per ogni tipo che ci passa. Il numero su una linea è quante volte un oggetto
passa da un'attività alla successiva; in modalità «Tempo» è il tempo mediano. Pannello «Controllo del grafo»:
tipi di oggetto, scheda Attività (spunte o cursore «le più frequenti»), scheda Collegamenti, ricerca; toccando il
nome di una linea la si vede da sola. Tipi «trasversali» (es. cliente): pochi oggetti in moltissimi eventi di
documenti diversi; di base la linea passa solo dagli eventi propri, un interruttore la mostra su tutti.
Due linee si incrociano in una fermata solo se lo stesso evento è collegato a entrambi i tipi.

Dati: con query_rows e compare_columns leggi il dataset in sola lettura. Tabelle:
una per tipo di oggetto (id, attributi, events, first, last, duration_days, activities = attività in ordine) e
"events" (id, activity, time, objects, attributi dell'evento). Usale quando la risposta richiede numeri o elenchi che
la pagina non mostra (es. quali ordini di un cliente sono ancora aperti, quanti oggetti hanno un'attività); fai poche
interrogazioni mirate e cita i numeri.

Il dataset si prepara nel modulo Ingestion (Data Engineer): passo «Oggetti di business» (quali oggetti e con che
nome), «Revisione mapping» (eventi, attività, collegamenti). Lì si correggono nomi, oggetti e collegamenti:
nell'analisi i nomi non si cambiano.

Cosa fare:
- Le etichette dell'app tra «» qui sopra sono in italiano. Quando nomini un pulsante, un pannello o un passo dell'app
  usa SEMPRE il testo corrispondente in "ui_labels": è quello che l'utente vede nella sua lingua.
- Rispondi usando i DATI: provenienza dai dati sorgente (tabelle, colonne, collegamenti, oggetti di business e
  perché sono stati scelti), numeri della pagina, contesto del processo e obiettivo misurabile. Per spiegare cos'è
  un oggetto o un'attività unisci la provenienza alle tue conoscenze del sistema sorgente (es. tabelle SAP),
  dicendo cosa è certo dai dati e cosa è interpretazione.
- Usa solo i numeri forniti o letti con le interrogazioni; se un numero non c'è, dillo e suggerisci come vederlo
  nell'app. Non inventare funzioni.
- Se l'utente vuole un altro nome per un oggetto o un'attività, spiega che i nomi si decidono nell'ingestion
  (ui_labels.business_objects_step o ui_labels.mapping_review) e che nell'analisi non si cambiano.
- Rispondi nella lingua indicata da "language" (it = italiano, en = inglese), in modo semplice e concreto,
  al massimo 200 parole, in un italiano (o inglese) corretto. Testo semplice: niente titoli markdown, al massimo
  elenchi con "-".
"""

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
        elif r.ocel_element == "object_type.split" and r.object_type:
            # divisione per valore: ogni tipo prodotto viene dalle righe con quei valori della colonna
            objects.setdefault(r.object_type, {"tables": set(), "key": []})
            for value, name in (r.activity_values or {}).items():
                if name:
                    sub = objects.setdefault(name, {"tables": set(), "key": [], "filter": []})
                    sub["tables"].add(r.source_table)
                    sub.setdefault("filter", []).append(f"{col} = {value}")
                    sub["split_of"] = r.object_type
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
        "object_types": {k: {"tables": sorted(v["tables"]), "key": v["key"] or objects.get(v.get("split_of"), {}).get("key", []),
                             **({"rows_where": v["filter"], "split_of": v["split_of"]} if v.get("filter") else {})}
                         for k, v in objects.items()},
        "event_types": events,
    }


# Etichette dell'interfaccia che l'assistente puo' citare (testi italiani = chiavi del catalogo)
UI_LABELS = {
    "confirm": "Conferma", "graph_control": "Controllo del grafo", "object_types": "Tipi di oggetto",
    "activities_tab": "Attività", "connections_tab": "Collegamenti", "most_frequent": "Le più frequenti",
    "search": "Cerca", "frequency": "Frequenza", "time": "Tempo", "legend": "Legenda", "fit": "Adatta alla finestra",
    "cross_cutting_switch": "Tipi trasversali: mostra la linea su tutti gli eventi collegati",
    "cross_cutting_tag": "trasversale", "assistant": "Assistente",
    "ask_assistant": "Chiedi all'assistente", "mapping_review": "Revisione mapping", "add_link": "Aggiungi collegamento",
    "business_objects_step": "Oggetti di business", "lead_object": "Oggetto guida",
    "throughput_time": "Tempo di attraversamento", "start_to_end": "Da inizio a fine", "object_only": "Solo l'oggetto",
    "with_related": "Con gli oggetti collegati", "open_cases": "Aperti", "measurable_objective": "Obiettivo misurabile",
    "variants": "Varianti", "happy_path": "Happy path", "other_activities": "Altre attività frequenti",
}


def explorer_view(graph: dict, focus: str | None) -> dict:
    """Cosa mostra il Process Explorer: tipi scelti, attivita', collegamenti visibili con numeri e tempi."""
    edges = sorted((e for e in graph["edges"] if e["kept"]), key=lambda e: -e["count"])[:MAX_EDGES]
    return {
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


def overview_view(ov: dict | None, focus: str | None) -> dict:
    """Cosa mostra la Process Overview: oggetto guida, misura del tempo, volumi, tempi, aperti, varianti."""
    if not ov:
        return {"lead_object": None, "user_is_looking_at": focus or None}
    st = ov["throughput"]["stats"] or {}
    days = lambda sec: _round_days(sec) if sec is not None else None  # noqa: E731
    return {
        "lead_object": ov["lead"], "throughput_scope": ov["scope"],
        "objective_filter": ({"attribute": ov["objective"]["filter_attribute"], "values": ov["objective"]["filter_values"]}
                             if ov.get("objective") and ov["objective"].get("filter_attribute") else None),
        "related_scope_adds_events_for_objects": ov.get("related_adds"),
        "volumes": {k: ov["volumes"][k] for k in ("objects", "events", "events_per_object")},
        "objects_started_per_month": ov["volumes"]["months"],
        "throughput_days": {k: days(st.get(k)) for k in ("median", "mean", "min", "max", "p90")} if st else None,
        "throughput_histogram_days": [{"from": round(b["from"], 1), "to": round(b["to"], 1), "objects": b["count"]}
                                      for b in ov["throughput"]["histogram"]],
        "open_cases": ({"count": ov["open"]["count"], "median_age_days": days(ov["open"]["median_age"]),
                        "max_age_days": days(ov["open"]["max_age"])} if ov.get("open") else None),
        "linked_objects_per_lead": [{"type": r["type"], "avg": round(r["avg"], 2), "share": round(r["share"], 3),
                                     "cross_cutting": r["hub"]} for r in ov["relations"]],
        "variants_total": ov["variants_total"],
        "top_variants": [{"steps": [x["activity"] + (" ×" if x["repeated"] else "") for x in v["steps"]],
                          "objects": v["count"], "share": round(v["share"], 3), "median_days": days(v["median"])}
                         for v in ov["variants"][:12]],
        "happy_path": [x["activity"] for x in ov["happy_path"]["steps"]] if ov.get("happy_path") else None,
        "other_frequent_activities": [{"activity": a["activity"], "objects": a["objects"], "share": round(a["share"], 3)}
                                      for a in ov["other_activities"]],
        "object_types": [{"name": t["name"], "objects": t["objects"], "cross_cutting": t["hub"],
                          "median_lifetime_days": days(t["median_lifetime"])} for t in ov["types"]],
        "user_is_looking_at": focus or None,
    }


def build_context(*, language: str, process_name: str, assessment: dict, dataset: dict, model, mapping: dict,
                  page: str, view: dict, ui_labels: dict | None = None, objective: dict | None = None) -> str:
    """Contesto in JSON per Claude: processo, dataset, provenienza, pagina e cosa mostra."""
    types = [{
        "name": t["name"], "objects": t["objects"], "objects_with_events": t["with_events"],
        "related_events": t["events"], "cross_cutting": t["hub"], "avg_events_per_object": t["avg"],
        "own_activities": model.own_activities(t["name"]) if t["hub"] else None,
    } for t in model.type_summary()]
    payload = {
        "language": language,
        "page": page,
        "ui_labels": ui_labels or {},
        "process": {"name": process_name, "assessment": assessment},
        "dataset": {**dataset, "object_types": types},
        "source_mapping": mapping,
        # obiettivo misurabile salvato: oggetto, filtro, attivita' di inizio e fine (il tempo che conta per il business)
        "measurable_objective": objective,
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
    chars = len(SYSTEM_PROMPT) + len(context) + sum(
        len(m["content"]) for m in _messages(history))
    chars += len(json.dumps(data_query.TOOLS))
    tokens_in = int(chars / _CHARS_PER_TOKEN) + 80
    # il contesto va in cache (scrittura: +25%); le interrogazioni dei dati lo rileggono a un decimo
    typical = (tokens_in * 1.25 * price_in + TYPICAL_OUTPUT_TOKENS * price_out) / 1_000_000
    maximum = ((tokens_in * 1.25 * price_in + MAX_OUTPUT_TOKENS * price_out) / 1_000_000
               + data_query.extra_cost(tokens_in, (price_in, price_out)))
    return {"cost_usd": round(typical, 4), "max_usd": round(maximum, 4), "model": EXPLAIN_MODEL}


def ask(context: str, history: list[dict], tables: dict[str, list[dict]] | None = None) -> dict:
    """Chiama Claude, che puo' leggere il dataset (tables) prima di rispondere.
    Ritorna {"answer", "cost_usd", "truncated", "queries"}."""
    messages = _messages(history)
    if not messages:
        raise ValueError("nessuna domanda")
    out = data_query.converse(model=EXPLAIN_MODEL, system_text=SYSTEM_PROMPT + "\nDATI:\n" + context,
                              messages=messages, max_tokens=MAX_OUTPUT_TOKENS, price=_price(), tables=tables)
    out.pop("actions", None)
    return out
