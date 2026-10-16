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
from app.models import (AnalysisObjective, AnalysisView, ControlTower, ExtractionRun, FieldMapping, IngestionConfig,
                        ProcessWorkspace, RcaAnalysis, RcaText)
from app.routers.assessment import load_assessment
from app.services import (analysis_assistant, control_tower, control_tower_assistant, explain, explorer, objectives,
                          ocel_filter, overview, rca)

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
            "plurals": _plurals(get_lang(request), model.types, _aliases(user.id, config.id)),
        },
    )


@router.get("/analysis/explorer/graph")
def process_explorer_graph(request: Request, workspace_id: str, config_id: str,
                           type: list[str] = Query(default=[]), act: list[str] = Query(default=[]),
                           acts: str = "top", top: int | None = None, paths: int = 100,
                           hub: str = "own", vtype: str = "", var: list[int] = Query(default=[]), filters: str = ""):
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
    model = explorer.load_model(run.ocel_file_path, _filters(filters))
    activities = list(act) if acts == "list" else None
    return JSONResponse(explorer.build_graph(model, list(type), activities, top, paths,
                                             "all" if hub == "all" else "own", vtype or None, list(var)))


@router.get("/analysis/explorer/variants")
def process_explorer_variants(request: Request, workspace_id: str, config_id: str, type: str,
                              with_type: list[str] = Query(default=[]), filters: str = ""):
    """Varianti di un tipo di oggetto (sequenze di attivita'), dalla piu' frequente: per il filtro per varianti."""
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(get_lang(request), "Accesso negato.")}, status_code=403)
    config, run, _ = _explorer_dataset(workspace_id, config_id)
    if config is None or run is None or not Path(run.ocel_file_path).exists():
        return JSONResponse({"error": t(get_lang(request), "Dataset non disponibile per l'analisi.")}, status_code=404)
    model = explorer.load_model(run.ocel_file_path, _filters(filters))
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


def _overlay_view(config_id: str, model, view: dict, lang: str, focus: str | None) -> dict:
    """Process Overlay per l'assistente: il confronto che l'utente vede (BPMN di riferimento, varianti spuntate)."""
    from app.routers.bpa import _json, _known_labels_with, _repo_choice, reference_of
    from app.services import overlay
    db = SessionLocal()
    try:
        m, lead = reference_of(db, config_id)
        if m is None:
            return analysis_assistant.overlay_view(None, None, None, focus)
        ref = _json(m, m.id)
        repo = _repo_choice(db, m, lang) if m.stage == "repository" else None
        xml, steps, labels = (m.xml_it if lang == "it" else m.xml_en), m.steps, _known_labels_with(db, config_id, m)
    finally:
        db.close()
    reference = {"lead_object": lead, "from_variant": (ref["stats"] or {}).get("rank"),
                 "variant_steps": [x["activity"] + (" ×" if x.get("repeated") else "") for x in steps or []],
                 "edited": ref["edited"]}
    if repo:   # modello del repository BPA (la tassonomia dei processi), non una variante di questo dataset
        reference.update({"from_bpa_repository": True, "repository_process": repo["process"],
                          "model_name": repo["title"]})
    if lead not in model.sequences_by_type:
        return analysis_assistant.overlay_view(None, None, reference, focus)
    var = view.get("var")
    picked = [int(x) for x in var if str(x).isdigit()] if isinstance(var, list) else None
    out = overlay.compute(model, lead, xml, steps, labels, lang, picked)
    return analysis_assistant.overlay_view(out, overlay._model_graph(xml), reference, focus)


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
            lang, "L'LLM non è configurato su questo server (manca la chiave API): l'assistente non è disponibile.")})
    config, run, _ = _explorer_dataset(workspace_id, config_id)
    if config is None or run is None or not Path(run.ocel_file_path).exists():
        return JSONResponse({"error": t(lang, "Dataset non disponibile per l'analisi.")}, status_code=404)
    view = body.get("view") or {}
    sel = _filters(body.get("filters"))
    model = explorer.load_model(run.ocel_file_path, sel)

    page = body.get("page") if body.get("page") in ("overview", "overlay") else "explorer"
    focus = str(body.get("focus") or "")[:300] or None
    if page == "overlay":
        page_view = _overlay_view(config.id, model, view, lang, focus)
    elif page == "overview":
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
    if isinstance(page_view, dict):
        page_view["case_filters"] = _filter_labels(body)
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
        data = await run_in_threadpool(_filtered_tables, run.ocel_file_path, sel)
        result = await run_in_threadpool(analysis_assistant.ask, context, history, data)
    except Exception as exc:
        print(f"Assistente dell'analisi non riuscito ({exc!r}).")
        return JSONResponse({"available": True, "error": t(lang, "La richiesta all'LLM non è riuscita: riprova tra poco.")},
                            status_code=502)
    if result.get("truncated"):
        result["answer"] = result["answer"].rstrip() + "…\n\n" + t(
            lang, "(Risposta interrotta perché troppo lunga: fai una domanda più precisa per avere il resto.)")
    return JSONResponse({"available": True, **result})


# ---------- filtri sui casi, condivisi tra le analisi dello stesso dataset ----------

def _filters(raw) -> list[dict]:
    """Filtri ricevuti dal browser (JSON nella query o lista nel corpo), normalizzati."""
    import json as _json
    if isinstance(raw, str):
        try:
            raw = _json.loads(raw) if raw.strip() else []
        except ValueError:
            raw = []
    return ocel_filter.clean(raw if isinstance(raw, list) else [])


def _filtered_tables(path, sel: list[dict]) -> dict:
    """Il dataset (ridotto ai casi dei filtri, se ce ne sono) come tabelle per le interrogazioni dell'assistente."""
    raw = ocel_filter.load_raw(path)
    if not sel:
        return ocel_filter.tables(raw)
    full = explorer.load_model(path)
    hubs = {t for t in full.types if full.is_hub(t)}
    return ocel_filter.tables(ocel_filter.apply(raw, explorer.resolve(full, sel), hubs))


def _filter_labels(body: dict) -> list[str]:
    """Come l'utente vede i filtri attivi (testo della barra dei filtri), per il contesto dell'assistente."""
    return [str(x)[:300] for x in (body.get("filter_labels") or []) if x][:20]


def _without(filters: list[dict], kind: str, type_: str) -> list[dict]:
    """I filtri senza quelli di un grafico (il grafico da cui parte la selezione mostra tutti i valori)."""
    return [f for f in filters if not (f["kind"] == kind and f.get("type") == type_)]


def _view_json(v: AnalysisView, user) -> dict:
    return {"id": v.id, "name": v.name, "filters": v.filters or [], "created_by": v.created_by,
            "created_at": utc_iso(v.created_at), "mine": v.created_by_id == user.id}


@router.get("/analysis/views")
def views_list(request: Request, workspace_id: str, config_id: str):
    """Viste salvate del dataset (filtri con un nome), visibili a tutti i Data Analyst del processo."""
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(get_lang(request), "Accesso negato.")}, status_code=403)
    config, _, _ = _explorer_dataset(workspace_id, config_id)
    if config is None:
        return JSONResponse({"views": []})
    db = SessionLocal()
    try:
        views = db.query(AnalysisView).filter_by(ingestion_config_id=config.id).order_by(AnalysisView.name).all()
        return JSONResponse({"views": [_view_json(v, user) for v in views]})
    finally:
        db.close()


@router.post("/analysis/views")
async def views_save(request: Request):
    """Salva i filtri attivi come vista (stesso nome: la sostituisce, se e' dell'utente)."""
    body = await request.json()
    workspace_id, config_id = str(body.get("workspace_id") or ""), str(body.get("config_id") or "")
    lang = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    config, _, _ = _explorer_dataset(workspace_id, config_id)
    name = str(body.get("name") or "").strip()[:200]
    filters = _filters(body.get("filters"))
    # le etichette dei filtri restano (servono alla barra dei filtri), come le ha mandate il browser
    raw = [f for f in body.get("filters") or [] if isinstance(f, dict) and ocel_filter._small_json(f)]
    if config is None or not name or not filters:
        return JSONResponse({"error": t(lang, "Dai un nome alla vista e scegli almeno un filtro.")}, status_code=400)
    db = SessionLocal()
    try:
        v = db.query(AnalysisView).filter_by(ingestion_config_id=config.id, name=name).first()
        if v is not None and v.created_by_id != user.id:
            return JSONResponse({"error": t(lang, "Esiste già una vista con questo nome, creata da un altro utente.")},
                                status_code=400)
        if v is None:
            v = AnalysisView(ingestion_config_id=config.id, name=name, created_by_id=user.id, created_by=user.name)
        v.filters = raw[:30]
        v.created_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.add(v)
        db.commit()
        return JSONResponse({"saved": True, "view": _view_json(v, user)})
    finally:
        db.close()


@router.post("/analysis/views/{view_id}/delete")
async def views_delete(request: Request, view_id: str):
    body = await request.json()
    workspace_id = str(body.get("workspace_id") or "")
    lang = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    db = SessionLocal()
    try:
        v = db.get(AnalysisView, view_id)
        config, _, _ = _explorer_dataset(workspace_id, v.ingestion_config_id) if v else (None, None, None)
        if v is None or config is None or config.id != v.ingestion_config_id:
            return JSONResponse({"error": t(lang, "Vista non trovata.")}, status_code=404)
        if v.created_by_id != user.id:
            return JSONResponse({"error": t(lang, "Solo chi ha creato la vista può toglierla.")}, status_code=403)
        db.delete(v)
        db.commit()
        return JSONResponse({"deleted": True})
    finally:
        db.close()


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
                          var: list[int] = Query(default=[]), vsel: int = 0, filters: str = ""):
    """Volumi, tempi e varianti visti dall'oggetto guida `lead`; scope=object|related per i tempi."""
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(get_lang(request), "Accesso negato.")}, status_code=403)
    config, run, _ = _explorer_dataset(workspace_id, config_id)
    if config is None or run is None or not Path(run.ocel_file_path).exists():
        return JSONResponse({"error": t(get_lang(request), "Dataset non disponibile per l'analisi.")}, status_code=404)
    sel = _filters(filters)
    model = explorer.load_model(run.ocel_file_path, sel)
    full = explorer.load_model(run.ocel_file_path)
    if lead not in overview.lead_options(full):   # il tipo guida si sceglie sul dataset intero, non su quello filtrato
        lead = overview.default_lead(full)
    if lead is None:
        return JSONResponse({"error": t(get_lang(request), "Nessun tipo di oggetto con eventi propri da analizzare.")},
                            status_code=404)
    # l'obiettivo misurabile si verifica nella preparazione del dataset; nella Overview i tempi sono dell'oggetto
    # o con gli oggetti collegati
    scope = scope if scope in ("object", "related") else "object"
    # vsel=1: var sono le varianti spuntate (anche nessuna); senza vsel tutte
    if lead not in overview.lead_options(model):   # nessun oggetto del tipo guida nei casi filtrati
        return JSONResponse({"empty": True, "lead": lead, "lead_options": overview.lead_options(full),
                             "scope": scope, "filters": sel})
    out = overview.build_overview(model, lead, scope, None, list(var) if vsel else None)
    # i grafici da cui parte una selezione mostrano tutti i valori (con gli altri filtri), i valori scelti evidenziati
    month_f = _without(sel, "start_month", lead)
    if month_f != sel:
        m2 = explorer.load_model(run.ocel_file_path, month_f)
        if lead in overview.lead_options(m2):
            out["volumes"]["months"] = overview.build_overview(m2, lead, scope, None, None)["volumes"]["months"]
    dur_f = [f for f in sel if not (f["kind"] == "duration" and f.get("type") == lead and f.get("scope") == scope)]
    if dur_f != sel:
        m3 = explorer.load_model(run.ocel_file_path, dur_f)
        if lead in overview.lead_options(m3):
            out["throughput"]["histogram"] = overview.build_overview(m3, lead, scope, None, None)["throughput"]["histogram"]
    out["filters"] = sel
    return JSONResponse(out)


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

def _type_names(lang: str, types) -> dict:
    """Come chiamare i tipi di oggetto nelle frasi, nella lingua attiva: «Invoice» -> fattura / fatture (nomi
    standard conosciuti); gli altri restano come sono, al plurale della lingua. Le definizioni della Control Tower
    possono avere nomi propri (object_names), che valgono prima di questi."""
    from app.services import names_i18n
    from app.services.nouns import plural
    out = {}
    for t in types:
        one = names_i18n.name(t, lang) or t
        out[t] = {"one": one, "many": plural(one, lang)}
    return out


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
            "type_names": _type_names(get_lang(request), model.types),
            "plurals": _plurals(get_lang(request), model.types, _aliases(user.id, config.id)),
            "claude": explain.available(),
        },
    )


def _tower_payload(request: Request, model, spec: dict | None, row, period: str, lang: str, lead: str | None,
                   path=None, sel: list[dict] | None = None) -> dict:
    """model: il dataset intero (catalogo e dati mancanti); sel: i filtri condivisi sui casi. I filtri sul mese di
    inizio degli oggetti dei KPI diventano il periodo (con il confronto con i mesi precedenti); gli altri riducono i
    casi. Un grafico da cui parte una selezione si calcola senza la sua selezione (mostra tutti i valori)."""
    out = {"info": _tower_info(row, lang), "catalog": control_tower.catalog(model, lead), "spec": None}
    if spec:
        clean, issues = control_tower.validate(spec, model)
        sel = sel or []
        kpi_types = {k.get("object_type") for k in clean["kpis"]}
        month_f = [f for f in sel if f["kind"] == "start_month" and f["type"] in kpi_types]
        case_f = [f for f in sel if f not in month_f]
        if month_f:
            period = control_tower.period_from_months([m for f in month_f for m in f["values"]])
        filtered = explorer.load_model(path, case_f) if case_f else model
        values = control_tower.compute(clean, filtered, period, full=model)
        if case_f:
            kpis = {k["id"]: k for k in clean["kpis"]}
            for s_out, s_spec in zip(values["sections"], clean["sections"]):
                for i, (c_out, c_spec) in enumerate(zip(s_out["charts"], s_spec["charts"])):
                    k = kpis.get(c_spec["kpi"])
                    if k is None or c_out.get("issues"):
                        continue
                    key = control_tower.own_key(k, c_spec)
                    own = [f for f in case_f if f.get("key") == key]
                    if own:
                        rest = [f for f in case_f if f not in own]
                        m2 = explorer.load_model(path, rest) if rest else model
                        s_out["charts"][i] = control_tower.chart(control_tower.context(m2), k, c_spec, values["months"])
        values["case_filtered"] = bool(case_f)
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
    sel = _filters(body.get("filters"))
    answers, _ = load_assessment(workspace_id)
    lead = overview.default_lead(model, with_business_lead(answers, config.id).get("main_object"))
    db = SessionLocal()
    try:
        row = _tower_row(db, config.id)
    finally:
        db.close()
    spec = body.get("spec") if isinstance(body.get("spec"), dict) else (row.spec if row else None)
    period = str(body.get("period") or "all")
    return JSONResponse(await run_in_threadpool(_tower_payload, request, model, spec, row, period, lang, lead,
                                                run.ocel_file_path, sel))


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
            lang, "L'LLM non è configurato su questo server (manca la chiave API): l'assistente non è disponibile.")})
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
    except control_tower.ProposalError as exc:
        # il motivo si vede nella pagina: dall'iPad il terminale del server non e' a portata di mano
        return JSONResponse({"available": True, "error": render(lang, exc.reason)}, status_code=502)
    except Exception as exc:
        print(f"Control Tower: proposta non riuscita ({exc!r}).")
        return JSONResponse({"available": True, "error": t(lang, "La richiesta all'LLM non è riuscita: riprova tra poco.")
                             + f" ({type(exc).__name__}: {str(exc)[:200]})"}, status_code=502)
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
                # domande e KPI (come si calcolano, obiettivi) tornano da approvare; grafici e sintesi no
                if control_tower.definitions_part(clean) != control_tower.definitions_part(row.spec):
                    row.status, row.approved_by, row.approved_at = "draft", None, None
                row.spec, row.updated_by, row.updated_at = clean, user.name, now
        if body.get("approve"):
            row.status, row.approved_by, row.approved_at = "approved", user.name, now
        db.add(row)
        db.commit()
        return JSONResponse({"saved": True, "status": row.status})
    finally:
        db.close()


@router.post("/analysis/control-tower/apply")
async def control_tower_apply(request: Request):
    """Applica alle definizioni in corso (non salvate) le modifiche proposte dall'assistente: ritorna le definizioni
    nuove, da vedere in anteprima e poi salvare."""
    body = await request.json()
    workspace_id, config_id = str(body.get("workspace_id") or ""), str(body.get("config_id") or "")
    lang = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    config, run, _ = _explorer_dataset(workspace_id, config_id)
    if config is None or run is None or not Path(run.ocel_file_path).exists():
        return JSONResponse({"error": t(lang, "Dataset non disponibile per l'analisi.")}, status_code=404)
    spec, skipped = control_tower.apply_operations(body.get("spec") or {}, body.get("operations") or [])
    clean, _ = control_tower.validate(spec, explorer.load_model(run.ocel_file_path))
    for s in clean["sections"]:
        for c in s["charts"]:
            c.pop("issues", None)
    return JSONResponse({"spec": clean, "skipped": [render(lang, x) for x in skipped]})


@router.post("/analysis/control-tower/assistant")
async def control_tower_assistant_ask(request: Request):
    """Domanda all'assistente della Control Tower. Con estimate=true solo il costo indicativo."""
    body = await request.json()
    workspace_id, config_id = str(body.get("workspace_id") or ""), str(body.get("config_id") or "")
    lang = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    if not explain.available():
        return JSONResponse({"available": False, "error": t(
            lang, "L'LLM non è configurato su questo server (manca la chiave API): l'assistente non è disponibile.")})
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
    finally:
        db.close()
    view = body.get("view") if isinstance(body.get("view"), dict) else {}
    # le definizioni che l'utente sta guardando (anche modifiche non ancora salvate)
    spec = view.pop("spec", None) if isinstance(view.get("spec"), dict) else (row.spec if row else None)
    view.pop("spec", None)
    sel = _filters(body.get("filters"))
    values = None
    if spec:
        # stessi numeri della pagina: periodo e casi dai filtri condivisi
        payload = await run_in_threadpool(_tower_payload, request, model, spec, row, "all", lang, lead,
                                          run.ocel_file_path, sel)
        spec, values = payload["spec"], payload.get("values")
    view["focus"] = str(body.get("focus") or "")[:300] or None
    view["case_filters"] = _filter_labels(body)
    context = control_tower_assistant.build_context(
        language=lang, process_name=ws.process_name, assessment=answers, cat=control_tower.catalog(model, lead),
        spec=spec, values=values, view=view)
    history = [m for m in body.get("messages") or [] if isinstance(m, dict)]
    est = control_tower_assistant.estimate(context, history)
    if body.get("estimate"):
        return JSONResponse({"available": True, **est})
    try:
        data = await run_in_threadpool(_filtered_tables, run.ocel_file_path, sel)
        result = await run_in_threadpool(control_tower_assistant.ask, context, history, data)
    except Exception as exc:
        print(f"Assistente della Control Tower non riuscito ({exc!r}).")
        return JSONResponse({"available": True, "error": t(lang, "La richiesta all'LLM non è riuscita: riprova tra poco.")
                             + f" ({type(exc).__name__}: {str(exc)[:200]})"}, status_code=502)
    if result.get("truncated"):
        result["answer"] = (result.get("answer") or "").rstrip() + "…\n\n" + t(
            lang, "(Risposta interrotta perché troppo lunga: fai una domanda più precisa per avere il resto.)")
    return JSONResponse({"available": True, **result})


@router.post("/analysis/control-tower/translate")
async def control_tower_translate(request: Request):
    """Completa in italiano e in inglese i testi delle definizioni scritti in una lingua sola (spec: le definizioni
    in corso; senza, quelle salvate, che vengono salvate tradotte). Con estimate=true solo il costo."""
    body = await request.json()
    workspace_id, config_id = str(body.get("workspace_id") or ""), str(body.get("config_id") or "")
    lang = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    if not explain.available():
        return JSONResponse({"available": False, "error": t(
            lang, "L'LLM non è configurato su questo server (manca la chiave API): l'assistente non è disponibile.")})
    config, run, _ = _explorer_dataset(workspace_id, config_id)
    if config is None or run is None:
        return JSONResponse({"error": t(lang, "Dataset non disponibile per l'analisi.")}, status_code=404)
    db = SessionLocal()
    try:
        row = _tower_row(db, config.id)
    finally:
        db.close()
    if row is None:
        return JSONResponse({"error": t(lang, "Non ci sono definizioni da salvare.")}, status_code=400)
    working = body.get("spec") if isinstance(body.get("spec"), dict) else None
    spec = working or row.spec
    payload = control_tower.translate_payload(spec, row.language or "it")
    if payload is None:
        return JSONResponse({"available": True, "spec": spec, "cost_usd": 0.0, "nothing": True})
    if body.get("estimate"):
        return JSONResponse({"available": True, **control_tower.translate_estimate(payload)})
    try:
        result = await run_in_threadpool(control_tower.translate, spec, row.language or "it")
    except control_tower.ProposalError as exc:
        return JSONResponse({"available": True, "error": render(lang, exc.reason)}, status_code=502)
    except Exception as exc:
        print(f"Control Tower: traduzione non riuscita ({exc!r}).")
        return JSONResponse({"available": True, "error": t(lang, "La richiesta all'LLM non è riuscita: riprova tra poco.")
                             + f" ({type(exc).__name__}: {str(exc)[:200]})"}, status_code=502)
    if working is None:
        # definizioni salvate: si salvano tradotte (i testi non cambiano cosa e' approvato)
        db = SessionLocal()
        try:
            row = _tower_row(db, config.id)
            row.spec = result["spec"]
            row.cost_usd = round((row.cost_usd or 0.0) + result["cost_usd"], 4)
            db.add(row)
            db.commit()
        finally:
            db.close()
    return JSONResponse({"available": True, "spec": result["spec"], "cost_usd": result["cost_usd"], "saved": working is None})


# ---------- Root Cause Analysis (terzo livello della Control Tower) ----------

def _rca_scope(model, path, spec: dict, kpi_id: str, sel: list[dict], period: str):
    """KPI, dataset filtrato e mesi del periodo: gli stessi casi del secondo livello (vedi _tower_payload)."""
    clean, issues = control_tower.validate(spec, model)
    kpi = next((k for k in clean["kpis"] if k["id"] == kpi_id), None)
    if kpi is None or kpi_id in issues:
        return None
    kpi_types = {k.get("object_type") for k in clean["kpis"]}
    month_f = [f for f in sel if f["kind"] == "start_month" and f["type"] in kpi_types]
    case_f = [f for f in sel if f not in month_f]
    if month_f:
        period = control_tower.period_from_months([m for f in month_f for m in f["values"]])
    seg = explorer.load_model(path, case_f) if case_f else model
    ctx = control_tower.context(seg)
    picked = control_tower.month_list(period)
    if picked:
        months = picked
    else:
        p = period if period in control_tower.PERIODS or control_tower.parse_range(period) else "all"
        months = control_tower.period_months(ctx, p, ctx.last_start(kpi_types), ctx.first_start(kpi_types))[0]
    return {"kpi": kpi, "seg": seg, "case_f": case_f, "months": months}


def _rca_stale(row: RcaAnalysis, version: int, run_id: str | None) -> bool:
    """Fatta su dati precedenti: un'altra versione del dataset o un altro caricamento dei dati."""
    return row.dataset_version != version or bool(row.run_id and run_id and row.run_id != run_id)


def _rca_out(row: RcaAnalysis, texts: dict[str, RcaText], lang: str, current_version: int,
             run_id: str | None = None) -> dict:
    res = dict(row.result or {})
    res["notes"] = [render(lang, x) for x in res.get("notes") or []]
    findings, dismissed = [], []
    for f in res.get("findings") or []:
        tx = texts.get(f.get("text_key"))
        if tx is not None:
            f = {**f, "verdict": tx.verdict, **(tx.texts or {})}
        (dismissed if f.get("verdict") == "consequence" else findings).append(f)
    res["findings"], res["dismissed"] = findings, dismissed
    return {"id": row.id, "result": res, "version": row.dataset_version, "stale": _rca_stale(row, current_version, run_id),
            "months": row.months, "filter_labels": row.filter_labels or [], "cost_usd": row.cost_usd,
            "created_by": row.created_by, "created_at": utc_iso(row.created_at)}


@router.post("/analysis/control-tower/rca")
async def control_tower_rca(request: Request):
    """RCA di un KPI sui casi del secondo livello. Senza run: l'analisi salvata con la stessa impronta (o quella di
    una versione precedente del dataset) e il costo indicativo. Con run: calcolo (statistica, zero token), testi
    dell'LLM solo per i fattori senza testo, salvataggio."""
    body = await request.json()
    workspace_id, config_id = str(body.get("workspace_id") or ""), str(body.get("config_id") or "")
    lang = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    config, run, _ = _explorer_dataset(workspace_id, config_id)
    if config is None or run is None or not Path(run.ocel_file_path).exists():
        return JSONResponse({"error": t(lang, "Dataset non disponibile per l'analisi.")}, status_code=404)
    db = SessionLocal()
    try:
        row = _tower_row(db, config.id)
        ws = db.get(ProcessWorkspace, workspace_id)
    finally:
        db.close()
    if row is None or not row.spec:
        return JSONResponse({"error": t(lang, "Salva prima le definizioni dei KPI.")}, status_code=400)
    model = explorer.load_model(run.ocel_file_path)
    sel = _filters(body.get("filters"))
    scope = await run_in_threadpool(_rca_scope, model, run.ocel_file_path, row.spec, str(body.get("kpi_id") or ""),
                                    sel, str(body.get("period") or "all"))
    if scope is None:
        return JSONResponse({"error": t(lang, "Questo KPI non si può calcolare sul dataset: la RCA non è possibile.")},
                            status_code=400)
    kpi = scope["kpi"]
    key = rca.key_of(config.id, kpi, scope["months"], scope["case_f"])
    fp = rca.fingerprint(key, f"{config.current_version}|{run.id}")
    version = config.current_version or 1
    db = SessionLocal()
    try:
        def texts_for(r):
            keys = [f.get("text_key") for f in (r.result or {}).get("findings") or [] if f.get("text_key")]
            if not keys:
                return {}
            return {x.text_key: x for x in db.query(RcaText).filter(RcaText.ingestion_config_id == config.id,
                                                                     RcaText.text_key.in_(keys)).all()}
        if not body.get("run"):
            saved = db.query(RcaAnalysis).filter_by(ingestion_config_id=config.id, fingerprint=fp) \
                .order_by(RcaAnalysis.created_at.desc()).first()
            if saved is None:
                saved = db.query(RcaAnalysis).filter_by(ingestion_config_id=config.id, key=key) \
                    .order_by(RcaAnalysis.created_at.desc()).first()
            out = {"analysis": _rca_out(saved, texts_for(saved), lang, version, run.id) if saved else None,
                   "llm": explain.available()}
            if explain.available():
                out["estimate"] = rca.narrate_estimate(rca.MAX_FINDINGS)
            return JSONResponse(out)
    finally:
        db.close()
    # calcolo: statistica (zero token), poi i testi dell'LLM per i fattori nuovi
    result = await run_in_threadpool(rca.analyze, model, scope["seg"], kpi, scope["months"], scope["case_f"])
    for f in result.get("findings") or []:
        f["text_key"] = rca.text_key(kpi, f)
    cost = 0.0
    db = SessionLocal()
    try:
        known = {x.text_key for x in db.query(RcaText).filter(
            RcaText.ingestion_config_id == config.id,
            RcaText.text_key.in_([f["text_key"] for f in result["findings"]] or ["-"])).all()}
        new = [f for f in result["findings"] if f["text_key"] not in known]
        if new and explain.available():
            payload = rca.narrate_payload(
                process_name=ws.process_name if ws else "", kpi=kpi, filter_labels=_filter_labels(body),
                object_names=(row.spec or {}).get("object_names") or {}, result=result, findings=new)
            try:
                got = await run_in_threadpool(rca.narrate, payload)
                cost = got["cost_usd"]
                share = cost / max(1, len(got["items"]))
                for f in new:
                    it = got["items"].get(f["text_key"])
                    if it:
                        db.add(RcaText(ingestion_config_id=config.id, text_key=f["text_key"], verdict=it["verdict"],
                                       texts={k: it[k] for k in ("title", "explanation", "check") if k in it},
                                       cost_usd=share))
            except Exception as exc:   # i numeri restano validi anche senza testi
                print(f"RCA: testi dell'LLM non riusciti ({exc!r}).")
                result["notes"].append(msg("I testi dell'LLM non sono arrivati: le cause sono mostrate con i soli dati."))
        db.query(RcaAnalysis).filter_by(ingestion_config_id=config.id, fingerprint=fp).delete()
        saved = RcaAnalysis(ingestion_config_id=config.id, fingerprint=fp, key=key, dataset_version=version, run_id=run.id,
                            kpi_id=kpi["id"], section_id=str(body.get("section_id") or "")[:100] or None,
                            filter_labels=_filter_labels(body), filters=sel, months=scope["months"], result=result,
                            cost_usd=cost, created_by=user.name)
        db.add(saved)
        db.commit()
        db.refresh(saved)
        return JSONResponse({"analysis": _rca_out(saved, texts_for(saved), lang, version, run.id),
                             "llm": explain.available()})
    finally:
        db.close()


@router.post("/analysis/control-tower/rca/list")
async def control_tower_rca_list(request: Request):
    """RCA salvate del dataset: l'ultima per ogni analisi (stessa impronta senza versione), con filtri e periodo per
    riaprirla."""
    body = await request.json()
    workspace_id, config_id = str(body.get("workspace_id") or ""), str(body.get("config_id") or "")
    lang = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    config, run, _ = _explorer_dataset(workspace_id, config_id)
    if config is None:
        return JSONResponse({"error": t(lang, "Dataset non disponibile per l'analisi.")}, status_code=404)
    db = SessionLocal()
    try:
        rows = db.query(RcaAnalysis).filter_by(ingestion_config_id=config.id).order_by(RcaAnalysis.created_at.desc()).all()
        latest = {}
        for r in rows:
            latest.setdefault(r.key, r)
        keys = [f.get("text_key") for r in latest.values() for f in ((r.result or {}).get("findings") or [])[:1]
                if f.get("text_key")]
        texts = {x.text_key: x for x in db.query(RcaText).filter(RcaText.ingestion_config_id == config.id,
                                                                 RcaText.text_key.in_(keys or ["-"])).all()}
        out = []
        for r in latest.values():
            res = r.result or {}
            causes = [f for f in res.get("findings") or []
                      if (texts.get(f.get("text_key")) is None or texts[f["text_key"]].verdict != "consequence")]
            top = causes[0] if causes else None
            tx = texts.get(top.get("text_key")) if top else None
            out.append({"id": r.id, "kpi_id": r.kpi_id, "section_id": r.section_id, "filter_labels": r.filter_labels or [],
                        "filters": r.filters, "months": r.months, "version": r.dataset_version,
                        "stale": _rca_stale(r, config.current_version or 1, run.id if run else None),
                        "status": res.get("status"),
                        "causes": len(causes), "top": ({"parts": top.get("parts"), "title": (tx.texts or {}).get("title")
                                                        if tx else None} if top else None),
                        "created_by": r.created_by, "created_at": utc_iso(r.created_at)})
        return JSONResponse({"analyses": out})
    finally:
        db.close()


@router.post("/analysis/control-tower/rca/delete")
async def control_tower_rca_delete(request: Request):
    """Elimina una RCA salvata, con le sue versioni precedenti (i testi delle cause restano, si riusano)."""
    body = await request.json()
    workspace_id, config_id = str(body.get("workspace_id") or ""), str(body.get("config_id") or "")
    lang = get_lang(request)
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    config, run, _ = _explorer_dataset(workspace_id, config_id)
    if config is None:
        return JSONResponse({"error": t(lang, "Dataset non disponibile per l'analisi.")}, status_code=404)
    db = SessionLocal()
    try:
        row = db.get(RcaAnalysis, str(body.get("id") or ""))
        if row is None or row.ingestion_config_id != config.id:
            return JSONResponse({"error": t(lang, "Analisi non trovata.")}, status_code=404)
        n = db.query(RcaAnalysis).filter_by(ingestion_config_id=config.id, key=row.key).delete()
        db.commit()
        return JSONResponse({"deleted": n})
    finally:
        db.close()
