from sqlalchemy import UniqueConstraint, inspect, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .config import DATABASE_URL
from .economy import roll_appeal
from .models import Base, Rescue

engine = create_async_engine(DATABASE_URL)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


def _add_missing_columns(conn) -> None:
    # create_all only creates missing tables, so columns added to an existing
    # model would never reach an existing bot.db. Add them here; a new
    # NOT NULL column needs a server_default for this to work.
    inspector = inspect(conn)
    for table in Base.metadata.sorted_tables:
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            ddl = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column.type.compile(conn.dialect)}"
            if column.server_default is not None:
                ddl += f" NOT NULL DEFAULT '{column.server_default.arg}'"
            conn.exec_driver_sql(ddl)


def _drop_removed_unique_constraints(conn) -> None:
    # SQLite can't ALTER a constraint away, so a table still carrying a unique
    # constraint the model dropped (e.g. rescues' old one-rescuer-per-animal
    # rule) is rebuilt: renamed, recreated from the model, rows copied over.
    inspector = inspect(conn)
    for table in Base.metadata.sorted_tables:
        wanted = {
            frozenset(c.name for c in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        existing = inspector.get_unique_constraints(table.name)
        if all(frozenset(uc["column_names"]) in wanted for uc in existing):
            continue
        columns = ", ".join(
            c["name"] for c in inspector.get_columns(table.name) if c["name"] in table.columns
        )
        conn.exec_driver_sql(f"ALTER TABLE {table.name} RENAME TO {table.name}_old")
        table.create(conn)
        conn.exec_driver_sql(f"INSERT INTO {table.name} ({columns}) SELECT {columns} FROM {table.name}_old")
        conn.exec_driver_sql(f"DROP TABLE {table.name}_old")


def _roll_missing_appeal(conn) -> None:
    # Rescues made before the reserve existed have no appeal (0): roll one
    # now, with the same odds as a fresh roll.
    for rescue_id in conn.execute(select(Rescue.id).where(Rescue.appeal == 0)).scalars().all():
        conn.execute(update(Rescue).where(Rescue.id == rescue_id).values(appeal=roll_appeal()))


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_drop_removed_unique_constraints)
        await conn.run_sync(_add_missing_columns)
        await conn.run_sync(_roll_missing_appeal)
