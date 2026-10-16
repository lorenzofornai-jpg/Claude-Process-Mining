"""Archivio dei simboli dei BPMN (unico per l'azienda), aggiornato a ogni salvataggio di un modello della BPA.

- Simbolo (BpaSymbol): pool, lane, attivita', sottoprocesso, evento, gateway, oggetto dati, archivio dati, con un
  identificativo univoco nascosto, il nome in italiano e in inglese e gli attributi definiti dall'Amministratore
  (BpaAttributeDef). Il nome e' unico per tipo e lingua dentro un dataset: stesso nome = stesso simbolo (si riusa)
  oppure si rinomina; in dataset diversi lo stesso nome puo' essere di simboli diversi (l'Amministratore li puo'
  consolidare in uno, merge()). Per i BPMN generati dalle varianti guida il nome inglese.
- Occorrenza (BpaOccurrence): l'elemento di un modello BPMN che rappresenta il simbolo. Lo stesso simbolo puo'
  comparire in piu' modelli (es. la lane «Buyer»); rinominarlo cambia il nome in tutti.
- Relazione (BpaRelation): contiene, precede, attiva, crea, input, output, attaccato a, messaggio; ricalcolate dal
  disegno a ogni salvataggio.
- Per ogni tipo una vista SQL (bpa_view_<tipo>) con una colonna per attributo, per consultare l'archivio come tabelle.

Gli elementi senza nome, o con un nome generico messo dall'app (Inizio, Fine, Ruolo da definire), non entrano
nell'archivio finche' non hanno un nome loro; le relazioni «precede» li attraversano.
"""
from __future__ import annotations

import copy
import io
import re
import secrets
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, datetime, timezone

from sqlalchemy import text

from app.services import bpmn

B = "{%s}" % bpmn.NS["bpmn"]

# tipi di simbolo: nomi nelle due lingue e tipi BPMN che li rappresentano
SYMBOL_TYPES = {
    "pool": {"it": "Pool", "en": "Pool", "tags": ["participant"]},
    "lane": {"it": "Lane", "en": "Lane", "tags": ["lane"]},
    "activity": {"it": "Attività", "en": "Activity",
                 "tags": ["task", "userTask", "serviceTask", "manualTask", "scriptTask", "businessRuleTask",
                          "sendTask", "receiveTask", "callActivity"]},
    "subprocess": {"it": "Sottoprocesso", "en": "Sub-process", "tags": ["subProcess", "transaction", "adHocSubProcess"]},
    "event": {"it": "Evento", "en": "Event",
              "tags": ["startEvent", "endEvent", "intermediateCatchEvent", "intermediateThrowEvent", "boundaryEvent"]},
    "gateway": {"it": "Gateway", "en": "Gateway",
                "tags": ["exclusiveGateway", "inclusiveGateway", "parallelGateway", "complexGateway", "eventBasedGateway"]},
    "data_object": {"it": "Oggetto dati", "en": "Data object", "tags": ["dataObjectReference"]},
    "data_store": {"it": "Archivio dati", "en": "Data store", "tags": ["dataStoreReference"]},
}
TAG_TYPE = {tag: t for t, v in SYMBOL_TYPES.items() for tag in v["tags"]}
FLOW_TYPES = {"activity", "subprocess", "event", "gateway"}

# relazioni: lettura dal simbolo di partenza (forward) e da quello di arrivo (inverse)
RELATION_KINDS = {
    "contains": {"it": ("contiene", "è contenuto in"), "en": ("contains", "is contained in")},
    "precedes": {"it": ("precede", "segue"), "en": ("precedes", "follows")},
    "activates": {"it": ("attiva", "è attivato da"), "en": ("activates", "is activated by")},
    "creates": {"it": ("crea", "è creato da"), "en": ("creates", "is created by")},
    "input": {"it": ("è input per", "ha come input"), "en": ("is input for", "has input")},
    "output": {"it": ("ha come output", "è output di"), "en": ("has output", "is output of")},
    "attached": {"it": ("è attaccato a", "ha attaccato"), "en": ("is attached to", "has attached")},
    "message": {"it": ("invia un messaggio a", "riceve un messaggio da"), "en": ("sends a message to", "receives a message from")},
}

# sottotipi BPMN (eventi: inizio/fine/...; gateway: XOR/AND/OR...) e trigger degli eventi, nelle due lingue
SUBTYPES = {
    "startEvent": {"it": "Evento di inizio", "en": "Start event"},
    "endEvent": {"it": "Evento di fine", "en": "End event"},
    "intermediateCatchEvent": {"it": "Evento intermedio (ricezione)", "en": "Intermediate catch event"},
    "intermediateThrowEvent": {"it": "Evento intermedio (lancio)", "en": "Intermediate throw event"},
    "boundaryEvent": {"it": "Evento di confine", "en": "Boundary event"},
    "exclusiveGateway": {"it": "Gateway esclusivo (XOR)", "en": "Exclusive gateway (XOR)"},
    "parallelGateway": {"it": "Gateway parallelo (AND)", "en": "Parallel gateway (AND)"},
    "inclusiveGateway": {"it": "Gateway inclusivo (OR)", "en": "Inclusive gateway (OR)"},
    "complexGateway": {"it": "Gateway complesso", "en": "Complex gateway"},
    "eventBasedGateway": {"it": "Gateway basato su eventi", "en": "Event-based gateway"},
    "task": {"it": "Task", "en": "Task"}, "userTask": {"it": "Task utente", "en": "User task"},
    "serviceTask": {"it": "Task di servizio", "en": "Service task"}, "manualTask": {"it": "Task manuale", "en": "Manual task"},
    "scriptTask": {"it": "Task di script", "en": "Script task"},
    "businessRuleTask": {"it": "Task di regola di business", "en": "Business rule task"},
    "sendTask": {"it": "Task di invio", "en": "Send task"}, "receiveTask": {"it": "Task di ricezione", "en": "Receive task"},
    "callActivity": {"it": "Attività di chiamata", "en": "Call activity"},
    "subProcess": {"it": "Sottoprocesso", "en": "Sub-process"}, "transaction": {"it": "Transazione", "en": "Transaction"},
    "adHocSubProcess": {"it": "Sottoprocesso ad hoc", "en": "Ad-hoc sub-process"},
    "participant": {"it": "Pool", "en": "Pool"}, "lane": {"it": "Lane", "en": "Lane"},
    "dataObjectReference": {"it": "Oggetto dati", "en": "Data object"},
    "dataStoreReference": {"it": "Archivio dati", "en": "Data store"},
}
TRIGGERS = {
    "messageEventDefinition": {"it": "messaggio", "en": "message"},
    "timerEventDefinition": {"it": "timer", "en": "timer"},
    "signalEventDefinition": {"it": "segnale", "en": "signal"},
    "conditionalEventDefinition": {"it": "condizione", "en": "conditional"},
    "errorEventDefinition": {"it": "errore", "en": "error"},
    "escalationEventDefinition": {"it": "escalation", "en": "escalation"},
    "compensateEventDefinition": {"it": "compensazione", "en": "compensation"},
    "cancelEventDefinition": {"it": "annullamento", "en": "cancel"},
    "linkEventDefinition": {"it": "collegamento", "en": "link"},
    "terminateEventDefinition": {"it": "termina", "en": "terminate"},
}


def subtype_label(tag: str | None, detail: str | None, lang: str) -> str:
    """Es. «Evento di inizio · messaggio», «Gateway esclusivo (XOR)»."""
    base = (SUBTYPES.get(tag or "") or {}).get(lang) or (tag or "")
    trig = (TRIGGERS.get(detail or "") or {}).get(lang)
    return f"{base} · {trig}" if trig else base


# nomi generici messi dall'app nei modelli generati: non sono nomi propri (simboli senza nome, non condivisi)
GENERIC_NAMES = {"ruolo da definire", "role to be defined", "inizio", "fine", "start", "end"}

DATA_TYPES = {
    "short_text": {"it": "Testo breve", "en": "Short text"},
    "long_text": {"it": "Testo lungo", "en": "Long text"},
    "integer": {"it": "Numero intero", "en": "Whole number"},
    "number": {"it": "Numero decimale", "en": "Decimal number"},
    "date": {"it": "Data", "en": "Date"},
    "datetime": {"it": "Data e ora", "en": "Date and time"},
    "boolean": {"it": "Sì / No", "en": "Yes / No"},
    "choice": {"it": "Elenco (una scelta)", "en": "List (one choice)"},
    "multi_choice": {"it": "Elenco (più scelte)", "en": "List (several choices)"},
    "url": {"it": "Link (URL)", "en": "Link (URL)"},
    "email": {"it": "Email", "en": "Email"},
}
TEXT_TYPES = {"short_text", "long_text"}


def norm(name: str | None) -> str:
    return " ".join(str(name or "").split()).casefold()


def other_lang(lang: str) -> str:
    return "en" if lang == "it" else "it"


def new_attr_key() -> str:
    return "a_" + secrets.token_hex(4)


# ---------- lettura del disegno ----------

def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def extract(xml: str) -> dict:
    """Elementi del modello e relazioni del disegno (per id di elemento).
    {"elements": {id: {"id", "tag", "type", "name"}}, "relations": [(kind, src_id, tgt_id)], "flows": [(src, tgt)]}"""
    root = bpmn._parse(xml).getroot()
    elements: dict[str, dict] = {}
    relations: list[tuple[str, str, str]] = []
    flows: list[tuple[str, str]] = []

    def add(el):
        tag = _local(el.tag)
        eid = el.get("id")
        if eid and tag in TAG_TYPE:
            detail = next((_local(c.tag) for c in el if _local(c.tag).endswith("EventDefinition")), None)
            elements[eid] = {"id": eid, "tag": tag, "type": TAG_TYPE[tag], "name": (el.get("name") or "").strip(),
                             "detail": detail}

    proc_of_participant = {}
    for part in root.iter(B + "participant"):
        add(part)
        if part.get("processRef"):
            proc_of_participant[part.get("processRef")] = part.get("id")
    for mf in root.iter(B + "messageFlow"):
        if mf.get("sourceRef") and mf.get("targetRef"):
            relations.append(("message", mf.get("sourceRef"), mf.get("targetRef")))

    for proc in root.iter(B + "process"):
        owner = proc_of_participant.get(proc.get("id"))
        # lane (anche annidate): la lane piu' interna che elenca un elemento lo contiene
        lane_of: dict[str, tuple[int, str]] = {}

        def lanes(lane_set, parent, depth):
            for lane in lane_set.findall(B + "lane"):
                add(lane)
                if parent:
                    relations.append(("contains", parent, lane.get("id")))
                for ref in lane.findall(B + "flowNodeRef"):
                    rid = (ref.text or "").strip()
                    if rid and (rid not in lane_of or lane_of[rid][0] < depth):
                        lane_of[rid] = (depth, lane.get("id"))
                child = lane.find(B + "childLaneSet")
                if child is not None:
                    lanes(child, lane.get("id"), depth + 1)
        for ls in proc.findall(B + "laneSet"):
            lanes(ls, owner, 0)

        def walk(container, container_id, top):
            for el in container:
                tag = _local(el.tag)
                eid = el.get("id")
                if tag == "sequenceFlow":
                    if el.get("sourceRef") and el.get("targetRef"):
                        flows.append((el.get("sourceRef"), el.get("targetRef")))
                    continue
                if tag in TAG_TYPE:
                    add(el)
                    parent = lane_of.get(eid, (0, None))[1] if top else None
                    parent = parent or container_id
                    if parent:
                        relations.append(("contains", parent, eid))
                    if tag == "boundaryEvent" and el.get("attachedToRef"):
                        relations.append(("attached", eid, el.get("attachedToRef")))
                    for a in el.findall(B + "dataInputAssociation"):
                        for src in a.findall(B + "sourceRef"):
                            if (src.text or "").strip():
                                relations.append(("input", src.text.strip(), eid))
                    for a in el.findall(B + "dataOutputAssociation"):
                        tgt = a.find(B + "targetRef")
                        if tgt is not None and (tgt.text or "").strip():
                            relations.append(("output", eid, tgt.text.strip()))
                    if TAG_TYPE[tag] == "subprocess":
                        walk(el, eid, False)
        walk(proc, owner, True)
    return {"elements": elements, "relations": relations, "flows": flows}


def is_symbol(e: dict) -> bool:
    return bool(e.get("name")) and norm(e["name"]) not in GENERIC_NAMES


def relations_between_symbols(ext: dict, assign: dict[str, str]) -> list[tuple[str, str, str]]:
    """(kind, simbolo di partenza, simbolo di arrivo) dal disegno; «precede» attraversa gli elementi senza simbolo
    (gateway ed eventi senza nome)."""
    out = set()
    els = ext["elements"]
    for kind, a, b in ext["relations"]:
        if a in assign and b in assign and assign[a] != assign[b]:
            out.add((kind, assign[a], assign[b]))
    nxt = defaultdict(list)
    for a, b in ext["flows"]:
        nxt[a].append(b)
    for a in [x for x in nxt if x in assign]:
        seen, stack = {a}, list(nxt[a])
        while stack:
            b = stack.pop()
            if b in seen:
                continue
            seen.add(b)
            if b in assign:
                ta, tb = els.get(a, {}).get("type"), els.get(b, {}).get("type")
                if ta == "event" and tb in ("activity", "subprocess"):
                    kind = "activates"
                elif ta in ("activity", "subprocess") and tb == "event":
                    kind = "creates"
                else:
                    kind = "precedes"
                if assign[a] != assign[b]:
                    out.add((kind, assign[a], assign[b]))
            else:
                stack.extend(nxt.get(b, ()))
    return sorted(out)


def set_names(xml: str, names: dict[str, str]) -> str:
    """Lo stesso modello con i nomi di alcuni elementi cambiati (per id)."""
    if not names:
        return xml
    tree = bpmn._parse(xml)
    changed = False
    for el in tree.getroot().iter():
        eid = el.get("id")
        if eid in names and _local(el.tag) in TAG_TYPE:
            if el.get("name") != names[eid]:
                el.set("name", names[eid])
                changed = True
    if not changed:
        return xml
    out = io.BytesIO()
    tree.write(out, encoding="utf-8", xml_declaration=True)
    return out.getvalue().decode("utf-8")


# ---------- simboli ----------

def name_of(s, lang: str) -> str:
    return (s.name_it if lang == "it" else s.name_en) or (s.name_en if lang == "it" else s.name_it) or ""


def set_name(s, lang: str, name: str) -> None:
    if lang == "it":
        s.name_it, s.key_it = name, norm(name)
    else:
        s.name_en, s.key_en = name, norm(name)


REPO_SCOPE = "@repo"   # ambito dei nomi dei modelli del repository BPA (gli altri: il dataset del modello di staging)


def scope_of(model) -> str:
    """Ambito in cui i nomi devono essere unici: il dataset del modello di staging, o tutto il repository BPA."""
    return REPO_SCOPE if getattr(model, "stage", None) == "repository" else model.ingestion_config_id


def _scope_filter(BpaModel, scopes: set[str]):
    from sqlalchemy import or_
    conds = []
    cfgs = sorted(x for x in scopes if x and x != REPO_SCOPE)
    if cfgs:
        conds.append(BpaModel.ingestion_config_id.in_(cfgs))
    if REPO_SCOPE in scopes:
        conds.append(BpaModel.stage == "repository")
    return or_(*conds) if conds else None


def find_by_name(db, symbol_type: str, name: str, lang: str, exclude: str | None = None,
                 configs: set[str] | None = None):
    """Simbolo con questo tipo e nome. configs: solo tra i simboli che compaiono in un BPMN di questi ambiti
    (dataset, o REPO_SCOPE per il repository BPA)."""
    from app.models import BpaModel, BpaOccurrence, BpaSymbol
    col = BpaSymbol.key_it if lang == "it" else BpaSymbol.key_en
    q = db.query(BpaSymbol).filter(BpaSymbol.symbol_type == symbol_type, col == norm(name))
    if exclude:
        q = q.filter(BpaSymbol.id != exclude)
    if configs is not None:
        cond = _scope_filter(BpaModel, configs)
        if cond is None:
            return None
        q = q.filter(BpaSymbol.id.in_(
            db.query(BpaOccurrence.symbol_id).join(BpaModel, BpaModel.id == BpaOccurrence.model_id).filter(cond)))
    return q.first()


def symbol_configs(db, symbol_id: str) -> set[str]:
    """Gli ambiti (dataset, REPO_SCOPE) dei BPMN in cui compare il simbolo."""
    from app.models import BpaModel, BpaOccurrence
    rows = (db.query(BpaModel.ingestion_config_id, BpaModel.stage).join(BpaOccurrence, BpaOccurrence.model_id == BpaModel.id)
            .filter(BpaOccurrence.symbol_id == symbol_id).distinct())
    return {REPO_SCOPE if st == "repository" else c for c, st in rows}


def resolve(db, model, lang: str, xml: str, links: dict, loaded_at: datetime | None = None,
            policy: str = "ask") -> dict:
    """Quale simbolo e' ogni elemento con nome. Non scrive nulla.
    I nomi gia' usati si cercano solo tra i simboli del dataset del modello (e, per un simbolo rinominato, anche dei
    dataset in cui compare): lo stesso nome in un altro dataset e' un altro simbolo.
    links: {id elemento: {"symbol": id} | {"copy_of": id}} scelti dall'utente nell'editor.
    policy: "ask" -> i nomi gia' usati da un altro simbolo sono conflitti da far decidere all'utente;
            "reuse" -> stesso nome = stesso simbolo (modelli generati dalle varianti, con lang="en").
    Ritorna {"assign", "new", "renames", "conflicts", "xml", "refreshed", "ext"}."""
    from app.models import BpaOccurrence, BpaSymbol
    ext = extract(xml)
    occ = {o.element_id: o.symbol_id for o in db.query(BpaOccurrence).filter_by(model_id=model.id).all()}
    symbols = {}

    def sym(sid):
        if sid and sid not in symbols:
            symbols[sid] = db.get(BpaSymbol, sid)
        return symbols.get(sid)

    assign, new, renames, conflicts, refresh = {}, {}, {}, [], {}
    taken = {}   # (tipo, nome normalizzato) -> elemento nuovo gia' visto in questo salvataggio
    anon_names = {}
    for eid, e in ext["elements"].items():
        if not is_symbol(e):
            # senza nome proprio: simbolo della sola occorrenza (resta lo stesso finche' l'elemento esiste)
            s = sym(occ.get(eid))
            if s is not None and s.symbol_type == e["type"] and s.anonymous:
                assign[eid] = s.id
                if (name_of(s, lang) or "") != e["name"]:
                    anon_names[s.id] = e["name"]
            else:
                assign[eid] = "new:" + eid
                new[eid] = {"type": e["type"], "name": e["name"], "anonymous": True}
            continue
        link = links.get(eid) or {}
        s = sym(link.get("symbol")) if link.get("symbol") else None
        if s is not None and s.symbol_type != e["type"]:
            s = None
        if s is None and not link.get("copy_of"):
            s = sym(occ.get(eid))
            if s is not None and s.symbol_type != e["type"]:
                s = None   # l'elemento ha cambiato tipo (es. attivita' -> evento): e' un altro simbolo
        if s is not None and s.anonymous and link.get("symbol") == s.id:
            s = None   # un simbolo senza nome non si condivide
        scope = {scope_of(model)}
        if s is not None and not s.anonymous:
            scope |= symbol_configs(db, s.id)   # un nome cambiato vale in tutti i BPMN del simbolo
        same = find_by_name(db, e["type"], e["name"], lang, exclude=s.id if s else None, configs=scope)
        if s is not None and s.anonymous:   # elemento che prima non aveva un nome proprio e ora ce l'ha
            if same is not None:
                if policy == "reuse":
                    assign[eid] = same.id
                else:
                    conflicts.append({"element_id": eid, "name": e["name"], "type": e["type"], "existing": same.id})
                continue
            assign[eid] = s.id
            renames[s.id] = e["name"]
            continue
        if s is not None:
            assign[eid] = s.id
            if norm(name_of(s, lang)) != norm(e["name"]):
                if loaded_at and s.renamed_at and s.renamed_at > loaded_at and not link.get("symbol"):
                    refresh[eid] = name_of(s, lang)       # rinominato altrove dopo l'apertura: vale il nome nuovo
                elif same is not None:
                    conflicts.append({"element_id": eid, "name": e["name"], "type": e["type"], "existing": same.id})
                elif s.id in renames and norm(renames[s.id]) != norm(e["name"]):
                    conflicts.append({"element_id": eid, "name": e["name"], "type": e["type"], "existing": s.id})
                else:
                    renames[s.id] = e["name"]
            continue
        if same is not None:
            if policy == "reuse":
                assign[eid] = same.id
            else:
                conflicts.append({"element_id": eid, "name": e["name"], "type": e["type"], "existing": same.id})
            continue
        k = (e["type"], norm(e["name"]))
        if k in taken:
            assign[eid] = "new:" + taken[k]      # due elementi nuovi con lo stesso nome nello stesso modello: un simbolo
            continue
        taken[k] = eid
        assign[eid] = "new:" + eid
        new[eid] = {"type": e["type"], "name": e["name"], "copy_of": link.get("copy_of")}
    if refresh:
        xml = set_names(xml, refresh)
        for eid, nm in refresh.items():
            ext["elements"][eid]["name"] = nm
    return {"assign": assign, "new": new, "renames": renames, "conflicts": conflicts, "xml": xml,
            "refreshed": bool(refresh), "ext": ext, "anon_names": anon_names}


def other_names(db, plan: dict, lang: str) -> dict[str, str]:
    """Per ogni elemento legato a un simbolo esistente e non rinominato: il nome del simbolo nell'altra lingua
    (cosi' un simbolo riutilizzato ha lo stesso nome tradotto ovunque, senza passare dall'LLM)."""
    from app.models import BpaSymbol
    ol = other_lang(lang)
    out = {}
    for eid, sid in plan["assign"].items():
        if sid.startswith("new:") or sid in plan["renames"]:
            continue
        s = db.get(BpaSymbol, sid)
        if s is not None and name_of(s, ol) and (s.name_en if ol == "en" else s.name_it):
            out[eid] = s.name_en if ol == "en" else s.name_it
    return out


def apply(db, model, lang: str, xml: str, other_xml: str, plan: dict, user_name: str | None) -> dict:
    """Scrive simboli, occorrenze e relazioni del modello; propaga i nomi cambiati agli altri modelli e alle altre
    occorrenze dello stesso simbolo in questo modello.
    Ritorna {"others": id degli altri modelli aggiornati, "xml", "other_xml", "self_changed"}."""
    from app.models import BpaModel, BpaOccurrence, BpaRelation, BpaSymbol
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ol = other_lang(lang)
    ext = plan["ext"]
    ext_other = {e["id"]: e["name"] for e in extract(other_xml)["elements"].values()}
    real: dict[str, str] = {}
    for eid, info in plan["new"].items():
        s = BpaSymbol(symbol_type=info["type"], created_by=user_name, created_at=now, anonymous=bool(info.get("anonymous")))
        set_name(s, lang, info["name"])
        set_name(s, ol, ext_other.get(eid) or info["name"])
        if s.anonymous:
            s.key_it = s.key_en = ""   # non si trova per nome: non e' condiviso
        if info.get("copy_of"):
            src = db.get(BpaSymbol, info["copy_of"])
            if src is not None and src.symbol_type == info["type"]:
                s.attrs = copy.deepcopy(src.attrs or {})
        db.add(s)
        db.flush()
        real["new:" + eid] = s.id
    assign = {eid: real.get(sid, sid) for eid, sid in plan["assign"].items()}

    # nomi cambiati: nel simbolo (le due lingue) e in tutti gli altri modelli in cui compare
    touched: dict[str, dict[str, dict[str, str]]] = defaultdict(lambda: {"it": {}, "en": {}})
    for sid, name in plan["renames"].items():
        s = db.get(BpaSymbol, sid)
        if s is None:
            continue
        eids = [e for e, x in assign.items() if x == sid]
        s.anonymous = False
        set_name(s, lang, name)
        if eids and ext_other.get(eids[0]):
            set_name(s, ol, ext_other[eids[0]])
        s.renamed_at, s.updated_at, s.updated_by = now, now, user_name
        for o in db.query(BpaOccurrence).filter(BpaOccurrence.symbol_id == sid, BpaOccurrence.model_id != model.id).all():
            touched[o.model_id]["it"][o.element_id] = s.name_it
            touched[o.model_id]["en"][o.element_id] = s.name_en
    for sid in set(assign.values()):   # simboli esistenti senza il nome in una lingua: si completa
        s = db.get(BpaSymbol, sid)
        if s is not None and not (s.name_en if ol == "en" else s.name_it):
            eid = next(e for e, x in assign.items() if x == sid)
            if ext_other.get(eid):
                set_name(s, ol, ext_other[eid])
    # altre occorrenze dello stesso simbolo in questo modello: stesso nome
    same, same_other = {}, {}
    for sid in plan["renames"]:
        s = db.get(BpaSymbol, sid)
        for eid, x in assign.items():
            if x == sid and s is not None:
                same[eid] = name_of(s, lang)
                same_other[eid] = s.name_en if ol == "en" else s.name_it
    new_xml, new_other = set_names(xml, same), set_names(other_xml, same_other)
    self_changed = new_xml != xml
    xml, other_xml = new_xml, new_other
    others = []
    for mid, names in touched.items():
        m2 = db.get(BpaModel, mid)
        if m2 is None:
            continue
        m2.xml_it = set_names(m2.xml_it, names["it"])
        m2.xml_en = set_names(m2.xml_en, names["en"])
        db.add(m2)
        others.append(mid)

    for sid, name in (plan.get("anon_names") or {}).items():   # simboli senza nome proprio: il nome dell'elemento
        s = db.get(BpaSymbol, sid)
        if s is not None:
            eid = next((e for e, x in assign.items() if x == sid), None)
            if lang == "it":
                s.name_it, s.name_en = name, ext_other.get(eid, s.name_en) or name
            else:
                s.name_en, s.name_it = name, ext_other.get(eid, s.name_it) or name
    db.query(BpaOccurrence).filter_by(model_id=model.id).delete(synchronize_session=False)
    for eid, sid in assign.items():
        e = ext["elements"][eid]
        db.add(BpaOccurrence(symbol_id=sid, model_id=model.id, element_id=eid, bpmn_type=e["tag"], bpmn_detail=e.get("detail")))
    db.query(BpaRelation).filter_by(model_id=model.id).delete(synchronize_session=False)
    for kind, a, b in relations_between_symbols(ext, assign):
        db.add(BpaRelation(model_id=model.id, kind=kind, source_id=a, target_id=b))
    db.flush()
    drop_orphan_anonymous(db)
    model.registered = True
    return {"others": others, "xml": xml, "other_xml": other_xml, "self_changed": self_changed}


def drop_orphan_anonymous(db) -> None:
    """I simboli senza nome proprio esistono solo con la loro occorrenza: senza, si eliminano."""
    from app.models import BpaOccurrence, BpaRelation, BpaSymbol
    used = {sid for (sid,) in db.query(BpaOccurrence.symbol_id).distinct()}
    for s in db.query(BpaSymbol).filter(BpaSymbol.anonymous.is_(True)).all():
        if s.id not in used:
            db.query(BpaRelation).filter((BpaRelation.source_id == s.id) | (BpaRelation.target_id == s.id)).delete(synchronize_session=False)
            db.delete(s)


def display_name(db, s, lang: str) -> str:
    """Il nome del simbolo, o per uno senza nome il suo sottotipo (es. «Gateway esclusivo (XOR)»)."""
    from app.models import BpaOccurrence
    n = name_of(s, lang)
    if n and not (s.anonymous and norm(n) in GENERIC_NAMES):
        return n
    o = db.query(BpaOccurrence).filter_by(symbol_id=s.id).first()
    lab = subtype_label(o.bpmn_type, o.bpmn_detail, lang) if o else SYMBOL_TYPES[s.symbol_type][lang]
    return f"{lab} «{n}»" if n else lab


def subtypes(db, s, lang: str) -> list[str]:
    from app.models import BpaOccurrence
    seen = []
    for o in db.query(BpaOccurrence).filter_by(symbol_id=s.id).all():
        lab = subtype_label(o.bpmn_type, o.bpmn_detail, lang)
        if lab not in seen:
            seen.append(lab)
    return seen


def text_attrs_in_both_languages(db) -> int:
    """Gli attributi di testo hanno sempre due valori (italiano e inglese): quelli creati prima con un valore solo
    diventano {"it", "en"} con lo stesso testo. Ritorna quanti valori sono stati convertiti."""
    from app.models import BpaAttributeDef, BpaSymbol
    keys = set()
    for d in db.query(BpaAttributeDef).filter(BpaAttributeDef.data_type.in_(sorted(TEXT_TYPES))).all():
        if not d.translatable:
            d.translatable = True
            db.add(d)
        keys.add(d.key)
    n = 0
    for s in db.query(BpaSymbol).all():
        attrs = dict(s.attrs or {})
        changed = False
        for k in keys:
            if isinstance(attrs.get(k), str):
                attrs[k] = {"it": attrs[k], "en": attrs[k]}
                changed = True
                n += 1
        if changed:
            s.attrs = attrs
            db.add(s)
    return n


def backfill(db) -> int:
    """Modelli gia' registrati prima dei simboli senza nome (eventi, gateway...): si completano. Ritorna quanti."""
    from app.models import BpaModel, BpaOccurrence
    n = 0
    for m in db.query(BpaModel).filter(BpaModel.registered.is_(True)).all():
        have = {o.element_id for o in db.query(BpaOccurrence).filter_by(model_id=m.id).all()}
        try:
            ext = extract(m.xml_it)
        except Exception:
            continue
        if all(eid in have for eid in ext["elements"]):
            continue
        plan = resolve(db, m, "en", m.xml_en, {}, policy="reuse")
        if plan["conflicts"]:
            continue
        out = apply(db, m, "en", plan["xml"], m.xml_it, plan, None)
        m.xml_en, m.xml_it = out["xml"], out["other_xml"]
        n += 1
    return n


def register_generated(db, model, user_name: str | None = None) -> None:
    """Modello generato da una variante (o mai registrato): i suoi simboli entrano nell'archivio. Guida il nome
    inglese e si cerca solo nel dataset: stesso tipo e stesso nome inglese di un simbolo gia' in un BPMN dello stesso
    dataset = stesso simbolo (es. la stessa attivita' in due varianti); altrimenti un simbolo nuovo, anche se un altro
    dataset ha un simbolo con lo stesso nome (l'Amministratore li puo' consolidare)."""
    plan = resolve(db, model, "en", model.xml_en, {}, policy="reuse")
    # in italiano i simboli gia' presenti hanno il loro nome: si allinea il modello
    names = other_names(db, plan, "en")
    if names:
        model.xml_it = set_names(model.xml_it, names)
    out = apply(db, model, "en", plan["xml"], model.xml_it, plan, user_name)
    model.xml_en, model.xml_it = out["xml"], out["other_xml"]


# ---------- consolidamento (Amministratore) ----------

def merge(db, master_id: str, ids: list[str], user_name: str | None = None) -> dict:
    """Consolida piu' simboli dello stesso tipo nel master: occorrenze e relazioni passano al master, gli altri si
    eliminano; il master tiene nomi e attributi suoi e prende dagli altri solo gli attributi che non ha. In tutti i
    BPMN gli elementi prendono i nomi del master.
    Ritorna {"merged": n, "models": n} oppure {"error": msg, ...} senza scrivere nulla."""
    from app.models import BpaModel, BpaOccurrence, BpaRelation, BpaSymbol
    from app.i18n import msg
    master = db.get(BpaSymbol, master_id)
    others = [db.get(BpaSymbol, i) for i in dict.fromkeys(ids) if i != master_id]
    others = [o for o in others if o is not None]
    if master is None or not others:
        return {"error": msg("Scegli almeno due oggetti e quale tenere.")}
    if any(o.symbol_type != master.symbol_type for o in others):
        return {"error": msg("Si consolidano solo oggetti dello stesso tipo.")}
    if master.anonymous or any(o.anonymous for o in others):
        return {"error": msg("Gli oggetti senza nome non si consolidano.")}
    group = {master.id} | {o.id for o in others}
    configs = set()
    for sid in group:
        configs |= symbol_configs(db, sid)
    # nei dataset dei simboli consolidati il nome del master non deve essere gia' di un altro oggetto
    for lang in ("it", "en"):
        for cfg in sorted(configs):
            hit = find_by_name(db, master.symbol_type, name_of(master, lang), lang, configs={cfg})
            if hit is not None and hit.id not in group:
                return {"error": msg("Il nome «{n}» è già di un altro oggetto dello stesso tipo in un dataset di questi "
                                     "oggetti: consolidalo insieme o rinominalo prima.", n=name_of(master, lang)),
                        "conflict": hit.id}
    attrs = dict(master.attrs or {})
    for o in others:
        for k, v in (o.attrs or {}).items():
            if attrs.get(k) in (None, "", [], {}):
                attrs[k] = copy.deepcopy(v)
    master.attrs = attrs
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    master.updated_at, master.updated_by = now, user_name
    ids_other = [o.id for o in others]
    db.query(BpaOccurrence).filter(BpaOccurrence.symbol_id.in_(ids_other)).update(
        {BpaOccurrence.symbol_id: master.id}, synchronize_session=False)
    db.query(BpaRelation).filter(BpaRelation.source_id.in_(ids_other)).update(
        {BpaRelation.source_id: master.id}, synchronize_session=False)
    db.query(BpaRelation).filter(BpaRelation.target_id.in_(ids_other)).update(
        {BpaRelation.target_id: master.id}, synchronize_session=False)
    db.flush()
    seen = set()
    for r in db.query(BpaRelation).filter((BpaRelation.source_id == master.id) | (BpaRelation.target_id == master.id)).all():
        k = (r.model_id, r.kind, r.source_id, r.target_id)
        if r.source_id == r.target_id or k in seen:
            db.delete(r)
        else:
            seen.add(k)
    for o in others:
        db.delete(o)
    # i nomi del master in tutti i BPMN in cui compare
    by_model = defaultdict(list)
    for o in db.query(BpaOccurrence).filter_by(symbol_id=master.id).all():
        by_model[o.model_id].append(o.element_id)
    for mid, eids in by_model.items():
        m = db.get(BpaModel, mid)
        if m is None:
            continue
        m.xml_it = set_names(m.xml_it, {e: master.name_it for e in eids if master.name_it})
        m.xml_en = set_names(m.xml_en, {e: master.name_en for e in eids if master.name_en})
        db.add(m)
    db.add(master)
    db.flush()
    return {"merged": len(others), "models": len(by_model)}


# ---------- valori degli attributi ----------

def clean_value(d, value, lang: str, current=None):
    """Valore di un attributo controllato secondo il tipo; (valore, errore)."""
    t = d.data_type
    if value is None or value == "" or value == []:
        if d.translatable and t in TEXT_TYPES:
            cur = dict(current) if isinstance(current, dict) else {}
            cur.pop(lang, None)
            return (cur or None), None
        return None, None
    try:
        if t in TEXT_TYPES:
            v = str(value)[:20000 if t == "long_text" else 500]
            if t == "short_text":
                v = " ".join(v.split())
            if d.translatable:
                cur = dict(current) if isinstance(current, dict) else {}
                cur[lang] = v
                return cur, None
            return v, None
        if t == "integer":
            return int(str(value).strip()), None
        if t == "number":
            return float(str(value).replace(",", ".").strip()), None
        if t == "date":
            return date.fromisoformat(str(value)[:10]).isoformat(), None
        if t == "datetime":
            return datetime.fromisoformat(str(value)).replace(second=0, microsecond=0).isoformat(timespec="minutes"), None
        if t == "boolean":
            return (value in (True, "true", "1", 1, "yes", "si", "sì")), None
        opts = [o.get("value") for o in d.options or []]
        if t == "choice":
            return (value, None) if value in opts else (None, "choice")
        if t == "multi_choice":
            vals = [v for v in (value if isinstance(value, list) else [value]) if v in opts]
            return (vals or None), None
        if t == "url":
            v = str(value).strip()
            return (v, None) if re.match(r"^https?://\S+$", v) else (None, "url")
        if t == "email":
            v = str(value).strip()
            return (v, None) if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v) else (None, "email")
    except (TypeError, ValueError):
        return None, t
    return None, t


# ---------- viste SQL per tipo (consultazione dell'archivio come tabelle) ----------

def _slug(s: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", (s or "").lower()).strip("_")
    return s[:40] or "attr"


def refresh_views(engine=None, session_factory=None) -> None:
    """bpa_view_<tipo>: id, nome_it, nome_en, occorrenze e una colonna per attributo; piu' bpa_view_relations."""
    from app.db import SessionLocal, engine as default_engine
    from app.models import BpaAttributeDef
    engine = engine or default_engine
    if engine.dialect.name != "sqlite":
        return
    db = (session_factory or SessionLocal)()
    try:
        defs = db.query(BpaAttributeDef).order_by(BpaAttributeDef.position).all()
    finally:
        db.close()
    with engine.begin() as conn:
        for t in SYMBOL_TYPES:
            cols, used = [], {"id", "nome_it", "nome_en", "occorrenze"}
            for d in defs:
                if d.symbol_type != t:
                    continue
                c = _slug((d.name or {}).get("it") or d.key)
                if c in used:
                    c = c + "_" + d.key
                used.add(c)
                path = '$."%s"' % d.key
                if d.translatable and d.data_type in TEXT_TYPES:
                    cols.append(f"json_extract(s.attrs, '{path}.it') AS \"{c}_it\"")
                    cols.append(f"json_extract(s.attrs, '{path}.en') AS \"{c}_en\"")
                else:
                    cols.append(f"json_extract(s.attrs, '{path}') AS \"{c}\"")
            extra = (", " + ", ".join(cols)) if cols else ""
            conn.execute(text(f"DROP VIEW IF EXISTS bpa_view_{t}"))
            conn.execute(text(
                f"CREATE VIEW bpa_view_{t} AS SELECT s.id AS id, s.name_it AS nome_it, s.name_en AS nome_en, "
                f"(SELECT COUNT(*) FROM bpa_occurrence o WHERE o.symbol_id = s.id) AS occorrenze{extra} "
                f"FROM bpa_symbol s WHERE s.symbol_type = '{t}'"))
        conn.execute(text("DROP VIEW IF EXISTS bpa_view_relations"))
        conn.execute(text(
            "CREATE VIEW bpa_view_relations AS SELECT r.model_id AS modello, r.kind AS relazione, "
            "a.symbol_type AS tipo_da, a.name_it AS da_it, a.name_en AS da_en, "
            "b.symbol_type AS tipo_a, b.name_it AS a_it, b.name_en AS a_en "
            "FROM bpa_relation r JOIN bpa_symbol a ON a.id = r.source_id JOIN bpa_symbol b ON b.id = r.target_id"))
