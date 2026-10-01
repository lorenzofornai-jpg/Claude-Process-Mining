# Checklist di assessment del processo

Compilata dal Data Engineer nell'app (pagina **Assessment** del processo) prima di caricare i dati sorgente.
Serve a ottimizzare la costruzione del dataset per l'analisi (formato OCEL 2.0): le risposte e le attività lette dai BPMN vengono date
all'AI che propone il mapping e al controllo di pertinenza dei dati. Le voci con * sono essenziali e contano
nell'indicatore di completamento.

Generata da `backend/app/services/assessment.py`, che è la fonte di verità: per cambiare le domande si modifica quel file.

## 1. Obiettivi dell'analisi

*Perché serve:* Decidono quali oggetti, eventi e attributi servono davvero: è su questo che si valuta se i dati caricati bastano, e l'AI dà priorità alle colonne utili a queste domande.

- Cosa vuoi analizzare? * — Tempi di attraversamento e colli di bottiglia / Conformità al processo standard (varianti, deviazioni) / Rilavorazioni e modifiche (prezzi, quantità, date, blocchi) / Automazione e attività manuali / Compliance e segregazione dei compiti / Performance per fornitore / cliente / reparto / Puntualità di pagamenti o incassi
- Domande di business a cui l'analisi deve rispondere *
- KPI già in uso (opzionale)

## 2. Perimetro

*Perché serve:* Diventa il filtro dell'estrazione e permette di verificare che le tabelle caricate coprano davvero il periodo e le società richieste.

- Società / unità organizzative incluse *
- Volume indicativo (documenti principali per anno) — meno di 10.000 / 10.000 - 100.000 / 100.000 - 1 milione / oltre 1 milione / non so
- Periodo dal *
- Periodo al *
- Esclusioni (opzionale)

## 3. Sistemi a supporto

*Perché serve:* Dicono da dove arriveranno le tabelle e quali passaggi del processo non lasceranno traccia nei dati: senza questo si scopre a mapping finito che manca un pezzo di processo.

- Sistemi coinvolti nel processo * — elenco: nome, tipo, ruolo nel processo
- Ci sono passaggi gestiti fuori dai sistemi (email, Excel, carta)? — No / Sì, alcuni / Sì, molti / Non so
- Quali? (opzionale)
- Integrazioni tra i sistemi (opzionale)

## 4. Il processo

*Perché serve:* L'oggetto principale e gli eventi di inizio e fine definiscono come leggere il processo: guidano la scelta di chiavi ed eventi nel mapping e i controlli di qualità sul dataset.

- Oggetto principale del processo *
- Altri oggetti coinvolti
- Evento che fa iniziare il processo *
- Evento che lo conclude *
- Varianti note (opzionale)

## 5. Dati ed estrazione

*Perché serve:* Sono i problemi che più spesso bloccano la trasformazione: saperli prima evita rifare estrazioni e mapping.

- Come verranno estratti i dati? * — Export di tabelle (es. SE16 / tabelle DB) / Report standard del sistema / Query sul database / API / connettore / Non ancora definito
- Chi fa l'estrazione (opzionale)
- Tabelle o report già individuati (opzionale)
- È disponibile lo storico delle modifiche? * — Sì (es. CDHDR/CDPOS, audit trail) / No / Non so
- Le date nei dati hanno anche l'ora? * — Sì, data e ora / Solo la data / Dipende dalla tabella / Non so
- Fuso orario dei timestamp (opzionale)
- I dati contengono dati personali? * — Sì, serve pseudonimizzarli / Sì, ma possono restare in chiaro / No / Non so
- Campi o tabelle personalizzate (opzionale)
## 6. Documenti di supporto

*Perché serve:* manuali, procedure e disegni spiegano tabelle, campi e stati. Dai file BPMN si leggono i nomi delle attività, usati per chiamare gli eventi del log. Almeno un documento conta come voce essenziale.

Tipi: Disegno del processo (BPMN o altro diagramma); Procedura operativa; Manuale di sistema; Data dictionary / descrizione tabelle; Altro. Max 25 MB per file.
