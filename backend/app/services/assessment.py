"""Checklist di assessment del processo (compilata dal Data Engineer).

Scopo: raccogliere PRIMA dell'upload il contesto che serve a trasformare bene i
dati in OCEL 2.0 - cosa si vuole analizzare, su quale perimetro, da quali
sistemi arrivano i dati, dove inizia e finisce il processo, che caratteristiche
hanno timestamp e storico modifiche. Le risposte vengono passate all'AI Mapping
Service (vedi build_mapping_context) e al controllo di pertinenza, e restano
la base per i passi successivi della roadmap (report lacune, richiesta di
estrazione).

Le domande sono definite qui come dati, non nel template: aggiungerne una non
richiede di toccare HTML ne' schema DB (le risposte sono un dict JSON).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

SYSTEM_TYPES = ["ERP", "CRM", "Workflow / BPM", "Ticketing", "E-procurement / portale", "Gestionale custom", "Excel / file", "Altro"]
MAX_SYSTEM_ROWS = 6

SECTIONS: list[dict] = [
    {
        "id": "objectives",
        "title": "1. Obiettivi dell'analisi",
        "why": "Decidono quali oggetti, eventi e attributi servono davvero: è su questo che si valuta "
               "se i dati caricati bastano, e l'AI dà priorità alle colonne utili a queste domande.",
        "questions": [
            {"id": "objectives", "label": "Cosa vuoi analizzare?", "type": "multiselect", "required": True, "options": [
                "Tempi di attraversamento e colli di bottiglia",
                "Conformità al processo standard (varianti, deviazioni)",
                "Rilavorazioni e modifiche (prezzi, quantità, date, blocchi)",
                "Automazione e attività manuali",
                "Compliance e segregazione dei compiti",
                "Performance per fornitore / cliente / reparto",
                "Puntualità di pagamenti o incassi",
            ]},
            {"id": "key_questions", "label": "Domande di business a cui l'analisi deve rispondere", "type": "textarea",
             "required": True, "placeholder": "es. Perché il 30% delle fatture viene pagato in ritardo? Quali fornitori generano più modifiche d'ordine?"},
            {"id": "kpis", "label": "KPI già in uso (opzionale)", "type": "textarea",
             "placeholder": "es. Lead time ordine-pagamento, % fatture con blocco, DSO"},
        ],
    },
    {
        "id": "scope",
        "title": "2. Perimetro",
        "why": "Diventa il filtro dell'estrazione e permette di verificare che le tabelle caricate coprano "
               "davvero il periodo e le società richieste.",
        "questions": [
            {"id": "org_scope", "label": "Società / unità organizzative incluse", "type": "text", "required": True,
             "placeholder": "es. Società IT01 e IT02 (company code), divisione Industrial"},
            {"id": "volume", "label": "Volume indicativo (documenti principali per anno)", "type": "select",
             "options": ["meno di 10.000", "10.000 - 100.000", "100.000 - 1 milione", "oltre 1 milione", "non so"]},
            {"id": "period_from", "label": "Periodo dal", "type": "date", "required": True},
            {"id": "period_to", "label": "Periodo al", "type": "date", "required": True},
            {"id": "exclusions", "label": "Esclusioni (opzionale)", "type": "textarea",
             "placeholder": "es. ordini intercompany, documenti di test, tipi documento ZTST"},
        ],
    },
    {
        "id": "systems",
        "title": "3. Sistemi a supporto",
        "why": "Dicono da dove arriveranno le tabelle e quali passaggi del processo non lasceranno traccia "
               "nei dati: senza questo si scopre a mapping finito che manca un pezzo di processo.",
        "questions": [
            {"id": "systems", "label": "Sistemi coinvolti nel processo", "type": "systems", "required": True},
            {"id": "manual_steps", "label": "Ci sono passaggi gestiti fuori dai sistemi (email, Excel, carta)?", "type": "select",
             "options": ["No", "Sì, alcuni", "Sì, molti", "Non so"]},
            {"id": "manual_steps_detail", "label": "Quali? (opzionale)", "type": "text",
             "placeholder": "es. L'approvazione degli ordini sopra 50k avviene via email"},
            {"id": "integrations", "label": "Integrazioni tra i sistemi (opzionale)", "type": "textarea",
             "placeholder": "es. Le fatture arrivano dal portale SDI e vengono registrate in SAP; il numero d'ordine è il collegamento"},
        ],
    },
    {
        "id": "process",
        "title": "4. Il processo",
        "why": "L'oggetto principale e gli eventi di inizio e fine definiscono come leggere il processo: "
               "guidano la scelta di chiavi ed eventi nel mapping e i controlli di qualità sul dataset.",
        "questions": [
            {"id": "main_object", "label": "Oggetto principale del processo", "type": "text", "required": True,
             "placeholder": "es. Ordine d'acquisto, Fattura cliente, Ticket"},
            {"id": "other_objects", "label": "Altri oggetti coinvolti", "type": "text",
             "placeholder": "es. Righe ordine, Entrate merci, Fatture fornitore, Pagamenti, Fornitori"},
            {"id": "start_event", "label": "Evento che fa iniziare il processo", "type": "text", "required": True,
             "placeholder": "es. Creazione richiesta d'acquisto"},
            {"id": "end_event", "label": "Evento che lo conclude", "type": "text", "required": True,
             "placeholder": "es. Pagamento della fattura"},
            {"id": "known_variants", "label": "Varianti note (opzionale)", "type": "textarea",
             "placeholder": "es. Ordini di servizio senza entrata merce; fatture senza ordine"},
        ],
    },
    {
        "id": "data",
        "title": "5. Dati ed estrazione",
        "why": "Sono i problemi che più spesso bloccano la trasformazione: saperli prima evita rifare "
               "estrazioni e mapping.",
        "questions": [
            {"id": "extraction_method", "label": "Come verranno estratti i dati?", "type": "select", "required": True,
             "options": ["Export di tabelle (es. SE16 / tabelle DB)", "Report standard del sistema", "Query sul database",
                         "API / connettore", "Non ancora definito"]},
            {"id": "extractor", "label": "Chi fa l'estrazione (opzionale)", "type": "text",
             "placeholder": "es. Team SAP Basis, referente IT di stabilimento"},
            {"id": "known_tables", "label": "Tabelle o report già individuati (opzionale)", "type": "textarea",
             "placeholder": "es. EKKO, EKPO, EKBE, RBKP, RSEG, BSAK"},
            {"id": "change_log", "label": "È disponibile lo storico delle modifiche?", "type": "select", "required": True,
             "options": ["Sì (es. CDHDR/CDPOS, audit trail)", "No", "Non so"]},
            {"id": "timestamp_granularity", "label": "Le date nei dati hanno anche l'ora?", "type": "select", "required": True,
             "options": ["Sì, data e ora", "Solo la data", "Dipende dalla tabella", "Non so"]},
            {"id": "timezone", "label": "Fuso orario dei timestamp (opzionale)", "type": "text", "placeholder": "es. Europe/Rome"},
            {"id": "personal_data", "label": "I dati contengono dati personali?", "type": "select", "required": True,
             "options": ["Sì, serve pseudonimizzarli", "Sì, ma possono restare in chiaro", "No", "Non so"]},
            {"id": "custom_fields", "label": "Campi o tabelle personalizzate (opzionale)", "type": "textarea",
             "placeholder": "es. ZZ_APPROVER su EKKO indica l'approvatore; tabella ZPO_STATUS con gli stati custom"},
        ],
    },
]

DOCUMENT_TYPES = {
    "bpmn": "Disegno del processo (BPMN o altro diagramma)",
    "procedure": "Procedura operativa",
    "manual": "Manuale di sistema",
    "data_dictionary": "Data dictionary / descrizione tabelle",
    "other": "Altro",
}
ALLOWED_DOCUMENT_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".xls", ".csv", ".txt", ".md",
    ".bpmn", ".xml", ".png", ".jpg", ".jpeg", ".vsdx", ".drawio", ".svg",
}
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024

QUESTIONS = {q["id"]: q for s in SECTIONS for q in s["questions"]}


def parse_answers(form) -> dict:
    """Legge le risposte dal form HTML (un campo per domanda; multiselect come
    valori ripetuti; sistemi come righe sys_<i>_<campo>)."""
    answers: dict = {}
    for qid, q in QUESTIONS.items():
        if q["type"] == "multiselect":
            values = [v for v in form.getlist(qid) if v in q["options"]]
            if values:
                answers[qid] = values
        elif q["type"] == "systems":
            rows = []
            for i in range(MAX_SYSTEM_ROWS):
                row = {f: (form.get(f"sys_{i}_{f}") or "").strip() for f in ("name", "type", "role")}
                if row["name"]:
                    rows.append(row)
            if rows:
                answers[qid] = rows
        else:
            value = (form.get(qid) or "").strip()
            if value:
                answers[qid] = value
    return answers


def completeness(answers: dict, document_count: int) -> dict:
    """Quante domande obbligatorie hanno risposta (+1 per almeno un documento)."""
    required = [qid for qid, q in QUESTIONS.items() if q.get("required")]
    done = sum(1 for qid in required if answers.get(qid))
    total = len(required) + 1
    done += 1 if document_count else 0
    return {"done": done, "total": total, "percent": round(100 * done / total)}


def extract_bpmn_activities(path: Path) -> list[str] | None:
    """Nomi di task ed eventi da un file BPMN 2.0 (XML). None se non e' BPMN."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    if "bpmn" not in root.tag.lower() and "definitions" not in root.tag.lower():
        return None
    names: list[str] = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if (tag.endswith("Task") or tag in ("task", "startEvent", "endEvent", "intermediateCatchEvent",
                                           "intermediateThrowEvent", "subProcess", "callActivity")):
            name = (el.get("name") or "").strip()
            if name and name not in names:
                names.append(name)
    return names or None


def build_mapping_context(process_name: str, answers: dict, bpmn_activities: list[str]) -> dict:
    """Contesto compatto (solo risposte date, con etichette leggibili) passato
    all'AI Mapping Service e al controllo di pertinenza come process_context."""
    ctx: dict = {"process_name": process_name}
    if not answers and not bpmn_activities:
        return ctx
    assessment: dict = {}
    for qid, value in answers.items():
        q = QUESTIONS.get(qid)
        if q is None:
            continue
        if q["type"] == "systems":
            value = [{k: v for k, v in row.items() if v} for row in value]
        assessment[q["label"]] = value
    if assessment:
        ctx["assessment"] = assessment
    if bpmn_activities:
        ctx["bpmn_activities"] = bpmn_activities
    return ctx
