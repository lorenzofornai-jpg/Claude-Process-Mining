from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

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
    ProcessIngestionLink,
    ProcessWorkspace,
    SourceSystem,
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


@router.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse(url="/ingestion/new")


@router.get("/ingestion/new", response_class=HTMLResponse)
def new_context_form(request: Request):
    return templates.TemplateResponse("context.html", {"request": request, "step": 1})


@router.post("/ingestion/new")
def create_workspace(
    process_name: str = Form(...),
    process_type: str = Form(...),
    business_unit: str = Form(""),
    period_from: str = Form(""),
    period_to: str = Form(""),
):
    db = SessionLocal()
    try:
        ws = ProcessWorkspace(
            process_name=process_name,
            process_type=process_type,
            business_unit=business_unit or None,
            period_from=period_from or None,
            period_to=period_to or None,
        )
        db.add(ws)
        db.commit()
        db.refresh(ws)
        workspace_id = ws.id
    finally:
        db.close()

    session_id = state.new_session()
    state.get(session_id)["workspace_id"] = workspace_id
    state.get(session_id)["context"] = {
        "process_name": process_name,
        "process_type": process_type,
        "business_unit": business_unit,
        "period_from": period_from,
        "period_to": period_to,
    }
    return RedirectResponse(url=f"/ingestion/upload?session_id={session_id}", status_code=303)


@router.get("/ingestion/upload", response_class=HTMLResponse)
def upload_page(request: Request, session_id: str):
    ctx = state.get(session_id)["context"]
    return templates.TemplateResponse(
        "upload.html", {"request": request, "session_id": session_id, "context": ctx, "step": 2}
    )


@router.post("/ingestion/upload")
async def handle_upload(
    session_id: str = Form(...),
    use_synthetic: str = Form(""),
    files: list[UploadFile] = File(default_factory=list),
):
    sess = state.get(session_id)

    if use_synthetic:
        file_paths = sorted(SYNTHETIC_P2P_DIR.glob("*.csv"))
        dataset_label = "Dataset sintetico P2P"
    else:
        session_upload_dir = UPLOAD_DIR / session_id
        session_upload_dir.mkdir(parents=True, exist_ok=True)
        file_paths = []
        for f in files or []:
            if not f.filename:
                continue
            dest = session_upload_dir / f.filename
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
        d["status"] = "confirmed" if p.confidence >= AUTO_ACCEPT_CONFIDENCE_THRESHOLD else "proposed"
        rows.append(d)

    sess["dataset_label"] = dataset_label
    sess["tables_schema"] = [asdict(t) for t in tables_schema]
    sess["tables_data"] = tables_data
    sess["mapping_rows"] = rows

    return RedirectResponse(url=f"/ingestion/review?session_id={session_id}", status_code=303)


def _target_label(r: dict) -> str:
    el = r["ocel_element"]
    if el == "object_type.key":
        return f'Chiave oggetto → {r["object_type"]}'
    if el == "object_type.attribute":
        return f'Attributo oggetto → {r["object_type"]}.{r["attribute_name"]}'
    if el == "event_type.timestamp":
        return f'Timestamp evento → "{r["event_type"]}"'
    if el == "event_type.attribute":
        return f'Attributo evento → "{r["event_type"]}".{r["attribute_name"]}'
    if el == "e2o_relationship":
        return f'Relazione evento→oggetto → "{r["event_type"]}" —[{r["qualifier"]}]→ {r["related_object_type"]}'
    return el


@router.get("/ingestion/review", response_class=HTMLResponse)
def review_page(request: Request, session_id: str):
    sess = state.get(session_id)
    rows = sess["mapping_rows"]
    for r in rows:
        r["target_label"] = _target_label(r)

    by_table: dict[str, list[dict]] = {}
    for r in rows:
        by_table.setdefault(r["source_table"], []).append(r)

    pending_count = sum(1 for r in rows if r["status"] == "proposed")

    return templates.TemplateResponse(
        "mapping_review.html",
        {
            "request": request,
            "session_id": session_id,
            "context": sess["context"],
            "dataset_label": sess["dataset_label"],
            "by_table": by_table,
            "pending_count": pending_count,
            "threshold": AUTO_ACCEPT_CONFIDENCE_THRESHOLD,
            "step": 3,
        },
    )


@router.post("/ingestion/review")
async def submit_review(request: Request, session_id: str = Form(...), action: str = Form(...)):
    sess = state.get(session_id)
    rows = sess["mapping_rows"]
    form = await request.form()

    for r in rows:
        decision = form.get(f"decision_{r['row_id']}")
        if decision in ("confirmed", "rejected"):
            r["status"] = decision

    if action == "bulk_accept":
        for r in rows:
            if r["status"] == "proposed" and r["confidence"] >= AUTO_ACCEPT_CONFIDENCE_THRESHOLD:
                r["status"] = "confirmed"
        return RedirectResponse(url=f"/ingestion/review?session_id={session_id}", status_code=303)

    if action == "save":
        return RedirectResponse(url=f"/ingestion/review?session_id={session_id}", status_code=303)

    if action == "finalize":
        still_pending = [r for r in rows if r["status"] == "proposed"]
        if still_pending:
            return RedirectResponse(url=f"/ingestion/review?session_id={session_id}", status_code=303)
        _finalize(session_id, sess)
        return RedirectResponse(url=f"/ingestion/result?session_id={session_id}", status_code=303)

    return RedirectResponse(url=f"/ingestion/review?session_id={session_id}", status_code=303)


def _finalize(session_id: str, sess: dict) -> None:
    rows = sess["mapping_rows"]
    confirmed = [r for r in rows if r["status"] == "confirmed"]

    ocel, skip_log, stats = build_ocel(sess["tables_data"], confirmed)
    dq_results = run_data_quality_checks(ocel, skip_log)
    object_defs, event_defs = compile_defs(confirmed)

    ocel_path = OUTPUT_DIR / f"{session_id}.ocel.json"
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
            owner="admin",
        )
        db.add(config)
        db.flush()

        db.add(IngestionConfigVersion(
            ingestion_config_id=config.id, version=1,
            changelog="Prima versione confermata dall'utente nel wizard di ingestion.",
            approved_by="admin",
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
            db.add(FieldMapping(
                ingestion_config_id=config.id,
                source_table=r["source_table"], source_column=r["source_column"],
                ocel_element=r["ocel_element"], object_type=r["object_type"], event_type=r["event_type"],
                attribute_name=r["attribute_name"], qualifier=r["qualifier"],
                related_object_type=r["related_object_type"],
                proposal_source="ai", confidence=r["confidence"], rationale=r["rationale"],
                based_on_template=r["based_on_template"],
                status=r["status"], confirmed_by="admin",
            ))

        db.add(ProcessIngestionLink(
            workspace_id=sess["workspace_id"], ingestion_config_id=config.id,
            pinned_version=1, linked_by="admin", approved_by="admin",
        ))

        run = ExtractionRun(
            workspace_id=sess["workspace_id"], ingestion_config_id=config.id,
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
        "rejected_count": len(rows) - len(confirmed),
    }


@router.get("/ingestion/result", response_class=HTMLResponse)
def result_page(request: Request, session_id: str):
    sess = state.get(session_id)
    return templates.TemplateResponse(
        "result.html",
        {
            "request": request,
            "session_id": session_id,
            "context": sess["context"],
            "result": sess["result"],
            "step": 4,
        },
    )


@router.get("/ingestion/download/{session_id}")
def download_ocel(session_id: str):
    sess = state.get(session_id)
    path = sess["result"]["ocel_path"]
    return FileResponse(path, media_type="application/json", filename="event_log.ocel.json")
