from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import crud, models, schemas
from app.db import engine, get_db, run_schema_migrations
from app.ml import compute_user_ai_score

run_schema_migrations()
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Goal Tracker AI", version="2.0.0")

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/api/users", response_model=schemas.UserOut)
def create_user(payload: schemas.UserCreate, db: Session = Depends(get_db)) -> schemas.UserOut:
    return crud.create_or_get_user(db, payload)


@app.post("/api/goals", response_model=schemas.GoalOut)
def create_goal(payload: schemas.GoalCreate, db: Session = Depends(get_db)) -> schemas.GoalOut:
    if crud.get_user_by_id(db, payload.user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    return crud.create_goal(db, payload)


@app.get("/api/goals", response_model=list[schemas.GoalOut])
def list_goals(user_id: int = Query(..., ge=1), db: Session = Depends(get_db)) -> list[schemas.GoalOut]:
    return crud.get_goals_by_user(db, user_id)


@app.delete("/api/goals/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_goal(goal_id: int, db: Session = Depends(get_db)) -> Response:
    goal = crud.get_goal(db, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")

    crud.delete_goal(db, goal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/goals/{goal_id}/check", response_model=schemas.GoalLogOut)
def check_goal(
    goal_id: int,
    payload: schemas.GoalCheckCreate,
    db: Session = Depends(get_db),
) -> schemas.GoalLogOut:
    goal = crud.get_goal(db, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")

    log_date = payload.date or date.today()
    return crud.upsert_goal_log(db, goal_id=goal_id, log_date=log_date, completed=payload.completed)


@app.get("/api/goals/{goal_id}/history", response_model=list[schemas.HistoryItem])
def goal_history(
    goal_id: int,
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
) -> list[schemas.HistoryItem]:
    goal = crud.get_goal(db, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")

    start_date = date.today() - timedelta(days=days - 1)
    logs = crud.get_goal_history(db, goal_id=goal_id, start_date=start_date)
    return [schemas.HistoryItem(date=log.date, completed=log.completed) for log in logs]


@app.get("/api/users/{user_id}/ai-score", response_model=schemas.AIScoreOut)
def user_ai_score(user_id: int, db: Session = Depends(get_db)) -> schemas.AIScoreOut:
    user = crud.get_user_by_id(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    result = compute_user_ai_score(db, user_id)

    return schemas.AIScoreOut(
        score=result.score,
        reachable=result.reachable,
        comment=result.comment,
        comment_source="local",
        generated_at=datetime.utcnow(),
        not_enough_data=result.not_enough_data,
        safe_mode=result.safe_mode,
        engine=result.engine,
        details=result.details,
    )


@app.post("/api/admin/clear", response_model=schemas.ClearDatabaseOut)
def clear_database(db: Session = Depends(get_db)) -> schemas.ClearDatabaseOut:
    try:
        logs_deleted = db.execute(delete(models.GoalLog)).rowcount or 0
        goals_deleted = db.execute(delete(models.Goal)).rowcount or 0
        users_deleted = db.execute(delete(models.User)).rowcount or 0
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to clear database: {exc}") from exc

    return schemas.ClearDatabaseOut(
        users_deleted=int(users_deleted),
        goals_deleted=int(goals_deleted),
        goal_logs_deleted=int(logs_deleted),
        message="Database cleared successfully.",
    )
