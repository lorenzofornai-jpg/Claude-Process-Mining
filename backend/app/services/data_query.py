"""Interrogazioni in sola lettura sui dati caricati, per gli assistenti (strumenti che Claude puo' chiamare).

Claude non riceve i file interi (costerebbe troppo e non servirebbe): chiede quello che gli serve per rispondere
e riceve risultati piccoli e gia' contati.

- query_rows: righe che soddisfano dei filtri, o i conteggi per una o piu' colonne (group_by);
- compare_columns: quanti valori di una colonna si trovano in un'altra (anche usando solo una parte del valore):
  serve a capire se un collegamento tra tabelle funziona davvero.

Funziona su qualunque tabella e sistema sorgente; le colonne calcolate del mapping (es. CHANGE_KIND) sono gia'
presenti nei dati passati.
"""
from __future__ import annotations

import json
from collections import Counter

MAX_SAMPLE = 20
MAX_GROUPS = 40
MAX_COLS = 15
MAX_CELL = 80

OPS = ["eq", "ne", "in", "not_in", "contains", "startswith", "empty", "not_empty", "gt", "lt"]

TOOLS = [
    {
        "name": "query_rows",
        "description": "Legge i dati caricati (sola lettura). Con group_by restituisce quante righe per ogni "
                       "combinazione di valori (le piu' frequenti); senza, alcune righe di esempio. I filtri in where "
                       "sono in AND. Usalo per verificare con i numeri prima di rispondere o proporre una modifica.",
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "where": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"column": {"type": "string"}, "op": {"type": "string", "enum": OPS},
                                   "value": {"description": "Valore (stringa) o lista di valori per in / not_in."}},
                    "required": ["column", "op"]}},
                "group_by": {"type": "array", "items": {"type": "string"}, "description": "Colonne da contare."},
                "columns": {"type": "array", "items": {"type": "string"}, "description": "Colonne delle righe di esempio."},
                "limit": {"type": "integer", "description": f"Righe di esempio (massimo {MAX_SAMPLE})."},
            },
            "required": ["table"],
        },
    },
    {
        "name": "compare_columns",
        "description": "Quanti valori distinti di una colonna (eventualmente solo i caratteri da start per length) "
                       "si trovano in un'altra colonna, con esempi di valori trovati e non trovati. Serve a verificare "
                       "un collegamento tra tabelle (es. il documento che ha chiuso una partita, un numero dentro un "
                       "valore composto).",
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {"type": "string"}, "column": {"type": "string"},
                "start": {"type": "integer"}, "length": {"type": "integer"},
                "in_table": {"type": "string"}, "in_column": {"type": "string"},
            },
            "required": ["table", "column", "in_table", "in_column"],
        },
    },
]
NAMES = {t["name"] for t in TOOLS}


def _s(v) -> str:
    s = "" if v is None else str(v).strip()
    return "" if s in ("nan", "None", "NaT") else s


def _num(s: str):
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def _match(value: str, op: str, target) -> bool:
    if op == "empty":
        return value == ""
    if op == "not_empty":
        return value != ""
    if op in ("in", "not_in"):
        values = {_s(x) for x in (target if isinstance(target, list) else [target])}
        return (value in values) == (op == "in")
    t = _s(target[0] if isinstance(target, list) and target else target)
    if op == "eq":
        return value == t
    if op == "ne":
        return value != t
    if op == "contains":
        return t.lower() in value.lower()
    if op == "startswith":
        return value.lower().startswith(t.lower())
    if op in ("gt", "lt"):
        a, b = _num(value), _num(t)
        if a is None or b is None:   # date ISO e testi: confronto come testo
            a, b = value, t
        return a > b if op == "gt" else a < b
    raise ValueError(op)


def _columns(rows: list[dict]) -> list[str]:
    """Colonne della tabella (unione delle chiavi: le righe di un dataset OCEL non hanno tutte gli stessi attributi)."""
    return list(dict.fromkeys(k for r in rows[:2000] for k in r))


def query_rows(tables: dict[str, list[dict]], args: dict) -> dict:
    table = args.get("table")
    if table not in tables:
        return {"error": f"unknown table: {table}", "tables": sorted(tables)}
    rows = tables[table]
    cols = _columns(rows)
    where = args.get("where") or []
    for w in where:
        if w.get("column") not in cols:
            return {"error": f"unknown column in {table}: {w.get('column')}", "columns": cols}
        if w.get("op") not in OPS:
            return {"error": f"unknown operator: {w.get('op')}", "ops": OPS}
    hits = [r for r in rows if all(_match(_s(r.get(w["column"])), w["op"], w.get("value")) for w in where)]
    out = {"table": table, "rows_total": len(rows), "rows_matching": len(hits)}
    group_by = [c for c in args.get("group_by") or []]
    if group_by:
        bad = [c for c in group_by if c not in cols]
        if bad:
            return {"error": f"unknown columns in {table}: {bad}", "columns": cols}
        counts = Counter(tuple(_s(r.get(c))[:MAX_CELL] or "(vuoto)" for c in group_by) for r in hits)
        out["groups"] = [{**dict(zip(group_by, k)), "rows": n} for k, n in counts.most_common(MAX_GROUPS)]
        out["distinct_groups"] = len(counts)
        return out
    show = [c for c in args.get("columns") or [] if c in cols] or cols[:MAX_COLS]
    limit = max(1, min(int(args.get("limit") or 10), MAX_SAMPLE))
    out["sample"] = [{c: _s(r.get(c))[:MAX_CELL] for c in show[:MAX_COLS]} for r in hits[:limit]]
    return out


def compare_columns(tables: dict[str, list[dict]], args: dict) -> dict:
    a, b = args.get("table"), args.get("in_table")
    for t in (a, b):
        if t not in tables:
            return {"error": f"unknown table: {t}", "tables": sorted(tables)}
    ca, cb = args.get("column"), args.get("in_column")
    if ca not in _columns(tables[a]) or cb not in _columns(tables[b]):
        return {"error": f"unknown column: {ca} or {cb}", "columns": {a: _columns(tables[a]), b: _columns(tables[b])}}
    start = args.get("start")
    length = args.get("length")

    def part(v: str) -> str:
        if start is None:
            return v
        s = int(start)
        return v[s:s + int(length)] if length is not None else v[s:]
    values = {part(_s(r.get(ca))) for r in tables[a]} - {""}
    target = {_s(r.get(cb)) for r in tables[b]} - {""}
    found = values & target
    return {"distinct_values": len(values), "found": len(found),
            "found_pct": round(100 * len(found) / len(values), 1) if values else 0.0,
            "examples_found": sorted(found)[:5], "examples_not_found": sorted(values - target)[:5],
            "rows_in_table": len(tables[a]), "rows_with_value": sum(1 for r in tables[a] if part(_s(r.get(ca))))}


def run(name: str, args: dict, tables: dict[str, list[dict]]) -> dict:
    try:
        if name == "query_rows":
            return query_rows(tables, args or {})
        if name == "compare_columns":
            return compare_columns(tables, args or {})
    except Exception as exc:   # un input sbagliato non deve rompere la conversazione: Claude riceve l'errore
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"error": f"unknown tool: {name}"}


def converse(*, model: str, system_text: str, messages: list[dict], max_tokens: int, price: tuple[float, float],
             tables: dict[str, list[dict]] | None, action_tools: list[dict] | None = None, max_rounds: int = 4) -> dict:
    """Conversazione con Claude in cui Claude puo' leggere i dati (query_rows, compare_columns) prima di rispondere.

    Il contesto (system_text) e' in cache: i giri successivi lo rileggono a un decimo del prezzo. action_tools:
    strumenti che propongono modifiche (non vengono eseguiti qui: tornano in "actions").
    Ritorna {"answer", "actions", "cost_usd", "truncated", "queries"}."""
    import anthropic

    client = anthropic.Anthropic()
    system = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
    actions_only = list(action_tools or [])
    tools = actions_only + (TOOLS if tables is not None else [])
    price_in, price_out = price
    cost, answer, actions, queries, truncated = 0.0, [], [], 0, False
    for round_no in range(max_rounds + 1):
        last = round_no >= max_rounds
        request = dict(model=model, max_tokens=max_tokens, system=system, messages=messages)
        use = actions_only if last else tools
        if use:
            request["tools"] = use
        try:
            response = client.messages.create(**request, output_config={"effort": "low"})
        except (TypeError, anthropic.BadRequestError):
            response = client.messages.create(**request)
        u = response.usage
        cost += ((u.input_tokens or 0) * price_in + (getattr(u, "cache_creation_input_tokens", 0) or 0) * price_in * 1.25
                 + (getattr(u, "cache_read_input_tokens", 0) or 0) * price_in * 0.1
                 + (u.output_tokens or 0) * price_out) / 1_000_000
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text").strip()
        if text:
            answer.append(text)
        uses = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
        actions += [{"type": b.name, **(b.input or {})} for b in uses if b.name not in NAMES]
        truncated = response.stop_reason == "max_tokens"
        if not any(b.name in NAMES for b in uses) or tables is None:
            break
        # ogni tool_use vuole il suo risultato: i dati per le interrogazioni, una conferma per le proposte
        results = []
        for b in uses:
            if b.name in NAMES:
                queries += 1
                out = run(b.name, b.input or {}, tables)
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": json.dumps(out, ensure_ascii=False, default=str)[:12000]})
            else:
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": "Proposta mostrata all'utente, che la confermerà."})
        messages = messages + [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]
    return {"answer": "\n\n".join(answer), "actions": actions, "cost_usd": round(cost, 4), "truncated": truncated,
            "queries": queries}


def extra_cost(tokens_in: int, price: tuple[float, float], rounds: int = 4) -> float:
    """Costo in piu' (USD) se Claude usa tutte le interrogazioni: contesto dalla cache, risultati, risposta breve."""
    price_in, price_out = price
    return rounds * (tokens_in * 0.1 * price_in + 2500 * price_in + 300 * price_out) / 1_000_000
