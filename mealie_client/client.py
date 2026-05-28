import os
import requests
from typing import Optional
from .recipes import RecipesMixin
from .meal_plans import MealPlansMixin
from .shopping import ShoppingMixin


class MealieClient(RecipesMixin, MealPlansMixin, ShoppingMixin):
    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None):
        self.base_url = (base_url or os.environ["MEALIE_BASE_URL"]).rstrip("/")
        self._token = token or os.environ.get("MEALIE_TOKEN")
        if not self._token:
            self._token = self._login(
                os.environ["MEALIE_USERNAME"],
                os.environ["MEALIE_PASSWORD"],
            )

    def _login(self, username: str, password: str) -> str:
        resp = requests.post(
            f"{self.base_url}/api/auth/token",
            data={"username": username, "password": password},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    def get(self, path: str, **params) -> dict:
        resp = requests.get(f"{self.base_url}{path}", headers=self._headers(), params=params)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, json: dict) -> dict:
        resp = requests.post(f"{self.base_url}{path}", headers=self._headers(), json=json)
        resp.raise_for_status()
        return resp.json()

    def put(self, path: str, json: dict) -> dict:
        resp = requests.put(f"{self.base_url}{path}", headers=self._headers(), json=json)
        resp.raise_for_status()
        return resp.json()

    def put_multipart(self, path: str, files: dict) -> dict:
        headers = {"Authorization": f"Bearer {self._token}"}
        resp = requests.put(f"{self.base_url}{path}", headers=headers, files=files)
        resp.raise_for_status()
        return resp.json()

    def delete(self, path: str) -> None:
        resp = requests.delete(f"{self.base_url}{path}", headers=self._headers())
        resp.raise_for_status()
