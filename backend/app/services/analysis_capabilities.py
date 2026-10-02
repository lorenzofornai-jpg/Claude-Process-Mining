"""Cosa si potra' analizzare con il dataset che sta per essere generato.

Una sola chiamata AI, fatta in anteprima prima di confermare il mapping, che
legge modello (oggetti, eventi, attributi), statistiche dell'anteprima e
assessment, e restituisce in forma strutturata:
- casi d'uso (process overview, control tower, dettaglio KPI, root cause...)
  con stato possibile / parziale / non possibile e cosa manca;
- 5-7 KPI di business calcolabili e non, con definizione e dati mancanti;
  il data engineer puo' chiederne altri (check_requested_kpis: solo verdetto e dati da integrare);
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
from app.services.business_kpi_library import LIBRARY

Status = Literal["possibile", "parziale", "non_possibile"]


class UseCase(BaseModel):
    area: Literal["process_overview", "control_tower", "kpi_detail", "root_cause", "conformance", "altro"]
    title: str
    status: Status
    what_you_can_do: str
    missing: list[str]


BusinessObjective = Literal[
    "cassa_capitale_circolante", "costi_efficienza", "rischio_compliance", "clienti_fornitori", "ricavi_margini",
]


class Kpi(BaseModel):
    objective: BusinessObjective   # obiettivo di business a cui il KPI risponde
    name: str                      # nome di business, come lo usa il management (es. "DSO", "Touchless collection rate")
    owner: str                     # chi lo segue (CFO, COO, CPO, Credit Manager...)
    executive_question: str        # la domanda che si pone il C-level
    business_value: str            # leva di valore: cosa si guadagna migliorandolo, in termini economici/operativi
    benchmark: str                 # riferimento indicativo di mercato o "" se non significativo
    process_mining_view: str       # cosa aggiunge il process mining rispetto a un report tradizionale
    definition: str
    computed_as: str               # con quali eventi/attributi del modello (o cosa servirebbe)
    status: Status                 # possibile = osservabile con questi dati
    missing: list[str]             # dati che servirebbero (vuoto se osservabile)
    breakdowns: list[str]          # dimensioni disponibili con cui scomporlo


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
    process_family: str          # famiglia di processo riconosciuta (es. "Accounts Receivable")
    summary: str
    use_cases: list[UseCase]
    kpis: list[Kpi]
    dimensions: list[Dimension]
    data_suggestions: list[DataSuggestion]


_SYSTEM = """\
Sei un partner di una societa' di consulenza esperto di process mining e di performance management.
Prepari, per un dataset OCEL 2.0 che un data engineer sta per generare, la parte "KPI e analisi
possibili" che verra' letta anche dal management (CFO, COO, CPO, Credit Manager).

Ricevi: nome del processo, assessment (obiettivi, domande di business, oggetto principale, sistemi,
storico modifiche, orari), il modello di dati (tipi di oggetto con attributi, tipi di evento con
attributi e oggetti collegati), statistiche reali dell'anteprima e gli attributi candidati a dimensioni.
Ricevi anche una libreria di riferimento di KPI di business per i processi piu' noti.

1. Riconosci la famiglia di processo (process_family) da nome, assessment, tabelle e attivita' (es.
   tabelle SAP BSID/BSAD/KNA1 -> Accounts Receivable; EKKO/EKBE/RBKP -> Purchase-to-Pay; VBAK/LIKP/VBRK
   -> Order-to-Cash). Le informazioni dell'utente possono essere scarse: usa la tua conoscenza del
   processo per proporre comunque i KPI che contano.
2. kpis: e' la parte piu' importante. Solo 5-7 KPI / casi di valore: i piu' rilevanti che un C-level
   considera prioritari per QUESTO processo (gli altri li chiedera' il data engineer, se servono), nel suo linguaggio e con i suoi obiettivi di miglioramento (capitale circolante,
   costo per transazione, automazione, rischio, esperienza di clienti e fornitori, ricavi). Parti dalla
   libreria per la famiglia riconosciuta (es. per Accounts Receivable: DSO, touchless collection,
   payment terms mismatch...) e completala; NON limitarti a tempi di attraversamento e colli di
   bottiglia: quelli sono strumenti, non obiettivi. Scegli per rilevanza, non per osservabilita': se un
   KPI chiave NON e' osservabile con questi dati includilo comunque, mostra cosa manca. Per ognuno:
   - objective: l'obiettivo di business; name: il nome usato dal management; owner: chi lo segue;
   - executive_question: la domanda che si pone il C-level, in prima persona;
   - business_value: cosa vale migliorarlo, in termini economici/operativi concreti (es. "ogni giorno
     di DSO in meno libera cassa pari a un giorno di fatturato");
   - benchmark: un riferimento indicativo di mercato se esiste ed e' ragionevolmente noto (es. range
     tipici o best-in-class), dichiarandolo come indicativo; stringa vuota se non lo sai;
   - process_mining_view: cosa aggiunge il process mining (es. quali varianti, clienti, aree o
     utenti spiegano il valore del KPI; dove intervenire);
   - definition, computed_as (con quali eventi/attributi del modello; per i non osservabili cosa
     servirebbe), status (possibile = osservabile con questi dati; parziale = solo in parte o con
     approssimazioni; non_possibile), missing (dati mancanti precisi), breakdowns (dimensioni
     DISPONIBILI con cui scomporlo, max 4).
   Ordina per objective e, dentro l'obiettivo, per rilevanza per il management.
3. use_cases: breve (4-5 voci): process_overview, control_tower, kpi_detail, root_cause e, se sensato,
   conformance; status possibile/parziale/non_possibile e cosa manca.
4. dimensions: dimensioni di filtro disponibili (dagli attributi candidati, nome leggibile, source
   "Tipo.attributo") e quelle che il management si aspetterebbe ma mancano (available=false), es.
   segmento/area cliente, paese, business unit, categoria merceologica, organizzazione acquisti.
5. data_suggestions: integrazioni di dati che rendono osservabili i KPI piu' preziosi oggi non
   osservabili o parziali; why deve citare quali KPI sblocca; how con tabelle/campi tipici del sistema
   dichiarato (es. SAP: BSEG.ZFBDT, KNB1.ZTERM, CDHDR/CDPOS, USR02.USTYP) se noto. Ordina per priorita'.
6. summary: 2-3 frasi rivolte al management: cosa si potra' misurare subito, il valore in gioco e il
   limite principale dei dati.
Scrivi in italiano (i nomi dei KPI possono restare nella forma inglese usata nella pratica, es. DSO,
touchless). Non inventare dati che non risultano dal modello.
"""


def assess_capabilities(process_context: dict, model_summary: dict) -> Capabilities:
    import anthropic

    client = anthropic.Anthropic()
    payload = {
        "process_name": process_context.get("process_name"),
        "assessment": process_context.get("assessment"),
        "bpmn_activities": process_context.get("bpmn_activities"),
        "dataset_model_and_preview": model_summary,
        "business_kpi_library": LIBRARY,
    }
    with client.messages.stream(
        model=ANTHROPIC_MODEL,
        max_tokens=32000,
        system=_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        output_format=Capabilities,
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason != "end_turn" or response.parsed_output is None:
        raise RuntimeError(f"valutazione non completata (stop_reason={response.stop_reason})")
    return response.parsed_output


class KpiVerdict(BaseModel):
    name: str                      # nome del KPI come lo usa il management
    objective: BusinessObjective
    status: Status
    explanation: str               # perche' si', in parte o no, in una o due frasi
    computed_as: str               # con quali eventi/attributi; per i non osservabili cosa servirebbe
    missing: list[str]
    integration_what: str          # dato da integrare per renderlo osservabile ("" se non serve)
    integration_how: str           # dove si trova di solito (tabelle/campi del sistema)


class KpiCheck(BaseModel):
    verdicts: list[KpiVerdict]


_CHECK_SYSTEM = """\
Sei un partner di una societa' di consulenza esperto di process mining. Un data engineer sta per
generare un dataset OCEL 2.0 e ti chiede se alcuni KPI aggiuntivi saranno analizzabili con questi dati.
Ricevi nome del processo, assessment, modello di dati con statistiche dell'anteprima e i KPI richiesti
(testo libero, anche piu' di uno). Per ciascun KPI richiesto restituisci un verdetto sintetico:
- name: il nome corretto con cui lo conosce il management; objective: l'obiettivo di business;
- status: possibile (osservabile con questi dati), parziale (solo in parte o con approssimazioni),
  non_possibile;
- explanation: una o due frasi, perche';
- computed_as: con quali eventi/attributi del modello si calcola; se non osservabile cosa servirebbe;
- missing: dati mancanti precisi (vuoto se possibile);
- integration_what / integration_how: se non e' pienamente osservabile, quale dato integrare e dove si
  trova di solito nel sistema dichiarato (es. SAP: tabella.campo); stringhe vuote se non serve.
Se il testo non e' un KPI riconoscibile, interpretalo nel modo piu' plausibile per questo processo.
Scrivi in italiano. Non inventare dati che non risultano dal modello.
"""


def check_requested_kpis(process_context: dict, model_summary: dict, requested: str) -> KpiCheck:
    """Verdetto sintetico (osservabile / in parte / no + dati da integrare) sui KPI
    che il data engineer chiede oltre a quelli proposti."""
    import anthropic

    client = anthropic.Anthropic()
    payload = {
        "process_name": process_context.get("process_name"),
        "assessment": process_context.get("assessment"),
        "dataset_model_and_preview": model_summary,
        "requested_kpis": requested,
    }
    with client.messages.stream(
        model=ANTHROPIC_MODEL,
        max_tokens=8000,
        system=_CHECK_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        output_format=KpiCheck,
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason != "end_turn" or response.parsed_output is None:
        raise RuntimeError(f"verifica KPI non completata (stop_reason={response.stop_reason})")
    return response.parsed_output


def merge_requested(existing: list[dict], verdicts: list[dict]) -> list[dict]:
    """Aggiunge i verdetti nuovi; un KPI richiesto di nuovo sostituisce il precedente."""
    new_names = {v["name"].strip().lower() for v in verdicts}
    return [k for k in existing if k["name"].strip().lower() not in new_names] + verdicts


def fallback_capabilities(model_summary: dict) -> Capabilities:
    """Versione deterministica quando l'AI non e' disponibile: solo cio' che si
    deduce con certezza dal modello, senza catalogo KPI di processo."""
    activities = model_summary.get("activity_order", [])
    dims = model_summary.get("dimension_candidates", [])
    has_flow = len(activities) >= 2 and model_summary.get("cases", 0) > 0
    dim_names = [d["attribute"] for d in dims]
    return Capabilities(
        process_family="",
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
        kpis=[Kpi(objective="costi_efficienza", name="Tempo di attraversamento", owner="Process owner",
                  executive_question="Quanto dura il processo dall'inizio alla fine?",
                  business_value="Tempi più brevi riducono costi e capitale immobilizzato.", benchmark="",
                  process_mining_view="Mostra quali varianti e quali casi allungano i tempi.",
                  definition="Tempo dal primo all'ultimo evento di ogni caso",
                  computed_as=f"{activities[0]} → {activities[-1]}" if has_flow else "—",
                  status="possibile" if has_flow else "non_possibile", missing=[], breakdowns=dim_names[:4])],
        dimensions=[Dimension(name=d["attribute"], source=d["attribute"], available=True,
                              note=f"{d['distinct_values']} valori, es. {', '.join(map(str, d['examples'][:3]))}") for d in dims],
        data_suggestions=[],
    )
