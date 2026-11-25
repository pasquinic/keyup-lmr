from __future__ import annotations

import os
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

if settings.database_url.startswith("sqlite"):
    os.makedirs(settings.data_dir, exist_ok=True)
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
else:
    engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=10)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def upgrade_schema(log=None) -> list[str]:
    """Bring an existing database up to the current models.

    ``create_all`` creates missing tables but never changes existing ones, so a database made by an
    older KeyUp lacks newer columns. Add each missing column, with its default so existing rows stay
    valid. Columns are only ever added; nothing is dropped or altered.
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    added: list[str] = []
    prep = engine.dialect.identifier_preparer
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = f"ALTER TABLE {prep.quote(table.name)} ADD COLUMN {prep.quote(col.name)} " \
                      f"{col.type.compile(dialect=engine.dialect)}"
                default = col.default.arg if col.default is not None and col.default.is_scalar else None
                if callable(default):
                    default = None
                if default is not None:
                    if isinstance(default, bool):
                        lit = ("TRUE" if default else "FALSE") if engine.dialect.name != "sqlite" else str(int(default))
                    elif isinstance(default, (int, float)):
                        lit = str(default)
                    else:
                        lit = "'" + str(default).replace("'", "''") + "'"
                    ddl += f" DEFAULT {lit}"
                    if not col.nullable:
                        ddl += " NOT NULL"
                conn.execute(text(ddl))
                added.append(f"{table.name}.{col.name}")
    if added and log:
        log.warning("upgraded database schema: added %s", ", ".join(added))
    return added
