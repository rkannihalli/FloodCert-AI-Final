import os

_raw = os.getenv("DATABASE_URL", "sqlite:///determinations.db")

# Railway provides postgres:// but SQLAlchemy requires postgresql://
DATABASE_URL = _raw.replace("postgres://", "postgresql://", 1)
