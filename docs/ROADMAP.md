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

## Log di modifiche e colonne calcolate (9 ottobre 2026)
- ✅ **Log di modifiche riconosciuti in fase 1** (campo, valore vecchio, valore nuovo: SAP CDHDR/CDPOS, audit
  trail, cronologia dei campi di un CRM) in `services/change_logs.py`. Il mapping propone in automatico la colonna
  calcolata `CHANGE_KIND` (Imposta / Rimuovi / Modifica + campo) come colonna attività, con un nome per ogni
  combinazione presente («Set Dunning Block», «Remove Dunning Block»…).
- ✅ **Controlli sui log**: campo solo impostato e mai tolto, valori di un campo che sono valori di un altro
  (estrazione con righe o colonne mescolate), operazioni senza la loro opposta in una colonna attività (SET_X senza
  REMOVE_X).
- ✅ **Chiave dentro un valore composto** (es. CDHDR.OBJECTID = società + documento + esercizio): colonna calcolata
  con quella parte del valore e collegamento degli eventi all'oggetto (`services/derived_columns.py`).
- ✅ **Assistente della revisione**: può proporre colonne calcolate (`add_computed`) e, quando una cosa non c'è,
  lo spiega con i conteggi dei dati (`data_facts`).
- ✅ **L'assistente della revisione legge i dati** in sola lettura (`services/data_query.py`): `query_rows` (righe
  filtrate o conteggi per colonne, colonne calcolate comprese) e `compare_columns` (quanti valori di una colonna, o
  di una sua parte, si trovano in un'altra). Fino a 4 interrogazioni per domanda; il contesto è in cache, quindi le
  interrogazioni costano poco. Il costo massimo mostrato le comprende.

## Nomi nella lingua dell'utente (9 ottobre 2026)
- ✅ Oggetti, eventi, attività e tipi della divisione proposti nella lingua scelta (IT/EN): Claude li scrive già
  nella lingua (prompt del mapping e degli oggetti di business); i nomi che arrivano senza AI (dizionario SAP,
  modello P2P, catalogo, nomi generici «Bkpf event») passano da `services/names_i18n.py`, nei due versi. I nomi
  scelti dall'utente non si cambiano. Plurali italiani con aggettivo finale («Partite cliente aperte»).

## Ruoli degli oggetti di business con effetti concreti (10 ottobre 2026)
- ✅ Spiegazione in testa alla pagina «Oggetti di business»: cosa cambiano inclusione, oggetto guida (rivedibile in
  analisi, ma conta per il mapping), necessario e di contesto.
- ✅ Mapping: gli eventi devono raggiungere guida e necessari (relation anche da colonne con altro nome); per quelli
  di contesto solo collegamenti evidenti.
- ✅ Controllo dopo la generazione «Collegamento agli oggetti necessari»: quanti oggetti guida raggiungono ogni
  oggetto necessario, anche passando per altri oggetti (fino a 3 passaggi); avviso sotto la metà.
- ✅ Process Explorer: all'apertura sono selezionati l'oggetto guida e i necessari (con eventi).

## Oggetti inclusi o esclusi, senza ruoli (10 ottobre 2026)
- ✅ Tolta la scelta «necessario / di contesto»: un oggetto è incluso o no, più l'oggetto guida. Gli inclusi hanno
  tutti i collegamenti curati nel mapping, il controllo «Collegamento all'oggetto guida» e la selezione iniziale nel
  Process Explorer (internamente role = needed se incluso, context se escluso).

## Filtri per attributo e assistente dell'analisi con i dati (10 ottobre 2026)
- ⏸️ (tolti dall'interfaccia il 10 ottobre: da ripensare come entrano nelle analisi; il motore resta in
  `services/ocel_filter.py` e `explorer.load_model(path, filters)`, con i test) Filtri per attributo in Process
  Explorer e Process Overview:
  attributo di un tipo di oggetto o degli eventi, ricerca tra i valori (anche migliaia), più valori e più filtri.
  Restano i casi di partenza, gli oggetti collegati a loro (esclusi i trasversali) con tutta la loro storia.
  Stessi filtri nelle due pagine (per dataset, nella scheda del browser); colori e tipi trasversali stabili.
- ✅ L'assistente dell'analisi legge il dataset (senza filtri) (tabelle per tipo di oggetto ed eventi,
  `data_query.converse` comune con l'assistente della revisione): risponde a «quali ordini del cliente X sono
  ancora aperti?» con i numeri.

## Process Explorer: filtro per varianti e pannello richiudibile (10 ottobre 2026)
- ✅ Scheda «Varianti», alternativa a «Attività» (vale l'ultima delle due aperta): tipo di oggetto, elenco delle
  varianti (sequenze di attività, ripetizioni raggruppate come nella Overview) con oggetti, quota e tempo mediano;
  «− Meno», «Solo la più frequente», «+ Più». Varianti del caso: la sequenza comprende le attività degli oggetti
  collegati tra i tipi selezionati (fino a 2 passaggi, senza trasversali né oggetti dello stesso tipo); il grafo
  mostra i tipi selezionati, solo per i casi delle varianti spuntate.
- ✅ Pannello «Controllo del grafo» richiudibile («) per dare spazio al grafo; si riapre dal pulsante sul grafo
  e la scelta resta per le visite successive.

## Process Overview più essenziale (10 ottobre 2026)
- ✅ Tolti: riquadro «Oggetti del dataset» (l'oggetto guida si sceglie dal menu), tempo «Da inizio a fine» e
  riquadro dell'obiettivo, «Altre attività frequenti».
- ✅ L'obiettivo misurabile resta come verifica nella pagina del risultato del dataset, senza salvataggio.
- ✅ Istogrammi uno sotto l'altro a tutta larghezza; con tante barre (es. 36 mesi) larghezza minima per barra e
  scorrimento orizzontale.

## Modulo 2 — Analisi
- ✅ **Pulsante «Apri analisi»** attivo solo con almeno un dataset pronto; disattivato con il messaggio
  «Nessun dataset disponibile».
- ✅ **Copertura degli obiettivi** (7 ottobre 2026): nel profilo dei dati, prima del mapping, gli obiettivi
  dell'assessment confrontati con le tabelle caricate (`services/coverage.py`, `_coverage.html`).
  Controllo rapido senza costi: per ogni obiettivo spuntato si cercano i dati che servono (date di chiusura e
  scadenza, importi, utente, transazione/canale, storico modifiche, motivi, condizioni di pagamento, cliente o
  fornitore, dispute) con nomi generici e SAP; esito coperto / in parte / non coperto con colonne trovate e cosa
  manca. Valutazione con Claude su richiesta (costo indicativo prima, risultato salvato per quelle tabelle in
  `objective_coverage`): le domande di business libere diventano obiettivi distinti, con come si misurano, i dati
  presenti e quelli mancanti con dove trovarli nel sistema dichiarato. Nato dal caso AR (DSO, touchless, dispute,
  termini di pagamento rispetto a ordini e contratti).
- ✅ **Profilo dei dati più preciso** (7 ottobre 2026), regole generiche verificate anche su dati non SAP:
  colonna attività scelta per nome (EVENT_NAME, ACTION… prima di stato o motivo; «name» non è più scambiato per
  un utente); motivi e causali proposti come attributi dell'evento; tabelle che ripetono le righe di un'altra
  (indici, viste, export filtrati) con un solo avviso ed esclusione dal mapping su casella; date previste con i
  giorni da sommare indicate come scadenza, collegate all'obiettivo dell'assessment che ne ha bisogno.
- ✅ **Oggetti divisi per valore** (7 ottobre 2026): primo passo verso oggetti con valenza di business. Una tabella
  non è un oggetto: la trasformazione ora divide i record di una tabella in più tipi di oggetto secondo una colonna
  (es. tipo documento: fatture, incassi; esclusi gli altri). Da revisione o proposto da Claude.
- ✅ **Oggetti di business guidati dagli obiettivi** (7 ottobre 2026): nuovo passo prima del mapping colonna per
  colonna. Bozza senza costi o proposta di Claude (guida, necessari, di contesto) dagli obiettivi dell'assessment, con
  nome di business, tabella, chiave e filtro per valore; l'utente conferma, rinomina, divide o aggiunge. Il mapping si
  allinea agli oggetti confermati qualunque sia il mapper; l'oggetto guida apre Process Overview e obiettivo misurabile.
- ✅ **Revisione del mapping riapribile e con assistente** (9 ottobre 2026): dall'elenco dei dataset «Rivedi mapping»
  riapre la revisione di un dataset già generato (righe e decisioni salvate, tabelle rilette dai file caricati;
  rigenerando si aggiorna lo stesso dataset). Nella revisione l'assistente risponde e propone modifiche (accettare o
  rifiutare gruppi e righe, rinominare tipi di oggetto ed eventi, nomi per valore, collegamenti) che l'utente
  applica con «Applica» (`services/review_assistant.py`, `POST /ingestion/review/assistant[/apply]`).
- ✅ **Process Explorer senza sovrapposizioni** (7 ottobre 2026): percorsi a linee spezzate calcolati dall'app
  (punti intermedi di dagre accanto alle fermate, una corsia orizzontale per ogni tratto tra due righe, ritorni
  all'indietro spostati di lato) e numeri sulle linee posati dove non coprono fermate, punti, altri numeri o linee.
- ✅ **Obiettivo misurabile** (7 ottobre 2026): la domanda di business dell'assessment (oggetto principale, evento
  di inizio e di fine) diventa un obiettivo legato al dataset: tipo di oggetto, filtro facoltativo su un attributo,
  attività di inizio e di fine (`services/objectives.py`, tabella `analysis_objective`). Proposto in automatico,
  controllato dal vivo e salvato dalla pagina Risultato (Data Engineer, anche sulla bozza) o dall'Overview (Data
  Analyst). Il controllo è generico: inizio/fine su tipi diversi (manca un collegamento), tipo che mescola oggetti
  di natura diversa (propone il filtro sulla colonna che li distingue, es. BLART = RV), pratiche aperte con età,
  fine senza inizio o prima dell'inizio. L'Overview parte dall'obiettivo: oggetto guida e filtro, tempo «da inizio a
  fine», aperti. Nato dal caso AR (fatture e incassi nello stesso tipo: mediana 0 → 27,3 giorni fattura→incasso,
  73,5 giorni con blocco sollecito contro 24,6).
- ⬜ Prossimi passi dell'obiettivo: data di riferimento (scadenza) e quota entro scadenza; confronto per dimensione
  (es. con/senza blocco, per cliente); nel mapping, proposte guidate dall'obiettivo (sottotipi, tabelle di pareggio
  come relazioni tra oggetti, date previste come attributi).
- ✅ **Process Overview** (7 ottobre 2026): prima analisi della pagina Analisi, prima del Process Explorer. In alto
  tutti i tipi di oggetto (oggetti, eventi, durata mediana); l'utente sceglie l'**oggetto guida** (i tipi trasversali
  sono esclusi), come le «perspectives» di Celonis e il «leading object type» di Adams et al. 2022. Su di lui:
  volumi (oggetti, eventi, iniziati per mese), relazioni medie con gli altri tipi, **tempo di attraversamento**
  «solo l'oggetto» o «con gli oggetti collegati» (finito quando finisce l'ultimo oggetto collegato, esclusi i
  trasversali) con distribuzione, **happy path** (variante più frequente: quota, numero, tempo contro tutti),
  **varianti** con cursore per mostrarne via via di più (copertura cumulata) e ripetizioni consecutive raggruppate
  (×), altre attività frequenti. Varianti, conteggi e tempi verificati identici a `pm4py.ocel_flattening`
  (`services/overview.py`). Prossimo: varianti «arricchite» con gli oggetti collegati.
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
- ✅ **Grafo a tutto schermo** (7 ottobre 2026): pulsante «Schermo intero» tra i comandi del grafo; «Riduci a pagina»
  (o Esc) riporta la pagina com'era. A tutto schermo l'assistente si apre dal pulsante ✦ accanto.
- ✅ **Assistente dell'analisi** (7 ottobre 2026): pannello «✦ Assistente» nel Process Explorer (anche
  «Chiedi all'assistente» dal dettaglio di una fermata o di un collegamento). Risponde su oggetti, attività e
  numeri usando il contesto del processo (assessment), la provenienza dai dati sorgente (tabelle e colonne
  del mapping) e la vista corrente (tipi scelti, collegamenti visibili con passaggi e tempi). Costo indicativo
  mostrato prima dell'invio e speso finora; risposta nella lingua attiva (`services/analysis_assistant.py`,
  modello `EXPLAIN_MODEL`). Dal 9 ottobre 2026 l'assistente c'è anche nella Process Overview (conosce oggetto
  guida, misura del tempo, KPI, aperti, varianti; `static/assistant.js`, `_assistant.html`, `POST /analysis/assistant`).
  I nomi personali nell'analisi sono stati tolti: oggetti e attività arrivano con il nome giusto dall'ingestion
  (passo «Oggetti di business», revisione del mapping).
- ✅ **Pagina Analisi riorganizzata** (7 ottobre 2026): processo → dataset → analisi del dataset (per ora Process
  Explorer). Tolti lo scarico OCEL (strumento del Data Engineer) e l'avviso «in costruzione».
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
