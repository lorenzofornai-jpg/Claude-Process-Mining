"""Repository BPA > «Attributi della BPA» (Process Architect): attributi degli oggetti dei BPMN del repository e dei
processi della tassonomia, oggetti senza occorrenze, consolidamento di piu' oggetti in uno, tabella degli oggetti del
repository con il pannello delle proprieta'. I BPMN delle varianti (staging) non sono nell'archivio degli oggetti."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import STATIC_VERSION
from app.db import SessionLocal
from app.i18n import get_lang, msg, setup_templates, t
from app.routers.bpa_repository import _access

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.globals["static_version"] = STATIC_VERSION
setup_templates(templates)

def _bpa_options(raw: str) -> list[dict]:
    """Voci di un elenco, una per riga: «Etichetta italiana | English label» (la seconda parte e' facoltativa)."""
    out, seen = [], set()
    for line in (raw or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if not parts or not parts[0]:
            continue
        value = parts[0][:200]
        if value.casefold() in seen:
            continue
        seen.add(value.casefold())
        out.append({"value": value, "it": parts[0][:200], "en": (parts[1] if len(parts) > 1 and parts[1] else parts[0])[:200]})
    return out[:200]


def _repo_symbol_ids(db) -> set[str]:
    """Gli oggetti che compaiono in almeno un BPMN del repository BPA."""
    from app.models import BpaModel, BpaOccurrence
    return {sid for (sid,) in db.query(BpaOccurrence.symbol_id).join(BpaModel, BpaModel.id == BpaOccurrence.model_id)
            .filter(BpaModel.stage == "repository").distinct()}


def _bpa_processes(db) -> dict[str, set[str]]:
    """{id oggetto: codici dei processi della tassonomia ai cui BPMN appartiene}."""
    from app.models import BpaModel, BpaOccurrence, BpaProcess
    out: dict[str, set[str]] = {}
    q = (db.query(BpaOccurrence.symbol_id, BpaProcess.code)
         .join(BpaModel, BpaModel.id == BpaOccurrence.model_id)
         .join(BpaProcess, BpaProcess.id == BpaModel.process_id)
         .filter(BpaModel.stage == "repository").distinct())
    for sid, code in q.all():
        out.setdefault(sid, set()).add(code)
    return out


def _bpa_context(db, request, symbol_type: str, edit_key: str | None, errors=None, form=None):
    """Attributi del tipo scelto, oggetti senza occorrenze e oggetti del repository BPA del tipo (con i valori)."""
    from app.models import BpaAttributeDef, BpaOccurrence, BpaProcess, BpaSymbol
    from app.services import bpa_repo
    defs = (db.query(BpaAttributeDef).filter_by(symbol_type=symbol_type).order_by(BpaAttributeDef.position).all())
    used = {sid for (sid,) in db.query(BpaOccurrence.symbol_id).distinct()}
    repo = _repo_symbol_ids(db)
    counts, unused = {}, []
    for s in db.query(BpaSymbol).order_by(BpaSymbol.symbol_type, BpaSymbol.name_it).all():
        if s.id in repo:
            counts[s.symbol_type] = counts.get(s.symbol_type, 0) + 1
        elif s.id not in used and not s.anonymous:
            unused.append(s)
    counts[bpa_repo.PROCESS_TYPE] = db.query(BpaProcess).count()
    lang = get_lang(request)
    occ_count = {}
    for (sid,) in db.query(BpaOccurrence.symbol_id).all():
        occ_count[sid] = occ_count.get(sid, 0) + 1
    processes = _bpa_processes(db)
    rows = []
    if symbol_type != bpa_repo.PROCESS_TYPE:
        for s in db.query(BpaSymbol).filter_by(symbol_type=symbol_type).all():
            if s.id not in repo:
                continue
            rows.append({"id": s.id, "name": bpa_repo.display_name(db, s, lang), "anonymous": bool(s.anonymous),
                         "other": "" if s.anonymous else bpa_repo.name_of(s, "en" if lang == "it" else "it"),
                         "subtypes": ", ".join(bpa_repo.subtypes(db, s, lang)), "occ": occ_count.get(s.id, 0),
                         "processes": ", ".join(sorted(processes.get(s.id, ()))),
                         "values": [_bpa_value_text(d, (s.attrs or {}).get(d.key), lang) for d in defs]})
    rows.sort(key=lambda r: (r["anonymous"], r["name"].casefold()))
    return {"request": request, "types": bpa_repo.ATTR_TYPES, "symbol_type": symbol_type, "defs": defs,
            "data_types": bpa_repo.DATA_TYPES, "text_types": sorted(bpa_repo.TEXT_TYPES), "counts": counts,
            "unused": unused, "edit": next((d for d in defs if d.key == edit_key), None),
            "errors": errors or [], "form": form, "rows": rows}


def _bpa_merge_context(db, request, symbol_type: str, ids: list[str], master: str | None = None, error=None):
    """Pagina del consolidamento: gli oggetti del repository scelti con nomi, processi, occorrenze e attributi."""
    from app.models import BpaAttributeDef, BpaOccurrence, BpaSymbol
    from app.services import bpa_repo
    lang = get_lang(request)
    defs = db.query(BpaAttributeDef).filter_by(symbol_type=symbol_type).order_by(BpaAttributeDef.position).all()
    processes, repo = _bpa_processes(db), _repo_symbol_ids(db)
    items = []
    for sid in dict.fromkeys(ids):
        s = db.get(BpaSymbol, sid)
        if s is None or s.symbol_type != symbol_type or s.anonymous or s.id not in repo:
            continue
        items.append({"id": s.id, "name_it": s.name_it, "name_en": s.name_en,
                      "processes": ", ".join(sorted(processes.get(s.id, ()))),
                      "occ": db.query(BpaOccurrence).filter_by(symbol_id=s.id).count(),
                      "created_by": s.created_by,
                      "values": [_bpa_value_text(d, (s.attrs or {}).get(d.key), lang) for d in defs]})
    if master not in {i["id"] for i in items}:
        master = max(items, key=lambda i: i["occ"])["id"] if items else None
    return {"request": request, "types": bpa_repo.SYMBOL_TYPES, "symbol_type": symbol_type, "defs": defs,
            "items": items, "master": master, "error": error}


def _bpa_value_text(d, v, lang: str) -> str:
    """Valore di un attributo come testo per la tabella dei simboli."""
    if v is None or v == "" or v == []:
        return ""
    if d.data_type == "boolean":
        return t(lang, "Sì") if v else t(lang, "No")
    labels = {o.get("value"): o.get(lang) or o.get("it") or o.get("value") for o in d.options or []}
    if d.data_type == "choice":
        return labels.get(v, str(v))
    if d.data_type == "multi_choice":
        return ", ".join(labels.get(x, str(x)) for x in (v if isinstance(v, list) else [v]))
    if isinstance(v, dict):
        return v.get(lang) or v.get("it") or v.get("en") or ""
    if d.data_type == "datetime":
        return str(v).replace("T", " ")
    return str(v)


@router.get("/bpa/attributes", response_class=HTMLResponse)
def bpa_attributes(request: Request, type: str = "activity", edit: str = "", saved: int = 0, merged: int = 0):
    from app.services import bpa_repo
    user, denied = _access(request, edit=True, json=False)
    if denied:
        return denied
    symbol_type = type if type in bpa_repo.ATTR_TYPES else "activity"
    db = SessionLocal()
    try:
        ctx = _bpa_context(db, request, symbol_type, edit or None)
    finally:
        db.close()
    return templates.TemplateResponse("bpa_attributes.html", {**ctx, "user": user, "saved": saved, "merged": merged})


@router.post("/bpa/attributes/attr")
async def bpa_attributes_attr_save(request: Request):
    from app.models import BpaAttributeDef
    from app.services import bpa_repo
    user, denied = _access(request, edit=True, json=False)
    if denied:
        return denied
    form = await request.form()
    symbol_type = form.get("symbol_type") if form.get("symbol_type") in bpa_repo.ATTR_TYPES else "activity"
    key = str(form.get("key") or "")
    name_it, name_en = str(form.get("name_it") or "").strip()[:200], str(form.get("name_en") or "").strip()[:200]
    data_type = str(form.get("data_type") or "")
    errors = []
    if not name_it or not name_en:
        errors.append(msg("Dai il nome dell'attributo in italiano e in inglese."))
    if name_it.casefold() in ("nome", "identificativo", "name", "id") or name_en.casefold() in ("name", "id", "identifier"):
        errors.append(msg("«Nome» e «Identificativo» sono attributi di sistema: scegli un altro nome."))
    if symbol_type == bpa_repo.PROCESS_TYPE and (name_it.casefold() in ("codice", "descrizione")
                                                 or name_en.casefold() in ("code", "description")):
        errors.append(msg("«Codice», «Nome» e «Descrizione» sono attributi di sistema dei processi: scegli un altro nome."))
    db = SessionLocal()
    try:
        d = db.query(BpaAttributeDef).filter_by(key=key).first() if key else None
        if d is None and data_type not in bpa_repo.DATA_TYPES:
            errors.append(msg("Scegli il tipo di dato."))
        dtype = d.data_type if d is not None else data_type
        options = _bpa_options(str(form.get("options") or "")) if dtype in ("choice", "multi_choice") else None
        if dtype in ("choice", "multi_choice") and not options:
            errors.append(msg("Un elenco deve avere almeno una voce."))
        same = [x for x in db.query(BpaAttributeDef).filter_by(symbol_type=symbol_type).all()
                if x.key != key and ((x.name or {}).get("it", "").casefold() == name_it.casefold()
                                     or (x.name or {}).get("en", "").casefold() == name_en.casefold())]
        if same:
            errors.append(msg("Esiste già un attributo con questo nome per questo tipo di oggetto."))
        if errors:
            ctx = _bpa_context(db, request, symbol_type, key or None, errors, dict(form))
            return templates.TemplateResponse("bpa_attributes.html", {**ctx, "user": user, "saved": 0}, status_code=400)
        if d is None:
            last = db.query(BpaAttributeDef).filter_by(symbol_type=symbol_type).count()
            d = BpaAttributeDef(symbol_type=symbol_type, key=bpa_repo.new_attr_key(), data_type=dtype, position=last)
        d.name = {"it": name_it, "en": name_en}
        hi, he = str(form.get("help_it") or "").strip()[:500], str(form.get("help_en") or "").strip()[:500]
        d.help = {"it": hi, "en": he or hi} if hi or he else None
        d.options = options
        d.required = form.get("required") == "1"
        d.translatable = dtype in bpa_repo.TEXT_TYPES   # i testi hanno sempre un valore in italiano e uno in inglese
        db.add(d)
        db.commit()
    finally:
        db.close()
    bpa_repo.refresh_views()
    return RedirectResponse(f"/bpa/attributes?type={symbol_type}&saved=1", status_code=303)


@router.post("/bpa/attributes/attr/{key}/move")
def bpa_attributes_attr_move(request: Request, key: str, dir: str = Form("up")):
    from app.models import BpaAttributeDef
    from app.services import bpa_repo
    user, denied = _access(request, edit=True, json=False)
    if denied:
        return denied
    db = SessionLocal()
    try:
        d = db.query(BpaAttributeDef).filter_by(key=key).first()
        if d is None:
            return RedirectResponse("/bpa/attributes", status_code=303)
        defs = db.query(BpaAttributeDef).filter_by(symbol_type=d.symbol_type).order_by(BpaAttributeDef.position).all()
        i = defs.index(d)
        j = i - 1 if dir == "up" else i + 1
        if 0 <= j < len(defs):
            defs[i], defs[j] = defs[j], defs[i]
        for n, x in enumerate(defs):
            x.position = n
            db.add(x)
        db.commit()
        symbol_type = d.symbol_type
    finally:
        db.close()
    bpa_repo.refresh_views()
    return RedirectResponse(f"/bpa/attributes?type={symbol_type}", status_code=303)


@router.post("/bpa/attributes/attr/{key}/delete")
def bpa_attributes_attr_delete(request: Request, key: str):
    """Toglie l'attributo e i suoi valori da tutti i simboli."""
    from app.models import BpaAttributeDef, BpaSymbol
    from app.services import bpa_repo
    user, denied = _access(request, edit=True, json=False)
    if denied:
        return denied
    db = SessionLocal()
    try:
        d = db.query(BpaAttributeDef).filter_by(key=key).first()
        if d is None:
            return RedirectResponse("/bpa/attributes", status_code=303)
        symbol_type = d.symbol_type
        if symbol_type == bpa_repo.PROCESS_TYPE:
            from app.models import BpaProcess
            holders = db.query(BpaProcess).all()
        else:
            holders = db.query(BpaSymbol).filter_by(symbol_type=symbol_type).all()
        for s in holders:
            if key in (s.attrs or {}):
                s.attrs = {k: v for k, v in s.attrs.items() if k != key}
                db.add(s)
        db.delete(d)
        db.commit()
    finally:
        db.close()
    bpa_repo.refresh_views()
    return RedirectResponse(f"/bpa/attributes?type={symbol_type}&saved=1", status_code=303)


@router.post("/bpa/attributes/unused/delete")
async def bpa_attributes_unused_delete(request: Request):
    """Elimina i simboli scelti che non compaiono in nessun BPMN."""
    from app.models import BpaOccurrence, BpaRelation, BpaSymbol
    user, denied = _access(request, edit=True, json=False)
    if denied:
        return denied
    form = await request.form()
    ids = [str(x) for x in form.getlist("symbol")]
    db = SessionLocal()
    try:
        for sid in ids:
            if db.query(BpaOccurrence).filter_by(symbol_id=sid).first() is not None:
                continue   # nel frattempo e' stato usato in un BPMN: resta
            db.query(BpaRelation).filter((BpaRelation.source_id == sid) | (BpaRelation.target_id == sid)).delete(synchronize_session=False)
            s = db.get(BpaSymbol, sid)
            if s is not None:
                db.delete(s)
        db.commit()
    finally:
        db.close()
    return RedirectResponse(f"/bpa/attributes?type={form.get('symbol_type') or 'activity'}&saved=1#unused", status_code=303)


@router.get("/bpa/attributes/merge", response_class=HTMLResponse)
def bpa_attributes_merge_page(request: Request, type: str = "activity", symbol: list[str] = Query(default=[])):
    """Consolidamento: l'Amministratore sceglie l'oggetto da tenere (master) tra quelli spuntati nella tabella."""
    from app.services import bpa_repo
    user, denied = _access(request, edit=True, json=False)
    if denied:
        return denied
    symbol_type = type if type in bpa_repo.SYMBOL_TYPES else "activity"
    db = SessionLocal()
    try:
        ctx = _bpa_merge_context(db, request, symbol_type, symbol)
    finally:
        db.close()
    if len(ctx["items"]) < 2:
        return RedirectResponse(f"/bpa/attributes?type={symbol_type}#symbols", status_code=303)
    return templates.TemplateResponse("bpa_attributes_merge.html", {**ctx, "user": user})


@router.post("/bpa/attributes/merge")
async def bpa_attributes_merge(request: Request):
    """Consolida gli oggetti scelti nel master (services/bpa_repo.merge)."""
    from app.services import bpa_repo
    user, denied = _access(request, edit=True, json=False)
    if denied:
        return denied
    form = await request.form()
    symbol_type = form.get("symbol_type") if form.get("symbol_type") in bpa_repo.SYMBOL_TYPES else "activity"
    ids = [str(x) for x in form.getlist("symbol")]
    master = str(form.get("master") or "")
    db = SessionLocal()
    try:
        repo = _repo_symbol_ids(db)   # si consolidano solo oggetti del repository BPA
        ids = [i for i in ids if i in repo]
        out = bpa_repo.merge(db, master, ids, user.name)
        if out.get("error"):
            db.rollback()
            ctx = _bpa_merge_context(db, request, symbol_type, ids, master, out["error"])
            return templates.TemplateResponse("bpa_attributes_merge.html", {**ctx, "user": user}, status_code=400)
        db.commit()
    finally:
        db.close()
    return RedirectResponse(f"/bpa/attributes?type={symbol_type}&merged={out['merged']}#symbols", status_code=303)


@router.get("/bpa/attributes/symbols/{symbol_id}")
def bpa_attributes_symbol(request: Request, symbol_id: str):
    """Pannello delle proprieta' di un simbolo (come nell'editor BPMN)."""
    from fastapi.responses import JSONResponse
    from app.routers.bpa import symbol_detail
    user, denied = _access(request, edit=True)
    if denied:
        return denied
    lang = get_lang(request)
    db = SessionLocal()
    try:
        d = symbol_detail(db, symbol_id, lang, lang, user)
        return JSONResponse(d or {"error": t(lang, "Oggetto non trovato.")}, status_code=200 if d else 404)
    finally:
        db.close()


@router.post("/bpa/attributes/symbols/{symbol_id}/attrs")
async def bpa_attributes_symbol_attrs(request: Request, symbol_id: str):
    from fastapi.responses import JSONResponse
    from app.routers.bpa import save_symbol_attrs
    user, denied = _access(request, edit=True)
    if denied:
        return denied
    body = await request.json()
    ui = get_lang(request)
    lang = body.get("lang") if body.get("lang") in ("it", "en") else ui
    values = body.get("values") if isinstance(body.get("values"), dict) else {}
    db = SessionLocal()
    try:
        out = save_symbol_attrs(db, symbol_id, values, lang, ui, user.name)
        return JSONResponse(out or {"error": t(ui, "Oggetto non trovato.")}, status_code=200 if out else 404)
    finally:
        db.close()
