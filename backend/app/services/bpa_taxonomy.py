"""Repository BPA: tassonomia dei processi (BpaProcess) e modelli BPMN del repository (BpaModel con stage
"repository"), separati dai BPMN generati dalle varianti, che restano nella staging area.

- La tassonomia e' un albero di processi (distinti dalle attivita' dei BPMN), unico per l'azienda: codice interno
  (PR-0001...), nome e descrizione in italiano e in inglese.
- Un modello del repository e' assegnato a un processo. Si crea vuoto o si importa dalla staging: l'import e' una
  copia (la variante resta in staging, per il Process Overlay) e registra gli oggetti nell'archivio, consolidandoli
  con quelli del repository con lo stesso tipo e nome inglese (nel repository i nomi sono unici per tipo).
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, timezone

from app.services import bpa_repo, bpmn

LANGS = ("it", "en")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def text_of(value, lang: str) -> str:
    """Il testo nella lingua chiesta, o nell'altra se manca."""
    v = value or {}
    return v.get(lang) or v.get("en" if lang == "it" else "it") or ""


def both(value: str, lang: str, old: dict | None, translated: str | None) -> dict:
    """{"it", "en"} con il testo nuovo nella lingua dell'utente; l'altra lingua prende la traduzione, oppure resta com'era
    se era gia' stata scritta a parte (diversa dal testo di prima nella lingua dell'utente)."""
    other = "en" if lang == "it" else "it"
    old = dict(old or {})
    keep = old.get(other) and old.get(other) != old.get(lang)
    out = {lang: value}
    out[other] = old[other] if keep else (translated or value)
    return out


def needs_translation(value: str, lang: str, old: dict | None) -> bool:
    other = "en" if lang == "it" else "it"
    old = old or {}
    if not value:
        return False
    return not (old.get(other) and old.get(other) != old.get(lang)) and old.get(lang) != value


def translate(texts: dict[str, str], target: str) -> tuple[dict[str, str], float]:
    """Traduzioni dall'LLM (senza LLM: {} e i testi restano uguali nelle due lingue)."""
    texts = {k: v for k, v in texts.items() if v}
    return bpmn.llm_translate(texts, target) if texts else ({}, 0.0)


# ---------- tassonomia ----------

def next_code(db) -> str:
    from app.models import BpaProcess
    n = 0
    for (c,) in db.query(BpaProcess.code).all():
        m = re.match(r"^PR-(\d+)$", c or "")
        if m:
            n = max(n, int(m.group(1)))
    return f"PR-{n + 1:04d}"


def path_name(db, p, lang: str) -> str:
    """Il processo con i suoi antenati («Gestione › Acquisti › Ordini»): i nomi possono ripetersi nella tassonomia e
    il codice interno non si mostra."""
    from app.models import BpaProcess
    names, seen, x = [], set(), p
    while x is not None and x.id not in seen:
        seen.add(x.id)
        names.append(text_of(x.name, lang))
        x = db.get(BpaProcess, x.parent_id) if x.parent_id else None
    return " › ".join(reversed(names))


def children(db, parent_id: str | None) -> list:
    from app.models import BpaProcess
    return db.query(BpaProcess).filter(BpaProcess.parent_id.is_(None) if parent_id is None
                                       else BpaProcess.parent_id == parent_id).order_by(BpaProcess.position).all()


def create_process(db, parent_id: str | None, name: dict, user_name: str | None):
    from app.models import BpaProcess
    p = BpaProcess(code=next_code(db), parent_id=parent_id, position=len(children(db, parent_id)), name=name,
                   description=None, created_by=user_name)
    db.add(p)
    db.flush()
    return p


def ancestors(db, process_id: str | None) -> list[str]:
    from app.models import BpaProcess
    out, seen = [], set()
    while process_id and process_id not in seen:
        seen.add(process_id)
        out.append(process_id)
        p = db.get(BpaProcess, process_id)
        process_id = p.parent_id if p else None
    return out


def move_process(db, p, parent_id: str | None, position: int) -> bool:
    """Sposta il processo sotto un altro (o alla radice) nella posizione data. False se il nuovo padre e' il processo
    stesso o un suo discendente."""
    if parent_id and p.id in ancestors(db, parent_id):
        return False
    old_parent = p.parent_id
    siblings = [x for x in children(db, parent_id) if x.id != p.id]
    position = max(0, min(position, len(siblings)))
    siblings.insert(position, p)
    p.parent_id = parent_id
    for i, x in enumerate(siblings):
        x.position = i
        db.add(x)
    if old_parent != parent_id:
        for i, x in enumerate(y for y in children(db, old_parent) if y.id != p.id):
            x.position = i
            db.add(x)
    db.flush()
    return True


def delete_process(db, p) -> str | None:
    """Elimina un processo senza sottoprocessi e senza modelli; altrimenti il motivo (msg)."""
    from app.i18n import msg
    from app.models import BpaModel, BpaProcess
    if db.query(BpaProcess).filter_by(parent_id=p.id).first() is not None:
        return msg("Il processo ha dei sottoprocessi: eliminali o spostali prima.")
    if db.query(BpaModel).filter_by(process_id=p.id, stage="repository").first() is not None:
        return msg("Al processo sono assegnati dei modelli BPMN: eliminali prima.")
    parent = p.parent_id
    db.delete(p)
    db.flush()
    for i, x in enumerate(children(db, parent)):
        x.position = i
        db.add(x)
    return None


def tree(db, lang: str) -> list[dict]:
    from app.models import BpaModel, BpaProcess
    counts: dict[str, int] = {}
    for (pid,) in db.query(BpaModel.process_id).filter(BpaModel.stage == "repository").all():
        if pid:
            counts[pid] = counts.get(pid, 0) + 1
    return [{"id": p.id, "parent": p.parent_id, "position": p.position,
             "name": text_of(p.name, lang), "models": counts.get(p.id, 0)}
            for p in db.query(BpaProcess).order_by(BpaProcess.position).all()]


# ---------- modelli del repository ----------

def repository_models(db, process_id: str) -> list:
    from app.models import BpaModel
    return (db.query(BpaModel).filter_by(process_id=process_id, stage="repository")
            .order_by(BpaModel.created_at).all())


def new_model(db, process, user_name: str | None, user_id: str | None = None):
    """BPMN nuovo, vuoto (pool con il nome del processo, una lane da rinominare, inizio e fine)."""
    from app.models import BpaModel
    name = {lang: text_of(process.name, lang) for lang in LANGS}
    xml = bpmn.generate_both(name, [], {}, {})
    m = BpaModel(ingestion_config_id=None, stage="repository", process_id=process.id, lead_type="", variant_key="",
                 steps=[], stats={}, labels={}, lanes={}, xml_it=xml["it"], xml_en=xml["en"], title=name,
                 created_by_id=user_id, created_by=user_name)
    db.add(m)
    db.flush()
    bpa_repo.register_generated(db, m, user_name)
    return m


def staging_title(src, lang: str) -> str:
    from app.i18n import t
    rank = (src.stats or {}).get("rank")
    v = t(lang, "Variante {n}", n=rank) if rank else t(lang, "Variante")
    return f"{v} · {src.lead_type}"


def import_model(db, src, process, user_name: str | None, user_id: str | None = None):
    """Copia nel repository un modello della staging, assegnata al processo, e ne registra gli oggetti: e' qui che si
    consolida. Un elemento con lo stesso tipo e lo stesso nome inglese di un oggetto del repository diventa
    un'occorrenza di quello; gli altri sono oggetti nuovi (anche gli elementi senza nome proprio)."""
    from app.models import BpaModel, BpaSymbol
    m = BpaModel(ingestion_config_id=None, stage="repository", process_id=process.id, source_model_id=src.id,
                 source_config_id=src.ingestion_config_id, lead_type=src.lead_type, variant_key=src.variant_key,
                 steps=copy.deepcopy(src.steps or []), stats=copy.deepcopy(src.stats or {}),
                 labels=copy.deepcopy(src.labels or {}), lanes=copy.deepcopy(src.lanes or {}),
                 xml_it=src.xml_it, xml_en=src.xml_en, edited=src.edited,
                 title={lang: staging_title(src, lang) for lang in LANGS},
                 created_by_id=user_id, created_by=user_name)
    db.add(m)
    db.flush()
    ext = bpa_repo.extract(m.xml_en)
    assign, new = {}, {}
    for eid, e in ext["elements"].items():
        if not bpa_repo.is_symbol(e):
            assign[eid] = "new:" + eid
            new[eid] = {"type": e["type"], "name": e["name"], "anonymous": True}
            continue
        hit = bpa_repo.find_by_name(db, e["type"], e["name"], "en", configs={bpa_repo.REPO_SCOPE})
        s = hit
        if s is not None and s.symbol_type == e["type"] and not s.anonymous:
            assign[eid] = s.id
        else:
            assign[eid] = "new:" + eid
            new[eid] = {"type": e["type"], "name": e["name"]}
    plan = {"assign": assign, "new": new, "renames": {}, "ext": ext, "anon_names": {}}
    names = bpa_repo.other_names(db, plan, "en")
    xml_it = bpa_repo.set_names(m.xml_it, names) if names else m.xml_it
    # nomi inglesi: quelli degli oggetti (es. lo stesso oggetto gia' nel repository)
    en_names = {}
    for eid, sid in assign.items():
        if not sid.startswith("new:"):
            s = db.get(BpaSymbol, sid)
            if s is not None and s.name_en:
                en_names[eid] = s.name_en
    xml_en = bpa_repo.set_names(m.xml_en, en_names)
    out = bpa_repo.apply(db, m, "en", xml_en, xml_it, plan, user_name)
    m.xml_en, m.xml_it = out["xml"], out["other_xml"]
    db.add(m)
    db.flush()
    return m


def delete_model(db, m) -> None:
    """Toglie un modello dal repository (la variante d'origine resta nella staging; i Process Overlay che lo usavano
    restano senza BPMN di riferimento)."""
    from app.models import BpaOccurrence, BpaOverlayRef, BpaRelation
    db.query(BpaOverlayRef).filter_by(model_id=m.id).delete(synchronize_session=False)
    db.query(BpaOccurrence).filter_by(model_id=m.id).delete(synchronize_session=False)
    db.query(BpaRelation).filter_by(model_id=m.id).delete(synchronize_session=False)
    db.flush()
    bpa_repo.drop_orphan_anonymous(db)
    db.delete(m)
    db.flush()
