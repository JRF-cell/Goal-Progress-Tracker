from __future__ import annotations

from datetime import date, timedelta
from dataclasses import dataclass
from math import ceil
import warnings

import numpy as np
import pandas as pd
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


@dataclass
class AIScoreResult:
    score: int
    reachable: bool
    comment: str
    not_enough_data: bool
    safe_mode: bool = False
    engine: str = "ai-heuristic"
    details: dict | None = None


def _clamp_score(value: float) -> int:
    return int(max(0, min(100, round(value))))


def _build_daily_frame(db: Session, user_id: int) -> tuple[pd.DataFrame, float, int]:
    goals_stmt = select(models.Goal).where(models.Goal.user_id == user_id)
    goals = list(db.scalars(goals_stmt).all())
    if not goals:
        return pd.DataFrame(), 0.0, 0

    goal_ids = [goal.id for goal in goals]
    target_per_week_sum = int(sum(goal.target_per_week for goal in goals))
    required_daily = target_per_week_sum / 7 if target_per_week_sum > 0 else 0.0

    logs_stmt = select(models.GoalLog).where(models.GoalLog.goal_id.in_(goal_ids))
    logs = list(db.scalars(logs_stmt).all())
    if not logs:
        return pd.DataFrame(), required_daily, target_per_week_sum

    rows = [{"date": log.date, "completed": 1 if log.completed else 0} for log in logs]
    raw_df = pd.DataFrame(rows)
    day_totals = raw_df.groupby("date", as_index=False)["completed"].sum()
    day_totals["date"] = pd.to_datetime(day_totals["date"])

    all_days = pd.date_range(day_totals["date"].min(), day_totals["date"].max(), freq="D")
    frame = pd.DataFrame({"date": all_days})
    frame = frame.merge(day_totals, on="date", how="left").fillna({"completed": 0})
    frame["completed"] = frame["completed"].astype(float)

    return frame, required_daily, target_per_week_sum


def _build_goal_insights(db: Session, user_id: int) -> dict:
    goals_stmt = select(models.Goal).where(models.Goal.user_id == user_id)
    goals = list(db.scalars(goals_stmt).all())
    if not goals:
        return {}

    goal_ids = [goal.id for goal in goals]
    today = date.today()
    week_start = today - timedelta(days=today.weekday())

    logs_stmt = select(models.GoalLog).where(models.GoalLog.goal_id.in_(goal_ids), models.GoalLog.date >= week_start)
    logs = list(db.scalars(logs_stmt).all())

    progress_by_goal: dict[int, float] = {goal_id: 0.0 for goal_id in goal_ids}
    for log in logs:
        if log.completed:
            progress_by_goal[log.goal_id] = progress_by_goal.get(log.goal_id, 0.0) + 1.0

    scored_goals: list[dict] = []
    for goal in goals:
        target = max(1, int(goal.target_per_week))
        progress = float(progress_by_goal.get(goal.id, 0.0))
        ratio = progress / target if target > 0 else 0.0
        deficit = max(0.0, target - progress)
        scored_goals.append(
            {
                "name": goal.name.strip(),
                "target": float(target),
                "progress": progress,
                "ratio": ratio,
                "deficit": deficit,
            }
        )

    if not scored_goals:
        return {}

    at_risk = max(scored_goals, key=lambda item: (item["deficit"], -item["ratio"]))
    top = max(scored_goals, key=lambda item: (item["ratio"], -item["deficit"]))

    if at_risk["deficit"] >= 1.0:
        chosen = at_risk
        tone = "risk"
    else:
        chosen = top
        tone = "positive"

    return {
        "focus_goal": chosen["name"],
        "focus_goal_tone": tone,
        "focus_goal_progress": round(float(chosen["progress"]), 2),
        "focus_goal_target": round(float(chosen["target"]), 2),
        "focus_goal_deficit": round(float(chosen["deficit"]), 2),
        "focus_goal_ratio": round(float(chosen["ratio"]), 3),
    }


def _goal_fun_fact(goal_name: str) -> str:
    goal = goal_name.lower()
    keyword_facts: list[tuple[tuple[str, ...], str]] = [
        (
            ("read", "book", "study", "learn"),
            "Fun fact: reading about 20 minutes daily can improve memory and focus over time.",
        ),
        (
            ("run", "jog", "cardio", "walk"),
            "Fun fact: regular cardio can improve sleep quality and reduce stress levels.",
        ),
        (
            ("gym", "workout", "fitness", "lift"),
            "Fun fact: strength training supports posture, bone health, and long-term energy.",
        ),
        (
            ("code", "program", "leetcode", "dev"),
            "Fun fact: short coding sessions repeated often improve retention better than rare marathons.",
        ),
        (
            ("meditat", "breath", "mindful"),
            "Fun fact: even brief daily mindfulness practice can boost attention control.",
        ),
        (
            ("sleep", "bed"),
            "Fun fact: consistent sleep and wake times help your brain recover faster.",
        ),
    ]

    for keywords, fact in keyword_facts:
        if any(keyword in goal for keyword in keywords):
            return fact

    return "Fun fact: habit consistency predicts long-term results better than motivation spikes."


def _compute_streaks(series: pd.Series, threshold: float) -> list[int]:
    streaks: list[int] = []
    current = 0
    for value in series.tolist():
        if threshold > 0 and value >= threshold:
            current += 1
        else:
            current = 0
        streaks.append(current)
    return streaks


def _safe_rate(window_sum: pd.Series, required: float, window_days: int) -> pd.Series:
    required_total = required * window_days
    if required_total <= 0:
        return pd.Series(np.zeros(len(window_sum)), index=window_sum.index)
    return (window_sum / required_total).fillna(0.0)


def _build_stats(frame: pd.DataFrame, required_daily: float, target_per_week_sum: int) -> dict:
    if frame.empty:
        return {
            "observed_days": 0,
            "weekly_progress": 0.0,
            "weekly_target": float(target_per_week_sum),
            "weekly_ratio": 0.0,
            "remaining_needed": float(target_per_week_sum),
            "days_left_in_week": 7,
            "required_daily_from_now": float(target_per_week_sum) / 7 if target_per_week_sum > 0 else 0.0,
            "today_completed": 0.0,
            "consistency_14d": 0.0,
            "trend_3d": 0.0,
            "current_streak": 0,
        }

    frame = frame.sort_values("date").reset_index(drop=True)
    streaks = _compute_streaks(frame["completed"], required_daily)

    today = pd.Timestamp.now().normalize()
    days_left_in_week = max(1, 7 - int(today.weekday()))

    weekly_progress = float(frame["completed"].tail(7).sum())
    weekly_ratio = weekly_progress / target_per_week_sum if target_per_week_sum > 0 else 0.0
    remaining_needed = max(0.0, target_per_week_sum - weekly_progress)
    required_daily_from_now = remaining_needed / days_left_in_week if days_left_in_week > 0 else remaining_needed

    today_completed = float(frame.loc[frame["date"] == today, "completed"].sum())

    success_days = (
        (frame["completed"] >= required_daily).astype(float)
        if required_daily > 0
        else pd.Series(np.zeros(len(frame)), index=frame.index)
    )
    consistency = float(success_days.tail(min(14, len(success_days))).mean()) if len(success_days) > 0 else 0.0

    recent_3 = float(frame["completed"].tail(3).mean()) if len(frame) >= 1 else 0.0
    previous_3 = float(frame["completed"].iloc[-6:-3].mean()) if len(frame) >= 6 else recent_3
    trend_3d = recent_3 - previous_3

    return {
        "observed_days": int(len(frame.index)),
        "weekly_progress": round(weekly_progress, 2),
        "weekly_target": float(target_per_week_sum),
        "weekly_ratio": round(weekly_ratio, 3),
        "remaining_needed": round(remaining_needed, 2),
        "days_left_in_week": int(days_left_in_week),
        "required_daily_from_now": round(required_daily_from_now, 2),
        "today_completed": round(today_completed, 2),
        "consistency_14d": round(consistency, 3),
        "trend_3d": round(trend_3d, 3),
        "current_streak": int(streaks[-1] if streaks else 0),
    }


def _personalized_comment(score: int, stats: dict, not_enough_data: bool) -> str:
    weekly_progress = stats.get("weekly_progress", 0.0)
    weekly_target = max(1, int(round(float(stats.get("weekly_target", 1.0)))))
    remaining_needed = int(round(float(stats.get("remaining_needed", weekly_target))))
    days_left = max(1, int(stats.get("days_left_in_week", 1)))
    required_daily = float(stats.get("required_daily_from_now", 0.0))
    streak = int(stats.get("current_streak", 0))
    consistency = int(round(float(stats.get("consistency_14d", 0.0)) * 100))
    trend = float(stats.get("trend_3d", 0.0))
    focus_goal = str(stats.get("focus_goal", "")).strip()
    focus_goal_tone = str(stats.get("focus_goal_tone", "")).strip()
    focus_goal_deficit = int(round(float(stats.get("focus_goal_deficit", 0.0))))
    focus_goal_progress = int(round(float(stats.get("focus_goal_progress", 0.0))))
    focus_goal_target = max(1, int(round(float(stats.get("focus_goal_target", 1.0)))))
    focus_goal_ratio = float(stats.get("focus_goal_ratio", 0.0))

    if trend > 0.15:
        trend_text = "Momentum is improving."
    elif trend < -0.15:
        trend_text = "Momentum dropped recently."
    else:
        trend_text = "Momentum is stable."

    prefix = "Early estimate" if not_enough_data else ("On track" if score >= 60 else "At risk")
    line_1 = (
        f"{prefix}: {weekly_progress:.0f}/{weekly_target} checks this week, "
        f"streak {streak} day(s), consistency {consistency}%. {trend_text}"
    )

    if remaining_needed <= 0:
        baseline_action = "Weekly target already reached. Keep 1 light check/day to protect momentum."
    elif required_daily <= 1:
        baseline_action = f"Baseline: do at least 1 check per day for the next {days_left} day(s)."
    else:
        baseline_action = f"Baseline: aim for about {required_daily:.1f} check(s)/day over the next {days_left} day(s)."

    if focus_goal:
        safe_goal_name = focus_goal or "this goal"
        if focus_goal_tone == "positive":
            line_2 = (
                f"Spotlight goal: {safe_goal_name} is on pace ({focus_goal_progress}/{focus_goal_target}). "
                f"Keep it alive with {max(1, ceil(required_daily))} check(s) today."
            )
        elif focus_goal_deficit > 0:
            line_2 = (
                f"Stretch focus: {safe_goal_name} needs {focus_goal_deficit} more check(s) "
                f"({focus_goal_progress}/{focus_goal_target} done). {baseline_action}"
            )
        elif focus_goal_ratio >= 1.0:
            line_2 = f"Spotlight goal: {safe_goal_name} is already completed this week. {baseline_action}"
        else:
            line_2 = f"Spotlight goal: keep moving on {safe_goal_name}. {baseline_action}"
        line_3 = _goal_fun_fact(safe_goal_name)
    else:
        line_2 = baseline_action
        line_3 = _goal_fun_fact("generic goal")

    return "\n".join([line_1, line_2, line_3])


def _heuristic_assessment(
    frame: pd.DataFrame, required_daily: float, target_per_week_sum: int, goal_insights: dict | None = None
) -> AIScoreResult:
    stats = _build_stats(frame, required_daily, target_per_week_sum)
    if goal_insights:
        stats = {**stats, **goal_insights}

    if target_per_week_sum <= 0:
        return AIScoreResult(
            score=0,
            reachable=False,
            comment="Add at least one goal to estimate reachability.",
            not_enough_data=True,
            engine="ai-heuristic",
            details={**stats, "reason": "no_goals"},
        )

    if frame.empty:
        return AIScoreResult(
            score=50,
            reachable=False,
            comment="Start checking your goals to unlock personalized feedback.",
            not_enough_data=True,
            engine="ai-heuristic",
            details={**stats, "reason": "no_logs"},
        )

    if float(stats.get("remaining_needed", 0.0)) <= 0.0:
        return AIScoreResult(
            score=100,
            reachable=True,
            comment=_personalized_comment(100, stats, False),
            not_enough_data=False,
            safe_mode=False,
            engine="ai-heuristic",
            details={**stats, "reason": "weekly_target_reached"},
        )

    weekly_ratio = float(stats["weekly_ratio"])
    consistency = float(stats["consistency_14d"])
    trend = float(stats["trend_3d"])

    trend_bonus = max(-10.0, min(15.0, trend * 10.0))
    ratio_component = min(1.5, weekly_ratio) / 1.5
    raw_score = 20.0 + ratio_component * 45.0 + consistency * 25.0 + trend_bonus
    score = _clamp_score(raw_score)

    not_enough_data = bool(stats["observed_days"] < 7)
    return AIScoreResult(
        score=score,
        reachable=score >= 60,
        comment=_personalized_comment(score, stats, not_enough_data),
        not_enough_data=not_enough_data,
        safe_mode=False,
        engine="ai-heuristic",
        details=stats,
    )


def compute_user_ai_score(db: Session, user_id: int) -> AIScoreResult:
    frame, required_daily, target_per_week_sum = _build_daily_frame(db, user_id)
    goal_insights = _build_goal_insights(db, user_id)

    heuristic_result = _heuristic_assessment(frame, required_daily, target_per_week_sum, goal_insights)
    if float((heuristic_result.details or {}).get("remaining_needed", 1.0)) <= 0.0:
        return heuristic_result
    if frame.empty or len(frame.index) < 7:
        return heuristic_result

    frame = frame.sort_values("date").reset_index(drop=True)
    frame["day_of_week"] = frame["date"].dt.weekday
    frame["current_streak"] = _compute_streaks(frame["completed"], required_daily)

    completed_7 = frame["completed"].rolling(window=7, min_periods=1).sum()
    completed_14 = frame["completed"].rolling(window=14, min_periods=1).sum()
    completed_30 = frame["completed"].rolling(window=30, min_periods=1).sum()

    frame["completion_rate_7d"] = _safe_rate(completed_7, required_daily, 7)
    frame["completion_rate_14d"] = _safe_rate(completed_14, required_daily, 14)
    frame["completion_rate_30d"] = _safe_rate(completed_30, required_daily, 30)

    labels: list[float] = []
    completed_values = frame["completed"].tolist()
    n = len(completed_values)
    for idx in range(n):
        end_idx = idx + 7
        if end_idx >= n:
            labels.append(np.nan)
            continue
        next_week_total = sum(completed_values[idx + 1 : end_idx + 1])
        labels.append(1.0 if next_week_total >= target_per_week_sum else 0.0)

    frame["label"] = labels

    feature_cols = [
        "day_of_week",
        "current_streak",
        "completion_rate_7d",
        "completion_rate_14d",
        "completion_rate_30d",
    ]

    train_df = frame.dropna(subset=["label"])
    if train_df.empty or train_df["label"].nunique() < 2:
        safe_stats = heuristic_result.details or {}
        safe_score = heuristic_result.score
        return AIScoreResult(
            score=safe_score,
            reachable=safe_score >= 60,
            comment=_personalized_comment(safe_score, safe_stats, False),
            not_enough_data=False,
            safe_mode=True,
            engine="ai-heuristic",
            details={**safe_stats, "reason": "insufficient_class_variation", "train_samples": int(len(train_df.index))},
        )

    X_train = train_df[feature_cols]
    y_train = train_df["label"].astype(int)
    X_pred = frame.iloc[[-1]][feature_cols]

    pipeline = Pipeline(
        steps=[
            ("scaler", StandardScaler()),
            ("model", LogisticRegression(max_iter=2000)),
        ]
    )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", ConvergenceWarning)
            pipeline.fit(X_train, y_train)
        probability = float(pipeline.predict_proba(X_pred)[0, 1])
    except Exception as exc:
        safe_stats = heuristic_result.details or {}
        safe_score = heuristic_result.score
        return AIScoreResult(
            score=safe_score,
            reachable=safe_score >= 60,
            comment=_personalized_comment(safe_score, safe_stats, False),
            not_enough_data=False,
            safe_mode=True,
            engine="ai-heuristic",
            details={**safe_stats, "reason": "model_training_failed", "error": str(exc)},
        )

    sample_count = int(len(X_train.index))
    ml_score = _clamp_score(probability * 100)
    heuristic_score = heuristic_result.score

    if sample_count < 21:
        score = _clamp_score((ml_score * 0.55) + (heuristic_score * 0.45))
        engine = "ml-logistic-regression+ai-heuristic"
    else:
        score = ml_score
        engine = "ml-logistic-regression"

    stats = _build_stats(frame, required_daily, target_per_week_sum)
    if goal_insights:
        stats = {**stats, **goal_insights}
    details = {
        **stats,
        "samples": sample_count,
        "ml_score_raw": ml_score,
        "heuristic_score_raw": heuristic_score,
        "as_of": str(frame.iloc[-1]["date"].date()),
        "feature_schema": feature_cols,
    }

    return AIScoreResult(
        score=score,
        reachable=score >= 60,
        comment=_personalized_comment(score, stats, False),
        not_enough_data=False,
        safe_mode=False,
        engine=engine,
        details=details,
    )
