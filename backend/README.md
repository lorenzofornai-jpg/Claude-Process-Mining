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
| E. Mapping AI-assisted | `services/ai_mapping.py` (interfaccia `AIMapper`, mock `HeuristicAIMapper`, reale `ClaudeAIMapper`) |
| F. Validazione + conferma umana | `templates/mapping_review.html`, `services/validation.py` |
| G. Salvataggio config + run | `models.py` (schema completo), `_finalize()` in `routers/ingestion.py` |

Lo schema dati (`models.py`) implementa esattamente le tabelle discusse in
fase di design: `source_system`, `connector`, `ingestion_config` (+
versioning), `object_type_def`, `event_type_def`, `field_mapping` (con
`proposal_source`, `confidence`, `rationale`, `based_on_template`),
`process_ingestion_link` (riuso Ingestion Config tra processi) ed
`extraction_run` + `data_quality_check_result`.

## Come si avvia

Comando unico (macOS/Linux, richiede Python 3.11+): crea il venv, installa le
dipendenze, genera il dataset di test se manca, avvia il server.

```bash
bash backend/run_dev.sh
```

Passo-passo equivalente (anche per Windows, adattando l'attivazione del venv):

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/generate_synthetic_p2p.py   # genera il dataset di test in data/synthetic_p2p/
uvicorn app.main:app --reload
```

Apri `http://127.0.0.1:8000` e segui il wizard: Contesto → Dati sorgente
(seleziona "usa dataset sintetico") → Revisione mapping → Risultato.

### AI Mapping Service reale (Claude) invece del mock

Di default l'app usa `HeuristicAIMapper` (nessuna chiamata esterna). Per
usare `ClaudeAIMapper` (vera chiamata all'API Anthropic, structured output
via `client.messages.parse`), crea `backend/.env` (mai committato, è in
`.gitignore`):

```
ANTHROPIC_API_KEY=sk-ant-...
AI_MAPPER=claude
```

`ANTHROPIC_MODEL` (opzionale, default `claude-opus-5`) per cambiare modello.
`ClaudeAIMapper` non ha nessuna conoscenza precodificata delle tabelle P2P
(a differenza del mock, che usa il template `genericfile_p2p_v1`): ragiona
da zero su nomi tabella/colonna, tipi, valori di esempio e Process Context
Profile — lo stesso materiale che avrebbe un revisore umano.

**Testato con una vera chiamata**: su questo dataset, Claude ha coperto
tutte le 29 colonne, riconosciuto correttamente chiavi/timestamp/relazioni,
ed è arrivato persino a proporre un 5° tipo oggetto (`Vendor`, separato da
`PurchaseOrder`) che l'euristica mock non modella — segno di un ragionamento
reale, non di un pattern-matching precotto. Ha anche nominato gli event type
in modo diverso dal mock (`"PO Created"` invece di `"Create Purchase
Order"`), il che ha fatto emergere un bug reale nel Data Quality Engine:
due check avevano il nome dell'evento di creazione hardcoded. Corretto
facendo leva sul qualifier `"involves"` (assegnato dal Transformation Engine
al collegamento evento→oggetto nativo) invece che sul nome dell'event type
— i check ora sono indipendenti da come l'AI Mapping Service, mock o reale,
decide di chiamare gli eventi.

## Semplificazioni deliberate di questo prototipo

Sono scelte fatte per avere qualcosa di testabile subito, non limiti
strutturali del disegno:

- **Solo relazioni E2O** (event-to-object), non O2O: per la process
  discovery multi-oggetto sono le E2O a fare il lavoro; le O2O restano nello
  schema dati come possibilità futura.
- **Attributi oggetto non time-varying**: presi come snapshot, non come
  storia di cambiamenti (OCEL 2.0 lo supporterebbe).
- **Revisione HITL: correzione dei campi target di una proposta esistente**,
  non creazione libera di un mapping da zero. In `mapping_review.html` ogni
  riga ha un pannello "Modifica" (ocel_element, object_type, event_type,
  attribute_name, qualifier, related_object_type); una modifica marca la
  riga come `overridden` (`proposal_source="user"`), conserva la proposta
  AI originale in `FieldMapping.original_ai_proposal` per audit, e la
  correzione si propaga davvero fino al log OCEL generato (verificato: la
  correzione di `invoices.payment_status` da attributo statico dell'oggetto
  Invoice ad attributo dell'evento "Post Invoice" cambia effettivamente
  l'OCEL prodotto).
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
