"""Modulo 2 - Analisi > BPA (Business Process Analysis).

Dalla Process Overview (oggetto guida con gli oggetti collegati) una variante si invia alla BPA: diventa un modello
BPMN 2.0 (services/bpmn.py) in italiano e in inglese, da aprire a tutta pagina nell'editor (bpmn-js) e completare.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from app.config import STATIC_VERSION
from app.db import SessionLocal
from app.i18n import get_lang, setup_templates, t, utc_iso
from app.models import BpaAttributeDef, BpaModel, BpaOccurrence, BpaRelation, BpaSymbol, ProcessWorkspace
from app.routers.analysis import _explorer_dataset, _require_analyst_access
from app.auth import has_process_access
from app.services import bpa_repo, bpmn, explorer, ocel_filter, overlay

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
            "reference": bool(m.overlay_reference),
            "edited": bool(m.edited), "created_by": m.created_by, "created_at": utc_iso(m.created_at),
            "updated_by": m.updated_by, "updated_at": utc_iso(m.updated_at) if m.updated_at else None}


def _model(db, model_id: str, workspace_id: str) -> BpaModel | None:
    """Il modello, se e' di un dataset di questo processo."""
    m = db.get(BpaModel, model_id)
    if m is None:
        return None
    config, _, _ = _explorer_dataset(workspace_id, m.ingestion_config_id)
    return m if config is not None and config.id == m.ingestion_config_id else None


def _forget(db, model_id: str) -> None:
    db.query(BpaOccurrence).filter_by(model_id=model_id).delete(synchronize_session=False)
    db.query(BpaRelation).filter_by(model_id=model_id).delete(synchronize_session=False)
    db.flush()
    bpa_repo.drop_orphan_anonymous(db)


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
        m = BpaModel(ingestion_config_id=config.id, workspace_id=workspace_id, lead_type=lead, variant_key=key, steps=steps, stats=stats,
                     labels=built["labels"], lanes=built["lanes"], xml_it=built["xml"]["it"], xml_en=built["xml"]["en"],
                     cost_usd=built["cost_usd"], created_by_id=user.id, created_by=user.name)
        db.add(m)
        db.flush()
        bpa_repo.register_generated(db, m, user.name)   # i simboli entrano nell'archivio (stesso nome = stesso simbolo)
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
        _forget(db, m.id)   # i simboli restano nell'archivio, anche senza occorrenze
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
        "config_id": config_id, "model": model, "full_page": True,
        "focus": str(request.query_params.get("focus") or "")[:200] or None})


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
    headers = {"X-Loaded-At": _now().isoformat()}
    if download:
        name = f"variante-{rank}" if rank else "variante"
        headers["Content-Disposition"] = f'attachment; filename="{name}-{lang}.bpmn"'
    return Response(xml, media_type="application/xml; charset=utf-8", headers=headers)


@router.post("/analysis/bpa/{model_id}/save")
async def bpa_save(request: Request, model_id: str):
    """Salva il modello nella lingua in cui e' stato modificato e aggiorna l'archivio dei simboli (occorrenze,
    relazioni, nomi cambiati anche negli altri modelli). L'altra lingua prende la stessa struttura con i nomi
    tradotti: quelli dei simboli gia' nell'archivio, gli altri nuovi o cambiati dall'LLM.
    Nomi gia' usati da un altro simbolo dello stesso tipo e non decisi dall'utente: 409 con i conflitti."""
    body = await request.json()
    workspace_id = str(body.get("workspace_id") or "")
    ui = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(ui, "Accesso negato.")}, status_code=403)
    lang = body.get("lang") if body.get("lang") in bpmn.LANGS else "it"
    other = "en" if lang == "it" else "it"
    xml = body.get("xml")
    links = {str(k): v for k, v in (body.get("links") or {}).items() if isinstance(v, dict)}
    try:
        loaded_at = datetime.fromisoformat(str(body.get("loaded_at"))) if body.get("loaded_at") else None
    except ValueError:
        loaded_at = None
    try:
        bpmn.validate(xml)
    except bpmn.InvalidBpmn as exc:
        return JSONResponse({"error": t(ui, "Il modello non è un BPMN 2.0 valido: non salvato.") + f" ({exc})"}, status_code=400)
    db = SessionLocal()
    try:
        m = _model(db, model_id, workspace_id)
        if m is None:
            return JSONResponse({"error": t(ui, "Modello non trovato.")}, status_code=404)
        if not m.registered:
            bpa_repo.register_generated(db, m, user.name)
            db.commit()
        plan = bpa_repo.resolve(db, m, lang, xml, links, loaded_at)
        if plan["conflicts"]:
            return JSONResponse({"conflicts": [{**c, "existing": _symbol_brief(db, c["existing"], lang, user)}
                                               for c in plan["conflicts"]]}, status_code=409)
        known = bpa_repo.other_names(db, plan, lang)
        old_same, old_other = (m.xml_it, m.xml_en) if lang == "it" else (m.xml_en, m.xml_it)
    finally:
        db.close()
    xml = plan["xml"]
    cost, n_done = [0.0], [0]

    def tr(items):
        # nomi dei simboli gia' nell'archivio: quelli dell'altra lingua; gli altri dall'LLM
        done = {k: known[k.split(":")[0]] for k in items if k.endswith(":name") and k.split(":")[0] in known}
        rest = {k: v for k, v in items.items() if k not in done}
        got, c = bpmn.llm_translate(rest, other) if rest else ({}, 0.0)
        cost[0] += c
        n_done[0] = len(done) + len(got)
        return {**done, **got}
    other_xml, todo = await run_in_threadpool(bpmn.sync_languages, xml, old_same, old_other, tr)
    db = SessionLocal()
    try:
        m = _model(db, model_id, workspace_id)
        if m is None:
            return JSONResponse({"error": t(ui, "Modello non trovato.")}, status_code=404)
        res = bpa_repo.apply(db, m, lang, xml, other_xml, plan, user.name)
        xml, other_xml, others = res["xml"], res["other_xml"], res["others"]
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
                             "cost_usd": round(cost[0], 5), "refreshed": plan["refreshed"] or res["self_changed"],
                             "renamed_elsewhere": len(others),
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
        _forget(db, m.id)
        db.flush()
        bpa_repo.register_generated(db, m, user.name)
        m.cost_usd = round((m.cost_usd or 0.0) + built["cost_usd"], 5)
        m.updated_by, m.updated_at = user.name, _now()
        db.add(m)
        db.commit()
        return JSONResponse({"reset": True})
    finally:
        db.close()


# ---------- archivio dei simboli (unico per l'azienda) ----------

def _workspace_of(db, m: BpaModel) -> str | None:
    if m.workspace_id:
        return m.workspace_id
    from app.models import ProcessIngestionLink
    link = db.query(ProcessIngestionLink).filter_by(ingestion_config_id=m.ingestion_config_id).first()
    return link.workspace_id if link else None


def _model_label(db, m: BpaModel, lang: str, user=None, cache: dict | None = None) -> dict:
    """Come si chiama un modello BPMN nelle occorrenze: processo · Variante n · oggetto guida (+ link se accessibile)."""
    if cache is not None and m.id in cache:
        return cache[m.id]
    ws_id = _workspace_of(db, m)
    ws = db.get(ProcessWorkspace, ws_id) if ws_id else None
    rank = (m.stats or {}).get("rank")
    variant = t(lang, "Variante {n}", n=rank) if rank else t(lang, "Variante")
    admin = bool(user and user.is_admin)   # l'Amministratore vede tutti i BPMN (senza aprirli: non lavora sulle analisi)
    ok = admin or bool(user and ws_id and has_process_access(user, ws_id, required_role="data_analyst"))
    out = {"id": m.id, "process": ws.process_name if ws else "—", "label": f"{variant} · {m.lead_type}",
           "url": f"/analysis/bpa/{m.id}/edit?workspace_id={ws_id}" if ok and not admin else None, "visible": ok}
    if cache is not None:
        cache[m.id] = out
    return out


def _symbol_brief(db, sid: str, lang: str, user, cache: dict | None = None) -> dict | None:
    s = db.get(BpaSymbol, sid)
    if s is None:
        return None
    models, hidden, seen = [], 0, set()
    for o in db.query(BpaOccurrence).filter_by(symbol_id=sid).all():
        if o.model_id in seen:
            continue
        seen.add(o.model_id)
        m = db.get(BpaModel, o.model_id)
        if m is None:
            continue
        lab = _model_label(db, m, lang, user, cache)
        if lab["visible"]:
            models.append(lab)
        else:
            hidden += 1
    return {"id": s.id, "type": s.symbol_type, "type_label": bpa_repo.SYMBOL_TYPES[s.symbol_type][lang],
            "name": bpa_repo.name_of(s, lang), "name_other": bpa_repo.name_of(s, bpa_repo.other_lang(lang)),
            "display": bpa_repo.display_name(db, s, lang), "anonymous": bool(s.anonymous),
            "subtypes": bpa_repo.subtypes(db, s, lang), "models": models, "hidden_models": hidden}


def _def_json(d: BpaAttributeDef, lang: str) -> dict:
    return {"key": d.key, "type": d.data_type, "name": (d.name or {}).get(lang) or (d.name or {}).get("it") or d.key,
            "help": (d.help or {}).get(lang) or "", "required": bool(d.required), "translatable": bool(d.translatable),
            "options": [{"value": o.get("value"), "label": o.get(lang) or o.get("it") or o.get("value")}
                        for o in d.options or []]}


@router.get("/analysis/bpa/meta")
def bpa_meta(request: Request, workspace_id: str):
    """Tipi di simbolo, relazioni e attributi definiti dall'Amministratore, nella lingua dell'interfaccia."""
    lang = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    db = SessionLocal()
    try:
        defs = db.query(BpaAttributeDef).order_by(BpaAttributeDef.position).all()
        attrs = {k: [_def_json(d, lang) for d in defs if d.symbol_type == k] for k in bpa_repo.SYMBOL_TYPES}
    finally:
        db.close()
    return JSONResponse({"types": {k: v[lang] for k, v in bpa_repo.SYMBOL_TYPES.items()},
                         "tags": bpa_repo.TAG_TYPE,
                         "relations": {k: v[lang] for k, v in bpa_repo.RELATION_KINDS.items()},
                         "generic": sorted(bpa_repo.GENERIC_NAMES), "attributes": attrs})


@router.get("/analysis/bpa/{model_id}/symbols")
def bpa_model_symbols(request: Request, model_id: str, workspace_id: str):
    """Elementi del modello legati a simboli dell'archivio: {id elemento: {symbol, type, elsewhere}}."""
    lang = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    db = SessionLocal()
    try:
        m = _model(db, model_id, workspace_id)
        if m is None:
            return JSONResponse({"error": t(lang, "Modello non trovato.")}, status_code=404)
        if not m.registered:
            bpa_repo.register_generated(db, m, user.name)
            db.commit()
        out = {}
        for o in db.query(BpaOccurrence).filter_by(model_id=m.id).all():
            s = db.get(BpaSymbol, o.symbol_id)
            others = {x.model_id for x in db.query(BpaOccurrence).filter(BpaOccurrence.symbol_id == o.symbol_id,
                                                                       BpaOccurrence.model_id != m.id).all()}
            out[o.element_id] = {"symbol": o.symbol_id, "type": s.symbol_type if s else None, "elsewhere": len(others),
                                 "anonymous": bool(s and s.anonymous)}
        return JSONResponse({"elements": out})
    finally:
        db.close()


@router.get("/analysis/bpa/symbols/lookup")
def bpa_symbol_lookup(request: Request, workspace_id: str, type: str, name: str, lang: str = "it", exclude: str = ""):
    """Simbolo dell'archivio con questo tipo e nome (nella lingua del modello), se c'e'."""
    ui = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(ui, "Accesso negato.")}, status_code=403)
    lang = lang if lang in bpmn.LANGS else "it"
    if type not in bpa_repo.SYMBOL_TYPES or not bpa_repo.norm(name):
        return JSONResponse({"symbol": None})
    db = SessionLocal()
    try:
        s = bpa_repo.find_by_name(db, type, name, lang, exclude=exclude or None)
        return JSONResponse({"symbol": _symbol_brief(db, s.id, ui, user) if s else None})
    finally:
        db.close()


@router.get("/analysis/bpa/symbols/{symbol_id}")
def bpa_symbol_detail(request: Request, symbol_id: str, workspace_id: str, lang: str = "", model_id: str = ""):
    """Pannello delle proprieta': attributi (con i valori), occorrenze e relazioni del simbolo."""
    ui = get_lang(request)
    lang = lang if lang in bpmn.LANGS else ui
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(ui, "Accesso negato.")}, status_code=403)
    db = SessionLocal()
    try:
        d = symbol_detail(db, symbol_id, ui, lang, user, model_id)
        if d is None:
            return JSONResponse({"error": t(ui, "Oggetto non trovato.")}, status_code=404)
        return JSONResponse(d)
    finally:
        db.close()


def symbol_detail(db, symbol_id: str, ui: str, lang: str, user, model_id: str = "") -> dict | None:
    """Attributi (con i valori), occorrenze e relazioni di un simbolo, per il pannello delle proprieta'."""
    s = db.get(BpaSymbol, symbol_id)
    if s is None:
        return None
    cache = {}
    brief = _symbol_brief(db, s.id, ui, user, cache)
    brief["name"] = bpa_repo.name_of(s, lang)
    brief["name_other"] = bpa_repo.name_of(s, bpa_repo.other_lang(lang))
    defs = [d for d in db.query(BpaAttributeDef).order_by(BpaAttributeDef.position).all() if d.symbol_type == s.symbol_type]
    attrs = [{**_def_json(d, ui), "value": (s.attrs or {}).get(d.key)} for d in defs]
    occ = defaultdict(list)
    for o in db.query(BpaOccurrence).filter_by(symbol_id=s.id).all():
        occ[o.model_id].append(o.element_id)
    occurrences, hidden = [], 0
    for mid, eids in occ.items():
        m = db.get(BpaModel, mid)
        if m is None:
            continue
        lab = _model_label(db, m, ui, user, cache)
        if not lab["visible"]:
            hidden += 1
            continue
        occurrences.append({**lab, "elements": eids, "current": mid == model_id})
    occurrences.sort(key=lambda x: (not x["current"], x["process"], x["label"]))
    rels = {}
    for r in db.query(BpaRelation).filter((BpaRelation.source_id == s.id) | (BpaRelation.target_id == s.id)).all():
        out = r.source_id == s.id
        oid = r.target_id if out else r.source_id
        k = (r.kind, out, oid)
        if k not in rels:
            o2 = db.get(BpaSymbol, oid)
            if o2 is None:
                continue
            rels[k] = {"kind": r.kind, "label": bpa_repo.RELATION_KINDS[r.kind][ui][0 if out else 1],
                       "other": {"id": o2.id, "type": bpa_repo.SYMBOL_TYPES[o2.symbol_type][ui],
                                 "name": bpa_repo.display_name(db, o2, lang)}, "models": [], "current": False}
        m = db.get(BpaModel, r.model_id)
        if m is not None:
            lab = _model_label(db, m, ui, user, cache)
            if lab["visible"] and lab not in rels[k]["models"]:
                rels[k]["models"].append(lab)
            rels[k]["current"] = rels[k]["current"] or r.model_id == model_id
    relations = sorted(rels.values(), key=lambda x: (not x["current"], x["label"], x["other"]["name"].casefold()))
    return {"symbol": brief, "attributes": attrs, "occurrences": occurrences, "hidden_occurrences": hidden,
            "relations": relations, "created_by": s.created_by, "created_at": utc_iso(s.created_at),
            "updated_by": s.updated_by, "updated_at": utc_iso(s.updated_at) if s.updated_at else None}


@router.post("/analysis/bpa/symbols/{symbol_id}/attrs")
async def bpa_symbol_attrs(request: Request, symbol_id: str):
    """Cambia i valori degli attributi di un simbolo (valgono in tutti i BPMN in cui compare). Il nome si cambia nel
    diagramma (passa dal controllo dei nomi gia' usati)."""
    body = await request.json()
    workspace_id = str(body.get("workspace_id") or "")
    ui = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(ui, "Accesso negato.")}, status_code=403)
    lang = body.get("lang") if body.get("lang") in bpmn.LANGS else ui
    values = body.get("values") if isinstance(body.get("values"), dict) else {}
    db = SessionLocal()
    try:
        out = save_symbol_attrs(db, symbol_id, values, lang, ui, user.name)
        return JSONResponse(out or {"error": t(ui, "Oggetto non trovato.")}, status_code=200 if out else 404)
    finally:
        db.close()


def save_symbol_attrs(db, symbol_id: str, values: dict, lang: str, ui: str, user_name: str) -> dict | None:
    s = db.get(BpaSymbol, symbol_id)
    if s is None:
        return None
    defs = {d.key: d for d in db.query(BpaAttributeDef).filter_by(symbol_type=s.symbol_type).all()}
    attrs, errors = dict(s.attrs or {}), {}
    for k, v in values.items():
        d = defs.get(k)
        if d is None:
            continue
        val, err = bpa_repo.clean_value(d, v, lang, attrs.get(k))
        if err:
            errors[k] = t(ui, "Valore non valido.")
            continue
        if val is None:
            attrs.pop(k, None)
        else:
            attrs[k] = val
    s.attrs = attrs
    s.updated_by, s.updated_at = user_name, _now()
    db.add(s)
    db.commit()
    return {"saved": not errors, "errors": errors, "values": attrs,
            "updated_by": s.updated_by, "updated_at": utc_iso(s.updated_at)}



# ---------- Process Overlay: il BPMN di riferimento confrontato con i dati ----------

@router.post("/analysis/bpa/{model_id}/reference")
async def bpa_reference(request: Request, model_id: str):
    """Sceglie il BPMN di riferimento per il Process Overlay (uno solo per dataset)."""
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
        for x in db.query(BpaModel).filter_by(ingestion_config_id=m.ingestion_config_id).all():
            x.overlay_reference = x.id == m.id
            db.add(x)
        db.commit()
        return JSONResponse({"reference": m.id})
    finally:
        db.close()


def _reference(db, config_id: str) -> BpaModel | None:
    return db.query(BpaModel).filter_by(ingestion_config_id=config_id, overlay_reference=True).first()


@router.get("/analysis/overlay", response_class=HTMLResponse)
def overlay_page(request: Request, workspace_id: str, config_id: str | None = None):
    ctx, denied = _page_context(request, workspace_id, config_id)
    if denied:
        return denied
    from app.routers.analysis import _aliases, _plurals
    db = SessionLocal()
    try:
        rows = (db.query(BpaModel).filter_by(ingestion_config_id=ctx["config"].id)
                .order_by(BpaModel.lead_type, BpaModel.created_at).all())
        models = [_json(m) for m in rows]
        ref = next((m for m in models if m["reference"]), None)
    finally:
        db.close()
    full = explorer.load_model(ctx["run"].ocel_file_path)
    return templates.TemplateResponse("process_overlay.html", {
        **ctx, "models": models, "reference": ref,
        "plurals": _plurals(get_lang(request), full.types, _aliases(ctx["user"].id, ctx["config"].id))})


@router.get("/analysis/overlay/data")
def overlay_data(request: Request, workspace_id: str, config_id: str, var: list[int] = Query(default=[]),
                 vsel: int = 0, filters: str = ""):
    """BPMN di riferimento (nella lingua dell'interfaccia) e confronto con i casi delle varianti scelte."""
    from app.routers.analysis import _filters
    lang = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    config, run, _ = _explorer_dataset(workspace_id, config_id)
    if config is None or run is None or not Path(run.ocel_file_path).exists():
        return JSONResponse({"error": t(lang, "Dataset non disponibile per l'analisi.")}, status_code=404)
    db = SessionLocal()
    try:
        m = _reference(db, config.id)
        if m is None:
            return JSONResponse({"reference": None})
        ref = _json(m)
        xml, steps, labels, lead = (m.xml_it if lang == "it" else m.xml_en), m.steps, m.labels or {}, m.lead_type
    finally:
        db.close()
    sel = _filters(filters)
    model = explorer.load_model(run.ocel_file_path, sel)
    if lead not in model.sequences_by_type:
        return JSONResponse({"reference": ref, "xml": xml, "empty": True, "filters": sel})
    out = overlay.compute(model, lead, xml, steps, labels, lang, list(var) if vsel else None)
    return JSONResponse({"reference": ref, "xml": xml, "filters": sel, **out})
