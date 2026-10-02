"""Cosa si potra' analizzare con il dataset che sta per essere generato.

Una sola chiamata AI, fatta in anteprima prima di confermare il mapping, che
legge modello (oggetti, eventi, attributi), statistiche dell'anteprima e
assessment, e restituisce in forma strutturata:
- casi d'uso (process overview, control tower, dettaglio KPI, root cause...)
  con stato possibile / parziale / non possibile e cosa manca;
- KPI calcolabili e non, con definizione e dati mancanti;
- dimensioni di analisi disponibili e quelle utili ma assenti;
- suggerimenti di integrazione dati, con il beneficio di ciascuno.
Senza AI (mapper euristico o errore) c'e' una versione deterministica ridotta.
Il risultato viene salvato insieme al dataset (IngestionConfig.analysis_capabilities).
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel

from app.config import ANTHROPIC_MODEL

Status = Literal["possibile", "parziale", "non_possibile"]


class UseCase(BaseModel):
    area: Literal["process_overview", "control_tower", "kpi_detail", "root_cause", "conformance", "altro"]
    title: str
    status: Status
    what_you_can_do: str
    missing: list[str]


class Kpi(BaseModel):
    name: str
    definition: str
    computed_as: str
    status: Status
    missing: list[str]


class Dimension(BaseModel):
    name: str
    source: str | None
    available: bool
    note: str


class DataSuggestion(BaseModel):
    what: str
    why: str
    how: str
    priority: Literal["alta", "media", "bassa"]


class Capabilities(BaseModel):
    summary: str
    use_cases: list[UseCase]
    kpis: list[Kpi]
    dimensions: list[Dimension]
    data_suggestions: list[DataSuggestion]


# Esempi di riferimento per famiglia di processo: guidano la scelta dei KPI
# senza imporli (l'AI li adatta al processo reale e ai dati disponibili).
REFERENCE_KPIS = {
    "Purchase-to-Pay": [
        "Lead time ordine -> entrata merce", "Lead time fattura -> pagamento", "Lead time end-to-end (RdA/ordine -> pagamento)",
        "% pagamenti in ritardo / in anticipo rispetto alla scadenza", "% fatture senza ordine o con ordine creato dopo la fattura (maverick buying)",
        "% three-way match al primo colpo", "Modifiche d'ordine (prezzo, quantita', data) per ordine", "% fatture bloccate e tempo di sblocco",
        "% attivita' automatiche vs manuali", "Sconti cassa persi",
    ],
    "Order-to-Cash / Crediti": [
        "DSO", "Lead time ordine -> consegna -> fattura -> incasso", "% incassi in ritardo e ritardo medio",
        "% fatture con nota di credito", "Solleciti per fattura", "% ordini consegnati in tempo e completi (OTIF)",
        "Blocchi di credito e tempo di rilascio", "% partite aperte scadute per fascia",
    ],
}

_SYSTEM = """\
Sei un esperto di process mining che affianca un data engineer nella preparazione di un dataset
OCEL 2.0 (object-centric). Ricevi: nome del processo, assessment (obiettivi, domande di business,
oggetto principale, sistemi, disponibilita' di storico modifiche e orari), il modello di dati che sta
per essere generato (tipi di oggetto con attributi, tipi di evento con attributi e oggetti collegati),
statistiche reali dell'anteprima (casi, eventi per tipo, passaggi frequenti, tempi, varianti) e gli
attributi candidati a dimensioni di analisi (con numero di valori distinti ed esempi).

Valuta in modo ONESTO cosa sara' possibile analizzare con QUESTI dati, per un utente di business:
- use_cases: includi sempre process_overview (performance: tempi, volumi, colli di bottiglia,
  filtrabili per dimensioni), control_tower (KPI principali), kpi_detail (dettaglio per KPI/caso d'uso),
  root_cause (analisi delle cause rispetto alle dimensioni disponibili) e, se sensato, conformance.
  status = possibile solo se eventi e attributi necessari ci sono davvero; parziale se manca qualcosa
  ma si ottiene un risultato utile; non_possibile altrimenti. In missing scrivi cosa manca in concreto.
- kpis: 6-10 KPI rilevanti per il processo (usa gli esempi di riferimento come ispirazione, adattali);
  computed_as spiega con quali eventi/attributi del modello si calcola; per quelli non calcolabili
  indica in missing i dati necessari.
- dimensions: le dimensioni di filtro disponibili (dagli attributi candidati, con nome leggibile e
  source "Tipo.attributo") E quelle tipiche del processo che mancano (available=false), es. per il P2P
  categoria merceologica, area geografica del fornitore, area/organizzazione del buyer, tipo documento.
- data_suggestions: integrazioni di dati che sbloccano KPI o casi d'uso importanti oggi non possibili o
  parziali (es. storico modifiche, anagrafica fornitori con paese, scadenze di pagamento, tipo utente
  per l'automazione); how indica tabelle/campi tipici del sistema dichiarato (es. SAP: LFA1.LAND1,
  CDHDR/CDPOS, BSEG.ZFBDT) se il sistema e' noto, altrimenti in modo generico. Ordina per priorita'.
- summary: 2-3 frasi per l'utente: cosa potra' fare subito e il limite principale.
Scrivi tutto in italiano, in linguaggio comprensibile a un utente di business. Non inventare dati che
non risultano dal modello.
"""


def assess_capabilities(process_context: dict, model_summary: dict) -> Capabilities:
    import anthropic

    client = anthropic.Anthropic()
    payload = {
        "process_name": process_context.get("process_name"),
        "assessment": process_context.get("assessment"),
        "bpmn_activities": process_context.get("bpmn_activities"),
        "dataset_model_and_preview": model_summary,
        "reference_kpi_examples": REFERENCE_KPIS,
    }
    response = client.messages.parse(
        model=ANTHROPIC_MODEL,
        max_tokens=12000,
        system=_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        output_format=Capabilities,
    )
    if response.stop_reason != "end_turn" or response.parsed_output is None:
        raise RuntimeError(f"valutazione non completata (stop_reason={response.stop_reason})")
    return response.parsed_output


def fallback_capabilities(model_summary: dict) -> Capabilities:
    """Versione deterministica quando l'AI non e' disponibile: solo cio' che si
    deduce con certezza dal modello, senza catalogo KPI di processo."""
    activities = model_summary.get("activity_order", [])
    dims = model_summary.get("dimension_candidates", [])
    has_flow = len(activities) >= 2 and model_summary.get("cases", 0) > 0
    dim_names = [d["attribute"] for d in dims]
    return Capabilities(
        summary="Valutazione automatica di base (AI non disponibile): elenca solo ciò che si deduce "
                "direttamente dal modello. KPI di processo e suggerimenti di integrazione richiedono l'AI.",
        use_cases=[
            UseCase(area="process_overview", title="Process overview", status="possibile" if has_flow else "non_possibile",
                    what_you_can_do=f"Flussi, volumi e tempi tra {len(activities)} attività"
                                    + (f", filtrabili per {', '.join(dim_names[:6])}" if dim_names else ""),
                    missing=[] if has_flow else ["almeno due tipi di evento collegati all'oggetto principale"]),
            UseCase(area="root_cause", title="Analisi delle cause", status="parziale" if dims else "non_possibile",
                    what_you_can_do="Confronto di tempi e varianti tra i valori delle dimensioni disponibili.",
                    missing=[] if dims else ["attributi descrittivi (categorie, organizzazione, controparte)"]),
        ],
        kpis=[Kpi(name="Tempo di attraversamento", definition="Tempo dal primo all'ultimo evento di ogni caso",
                  computed_as=f"{activities[0]} → {activities[-1]}" if has_flow else "—",
                  status="possibile" if has_flow else "non_possibile", missing=[])],
        dimensions=[Dimension(name=d["attribute"], source=d["attribute"], available=True,
                              note=f"{d['distinct_values']} valori, es. {', '.join(map(str, d['examples'][:3]))}") for d in dims],
        data_suggestions=[],
    )
