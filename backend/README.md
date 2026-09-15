# Modulo 1 — Ingestion (prototipo testabile)

Prototipo funzionante del Modulo 1 (Ingestion) dell'app di process mining
AI-native: contestualizzazione del processo, acquisizione dati via
connettore file, proposte di mapping generate da un "AI Mapping Service"
verso un modello OCEL 2.0, revisione umana (HITL) con conferma/rifiuto,
generazione del log OCEL 2.0 e Data Quality report — più un ciclo di vita
completo delle "strutture" di mapping usate per l'analisi (promozione,
aggiornamento dati, nuova versione, eliminazione).

## Come si mappa al disegno concettuale

| Fase disegnata | Dove nel codice |
|---|---|
| A. Contestualizzazione | `templates/context.html`, `POST /ingestion/new` |
| B/C/D. Acquisizione + tabelle | `connectors/file_connector.py`, `templates/upload.html` |
| E. Mapping AI-assisted | `services/ai_mapping.py` (interfaccia `AIMapper`, mock `HeuristicAIMapper`, reale `ClaudeAIMapper`) |
| F. Validazione + conferma umana | `templates/mapping_review.html`, `services/validation.py` |
| G. Salvataggio config + run (come bozza) | `models.py` (schema completo), `_finalize()` in `routers/ingestion.py` |
| Ciclo di vita struttura (nuovo) | `routers/ingestion.py`: `promote_structure`, `list_structures`, `update_data_*`, `delete_structure` |

### Ciclo di vita di una struttura di mapping

Il risultato di un giro di wizard (upload → revisione → conferma) è sempre
una **bozza**: `IngestionConfig.status = "draft"`, non ancora tracciata come
"in uso per l'analisi". Dal risultato, il pulsante **"Utilizza log per
analisi"** la promuove (`status = "approved"`), rendendola visibile nel
**registro strutture** del processo (`/ingestion/structures`). Da lì:

- **Aggiorna dati** — ricarica le tabelle sorgente nello stesso formato:
  riapplica esattamente lo stesso `field_mapping` già confermato (nessuna
  nuova chiamata AI, nessuna revisione), producendo un nuovo `ExtractionRun`
  collegato alla stessa struttura. Prima di generare qualunque cosa, verifica
  che ogni tabella/colonna richiesta dal mapping esistente (`IngestionConfig.
  schema_fingerprint`) sia presente nel nuovo caricamento; se manca qualcosa,
  blocca con un messaggio esplicito invece di produrre un log sbagliato.
- **Modifica struttura** — riapre l'intero wizard (nuova AI mapping +
  revisione) ma alla conferma aggiorna la struttura esistente invece di
  crearne una nuova: incrementa `current_version`, sostituisce
  `ObjectTypeDef`/`EventTypeDef`/`FieldMapping`, e la riporta a `status =
  "draft"` — richiede una nuova promozione esplicita prima di tornare attiva.
- **+ Nuova struttura** — il wizard normale da zero, crea un `IngestionConfig`
  completamente separato (nessuna delle due strade tocca l'altra).
- **Elimina** — cancellazione a cascata (FieldMapping, ObjectTypeDef,
  EventTypeDef, ExtractionRun, DataQualityCheckResult, ProcessIngestionLink,
  IngestionConfigVersion, i file OCEL su disco) e infine la config stessa.

Verificato end-to-end: genera → promuovi → aggiorna con dati compatibili
(nuovo run, stessa versione) → aggiorna con una tabella mancante (bloccato
con errore chiaro, nessun run creato) → modifica struttura (stessa config,
versione+1, torna a bozza) → ripromuovi → elimina (cascata completa
verificata riga per riga sul DB).

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

Apri `http://127.0.0.1:8000`: al primo avvio viene creato un utente
**admin** iniziale, con email/password stampate nei log di avvio (o
personalizzabili impostando `ADMIN_EMAIL`/`ADMIN_PASSWORD` in `backend/.env`).
Login come admin → **Amministrazione** → crea un utente Data Engineer e un
nuovo processo → assegna il Data Engineer al processo. Poi accedi come quel
Data Engineer (o resta admin, che ha accesso a tutto) e da **I miei
processi** apri il Modulo 1: Dati sorgente → Revisione mapping → Risultato.

L'unico modo di acquisire dati è caricare file CSV/TXT (nessuna scorciatoia
"dataset sintetico" nell'interfaccia): usa i CSV in `data/synthetic_p2p/`
(rigenerabili con `python scripts/generate_synthetic_p2p.py`) come file di
prova da caricare manualmente, singolarmente o raggruppati in un unico
`.zip`. L'upload ZIP è supportato ovunque si carichino file nell'app (nuova
struttura e "Aggiorna dati"): l'estrazione è sanificata (nessun path fuori
dalla cartella di destinazione, solo membri `.csv`/`.txt`, max 50 MB per
membro come guardia contro zip bomb).

### Ruoli e permessi

- **Admin**: crea utenti e processi, assegna un **Data Engineer** per
  processo, accesso illimitato a tutto.
- **Data Engineer**: accede solo al Modulo 1 (Ingestion) dei processi a cui è
  stato assegnato (`ProcessAssignment.role = "data_engineer"`); tentare di
  aprire un processo non assegnato risponde 403. Chi conferma/corregge un
  mapping viene registrato per nome in `IngestionConfig.owner` e
  `FieldMapping.confirmed_by` — audit trail reale, non un placeholder.
- Password con bcrypt, sessione via cookie firmato (Starlette
  `SessionMiddleware`). I ruoli Process Owner/Analyst/Viewer per gli altri
  moduli restano concettuali per ora: lo schema (`ProcessAssignment.role` è
  una stringa libera) è già pensato per estendersi senza migrazioni quando
  arriveranno.

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
- **Un solo ruolo cablato (Data Engineer → Modulo 1)**: Process
  Owner/Analyst/Viewer per gli altri moduli restano concettuali, non ancora
  implementati (lo schema li supporta senza modifiche).
- **Stato dell'ingestion in-memory per workspace**: sopravvive finché il
  processo del server resta attivo, non a un riavvio (a differenza di
  utenti/assegnazioni/IngestionConfig, quelli sono su DB).
- **Riuso della Ingestion Config *tra processi diversi* non automatizzato**:
  aggiornare/modificare una struttura già esistente *dentro lo stesso
  processo* ora è pieno supporto (vedi sopra); manca ancora un flusso per
  proporre a un secondo processo di riusare una struttura approvata su un
  primo processo.
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

### Stress test con nomenclatura SAP reale (volume + complessità)

`scripts/generate_sap_p2p.py` genera lo stesso processo P2P ma con nomi di
tabella/campo SAP reali (`LFA1`, `EKKO`, `EKPO`, `EKBE`, `RBKP`, `RSEG`,
`BSAK`), date in formato `YYYYMMDD` a 8 cifre, ~100 ordini d'acquisto e
910 righe totali — pensato per stressare l'app su volume e su una
nomenclatura senza suffissi inglesi (`EBELN`, `LIFNR`, `BELNR`... invece di
`order_id`, `vendor_id`...), che il mapper euristico generico non aveva mai
visto. Include le stesse categorie di anomalie deliberate (BUDAT mancante
su 2 ricevimenti merce, 2 fatture con data antecedente alla creazione
dell'ordine, fatture bloccate mai pagate).

Questo stress test ha fatto emergere e corretto 3 bug reali:

1. **Date SAP a 8 cifre scambiate per numeri.** `_infer_column_type()` in
   `connectors/file_connector.py` faceva il controllo "è un numero?" prima
   del controllo data, quindi colonne come `AEDAT=20260115` venivano
   classificate come intero invece che data. Aggiunto `_looks_like_yyyymmdd()`,
   controllato per primo nella catena di inferenza tipo.
2. **`_parse_time()` non riconosceva il formato `%Y%m%d`** in
   `services/transformation.py`: anche dopo la classificazione corretta come
   "date", il Transformation Engine non riusciva a parsare il timestamp in
   fase di generazione OCEL. Aggiunto il formato alla lista tentata.
3. **L'euristica generica di fallback non trovava mai una chiave oggetto su
   nomi di colonna non in stile inglese.** `_generic_fallback()` in
   `services/ai_mapping.py` richiedeva un suffisso tipo `_id`/`_no`/`_number`
   nel nome colonna per considerarla una possibile chiave — nessun campo SAP
   lo ha mai (`EBELN`, `LIFNR`, `BELNR`...), quindi su questo dataset **non
   veniva creato nessun oggetto, solo eventi**. Corretto: il segnale
   primario ora è la quasi-unicità dei valori (`distinct_ratio > 0.95`), con
   il pattern sul nome usato solo per alzare la confidence quando concorda.
   Scoperto nel farlo un quarto bug collegato: il controllo intero/float in
   `_infer_column_type()` usava `Series.astype("Int64", errors="ignore")`
   per distinguere i due tipi, ma su valori con decimali quel cast fallisce
   silenziosamente e restituisce la Series originale invariata — quindi il
   confronto di uguaglianza risultava sempre vero e **nessuna colonna
   numerica veniva mai classificata "float"**, nemmeno importi come
   `WRBTR=123.45`. Questo faceva sì che l'esclusione "gli importi non sono
   mai chiavi naturali" appena aggiunta all'euristica non scattasse mai.
   Corretto il controllo con `(as_num % 1 == 0).all()`.

Verificato end-to-end dopo i 4 fix: 707 oggetti, 6 tipi oggetto (`Lfa1` →
`LIFNR`, `Ekko` → `EBELN`, `Rbkp`/`Ekbe`/`Bsak` → `BELNR`, `Ekpo` →
`MATNR`), 444 eventi, 4 tipi evento, 2 righe scartate (le 2 anomalie
BUDAT iniettate) — coerente con i dati generati.

**Limite noto rimasto, non risolto**: l'euristica generica non rileva
chiavi composite. `EKPO` (chiave reale `EBELN`+`EBELP`) e `RSEG` (chiave
reale `BELNR`+`GJAHR`+`BUZEI`) non hanno una singola colonna quasi-unica
affidabile — per `EKPO` viene proposta `MATNR` come proxy a bassa
confidence (funziona in questo dataset per coincidenza, ma non è la vera
chiave di riga), per `RSEG` non viene proposta nessuna chiave (nessun
oggetto creato per quella tabella, solo attributi sugli eventi). Entrambe
le proposte restano a confidence media/bassa con rationale esplicito,
quindi visibili e correggibili in revisione — un vero `ClaudeAIMapper`
risolverebbe questo per ragionamento, non per pattern-matching. Sempre per
lo stesso motivo, l'euristica generica non propone relazioni e2o tra
tabelle diverse (es. fattura↔ordine): il DQ check "evento antecedente alla
creazione del case" su questo dataset passa banalmente perché non esiste
alcun collegamento evento→PO da poter violare, non perché l'anomalia
iniettata sia stata verificata assente.
