"""Modulo 2 - Analisi (fase iniziale).

Solo il punto d'accesso per ora: verifica del ruolo Data Analyst e un
elenco delle strutture (log OCEL 2.0) gia' disponibili per il processo,
prodotte dal Modulo 1. Il resto del modulo (definizione dashboard,
process discovery, ecc.) e' da disegnare.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import current_user, has_process_access
from app.config import STATIC_VERSION
from app.db import SessionLocal
from app.models import ExtractionRun, IngestionConfig, ProcessWorkspace

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.globals["static_version"] = STATIC_VERSION


def _require_analyst_access(request: Request, workspace_id: str):
    """Ritorna (user, None) se autorizzato al Modulo 2 (Analisi) di questo
    processo, altrimenti (None, redirect_o_403)."""
    user = current_user(request)
    if user is None:
        return None, RedirectResponse("/login", status_code=303)
    if not has_process_access(user, workspace_id, required_role="data_analyst"):
        return None, HTMLResponse(
            "Accesso negato: non sei assegnato come Data Analyst a questo processo.",
            status_code=403,
        )
    return user, None


@router.get("/analysis/dashboard", response_class=HTMLResponse)
def analysis_dashboard(request: Request, workspace_id: str):
    user, denied = _require_analyst_access(request, workspace_id)
    if denied:
        return denied

    db = SessionLocal()
    try:
        ws = db.get(ProcessWorkspace, workspace_id)
        configs = (
            db.query(IngestionConfig)
            .join(ExtractionRun, ExtractionRun.ingestion_config_id == IngestionConfig.id)
            .filter(ExtractionRun.workspace_id == workspace_id, IngestionConfig.status == "approved")
            .distinct()
            .all()
        )
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
