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

## Control Tower (10 ottobre 2026)
- Terza analisi, seconda nell'elenco dopo la Process Overview: la vista per il management sulle domande di business
  dell'assessment. Intestazione con il periodo scelto con due cursori tra il primo e l'ultimo mese dei dati (confronto con
  i mesi precedenti della stessa lunghezza, se il dataset li copre), «In sintesi»
  con i KPI principali, poi una sezione per domanda: KPI con valore, ▲▼ rispetto al periodo precedente (verde se
  migliora) e obiettivo (verde/rosso), grafici (andamento mensile con la linea dell'obiettivo, barre e tabelle per
  gruppo, ciambella della composizione), cosa manca nei dati.
- Definizioni in un vocabolario chiuso (services/control_tower.py), valido per ogni processo e sistema: misure
  quota / numero / somma di un importo / quota dell'importo / tempo in giorni; condizioni su attività (anche negli
  oggetti collegati), utente non di sistema, attributi, tempo tra due momenti (anche rispetto a una scadenza calcolata
  da data base + giorni). Claude le propone una volta per dataset (costo mostrato prima, ~0,10-0,25 $); il Data
  Analyst le modifica con l'editor o con «Rivedi con Claude» e le approva; i valori li calcola l'app.
- Dati mancanti: un KPI che usa attività o attributi che il dataset non ha mostra «Dato mancante» e il perché.
- ✅ Due livelli: la sintesi (una card per domanda con i KPI scelti per la sintesi e l'andamento mensile) e, toccando
  una card, il dettaglio della domanda con tutti i KPI e i grafici (#q=<id>: il pulsante Indietro torna alla sintesi).
- ✅ «Definizioni dei KPI» ridotte a domande di business, KPI e come si calcolano (testi, KPI della sintesi,
  eliminazioni); come si calcola un KPI, nuove domande e nuovi KPI si chiedono a Claude.
- ✅ Assistente della Control Tower: spiega i numeri e propone modifiche (grafici, statistiche, obiettivi, domande,
  KPI) come operazioni che l'app applica in anteprima; si salvano dalla barra in alto. Cambiare grafici e sintesi
  lascia l'approvazione; cambiare domande e KPI (anche gli obiettivi) le rimette da approvare.
- ✅ Testi nelle due lingue: Claude scrive domande, KPI, titoli ed etichette in italiano e in inglese e i nomi dei
  tipi di oggetto per le frasi («394 di 485 fatture» / «invoices»); i testi in una lingua sola (definizioni già
  salvate, correzioni a mano) si completano con «Traduci» (costo mostrato prima). Tradurre o correggere i testi non
  toglie l'approvazione.
- ✅ L'assistente gestisce anche l'impaginazione: ordine di domande, KPI e grafici (anche da una domanda all'altra),
  colonne della sintesi, dei KPI e dei grafici, larghezza e altezza dei grafici, quanti gruppi e in che ordine nelle
  barre e tabelle, andamento sulle card e «perché conta» visibili o no. Non tocca colori e stile dell'app.
  L'impaginazione non chiede una nuova approvazione.
- ✅ Un solo «Salva» (vale anche come conferma delle definizioni, al posto di «Salva e approva»); con modifiche non
  salvate la barra con «Salva» compare anche nel dettaglio di una domanda.
- ✅ 20 tipi di grafico sul modello della galleria di Power BI, che l'assistente può aggiungere o scambiare:
  andamento (linea, area, colonne per mese, combinato volume + KPI), per gruppo (barre, colonne verticali, tabella,
  Pareto con cumulato, bolle, treemap, mappa geografica dei paesi), gruppo per mese (colonne in pila, linee per
  gruppo, mappa di calore), composizione (ciambella, torta, imbuto delle attività), istogramma di tempi o importi
  con mediana e 90° percentile, tachimetro con fasce attorno all'obiettivo, semaforo con tolleranza. Disegnati in
  SVG senza librerie; i confini della mappa (Natural Earth 1:110m, dominio pubblico) sono in
  `static/world-110m.json` e si caricano solo se c'è una mappa; i paesi si riconoscono da codice ISO a 2 o 3
  lettere (es. LAND1 di SAP) o dal nome in italiano o in inglese.
- Da fare: grafico a cascata (variazione tra periodi per gruppo), albero di scomposizione, mappe per regione o
  città; esportazione PDF/immagine per il management; filtro in testata (cliente, società); avvisi quando un KPI
  esce dall'obiettivo.

## Filtri dinamici sui casi (13 ottobre 2026)
- Decisioni: filtri **condivisi** tra le analisi dello stesso dataset (Overview, Explorer, Control Tower; nella scheda
  del browser) e **sui casi**: gli oggetti scelti con gli oggetti collegati e tutta la loro storia (`ocel_filter`).
  Valori dello stesso grafico in OR, filtri di grafici diversi in AND.
- ✅ Fase 1: motore (filtri «mese di inizio» e «tempo di attraversamento» per tipo di oggetto; un altro oggetto dello
  stesso tipo collegato per caso non entra), barra «Filtri attivi» con ✕ e «Togli tutti» (`static/filters.js`),
  Process Overview: tocco su una barra = aggiunge o toglie, trascinamento = intervallo, sul grafico dei mesi e
  sull'istogramma dei tempi; il grafico da cui parte la selezione mostra tutti i valori (scelti in evidenza).
  Explorer e Control Tower ricalcolano sui casi filtrati (nella Control Tower i dati mancanti si controllano sul
  dataset intero).
- ✅ Fase 2: grafici della Control Tower selezionabili. Per gruppo (barre, colonne, tabella, Pareto, bolle, treemap,
  mappa, fette per dimensione, righe della mappa di calore, linee per gruppo, segmenti delle colonne in pila): filtro
  «valore di una dimensione»; per categoria (fette con categorie, passi dell'imbuto, classi dell'istogramma): filtro
  «condizione», con la stessa logica dei grafici (`explorer.resolve`). Andamenti (punti e colonne dei mesi), intestazioni
  dei mesi e cursore del periodo: filtro «mese di inizio» sugli oggetti dei KPI, che nella Control Tower diventa il
  periodo (confronto con i mesi precedenti se consecutivi). Il grafico da cui parte una selezione (e quelli con la
  stessa dimensione) mostra tutti i valori; gli andamenti mostrano tutti i mesi con il periodo in evidenza.
- ✅ Fase 3: gli assistenti (Overview, Explorer, Control Tower) ricevono i filtri attivi e rispondono sui casi
  filtrati (numeri della pagina e tabelle da interrogare), dicendolo. Barra dei filtri sempre visibile con «Viste»: i filtri attivi
  salvati con un nome per il dataset (tabella `analysis_view`), visibili a tutti i Data Analyst del processo, tolte
  solo da chi le ha create.
- ✅ Pennello di selezione (come in Celonis): trascinando su colonne, barre, righe di tabella, istogrammi e
  andamenti compare un riquadro con maniglie ai lati (si allarga, si stringe, si sposta) e tre pulsanti: ✓ applica,
  ✕ annulla, ◐ inverti (tutti tranne quelli nel riquadro). Tocco singolo = aggiunge o toglie un valore; Invio / Esc
  da tastiera. Non sulle torte.
- ✅ Al tocco (iPad) eventi touch nativi: sulle colonne il dito in orizzontale disegna il pennello, in verticale la
  pagina scorre; sulle righe delle tabelle e sulle barre orizzontali si tiene premuto un attimo e poi si trascina.
  Tolto «+ Filtro» (filtri scelti a mano da un elenco di attributi): troppo complesso, i filtri si mettono dai grafici.
  La versione degli script nella pagina si ricalcola a ogni pagina, così dopo un git pull il browser non tiene
  quelli vecchi anche senza riavviare il server.
- ✅ Il pennello scatta a colonne o righe intere: il riquadro va da metà strada dall'elemento precedente a metà strada
  dal successivo e, trascinando una maniglia o il riquadro, include o esclude la colonna/riga successiva appena si
  supera la metà. I pulsanti ✓ ✕ ◐ restano sempre nella parte visibile del grafico e dello schermo (colonne ai bordi,
  pagina ingrandita con due dita, grafici che scorrono).

## BPA — Business Process Analysis (14 ottobre 2026)
- ✅ Nella Process Overview, con «Con gli oggetti collegati», ogni variante ha «Invia alla BPA» (e «Togli dalla BPA»:
  la scelta si revoca); si possono inviare più varianti. La BPA è l'ultima analisi della pagina Analisi.
- ✅ Ogni variante diventa un vero modello BPMN 2.0 (XML con il disegno, `services/bpmn.py`, tabella `bpa_model`):
  pool con il nome del processo, lane dai ruoli del dataset (attributi degli eventi come role/department/org:role,
  valore più frequente per attività) o, senza, una lane «Ruolo da definire» da rinominare; evento di inizio, task
  in sequenza (ripetizioni «×» con il simbolo del ciclo), evento di fine.
- ✅ Sempre in italiano e in inglese con la stessa struttura: salvando in una lingua, l'altra prende le stesse
  modifiche; solo i nomi nuovi o cambiati passano dall'LLM (senza LLM restano uguali, e l'editor lo dice).
- ✅ Editor a tutta pagina con bpmn-js (bpmn.io, `static/vendor/bpmn-js`, licenza con il logo bpmn.io visibile):
  tavolozza e menu in italiano o inglese, lingua del modello IT | EN, annulla/ripeti, zoom, salva (Ctrl/Cmd+S),
  scarica .bpmn e .svg, «Rifai dalla variante». Al tocco (iPad): trascinare elementi e dalla tavolozza, doppio tocco
  per rinominare, pizzico per lo zoom, un dito sullo sfondo per spostare la vista.
- Da fare: gateway e rami alternativi da più varianti insieme (modello unico), pannello delle proprietà
  (documentazione, tipi di task), lane suggerite dall'LLM quando il dataset non ha ruoli.

## Process Overlay (14 ottobre 2026)
- ✅ Analisi tra Process Explorer e BPA. Nella BPA si sceglie un solo BPMN per dataset come «Riferimento per il
  process overlay» (anche dalla pagina dell'overlay). Il confronto usa i casi dell'oggetto guida del BPMN con gli
  oggetti collegati (come la variante da cui è nato) e i filtri condivisi sui casi.
- ✅ Sul BPMN (bpmn-js in sola lettura): in blu attività e collegamenti del modello trovati nei dati, con il numero di
  casi; in grigio quelli non trovati; in rosso, sotto la pool, le attività dei dati assenti dal modello e i passaggi
  che il modello non prevede (ognuno a un suo livello in una fascia sotto l'ultima lane con il flusso). Collegamenti attraverso gateway ed eventi: un collegamento è «nei dati» se un
  passaggio diretto tra le attività che unisce c'è nei casi. Task generati dalla variante legati per posizione (anche
  rinominati), gli altri per nome.
- ✅ Varianti a destra come filtro (tutte, nessuna, la più frequente), con varianti scelte, quota di casi e tempo.
- ✅ Assistente dell'analisi (page = overlay): conosce il BPMN di riferimento, colori e numeri di attività e frecce,
  attività e passaggi in rosso e le varianti (con quelle spuntate), es. «differenza tra la variante 1 e la 2».
- Da fare: conformità per caso (fitness), attività del modello con nomi diversi da quelli dei dati da collegare a mano.

## Root Cause Analysis — RCA (15 ottobre 2026)
Terzo livello della Control Tower. Al secondo livello (una domanda di business) l'utente vede i sintomi: KPI e
grafici con i filtri sui casi e il periodo che ha scelto (prodotto o servizio, area, cliente, fornitore…). Il
pulsante «RCA» (uguale in italiano e in inglese) apre il terzo livello: le cause probabili del sintomo, sugli stessi
casi. Principio: **meglio dire che i dati non bastano che dare evidenze banali**. Vale per qualunque processo e
dataset: il motore usa solo la struttura OCEL (oggetti, attributi, eventi, collegamenti, utenti), mai nomi di un
sistema sorgente.

**Il sintomo.** Un KPI della domanda, scelto dall'utente (uno alla volta). Ogni misura della Control Tower si
scompone per caso: quota, conteggio, importo → caso «colpito» sì/no (dalla parte sbagliata secondo l'obiettivo o il
verso del KPI); durata → giorni del caso (peggio = più lungo, se il KPI non dice il contrario).

**Due confronti, tutti e due nella prima versione.**
1. Dentro i filtri: tra i casi del segmento scelto, cosa distingue quelli con il sintomo.
2. Segmento contro resto (se ci sono filtri): perché il segmento va peggio degli altri casi dello stesso periodo,
   scomposto in **effetto mix** (il segmento ha più casi di un tipo difficile, es. più categorie lente) ed
   **effetto prestazione** (a parità di composizione va comunque peggio).

**I fattori candidati** (calcolati dall'app, zero token):
- di contesto, noti all'inizio del caso: attributi dell'oggetto guida (testo: i valori più frequenti; numeri: fasce
  per quartili; valori mancanti come segnale), oggetti collegati (anche trasversali, es. il cliente o il fornitore) e
  loro attributi (es. la regione del fornitore);
- di comportamento: presenza di un'attività, ripetizioni (rilavorazioni), modifiche dai log di modifiche
  (Imposta/Rimuovi/Modifica + campo);
- interventi manuali, ricavati dai dati: l'utente di ogni evento si classifica dal comportamento (utenti tecnici:
  molti eventi nello stesso istante, orari e giorni fissi, anche notte e festivi; il nome — BATCH, RFC, WF… — è
  solo un indizio); un'attività di solito automatica eseguita da una persona è un intervento manuale (es. un
  sollecito fatto a mano invece che dall'esecuzione periodica). Senza utenti, stima più debole dagli istanti degli
  eventi (eventi in blocco = esecuzione automatica). Ogni fattore manuale dice come è stato riconosciuto;
- temporali: giorno della settimana e fine trimestre all'inizio del caso, carico di lavoro (casi iniziati nella
  stessa settimana).

**Filtri anti-banalità** (un candidato arriva all'utente solo se li supera tutti):
- niente tautologie: attività e attributi usati nella definizione del KPI (condizione, popolazione, punti della
  durata) e quelli dei filtri attivi (se filtri «area Nord», «area Nord» non è una causa);
- precedenza nel tempo: un fattore di comportamento conta solo se accade prima che il sintomo si manifesti (per una
  durata: inizio + obiettivo, o la mediana se non c'è obiettivo); dopo è una conseguenza (es. il sollecito per una
  fattura già in ritardo);
- supporto minimo (almeno 20 casi e il 3% della popolazione, abbastanza casi con il sintomo);
- effetto grande (quota almeno 1,5 volte e +5 punti; durata almeno +2 giorni e +20% della mediana);
- significatività corretta per i confronti multipli (test esatto di Fisher o Mann-Whitney, Benjamini-Hochberg);
- stabilità: l'effetto c'è in tutte e due le metà del periodo;
- a parità di condizioni: se l'effetto sparisce confrontando casi simili rispetto a un altro fattore (es. il
  fornitore a parità di categoria), la causa è l'altro fattore;
- doppioni: fattori sugli stessi casi (≥ 80% in comune) diventano uno solo, che cita gli altri.
Combinazioni di due fattori di contesto: solo se la combinazione è molto più forte dei due fattori da soli (es.
cliente A × categoria B). Ordine per **impatto**: casi (o giorni) in eccesso che il fattore spiega, non per
correlazione. Al massimo 5 cause, con confidenza alta o media.

**Dati insufficienti.** Se non resta nulla, la RCA lo dice con il motivo (pochi casi con il sintomo, effetti piccoli
o instabili, quanti candidati e perché sono caduti) e cosa servirebbe (es. «nessun attributo dice chi esegue gli
eventi: con questo dato si potrebbero analizzare gli interventi manuali»).

**L'LLM viene dopo i numeri.** Una chiamata, solo sulle cause trovate: titolo, spiegazione e cosa verificare, in
italiano e in inglese, senza numeri nel testo (i numeri li mostra l'app); giudica anche la plausibilità e segnala le
probabili conseguenze scambiate per cause, che finiscono tra gli scartati. Non può aggiungere cause.

**Niente analisi ripetute.** Ogni RCA si salva con un'impronta (metodo, dataset e versione, definizione del KPI,
periodo, filtri normalizzati, caricamento dei dati): stessa impronta → il risultato salvato, con data e autore, per tutti quelli che
vedono la Control Tower, senza ricalcolo né token. Nuova versione del dataset o nuovi dati caricati («Aggiorna i
dati», che non cambia la versione) → la RCA precedente non si azzera: resta visibile con l'avviso e il pulsante
«Aggiorna», per confrontare prima e dopo. I testi dell'LLM si salvano per fattore: se lo stesso fattore riemerge (altri filtri, dati
aggiornati) il testo si riusa e l'LLM si chiama solo per i fattori nuovi.

- ✅ Motore statistico (`services/rca.py`), salvataggio (`rca_analysis`, `rca_text`), terzo livello nella Control
  Tower con il pulsante «RCA».
- ✅ «RCA salvate di questa domanda» in fondo al terzo livello: l'ultima versione di ogni analisi con KPI, filtri,
  periodo, esito (cause e la principale), autore e data; «Apri» rimette i suoi filtri e il suo periodo e la mostra
  senza ricalcolo, «Elimina» la toglie (con le versioni precedenti; i testi dell'LLM restano per essere riusati).
  I filtri completi si salvano con l'analisi (`rca_analysis.filters`).
- ✅ Assistente della Control Tower anche nella RCA (view.level = rca): conosce l'analisi che l'utente vede (cause,
  numeri, segmento contro resto, motivi degli scarti) e il metodo; domande suggerite sulla RCA.
- Da fare: classificazione semantica una tantum delle attività (storni, blocchi, condizioni commerciali) con l'LLM,
  confermata dai dati; drill-down sui casi di una causa e collegamento al Process Overlay con il filtro applicato.

## BPA — Archivio degli oggetti BPMN (14 ottobre 2026)
- Nell'interfaccia si parla di «oggetti» (EN «objects»), non di simboli; nel codice e nel database restano i nomi
  tecnici (`bpa_symbol`, `services/bpa_repo.py`).
- Decisioni: archivio **unico per l'azienda**; rinominare un simbolo usato in più BPMN lo cambia **ovunque** (prima si
  vede dove); all'**incolla** si sceglie ogni volta tra stessi simboli e simboli nuovi; i simboli senza occorrenze
  **restano** (l'Amministratore può eliminarli).
- ✅ Tabelle (`services/bpa_repo.py`): `bpa_symbol` (identificativo univoco nascosto, nome IT/EN unico per tipo,
  attributi), `bpa_attribute_def` (attributi per tipo definiti dall'Amministratore), `bpa_occurrence` (in quali BPMN e
  con quale elemento), `bpa_relation` (contiene, precede, attiva, crea, input, output, attaccato a, messaggio). Viste
  SQL per consultarle come tabelle: `bpa_view_<tipo>` (una colonna per attributo) e `bpa_view_relations`.
- ✅ Tipi di simbolo: pool, lane, attività (tutti i tipi di task), sottoprocesso, evento, gateway, oggetto dati,
  archivio dati. Elementi senza nome o con un nome generico dell'app (Inizio, Fine, Ruolo da definire) non entrano
  finché non hanno un nome loro; «precede» li attraversa.
- ✅ Aggiornamento a ogni salvataggio (e alla creazione da una variante: stesso nome = stesso simbolo, es. la stessa
  attività in due varianti). Conflitti di nome non decisi: il server risponde 409 e l'editor chiede.
- ✅ Editor: nome già usato per quel tipo → «Riutilizza» (stesso simbolo) o «Rinomina»; rinomina di un simbolo usato
  altrove → elenco dei BPMN dove cambierà; nomi rinominati in un altro BPMN dopo l'apertura → al salvataggio vale il
  nome nuovo e il modello si ricarica.
- ✅ Pannello delle proprietà a destra (accanto al diagramma, il logo bpmn.io resta visibile): Attributi (nome e
  attributi modificabili, salvati subito), Occorrenze (link ai BPMN, si aprono in una nuova scheda sull'elemento),
  Relazioni.
- ✅ Copia/incolla di attività, eventi, gateway, sottoprocessi, oggetti dati, annotazioni e frecce tra loro, anche tra
  schede (clipboard nel browser); pulsanti Copia/Incolla per iPad e Ctrl/Cmd+C/V.
- ✅ Amministrazione > «Attributi dei simboli BPMN»: per tipo, Nome e Identificativo di sistema; testo breve, testo
  lungo, numero intero/decimale, data, data e ora, sì/no, elenco a scelta singola o multipla, link, email; obbligatorio,
  un valore per lingua, aiuto, ordine; elenco ed eliminazione dei simboli senza occorrenze.
- ✅ Eventi e gateway (operatori AND/OR/XOR, eventi di inizio/fine/intermedi/di confine) sempre nell'archivio, con
  sottotipo e trigger (messaggio, timer, segnale...). Senza un nome proprio (o con «Inizio», «Fine», «Ruolo da
  definire») sono simboli della sola occorrenza: non condivisi, senza controllo dei nomi doppi, eliminati con
  l'elemento; dando loro un nome diventano simboli condivisibili. I modelli già registrati si completano all'avvio.
- ✅ Amministrazione: tabella dei simboli del tipo scelto con i valori degli attributi e ricerca; una riga apre lo
  stesso pannello delle proprietà dell'editor (`static/bpa_panel.js`, condiviso), con gli attributi modificabili.
- Da fare: pool e lane nel copia/incolla, relazioni aggiunte a mano (non dal disegno).

## BPA — Oggetti per dataset e consolidamento (16 ottobre 2026)
- Decisione: l'archivio resta unico per l'azienda, ma **i nomi si confrontano solo dentro il dataset**. All'invio di una
  variante **guida il nome inglese**: stesso tipo e stesso nome inglese di un oggetto già in un BPMN dello stesso dataset
  = stesso oggetto (il modello italiano prende il nome italiano dell'oggetto); altrimenti un oggetto nuovo, anche se un
  altro dataset ne ha uno con lo stesso nome.
- ✅ Editor: «Riutilizza» / «Rinomina» solo per i nomi già usati nel dataset del modello (nella lingua del modello); per
  un oggetto rinominato si controllano anche i dataset in cui compare. Lo stesso nome in un altro dataset = oggetto nuovo.
- ✅ Gli oggetti già condivisi tra dataset diversi restano come sono.
- ✅ Amministrazione > oggetti di un tipo: colonna «Dataset» e **consolidamento**: si spuntano due o più oggetti, si
  sceglie quello da tenere (master); occorrenze e relazioni passano al master, gli altri si eliminano, in tutti i BPMN
  gli elementi prendono i nomi del master; attributi del master, completati con quelli che non ha. Rifiutato se il nome
  del master è già di un altro oggetto in uno dei dataset coinvolti.

## Amministrazione > LLM (13 ottobre 2026)
- ✅ Pagina dell'amministratore (IT/EN) per configurare l'LLM: attivo/disattivo, indirizzo dell'API (gateway o
  proxy), chiave API, timeout, tentativi; per ogni uso (mapping, controllo di pertinenza, Control Tower,
  assistenti/spiegazioni/traduzioni) modello, effort e tetto di output; tetto di spesa del mapping; listino dei prezzi
  per le stime. «Salva e verifica la connessione» chiede all'API ogni modello configurato (nessun costo).
- ✅ Chiave API cifrata con Fernet (`cryptography`): chiave di cifratura da `APP_SECRET_KEY` o file generato in
  `backend/data/secret/` (fuori da git, permessi 600); in pagina solo le ultime 4 cifre. Senza chiave salvata vale
  quella dell'ambiente (`ANTHROPIC_API_KEY`).
- ✅ Tutte le chiamate passano da `services/llm.py` (client, modello, effort, prezzi): le impostazioni valgono subito,
  senza riavviare.
- Da fare: altri fornitori (es. Claude su Amazon Bedrock, Google Vertex AI, Microsoft Foundry), registro della spesa
  per mese e tetto di spesa mensile.

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

## Repository BPA — tassonomia dei processi (16 ottobre 2026)
Il repository BPA è autonomo dai dataset: il «digital twin» dei processi dell'azienda. Si parte dalla tassonomia dei
processi; poi altri cataloghi e oggetti.
- ✅ **Ruoli** (unici per l'azienda, in Amministrazione > Utenti): **Process Architect** (modifica tassonomia e modelli,
  importa dalla staging area) e **Process Viewer** (sola lettura). L'Amministratore non entra nel repository. Nel menu:
  «Repository BPA».
- ✅ **Staging area**: tutti i BPMN generati dalle varianti (Process Overview > «Invia alla BPA») restano dove sono.
  L'import è una **copia** nel repository, assegnata a un processo; la variante resta in staging (e per il Process
  Overlay). La staging area mostra processo, dataset, casi e dove un modello è già stato importato.
- ✅ **Tassonomia** (`bpa_process`, `services/bpa_taxonomy.py`): albero di processi distinti dalle attività dei BPMN;
  codice interno automatico (PR-0001…), nome e descrizione in italiano e in inglese (si scrive nella lingua attiva,
  l'altra si traduce con l'LLM se non era stata scritta a parte).
- ✅ **Pagina «Repository BPA»** (`/bpa`) come un esplora risorse: albero a sinistra (apri/chiudi, ricerca, frecce da
  tastiera, trascina un processo su un altro per spostarlo, o nella fascia in fondo per portarlo al primo livello), a
  destra gli attributi del processo (codice, nome, descrizione, «Dentro a») e sotto i modelli BPMN assegnati: Apri,
  Rinomina, Togli; «+ Nuovo BPMN» (pool con il nome del processo) e «Dalla staging area».
- ✅ I modelli del repository (`bpa_model` con `stage = "repository"`, senza dataset: provenienza in
  `source_model_id` / `source_config_id`) si aprono nello stesso editor delle varianti (Process Architect) o in una
  vista in sola lettura con il pannello delle proprietà (Process Viewer). Nel repository i nomi degli oggetti sono unici
  per tipo in tutto il repository; all'import un elemento con lo stesso nome inglese di un oggetto già nel repository
  diventa un'occorrenza di quello, altrimenti resta l'oggetto della variante.
- ✅ **Process Overlay con un BPMN del repository**: nella scelta del BPMN di riferimento, accanto alle varianti della BPA
  del dataset, i modelli del repository (per l'analista che è anche Process Viewer o Process Architect). Il modello resta
  nel repository: dall'analisi si apre in sola lettura. Si sceglie l'oggetto guida dei casi (proposto quello del
  modello, se il dataset lo ha). I task si legano alle attività dei dati per posizione (modelli importati da una
  variante) o per nome, anche tradotto con i nomi dei BPMN del dataset. Il riferimento è ora nella tabella
  `bpa_overlay_ref` (uno per dataset: modello e oggetto guida); quelli scelti prima si copiano all'avvio. Chi non ha un
  ruolo nel repository vede il confronto scelto da un collega ma non apre il modello né lo sceglie.
- ✅ **Attributi dei processi** definiti dall'Amministratore (Amministrazione > Attributi della BPA, scheda
  «Processo (tassonomia)», stessi tipi di dato dei simboli; Codice, Nome e Descrizione sono di sistema). Nel repository
  compaiono sotto nome e descrizione e si salvano subito (Process Architect); in sola lettura per il Process Viewer.
- ✅ **Sposta** un modello su un altro processo della tassonomia.
- ✅ Nel repository ogni modello dice in quali **Process Overlay** è il BPMN di riferimento (dataset · processo);
  togliendolo dal repository lo si avvisa e quei riferimenti si tolgono.
- Parcheggiato: altri cataloghi e oggetti del digital twin.

## BPA — staging solo per il Process Overlay, archivio solo nel repository (16 ottobre 2026)
- Decisione: nell'analisi i BPMN delle varianti (staging) servono **solo a generare il Process Overlay**: non sono
  nell'archivio degli oggetti. Niente controllo dei nomi già usati, niente pannello delle proprietà (attributi,
  occorrenze, relazioni), niente oggetti condivisi; l'editor resta per il disegno, bilingue, con copia/incolla.
  All'avvio le occorrenze registrate prima per i BPMN della staging si tolgono (gli oggetti rimasti senza occorrenze e
  senza valori si eliminano; quelli con valori restano tra gli «Oggetti senza occorrenze»).
- ✅ **Il consolidamento avviene all'import nel repository**: un elemento con lo stesso tipo e lo stesso nome inglese di
  un oggetto del repository diventa un'occorrenza di quello, gli altri sono oggetti nuovi. Il Process Architect può poi
  consolidare a mano più oggetti del repository in uno.
- ✅ **«Attributi della BPA»** passa dall'Amministratore al **Process Architect** (Repository BPA > «◇ Attributi della
  BPA», `/bpa/attributes`, `routers/bpa_attributes.py`): attributi degli oggetti dei BPMN del repository e dei processi
  della tassonomia; la tabella degli oggetti mostra solo quelli del repository, con i processi (codici) in cui compaiono.
- Sostituisce le decisioni «Oggetti per dataset e consolidamento» del 16 ottobre per la parte staging.
