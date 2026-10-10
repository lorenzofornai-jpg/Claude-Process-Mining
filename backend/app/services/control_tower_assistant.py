"""Assistente della Control Tower: spiega i numeri e propone modifiche a domande, KPI e grafici.

Il Data Analyst scrive cosa vuole («aggiungi un grafico per cliente», «togli la ciambella», «porta l'obiettivo al
15%», «aggiungi una domanda sul DSO», «metti i grafici uno sotto l'altro», «sposta la tabella in cima»); Claude risponde in breve e propone le modifiche con lo strumento
change_control_tower (operazioni nel vocabolario di services/control_tower.py). L'utente le vede, le applica in
anteprima e poi salva: niente cambia senza la sua conferma. Claude puo' leggere il dataset in sola lettura
(data_query) per verificare valori e utenti prima di proporre.
"""
from __future__ import annotations

import json

from app.config import EXPLAIN_MODEL
from app.services import control_tower, data_query
from app.services.ai_mapping import _CHARS_PER_TOKEN, _FALLBACK_PRICE, _PRICES_USD_PER_MTOK
from app.services.analysis_assistant import _messages

MAX_OUTPUT_TOKENS = 6000
TYPICAL_OUTPUT_TOKENS = 900

SYSTEM_PROMPT = """\
Sei l'assistente della «Control Tower» di un'app di process mining object-centric: la vista per il management che
risponde alle domande di business dell'assessment con KPI calcolati sul dataset. L'utente è il Data Analyst.
Primo livello: la sintesi, una card per domanda con i suoi KPI principali. Secondo livello: il dettaglio di una
domanda, con tutti i KPI e i grafici. "view" dice cosa sta guardando (level = summary | question | definitions,
section_id = la domanda aperta, period = mesi del periodo).

Cosa fai:
- Spieghi i numeri (usa "values": valori del periodo, numeratore, denominatore, periodo precedente, obiettivo) e come
  si calcola un KPI.
- Proponi modifiche quando l'utente le chiede: domande, KPI (statistiche), grafici, obiettivi, KPI della sintesi e
  impaginazione della pagina (ordine, colonne, larghezze e altezze, cosa mostrare).
  Usa SEMPRE lo strumento change_control_tower: l'utente vede la proposta, la applica in anteprima e poi salva.
  Non dire mai che la modifica è già fatta. Se la richiesta è ambigua, chiedi prima.
- Se l'utente guarda una domanda (view.section_id), le aggiunte vanno lì, salvo diversa indicazione.
- Se cambi come si calcola un KPI, aggiorna anche "description" in modo coerente. Cambiare domande e KPI le rimette
  da approvare; i grafici no.
- Operazioni (campo operations, in ordine):
  * add_section {section:{id, question, why, missing, kpis:[], charts:[]}}; update_section {section_id,
    changes:{question?, why?, missing?}}; remove_section {section_id}
  * add_kpi {section_id, kpi:{...}, summary:bool}; update_kpi {kpi_id, changes:{campo: valore, null = togli}};
    remove_kpi {kpi_id}
  * add_chart {section_id, chart:{...}, position?}; update_chart {chart_id, changes:{...}}; remove_chart {chart_id}
  * set_summary {section_id, kpi_ids:[...]} — quali KPI della domanda compaiono nella sintesi.
  * move_section {section_id, position}; move_kpi {kpi_id, position}; move_chart {chart_id, position, section_id?}
    — ordine (position 0 = in testa); move_chart con section_id sposta il grafico in un'altra domanda.
  * update_layout {changes:{...}} — impaginazione della pagina (null = torna al valore di base):
    summary_columns 1|2|3 (card delle domande per riga nella sintesi), summary_trend true|false (andamento mensile
    sulle card), kpi_columns 0|1|2|3|4 (KPI per riga nel dettaglio, 0 = automatico), chart_columns 1|2|3 (grafici
    per riga nel dettaglio), show_why true|false («perché conta» sotto la domanda). Valori attuali in spec.layout.
  Un KPI nuovo si può usare in un grafico nella stessa proposta con il suo id.
- Impaginazione dei singoli grafici (update_chart changes): width "auto" | "full" (a tutta larghezza; l'andamento
  mensile lo è di base), height "small" | "medium" | "large", per barre e tabelle top (quanti gruppi, 3-30) e
  sort "impact" | "value_desc" | "value_asc" | "label"; kind cambia il tipo di grafico (trend, bars, donut, table).
- Puoi quindi cambiare il layout: ordine di domande, KPI e grafici, colonne, larghezze, altezze, cosa mostrare
  nella sintesi. Non puoi cambiare colori, caratteri o lo stile grafico dell'app: se te lo chiedono, dillo e proponi
  l'alternativa più vicina. Cambiare impaginazione e grafici non chiede una nuova approvazione.
- Vocabolario delle definizioni (uguale a quello delle definizioni attuali in "spec"):
""" + control_tower.GRAMMAR + """
Regole:
- Usa solo nomi di tipi, attività e attributi del catalogo ("catalog"); un importo è un attributo "number".
- Con query_rows sulle tabelle del dataset (una per tipo di oggetto e "events") verifichi valori, utenti, attributi
  prima di proporre, se serve. Poche interrogazioni mirate.
- Rispondi nella lingua "language" (it = italiano, en = inglese), in modo semplice, al massimo 150 parole.
- I testi nelle definizioni (name, description, question, why, missing, title, label, reason) vanno SEMPRE in
  italiano e in inglese: {"it": "...", "en": "..."}, chiari per un dirigente.
  Testo semplice: niente titoli markdown, al massimo elenchi con "-".
"""

TOOL = {
    "name": "change_control_tower",
    "description": "Propone modifiche alla Control Tower (domande, KPI, grafici, obiettivi, KPI della sintesi, "
                   "impaginazione: ordine, colonne, larghezze e altezze dei grafici). "
                   "L'utente le vede e decide se applicarle.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Cosa cambia, in una o due frasi per l'utente."},
            "operations": {"type": "array", "items": {"type": "object", "properties": {
                "type": {"type": "string", "enum": list(control_tower.OPERATIONS)},
                "section_id": {"type": "string"}, "kpi_id": {"type": "string"}, "chart_id": {"type": "string"},
                "section": {"type": "object"}, "kpi": {"type": "object"}, "chart": {"type": "object"},
                "changes": {"type": "object"}, "summary": {"type": "boolean"},
                "kpi_ids": {"type": "array", "items": {"type": "string"}}, "position": {"type": "integer"}},
                "required": ["type"]}},
        },
        "required": ["summary", "operations"],
    },
}


def _price() -> tuple[float, float]:
    return _PRICES_USD_PER_MTOK.get(EXPLAIN_MODEL, _FALLBACK_PRICE)


def values_view(values: dict | None) -> dict:
    """I numeri della pagina, compatti."""
    if not values:
        return {}
    r = lambda x: round(x, 4) if isinstance(x, float) else x  # noqa: E731
    return {
        "period_months": values.get("months") or "all",
        "previous_period_months": values.get("previous_months"),
        "kpis": {k: ({"not_computable": v["issues"]} if v.get("issues") else
                     {f: r(v.get(f)) for f in ("value", "num", "den", "amount", "previous", "status", "target", "measured")
                      if v.get(f) is not None})
                 for k, v in (values.get("kpis") or {}).items()},
    }


def build_context(*, language: str, process_name: str, assessment: dict, cat: dict, spec: dict | None,
                  values: dict | None, view: dict) -> str:
    payload = {
        "language": language,
        "process": {"name": process_name, "business_questions": assessment.get("key_questions"),
                    "kpis_in_use": assessment.get("kpis")},
        "catalog": control_tower._compact_catalog(cat),
        "spec": spec,
        "values": values_view(values),
        "view": view,
    }
    return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))[:90000]


def estimate(context: str, history: list[dict]) -> dict:
    price_in, price_out = _price()
    chars = len(SYSTEM_PROMPT) + len(context) + sum(len(m["content"]) for m in _messages(history))
    chars += len(json.dumps(data_query.TOOLS)) + len(json.dumps(TOOL))
    tokens_in = int(chars / _CHARS_PER_TOKEN) + 80
    typical = (tokens_in * 1.25 * price_in + TYPICAL_OUTPUT_TOKENS * price_out) / 1_000_000
    maximum = ((tokens_in * 1.25 * price_in + MAX_OUTPUT_TOKENS * price_out) / 1_000_000
               + data_query.extra_cost(tokens_in, (price_in, price_out)))
    return {"cost_usd": round(typical, 4), "max_usd": round(maximum, 4), "model": EXPLAIN_MODEL}


def ask(context: str, history: list[dict], tables: dict[str, list[dict]] | None = None) -> dict:
    """Ritorna {"answer", "actions", "cost_usd", "truncated", "queries"}; actions = proposte di modifica."""
    messages = _messages(history)
    if not messages:
        raise ValueError("nessuna domanda")
    return data_query.converse(model=EXPLAIN_MODEL, system_text=SYSTEM_PROMPT + "\nDATI:\n" + context,
                               messages=messages, max_tokens=MAX_OUTPUT_TOKENS, price=_price(), tables=tables,
                               action_tools=[TOOL])
