from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL = "sqlite:///./goals.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)


def _table_exists(conn: Session, table_name: str) -> bool:
    row = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": table_name},
    ).scalar_one_or_none()
    return row is not None


def run_schema_migrations() -> None:
    with engine.begin() as conn:
        if not _table_exists(conn, "users"):
            return

        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(users)")).all()}
        if "email" not in columns:
            # Compatibility with schema versions that removed email.
            conn.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR(255)"))
            columns.add("email")

        if "username" not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN username VARCHAR(64)"))
            columns.add("username")

        if "email" in columns:
            conn.execute(
                text(
                    """
                    UPDATE users
                    SET username = CASE
                        WHEN username IS NOT NULL AND trim(username) != '' THEN username
                        WHEN instr(email, '@') > 1 THEN substr(email, 1, instr(email, '@') - 1)
                        ELSE email
                    END
                    WHERE username IS NULL OR trim(username) = ''
                    """
                )
            )

        conn.execute(
            text(
                """
                UPDATE users
                SET username = 'user_' || id
                WHERE username IS NULL OR trim(username) = ''
                """
            )
        )

        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users(username)"))


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
