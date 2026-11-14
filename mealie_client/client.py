import os
from contextlib import contextmanager
from typing import Iterator, Optional

import requests

from .recipes import RecipesMixin
from .meal_plans import MealPlansMixin
from .shopping import ShoppingMixin
from .foods import FoodsMixin

DEFAULT_TIMEOUT = 60  # seconds; Mealie's scraper and parser endpoints can be slow
MAX_PAGES = 1000  # safety stop for get_all() against a server that ignores `page`


class MealieClient(RecipesMixin, MealPlansMixin, ShoppingMixin, FoodsMixin):
    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        self.base_url = (base_url or os.environ["MEALIE_BASE_URL"]).rstrip("/")
        self.timeout = timeout or float(os.environ.get("MEALIE_TIMEOUT", DEFAULT_TIMEOUT))
        self.session = requests.Session()
        self._cache_depth = 0
        self._lookup_cache: dict[str, dict[str, dict]] = {}
        self._token = token or os.environ.get("MEALIE_TOKEN")
        if not self._token:
            self._token = self._login(
                os.environ["MEALIE_USERNAME"],
                os.environ["MEALIE_PASSWORD"],
            )
        self.session.headers["Authorization"] = f"Bearer {self._token}"

    def _login(self, username: str, password: str) -> str:
        resp = self.session.post(
            f"{self.base_url}/api/auth/token",
            data={"username": username, "password": password},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    # --- transport -----------------------------------------------------

    def get(self, path: str, **params) -> dict:
        resp = self.session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, json: dict) -> dict:
        resp = self.session.post(f"{self.base_url}{path}", json=json, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def put(self, path: str, json: dict) -> dict:
        resp = self.session.put(f"{self.base_url}{path}", json=json, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def patch(self, path: str, json: dict) -> dict:
        resp = self.session.patch(f"{self.base_url}{path}", json=json, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def put_multipart(self, path: str, files: dict) -> dict:
        resp = self.session.put(f"{self.base_url}{path}", files=files, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def delete(self, path: str) -> None:
        resp = self.session.delete(f"{self.base_url}{path}", timeout=self.timeout)
        resp.raise_for_status()

    # --- pagination ----------------------------------------------------

    def get_all(self, path: str, per_page: int = 200, **params) -> list[dict]:
        """Fetch every item from a paginated Mealie list endpoint.

        Mealie answers with `items`, `page` and `total_pages` (camelCase in
        some versions). If no page count is given, a short page ends the walk.
        """
        items: list[dict] = []
        page = 1
        while True:
            data = self.get(path, **{**params, "page": page, "perPage": per_page})
            if isinstance(data, list):  # unpaginated endpoint
                return data
            batch = data.get("items") or []
            items.extend(batch)
            total_pages = data.get("total_pages", data.get("totalPages"))
            if not batch:
                break
            if total_pages is not None:
                if page >= int(total_pages):
                    break
            elif len(batch) < per_page:
                break
            page += 1
            if page > MAX_PAGES:
                raise RuntimeError(f"Pagination of {path} did not finish after {MAX_PAGES} pages")
        return items

    # --- name lookups (tags, foods, units) -------------------------------

    @contextmanager
    def cached_lookups(self) -> Iterator["MealieClient"]:
        """Cache the tag/food/unit lists for the duration of a bulk operation.

        Outside this block every lookup re-reads the full list from Mealie, so
        a long-lived client never acts on stale ids.
        """
        self._cache_depth += 1
        try:
            yield self
        finally:
            self._cache_depth -= 1
            if self._cache_depth == 0:
                self._lookup_cache.clear()

    def _name_index(self, path: str) -> dict[str, dict]:
        """All items of a list endpoint keyed by lower-cased name."""
        if path in self._lookup_cache:
            return self._lookup_cache[path]
        index: dict[str, dict] = {}
        for item in self.get_all(path):
            index.setdefault((item.get("name") or "").lower(), item)
        if self._cache_depth:
            self._lookup_cache[path] = index
        return index

    def _get_or_create_named(self, path: str, name: str, payload: dict) -> dict:
        index = self._name_index(path)
        key = name.lower()
        if key not in index:
            index[key] = self.post(path, json=payload)
        return index[key]
