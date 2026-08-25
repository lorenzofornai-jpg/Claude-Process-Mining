# Modulo 1 — Ingestion (prototipo testabile)

Prototipo funzionante del Modulo 1 (Ingestion) dell'app di process mining
AI-native: contestualizzazione del processo, acquisizione dati via
connettore file, proposte di mapping generate da un "AI Mapping Service"
verso un modello OCEL 2.0, revisione umana (HITL) con conferma/rifiuto,
generazione del log OCEL 2.0 e Data Quality report.

## Come si mappa al disegno concettuale

| Fase disegnata | Dove nel codice |
|---|---|
| A. Contestualizzazione | `templates/context.html`, `POST /ingestion/new` |
| B/C/D. Acquisizione + tabelle | `connectors/file_connector.py`, `templates/upload.html` |
| E. Mapping AI-assisted | `services/ai_mapping.py` (interfaccia `AIMapper` + mock `HeuristicAIMapper`) |
| F. Validazione + conferma umana | `templates/mapping_review.html`, `services/validation.py` |
| G. Salvataggio config + run | `models.py` (schema completo), `_finalize()` in `routers/ingestion.py` |

Lo schema dati (`models.py`) implementa esattamente le tabelle discusse in
fase di design: `source_system`, `connector`, `ingestion_config` (+
versioning), `object_type_def`, `event_type_def`, `field_mapping` (con
`proposal_source`, `confidence`, `rationale`, `based_on_template`),
`process_ingestion_link` (riuso Ingestion Config tra processi) ed
`extraction_run` + `data_quality_check_result`.

## Come si avvia

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/generate_synthetic_p2p.py   # genera il dataset di test in data/synthetic_p2p/
uvicorn app.main:app --reload
```

Apri `http://127.0.0.1:8000` e segui il wizard: Contesto → Dati sorgente
(seleziona "usa dataset sintetico") → Revisione mapping → Risultato.

## Semplificazioni deliberate di questo prototipo

Sono scelte fatte per avere qualcosa di testabile subito, non limiti
strutturali del disegno:

- **AI Mapping Service è un mock euristico** (`HeuristicAIMapper`), non una
  vera chiamata LLM: nessuna `ANTHROPIC_API_KEY` era disponibile in questo
  ambiente. L'interfaccia `AIMapper.propose_mapping(...)` è pensata apposta
  per essere implementata da un `ClaudeAIMapper` senza toccare il resto
  della pipeline (review UI, transformation engine).
- **Solo relazioni E2O** (event-to-object), non O2O: per la process
  discovery multi-oggetto sono le E2O a fare il lavoro; le O2O restano nello
  schema dati come possibilità futura.
- **Attributi oggetto non time-varying**: presi come snapshot, non come
  storia di cambiamenti (OCEL 2.0 lo supporterebbe).
- **Revisione HITL solo accetta/rifiuta per riga**, non editing libero dei
  singoli campi di mapping (il modello dati lo prevede via
  `FieldMapping.override`, la UI no).
- **Nessuna autenticazione/ruoli**: un solo utente implicito ("admin"); i
  ruoli granulari Process Owner/Analyst/Viewer disegnati concettualmente non
  sono cablati qui.
- **Riuso della Ingestion Config non automatizzato**: ogni finalizzazione
  crea una nuova `IngestionConfig` invece di proporre il riuso di una
  config approvata esistente per lo stesso sistema+processo.
- **Connettori SAP/Salesforce/ServiceNow non implementati**: solo
  `FileConnector` (CSV/TXT), che è comunque un connettore a pieno titolo
  nell'architettura a plugin — aggiungere un sistema reale significa
  implementare `connectors/base.Connector` senza toccare il resto.

## Dataset di test

`scripts/generate_synthetic_p2p.py` genera 4 tabelle CSV che simulano un
processo Purchase-to-Pay (`purchase_orders`, `po_lines`, `goods_receipts`,
`invoices`), con alcune anomalie deliberate (timestamp mancanti, 2 fatture
con data antecedente alla creazione dell'ordine) per verificare che il Data
Quality Engine le rilevi davvero.

Verificato end-to-end (script + browser via Playwright): genera 4 tipi
oggetto, 4 tipi evento, ~160 oggetti, ~110-120 eventi, e il DQ report
segnala correttamente le anomalie iniettate.
