from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import MealieClient


class FoodsMixin:
    """Food operations mixed into MealieClient."""

    def create_or_get_food(self: "MealieClient", name: str) -> dict:
        """Return the food with this name (case-insensitive), creating it if missing."""
        return self._get_or_create_named("/api/foods", name, {"name": name})

    def create_or_get_unit(self: "MealieClient", name: str) -> dict:
        """Return the unit with this name (case-insensitive), creating it if missing."""
        return self._get_or_create_named("/api/units", name, {
            "id": "",
            "name": name,
            "fraction": True,
            "abbreviation": "",
            "description": "",
        })
