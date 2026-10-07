# Roadmap — preparazione dati e generazione OCEL

Idee emerse nel brainstorming del 1 ottobre 2026 su cosa dà valore reale a chi
deve produrre un log OCEL 2.0 adatto alle analisi. È in questa fase che i
progetti di process mining si arenano: estrazioni incomplete scoperte tardi,
nessuna certezza che il log rappresenti il processo, revisione lunga e opaca.

Stato: ✅ fatto · 🔄 in corso · ⬜ da fare

## 1. Partire dalle domande, non dalle tabelle
- ✅ **Checklist di assessment** (obiettivi, perimetro, sistemi, processo, dati,
  documenti di supporto) compilata dal Data Engineer; risposte e attività BPMN
  passate all'AI Mapping e al controllo di pertinenza. Vedi `assessment-checklist.md`.
- ⬜ **Report lacune**: dagli obiettivi scelti dedurre oggetti/eventi/attributi
  necessari e, dopo l'upload, dire cosa manca (es. "per le modifiche di prezzo
  serve il change log CDHDR/CDPOS").
- ⬜ **Richiesta di estrazione generata**: documento per IT/SAP Basis con
  tabelle, campi, filtri (società, periodo, tipi documento) e chiavi di join,
  derivato da assessment + catalogo.

## 2. Capire i dati prima di spendere AI (deterministico, zero token)
- ✅ **Profilazione tabelle** (pagina "Profilo dei dati" dopo l'upload, prima
  di qualunque chiamata AI): chiavi candidate anche composte, duplicati,
  colonne vuote/costanti, collegamenti tra tabelle con % di righe collegate e
  orfane, tabelle isolate, copertura del periodo rispetto all'assessment.
  Chiavi, date e collegamenti passati all'AI Mapping come evidenze misurate.
- ✅ **Controlli sui timestamp** con impatto e cosa fare: date senza ora, date
  segnaposto (00000000, 9999-12-31…), date nel futuro, fuori perimetro,
  registrazioni batch a mezzanotte, valori non leggibili.
- ⬜ Grafo visuale delle tabelle e dei collegamenti (oggi è una tabella).
- ⬜ Usare il profilo anche in trasformazione: scartare duplicati esatti e
  trattare le date segnaposto come mancanti in modo esplicito e tracciato.

## 3. Revisione rapida e comprensibile
- ✅ **Revisione a livello di modello**: in cima alla revisione i tipi di
  oggetto (chiave) e di evento (data, collegamenti) con "Accetta tutto /
  Rifiuta" per gruppo; il dettaglio per colonna è richiuso, aperto solo per le
  tabelle con proposte incerte.
- ✅ **Mapping più veloce e visibile**: prima un modello comune breve (oggetti ed
  eventi di tutte le tabelle), poi una chiamata per tabella in parallelo con quel
  modello come vocabolario; la pagina di attesa mostra le tabelle fatte e in corso.
- ✅ **Mapping misto**: prima le regole (catalogo dei dataset già confermati,
  dizionario delle tabelle SAP standard in `services/sap_dictionary.py`: P2P,
  AP, AR, O2C), senza AI e senza costo; Claude solo per le tabelle non
  riconosciute, con il modello già definito come vocabolario. Le 7 tabelle SAP
  di esempio si mappano tutte senza Claude.
- ✅ **Tetto di spesa del mapping AI** (default 0,50 $ per elaborazione,
  `AI_MAPPING_BUDGET_USD`): limiti di output per chiamata calcolati prima di
  chiamare Claude, effort basso, risposta compatta (solo colonne utili, chiavi
  corte). Se non tutto ci sta si escludono le tabelle più grandi (rigenerabili
  dalla revisione); costo mostrato in attesa e in revisione.
- ⬜ Arricchire il dizionario SAP (VBFA/flusso documenti O2C, CDHDR/CDPOS
  storico modifiche, MKPF, BKPF/BSEG) e collegare il pagamento alla fattura
  (BSAK ↔ RBKP via AWKEY), oggi non supportato dal motore di trasformazione.
- ✖ **Anteprima del processo e "KPI e analisi possibili"**: provate e tolte dal
  Modulo 1. Rallentavano la generazione del dataset e anticipavano ragionamenti
  che appartengono all'analisi. Dalla revisione si conferma e si genera il dataset.
  Da riprendere nel modulo di analisi: process overview filtrabile per
  dimensioni, control tower sui KPI più significativi (libreria di riferimento
  in `services/business_kpi_library.py`, da arricchire con gli esempi degli
  utenti), dettaglio KPI, root cause, suggerimenti di dati da integrare.

## 4. Limiti del motore OCEL attuale (debito tecnico)
- ✅ **Eventi da colonna "attività"** (5 ottobre 2026): oltre alla data, un
  tipo di evento può avere una colonna che dice cosa è successo in ogni riga
  (tipo movimento, azione, stato, causale; in SAP EKBE.VGABE), con una tabella
  valore → nome attività modificabile in revisione. Vale per qualunque
  sistema: storico stati, audit trail, movimenti con causale, export già in
  forma di event log. Il profilo dei dati segnala le probabili colonne
  attività; dizionario SAP, euristica e Claude le propongono; i codici senza
  nome restano nel dataset e sono segnalati nel report di qualità. Emerso
  provando pm4py: tutto EKBE finiva in un'unica attività generica.
- ✅ **Data e ora in colonne separate** (5 ottobre 2026): le colonne di sole
  ore (SAP CPUTM, ERZET; created_time, ora_…) non vengono più lette come
  date (diventavano "oggi a quell'ora": falsi avvisi di date nel futuro) e
  si uniscono alla loro data, così gli eventi hanno l'istante esatto.
- ⬜ Change log (CDHDR/CDPOS, audit trail) → eventi di modifica.
- ⬜ Storia degli attributi degli oggetti nel tempo (oggi timestamp fittizio 1970).
- ⬜ Relazioni oggetto-oggetto (OCEL 2.0).
- ✅ **Controlli di qualità indipendenti dal processo** (5 ottobre 2026): non
  più fissi su PurchaseOrder, girano su ogni tipo di oggetto ("evento prima
  della nascita dell'oggetto", "storico che inizia a metà", oggetti senza
  eventi per tipo); l'oggetto principale dell'assessment serve solo a
  mostrarlo per primo.
- ✅ **Date previste o di scadenza** (5 ottobre 2026): riconosciute senza AI
  (nome, date nel futuro, distanza fissa da un'altra data) e segnalate nel
  profilo, in revisione (proposta incerta, avviso sulla card dell'evento),
  a Claude e nel report di qualità: non devono diventare eventi.

## 5. Ostacoli non tecnici
- ⬜ **Pseudonimizzazione** dei dati personali con regola per colonna (la
  domanda è già nell'assessment).
- ⬜ **Tracciabilità**: ogni evento riporta tabella e riga di origine.

## Revisione e spiegazioni (7 ottobre 2026)
- ✅ **Testata → posizioni**: posizioni senza eventi (BSEG, EKPO, VBAP, righe ordine…) la cui chiave contiene
  quella di una testata con eventi ricevono la proposta di collegamento ponte, per qualunque sistema.
- ✅ **«Aggiungi collegamento»** sulla card di un evento: oggetto e colonna in comune scelti dall'utente.
- ✅ **Passi in alto cliccabili**: dal risultato si torna alla revisione e si rigenera lo stesso dataset
  (una bozza mai promossa si rifà; un dataset promosso diventa una nuova versione).
- ✅ **«Chiedi a Claude»** su messaggi del profilo, righe della revisione e controlli di qualità: costo
  indicativo mostrato e confermato prima dell'invio, risposta nella lingua dell'interfaccia
  (`services/explain.py`, modello `EXPLAIN_MODEL`, predefinito claude-sonnet-5-5).

## Modulo 2 — Analisi
- ✅ **Pulsante «Apri analisi»** attivo solo con almeno un dataset pronto; disattivato con il messaggio
  «Nessun dataset disponibile».
- ✅ **Process Explorer** (7 ottobre 2026): grafo dei flussi object-centric (OC-DFG) di un dataset, dalla
  pagina Analisi, disegnato come una mappa della metropolitana: ogni tipo di oggetto è una linea colorata
  con inizio e fine; ogni attività è una fermata con un punto per tipo e un'etichetta con le volte per tipo;
  numeri di passaggi sulle linee. Pannello «Controllo del grafo»: tipi di oggetto, ricerca, scheda
  **Attività** (spunte una per una o cursore «le più frequenti»; le attività tolte vengono saltate e i
  passaggi ricollegati) e scheda **Collegamenti** (cursore della quota più frequente e spunte una per una).
  Metrica frequenza o tempo mediano; zoom, adatta e legenda; dettaglio al tocco (eventi e oggetti per tipo,
  da dove arriva e dove prosegue, tempi mediano/medio/minimo/massimo). Il grafo è calcolato dal server
  (`services/explorer.py`; oggetti per collegamento verificati identici a `pm4py.discover_ocdfg`), il
  browser riceve solo l'aggregato. Disegno con Cytoscape.js + dagre (MIT), inclusi in `static/vendor/`.
  Nota sui nomi: le «attività» sono i tipi di evento dell'OCEL (gli «event name» di altri strumenti).
  Leggibilità: tutte le linee partono dalla stessa riga in alto, con un tratteggio fino alla prima attività
  (l'oggetto non è ancora entrato nel processo); i numeri sulle linee non si sovrappongono (si nascondono
  i meno importanti, restano nel dettaglio); toccando il nome di una linea la si vede da sola. I tipi
  «trasversali» (pochi oggetti in moltissimi eventi di documenti diversi: cliente, fornitore) sono
  segnalati e non scelti all'apertura. La loro linea passa solo dagli eventi propri (quelli di cui sono
  l'oggetto di casa o che non appartengono alla storia di un documento con più passaggi: per il cliente
  pulizia partite e cambio di rischio, non la registrazione della fattura); negli altri eventi compaiono
  come numero nella fermata. Un interruttore mostra la linea su tutti gli eventi collegati (sottile e in
  trasparenza). Scelta di modello: il cliente resta un oggetto nell'ingestion (ha eventi propri e collega
  documenti diversi); la lettura corretta si risolve nell'analisi.
- ✅ **Assistente dell'analisi** (7 ottobre 2026): pannello «✦ Assistente» nel Process Explorer (anche
  «Chiedi all'assistente» dal dettaglio di una fermata o di un collegamento). Risponde su oggetti, attività e
  numeri usando il contesto del processo (assessment), la provenienza dai dati sorgente (tabelle e colonne
  del mapping) e la vista corrente (tipi scelti, collegamenti visibili con passaggi e tempi). Costo indicativo
  mostrato prima dell'invio e speso finora; risposta nella lingua attiva (`services/analysis_assistant.py`,
  modello `EXPLAIN_MODEL`). **Nomi personali**: chiedendo all'assistente («chiamalo Invoice») un tipo di
  oggetto o un'attività prende un nome proprio dopo la conferma, solo nell'analisi di quell'utente e per quel
  dataset (tabella `analysis_alias`); il dataset non cambia; «↺ nome originale» nell'elenco dei tipi.
- ⬜ Prossimi passi: filtri per dimensione (attributi di oggetti ed eventi, periodo), storia del singolo
  oggetto, varianti, KPI e control tower, «Chiedi a Claude» sul grafo.

## Lingua
- ✅ **Interfaccia in italiano e inglese** (5 ottobre 2026): selettore IT | EN
  in ogni pagina, valido subito e salvato sull'utente; tradotti anche profilo
  dei dati, controlli di qualità e motivazioni del mapping (anche quelli già
  calcolati, perché tradotti al momento di mostrarli). Le spiegazioni scritte
  da Claude sono generate nella lingua attiva al momento del mapping.
  Catalogo in `app/i18n_en.py`, verificato da `tests/test_i18n.py`.

## Altre idee
- ✅ **Contenuto dei documenti come contesto per il mapping** (5 ottobre 2026):
  dai documenti dell'assessment (PDF, Word, PowerPoint, Excel, CSV, testo) si
  legge il testo; prima del mapping si scelgono senza AI i brani che citano
  ogni tabella, le sue colonne o i suoi codici (max ~2.500 caratteri per
  tabella) e solo quelli arrivano a Claude. In revisione si vede quanti brani
  e da quali documenti. Immagini e disegni restano esclusi (nessun testo).
