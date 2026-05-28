from __future__ import annotations
from typing import TYPE_CHECKING
from datetime import date, timedelta

if TYPE_CHECKING:
    from .client import MealieClient

# Meal types supported by the Mealie API
MEAL_TYPES = ["breakfast", "lunch", "dinner", "side"]


class MealPlansMixin:
    """Meal plan operations mixed into MealieClient."""

    def list_meal_plans(self: "MealieClient", start_date: date = None, end_date: date = None) -> list[dict]:
        params = {}
        if start_date:
            params["start_date"] = start_date.isoformat()
        if end_date:
            params["end_date"] = end_date.isoformat()
        data = self.get("/api/households/mealplans", **params)
        return data.get("items", [])

    def get_this_week(self: "MealieClient") -> list[dict]:
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        return self.list_meal_plans(monday, sunday)

    def add_meal_plan_entry(
        self: "MealieClient",
        plan_date: date,
        entry_type: str,
        recipe_id: str = None,
        note: str = None,
        title: str = None,
    ) -> dict:
        payload = {
            "date": plan_date.isoformat(),
            "entryType": entry_type,
        }
        if recipe_id:
            payload["recipeId"] = recipe_id
        if note:
            payload["note"] = note
        if title:
            payload["title"] = title
        return self.post("/api/households/mealplans", json=payload)

    def delete_meal_plan_entry(self: "MealieClient", item_id: str) -> None:
        self.delete(f"/api/households/mealplans/{item_id}")

    def get_random_meal(self: "MealieClient") -> dict:
        return self.post("/api/households/mealplans/random", json={})
