"""Modulo 2 - Analisi > BPA (Business Process Analysis).

Dalla Process Overview (oggetto guida con gli oggetti collegati) una variante si invia alla BPA: diventa un modello
BPMN 2.0 (services/bpmn.py) in italiano e in inglese, da aprire a tutta pagina nell'editor (bpmn-js) e completare.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from app.config import STATIC_VERSION
from app.db import SessionLocal
from app.i18n import get_lang, setup_templates, t, utc_iso
from app.models import BpaModel, ProcessWorkspace
from app.routers.analysis import _explorer_dataset, _require_analyst_access
from app.services import bpmn, ocel_filter

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.globals["static_version"] = STATIC_VERSION
setup_templates(templates)

MAX_STEPS = 200


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _steps(raw) -> list[dict]:
    out = []
    for s in raw or []:
        if isinstance(s, dict) and str(s.get("activity") or "").strip():
            out.append({"activity": str(s["activity"])[:300], "repeated": bool(s.get("repeated"))})
    return out[:MAX_STEPS]


def variant_key(lead: str, steps: list[dict]) -> str:
    return json.dumps([lead, [[s["activity"], s["repeated"]] for s in steps]], ensure_ascii=False, separators=(",", ":"))


def _json(m: BpaModel) -> dict:
    return {"id": m.id, "lead": m.lead_type, "key": m.variant_key, "steps": m.steps, "stats": m.stats or {},
            "edited": bool(m.edited), "created_by": m.created_by, "created_at": utc_iso(m.created_at),
            "updated_by": m.updated_by, "updated_at": utc_iso(m.updated_at) if m.updated_at else None}


def _model(db, model_id: str, workspace_id: str) -> BpaModel | None:
    """Il modello, se e' di un dataset di questo processo."""
    m = db.get(BpaModel, model_id)
    if m is None:
        return None
    config, _, _ = _explorer_dataset(workspace_id, m.ingestion_config_id)
    return m if config is not None and config.id == m.ingestion_config_id else None


def _known_labels(db, config_id: str) -> dict:
    """Traduzioni gia' fatte per gli altri modelli del dataset (nessuna chiamata in piu' per gli stessi nomi)."""
    out = {}
    for m in db.query(BpaModel).filter_by(ingestion_config_id=config_id).all():
        out.update(m.labels or {})
    return out


def _build(process_name: str, lead: str, steps: list[dict], ocel_path: str | None, known: dict) -> dict:
    lanes = bpmn.role_lanes(ocel_filter.load_raw(ocel_path), [s["activity"] for s in steps]) if ocel_path else {}
    texts = [process_name] + [s["activity"] for s in steps] + sorted(set(lanes.values()))
    labels, cost = bpmn.labels_both(list(dict.fromkeys(texts)), known)
    xml = bpmn.generate_both(labels.get(process_name) or {"it": process_name, "en": process_name}, steps, labels, lanes)
    return {"labels": labels, "lanes": lanes, "xml": xml, "cost_usd": cost}


# ---------- dalla Process Overview ----------

@router.get("/analysis/bpa/sent")
def bpa_sent(request: Request, workspace_id: str, config_id: str):
    """Varianti gia' inviate alla BPA per questo dataset (la Process Overview mostra «Nella BPA»)."""
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(get_lang(request), "Accesso negato.")}, status_code=403)
    config, _, _ = _explorer_dataset(workspace_id, config_id)
    if config is None:
        return JSONResponse({"models": []})
    db = SessionLocal()
    try:
        rows = db.query(BpaModel).filter_by(ingestion_config_id=config.id).all()
        return JSONResponse({"models": [{"id": m.id, "key": m.variant_key, "edited": bool(m.edited)} for m in rows]})
    finally:
        db.close()


@router.post("/analysis/bpa/send")
async def bpa_send(request: Request):
    """Invia una variante alla BPA: crea il modello BPMN (italiano e inglese). Se c'e' gia', lo restituisce."""
    body = await request.json()
    workspace_id, config_id = str(body.get("workspace_id") or ""), str(body.get("config_id") or "")
    lang = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    config, run, _ = _explorer_dataset(workspace_id, config_id)
    lead = str(body.get("lead") or "")[:200]
    steps = _steps(body.get("steps"))
    if config is None or not lead or not steps:
        return JSONResponse({"error": t(lang, "Variante non valida.")}, status_code=400)
    key = variant_key(lead, steps)
    st = body.get("stats") if isinstance(body.get("stats"), dict) else {}
    stats = {k: st.get(k) for k in ("rank", "count", "share", "median") if isinstance(st.get(k), (int, float))}
    db = SessionLocal()
    try:
        m = db.query(BpaModel).filter_by(ingestion_config_id=config.id, variant_key=key).first()
        if m is not None:
            return JSONResponse({"model": _json(m), "existing": True})
        ws = db.get(ProcessWorkspace, workspace_id)
        known = _known_labels(db, config.id)
    finally:
        db.close()
    ocel = run.ocel_file_path if run is not None and Path(run.ocel_file_path).exists() else None
    built = await run_in_threadpool(_build, ws.process_name, lead, steps, ocel, known)
    db = SessionLocal()
    try:
        m = BpaModel(ingestion_config_id=config.id, lead_type=lead, variant_key=key, steps=steps, stats=stats,
                     labels=built["labels"], lanes=built["lanes"], xml_it=built["xml"]["it"], xml_en=built["xml"]["en"],
                     cost_usd=built["cost_usd"], created_by_id=user.id, created_by=user.name)
        db.add(m)
        db.commit()
        return JSONResponse({"model": _json(m), "cost_usd": built["cost_usd"]})
    finally:
        db.close()


@router.post("/analysis/bpa/{model_id}/revoke")
async def bpa_revoke(request: Request, model_id: str):
    """Toglie la variante dalla BPA (con il suo modello BPMN)."""
    body = await request.json()
    workspace_id = str(body.get("workspace_id") or "")
    lang = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    db = SessionLocal()
    try:
        m = _model(db, model_id, workspace_id)
        if m is None:
            return JSONResponse({"error": t(lang, "Modello non trovato.")}, status_code=404)
        db.delete(m)
        db.commit()
        return JSONResponse({"revoked": True})
    finally:
        db.close()


# ---------- pagina BPA ed editor ----------

def _page_context(request: Request, workspace_id: str, config_id: str | None):
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return None, denied
    config, run, configs = _explorer_dataset(workspace_id, config_id)
    if config is None or run is None:
        return None, HTMLResponse(t(get_lang(request), "Dataset non disponibile per l'analisi."), status_code=404)
    db = SessionLocal()
    try:
        ws = db.get(ProcessWorkspace, workspace_id)
    finally:
        db.close()
    return {"request": request, "user": user, "workspace_id": workspace_id, "process_name": ws.process_name,
            "config": config, "run": run, "configs": configs}, None


@router.get("/analysis/bpa", response_class=HTMLResponse)
def bpa_page(request: Request, workspace_id: str, config_id: str | None = None):
    ctx, denied = _page_context(request, workspace_id, config_id)
    if denied:
        return denied
    db = SessionLocal()
    try:
        rows = (db.query(BpaModel).filter_by(ingestion_config_id=ctx["config"].id)
                .order_by(BpaModel.lead_type, BpaModel.created_at).all())
        models = [_json(m) for m in rows]
    finally:
        db.close()
    return templates.TemplateResponse("bpa.html", {**ctx, "models": models})


@router.get("/analysis/bpa/{model_id}/edit", response_class=HTMLResponse)
def bpa_editor(request: Request, model_id: str, workspace_id: str):
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return denied
    db = SessionLocal()
    try:
        m = _model(db, model_id, workspace_id)
        if m is None:
            return HTMLResponse(t(get_lang(request), "Modello non trovato."), status_code=404)
        ws = db.get(ProcessWorkspace, workspace_id)
        model = _json(m)
        config_id = m.ingestion_config_id
    finally:
        db.close()
    return templates.TemplateResponse("bpa_editor.html", {
        "request": request, "user": user, "workspace_id": workspace_id, "process_name": ws.process_name,
        "config_id": config_id, "model": model, "full_page": True})


@router.get("/analysis/bpa/{model_id}/xml")
def bpa_xml(request: Request, model_id: str, workspace_id: str, lang: str = "it", download: int = 0):
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(get_lang(request), "Accesso negato.")}, status_code=403)
    lang = lang if lang in bpmn.LANGS else "it"
    db = SessionLocal()
    try:
        m = _model(db, model_id, workspace_id)
        if m is None:
            return JSONResponse({"error": t(get_lang(request), "Modello non trovato.")}, status_code=404)
        xml = m.xml_it if lang == "it" else m.xml_en
        rank = (m.stats or {}).get("rank")
    finally:
        db.close()
    headers = {}
    if download:
        name = f"variante-{rank}" if rank else "variante"
        headers["Content-Disposition"] = f'attachment; filename="{name}-{lang}.bpmn"'
    return Response(xml, media_type="application/xml; charset=utf-8", headers=headers)


@router.post("/analysis/bpa/{model_id}/save")
async def bpa_save(request: Request, model_id: str):
    """Salva il modello nella lingua in cui e' stato modificato; l'altra lingua prende la stessa struttura con i
    nomi tradotti (solo quelli nuovi o cambiati passano dall'LLM)."""
    body = await request.json()
    workspace_id = str(body.get("workspace_id") or "")
    ui = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(ui, "Accesso negato.")}, status_code=403)
    lang = body.get("lang") if body.get("lang") in bpmn.LANGS else "it"
    other = "en" if lang == "it" else "it"
    xml = body.get("xml")
    try:
        bpmn.validate(xml)
    except bpmn.InvalidBpmn as exc:
        return JSONResponse({"error": t(ui, "Il modello non è un BPMN 2.0 valido: non salvato.") + f" ({exc})"}, status_code=400)
    db = SessionLocal()
    try:
        m = _model(db, model_id, workspace_id)
        if m is None:
            return JSONResponse({"error": t(ui, "Modello non trovato.")}, status_code=404)
        old_same, old_other = (m.xml_it, m.xml_en) if lang == "it" else (m.xml_en, m.xml_it)
    finally:
        db.close()
    cost, n_done = [0.0], [0]

    def tr(items):
        done, c = bpmn.llm_translate(items, other)
        cost[0] += c
        n_done[0] = len(done)
        return done
    other_xml, todo = await run_in_threadpool(bpmn.sync_languages, xml, old_same, old_other, tr)
    db = SessionLocal()
    try:
        m = _model(db, model_id, workspace_id)
        if m is None:
            return JSONResponse({"error": t(ui, "Modello non trovato.")}, status_code=404)
        if lang == "it":
            m.xml_it, m.xml_en = xml, other_xml
        else:
            m.xml_en, m.xml_it = xml, other_xml
        m.edited = True
        m.cost_usd = round((m.cost_usd or 0.0) + cost[0], 5)
        m.updated_by, m.updated_at = user.name, _now()
        db.add(m)
        db.commit()
        # untranslated: nomi nuovi o cambiati rimasti uguali nell'altra lingua (LLM non disponibile o senza risposta)
        return JSONResponse({"saved": True, "translated": n_done[0], "untranslated": len(todo) - n_done[0],
                             "cost_usd": round(cost[0], 5),
                             "updated_at": utc_iso(m.updated_at), "updated_by": m.updated_by})
    finally:
        db.close()


@router.post("/analysis/bpa/{model_id}/reset")
async def bpa_reset(request: Request, model_id: str):
    """Rifa il modello dalla variante (le modifiche si perdono)."""
    body = await request.json()
    workspace_id = str(body.get("workspace_id") or "")
    ui = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(ui, "Accesso negato.")}, status_code=403)
    db = SessionLocal()
    try:
        m = _model(db, model_id, workspace_id)
        if m is None:
            return JSONResponse({"error": t(ui, "Modello non trovato.")}, status_code=404)
        config, run, _ = _explorer_dataset(workspace_id, m.ingestion_config_id)
        ws = db.get(ProcessWorkspace, workspace_id)
        lead, steps, known = m.lead_type, m.steps, _known_labels(db, m.ingestion_config_id)
    finally:
        db.close()
    ocel = run.ocel_file_path if run is not None and Path(run.ocel_file_path).exists() else None
    built = await run_in_threadpool(_build, ws.process_name, lead, steps, ocel, known)
    db = SessionLocal()
    try:
        m = _model(db, model_id, workspace_id)
        m.labels, m.lanes = built["labels"], built["lanes"]
        m.xml_it, m.xml_en = built["xml"]["it"], built["xml"]["en"]
        m.edited = False
        m.cost_usd = round((m.cost_usd or 0.0) + built["cost_usd"], 5)
        m.updated_by, m.updated_at = user.name, _now()
        db.add(m)
        db.commit()
        return JSONResponse({"reset": True})
    finally:
        db.close()
