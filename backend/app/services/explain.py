"""«Chiedi a Claude»: spiegazione su richiesta di un messaggio dell'app.

Accanto ai messaggi del profilo dei dati, della revisione del mapping e dei
controlli di qualita' l'utente puo' chiedere a Claude cosa significano, cosa
comportano e cosa fare nell'app. Prima dell'invio vede un costo indicativo e
conferma: la chiamata consuma token.

La richiesta e' piccola e mirata: il messaggio, l'eventuale domanda
dell'utente, il contesto del processo (nome e assessment) e, se il messaggio
riguarda una tabella, le sue colonne con pochi valori di esempio. La risposta
arriva nella lingua dell'interfaccia.
"""
from __future__ import annotations

import json

from app.config import EXPLAIN_MODEL
from app.connectors.base import TableSchema
from app.services.ai_mapping import _CHARS_PER_TOKEN, _FALLBACK_PRICE, _PRICES_USD_PER_MTOK

# Limite largo: il modello puo' usare parte dei token per ragionare prima di rispondere, e con un
# limite stretto la risposta veniva troncata a meta' frase. Il costo massimo mostrato tiene conto di questo.
MAX_OUTPUT_TOKENS = 2500
TYPICAL_OUTPUT_TOKENS = 600

SYSTEM_PROMPT = """\
Sei l'assistente di un'app di process mining. L'utente (Data Engineer, non necessariamente esperto di
process mining o del sistema sorgente) non ha capito un messaggio dell'app e ti chiede di spiegarlo.

Come funziona l'app, per indicare azioni concrete:
1. Dati sorgente: carica i file (CSV/ZIP); il "Profilo dei dati" segnala problemi (bloccante/attenzione/info)
   con impatto e cosa fare; i dati si correggono solo ricaricando un'estrazione diversa.
2. Revisione mapping: il "Modello proposto" ha card di oggetti ed eventi con "Accetta tutto" / "Rifiuta" e,
   sugli eventi, "Aggiungi collegamento" (collega gli eventi a un oggetto di un'altra tabella tramite una colonna
   in comune). Nel "Dettaglio per colonna" ogni riga si accetta, si rifiuta o si corregge con "Modifica"
   ("Questa colonna è…": chiave/attributo di un oggetto, data/ora di un evento, colonna attività con la tabella
   "valore = nome attività", attributo di un evento, collegamento evento→oggetto).
3. Risultato: dataset OCEL 2.0 e "Controlli di qualità". Dai passi in alto si torna alla revisione e si
   rigenera lo stesso dataset; "Utilizza per l'analisi" lo rende disponibile al Data Analyst.

Rispondi nella lingua indicata da "language" (it = italiano, en = inglese), in modo semplice e concreto,
massimo 180 parole, in tre parti brevi: cosa significa, cosa comporta per l'analisi, cosa fare nell'app
(azioni precise tra quelle sopra, oppure "nessuna azione necessaria"). Se c'e' una domanda dell'utente,
rispondi prima a quella. Usa i dati di contesto (processo, tabella) per essere specifico; non inventare
funzioni che l'app non ha. Testo semplice: niente titoli markdown, al massimo elenchi con "-".
"""


def _price() -> tuple[float, float]:
    return _PRICES_USD_PER_MTOK.get(EXPLAIN_MODEL, _FALLBACK_PRICE)


def build_payload(*, language: str, page: str, message: str, question: str, context: dict,
                  table: TableSchema | None) -> str:
    payload = {
        "language": language,
        "page": page,
        "message": message[:4000],
        "user_question": (question or "").strip()[:1000] or None,
        "process": {k: v for k, v in context.items() if k in ("process_name", "assessment")},
        "table": None if table is None else {
            "name": table.name, "rows": table.row_count,
            "columns": [{"name": c.name, "type": c.inferred_type, "examples": [str(v)[:30] for v in c.sample_values[:2]]}
                        for c in table.columns[:60]],
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def estimate(payload: str) -> dict:
    """Costo indicativo (risposta di lunghezza tipica) e massimo (risposta piu' lunga possibile), in dollari."""
    price_in, price_out = _price()
    tokens_in = int(len(SYSTEM_PROMPT + payload) / _CHARS_PER_TOKEN) + 50
    typical = (tokens_in * price_in + TYPICAL_OUTPUT_TOKENS * price_out) / 1_000_000
    maximum = (tokens_in * price_in + MAX_OUTPUT_TOKENS * price_out) / 1_000_000
    return {"cost_usd": round(typical, 4), "max_usd": round(maximum, 4), "model": EXPLAIN_MODEL}


def ask(payload: str) -> dict:
    """Chiama Claude. Ritorna {"answer", "cost_usd"}; solleva eccezione se la chiamata non riesce."""
    import anthropic

    client = anthropic.Anthropic()
    request = dict(model=EXPLAIN_MODEL, max_tokens=MAX_OUTPUT_TOKENS, system=SYSTEM_PROMPT,
                   messages=[{"role": "user", "content": payload}])
    try:
        # ragionamento minimo: e' una spiegazione breve, non un problema difficile
        response = client.messages.create(**request, output_config={"effort": "low"})
    except (TypeError, anthropic.BadRequestError):
        response = client.messages.create(**request)  # modello che non accetta il parametro
    price_in, price_out = _price()
    usage = response.usage
    cost = ((usage.input_tokens or 0) * price_in + (usage.output_tokens or 0) * price_out) / 1_000_000
    answer = "".join(b.text for b in response.content if getattr(b, "type", "") == "text").strip()
    return {"answer": answer, "cost_usd": round(cost, 4), "truncated": response.stop_reason == "max_tokens"}


def available() -> bool:
    """Claude e' utilizzabile su questo server (credenziali presenti)?"""
    try:
        import anthropic

        anthropic.Anthropic()
        return True
    except Exception:
        return False
