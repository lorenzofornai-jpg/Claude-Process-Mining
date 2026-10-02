"""Libreria di riferimento dei KPI / casi di valore di business per i processi
piu' noti, usata per guidare la valutazione "KPI tipici del processo".

Non e' un elenco da copiare: Claude riconosce la famiglia di processo (anche da
nome processo, assessment e tabelle) e adatta i KPI, aggiungendone altri se
rilevanti. Ogni voce: nome, domanda del C-level, leva di valore, chi la
segue. Per arricchirla basta aggiungere voci qui (es. con gli esempi forniti
dagli utenti, come touchless collection, payment terms mismatch e DSO per
l'Accounts Receivable).
"""
from __future__ import annotations

LIBRARY: dict[str, list[dict]] = {
    "Accounts Receivable / Credit-to-Cash": [
        {"kpi": "DSO (Days Sales Outstanding)", "owner": "CFO",
         "question": "Quanti giorni di fatturato sono immobilizzati nei crediti e quanta cassa posso liberare?",
         "value": "Capitale circolante: ogni giorno di DSO in meno libera cassa pari a un giorno di fatturato."},
        {"kpi": "Touchless collection rate", "owner": "CFO / Head of Shared Services",
         "question": "Quanta parte degli incassi viene abbinata e chiusa senza intervento manuale?",
         "value": "Costo per transazione e FTE del cash application; meno partite non abbinate."},
        {"kpi": "Payment terms mismatch", "owner": "CFO / Credit Manager",
         "question": "Le condizioni di pagamento applicate in fattura sono quelle concordate in anagrafica e in ordine?",
         "value": "Termini concessi oltre lo standard allungano il DSO senza decisione esplicita (revenue leakage finanziario)."},
        {"kpi": "Best possible DSO vs DSO", "owner": "CFO",
         "question": "Quanta parte del DSO dipende da ritardi dei clienti e quanta dai termini concessi?"},
        {"kpi": "% crediti scaduti e aging", "owner": "Credit Manager",
         "question": "Quanto del portafoglio è scaduto e da quanto?"},
        {"kpi": "Tempo di risoluzione contestazioni / deductions", "owner": "Credit Manager / Customer Service",
         "question": "Quanto restano bloccati gli incassi per contestazioni e note di credito?"},
        {"kpi": "Efficacia dei solleciti (dunning)", "owner": "Credit Manager",
         "question": "I solleciti portano davvero all'incasso e dopo quanto?"},
        {"kpi": "Incassi non applicati (unapplied cash)", "owner": "Head of Shared Services"},
        {"kpi": "Perdite su crediti / write-off", "owner": "CFO"},
    ],
    "Accounts Payable": [
        {"kpi": "DPO (Days Payable Outstanding)", "owner": "CFO / Treasurer",
         "question": "Sto usando bene i termini di pagamento concessi dai fornitori?",
         "value": "Capitale circolante: pagare alla scadenza, non prima."},
        {"kpi": "Pagamenti anticipati rispetto alla scadenza", "owner": "CFO / Treasurer",
         "question": "Quanta cassa esce prima del dovuto?", "value": "Cassa lasciata sul tavolo ogni mese."},
        {"kpi": "Pagamenti in ritardo", "owner": "CFO / CPO",
         "question": "Quanto pago in ritardo e con quali conseguenze (penali, relazione con i fornitori)?"},
        {"kpi": "Sconti cassa persi", "owner": "CFO", "value": "Valore economico diretto e misurabile."},
        {"kpi": "Touchless invoice processing (first-pass match)", "owner": "Head of Shared Services",
         "question": "Quante fatture passano dal ricevimento al pagamento senza intervento manuale?",
         "value": "Costo per fattura, FTE, tempi di chiusura."},
        {"kpi": "Payment terms mismatch (ordine vs fattura vs anagrafica)", "owner": "CPO / CFO",
         "question": "Paghiamo con i termini negoziati dagli acquisti?"},
        {"kpi": "Pagamenti duplicati", "owner": "CFO / Internal Audit"},
        {"kpi": "Fatture bloccate e tempo di sblocco", "owner": "Head of Shared Services"},
    ],
    "Purchase-to-Pay": [
        {"kpi": "Maverick buying / spesa fuori processo", "owner": "CPO",
         "question": "Quanta spesa passa fuori dai canali e dai contratti negoziati?",
         "value": "Risparmi negoziati non realizzati, rischio di compliance."},
        {"kpi": "Ordini creati dopo la fattura (PO after invoice)", "owner": "CPO / Internal Audit"},
        {"kpi": "Three-way match al primo colpo", "owner": "CFO / CPO"},
        {"kpi": "Modifiche di prezzo/quantità dopo l'approvazione", "owner": "CPO",
         "question": "Quante volte cambiamo un ordine già approvato e perché?"},
        {"kpi": "Puntualità dei fornitori (OTD)", "owner": "COO / CPO"},
        {"kpi": "Lead time richiesta → ordine → entrata merce", "owner": "COO"},
        {"kpi": "Segregazione dei compiti (stesso utente su attività incompatibili)", "owner": "Internal Audit"},
        {"kpi": "Automation rate e costo per ordine", "owner": "CPO / Head of Shared Services"},
    ],
    "Order-to-Cash / Order Management": [
        {"kpi": "Perfect order rate / OTIF", "owner": "COO / CCO",
         "question": "Quanti ordini consegno puntuali, completi e senza errori?", "value": "Soddisfazione e fidelizzazione clienti."},
        {"kpi": "Touchless order rate", "owner": "COO", "value": "Costo di gestione ordine, scalabilità."},
        {"kpi": "Modifiche d'ordine (rework)", "owner": "COO"},
        {"kpi": "Blocchi di credito e tempo di rilascio", "owner": "CFO / Credit Manager",
         "question": "Quanto fatturato è fermo in attesa di sblocco credito?"},
        {"kpi": "Order-to-cash cycle time", "owner": "CFO"},
        {"kpi": "Accuratezza di fatturazione (note di credito)", "owner": "CFO / CCO"},
        {"kpi": "Revenue leakage (prezzi forzati, sconti non autorizzati, merce omaggio)", "owner": "CFO / CCO"},
    ],
}
