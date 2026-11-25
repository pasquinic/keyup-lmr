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


def relax_orphan_columns(log=None) -> list[str]:
    """Columns left behind by an older KeyUp build (no longer in the models) must not block inserts.

    PostgreSQL: drop NOT NULL from such columns. SQLite cannot alter columns; databases there are
    lab/test databases and are left alone.
    """
    from sqlalchemy import inspect, text

    if engine.dialect.name != "postgresql":
        return []
    insp = inspect(engine)
    relaxed: list[str] = []
    prep = engine.dialect.identifier_preparer
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            known = {c.name for c in table.columns}
            for col in insp.get_columns(table.name):
                if col["name"] not in known and not col.get("nullable", True):
                    conn.execute(text(f"ALTER TABLE {prep.quote(table.name)} ALTER COLUMN {prep.quote(col['name'])} DROP NOT NULL"))
                    relaxed.append(f"{table.name}.{col['name']}")
    if relaxed and log:
        log.warning("relaxed columns left by an older build: %s", ", ".join(relaxed))
    return relaxed


def import_legacy_tables(log=None) -> list[str]:
    """Carry data from the DVM-era tables into the native ones, once (only into empty tables)."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    moved: list[str] = []
    pairs = [
        ("iden_table", "band_plan", "id, name, base_mhz, spacing_khz, offset_mhz, bandwidth_khz",
         "id, name, base_mhz, spacing_khz, offset_mhz, bandwidth_khz"),
        ("patches", "talkgroup_patches", "name, tg_a, tg_b, enabled", "name, tg_a, tg_b, enabled"),
    ]
    with engine.begin() as conn:
        for old, new, cols_old, cols_new in pairs:
            if not (insp.has_table(old) and insp.has_table(new)):
                continue
            if conn.execute(text(f"SELECT COUNT(*) FROM {new}")).scalar():
                continue
            rows = conn.execute(text(f"SELECT {cols_old} FROM {old}")).fetchall()
            if not rows:
                continue
            if new == "talkgroup_patches":   # only patches whose talkgroups still exist
                tgs = {r[0] for r in conn.execute(text("SELECT id FROM talkgroups")).fetchall()}
                rows = [r for r in rows if r[1] in tgs and r[2] in tgs]
            ph = ", ".join(f":p{i}" for i in range(len(cols_new.split(","))))
            for r in rows:
                conn.execute(text(f"INSERT INTO {new} ({cols_new}) VALUES ({ph})"), {f"p{i}": v for i, v in enumerate(r)})
            moved.append(f"{old} -> {new}: {len(rows)}")
    if moved and log:
        log.warning("imported legacy data: %s", "; ".join(moved))
    return moved
