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
- ✅ **Tetto di spesa del mapping AI** (default 0,50 € per elaborazione,
  `AI_MAPPING_BUDGET_EUR`): limiti di output per chiamata calcolati prima di
  chiamare Claude, effort basso, risposta compatta (solo colonne utili, chiavi
  corte); se il dataset è troppo grande si ferma prima di spendere. Costo
  mostrato in attesa e in revisione.
- ⬜ **Mapping misto**: regole + dizionario SAP open source per ciò che si
  riconosce con certezza, Claude solo per le tabelle/colonne dubbie.
- ✖ **Anteprima del processo e "KPI e analisi possibili"**: provate e tolte dal
  Modulo 1. Rallentavano la generazione del dataset e anticipavano ragionamenti
  che appartengono all'analisi. Dalla revisione si conferma e si genera il dataset.
  Da riprendere nel modulo di analisi: process overview filtrabile per
  dimensioni, control tower sui KPI più significativi (libreria di riferimento
  in `services/business_kpi_library.py`, da arricchire con gli esempi degli
  utenti), dettaglio KPI, root cause, suggerimenti di dati da integrare.

## 4. Limiti del motore OCEL attuale (debito tecnico)
- ⬜ Eventi da colonna "attività"/tipo movimento (es. EKBE.VGABE), non solo
  una colonna data = un evento.
- ⬜ Change log (CDHDR/CDPOS, audit trail) → eventi di modifica.
- ⬜ Storia degli attributi degli oggetti nel tempo (oggi timestamp fittizio 1970).
- ⬜ Relazioni oggetto-oggetto (OCEL 2.0).
- ⬜ Controlli di qualità indipendenti dal processo: oggi il "caso" è fisso su
  PurchaseOrder; usare l'oggetto principale dichiarato nell'assessment.

## 5. Ostacoli non tecnici
- ⬜ **Pseudonimizzazione** dei dati personali con regola per colonna (la
  domanda è già nell'assessment).
- ⬜ **Tracciabilità**: ogni evento riporta tabella e riga di origine.

## Altre idee
- ⬜ Uso del contenuto dei documenti caricati (data dictionary, manuali) come
  contesto per il mapping, non solo i nomi delle attività BPMN.
