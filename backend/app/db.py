from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL


class Base(DeclarativeBase):
    pass


engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    from app import models  # noqa: F401  (registra i modelli su Base)

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    _relax_not_null("bpa_model", "ingestion_config_id")   # modelli creati nel repository BPA, senza dataset


def _add_missing_columns() -> None:
    """Poor-man's migration per SQLite: create_all() crea le tabelle nuove ma
    non aggiunge colonne a tabelle gia' esistenti da un run precedente. Senza
    questo, ogni volta che il modello dati si evolve serve cancellare
    ingestion.db (perdendo tutti i dati di test) solo per un ALTER TABLE che
    SQLite supporta benissimo per colonne nullable. Gestisce solo aggiunte
    additive nullable: non rinomina/rimuove colonne, non tocca vincoli."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # tabella nuova: create_all() l'ha gia' creata completa
            existing_columns = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))


def _relax_not_null(table: str, column: str) -> None:
    """Toglie NOT NULL da una colonna di una tabella SQLite gia' esistente (SQLite non ha ALTER COLUMN): si crea la
    tabella nuova con la stessa definizione senza il vincolo, si copiano i dati, si sostituisce la vecchia."""
    import re
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        cols = conn.execute(text(f'PRAGMA table_info("{table}")')).fetchall()
        if not any(c[1] == column and c[3] for c in cols):
            return
        sql = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:n"), {"n": table}).scalar()
        new_sql = re.sub(rf'(\b{column}\b\s+[A-Z]+(?:\(\d+\))?)\s+NOT NULL', r"\1", sql, count=1)
        if new_sql == sql:
            return
        tmp = f"{table}__new"
        new_sql = new_sql.replace(f"CREATE TABLE {table}", f'CREATE TABLE "{tmp}"', 1).replace(
            f'CREATE TABLE "{table}"', f'CREATE TABLE "{tmp}"', 1)
        names = ", ".join(f'"{c[1]}"' for c in cols)
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conn.execute(text(new_sql))
        conn.execute(text(f'INSERT INTO "{tmp}" ({names}) SELECT {names} FROM "{table}"'))
        conn.execute(text(f'DROP TABLE "{table}"'))
        conn.execute(text(f'ALTER TABLE "{tmp}" RENAME TO "{table}"'))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
