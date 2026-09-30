import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL


log = logging.getLogger(__name__)

log.info(f"[DB] Initializing database engine with URL: {DATABASE_URL.split('@')[-1] if '@' in DATABASE_URL else DATABASE_URL}")
print(f"[DB] Initializing database engine with URL: {DATABASE_URL.split('@')[-1] if '@' in DATABASE_URL else DATABASE_URL}")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

log.info(f"[DB] Database engine and session factory initialized")
print(f"[DB] Database engine and session factory initialized")


class Base(DeclarativeBase):
    pass


def get_db():
    log.debug(f"[DB] Creating new database session")
    print(f"[DB] Creating new database session")
    
    with SessionLocal() as db:
        yield db
    
    log.debug(f"[DB] Database session closed")
    print(f"[DB] Database session closed")
