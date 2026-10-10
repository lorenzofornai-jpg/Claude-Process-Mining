"""Modulo 2 - Analisi.

Pagina d'ingresso (verifica del ruolo Data Analyst, elenco dei dataset OCEL 2.0
pronti per il processo, prodotti dal Modulo 1) e Process Explorer: il grafo dei
flussi object-centric di un dataset (services/explorer.py).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import current_user, has_process_access
from app.i18n import get_lang, msg, render, setup_templates, t, ui_labels, utc_iso
from app.config import STATIC_VERSION
from app.db import SessionLocal
from app.models import AnalysisObjective, ControlTower, ExtractionRun, FieldMapping, IngestionConfig, ProcessWorkspace
from app.routers.assessment import load_assessment
from app.services import analysis_assistant, control_tower, explain, explorer, objectives, ocel_filter, overview

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
            "object_types": model.type_summary(), "default_types": model.default_types(business_focus(config)),
            "aliases": _aliases(user.id, config.id),
            "event_count": model.event_count,
        },
    )


@router.get("/analysis/explorer/graph")
def process_explorer_graph(request: Request, workspace_id: str, config_id: str,
                           type: list[str] = Query(default=[]), act: list[str] = Query(default=[]),
                           acts: str = "top", top: int | None = None, paths: int = 100,
                           hub: str = "own", vtype: str = "", var: list[int] = Query(default=[])):
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
                                             "all" if hub == "all" else "own", vtype or None, list(var)))


@router.get("/analysis/explorer/variants")
def process_explorer_variants(request: Request, workspace_id: str, config_id: str, type: str,
                              with_type: list[str] = Query(default=[])):
    """Varianti di un tipo di oggetto (sequenze di attivita'), dalla piu' frequente: per il filtro per varianti."""
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(get_lang(request), "Accesso negato.")}, status_code=403)
    config, run, _ = _explorer_dataset(workspace_id, config_id)
    if config is None or run is None or not Path(run.ocel_file_path).exists():
        return JSONResponse({"error": t(get_lang(request), "Dataset non disponibile per l'analisi.")}, status_code=404)
    model = explorer.load_model(run.ocel_file_path)
    others = [x for x in with_type if x in model.object_counts and x != type and not model.is_hub(x)]
    variants = model.variants(type, others) if type in model.object_counts else []
    return JSONResponse({"type": type, "with_types": others, "total": len(variants), "objects": sum(v["count"] for v in variants),
                         "variants": variants[:300]})


# ---------- assistente dell'analisi ----------
# I nomi di oggetti e attivita' arrivano corretti dall'ingestion (passo «Oggetti di business» e revisione del
# mapping): nell'analisi non si rinominano. I vecchi nomi personali (tabella analysis_alias) non si usano piu'.
ALIAS_KINDS = ("object_type", "activity")


def _aliases(user_id: str, config_id: str) -> dict:
    return {k: {} for k in ALIAS_KINDS}


@router.post("/analysis/assistant")
@router.post("/analysis/explorer/assistant")
async def analysis_assistant_ask(request: Request):
    """Domanda all'assistente (page: explorer | overview). Con estimate=true non chiama Claude: solo il costo
    indicativo."""
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
    view = body.get("view") or {}
    model = explorer.load_model(run.ocel_file_path)

    page = "overview" if body.get("page") == "overview" else "explorer"
    focus = str(body.get("focus") or "")[:300] or None
    if page == "overview":
        options = overview.lead_options(model)
        lead = view.get("lead") if view.get("lead") in options else (options[0] if options else None)
        scope = view.get("scope") if view.get("scope") in ("object", "related") else "object"
        page_view = analysis_assistant.overview_view(
            overview.build_overview(model, lead, scope, None,
                                    [int(x) for x in view["var"] if str(x).isdigit()] if isinstance(view.get("var"), list) else None)
            if lead else None, focus)
    else:
        acts = view.get("act") if view.get("acts") == "list" else None
        top = view.get("top")
        graph = explorer.build_graph(model, [str(x) for x in view.get("types") or []],
                                     [str(x) for x in acts] if acts is not None else None,
                                     int(top) if top not in (None, "") else None, int(view.get("paths") or 100),
                                     "all" if view.get("hub") == "all" else "own",
                                     view.get("vtype") or None, [int(x) for x in view.get("var") or [] if str(x).isdigit()])
        page_view = analysis_assistant.explorer_view(graph, focus)
    db = SessionLocal()
    try:
        ws = db.get(ProcessWorkspace, workspace_id)
        rows = db.query(FieldMapping).filter_by(ingestion_config_id=config.id).all()
        mapping = analysis_assistant.mapping_summary(rows)
        if config.business_objects:
            # perche' questi oggetti: scelti prima del mapping a partire dagli obiettivi
            mapping["business_objects"] = [
                {"name": o["name"], "table": o["table"], "role": o["role"], "included": o["include"],
                 "why": render(lang, o.get("why")), **({"rows_where": o["filter"]} if o.get("filter") else {})}
                for o in config.business_objects]
    finally:
        db.close()
    answers, _ = load_assessment(workspace_id)
    context = analysis_assistant.build_context(
        language=lang, process_name=ws.process_name, assessment=answers,
        dataset={"name": config.name, "version": config.current_version,
                 "objects": run.object_count, "events": run.event_count},
        model=model, mapping=mapping, page=page, view=page_view,
        ui_labels=ui_labels(lang, analysis_assistant.UI_LABELS),
        objective=None,   # l'obiettivo si verifica nella preparazione del dataset, non si salva
    )
    history = [m for m in body.get("messages") or [] if isinstance(m, dict)]
    est = analysis_assistant.estimate(context, history)
    if body.get("estimate"):
        return JSONResponse({"available": True, **est})
    try:
        # il dataset (con gli stessi filtri della pagina) come tabelle da interrogare in sola lettura
        data = ocel_filter.tables(ocel_filter.load_raw(run.ocel_file_path))
        result = await run_in_threadpool(analysis_assistant.ask, context, history, data)
    except Exception as exc:
        print(f"Assistente dell'analisi non riuscito ({exc!r}).")
        return JSONResponse({"available": True, "error": t(lang, "La richiesta a Claude non è riuscita: riprova tra poco.")},
                            status_code=502)
    if result.get("truncated"):
        result["answer"] = result["answer"].rstrip() + "…\n\n" + t(
            lang, "(Risposta interrotta perché troppo lunga: fai una domanda più precisa per avere il resto.)")
    return JSONResponse({"available": True, **result})


# ---------- Process Overview ----------

@router.get("/analysis/overview", response_class=HTMLResponse)
def process_overview(request: Request, workspace_id: str, config_id: str | None = None):
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return denied
    config, run, configs = _explorer_dataset(workspace_id, config_id)
    if config is None or run is None or not Path(run.ocel_file_path).exists():
        return HTMLResponse(t(get_lang(request), "Dataset non disponibile per l'analisi."), status_code=404)
    model = explorer.load_model(run.ocel_file_path)
    answers, _ = load_assessment(workspace_id)
    db = SessionLocal()
    try:
        ws = db.get(ProcessWorkspace, workspace_id)
    finally:
        db.close()
    return templates.TemplateResponse(
        "process_overview.html",
        {
            "request": request, "user": user, "workspace_id": workspace_id, "process_name": ws.process_name,
            "config": config, "run": run, "configs": configs,
            "object_types": model.type_summary(), "aliases": _aliases(user.id, config.id),
            "plurals": _plurals(get_lang(request), model.types, _aliases(user.id, config.id)),
            "default_lead": overview.default_lead(model, with_business_lead(answers, config.id).get("main_object")),
        },
    )


@router.get("/analysis/overview/data")
def process_overview_data(request: Request, workspace_id: str, config_id: str, lead: str = "", scope: str = "objective",
                          var: list[int] = Query(default=[]), vsel: int = 0):
    """Volumi, tempi e varianti visti dall'oggetto guida `lead`; scope=object|related per i tempi."""
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(get_lang(request), "Accesso negato.")}, status_code=403)
    config, run, _ = _explorer_dataset(workspace_id, config_id)
    if config is None or run is None or not Path(run.ocel_file_path).exists():
        return JSONResponse({"error": t(get_lang(request), "Dataset non disponibile per l'analisi.")}, status_code=404)
    model = explorer.load_model(run.ocel_file_path)
    if lead not in overview.lead_options(model):
        lead = overview.default_lead(model)
    if lead is None:
        return JSONResponse({"error": t(get_lang(request), "Nessun tipo di oggetto con eventi propri da analizzare.")},
                            status_code=404)
    # l'obiettivo misurabile si verifica nella preparazione del dataset; nella Overview i tempi sono dell'oggetto
    # o con gli oggetti collegati
    scope = scope if scope in ("object", "related") else "object"
    # vsel=1: var sono le varianti spuntate (anche nessuna); senza vsel tutte
    return JSONResponse(overview.build_overview(model, lead, scope, None, list(var) if vsel else None))


# ---------- obiettivo misurabile (pagina Risultato del Data Engineer e Process Overview) ----------

def _require_either_role(request: Request, workspace_id: str):
    user = current_user(request)
    if user is None or not (has_process_access(user, workspace_id, required_role="data_engineer")
                            or has_process_access(user, workspace_id, required_role="data_analyst")):
        return None
    return user


def _config_model(workspace_id: str, config_id: str):
    """Ultimo caricamento del dataset (anche bozza) e il suo modello, se appartiene al processo."""
    db = SessionLocal()
    try:
        run = _latest_run(db, workspace_id, config_id)
    finally:
        db.close()
    if run is None or not Path(run.ocel_file_path).exists():
        return None, None
    return run, explorer.load_model(run.ocel_file_path)


def _plurals(lang: str, types, aliases: dict) -> dict:
    """Plurale dei nomi dei tipi di oggetto (originali e rinominati) nella lingua attiva."""
    from app.services.nouns import plural
    names = set(types) | set((aliases or {}).get("object_type", {}).values())
    return {n: plural(n, lang) for n in names}


def business_focus(config) -> list[str]:
    """Oggetto guida e altri oggetti inclusi prima del mapping, in quest'ordine (selezione iniziale)."""
    objs = [o for o in (getattr(config, "business_objects", None) or []) if o.get("include")]
    return ([o["name"] for o in objs if o.get("role") == "lead"]
            + [o["name"] for o in objs if o.get("role") != "lead"])


def business_lead(config_id: str) -> str | None:
    """L'oggetto guida confermato tra gli oggetti di business prima del mapping (se il passo e' stato fatto)."""
    from app.services import business_objects
    db = SessionLocal()
    try:
        config = db.get(IngestionConfig, config_id)
        return business_objects.lead(config.business_objects or []) if config else None
    finally:
        db.close()


def with_business_lead(answers: dict, config_id: str) -> dict:
    """Risposte dell'assessment con l'oggetto guida confermato come oggetto principale: la Process Overview
    e l'obiettivo misurabile partono da li'."""
    lead = business_lead(config_id)
    return {**answers, "main_object": lead} if lead else answers


def saved_objective(config_id: str) -> dict | None:
    db = SessionLocal()
    try:
        o = db.query(AnalysisObjective).filter_by(ingestion_config_id=config_id).first()
        if o is None:
            return None
        return {"object_type": o.object_type, "filter_attribute": o.filter_attribute, "filter_values": o.filter_values,
                "start_activity": o.start_activity, "end_activity": o.end_activity}
    finally:
        db.close()


def _clean_binding(raw: dict) -> dict:
    fa = (raw.get("filter_attribute") or "").strip() or None
    fv = [str(v) for v in raw.get("filter_values") or []] if fa else None
    return {"object_type": str(raw.get("object_type") or ""), "filter_attribute": fa, "filter_values": fv or None if fa else None,
            "start_activity": str(raw.get("start_activity") or ""), "end_activity": str(raw.get("end_activity") or "")}


@router.get("/analysis/objective")
def objective_get(request: Request, workspace_id: str, config_id: str):
    """Scelte possibili, obiettivo salvato (o proposto dall'assessment) e suo controllo."""
    lang = get_lang(request)
    if _require_either_role(request, workspace_id) is None:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    run, model = _config_model(workspace_id, config_id)
    if model is None:
        return JSONResponse({"error": t(lang, "Dataset non disponibile per l'analisi.")}, status_code=404)
    answers, _ = load_assessment(workspace_id)
    binding = saved_objective(config_id)
    saved = binding is not None
    if binding is None:
        binding = objectives.propose(model, with_business_lead(answers, config_id))
    return JSONResponse({
        "options": objectives.options(model), "binding": binding, "saved": saved,
        "goal": objectives.goal_texts(answers),
        "check": objectives.rendered(lang, objectives.check(model, binding)) if binding else None,
    })


@router.post("/analysis/objective/check")
async def objective_check(request: Request):
    body = await request.json()
    workspace_id, config_id = str(body.get("workspace_id") or ""), str(body.get("config_id") or "")
    lang = get_lang(request)
    if _require_either_role(request, workspace_id) is None:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    run, model = _config_model(workspace_id, config_id)
    if model is None:
        return JSONResponse({"error": t(lang, "Dataset non disponibile per l'analisi.")}, status_code=404)
    return JSONResponse(objectives.rendered(lang, objectives.check(model, _clean_binding(body.get("binding") or {}))))


@router.post("/analysis/objective/save")
async def objective_save(request: Request):
    body = await request.json()
    workspace_id, config_id = str(body.get("workspace_id") or ""), str(body.get("config_id") or "")
    lang = get_lang(request)
    user = _require_either_role(request, workspace_id)
    if user is None:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    run, model = _config_model(workspace_id, config_id)
    if model is None:
        return JSONResponse({"error": t(lang, "Dataset non disponibile per l'analisi.")}, status_code=404)
    b = _clean_binding(body.get("binding") or {})
    acts = {a for (tt, a) in model.events_by_type_act if tt == b["object_type"]}
    if b["object_type"] not in model.types or b["start_activity"] not in acts or b["end_activity"] not in acts:
        return JSONResponse({"error": t(lang, "Scegli il tipo di oggetto e le attività di inizio e di fine.")}, status_code=400)
    db = SessionLocal()
    try:
        row = db.query(AnalysisObjective).filter_by(ingestion_config_id=config_id).first() or AnalysisObjective(
            ingestion_config_id=config_id)
        row.object_type, row.filter_attribute, row.filter_values = b["object_type"], b["filter_attribute"], b["filter_values"]
        row.start_activity, row.end_activity = b["start_activity"], b["end_activity"]
        row.updated_by, row.updated_at = user.name, datetime.now(timezone.utc).replace(tzinfo=None)
        db.add(row)
        db.commit()
    finally:
        db.close()
    return JSONResponse({"saved": True})


# ---------- Control Tower ----------
# Le domande di business dell'assessment tradotte in KPI (services/control_tower.py): Claude propone le definizioni
# una volta per dataset, il Data Analyst le rivede e le approva, l'app calcola i valori.

def _tower_row(db, config_id: str):
    return db.query(ControlTower).filter_by(ingestion_config_id=config_id).first()


def _tower_info(row, lang: str) -> dict | None:
    if row is None:
        return None
    return {"status": row.status, "language": row.language, "cost_usd": row.cost_usd,
            "created_by": row.created_by, "created_at": utc_iso(row.created_at),
            "updated_by": row.updated_by, "updated_at": utc_iso(row.updated_at),
            "approved_by": row.approved_by, "approved_at": utc_iso(row.approved_at) if row.approved_at else None}


@router.get("/analysis/control-tower", response_class=HTMLResponse)
def control_tower_page(request: Request, workspace_id: str, config_id: str | None = None):
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return denied
    config, run, configs = _explorer_dataset(workspace_id, config_id)
    if config is None or run is None or not Path(run.ocel_file_path).exists():
        return HTMLResponse(t(get_lang(request), "Dataset non disponibile per l'analisi."), status_code=404)
    model = explorer.load_model(run.ocel_file_path)
    answers, _ = load_assessment(workspace_id)
    db = SessionLocal()
    try:
        ws = db.get(ProcessWorkspace, workspace_id)
    finally:
        db.close()
    lead = overview.default_lead(model, with_business_lead(answers, config.id).get("main_object"))
    return templates.TemplateResponse(
        "control_tower.html",
        {
            "request": request, "user": user, "workspace_id": workspace_id, "process_name": ws.process_name,
            "config": config, "run": run, "lead": lead,
            "questions": (answers.get("key_questions") or "").strip(),
            "plurals": _plurals(get_lang(request), model.types, {}),
            "claude": explain.available(),
        },
    )


def _tower_payload(request: Request, model, spec: dict | None, row, period: str, lang: str, lead: str | None) -> dict:
    out = {"info": _tower_info(row, lang), "catalog": control_tower.catalog(model, lead), "spec": None}
    if spec:
        clean, issues = control_tower.validate(spec, model)
        values = control_tower.compute(clean, model, period)
        # problemi (dati mancanti) gia' tradotti
        for k in values["kpis"].values():
            if k.get("issues"):
                k["issues"] = [render(lang, x) for x in k["issues"]]
        for s in values["sections"]:
            for c in s["charts"]:
                if c.get("issues"):
                    c["issues"] = [render(lang, x) for x in c["issues"]]
        for s in clean["sections"]:
            for c in s["charts"]:
                c.pop("issues", None)
        out.update(spec=clean, values=values)
    return out


@router.api_route("/analysis/control-tower/data", methods=["GET", "POST"])
async def control_tower_data(request: Request):
    """Definizioni salvate, valori calcolati per il periodo e catalogo del dataset (per l'editor). In POST con
    "spec": anteprima di definizioni non ancora salvate."""
    body = await request.json() if request.method == "POST" else dict(request.query_params)
    workspace_id, config_id = str(body.get("workspace_id") or ""), str(body.get("config_id") or "")
    lang = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    config, run, _ = _explorer_dataset(workspace_id, config_id)
    if config is None or run is None or not Path(run.ocel_file_path).exists():
        return JSONResponse({"error": t(lang, "Dataset non disponibile per l'analisi.")}, status_code=404)
    model = explorer.load_model(run.ocel_file_path)
    answers, _ = load_assessment(workspace_id)
    lead = overview.default_lead(model, with_business_lead(answers, config.id).get("main_object"))
    db = SessionLocal()
    try:
        row = _tower_row(db, config.id)
    finally:
        db.close()
    spec = body.get("spec") if isinstance(body.get("spec"), dict) else (row.spec if row else None)
    period = str(body.get("period") or "12")
    return JSONResponse(await run_in_threadpool(_tower_payload, request, model, spec, row, period, lang, lead))


@router.post("/analysis/control-tower/propose")
async def control_tower_propose(request: Request):
    """Claude propone le definizioni (o le modifica secondo "instruction"). Con estimate=true solo il costo."""
    body = await request.json()
    workspace_id, config_id = str(body.get("workspace_id") or ""), str(body.get("config_id") or "")
    lang = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    if not explain.available():
        return JSONResponse({"available": False, "error": t(
            lang, "Claude non è configurato su questo server (manca la chiave API): l'assistente non è disponibile.")})
    config, run, _ = _explorer_dataset(workspace_id, config_id)
    if config is None or run is None or not Path(run.ocel_file_path).exists():
        return JSONResponse({"error": t(lang, "Dataset non disponibile per l'analisi.")}, status_code=404)
    model = explorer.load_model(run.ocel_file_path)
    answers, _ = load_assessment(workspace_id)
    lead = overview.default_lead(model, with_business_lead(answers, config.id).get("main_object"))
    db = SessionLocal()
    try:
        ws = db.get(ProcessWorkspace, workspace_id)
        row = _tower_row(db, config.id)
        current = row.spec if row else None
    finally:
        db.close()
    instruction = str(body.get("instruction") or "").strip()
    revise = bool(instruction) and current is not None
    payload = control_tower.build_payload(
        language=lang, process_name=ws.process_name, assessment=answers,
        cat=control_tower.catalog(model, lead), library=control_tower.library_hints(ws.process_name, ws.process_type),
        current_spec=current if revise else None, instruction=instruction if revise else None)
    est = control_tower.estimate(payload)
    if body.get("estimate"):
        return JSONResponse({"available": True, **est})
    try:
        result = await run_in_threadpool(control_tower.propose, payload)
    except Exception as exc:
        print(f"Control Tower: proposta non riuscita ({exc!r}).")
        return JSONResponse({"available": True, "error": t(lang, "La richiesta a Claude non è riuscita: riprova tra poco.")},
                            status_code=502)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db = SessionLocal()
    try:
        row = _tower_row(db, config.id) or ControlTower(ingestion_config_id=config.id, created_by=user.name, created_at=now)
        row.spec, row.language, row.status = result["spec"], lang, "draft"
        row.cost_usd = round((row.cost_usd or 0.0) + result["cost_usd"], 4)
        row.updated_by, row.updated_at, row.approved_by, row.approved_at = user.name, now, None, None
        db.add(row)
        db.commit()
    finally:
        db.close()
    return JSONResponse({"available": True, "cost_usd": result["cost_usd"]})


@router.post("/analysis/control-tower/save")
async def control_tower_save(request: Request):
    """Salva le definizioni modificate dal Data Analyst; approve=true le approva (senza: tornano da approvare)."""
    body = await request.json()
    workspace_id, config_id = str(body.get("workspace_id") or ""), str(body.get("config_id") or "")
    lang = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    config, run, _ = _explorer_dataset(workspace_id, config_id)
    if config is None or run is None or not Path(run.ocel_file_path).exists():
        return JSONResponse({"error": t(lang, "Dataset non disponibile per l'analisi.")}, status_code=404)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db = SessionLocal()
    try:
        row = _tower_row(db, config.id)
        spec = body.get("spec")
        if row is None and not isinstance(spec, dict):
            return JSONResponse({"error": t(lang, "Non ci sono definizioni da salvare.")}, status_code=400)
        if row is None:
            row = ControlTower(ingestion_config_id=config.id, created_by=user.name, created_at=now, language=lang)
        if isinstance(spec, dict):
            model = explorer.load_model(run.ocel_file_path)
            clean, _ = control_tower.validate(control_tower.normalize(spec), model)
            for s in clean["sections"]:
                for c in s["charts"]:
                    c.pop("issues", None)
            if clean != row.spec:
                row.spec, row.updated_by, row.updated_at = clean, user.name, now
                row.status, row.approved_by, row.approved_at = "draft", None, None
        if body.get("approve"):
            row.status, row.approved_by, row.approved_at = "approved", user.name, now
        db.add(row)
        db.commit()
        return JSONResponse({"saved": True, "status": row.status})
    finally:
        db.close()
