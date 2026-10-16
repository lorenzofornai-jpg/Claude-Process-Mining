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
    # lingua dell'interfaccia scelta dall'utente ("it" | "en"); None = non ancora scelta
    language: Mapped[str | None] = mapped_column(String(5), nullable=True)
    # ruolo nel repository BPA (unico per l'azienda, non per processo): "architect" (Process Architect, modifica) |
    # "viewer" (Process Viewer, sola lettura) | None (nessun accesso)
    bpa_role: Mapped[str | None] = mapped_column(String(20), nullable=True)
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
    # {source_table: [colonne]} al momento dell'ultima generazione: usato per verificare
    # se un nuovo caricamento dati e' compatibile con questa struttura ("Aggiorna dati").
    schema_fingerprint: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Se True, il mapping confermato di questa struttura (le sue FieldMapping) e'
    # riusabile come pattern di riferimento per tabelle con lo stesso nome in futuri
    # upload - sia per il mock (HeuristicAIMapper, vedi services/catalog.py) sia come
    # "wiki" di riferimento passato a ClaudeAIMapper. Eliminare la struttura elimina
    # anche questo: non e' un'entita' separata, e' un flag su un IngestionConfig che
    # ha gia' cascade completo (vedi delete_structure in routers/ingestion.py).
    in_catalog: Mapped[bool] = mapped_column(Boolean, default=False)
    # oggetti di business confermati prima del mapping (services/business_objects.py): nome, tabella,
    # chiave, filtro, ruolo (guida/necessario/contesto) e perche'; None se il passo e' stato saltato
    business_objects: Mapped[list | None] = mapped_column(JSON, nullable=True)
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
    # colonna da cui si legge l'attivita' di ogni riga (es. EKBE.VGABE); None = una sola attivita'
    activity_column: Mapped[str | None] = mapped_column(String(200), nullable=True)

    config: Mapped["IngestionConfig"] = relationship(back_populates="event_types")


class FieldMapping(Base):
    __tablename__ = "field_mapping"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    ingestion_config_id: Mapped[str] = mapped_column(ForeignKey("ingestion_config.id"))

    source_table: Mapped[str] = mapped_column(String(200))
    source_column: Mapped[str | None] = mapped_column(String(200), nullable=True)

    ocel_element: Mapped[str] = mapped_column(String(50))
    # object_type.key | object_type.attribute | event_type.timestamp |
    # event_type.attribute | event_type.activity | e2o_relationship | o2o_relationship
    object_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    event_type: Mapped[str | None] = mapped_column(String(150), nullable=True)
    attribute_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    qualifier: Mapped[str | None] = mapped_column(String(100), nullable=True)
    related_object_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # solo per event_type.activity: {valore della colonna: nome attivita'} ("" = escluso);
    # None = i valori sono gia' nomi di attivita'
    activity_values: Mapped[dict | None] = mapped_column(JSON, nullable=True)

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


class ProcessAssessment(Base):
    """Checklist di assessment compilata dal Data Engineer prima di caricare i
    dati: obiettivi di analisi, perimetro, sistemi, disponibilita' dei dati,
    confini del processo. Le domande sono definite in services/assessment.py;
    qui solo le risposte (dict {id_domanda: valore}), una riga per processo."""

    __tablename__ = "process_assessment"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("process_workspace.id"), unique=True)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ProcessDocument(Base):
    """Documento di contesto caricato nell'assessment (manuali di sistema,
    procedure operative, disegni del processo BPMN, data dictionary...)."""

    __tablename__ = "process_document"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("process_workspace.id"))
    doc_type: Mapped[str] = mapped_column(String(50))  # vedi assessment.DOCUMENT_TYPES
    filename: Mapped[str] = mapped_column(String(300))
    file_path: Mapped[str] = mapped_column(String(500))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    # Attivita'/eventi letti da un file BPMN (nomi di task, eventi di inizio/fine):
    # passati all'AI Mapping come riferimento per nominare gli event type.
    bpmn_activities: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Testo letto dal documento (services/documents.py), da cui si scelgono i brani
    # passati all'AI Mapping. None = non ancora letto; "" = nessun testo leggibile.
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AnalysisAlias(Base):
    """Nome personale di un tipo di oggetto o di un'attivita' nell'analisi di un utente
    (es. «Accounting Document» → «Invoice»). Vale solo per chi l'ha scelto e solo per
    quel dataset: il dataset OCEL non cambia."""

    __tablename__ = "analysis_alias"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id"))
    ingestion_config_id: Mapped[str] = mapped_column(ForeignKey("ingestion_config.id"))
    kind: Mapped[str] = mapped_column(String(20))  # object_type | activity
    original: Mapped[str] = mapped_column(String(200))
    alias: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AnalysisObjective(Base):
    """Obiettivo misurabile di un dataset: il tempo da un'attivita' di inizio a una di fine su un
    tipo di oggetto (es. fattura: da «registrazione» a «pareggio»), eventualmente solo per gli
    oggetti con certi valori di un attributo (es. tipo documento = fattura). Nasce dalle risposte
    dell'assessment, si controlla sul dataset generato e guida il Process Overview."""

    __tablename__ = "analysis_objective"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    ingestion_config_id: Mapped[str] = mapped_column(ForeignKey("ingestion_config.id"))
    object_type: Mapped[str] = mapped_column(String(200))
    filter_attribute: Mapped[str | None] = mapped_column(String(200), nullable=True)
    filter_values: Mapped[list | None] = mapped_column(JSON, nullable=True)
    start_activity: Mapped[str] = mapped_column(String(200))
    end_activity: Mapped[str] = mapped_column(String(200))
    updated_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ObjectiveCoverage(Base):
    """Valutazione con Claude della copertura degli obiettivi dell'assessment sulle tabelle caricate
    (services/coverage.py). Vale per il caricamento con la stessa impronta delle tabelle."""

    __tablename__ = "objective_coverage"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("process_workspace.id"))
    tables_signature: Mapped[str] = mapped_column(String(40))
    language: Mapped[str] = mapped_column(String(8), default="it")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ControlTower(Base):
    """Control Tower di un dataset (services/control_tower.py): le domande di business dell'assessment tradotte
    in KPI calcolabili (definizioni, obiettivi, grafici). Le propone Claude una volta per dataset; il Data Analyst
    le rivede, le modifica e le approva. I valori si calcolano dall'app a ogni apertura."""

    __tablename__ = "control_tower"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    ingestion_config_id: Mapped[str] = mapped_column(ForeignKey("ingestion_config.id"), unique=True)
    spec: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | approved
    language: Mapped[str] = mapped_column(String(8), default="it")
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    approved_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LLMSettings(Base):
    """Configurazione dell'LLM scelta dall'amministratore (services/llm.py): una sola riga. La chiave API e'
    salvata cifrata (Fernet); in chiaro non si salva mai e non torna mai all'interfaccia (solo le ultime 4 cifre).
    Senza chiave salvata qui vale quella dell'ambiente del server (ANTHROPIC_API_KEY o le credenziali dell'SDK)."""

    __tablename__ = "llm_settings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="default")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    provider: Mapped[str] = mapped_column(String(40), default="anthropic")
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key_hint: Mapped[str | None] = mapped_column(String(20), nullable=True)   # «…a1b2»
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    timeout_s: Mapped[int] = mapped_column(Integer, default=600)
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    uses: Mapped[dict] = mapped_column(JSON, default=dict)      # {uso: {"model", "effort", "max_tokens"}}
    prices: Mapped[dict] = mapped_column(JSON, default=dict)    # {modello: [input, output]} USD per milione di token
    mapping_budget_usd: Mapped[float] = mapped_column(Float, default=0.5)
    updated_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AnalysisView(Base):
    """Vista salvata delle analisi: un insieme di filtri sui casi con un nome (es. «Italia, primo trimestre»), per un
    dataset. La vedono tutti i Data Analyst del processo; la toglie chi l'ha creata."""

    __tablename__ = "analysis_view"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    ingestion_config_id: Mapped[str] = mapped_column(ForeignKey("ingestion_config.id"))
    name: Mapped[str] = mapped_column(String(200))
    filters: Mapped[list] = mapped_column(JSON, default=list)
    created_by_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class BpaModel(Base):
    """Modello BPMN 2.0 in italiano e in inglese (stessa struttura, nomi tradotti).
    - Staging (stage None): una variante della Process Overview (oggetto guida con gli oggetti collegati) inviata alla
      BPA; la revoca dalla Process Overview la toglie.
    - Repository BPA (stage "repository"): modello importato dalla staging (copia: la variante resta in staging) o
      creato nel repository, assegnato a un processo della tassonomia (BpaProcess). Non e' di un dataset
      (ingestion_config_id None): la provenienza e' in source_model_id / source_config_id."""

    __tablename__ = "bpa_model"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    ingestion_config_id: Mapped[str | None] = mapped_column(ForeignKey("ingestion_config.id"), nullable=True)
    lead_type: Mapped[str] = mapped_column(String(200))
    variant_key: Mapped[str] = mapped_column(Text)        # oggetto guida + attivita' della variante (JSON)
    steps: Mapped[list] = mapped_column(JSON, default=list)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)   # rank, count, share, median al momento dell'invio
    labels: Mapped[dict] = mapped_column(JSON, default=dict)  # {testo del dataset: {"it", "en"}}
    lanes: Mapped[dict] = mapped_column(JSON, default=dict)   # {attivita': ruolo} dal dataset
    xml_it: Mapped[str] = mapped_column(Text)
    xml_en: Mapped[str] = mapped_column(Text)
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
    registered: Mapped[bool | None] = mapped_column(Boolean, nullable=True)   # simboli gia' nell'archivio (bpa_repo)
    workspace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)   # processo da cui e' stata inviata
    overlay_reference: Mapped[bool | None] = mapped_column(Boolean, nullable=True)   # vecchio flag: ora BpaOverlayRef
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_by_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stage: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)   # None = staging | "repository"
    process_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)   # BpaProcess (repository)
    source_model_id: Mapped[str | None] = mapped_column(String(32), nullable=True)   # modello di staging importato
    source_config_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # suo dataset
    title: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # {"it", "en"} nome del modello nel repository


class BpaOverlayRef(Base):
    """BPMN di riferimento del Process Overlay di un dataset (uno per dataset): un modello della staging del dataset o
    un modello del repository BPA (in sola lettura dall'analisi), con l'oggetto guida dei casi (None = quello del
    modello)."""

    __tablename__ = "bpa_overlay_ref"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    ingestion_config_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    model_id: Mapped[str] = mapped_column(String(32))
    lead_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    set_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    set_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class BpaProcess(Base):
    """Processo della tassonomia del repository BPA (unica per l'azienda): albero di processi (distinti dalle attivita'
    dei BPMN), ognuno con codice, nome e descrizione in italiano e in inglese e i modelli BPMN assegnati."""

    __tablename__ = "bpa_process"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(40), unique=True)   # codice interno identificativo (es. PR-0001)
    parent_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[dict] = mapped_column(JSON, default=dict)          # {"it", "en"}
    description: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # {"it", "en"}
    attrs: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # attributi definiti dall'Amministratore
    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ---------- Archivio dei simboli dei BPMN (unico per l'azienda): services/bpa_repo.py ----------

class BpaAttributeDef(Base):
    """Attributo di un tipo di simbolo, definito dall'Amministratore (Nome e Identificativo sono di sistema)."""

    __tablename__ = "bpa_attribute_def"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    symbol_type: Mapped[str] = mapped_column(String(40))
    key: Mapped[str] = mapped_column(String(40), unique=True)       # identificativo univoco nascosto (es. a_3f9c21)
    name: Mapped[dict] = mapped_column(JSON, default=dict)          # {"it", "en"}
    help: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    data_type: Mapped[str] = mapped_column(String(30))
    options: Mapped[list | None] = mapped_column(JSON, nullable=True)   # elenchi: [{"value", "it", "en"}]
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    translatable: Mapped[bool] = mapped_column(Boolean, default=False)  # testo con un valore per lingua
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class BpaSymbol(Base):
    """Un simbolo dell'archivio (definizione): stesso simbolo in piu' BPMN = piu' occorrenze."""

    __tablename__ = "bpa_symbol"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)   # identificativo univoco nascosto
    symbol_type: Mapped[str] = mapped_column(String(40), index=True)
    name_it: Mapped[str] = mapped_column(String(500), default="")
    name_en: Mapped[str] = mapped_column(String(500), default="")
    key_it: Mapped[str] = mapped_column(String(500), default="", index=True)    # nome normalizzato (unicita' per tipo)
    key_en: Mapped[str] = mapped_column(String(500), default="", index=True)
    attrs: Mapped[dict] = mapped_column(JSON, default=dict)          # {chiave attributo: valore | {"it", "en"}}
    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    renamed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # senza nome proprio (evento, gateway, ... senza nome o con un nome generico): simbolo della sola occorrenza,
    # non condiviso e senza controllo dei nomi doppi
    anonymous: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class BpaOccurrence(Base):
    """Dove compare un simbolo: elemento di un modello BPMN."""

    __tablename__ = "bpa_occurrence"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    symbol_id: Mapped[str] = mapped_column(ForeignKey("bpa_symbol.id"), index=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("bpa_model.id"), index=True)
    element_id: Mapped[str] = mapped_column(String(200))
    bpmn_type: Mapped[str] = mapped_column(String(60))
    bpmn_detail: Mapped[str | None] = mapped_column(String(60), nullable=True)   # trigger: messaggio, timer, ...


class BpaRelation(Base):
    """Relazione tra due simboli in un modello (contiene, precede, attiva, crea, input, output, ...)."""

    __tablename__ = "bpa_relation"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    model_id: Mapped[str] = mapped_column(ForeignKey("bpa_model.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[str] = mapped_column(ForeignKey("bpa_symbol.id"), index=True)
    target_id: Mapped[str] = mapped_column(ForeignKey("bpa_symbol.id"), index=True)


class RcaAnalysis(Base):
    """Root Cause Analysis salvata (services/rca.py): stessa impronta (metodo, dataset e versione, definizione del
    KPI, periodo, filtri, caricamento dei dati) = stesso risultato, senza ricalcolo ne' token. «key» e' l'impronta senza
    versione e caricamento: la stessa analisi su dati precedenti si mostra con l'avviso e il pulsante «Aggiorna»."""

    __tablename__ = "rca_analysis"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    ingestion_config_id: Mapped[str] = mapped_column(ForeignKey("ingestion_config.id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[str] = mapped_column(String(64), index=True)
    dataset_version: Mapped[int] = mapped_column(Integer, default=1)
    # caricamento dei dati (ExtractionRun): «Aggiorna i dati» carica dati nuovi senza cambiare la versione
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kpi_id: Mapped[str] = mapped_column(String(100))
    section_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    filter_labels: Mapped[list | None] = mapped_column(JSON, nullable=True)   # come l'utente vedeva i filtri
    filters: Mapped[list | None] = mapped_column(JSON, nullable=True)   # filtri condivisi (per riaprirla con gli stessi)
    months: Mapped[list | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class RcaText(Base):
    """Testi dell'LLM di una causa della RCA, in italiano e in inglese e senza numeri: lo stesso fattore per lo stesso
    KPI riusa il testo (altri filtri, dati aggiornati) e l'LLM si chiama solo per i fattori nuovi."""

    __tablename__ = "rca_text"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    ingestion_config_id: Mapped[str] = mapped_column(ForeignKey("ingestion_config.id"), index=True)
    text_key: Mapped[str] = mapped_column(String(64), index=True)
    verdict: Mapped[str] = mapped_column(String(20), default="cause")    # cause | consequence | uncertain
    texts: Mapped[dict] = mapped_column(JSON, default=dict)              # {"title", "explanation", "check"}: {it, en}
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
