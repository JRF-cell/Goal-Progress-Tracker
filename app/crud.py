from datetime import date
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, schemas


def get_user_by_username(db: Session, username: str) -> models.User | None:
    stmt = select(models.User).where(models.User.username == username)
    return db.scalar(stmt)


def get_user_by_id(db: Session, user_id: int) -> models.User | None:
    stmt = select(models.User).where(models.User.id == user_id)
    return db.scalar(stmt)


def create_or_get_user(db: Session, payload: schemas.UserCreate) -> models.User:
    existing = get_user_by_username(db, payload.username)
    if existing:
        return existing

    # Keep compatibility with older schemas where email may still be required.
    legacy_email = f"{payload.username.lower().replace(' ', '_')}.{uuid4().hex[:8]}@local.invalid"
    user = models.User(username=payload.username, email=legacy_email)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def create_goal(db: Session, payload: schemas.GoalCreate) -> models.Goal:
    goal = models.Goal(
        user_id=payload.user_id,
        name=payload.name,
        target_per_week=payload.target_per_week,
    )
    db.add(goal)
    db.commit()
    db.refresh(goal)
    return goal


def get_goals_by_user(db: Session, user_id: int) -> list[models.Goal]:
    stmt = select(models.Goal).where(models.Goal.user_id == user_id).order_by(models.Goal.created_at.desc())
    return list(db.scalars(stmt).all())


def get_goal(db: Session, goal_id: int) -> models.Goal | None:
    stmt = select(models.Goal).where(models.Goal.id == goal_id)
    return db.scalar(stmt)


def delete_goal(db: Session, goal: models.Goal) -> None:
    db.delete(goal)
    db.commit()


def upsert_goal_log(db: Session, goal_id: int, log_date: date, completed: bool) -> models.GoalLog:
    stmt = select(models.GoalLog).where(models.GoalLog.goal_id == goal_id, models.GoalLog.date == log_date)
    log = db.scalar(stmt)

    if log:
        log.completed = completed
    else:
        log = models.GoalLog(goal_id=goal_id, date=log_date, completed=completed)
        db.add(log)

    db.commit()
    db.refresh(log)
    return log


def get_goal_history(db: Session, goal_id: int, start_date: date) -> list[models.GoalLog]:
    stmt = (
        select(models.GoalLog)
        .where(models.GoalLog.goal_id == goal_id, models.GoalLog.date >= start_date)
        .order_by(models.GoalLog.date.asc())
    )
    return list(db.scalars(stmt).all())
