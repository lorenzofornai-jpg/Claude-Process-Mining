from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import current_user, has_process_access
from app.config import AI_MAPPER, AUTO_ACCEPT_CONFIDENCE_THRESHOLD, DATA_DIR, SYNTHETIC_P2P_DIR
from app.connectors.file_connector import FileConnector
from app.db import SessionLocal
from app.models import (
    Connector as ConnectorModel,
    DataQualityCheckResult,
    EventTypeDef,
    ExtractionRun,
    FieldMapping,
    IngestionConfig,
    IngestionConfigVersion,
    ObjectTypeDef,
    ProcessAssignment,
    ProcessIngestionLink,
    ProcessWorkspace,
    SourceSystem,
    User,
)
from app import state
from app.services.ai_mapping import AIMapper, ClaudeAIMapper, HeuristicAIMapper
from app.services.transformation import build_ocel, compile_defs
from app.services.validation import run_data_quality_checks

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "output"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _get_ai_mapper() -> AIMapper:
    if AI_MAPPER == "claude":
        return ClaudeAIMapper()
    return HeuristicAIMapper()


def _require_process_access(request: Request, workspace_id: str):
    """Ritorna (user, None) se autorizzato al Modulo 1 di questo processo,
    altrimenti (None, redirect_o_403)."""
    user = current_user(request)
    if user is None:
        return None, RedirectResponse("/login", status_code=303)
    if not has_process_access(user, workspace_id):
        return None, HTMLResponse(
            "Accesso negato: non sei assegnato come Data Engineer a questo processo.",
            status_code=403,
        )
    return user, None


def _load_session(workspace_id: str) -> dict:
    """Stato in-memory per il workspace, inizializzato al volo dal DB se e' la
    prima visita di questa run del server (vedi state.py)."""
    sess = state.ensure(workspace_id)
    if "context" not in sess:
        db = SessionLocal()
        try:
            ws = db.get(ProcessWorkspace, workspace_id)
        finally:
            db.close()
        sess["context"] = {
            "process_name": ws.process_name,
            "process_type": ws.process_type,
            "business_unit": ws.business_unit or "",
            "period_from": ws.period_from or "",
            "period_to": ws.period_to or "",
        }
    return sess


@router.get("/", response_class=HTMLResponse)
def root(request: Request):
    if current_user(request) is None:
        return RedirectResponse(url="/login")
    return RedirectResponse(url="/ingestion/dashboard")


@router.get("/ingestion/dashboard", response_class=HTMLResponse)
def ingestion_dashboard(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    db = SessionLocal()
    try:
        if user.is_admin:
            workspaces = db.query(ProcessWorkspace).order_by(ProcessWorkspace.created_at.desc()).all()
        else:
            assigned_ids = [
                a.workspace_id
                for a in db.query(ProcessAssignment).filter_by(user_id=user.id, role="data_engineer").all()
            ]
            workspaces = (
                db.query(ProcessWorkspace)
                .filter(ProcessWorkspace.id.in_(assigned_ids))
                .order_by(ProcessWorkspace.created_at.desc())
                .all()
                if assigned_ids
                else []
            )
    finally:
        db.close()

    return templates.TemplateResponse(
        "ingestion_dashboard.html", {"request": request, "user": user, "workspaces": workspaces}
    )


@router.get("/ingestion/new", response_class=HTMLResponse)
def new_context_form(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not user.is_admin:
        return HTMLResponse("Accesso negato: solo l'amministratore può creare un nuovo processo.", status_code=403)
    return templates.TemplateResponse("context.html", {"request": request, "user": user, "step": 1})


@router.post("/ingestion/new")
def create_workspace(
    request: Request,
    process_name: str = Form(...),
    process_type: str = Form(...),
    business_unit: str = Form(""),
    period_from: str = Form(""),
    period_to: str = Form(""),
):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not user.is_admin:
        return HTMLResponse("Accesso negato: solo l'amministratore può creare un nuovo processo.", status_code=403)

    db = SessionLocal()
    try:
        ws = ProcessWorkspace(
            process_name=process_name,
            process_type=process_type,
            business_unit=business_unit or None,
            period_from=period_from or None,
            period_to=period_to or None,
            created_by=user.name,
        )
        db.add(ws)
        db.commit()
    finally:
        db.close()

    # Il wizard di Ingestion (Fasi B-G) lo porta avanti il Data Engineer assegnato,
    # non l'admin: si torna alla dashboard admin per fare l'assegnazione.
    return RedirectResponse(url="/admin", status_code=303)


EDITABLE_FIELDS = ["ocel_element", "object_type", "event_type", "attribute_name", "qualifier", "related_object_type"]
VALID_OCEL_ELEMENTS = [
    "object_type.key", "object_type.attribute",
    "event_type.timestamp", "event_type.attribute", "e2o_relationship",
]


def _target_label(r: dict) -> str:
    v = lambda k: r.get(k) or "—"  # noqa: E731
    el = r["ocel_element"]
    if el == "object_type.key":
        return f'Chiave oggetto → {v("object_type")}'
    if el == "object_type.attribute":
        return f'Attributo oggetto → {v("object_type")}.{v("attribute_name")}'
    if el == "event_type.timestamp":
        return f'Timestamp evento → "{v("event_type")}"'
    if el == "event_type.attribute":
        return f'Attributo evento → "{v("event_type")}".{v("attribute_name")}'
    if el == "e2o_relationship":
        return f'Relazione evento→oggetto → "{v("event_type")}" —[{v("qualifier")}]→ {v("related_object_type")}'
    return el


@router.get("/ingestion/upload", response_class=HTMLResponse)
def upload_page(request: Request, workspace_id: str):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    ctx = _load_session(workspace_id)["context"]
    return templates.TemplateResponse(
        "upload.html", {"request": request, "user": user, "workspace_id": workspace_id, "context": ctx, "step": 2}
    )


@router.post("/ingestion/upload")
async def handle_upload(
    request: Request,
    workspace_id: str = Form(...),
    use_synthetic: str = Form(""),
    files: list[UploadFile] = File(default_factory=list),
):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(workspace_id)

    if use_synthetic:
        file_paths = sorted(SYNTHETIC_P2P_DIR.glob("*.csv"))
        dataset_label = "Dataset sintetico P2P"
    else:
        workspace_upload_dir = UPLOAD_DIR / workspace_id
        workspace_upload_dir.mkdir(parents=True, exist_ok=True)
        file_paths = []
        for f in files or []:
            if not f.filename:
                continue
            dest = workspace_upload_dir / f.filename
            with dest.open("wb") as out:
                shutil.copyfileobj(f.file, out)
            file_paths.append(dest)
        dataset_label = f"{len(file_paths)} file caricati"

    connector = FileConnector(file_paths)
    tables_schema = connector.discover_schema()
    tables_data = {t.name: connector.extract_full(t.name) for t in tables_schema}

    mapper = _get_ai_mapper()
    proposals = mapper.propose_mapping(tables_schema, sess["context"])
    mapper_label = "Claude (LLM reale)" if AI_MAPPER == "claude" else "euristica mock"
    dataset_label = f"{dataset_label} · AI Mapping Service: {mapper_label}"

    rows = []
    for i, p in enumerate(proposals):
        d = asdict(p)
        d["row_id"] = i
        # Tutto parte come "proposed": e' il pulsante "Accetta tutte >= soglia" a promuovere
        # le righe ad alta confidence a "confirmed" in un click, esplicitamente. Pre-confermarle
        # gia' qui renderebbe quel pulsante un no-op silenzioso (bug reale trovato in test).
        d["status"] = "proposed"
        # snapshot immutabile di cio' che l'AI ha proposto in origine: sopravvive a
        # eventuali correzioni manuali successive, per audit trail (FieldMapping.original_ai_proposal)
        d["original_ai_proposal"] = {
            "ocel_element": p.ocel_element, "object_type": p.object_type, "event_type": p.event_type,
            "attribute_name": p.attribute_name, "qualifier": p.qualifier,
            "related_object_type": p.related_object_type, "confidence": p.confidence, "rationale": p.rationale,
        }
        rows.append(d)

    sess["dataset_label"] = dataset_label
    sess["tables_schema"] = [asdict(t) for t in tables_schema]
    sess["tables_data"] = tables_data
    sess["mapping_rows"] = rows

    return RedirectResponse(url=f"/ingestion/review?workspace_id={workspace_id}", status_code=303)


@router.get("/ingestion/review", response_class=HTMLResponse)
def review_page(request: Request, workspace_id: str, error: str | None = None):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(workspace_id)
    rows = sess["mapping_rows"]
    for r in rows:
        r["target_label"] = _target_label(r)

    by_table: dict[str, list[dict]] = {}
    for r in rows:
        by_table.setdefault(r["source_table"], []).append(r)

    pending_count = sum(1 for r in rows if r["status"] == "proposed")

    blocked_message = None
    if error == "pending" and pending_count > 0:
        blocked_message = (
            f"Non ho generato il log: ci sono ancora {pending_count} proposte senza una decisione "
            "esplicita (righe evidenziate in giallo qui sotto). Accettale, rifiutale o modificale "
            "prima di confermare — oppure usa \"Accetta tutte ≥ soglia\" per sbrigare in blocco "
            "quelle ad alta confidence."
        )

    return templates.TemplateResponse(
        "mapping_review.html",
        {
            "request": request,
            "user": user,
            "workspace_id": workspace_id,
            "context": sess["context"],
            "dataset_label": sess["dataset_label"],
            "by_table": by_table,
            "pending_count": pending_count,
            "threshold": AUTO_ACCEPT_CONFIDENCE_THRESHOLD,
            "ocel_elements": VALID_OCEL_ELEMENTS,
            "blocked_message": blocked_message,
            "step": 3,
        },
    )


@router.post("/ingestion/review")
async def submit_review(request: Request, workspace_id: str = Form(...), action: str = Form(...)):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(workspace_id)
    rows = sess["mapping_rows"]
    form = await request.form()

    for r in rows:
        decision = form.get(f"decision_{r['row_id']}")
        if decision in ("confirmed", "rejected"):
            r["status"] = decision

        changed = False
        for field in EDITABLE_FIELDS:
            submitted = form.get(f"field_{field}_{r['row_id']}")
            if submitted is None:
                continue
            submitted = submitted.strip() or None
            if submitted != r.get(field):
                r[field] = submitted
                changed = True
        # una correzione manuale prevale sulla decisione radio: la riga resta
        # "nel mapping" ma tracciata come intervento umano, non proposta AI accettata
        if changed and r["status"] != "rejected":
            r["status"] = "overridden"

    if action == "bulk_accept":
        for r in rows:
            if r["status"] == "proposed" and r["confidence"] >= AUTO_ACCEPT_CONFIDENCE_THRESHOLD:
                r["status"] = "confirmed"
        return RedirectResponse(url=f"/ingestion/review?workspace_id={workspace_id}", status_code=303)

    if action == "save":
        return RedirectResponse(url=f"/ingestion/review?workspace_id={workspace_id}", status_code=303)

    if action == "finalize":
        still_pending = [r for r in rows if r["status"] == "proposed"]
        if still_pending:
            return RedirectResponse(
                url=f"/ingestion/review?workspace_id={workspace_id}&error=pending", status_code=303
            )
        _finalize(workspace_id, sess, user)
        return RedirectResponse(url=f"/ingestion/result?workspace_id={workspace_id}", status_code=303)

    return RedirectResponse(url=f"/ingestion/review?workspace_id={workspace_id}", status_code=303)


def _finalize(workspace_id: str, sess: dict, user: User) -> None:
    rows = sess["mapping_rows"]
    confirmed = [r for r in rows if r["status"] in ("confirmed", "overridden")]

    ocel, skip_log, stats = build_ocel(sess["tables_data"], confirmed)
    dq_results = run_data_quality_checks(ocel, skip_log)
    object_defs, event_defs = compile_defs(confirmed)

    ocel_path = OUTPUT_DIR / f"{workspace_id}.ocel.json"
    ocel_path.write_text(json.dumps(ocel, indent=2, ensure_ascii=False), encoding="utf-8")

    db = SessionLocal()
    try:
        ctx = sess["context"]
        system_type = "GenericFile"
        source_system = db.query(SourceSystem).filter_by(system_type=system_type).first()
        if source_system is None:
            source_system = SourceSystem(name="File Upload (CSV/TXT)", system_type=system_type)
            db.add(source_system)
            db.flush()

        connector_row = db.query(ConnectorModel).filter_by(
            source_system_id=source_system.id, plugin_id="file_connector"
        ).first()
        if connector_row is None:
            connector_row = ConnectorModel(
                source_system_id=source_system.id, plugin_id="file_connector",
                supports_incremental=False,
            )
            db.add(connector_row)
            db.flush()

        config = IngestionConfig(
            name=f"{ctx['process_type']} - {source_system.name}",
            source_system_id=source_system.id,
            process_type=ctx["process_type"],
            status="approved",
            current_version=1,
            owner=user.name,
        )
        db.add(config)
        db.flush()

        db.add(IngestionConfigVersion(
            ingestion_config_id=config.id, version=1,
            changelog="Prima versione confermata dal Data Engineer nel wizard di ingestion.",
            approved_by=user.name,
        ))

        for od in object_defs.values():
            db.add(ObjectTypeDef(
                ingestion_config_id=config.id, name=od.name,
                source_table=od.source_table, key_columns=",".join(od.key_columns),
            ))
        for ed in event_defs.values():
            db.add(EventTypeDef(
                ingestion_config_id=config.id, name=ed.name,
                source_table=ed.source_table, timestamp_column=ed.timestamp_column,
            ))

        for r in rows:
            overridden = r["status"] == "overridden"
            db.add(FieldMapping(
                ingestion_config_id=config.id,
                source_table=r["source_table"], source_column=r["source_column"],
                ocel_element=r["ocel_element"], object_type=r["object_type"], event_type=r["event_type"],
                attribute_name=r["attribute_name"], qualifier=r["qualifier"],
                related_object_type=r["related_object_type"],
                proposal_source="user" if overridden else "ai",
                confidence=r["confidence"], rationale=r["rationale"],
                based_on_template=r["based_on_template"],
                original_ai_proposal=r["original_ai_proposal"] if overridden else None,
                status=r["status"], confirmed_by=user.name,
            ))

        db.add(ProcessIngestionLink(
            workspace_id=workspace_id, ingestion_config_id=config.id,
            pinned_version=1, linked_by=user.name, approved_by=user.name,
        ))

        run = ExtractionRun(
            workspace_id=workspace_id, ingestion_config_id=config.id,
            run_type="snapshot", status="completed",
            object_count=stats["object_count"], event_count=stats["event_count"],
            ocel_file_path=str(ocel_path),
        )
        db.add(run)
        db.flush()

        for dq in dq_results:
            db.add(DataQualityCheckResult(
                extraction_run_id=run.id, check_name=dq["check_name"], severity=dq["severity"],
                passed=dq["passed"], details=dq["details"], affected_count=dq["affected_count"],
            ))

        db.commit()
        ingestion_config_id = config.id
    finally:
        db.close()

    sess["result"] = {
        "ocel_path": str(ocel_path),
        "stats": stats,
        "dq_results": dq_results,
        "ingestion_config_id": ingestion_config_id,
        "rejected_count": sum(1 for r in rows if r["status"] == "rejected"),
        "overridden_count": sum(1 for r in rows if r["status"] == "overridden"),
    }


@router.get("/ingestion/result", response_class=HTMLResponse)
def result_page(request: Request, workspace_id: str):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(workspace_id)
    return templates.TemplateResponse(
        "result.html",
        {
            "request": request,
            "user": user,
            "workspace_id": workspace_id,
            "context": sess["context"],
            "result": sess["result"],
            "step": 4,
        },
    )


@router.get("/ingestion/download/{workspace_id}")
def download_ocel(request: Request, workspace_id: str):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(workspace_id)
    path = sess["result"]["ocel_path"]
    return FileResponse(path, media_type="application/json", filename="event_log.ocel.json")
