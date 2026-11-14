"""User-editable settings, persisted as JSON values in a key/value table."""
from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path

from . import db
from .errors import SettingsError

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

DEFAULT_PANTRY_STAPLES = [
    "salt", "sea salt", "kosher salt", "table salt", "fine salt", "flaky salt",
    "pepper", "black pepper", "white pepper", "ground pepper", "ground black pepper",
    "garlic", "lemon", "onion", "olive oil", "vegetable oil", "canola oil", "sunflower oil",
    "metal skewers", "wooden skewers",
]

# Placeholders: {household}, {servings}, {existing}. Literal braces are doubled.
DEFAULT_SYSTEM_PROMPT = """You are a meal planning assistant. Generate detailed dinner recipes in schema.org/Recipe JSON-LD format.

Rules:
- Family-friendly for {household}
- Vary cuisines: Asian, Italian, Mexican, Middle Eastern, Kiwi/Aussie, etc.
- Practical: 30-60 min cook time, ingredients available at NZ supermarkets
- Flavourful but not too spicy — meals kids enjoy
- Realistic ingredient quantities for {servings} servings
- Clear, numbered step-by-step instructions
- Avoid recipes already in the library: {existing}
- Only suggest recipes you know have a real, publicly accessible source URL
  (e.g. from allrecipes.com, bbc.co.uk/food, recipetineats.com, etc.)
- Include that URL as the "url" field. Never fabricate a URL.

Return ONLY a JSON array of schema.org/Recipe objects. Each must include:
  @context, @type, name, description, recipeYield, prepTime, cookTime, totalTime,
  recipeIngredient (array of strings), recipeInstructions (array of HowToStep objects),
  recipeCategory, recipeCuisine, keywords, effort, url

effort is an integer 1-5 rating the total time and complexity:
  1 = very quick, ≤20 min, minimal prep (e.g. simple stir-fry, pasta aglio e olio)
  2 = easy, 20-35 min, basic techniques
  3 = moderate, 35-50 min, some prep or multiple components
  4 = involved, 50-75 min, marinating/slow cooking/several steps
  5 = time-intensive, 75+ min or complex technique (e.g. slow braise, homemade pastry)

Example of one item:
{{
  "@context": "https://schema.org",
  "@type": "Recipe",
  "name": "Butter Chicken",
  "description": "A rich, creamy Indian curry the whole family will love.",
  "recipeYield": "4 servings",
  "prepTime": "PT15M",
  "cookTime": "PT30M",
  "totalTime": "PT45M",
  "recipeCuisine": "Indian",
  "recipeCategory": "Dinner",
  "keywords": "chicken, curry, family friendly",
  "url": "https://www.allrecipes.com/recipe/12345/butter-chicken/",
  "recipeIngredient": [
    "600g chicken thighs, cut into chunks",
    "1 cup tomato passata",
    "1 cup cream"
  ],
  "recipeInstructions": [
    {{"@type": "HowToStep", "text": "Marinate chicken in yoghurt and spices for 30 minutes."}},
    {{"@type": "HowToStep", "text": "Cook chicken in a hot pan until golden."}}
  ],
  "effort": 3
}}"""


@dataclass
class Settings:
    # Household
    adults: int = 3
    children: int = 1
    child_weight: float = 0.5  # a child counts as this many adult servings
    household_description: str = "3 adults and 1 child (approx. 13 years old), based in New Zealand"

    # Planning. Weekdays are Python's: Monday=0 … Sunday=6.
    week_start_day: int = 5  # Saturday
    plan_weekdays: list[int] = field(default_factory=lambda: [5, 6, 0, 1, 2])  # Sat–Wed
    week_interval: int = 2  # plan every Nth week
    weeks_to_plan: int = 2  # how many weeks one planning run fills
    day_effort_max: dict[int, int] = field(default_factory=lambda: {0: 3, 2: 3})  # weekday -> max effort
    lookback_days: int = 40  # don't repeat a recipe eaten this recently
    replace_lookahead_weeks: int = 6  # 'replace' hides recipes planned this far ahead
    default_servings: int = 4  # assumed when a recipe has no servings
    pantry_staples: list[str] = field(default_factory=lambda: list(DEFAULT_PANTRY_STAPLES))

    # Recipe generation
    claude_model: str = "claude-haiku-4-5-20251001"
    batch_size: int = 5  # recipes per Claude call, to stay within token limits
    suggest_count: int = 14
    system_prompt: str = DEFAULT_SYSTEM_PROMPT

    # --- derived -------------------------------------------------------

    @property
    def effective_servings(self) -> float:
        return self.adults + self.children * self.child_weight

    def scale_factor(self, recipe_servings: float | None) -> float:
        return round(self.effective_servings / (recipe_servings or self.default_servings), 2)

    def render_system_prompt(self, existing: str) -> str:
        return self.system_prompt.format(
            household=self.household_description,
            servings=self.default_servings,
            existing=existing,
        )

    # --- conversion ----------------------------------------------------

    def to_dict(self) -> dict:
        """JSON-ready dict. Note JSON turns the day_effort_max keys into strings."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        """Build from a (possibly partial, possibly string-typed) dict.

        Unknown keys are ignored and missing ones take their defaults. Raises
        SettingsError if a value can't be converted; call validate() for range checks.
        """
        settings = cls()
        problems = []
        for f in fields(cls):
            if f.name not in data:
                continue
            try:
                setattr(settings, f.name, _COERCE[f.name](data[f.name]))
            except (TypeError, ValueError, AttributeError):
                problems.append(f"{f.name}: invalid value {data[f.name]!r}")
        if problems:
            raise SettingsError(problems)
        return settings

    def validate(self) -> list[str]:
        """Return a list of problems; empty means the settings are valid."""
        problems = []

        def check(ok: bool, message: str) -> None:
            if not ok:
                problems.append(message)

        check(self.adults >= 0 and self.children >= 0, "adults and children can't be negative")
        check(0 <= self.child_weight <= 1, "child_weight must be between 0 and 1")
        check(self.effective_servings > 0, "the household must eat at least some servings")
        check(bool(self.household_description.strip()), "household_description can't be empty")
        check(0 <= self.week_start_day <= 6, "week_start_day must be a weekday 0-6 (Monday=0)")
        check(bool(self.plan_weekdays), "plan_weekdays needs at least one day")
        check(all(0 <= d <= 6 for d in self.plan_weekdays), "plan_weekdays must be weekdays 0-6 (Monday=0)")
        check(len(set(self.plan_weekdays)) == len(self.plan_weekdays), "plan_weekdays has a repeated day")
        check(1 <= self.week_interval <= 52, "week_interval must be between 1 and 52")
        check(1 <= self.weeks_to_plan <= 12, "weeks_to_plan must be between 1 and 12")
        check(all(0 <= d <= 6 for d in self.day_effort_max), "day_effort_max days must be weekdays 0-6 (Monday=0)")
        check(all(1 <= e <= 5 for e in self.day_effort_max.values()), "day_effort_max efforts must be between 1 and 5")
        check(0 <= self.lookback_days <= 365, "lookback_days must be between 0 and 365")
        check(1 <= self.replace_lookahead_weeks <= 52, "replace_lookahead_weeks must be between 1 and 52")
        check(self.default_servings >= 1, "default_servings must be at least 1")
        check(bool(self.claude_model.strip()), "claude_model can't be empty")
        check(1 <= self.batch_size <= 10, "batch_size must be between 1 and 10")
        check(1 <= self.suggest_count <= 50, "suggest_count must be between 1 and 50")
        try:
            self.render_system_prompt("none")
        except (KeyError, IndexError, ValueError) as e:
            problems.append(
                "system_prompt may only use the placeholders {household}, {servings} and {existing}; "
                f"write literal braces as {{{{ and }}}} ({type(e).__name__}: {e})"
            )
        return problems


def _int(value) -> int:
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError(value)
    return int(value)


def _int_list(value) -> list[int]:
    if isinstance(value, (str, bytes, dict)):
        raise TypeError(value)
    return [_int(v) for v in value]


def _effort_map(value) -> dict[int, int]:
    return {_int(k): _int(v) for k, v in dict(value).items()}


def _staples(value) -> list[str]:
    """Lower-cased, de-duplicated names. Accepts a list or newline/comma-separated text."""
    if isinstance(value, str):
        value = value.replace(",", "\n").splitlines()
    names = [str(v).strip().lower() for v in value]
    return list(dict.fromkeys(n for n in names if n))


_COERCE = {
    "adults": _int,
    "children": _int,
    "child_weight": float,
    "household_description": str,
    "week_start_day": _int,
    "plan_weekdays": _int_list,
    "week_interval": _int,
    "weeks_to_plan": _int,
    "day_effort_max": _effort_map,
    "lookback_days": _int,
    "replace_lookahead_weeks": _int,
    "default_servings": _int,
    "pantry_staples": _staples,
    "claude_model": str,
    "batch_size": _int,
    "suggest_count": _int,
    "system_prompt": str,
}

_CREATE_TABLE = "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"


def load_settings(path: str | Path | None = None) -> Settings:
    """Stored settings over the defaults. Never raises on bad stored values:
    an unreadable key falls back to its default. Does not create the database."""
    target = Path(path) if path else db.db_path()
    settings = Settings()
    if not target.exists():
        return settings
    with closing(db.connect(target)) as conn:
        conn.execute(_CREATE_TABLE)
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    for row in rows:
        coerce = _COERCE.get(row["key"])
        if coerce is None:
            continue
        try:
            setattr(settings, row["key"], coerce(json.loads(row["value"])))
        except (TypeError, ValueError, AttributeError):
            pass
    return settings


def save_settings(settings: Settings, path: str | Path | None = None) -> None:
    """Validate and store every setting. Raises SettingsError if invalid."""
    settings = Settings.from_dict(settings.to_dict())  # normalise types before checking
    problems = settings.validate()
    if problems:
        raise SettingsError(problems)
    rows = [(key, json.dumps(value)) for key, value in settings.to_dict().items()]
    with closing(db.connect(path)) as conn, conn:
        conn.execute(_CREATE_TABLE)
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            rows,
        )
