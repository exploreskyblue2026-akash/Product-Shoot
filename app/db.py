"""Database models. Postgres on Railway, SQLite locally."""
from datetime import datetime, timezone
from sqlalchemy import (create_engine, String, Integer, Float, Boolean, DateTime, Text, JSON, ForeignKey, select)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker, Session

from .config import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_pre_ping=True,
                       connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    role: Mapped[str] = mapped_column(String(20), default="editor")       # owner | admin | editor | viewer
    password_hash: Mapped[str] = mapped_column(String(300), default="")
    invite_token: Mapped[str] = mapped_column(String(100), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Source(Base):
    """An input repository added from the left rail (Drive link, catalogue, files, sheet, Shopify, web link)."""
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str] = mapped_column(String(500), default="")
    config: Mapped[dict] = mapped_column(JSON, default=dict)          # link, brand hint, code rule …
    watch: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="new")    # new | ok | error
    error: Mapped[str] = mapped_column(Text, default="")
    last_scan: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    brand_id: Mapped[str] = mapped_column(String(80), default="")
    category_id: Mapped[str] = mapped_column(String(80), default="")
    colourway: Mapped[str] = mapped_column(String(80), default="standard")
    data: Mapped[dict] = mapped_column(JSON, default=dict)             # sheet fields: contents, material, features…
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    raw_path: Mapped[str] = mapped_column(String(500), default="")     # file under STORAGE_DIR
    stage: Mapped[str] = mapped_column(String(20), default="queue", index=True)  # queue|gen|review|saved|rejected|error
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
    shopify_id: Mapped[str] = mapped_column(String(80), default="")
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    shots: Mapped[list["Shot"]] = relationship(back_populates="product", order_by="Shot.no", cascade="all, delete-orphan")


class Shot(Base):
    __tablename__ = "shots"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="wait")   # wait|gen|pass|fail|approved|rejected
    prompt: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(80), default="")
    rule_version: Mapped[int] = mapped_column(Integer, default=0)
    image_path: Mapped[str] = mapped_column(String(500), default="")
    qc: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    product: Mapped[Product] = relationship(back_populates="shots")


class RuleVersion(Base):
    """Versioned static layer: studio_standard, shot_directions, qc_rules, prompt_template, strengths."""
    __tablename__ = "rule_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | pending | live | archived
    data: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(200), default="")
    approved_by: Mapped[str] = mapped_column(String(200), default="")
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LibraryRow(Base):
    """Dynamic layer rows: table in (brands, categories, moods, props); key = its id column."""
    __tablename__ = "library"
    id: Mapped[int] = mapped_column(primary_key=True)
    table: Mapped[str] = mapped_column(String(30), index=True)
    key: Mapped[str] = mapped_column(String(80), index=True)
    data: Mapped[dict] = mapped_column(JSON)


class Repository(Base):
    """Admin default repositories (output folder, publish target, defaults)."""
    __tablename__ = "repositories"
    id: Mapped[int] = mapped_column(primary_key=True)
    use: Mapped[str] = mapped_column(String(40))
    type: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    path: Mapped[str] = mapped_column(String(500))
    watch: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="new")


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON)


class Usage(Base):
    __tablename__ = "usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(30))       # vision | image | qc
    model: Mapped[str] = mapped_column(String(80))
    cost: Mapped[float] = mapped_column(Float)


class Activity(Base):
    __tablename__ = "activity"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    who: Mapped[str] = mapped_column(String(200))
    what: Mapped[str] = mapped_column(Text)



class LibProduct(Base):
    """One product from the master sheet. key = code with only letters+digits, upper case (BCO64/GY -> BCO64GY)."""
    __tablename__ = "lib_products"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(80), index=True)
    product_id: Mapped[str] = mapped_column(String(80), default="")
    item_code: Mapped[str] = mapped_column(String(40), default="", index=True)
    ean: Mapped[str] = mapped_column(String(40), default="", index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    brand: Mapped[str] = mapped_column(String(80), default="")
    category: Mapped[str] = mapped_column(String(80), default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class LibImage(Base):
    """A raw image found for a product (photo, catalogue picture, Drive file) and its cut-out."""
    __tablename__ = "lib_images"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(80), default="", index=True)      # "" = unmatched
    source: Mapped[str] = mapped_column(String(20), default="file")          # file | catalogue | drive
    src_name: Mapped[str] = mapped_column(String(300), default="")
    drive_id: Mapped[str] = mapped_column(String(100), default="")
    path: Mapped[str] = mapped_column(String(500), default="")
    w: Mapped[int] = mapped_column(Integer, default=0)
    h: Mapped[int] = mapped_column(Integer, default=0)
    phash: Mapped[str] = mapped_column(String(32), default="")
    dup_of: Mapped[int | None] = mapped_column(Integer, nullable=True)
    primary: Mapped[bool] = mapped_column(Boolean, default=False)
    cut_path: Mapped[str] = mapped_column(String(500), default="")
    cut_status: Mapped[str] = mapped_column(String(20), default="pending")   # pending | ok | check | skip
    cut_note: Mapped[str] = mapped_column(String(300), default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)                  # angle, sharpness, quality flags
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SceneAsset(Base):
    """A generated empty scene (no product) that v2 can reuse for other products of the same brand/category/shot."""
    __tablename__ = "scene_assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    pool_key: Mapped[str] = mapped_column(String(200), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    path: Mapped[str] = mapped_column(String(500))
    uses: Mapped[int] = mapped_column(Integer, default=0)
    used_by: Mapped[list] = mapped_column(JSON, default=list)
    retired: Mapped[bool] = mapped_column(Boolean, default=False)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FileMirror(Base):
    """Where each stored file lives in Google Drive, so it can be restored after a redeploy wipes the local disk."""
    __tablename__ = "file_mirror"
    rel: Mapped[str] = mapped_column(String(500), primary_key=True)
    drive_id: Mapped[str] = mapped_column(String(100))
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

def init_db():
    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns():
    """Tiny auto-migration: new columns added in an update are created in an existing database (SQLite or Postgres)."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    with engine.begin() as con:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    ddl = col.type.compile(dialect=engine.dialect)
                    con.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}'))


def get_setting(s: Session, key: str, default=None):
    row = s.get(Setting, key)
    return default if row is None else row.value


def set_setting(s: Session, key: str, value):
    row = s.get(Setting, key)
    if row is None:
        s.add(Setting(key=key, value=value))
    else:
        row.value = value


def log(s: Session, who: str, what: str):
    s.add(Activity(who=who, what=what))
