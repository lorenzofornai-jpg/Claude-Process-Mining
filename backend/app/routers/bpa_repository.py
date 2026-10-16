"""Repository BPA (Business Process Analysis): il «digital twin» dei processi dell'azienda, autonomo dai dataset.

- Tassonomia dei processi a sinistra (come un esplora risorse), a destra gli attributi del processo scelto e i modelli
  BPMN assegnati (services/bpa_taxonomy.py).
- Process Architect (User.bpa_role = "architect"): modifica tassonomia e modelli, crea BPMN nuovi, importa quelli della
  staging area (i BPMN generati dalle varianti, che restano dove sono). Process Viewer ("viewer"): sola lettura.
- I modelli del repository si aprono nello stesso editor delle varianti (indirizzi /bpa/m/...).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import current_user
from app.config import STATIC_VERSION
from app.db import SessionLocal
from app.i18n import get_lang, render, setup_templates, t, utc_iso
from app.models import BpaModel, BpaProcess, IngestionConfig, ProcessWorkspace
from app.routers import bpa as staging
from app.services import bpa_taxonomy as tax

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.globals["static_version"] = STATIC_VERSION
setup_templates(templates)

ROLES = {"architect": "Process Architect", "viewer": "Process Viewer"}
MAX_NAME, MAX_DESC = 300, 20000


def _access(request: Request, edit: bool = False, json: bool = True):
    """(utente, None) se ha un ruolo nel repository BPA (Process Architect per modificare), altrimenti (None, errore)."""
    user = current_user(request)
    lang = get_lang(request)
    if user is None:
        return None, (JSONResponse({"error": t(lang, "Sessione scaduta: accedi di nuovo.")}, status_code=401) if json
                      else RedirectResponse("/login", status_code=303))
    role = None if user.is_admin else user.bpa_role
    if role not in ("architect", "viewer") or (edit and role != "architect"):
        text = (t(lang, "Accesso negato: serve il ruolo Process Architect.") if edit and role == "viewer"
                else t(lang, "Accesso negato: non hai un ruolo nel repository BPA (Process Architect o Process Viewer)."))
        return None, (JSONResponse({"error": text}, status_code=403) if json else HTMLResponse(text, status_code=403))
    return user, None


def _repo_model(db, model_id: str):
    m = db.get(BpaModel, model_id)
    return m if m is not None and m.stage == "repository" else None


def _model_json(db, m: BpaModel, lang: str, cache: dict) -> dict:
    src = None
    if m.source_config_id:
        if m.source_config_id not in cache:
            c = db.get(IngestionConfig, m.source_config_id)
            cache[m.source_config_id] = c.name if c else None
        src = cache[m.source_config_id]
    return {"id": m.id, "title": tax.text_of(m.title, lang) or "BPMN", "source": src,
            "from_staging": bool(m.source_model_id), "created_by": m.created_by, "created_at": utc_iso(m.created_at),
            "updated_by": m.updated_by, "updated_at": utc_iso(m.updated_at) if m.updated_at else None}


def _process_json(db, p: BpaProcess, lang: str) -> dict:
    cache: dict = {}
    return {"id": p.id, "code": p.code, "parent": p.parent_id,
            "name": tax.text_of(p.name, lang), "description": tax.text_of(p.description, lang),
            "name_other": tax.text_of(p.name, "en" if lang == "it" else "it"),
            "created_by": p.created_by, "created_at": utc_iso(p.created_at),
            "updated_by": p.updated_by, "updated_at": utc_iso(p.updated_at) if p.updated_at else None,
            "models": [_model_json(db, m, lang, cache) for m in tax.repository_models(db, p.id)]}


# ---------- pagina ----------

@router.get("/bpa", response_class=HTMLResponse)
def bpa_home(request: Request, process: str = ""):
    user, denied = _access(request, json=False)
    if denied:
        return denied
    return templates.TemplateResponse("bpa_repository.html", {
        "request": request, "user": user, "can_edit": user.bpa_role == "architect", "selected": process[:40] or None})


# ---------- tassonomia ----------

@router.get("/bpa/api/tree")
def api_tree(request: Request):
    user, denied = _access(request)
    if denied:
        return denied
    db = SessionLocal()
    try:
        return JSONResponse({"nodes": tax.tree(db, get_lang(request))})
    finally:
        db.close()


@router.get("/bpa/api/processes/{process_id}")
def api_process(request: Request, process_id: str):
    user, denied = _access(request)
    if denied:
        return denied
    lang = get_lang(request)
    db = SessionLocal()
    try:
        p = db.get(BpaProcess, process_id)
        if p is None:
            return JSONResponse({"error": t(lang, "Processo non trovato.")}, status_code=404)
        return JSONResponse({"process": _process_json(db, p, lang)})
    finally:
        db.close()


async def _translated(value: str, lang: str, old: dict | None) -> tuple[dict, float]:
    other = "en" if lang == "it" else "it"
    tr, cost = ({}, 0.0)
    if tax.needs_translation(value, lang, old):
        tr, cost = await run_in_threadpool(tax.translate, {"v": value}, other)
    return tax.both(value, lang, old, tr.get("v")), cost


@router.post("/bpa/api/processes")
async def api_create(request: Request):
    user, denied = _access(request, edit=True)
    if denied:
        return denied
    body = await request.json()
    lang = get_lang(request)
    name = " ".join(str(body.get("name") or "").split())[:MAX_NAME]
    parent = str(body.get("parent_id") or "") or None
    if not name:
        return JSONResponse({"error": t(lang, "Dai un nome al processo.")}, status_code=400)
    db = SessionLocal()
    try:
        if parent and db.get(BpaProcess, parent) is None:
            return JSONResponse({"error": t(lang, "Processo non trovato.")}, status_code=404)
    finally:
        db.close()
    names, _ = await _translated(name, lang, None)
    db = SessionLocal()
    try:
        p = tax.create_process(db, parent, names, user.name)
        db.commit()
        return JSONResponse({"process": _process_json(db, p, lang)})
    finally:
        db.close()


@router.post("/bpa/api/processes/{process_id}")
async def api_update(request: Request, process_id: str):
    """Nome e descrizione nella lingua dell'interfaccia; l'altra lingua si traduce (se non era stata scritta a parte)."""
    user, denied = _access(request, edit=True)
    if denied:
        return denied
    body = await request.json()
    lang = get_lang(request)
    name = " ".join(str(body.get("name") or "").split())[:MAX_NAME]
    desc = str(body.get("description") or "").strip()[:MAX_DESC]
    if not name:
        return JSONResponse({"error": t(lang, "Dai un nome al processo.")}, status_code=400)
    db = SessionLocal()
    try:
        p = db.get(BpaProcess, process_id)
        if p is None:
            return JSONResponse({"error": t(lang, "Processo non trovato.")}, status_code=404)
        old_name, old_desc = dict(p.name or {}), dict(p.description or {})
    finally:
        db.close()
    names, _ = await _translated(name, lang, old_name)
    descs, _ = await _translated(desc, lang, old_desc) if desc else ({}, 0.0)
    db = SessionLocal()
    try:
        p = db.get(BpaProcess, process_id)
        if p is None:
            return JSONResponse({"error": t(lang, "Processo non trovato.")}, status_code=404)
        p.name, p.description = names, (descs or None)
        p.updated_by, p.updated_at = user.name, tax._now()
        db.add(p)
        db.commit()
        return JSONResponse({"process": _process_json(db, p, lang)})
    finally:
        db.close()


@router.post("/bpa/api/processes/{process_id}/move")
async def api_move(request: Request, process_id: str):
    user, denied = _access(request, edit=True)
    if denied:
        return denied
    body = await request.json()
    lang = get_lang(request)
    parent = str(body.get("parent_id") or "") or None
    try:
        position = int(body.get("position", 10**6))
    except (TypeError, ValueError):
        position = 10**6
    db = SessionLocal()
    try:
        p = db.get(BpaProcess, process_id)
        if p is None or (parent and db.get(BpaProcess, parent) is None):
            return JSONResponse({"error": t(lang, "Processo non trovato.")}, status_code=404)
        if not tax.move_process(db, p, parent, position):
            return JSONResponse({"error": t(lang, "Un processo non si sposta dentro se stesso o un suo sottoprocesso.")},
                                status_code=400)
        db.commit()
        return JSONResponse({"moved": True})
    finally:
        db.close()


@router.post("/bpa/api/processes/{process_id}/delete")
def api_delete(request: Request, process_id: str):
    user, denied = _access(request, edit=True)
    if denied:
        return denied
    lang = get_lang(request)
    db = SessionLocal()
    try:
        p = db.get(BpaProcess, process_id)
        if p is None:
            return JSONResponse({"error": t(lang, "Processo non trovato.")}, status_code=404)
        reason = tax.delete_process(db, p)
        if reason:
            return JSONResponse({"error": render(lang, reason)}, status_code=400)
        db.commit()
        return JSONResponse({"deleted": True})
    finally:
        db.close()


# ---------- modelli: staging area, nuovi, importati ----------

@router.get("/bpa/api/staging")
def api_staging(request: Request):
    """I BPMN generati dalle varianti (tutti i processi e dataset), con dove sono gia' stati importati."""
    user, denied = _access(request, edit=True)
    if denied:
        return denied
    lang = get_lang(request)
    db = SessionLocal()
    try:
        imported: dict[str, list] = {}
        for m in db.query(BpaModel).filter(BpaModel.stage == "repository", BpaModel.source_model_id.isnot(None)).all():
            p = db.get(BpaProcess, m.process_id) if m.process_id else None
            imported.setdefault(m.source_model_id, []).append(f"{p.code} {tax.text_of(p.name, lang)}" if p else "—")
        configs, workspaces, rows = {}, {}, []
        for m in db.query(BpaModel).filter(BpaModel.stage.is_(None)).order_by(BpaModel.created_at.desc()).all():
            if m.ingestion_config_id not in configs:
                c = db.get(IngestionConfig, m.ingestion_config_id) if m.ingestion_config_id else None
                configs[m.ingestion_config_id] = c.name if c else "—"
            ws_id = staging._workspace_of(db, m)
            if ws_id not in workspaces:
                ws = db.get(ProcessWorkspace, ws_id) if ws_id else None
                workspaces[ws_id] = ws.process_name if ws else "—"
            rows.append({"id": m.id, "title": tax.staging_title(m, lang), "process": workspaces[ws_id],
                         "dataset": configs[m.ingestion_config_id], "stats": m.stats or {}, "edited": bool(m.edited),
                         "steps": len(m.steps or []), "created_at": utc_iso(m.created_at),
                         "imported": imported.get(m.id, [])})
        return JSONResponse({"models": rows})
    finally:
        db.close()


@router.post("/bpa/api/processes/{process_id}/import")
async def api_import(request: Request, process_id: str):
    user, denied = _access(request, edit=True)
    if denied:
        return denied
    body = await request.json()
    lang = get_lang(request)
    db = SessionLocal()
    try:
        p = db.get(BpaProcess, process_id)
        src = db.get(BpaModel, str(body.get("model_id") or ""))
        if p is None:
            return JSONResponse({"error": t(lang, "Processo non trovato.")}, status_code=404)
        if src is None or src.stage is not None:
            return JSONResponse({"error": t(lang, "Modello non trovato.")}, status_code=404)
        m = tax.import_model(db, src, p, user.name, user.id)
        db.commit()
        return JSONResponse({"model": _model_json(db, m, lang, {})})
    finally:
        db.close()


@router.post("/bpa/api/processes/{process_id}/models/new")
def api_new_model(request: Request, process_id: str):
    user, denied = _access(request, edit=True)
    if denied:
        return denied
    lang = get_lang(request)
    db = SessionLocal()
    try:
        p = db.get(BpaProcess, process_id)
        if p is None:
            return JSONResponse({"error": t(lang, "Processo non trovato.")}, status_code=404)
        m = tax.new_model(db, p, user.name, user.id)
        db.commit()
        return JSONResponse({"model": _model_json(db, m, lang, {})})
    finally:
        db.close()


@router.post("/bpa/api/models/{model_id}/rename")
async def api_rename_model(request: Request, model_id: str):
    user, denied = _access(request, edit=True)
    if denied:
        return denied
    body = await request.json()
    lang = get_lang(request)
    title = " ".join(str(body.get("title") or "").split())[:MAX_NAME]
    if not title:
        return JSONResponse({"error": t(lang, "Dai un nome al modello.")}, status_code=400)
    db = SessionLocal()
    try:
        m = _repo_model(db, model_id)
        if m is None:
            return JSONResponse({"error": t(lang, "Modello non trovato.")}, status_code=404)
        old = dict(m.title or {})
    finally:
        db.close()
    titles, _ = await _translated(title, lang, old)
    db = SessionLocal()
    try:
        m = _repo_model(db, model_id)
        m.title = titles
        m.updated_by, m.updated_at = user.name, tax._now()
        db.add(m)
        db.commit()
        return JSONResponse({"model": _model_json(db, m, lang, {})})
    finally:
        db.close()


@router.post("/bpa/api/models/{model_id}/delete")
def api_delete_model(request: Request, model_id: str):
    user, denied = _access(request, edit=True)
    if denied:
        return denied
    lang = get_lang(request)
    db = SessionLocal()
    try:
        m = _repo_model(db, model_id)
        if m is None:
            return JSONResponse({"error": t(lang, "Modello non trovato.")}, status_code=404)
        tax.delete_model(db, m)
        db.commit()
        return JSONResponse({"deleted": True})
    finally:
        db.close()


# ---------- editor e visualizzazione dei modelli del repository ----------

def _page_model(request: Request, model_id: str, edit: bool):
    user, denied = _access(request, edit=edit, json=False)
    if denied:
        return None, None, denied
    db = SessionLocal()
    try:
        m = _repo_model(db, model_id)
        if m is None:
            return None, None, HTMLResponse(t(get_lang(request), "Modello non trovato."), status_code=404)
        p = db.get(BpaProcess, m.process_id) if m.process_id else None
        lang = get_lang(request)
        info = {"model": staging._json(m), "repo_title": tax.text_of(m.title, lang) or "BPMN",
                "process_name": f"{p.code} {tax.text_of(p.name, lang)}" if p else "—", "process_id": p.id if p else "",
                "file_name": (p.code if p else "bpmn")}
    finally:
        db.close()
    return user, info, None


@router.get("/bpa/models/{model_id}/edit", response_class=HTMLResponse)
def model_edit(request: Request, model_id: str):
    user, info, denied = _page_model(request, model_id, edit=True)
    if denied:
        if user is None and current_user(request) is not None and current_user(request).bpa_role == "viewer":
            return RedirectResponse(f"/bpa/models/{model_id}/view", status_code=303)
        return denied
    return templates.TemplateResponse("bpa_editor.html", {
        "request": request, "user": user, "workspace_id": "", "full_page": True,
        "focus": str(request.query_params.get("focus") or "")[:200] or None, **info})


@router.get("/bpa/models/{model_id}/view", response_class=HTMLResponse)
def model_view(request: Request, model_id: str):
    user, info, denied = _page_model(request, model_id, edit=False)
    if denied:
        return denied
    return templates.TemplateResponse("bpa_viewer.html", {
        "request": request, "user": user, "can_edit": user.bpa_role == "architect",
        "focus": str(request.query_params.get("focus") or "")[:200] or None, **info})


# indirizzi dell'editor (bpa_editor.js / bpa_repo.js con base "/bpa/m"): gli stessi della staging, per il repository

@router.get("/bpa/m/meta")
def m_meta(request: Request):
    user, denied = _access(request)
    if denied:
        return denied
    return staging.meta(get_lang(request))


@router.get("/bpa/m/symbols/lookup")
def m_lookup(request: Request, model_id: str, type: str, name: str, lang: str = "it", exclude: str = ""):
    user, denied = _access(request)
    if denied:
        return denied
    return staging.symbol_lookup(lambda db: _repo_model(db, model_id), type, name, lang, exclude, get_lang(request), user)


@router.get("/bpa/m/symbols/{symbol_id}")
def m_symbol(request: Request, symbol_id: str, lang: str = "", model_id: str = ""):
    user, denied = _access(request)
    if denied:
        return denied
    ui = get_lang(request)
    lang = lang if lang in ("it", "en") else ui
    db = SessionLocal()
    try:
        d = staging.symbol_detail(db, symbol_id, ui, lang, user, model_id)
        if d is None:
            return JSONResponse({"error": t(ui, "Oggetto non trovato.")}, status_code=404)
        return JSONResponse(d)
    finally:
        db.close()


@router.post("/bpa/m/symbols/{symbol_id}/attrs")
async def m_symbol_attrs(request: Request, symbol_id: str):
    user, denied = _access(request, edit=True)
    if denied:
        return denied
    body = await request.json()
    ui = get_lang(request)
    lang = body.get("lang") if body.get("lang") in ("it", "en") else ui
    values = body.get("values") if isinstance(body.get("values"), dict) else {}
    db = SessionLocal()
    try:
        out = staging.save_symbol_attrs(db, symbol_id, values, lang, ui, user.name)
        return JSONResponse(out or {"error": t(ui, "Oggetto non trovato.")}, status_code=200 if out else 404)
    finally:
        db.close()


@router.get("/bpa/m/{model_id}/xml")
def m_xml(request: Request, model_id: str, lang: str = "it", download: int = 0):
    user, denied = _access(request)
    if denied:
        return denied
    return staging.model_xml(lambda db: _repo_model(db, model_id), get_lang(request), lang, download)


@router.get("/bpa/m/{model_id}/symbols")
def m_symbols(request: Request, model_id: str):
    user, denied = _access(request)
    if denied:
        return denied
    return staging.model_symbols(lambda db: _repo_model(db, model_id), get_lang(request), user)


@router.post("/bpa/m/{model_id}/save")
async def m_save(request: Request, model_id: str):
    user, denied = _access(request, edit=True)
    if denied:
        return denied
    body = await request.json()

    def get(db):
        return _repo_model(db, model_id)
    return await staging.save_model(get, body, get_lang(request), user)
