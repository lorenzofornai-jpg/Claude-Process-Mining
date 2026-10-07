# APP Process Mining — note per chi sviluppa

- Si lavora sempre sul branch principale (`claude/ai-process-mining-brainstorm-7oswos`), senza branch paralleli.
- **Ogni sviluppo è in italiano e in inglese.** L'interfaccia è bilingue con selettore IT | EN:
  - nei template ogni testo passa da `_("testo italiano")` (con segnaposto: `_("... {n} ...", n=valore)`);
  - i testi prodotti dal codice e mostrati dopo (profilo, controlli, motivazioni) sono `msg(...)` di `app/i18n.py`,
    tradotti al momento di mostrarli con `|tr`; nel database si salvano con `to_text`;
  - ogni testo nuovo ha la sua voce inglese in `backend/app/i18n_en.py` (chiave = testo italiano);
  - `python -m pytest backend/tests` verifica che non manchino traduzioni.
- Le spiegazioni scritte da Claude seguono la lingua attiva (`process_context.language`).
- Python 3.11 (devcontainer); analisi con pm4py.
