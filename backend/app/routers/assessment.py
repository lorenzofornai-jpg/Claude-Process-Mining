"""Assessment del processo: checklist di contesto + documenti di supporto,
compilati dal Data Engineer assegnato prima di caricare i dati sorgente."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import current_user, has_process_access
from app.config import DATA_DIR, STATIC_VERSION
from app.db import SessionLocal
from app.models import ProcessAssessment, ProcessDocument, ProcessWorkspace
from app.services import assessment as A
from app.services import documents as D

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.globals["static_version"] = STATIC_VERSION

DOCS_DIR = DATA_DIR / "documents"


def _require_engineer(request: Request, workspace_id: str):
    user = current_user(request)
    if user is None:
        return None, RedirectResponse("/login", status_code=303)
    if not has_process_access(user, workspace_id, required_role="data_engineer"):
        return None, HTMLResponse(
            "Accesso negato: non sei assegnato come Data Engineer a questo processo.", status_code=403
        )
    return user, None


def load_assessment(workspace_id: str) -> tuple[dict, list[ProcessDocument]]:
    db = SessionLocal()
    try:
        row = db.query(ProcessAssessment).filter_by(workspace_id=workspace_id).first()
        docs = (
            db.query(ProcessDocument).filter_by(workspace_id=workspace_id)
            .order_by(ProcessDocument.created_at).all()
        )
        return (dict(row.answers or {}) if row else {}), docs
    finally:
        db.close()


def _ensure_text(docs: list[ProcessDocument]) -> None:
    """Legge il testo dei documenti caricati prima che l'app lo facesse (una volta sola)."""
    missing = [d for d in docs if d.extracted_text is None]
    if not missing:
        return
    db = SessionLocal()
    try:
        for d in missing:
            row = db.get(ProcessDocument, d.id)
            path = Path(d.file_path)
            row.extracted_text = d.extracted_text = D.extract_text(path) if path.exists() else ""
        db.commit()
    finally:
        db.close()


def documents_for_mapping(workspace_id: str) -> list[dict]:
    """Testo dei documenti di supporto del processo, per scegliere i brani da dare all'AI."""
    _, docs = load_assessment(workspace_id)
    _ensure_text(docs)
    return [{"filename": d.filename, "doc_type": d.doc_type, "text": d.extracted_text}
            for d in docs if d.extracted_text]


def mapping_context(workspace_id: str) -> dict:
    """Contesto di processo (nome + assessment + attivita' BPMN) per AI Mapping e controllo di pertinenza."""
    db = SessionLocal()
    try:
        ws = db.get(ProcessWorkspace, workspace_id)
        name = ws.process_name if ws else ""
    finally:
        db.close()
    answers, docs = load_assessment(workspace_id)
    activities: list[str] = []
    for d in docs:
        for a in d.bpmn_activities or []:
            if a not in activities:
                activities.append(a)
    return A.build_mapping_context(name, answers, activities)


def assessment_status(workspace_id: str) -> dict:
    answers, docs = load_assessment(workspace_id)
    return A.completeness(answers, len(docs))


def _page(request, user, workspace_id, saved=False, error=None, status_code=200):
    db = SessionLocal()
    try:
        ws = db.get(ProcessWorkspace, workspace_id)
    finally:
        db.close()
    answers, docs = load_assessment(workspace_id)
    _ensure_text(docs)
    return templates.TemplateResponse(
        "assessment.html",
        {
            "request": request, "user": user, "workspace_id": workspace_id, "process_name": ws.process_name,
            "sections": A.SECTIONS, "answers": answers, "docs": docs, "doc_types": A.DOCUMENT_TYPES,
            "system_types": A.SYSTEM_TYPES, "max_system_rows": A.MAX_SYSTEM_ROWS,
            "status": A.completeness(answers, len(docs)), "saved": saved, "error": error,
            "allowed_ext": ", ".join(sorted(A.ALLOWED_DOCUMENT_EXTENSIONS)),
        },
        status_code=status_code,
    )


@router.get("/ingestion/assessment", response_class=HTMLResponse)
def assessment_page(request: Request, workspace_id: str, saved: int = 0):
    user, denied = _require_engineer(request, workspace_id)
    if denied:
        return denied
    return _page(request, user, workspace_id, saved=bool(saved))


@router.post("/ingestion/assessment")
async def save_assessment(request: Request, workspace_id: str = Form(...)):
    user, denied = _require_engineer(request, workspace_id)
    if denied:
        return denied
    answers = A.parse_answers(await request.form())
    db = SessionLocal()
    try:
        row = db.query(ProcessAssessment).filter_by(workspace_id=workspace_id).first()
        if row is None:
            row = ProcessAssessment(workspace_id=workspace_id)
            db.add(row)
        row.answers = answers
        row.updated_by = user.name
        row.updated_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()
    return RedirectResponse(f"/ingestion/assessment?workspace_id={workspace_id}&saved=1#documenti", status_code=303)


@router.post("/ingestion/assessment/documents")
async def upload_document(
    request: Request,
    workspace_id: str = Form(...),
    doc_type: str = Form("other"),
    files: list[UploadFile] = File(default_factory=list),
):
    user, denied = _require_engineer(request, workspace_id)
    if denied:
        return denied
    if doc_type not in A.DOCUMENT_TYPES:
        doc_type = "other"
    dest = DOCS_DIR / workspace_id
    dest.mkdir(parents=True, exist_ok=True)
    rejected = []
    db = SessionLocal()
    try:
        for f in files or []:
            if not f.filename:
                continue
            name = Path(f.filename).name
            if Path(name).suffix.lower() not in A.ALLOWED_DOCUMENT_EXTENSIONS:
                rejected.append(f"{name} (formato non supportato)")
                continue
            data = await f.read()
            if len(data) > A.MAX_DOCUMENT_BYTES:
                rejected.append(f"{name} (oltre 25 MB)")
                continue
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
            path = dest / f"{uuid.uuid4().hex[:8]}-{safe}"
            path.write_bytes(data)
            activities = None
            file_type = doc_type
            text = D.extract_text(path)
            if path.suffix.lower() in (".bpmn", ".xml"):
                activities = A.extract_bpmn_activities(path)
                if activities:
                    file_type = "bpmn"
            db.add(ProcessDocument(
                workspace_id=workspace_id, doc_type=file_type, filename=name, file_path=str(path),
                size_bytes=len(data), bpmn_activities=activities, extracted_text=text, uploaded_by=user.name,
            ))
        db.commit()
    finally:
        db.close()
    if rejected:
        return _page(request, user, workspace_id, error="Non caricati: " + "; ".join(rejected), status_code=400)
    return RedirectResponse(f"/ingestion/assessment?workspace_id={workspace_id}#documenti", status_code=303)


def _get_doc(workspace_id: str, doc_id: str) -> ProcessDocument | None:
    db = SessionLocal()
    try:
        doc = db.get(ProcessDocument, doc_id)
        return doc if doc and doc.workspace_id == workspace_id else None
    finally:
        db.close()


@router.get("/ingestion/assessment/documents/{doc_id}")
def download_document(request: Request, doc_id: str, workspace_id: str):
    user, denied = _require_engineer(request, workspace_id)
    if denied:
        return denied
    doc = _get_doc(workspace_id, doc_id)
    if doc is None or not Path(doc.file_path).exists():
        return HTMLResponse("Documento non trovato.", status_code=404)
    return FileResponse(doc.file_path, filename=doc.filename)


@router.post("/ingestion/assessment/documents/{doc_id}/delete")
def delete_document(request: Request, doc_id: str, workspace_id: str = Form(...)):
    user, denied = _require_engineer(request, workspace_id)
    if denied:
        return denied
    db = SessionLocal()
    try:
        doc = db.get(ProcessDocument, doc_id)
        if doc and doc.workspace_id == workspace_id:
            Path(doc.file_path).unlink(missing_ok=True)
            db.delete(doc)
            db.commit()
    finally:
        db.close()
    return RedirectResponse(f"/ingestion/assessment?workspace_id={workspace_id}#documenti", status_code=303)
