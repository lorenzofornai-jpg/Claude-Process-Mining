"""Modulo 2 - Analisi.

Pagina d'ingresso (verifica del ruolo Data Analyst, elenco dei dataset OCEL 2.0
pronti per il processo, prodotti dal Modulo 1) e Process Explorer: il grafo dei
flussi object-centric di un dataset (services/explorer.py).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import current_user, has_process_access
from app.i18n import get_lang, msg, setup_templates, t
from app.config import STATIC_VERSION
from app.db import SessionLocal
from app.models import AnalysisAlias, ExtractionRun, FieldMapping, IngestionConfig, ProcessWorkspace
from app.routers.assessment import load_assessment
from app.services import analysis_assistant, explain, explorer

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.globals["static_version"] = STATIC_VERSION
setup_templates(templates)


def _require_analyst_access(request: Request, workspace_id: str):
    """Ritorna (user, None) se autorizzato al Modulo 2 (Analisi) di questo
    processo, altrimenti (None, redirect_o_403)."""
    user = current_user(request)
    if user is None:
        return None, RedirectResponse("/login", status_code=303)
    if not has_process_access(user, workspace_id, required_role="data_analyst"):
        return None, HTMLResponse(t(get_lang(request), "Accesso negato: non sei assegnato come Data Analyst a questo processo."),
            status_code=403,
        )
    return user, None


def available_datasets(db, workspace_id: str) -> list[IngestionConfig]:
    """Dataset pronti per l'analisi: promossi («Utilizza per l'analisi») e con almeno
    un caricamento di dati per questo processo."""
    return (
        db.query(IngestionConfig)
        .join(ExtractionRun, ExtractionRun.ingestion_config_id == IngestionConfig.id)
        .filter(ExtractionRun.workspace_id == workspace_id, IngestionConfig.status == "approved")
        .distinct()
        .all()
    )


@router.get("/analysis/dashboard", response_class=HTMLResponse)
def analysis_dashboard(request: Request, workspace_id: str):
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return denied

    db = SessionLocal()
    try:
        ws = db.get(ProcessWorkspace, workspace_id)
        configs = available_datasets(db, workspace_id)
        structures = []
        for config in configs:
            last_run = (
                db.query(ExtractionRun)
                .filter_by(workspace_id=workspace_id, ingestion_config_id=config.id)
                .order_by(ExtractionRun.started_at.desc())
                .first()
            )
            structures.append({"config": config, "last_run": last_run})
    finally:
        db.close()

    return templates.TemplateResponse(
        "analysis_dashboard.html",
        {
            "request": request, "user": user, "workspace_id": workspace_id,
            "process_name": ws.process_name, "structures": structures,
        },
    )


def _latest_run(db, workspace_id: str, config_id: str) -> ExtractionRun | None:
    return (
        db.query(ExtractionRun)
        .filter_by(workspace_id=workspace_id, ingestion_config_id=config_id)
        .order_by(ExtractionRun.started_at.desc())
        .first()
    )


def _explorer_dataset(workspace_id: str, config_id: str | None):
    """(dataset scelto, ultimo caricamento, tutti i dataset disponibili). Senza config_id: il primo disponibile."""
    db = SessionLocal()
    try:
        configs = available_datasets(db, workspace_id)
        config = next((c for c in configs if c.id == config_id), None) if config_id else (configs[0] if configs else None)
        run = _latest_run(db, workspace_id, config.id) if config else None
        return config, run, configs
    finally:
        db.close()


@router.get("/analysis/explorer", response_class=HTMLResponse)
def process_explorer(request: Request, workspace_id: str, config_id: str | None = None):
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return denied
    config, run, configs = _explorer_dataset(workspace_id, config_id)
    if config is None or run is None or not Path(run.ocel_file_path).exists():
        return HTMLResponse(t(get_lang(request), "Dataset non disponibile per l'analisi."), status_code=404)
    model = explorer.load_model(run.ocel_file_path)
    db = SessionLocal()
    try:
        ws = db.get(ProcessWorkspace, workspace_id)
    finally:
        db.close()
    return templates.TemplateResponse(
        "process_explorer.html",
        {
            "request": request, "user": user, "workspace_id": workspace_id, "process_name": ws.process_name,
            "config": config, "run": run, "configs": configs,
            "object_types": model.type_summary(), "default_types": model.default_types(),
            "aliases": _aliases(user.id, config.id),
            "event_count": model.event_count,
        },
    )


@router.get("/analysis/explorer/graph")
def process_explorer_graph(request: Request, workspace_id: str, config_id: str,
                           type: list[str] = Query(default=[]), act: list[str] = Query(default=[]),
                           acts: str = "top", top: int | None = None, paths: int = 100,
                           hub: str = "own"):
    """Grafo aggregato in JSON per i filtri scelti.

    type=...&type=...: tipi di oggetto. acts=top: le `top` attivita' piu' frequenti;
    acts=list: esattamente le attivita' act=...&act=... (anche nessuna). hub=own|all: per i tipi trasversali,
    solo i loro eventi propri oppure tutti gli eventi collegati.
    """
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(get_lang(request), "Accesso negato.")}, status_code=403)
    config, run, _ = _explorer_dataset(workspace_id, config_id)
    if config is None or run is None or not Path(run.ocel_file_path).exists():
        return JSONResponse({"error": t(get_lang(request), "Dataset non disponibile per l'analisi.")}, status_code=404)
    model = explorer.load_model(run.ocel_file_path)
    activities = list(act) if acts == "list" else None
    return JSONResponse(explorer.build_graph(model, list(type), activities, top, paths,
                                             "all" if hub == "all" else "own"))


# ---------- nomi personali e assistente dell'analisi ----------

ALIAS_KINDS = ("object_type", "activity")


def _aliases(user_id: str, config_id: str) -> dict:
    """{"object_type": {originale: nome}, "activity": {...}} scelti da questo utente per questo dataset."""
    out = {k: {} for k in ALIAS_KINDS}
    db = SessionLocal()
    try:
        for a in db.query(AnalysisAlias).filter_by(user_id=user_id, ingestion_config_id=config_id).all():
            if a.kind in out:
                out[a.kind][a.original] = a.alias
    finally:
        db.close()
    return out


def _known_names(model, kind: str) -> set[str]:
    if kind == "object_type":
        return set(model.types)
    return {act for (_, act) in model.events_by_type_act}


@router.post("/analysis/explorer/alias")
async def set_alias(request: Request):
    """Salva (o toglie, con alias vuoto) il nome personale di un tipo di oggetto o di un'attivita'."""
    body = await request.json()
    workspace_id, config_id = str(body.get("workspace_id") or ""), str(body.get("config_id") or "")
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(get_lang(request), "Accesso negato.")}, status_code=403)
    config, run, _ = _explorer_dataset(workspace_id, config_id)
    if config is None or run is None:
        return JSONResponse({"error": t(get_lang(request), "Dataset non disponibile per l'analisi.")}, status_code=404)
    kind, original = str(body.get("kind") or ""), str(body.get("original") or "")
    alias = " ".join(str(body.get("alias") or "").split())[:80]
    model = explorer.load_model(run.ocel_file_path)
    if kind not in ALIAS_KINDS or original not in _known_names(model, kind):
        return JSONResponse({"error": t(get_lang(request), "Nome non trovato nel dataset.")}, status_code=400)
    db = SessionLocal()
    try:
        row = db.query(AnalysisAlias).filter_by(user_id=user.id, ingestion_config_id=config.id,
                                                kind=kind, original=original).first()
        if not alias or alias == original:
            if row:
                db.delete(row)
        elif row:
            row.alias = alias
        else:
            db.add(AnalysisAlias(user_id=user.id, ingestion_config_id=config.id, kind=kind,
                                 original=original, alias=alias))
        db.commit()
    finally:
        db.close()
    return JSONResponse({"aliases": _aliases(user.id, config.id)})


@router.post("/analysis/explorer/assistant")
async def explorer_assistant(request: Request):
    """Domanda all'assistente. Con estimate=true non chiama Claude: solo il costo indicativo."""
    body = await request.json()
    workspace_id, config_id = str(body.get("workspace_id") or ""), str(body.get("config_id") or "")
    user, denied = _require_analyst_access(request, workspace_id)
    lang = get_lang(request)
    if denied:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    if not explain.available():
        return JSONResponse({"available": False, "error": t(
            lang, "Claude non è configurato su questo server (manca la chiave API): l'assistente non è disponibile.")})
    config, run, _ = _explorer_dataset(workspace_id, config_id)
    if config is None or run is None or not Path(run.ocel_file_path).exists():
        return JSONResponse({"error": t(lang, "Dataset non disponibile per l'analisi.")}, status_code=404)
    model = explorer.load_model(run.ocel_file_path)

    view = body.get("view") or {}
    acts = view.get("act") if view.get("acts") == "list" else None
    top = view.get("top")
    graph = explorer.build_graph(model, [str(x) for x in view.get("types") or []],
                                 [str(x) for x in acts] if acts is not None else None,
                                 int(top) if top not in (None, "") else None, int(view.get("paths") or 100),
                                 "all" if view.get("hub") == "all" else "own")
    db = SessionLocal()
    try:
        ws = db.get(ProcessWorkspace, workspace_id)
        rows = db.query(FieldMapping).filter_by(ingestion_config_id=config.id).all()
        mapping = analysis_assistant.mapping_summary(rows)
    finally:
        db.close()
    answers, _ = load_assessment(workspace_id)
    context = analysis_assistant.build_context(
        language=lang, process_name=ws.process_name, assessment=answers,
        dataset={"name": config.name, "version": config.current_version,
                 "objects": run.object_count, "events": run.event_count},
        model=model, graph=graph, mapping=mapping, aliases=_aliases(user.id, config.id),
        focus=str(body.get("focus") or "")[:300] or None,
    )
    history = [m for m in body.get("messages") or [] if isinstance(m, dict)]
    est = analysis_assistant.estimate(context, history)
    if body.get("estimate"):
        return JSONResponse({"available": True, **est})
    try:
        result = await run_in_threadpool(analysis_assistant.ask, context, history)
    except Exception as exc:
        print(f"Assistente dell'analisi non riuscito ({exc!r}).")
        return JSONResponse({"available": True, "error": t(lang, "La richiesta a Claude non è riuscita: riprova tra poco.")},
                            status_code=502)
    known = {k: _known_names(model, k) for k in ALIAS_KINDS}
    result["actions"] = [a for a in result.get("actions", [])
                         if a.get("kind") in ALIAS_KINDS and a.get("original") in known[a["kind"]]]
    if result.get("truncated"):
        result["answer"] = result["answer"].rstrip() + "…\n\n" + t(
            lang, "(Risposta interrotta perché troppo lunga: fai una domanda più precisa per avere il resto.)")
    return JSONResponse({"available": True, **result})
