"""Controllo di pertinenza dei dati caricati rispetto al processo.

Gira subito dopo l'upload, PRIMA della chiamata di mapping completa (che manda
a Claude tutte le colonne con statistiche e valori di esempio ed e' la parte
costosa): se i dati sono chiaramente di un altro processo (es. tabelle P2P
caricate su "Account Receivable") l'utente viene fermato prima di spendere
token per un mapping senza senso.

La chiamata e' volutamente piccola: solo nomi di tabelle e colonne, piu' due
valori di esempio per colonna, risposta strutturata di poche righe, effort
basso. Se fallisce per qualunque motivo (rete, chiave assente, rifiuto) non
blocca nulla: il controllo e' un aiuto, non un requisito.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel

from app.config import RELEVANCE_CHECK_MODEL
from app.connectors.base import TableSchema


class RelevanceVerdict(BaseModel):
    verdict: Literal["coerente", "dubbio", "non_coerente"]
    detected_process: str  # processo a cui i dati sembrano appartenere, es. "Purchase-to-Pay"
    reason: str  # una o due frasi in italiano


_SYSTEM_PROMPT = """\
Sei un controllo preliminare in una piattaforma di process mining. Ricevi il nome di un processo di
business (con, se compilato, l'assessment: oggetto principale, sistemi coinvolti, obiettivi) e lo schema (nomi tabelle, nomi colonne, pochi valori di esempio) dei file che un data
engineer ha caricato per quel processo. Devi dire se i dati sono plausibilmente quelli giusti per
quel processo, prima che parta un'elaborazione costosa.

- "coerente": le tabelle sono tipiche di quel processo (anche solo in parte).
- "dubbio": non e' chiaro (nomi opachi, processo con nome generico, mix di tabelle).
- "non_coerente": le tabelle appartengono chiaramente a un processo diverso (es. ordini d'acquisto,
  entrate merci e fatture fornitore caricate su un processo di crediti verso clienti).

Riconosci anche le tabelle SAP standard dal nome (es. EKKO/EKPO ordini d'acquisto, MSEG/MKPF
movimenti merce, RBKP/RSEG fatture fornitore, VBAK/VBAP ordini cliente, BSID/BSAD partite clienti).
Nel dubbio preferisci "dubbio" a "non_coerente": un falso allarme blocca il lavoro dell'utente.
reason: in italiano, una o due frasi concrete che citano le tabelle decisive.
"""


def check_relevance(process_context: dict, tables: list[TableSchema]) -> RelevanceVerdict | None:
    """Ritorna il verdetto, o None se il controllo non e' stato possibile."""
    try:
        import anthropic

        client = anthropic.Anthropic()
        payload = {
            "process_name": process_context.get("process_name", ""),
            # risposte dell'assessment (oggetto principale, sistemi, obiettivi...), se compilato
            "process_assessment": process_context.get("assessment"),
            "tables": [
                {
                    "name": t.name,
                    "columns": [
                        {"name": c.name, "examples": [str(v)[:40] for v in c.sample_values[:2]]}
                        for c in t.columns
                    ],
                }
                for t in tables
            ],
        }
        response = client.messages.parse(
            model=RELEVANCE_CHECK_MODEL,
            max_tokens=4000,
            output_config={"effort": "low"},
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            output_format=RelevanceVerdict,
        )
        if response.stop_reason != "end_turn":
            print(f"Controllo pertinenza saltato (stop_reason={response.stop_reason}).")
            return None
        return response.parsed_output
    except Exception as exc:  # il controllo non deve mai bloccare l'upload
        print(f"Controllo pertinenza non riuscito ({exc!r}): si prosegue senza.")
        return None
