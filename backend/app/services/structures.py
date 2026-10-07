"""Eliminazione delle strutture (IngestionConfig) con tutto cio' che ne dipende.

Usata sia da "Elimina" nel registro strutture (Data Engineer) sia
dall'eliminazione di un processo in Amministrazione. Il catalogo di pattern
riusabili non e' un'entita' separata (e' il flag IngestionConfig.in_catalog,
letto dal DB a ogni proposta di mapping da catalog.dynamic_lookup): eliminare
la struttura la toglie quindi anche dal catalogo, senza altri passaggi.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import DATA_DIR
from app.models import (
    DataQualityCheckResult,
    EventTypeDef,
    ExtractionRun,
    FieldMapping,
    IngestionConfig,
    IngestionConfigVersion,
    ObjectTypeDef,
    ProcessIngestionLink,
    AnalysisAlias,
    AnalysisObjective,
)


def delete_structures(db: Session, config_ids: list[str]) -> list[Path]:
    """Cancella (senza commit) le strutture indicate con run, DQ, link e
    definizioni. Ritorna i file da rimuovere dal disco DOPO il commit, via
    remove_files()."""
    if not config_ids:
        return []
    runs = db.query(ExtractionRun).filter(ExtractionRun.ingestion_config_id.in_(config_ids)).all()
    run_ids = [r.id for r in runs]
    files = [Path(r.ocel_file_path) for r in runs]
    files += [DATA_DIR / "uploads" / f"{cid}-update" for cid in config_ids]

    db.query(DataQualityCheckResult).filter(DataQualityCheckResult.extraction_run_id.in_(run_ids)).delete(
        synchronize_session=False
    )
    # anche i nomi personali dati nell'analisi (valgono solo per quel dataset)
    for model in (ExtractionRun, ProcessIngestionLink, FieldMapping, ObjectTypeDef, EventTypeDef, IngestionConfigVersion,
                  AnalysisAlias, AnalysisObjective):
        db.query(model).filter(model.ingestion_config_id.in_(config_ids)).delete(synchronize_session=False)
    db.query(IngestionConfig).filter(IngestionConfig.id.in_(config_ids)).delete(synchronize_session=False)
    return files


def remove_files(paths: list[Path]) -> None:
    for p in paths:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


def workspace_config_ids(db: Session, workspace_id: str) -> set[str]:
    """Le strutture di un processo: quelle collegate al workspace o con run su di esso."""
    return {
        cid for (cid,) in db.query(ProcessIngestionLink.ingestion_config_id).filter_by(workspace_id=workspace_id)
    } | {
        cid for (cid,) in db.query(ExtractionRun.ingestion_config_id).filter_by(workspace_id=workspace_id)
    }
