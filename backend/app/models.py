"""Modelli dati di piattaforma per il Modulo 1 - Ingestion.

Implementa lo schema discusso in fase di design: registry di sistemi/connettori,
Ingestion Config riusabile e versionata, field mapping con provenienza
AI/utente, link N:M verso i processi, e le run di estrazione con i relativi
esiti di data quality.

L'OCEL 2.0 log risultante NON vive in queste tabelle relazionali: viene
prodotto dal Transformation Engine come documento OCEL 2.0 JSON standard
(vedi app/services/transformation.py) e referenziato da ExtractionRun
tramite ocel_file_path.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    """Utente della piattaforma. is_admin=True puo' creare utenti/processi e assegnare
    accessi; gli altri utenti operano solo sui processi a cui sono assegnati
    (vedi ProcessAssignment)."""

    __tablename__ = "app_user"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    assignments: Mapped[list["ProcessAssignment"]] = relationship(back_populates="user")


class ProcessWorkspace(Base):
    """Il processo attivato dall'utente (contesto minimo per il test del Modulo 1)."""

    __tablename__ = "process_workspace"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    process_name: Mapped[str] = mapped_column(String(200))
    process_type: Mapped[str] = mapped_column(String(100))  # es. "P2P"
    business_unit: Mapped[str | None] = mapped_column(String(200), nullable=True)
    period_from: Mapped[str | None] = mapped_column(String(50), nullable=True)
    period_to: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    links: Mapped[list["ProcessIngestionLink"]] = relationship(back_populates="workspace")
    assignments: Mapped[list["ProcessAssignment"]] = relationship(back_populates="workspace")


class ProcessAssignment(Base):
    """Assegnazione di un utente a un processo con un ruolo per modulo.

    Per ora il solo ruolo cablato e' "data_engineer" (accesso al Modulo 1 -
    Ingestion). E' pensato per estendersi ad altri ruoli/moduli (es.
    "analyst" per il Modulo 2) senza cambiare schema.
    """

    __tablename__ = "process_assignment"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("process_workspace.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"))
    role: Mapped[str] = mapped_column(String(50))  # "data_engineer"
    assigned_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    workspace: Mapped["ProcessWorkspace"] = relationship(back_populates="assignments")
    user: Mapped["User"] = relationship(back_populates="assignments")


class SourceSystem(Base):
    __tablename__ = "source_system"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    system_type: Mapped[str] = mapped_column(String(50))  # SAP | Salesforce | ServiceNow | GenericFile
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Connector(Base):
    __tablename__ = "connector"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    source_system_id: Mapped[str] = mapped_column(ForeignKey("source_system.id"))
    plugin_id: Mapped[str] = mapped_column(String(100))  # es. "file_connector"
    version: Mapped[str] = mapped_column(String(20), default="0.1.0")
    supports_sample: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_full: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_incremental: Mapped[bool] = mapped_column(Boolean, default=False)


class SystemTableCatalogEntry(Base):
    """Libreria di tabelle/campi noti per combinazione sistema+processo.

    Alimenta i suggerimenti dell'AI Mapping Service (Fase C e Step 1-2 del
    flusso AI). In questo prototipo contiene la voce "GenericFile + P2P".
    """

    __tablename__ = "system_table_catalog_entry"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    system_type: Mapped[str] = mapped_column(String(50))
    process_type: Mapped[str] = mapped_column(String(100))
    table_name_pattern: Mapped[str] = mapped_column(String(200))
    suggested_object_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    suggested_event_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rationale: Mapped[str] = mapped_column(Text)


class IngestionConfig(Base):
    """Unita' riusabile: il mapping approvato per sistema+processo."""

    __tablename__ = "ingestion_config"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    source_system_id: Mapped[str] = mapped_column(ForeignKey("source_system.id"))
    process_type: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|approved|deprecated
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    owner: Mapped[str] = mapped_column(String(200), default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    object_types: Mapped[list["ObjectTypeDef"]] = relationship(back_populates="config", cascade="all, delete-orphan")
    event_types: Mapped[list["EventTypeDef"]] = relationship(back_populates="config", cascade="all, delete-orphan")
    field_mappings: Mapped[list["FieldMapping"]] = relationship(back_populates="config", cascade="all, delete-orphan")
    versions: Mapped[list["IngestionConfigVersion"]] = relationship(back_populates="config", cascade="all, delete-orphan")
    links: Mapped[list["ProcessIngestionLink"]] = relationship(back_populates="config")


class IngestionConfigVersion(Base):
    __tablename__ = "ingestion_config_version"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    ingestion_config_id: Mapped[str] = mapped_column(ForeignKey("ingestion_config.id"))
    version: Mapped[int] = mapped_column(Integer)
    changelog: Mapped[str] = mapped_column(Text)
    approved_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    config: Mapped["IngestionConfig"] = relationship(back_populates="versions")


class ObjectTypeDef(Base):
    __tablename__ = "object_type_def"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    ingestion_config_id: Mapped[str] = mapped_column(ForeignKey("ingestion_config.id"))
    name: Mapped[str] = mapped_column(String(100))  # es. "PurchaseOrder"
    source_table: Mapped[str] = mapped_column(String(200))
    key_columns: Mapped[str] = mapped_column(String(300))  # CSV di colonne chiave

    config: Mapped["IngestionConfig"] = relationship(back_populates="object_types")


class EventTypeDef(Base):
    __tablename__ = "event_type_def"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    ingestion_config_id: Mapped[str] = mapped_column(ForeignKey("ingestion_config.id"))
    name: Mapped[str] = mapped_column(String(150))  # es. "Post Goods Receipt"
    source_table: Mapped[str] = mapped_column(String(200))
    timestamp_column: Mapped[str] = mapped_column(String(200))

    config: Mapped["IngestionConfig"] = relationship(back_populates="event_types")


class FieldMapping(Base):
    __tablename__ = "field_mapping"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    ingestion_config_id: Mapped[str] = mapped_column(ForeignKey("ingestion_config.id"))

    source_table: Mapped[str] = mapped_column(String(200))
    source_column: Mapped[str | None] = mapped_column(String(200), nullable=True)

    ocel_element: Mapped[str] = mapped_column(String(50))
    # object_type.key | object_type.attribute | event_type.timestamp |
    # event_type.attribute | e2o_relationship | o2o_relationship
    object_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    event_type: Mapped[str | None] = mapped_column(String(150), nullable=True)
    attribute_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    qualifier: Mapped[str | None] = mapped_column(String(100), nullable=True)
    related_object_type: Mapped[str | None] = mapped_column(String(100), nullable=True)

    proposal_source: Mapped[str] = mapped_column(String(20), default="ai")  # ai|user|template
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    rationale: Mapped[str] = mapped_column(Text, default="")
    based_on_template: Mapped[str | None] = mapped_column(String(200), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="proposed")  # proposed|confirmed|overridden|rejected
    original_ai_proposal: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    config: Mapped["IngestionConfig"] = relationship(back_populates="field_mappings")


class ProcessIngestionLink(Base):
    """Collega un processo (workspace) a una Ingestion Config riusabile.

    Come deciso: il riuso tra processi diversi e' ammesso solo con
    approvazione del Process Owner (campo approved_by).
    """

    __tablename__ = "process_ingestion_link"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("process_workspace.id"))
    ingestion_config_id: Mapped[str] = mapped_column(ForeignKey("ingestion_config.id"))
    pinned_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    linked_by: Mapped[str] = mapped_column(String(200), default="admin")
    approved_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    workspace: Mapped["ProcessWorkspace"] = relationship(back_populates="links")
    config: Mapped["IngestionConfig"] = relationship(back_populates="links")


class ExtractionRun(Base):
    __tablename__ = "extraction_run"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("process_workspace.id"))
    ingestion_config_id: Mapped[str] = mapped_column(ForeignKey("ingestion_config.id"))
    run_type: Mapped[str] = mapped_column(String(20), default="snapshot")  # snapshot|incremental
    status: Mapped[str] = mapped_column(String(20), default="completed")
    object_count: Mapped[int] = mapped_column(Integer, default=0)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    ocel_file_path: Mapped[str] = mapped_column(String(500))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    dq_results: Mapped[list["DataQualityCheckResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class DataQualityCheckResult(Base):
    __tablename__ = "data_quality_check_result"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    extraction_run_id: Mapped[str] = mapped_column(ForeignKey("extraction_run.id"))
    check_name: Mapped[str] = mapped_column(String(150))
    severity: Mapped[str] = mapped_column(String(20))  # info|warning|error
    passed: Mapped[bool] = mapped_column(Boolean)
    details: Mapped[str] = mapped_column(Text)
    affected_count: Mapped[int] = mapped_column(Integer, default=0)

    run: Mapped["ExtractionRun"] = relationship(back_populates="dq_results")
