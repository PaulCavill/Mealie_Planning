from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import MealieClient


class FoodsMixin:
    """Food operations mixed into MealieClient."""

    def create_or_get_food(self: "MealieClient", name: str) -> dict:
        """Create a food if it doesn't exist, or return existing food by name."""
        # Check if food already exists (get all foods and search by name)
        data = self.get("/api/foods", perPage=200)
        for item in data.get("items", []):
            if item.get("name", "").lower() == name.lower():
                return item

        # Create new food
        return self.post("/api/foods", json={"name": name})

    def create_or_get_unit(self: "MealieClient", name: str) -> dict:
        """Create a unit if it doesn't exist, or return existing unit by name."""
        # Check if unit already exists
        data = self.get("/api/units", perPage=200)
        for item in data.get("items", []):
            if item.get("name", "").lower() == name.lower():
                return item

        # Create new unit with standard fields
        return self.post("/api/units", json={
            "id": "",
            "name": name,
            "fraction": True,
            "abbreviation": "",
            "description": "",
        })
