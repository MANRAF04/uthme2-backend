import os

from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

db_pass = os.getenv("DB_PASSWORD")
if not db_pass:
    raise RuntimeError(
        "DB_PASSWORD is not set. The API and the Celery worker both need it."
    )

DATABASE_URL = URL.create(
    "postgresql",
    username="grades_user",
    password=db_pass,
    host="postgres-grades",
    port=5432,
    database="grades_db",
)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
