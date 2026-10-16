import re

from sqlalchemy import text

from app.db import Base, engine
import app.models  # noqa: F401
from app.config import DATABASE_URL


def _db_name() -> str:
    m = re.search(r"/([^/?]+)(?:\?|$)", DATABASE_URL)
    return m.group(1) if m else "electoral"


if __name__ == "__main__":
    Base.metadata.create_all(engine)
    # Session default timezone for direct reads only; stored UTC instants
    # are unchanged.
    with engine.begin() as conn:
        conn.execute(text(f"ALTER DATABASE {_db_name()} SET timezone TO 'Asia/Kolkata'"))
