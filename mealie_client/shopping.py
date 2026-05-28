from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import MealieClient


class ShoppingMixin:
    """Shopping list operations mixed into MealieClient."""

    def list_shopping_lists(self: "MealieClient") -> list[dict]:
        data = self.get("/api/households/shopping/lists")
        return data.get("items", [])

    def get_shopping_list(self: "MealieClient", list_id: str) -> dict:
        return self.get(f"/api/households/shopping/lists/{list_id}")

    def create_shopping_list(self: "MealieClient", name: str) -> dict:
        return self.post("/api/households/shopping/lists", json={"name": name})

    def add_recipe_to_list(self: "MealieClient", list_id: str, recipe_id: str, scale: float = 1.0) -> dict:
        return self.post(
            f"/api/households/shopping/lists/{list_id}/recipe",
            json={"recipeId": recipe_id, "recipeIncrementQuantity": scale},
        )

    def get_list_items(self: "MealieClient", list_id: str) -> list[dict]:
        data = self.get("/api/households/shopping/items", shoppingListId=list_id)
        return data.get("items", [])

    def add_list_item(self: "MealieClient", list_id: str, note: str, quantity: float = 1.0) -> dict:
        return self.post(
            "/api/households/shopping/items",
            json={"shoppingListId": list_id, "note": note, "quantity": quantity, "checked": False},
        )
