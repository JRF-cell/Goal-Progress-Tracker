from datetime import date as dt_date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("username cannot be empty")
        return normalized


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    created_at: datetime


class GoalCreate(BaseModel):
    user_id: int
    name: str = Field(min_length=1, max_length=255)
    target_per_week: int = Field(ge=1, le=21)


class GoalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    target_per_week: int
    created_at: datetime


class GoalCheckCreate(BaseModel):
    date: dt_date | None = None
    completed: bool = True


class GoalLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    goal_id: int
    date: dt_date
    completed: bool
    created_at: datetime


class HistoryItem(BaseModel):
    date: dt_date
    completed: bool


class AIScoreOut(BaseModel):
    score: int
    reachable: bool
    comment: str
    comment_source: str
    generated_at: datetime
    not_enough_data: bool
    safe_mode: bool = False
    engine: str
    details: dict | None = None


class ClearDatabaseOut(BaseModel):
    users_deleted: int
    goals_deleted: int
    goal_logs_deleted: int
    message: str
