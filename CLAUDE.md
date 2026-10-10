# APP Process Mining — note per chi sviluppa

- Si lavora sempre sul branch principale (`claude/ai-process-mining-brainstorm-7oswos`), senza branch paralleli.
- **Ogni sviluppo è in italiano e in inglese.** L'interfaccia è bilingue con selettore IT | EN:
  - nei template ogni testo passa da `_("testo italiano")` (con segnaposto: `_("... {n} ...", n=valore)`);
  - i testi prodotti dal codice e mostrati dopo (profilo, controlli, motivazioni) sono `msg(...)` di `app/i18n.py`,
    tradotti al momento di mostrarli con `|tr`; nel database si salvano con `to_text`;
  - ogni testo nuovo ha la sua voce inglese in `backend/app/i18n_en.py` (chiave = testo italiano);
  - `python -m pytest backend/tests` verifica che non manchino traduzioni.
- Le spiegazioni scritte da Claude seguono la lingua attiva (`process_context.language`).
- I testi che Claude scrive e l'app salva per mostrarli dopo (es. domande, KPI, titoli dei grafici della Control Tower)
  si salvano in tutte e due le lingue, `{"it": ..., "en": ...}`, e si mostrano nella lingua attiva; i nomi dei tipi di
  oggetto dentro le frasi vanno nella lingua attiva, al singolare o al plurale.
- Nell'interfaccia il modello si chiama sempre «LLM» (es. «Chiedi all'LLM», «Proponi con l'LLM»), mai «Claude».
- Python 3.11 (devcontainer); analisi con pm4py.
