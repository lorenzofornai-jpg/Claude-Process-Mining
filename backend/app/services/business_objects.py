"""Oggetti di business: quali oggetti entrano nel dataset, decisi prima del mapping colonna per colonna.

Una tabella non e' un oggetto. Gli oggetti di un'analisi sono le cose di cui il business misura le
prestazioni (fattura, incasso, ordine, riga d'ordine, ticket) e vanno scelti a partire dagli obiettivi
dell'assessment:
- guida («lead»): l'oggetto che l'obiettivo misura (per il DSO la fattura); uno solo;
- necessari («needed»): senza di loro la misura non si fa (l'incasso che chiude la fattura);
- di contesto («context»): altri oggetti di business presenti nei dati, proposti ma non spuntati.
Il resto non diventa oggetto: i log sono eventi di altri oggetti, le anagrafiche (cliente, prodotto) sono
di solito dimensioni con cui filtrare e confrontare, le copie si escludono.

Un oggetto puo' venire solo da una parte di una tabella (filtro per valore: righe con tipo documento RV =
fatture) e piu' oggetti dalla stessa tabella: diventa la divisione per valore della trasformazione
(object_type.split).

Due livelli, come la copertura degli obiettivi:
1. bozza senza costi (sempre): regole generiche sul profilo dei dati (chiavi, date, log, anagrafiche);
2. proposta di Claude su richiesta (costo indicativo prima): nomi di business, ruoli e filtri dagli obiettivi.
Quello che l'utente conferma guida il mapping (nomi dei tipi di oggetto, divisioni) e la Process Overview
(oggetto guida).
"""
from __future__ import annotations

import json
import re

from app.config import EXPLAIN_MODEL
from app.connectors.base import TableSchema
from app.i18n import msg
from app.services import sap_dictionary
from app.services.ai_mapping import _CHARS_PER_TOKEN, _FALLBACK_PRICE, _PRICES_USD_PER_MTOK, MappingProposal

ROLES = ("lead", "needed", "context")
BECOMES = ("events", "attributes", "excluded")
MAX_OUTPUT_TOKENS = 3000
TYPICAL_OUTPUT_TOKENS = 1200


def _words(text: str | None) -> set[str]:
    return {w for w in re.split(r"[^a-zà-ÿ0-9]+", (text or "").lower()) if len(w) >= 4}


def _pretty(table: str) -> str:
    return " ".join(p.capitalize() for p in re.split(r"[_\-\s]+", table) if p) or table


# ---------- bozza senza costi ----------

def draft(tables: list[TableSchema], profile: dict, answers: dict) -> dict:
    """Prima proposta con regole generiche: tabelle con chiave e date di fatti = oggetti di processo;
    tabelle senza chiave o con una colonna attivita' = eventi di altri oggetti; tabelle con chiave e senza
    date = anagrafiche (dimensioni). Nomi dal dizionario SAP se la tabella e' nota, altrimenti dal nome."""
    by_name = {t["name"]: t for t in profile.get("tables", [])}
    rels = profile.get("relationships", [])
    objects, not_objects = [], []
    for t in tables:
        p = by_name.get(t.name, {})
        key = p.get("key")
        planned = set(p.get("planned_dates") or {})
        facts = [d["column"] for d in p.get("dates", []) if d["column"] not in planned and d.get("min")]
        hit = sap_dictionary.lookup(t.name, [c.name for c in t.columns])
        sap_obj = hit[1]["object"][0] if hit and hit[1].get("object") else None
        # una colonna «attivita'» che e' anche il tipo dei record di una tabella con chiave (tipo ordine,
        # tipo documento) dice che oggetto e', non che operazione: la tabella resta di oggetti
        type_cols = {c["column"] for c in p.get("type_columns") or []}
        log_like = [a for a in p.get("activity_columns") or [] if a["column"] not in type_cols]
        if not key or log_like:
            linked = sorted({r["parent_table"] for r in rels if r["child_table"] == t.name})
            not_objects.append({"table": t.name, "becomes": "events",
                                "why": msg("Registra operazioni (una riga per operazione) su altri oggetti: diventa eventi di {o}.",
                                           o=", ".join(linked) or "—") if linked else
                                msg("Registra operazioni, non oggetti: diventa eventi.")})
            continue
        if not facts:
            objects.append({"name": sap_obj or _pretty(t.name), "table": t.name, "key": key, "filter": None,
                            "role": "context", "include": False, "objective": "",
                            "why": msg("Anagrafica senza date di fatti: di solito è meglio come dimensione dei documenti "
                                       "(per filtrare e confrontare) che come oggetto con una storia.")})
            continue
        types = p.get("type_columns") or []
        why = msg("Ha una chiave e date di fatti avvenuti ({d}): ha un ciclo di vita misurabile.", d=", ".join(facts[:3]))
        if types:
            tc = types[0]
            values = ", ".join(f"{v} ({n})" for v, n in list(tc["values"].items())[:6])
            why = msg("Ha una chiave e date di fatti avvenuti ({d}). Contiene tipi diversi in {c} ({v}): se sono "
                      "oggetti di business diversi, dividilo.", d=", ".join(facts[:3]), c=tc["column"], v=values)
        objects.append({"name": sap_obj or _pretty(t.name), "table": t.name, "key": key, "filter": None,
                        "role": "needed", "include": True, "objective": "", "why": why,
                        "split_hint": types[0] if types else None, "_rows": t.row_count})
    # oggetto guida: quello che somiglia all'oggetto principale dell'assessment, altrimenti il piu' numeroso
    candidates = [o for o in objects if o["include"]]
    if candidates:
        words = _words(answers.get("main_object"))
        scored = [(len(words & (_words(o["name"]) | _words(o["table"]))), o.get("_rows", 0), i) for i, o in enumerate(candidates)]
        lead = max(scored)[2]
        candidates[lead]["role"] = "lead"
        lead_table = candidates[lead]["table"]
        linked = {r["child_table"] for r in rels if r["parent_table"] == lead_table} | \
                 {r["parent_table"] for r in rels if r["child_table"] == lead_table}
        for o in candidates:
            if o["role"] != "lead" and o["table"] not in linked:
                o["role"], o["include"] = "context", False
    for o in objects:
        o.pop("_rows", None)
    return {"source": "draft", "objects": objects, "not_objects": not_objects, "summary": None}


def split_object(obj: dict, column: str, values: list[str]) -> list[dict]:
    """Un oggetto diventa uno per valore della colonna (nomi da rinominare), con il filtro."""
    out = []
    for v in values:
        out.append({**{k: x for k, x in obj.items() if k != "split_hint"},
                    "name": f"{obj['name']} {v}", "filter": {"column": column, "values": [v]}})
    return out


def normalize(objects: list[dict], tables: list[TableSchema]) -> list[dict]:
    """Oggetti coerenti con i dati: tabella e colonne esistenti, nome unico, un solo oggetto guida (il primo)."""
    cols = {t.name: {c.name for c in t.columns} for t in tables}
    out, seen, lead = [], set(), False
    for o in objects:
        table = o.get("table")
        name = re.sub(r"\s+", " ", str(o.get("name") or "")).strip()[:80]
        if table not in cols or not name or name.lower() in seen:
            continue
        key = [k for k in (o.get("key") or []) if k in cols[table]]
        f = o.get("filter")
        if f and (f.get("column") not in cols[table] or not f.get("values")):
            f = None
        role = o.get("role") if o.get("role") in ROLES else "context"
        include = bool(o.get("include", True))
        if role == "lead" and (lead or not include):
            role = "needed" if include else "context"
        lead = lead or role == "lead"
        seen.add(name.lower())
        out.append({**o, "name": name, "key": key, "filter": ({"column": f["column"], "values": [str(v) for v in f["values"]]}
                                                              if f else None), "role": role, "include": include})
    return out


def lead(objects: list[dict]) -> str | None:
    return next((o["name"] for o in objects if o.get("include") and o.get("role") == "lead"), None)


# ---------- proposta di Claude ----------

SYSTEM_PROMPT = """\
Sei un esperto di process mining object-centric (OCEL 2.0) e dei sistemi gestionali (ERP, CRM, ticketing).
Un utente ha descritto nell'assessment gli obiettivi della sua analisi e ha caricato delle tabelle. Prima del
mapping colonna per colonna devi proporre gli OGGETTI DI BUSINESS del dataset.

Un oggetto di business e' una cosa di cui il business misura le prestazioni e che ha un ciclo di vita
(fattura, incasso, ordine di vendita, riga d'ordine, consegna, ticket, pratica). NON sono oggetti: le tabelle di
log o storico (sono eventi di altri oggetti), le tabelle indice o copie, le anagrafiche (cliente, fornitore,
prodotto, sono di solito dimensioni per filtrare e confrontare: oggetto solo se l'obiettivo riguarda proprio
il loro ciclo di vita). Una tabella non e' un oggetto: una tabella puo' contenere oggetti diversi distinti da
una colonna (tipo documento, categoria: es. fatture e incassi nei documenti contabili) e allora ogni oggetto ha
un filtro su quella colonna; i valori che non servono agli obiettivi restano fuori.

Per ogni oggetto:
- name: nome di business breve nella lingua indicata da "language", come lo direbbe l'utente (es. "Fattura",
  "Incasso"; in inglese "Invoice", "Payment");
- table: la tabella da cui nasce; key: le colonne che lo identificano (preferisci le candidate_keys misurate);
- filter: null, oppure {"column": colonna, "values": [valori]} se l'oggetto e' solo una parte della tabella
  (usa i valori reali in type_columns);
- role: "lead" (l'oggetto che l'obiettivo principale misura; uno solo), "needed" (senza di lui la misura non
  si fa, es. l'incasso che chiude la fattura), "context" (oggetto di business presente ma non necessario);
- include: true per lead e needed, false per context;
- objective: a quale obiettivo dell'assessment serve (breve); why: perche' (una frase, concreta, citando colonne).
In "not_objects" elenca le tabelle che non generano oggetti: becomes "events" (log di operazioni su un
oggetto), "attributes" (anagrafica o dettaglio da usare come dimensione) o "excluded" (copia o fuori obiettivo),
con why. Scegli la granularita' che l'obiettivo richiede (es. con pagamenti parziali serve la partita, non la
testata). Usa nota utente, documenti e conoscenza del sistema sorgente per il significato di tabelle e codici.
Non inventare tabelle, colonne o valori. Scrivi nella lingua indicata da "language", in modo semplice.
Rispondi SOLO con JSON valido, senza testo prima o dopo, con questa forma:
{"objects":[{"name":"...","table":"...","key":["..."],"filter":null,"role":"lead|needed|context","include":true,
"objective":"...","why":"..."}],"not_objects":[{"table":"...","becomes":"events|attributes|excluded","why":"..."}],
"summary":"una o due frasi"}
"""


def _price():
    return _PRICES_USD_PER_MTOK.get(EXPLAIN_MODEL, _FALLBACK_PRICE)


def build_payload(*, language: str, process_name: str, answers: dict, tables: list[TableSchema], data_profile: dict,
                  descriptions: dict[str, str]) -> str:
    keep = ("objectives", "key_questions", "kpis", "main_object", "other_objects", "start_event", "end_event", "systems")
    compact = []
    for t in tables:
        hit = sap_dictionary.lookup(t.name, [c.name for c in t.columns])
        compact.append({
            "name": t.name, "rows": t.row_count, "dictionary": hit[1].get("label") if hit else None,
            "user_description": descriptions.get(t.name),
            "columns": [{"name": c.name, "type": c.inferred_type, "examples": [str(v)[:24] for v in c.sample_values[:2]],
                         **({"planned_date": True} if c.planned_reason else {})} for c in t.columns[:60]],
        })
    profile = {k: data_profile.get(k) for k in ("candidate_keys", "relationships", "type_columns", "activity_columns",
                                                 "reason_columns", "copy_of") if data_profile.get(k)}
    payload = {"language": language, "process": process_name,
               "assessment": {k: answers[k] for k in keep if answers.get(k)},
               "tables": compact, "data_profile": profile}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def estimate(payload: str) -> dict:
    price_in, price_out = _price()
    tokens_in = int(len(SYSTEM_PROMPT + payload) / _CHARS_PER_TOKEN) + 50
    return {"cost_usd": round((tokens_in * price_in + TYPICAL_OUTPUT_TOKENS * price_out) / 1_000_000, 4),
            "max_usd": round((tokens_in * price_in + MAX_OUTPUT_TOKENS * price_out) / 1_000_000, 4),
            "model": EXPLAIN_MODEL}


def parse(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start:end + 1])
    objects = []
    for o in data.get("objects") or []:
        if not isinstance(o, dict):
            continue
        f = o.get("filter") if isinstance(o.get("filter"), dict) else None
        objects.append({"name": str(o.get("name") or "")[:80], "table": str(o.get("table") or ""),
                        "key": [str(k) for k in o.get("key") or []][:4],
                        "filter": {"column": str(f.get("column") or ""), "values": [str(v) for v in f.get("values") or []][:20]} if f else None,
                        "role": o.get("role"), "include": o.get("include", o.get("role") in ("lead", "needed")),
                        "objective": str(o.get("objective") or "")[:200], "why": str(o.get("why") or "")[:400]})
    not_objects = [{"table": str(n.get("table") or ""),
                    "becomes": n.get("becomes") if n.get("becomes") in BECOMES else "attributes",
                    "why": str(n.get("why") or "")[:400]}
                   for n in data.get("not_objects") or [] if isinstance(n, dict)]
    return {"objects": objects, "not_objects": not_objects, "summary": str(data.get("summary") or "")[:800]}


def ask(payload: str) -> dict:
    """Chiama Claude; ritorna {"result", "cost_usd", "truncated"}."""
    import anthropic

    client = anthropic.Anthropic()
    request = dict(model=EXPLAIN_MODEL, max_tokens=MAX_OUTPUT_TOKENS, system=SYSTEM_PROMPT,
                   messages=[{"role": "user", "content": payload}])
    try:
        response = client.messages.create(**request, output_config={"effort": "low"})
    except (TypeError, anthropic.BadRequestError):
        response = client.messages.create(**request)
    price_in, price_out = _price()
    cost = ((response.usage.input_tokens or 0) * price_in + (response.usage.output_tokens or 0) * price_out) / 1_000_000
    text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
    return {"result": parse(text), "cost_usd": round(cost, 4), "truncated": response.stop_reason == "max_tokens"}


# ---------- dagli oggetti confermati al mapping ----------

def for_context(objects: list[dict]) -> list[dict]:
    """Gli oggetti confermati come li riceve il mapping AI (process_context.business_objects)."""
    return [{"name": o["name"], "table": o["table"], "key": o["key"], "role": o["role"],
             **({"only_rows_where": {o["filter"]["column"]: o["filter"]["values"]}} if o.get("filter") else {})}
            for o in objects if o.get("include")]


def _rule(table, col, el, rationale, **kw) -> MappingProposal:
    base = dict(source_table=table, source_column=col, ocel_element=el, object_type=None, event_type=None,
                attribute_name=None, qualifier=None, related_object_type=None, confidence=1.0,
                rationale=rationale, based_on_template=None, activity_values=None)
    base.update(kw)
    return MappingProposal(**base)


def apply(proposals: list[MappingProposal], objects: list[dict], tables_data: dict[str, list[dict]],
          other_label: str = "altro") -> tuple[list[MappingProposal], set[int]]:
    """Il mapping proposto si allinea agli oggetti di business confermati, qualunque mapper l'abbia fatto
    (regole, catalogo, euristica, Claude):
    - il tipo di oggetto di una tabella prende il nome di business (anche in attributi e collegamenti);
    - piu' oggetti filtrati dalla stessa tabella: un tipo di partenza «<tabella> (altro)» con la divisione
      per valore (object_type.split), i valori non confermati esclusi;
    - un tipo di oggetto che non e' tra quelli confermati (log, anagrafica): le sue righe chiave e attributo
      sono proposte come rifiutate e i collegamenti verso di lui diventano attributi dell'evento (es. il
      cliente resta come dimensione sugli eventi).
    Ritorna (proposte, indici delle proposte da presentare gia' rifiutate)."""
    confirmed = [o for o in objects if o.get("include")]
    if not confirmed:
        return proposals, set()
    by_table: dict[str, list[dict]] = {}
    for o in confirmed:
        by_table.setdefault(o["table"], []).append(o)
    # nome scelto dal mapper per il tipo di oggetto di ogni tabella (dalle righe chiave)
    mapper_name: dict[str, str] = {}
    for p in proposals:
        if p.ocel_element == "object_type.key" and p.object_type:
            mapper_name.setdefault(p.source_table, p.object_type)
    rename: dict[str, str] = {}
    base_of_table: dict[str, str] = {}
    for table, objs in by_table.items():
        filtered = [o for o in objs if o.get("filter")]
        target = objs[0]["name"] if not filtered else f"{_pretty(table)} ({other_label})"
        base_of_table[table] = target
        if table in mapper_name:
            rename[mapper_name[table]] = target
    # tipi che restano: quelli di partenza delle tabelle che ospitano oggetti e i nomi di business confermati
    kept = set(base_of_table.values()) | {o["name"] for o in confirmed}

    out: list[MappingProposal] = []
    rejected: set[int] = set()
    for p in proposals:
        if p.object_type in rename:
            p.object_type = rename[p.object_type]
        if p.related_object_type in rename:
            p.related_object_type = rename[p.related_object_type]
        if p.ocel_element == "object_type.split" and p.source_table in by_table:
            continue  # la divisione la decidono gli oggetti confermati (sotto)
        # una tabella che non ospita oggetti confermati non definisce oggetti, anche se il mapper le ha dato il nome
        # di uno di loro (es. le righe contabili chiamate «Incasso»: creerebbero incassi con i numeri delle fatture)
        if p.ocel_element in ("object_type.key", "object_type.attribute") and (
                p.source_table not in by_table or p.object_type not in kept):
            p.rationale = msg("Non è tra gli oggetti di business confermati: proposta rifiutata (puoi ripristinarla).")
            rejected.add(len(out))
        elif p.ocel_element == "e2o_relationship" and p.related_object_type and p.related_object_type not in kept:
            p.ocel_element, p.attribute_name = "event_type.attribute", p.source_column
            p.rationale = msg("{o} non è tra gli oggetti di business confermati: resta come attributo dell'evento "
                              "(dimensione per filtrare e confrontare).", o=p.related_object_type)
            p.related_object_type = p.qualifier = None
        out.append(p)

    why = msg("Oggetti di business confermati prima del mapping.")
    for table, objs in by_table.items():
        target = base_of_table[table]
        keys = [p for p in out if p.source_table == table and p.ocel_element == "object_type.key" and p.object_type == target]
        if not keys:
            for col in objs[0].get("key") or []:
                out.append(_rule(table, col, "object_type.key", why, object_type=target))
        filtered = [o for o in objs if o.get("filter")]
        if filtered:
            column = filtered[0]["filter"]["column"]
            present = sorted({str(r.get(column) or "").strip() for r in tables_data.get(table, [])} - {"", "nan"})
            values = {v: "" for v in present}
            for o in filtered:
                if o["filter"]["column"] == column:
                    for v in o["filter"]["values"]:
                        values[v] = o["name"]
            out.append(_rule(table, column, "object_type.split", why, object_type=target, activity_values=values))
    out.extend(_shared_key_links(out, confirmed, tables_data, rejected, base_of_table))
    return out, rejected


def _shared_key_links(rows: list[MappingProposal], confirmed: list[dict], tables_data: dict[str, list[dict]],
                      rejected: set[int], base_of_table: dict[str, str]) -> list[MappingProposal]:
    """Gli eventi di una tabella riguardano un oggetto confermato di un'altra tabella quando contengono la sua
    chiave (stesse colonne) e i valori corrispondono davvero (es. la registrazione della testata contabile
    BKPF riguarda la fattura vista dalla sua riga cliente BSEG, stesso BELNR). Se il mapping non ha gia' quel
    collegamento, lo si aggiunge: senza, gli eventi dei documenti non confermati come oggetti andrebbero persi."""
    live = [r for i, r in enumerate(rows) if i not in rejected]
    events = {(r.source_table, r.event_type) for r in live if r.ocel_element == "event_type.timestamp" and r.event_type}
    linked = {(r.event_type, r.related_object_type) for r in live if r.ocel_element == "e2o_relationship"}
    out = []
    for table, event in sorted(events):
        data = tables_data.get(table) or []
        if not data:
            continue
        columns = set(data[0])
        for o in confirmed:
            key = o.get("key") or []
            if (o["table"] == table or not key or not set(key) <= columns or (event, o["name"]) in linked
                    or (event, base_of_table.get(o["table"])) in linked):  # gia' collegato al tipo di partenza
                continue
            f = o.get("filter")
            mine = {tuple(str(r.get(k) or "").strip() for k in key) for r in data}
            theirs = {tuple(str(r.get(k) or "").strip() for k in key) for r in tables_data.get(o["table"]) or []
                      if not f or str(r.get(f["column"]) or "").strip() in f["values"]}
            mine.discard(tuple("" for _ in key))
            if not mine or len(mine & theirs) / len(mine) < 0.2:
                continue
            out.append(_rule(table, key[0], "e2o_relationship",
                             msg("Gli eventi di {t} contengono la chiave di {o} ({k}): riguardano anche quell'oggetto.",
                                 t=table, o=o["name"], k=", ".join(key)),
                             event_type=event, related_object_type=o["name"], qualifier=f"for {o['name'].lower()}"))
            linked.add((event, o["name"]))
    return out
