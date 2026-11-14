import json

import pytest

from core import db
from core.errors import SettingsError
from core.settings import Settings, load_settings, save_settings


def test_defaults_match_the_original_hardcoded_behaviour():
    s = Settings()
    assert (s.adults, s.children, s.child_weight, s.effective_servings) == (3, 1, 0.5, 3.5)
    assert (s.week_start_day, s.plan_weekdays) == (5, [5, 6, 0, 1, 2])
    assert (s.week_interval, s.weeks_to_plan) == (2, 2)
    assert s.day_effort_max == {0: 3, 2: 3}
    assert (s.lookback_days, s.replace_lookahead_weeks, s.default_servings) == (40, 6, 4)
    assert (s.claude_model, s.batch_size, s.suggest_count) == ("claude-haiku-4-5-20251001", 5, 14)
    assert {"salt", "garlic", "olive oil", "wooden skewers"} <= set(s.pantry_staples) and len(s.pantry_staples) == 20
    prompt = s.render_system_prompt("Butter Chicken")
    assert "Family-friendly for 3 adults and 1 child (approx. 13 years old), based in New Zealand" in prompt
    assert "Realistic ingredient quantities for 4 servings" in prompt
    assert "Avoid recipes already in the library: Butter Chicken" in prompt
    assert '{"@type": "HowToStep", "text": "Cook chicken in a hot pan until golden."}' in prompt
    assert s.validate() == []


def test_load_without_a_database_returns_defaults_and_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    assert load_settings() == Settings()
    assert not (tmp_path / "data").exists()


def test_round_trip_through_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    s = Settings(adults=2, children=3, child_weight=0.75, plan_weekdays=[0, 3], week_start_day=0,
                 day_effort_max={3: 2}, pantry_staples=["Butter", " salt ", "butter"],
                 household_description="two adults", system_prompt="Cook for {household}. Skip {existing}.")
    save_settings(s)
    assert db.db_path() == tmp_path / "data" / "planner.db" and db.db_path().exists()

    loaded = load_settings()
    assert loaded.day_effort_max == {3: 2}  # int keys survive JSON
    assert loaded.pantry_staples == ["butter", "salt"]
    s.pantry_staples = ["butter", "salt"]
    assert loaded == s

    loaded.adults = 5
    save_settings(loaded)  # updates in place
    assert load_settings().adults == 5


def test_other_tables_can_share_the_database(tmp_path):
    path = tmp_path / "planner.db"
    save_settings(Settings(), path)
    conn = db.connect(path)
    conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO jobs (name) VALUES ('plan')")
    conn.commit()
    assert conn.execute("SELECT name FROM jobs").fetchone()["name"] == "plan"
    conn.close()
    assert load_settings(path) == Settings()


@pytest.mark.parametrize("change", [
    {"day_effort_max": {0: 6}},
    {"day_effort_max": {0: 0}},
    {"day_effort_max": {7: 3}},
    {"plan_weekdays": [0, 7]},
    {"plan_weekdays": []},
    {"plan_weekdays": [1, 1]},
    {"week_start_day": -1},
    {"week_interval": 0},
    {"weeks_to_plan": 0},
    {"adults": 0, "children": 0},
    {"adults": -1},
    {"child_weight": 1.5},
    {"lookback_days": -1},
    {"default_servings": 0},
    {"batch_size": 0},
    {"suggest_count": 0},
    {"claude_model": " "},
    {"system_prompt": "Use {unknown} and {household}"},
    {"system_prompt": 'Example: {"name": "x"}'},
    {"system_prompt": "For {household.members} people"},  # AttributeError from str.format
    {"system_prompt": "For {servings[0]} servings"},  # TypeError
    {"adults": "three"},
])
def test_invalid_settings_are_rejected_and_not_saved(tmp_path, change):
    path = tmp_path / "planner.db"
    settings = Settings()
    for key, value in change.items():
        setattr(settings, key, value)
    with pytest.raises(SettingsError) as err:
        save_settings(settings, path)
    assert err.value.problems
    assert load_settings(path) == Settings()


def test_from_dict_accepts_form_style_values_and_json_round_trip():
    s = Settings.from_dict({"adults": "2", "child_weight": "0.25", "plan_weekdays": ["5", "6"],
                            "day_effort_max": {"0": "2"}, "pantry_staples": "Salt, Pepper\nGarlic",
                            "not_a_setting": 1})
    assert (s.adults, s.child_weight, s.plan_weekdays, s.day_effort_max) == (2, 0.25, [5, 6], {0: 2})
    assert s.pantry_staples == ["salt", "pepper", "garlic"]
    assert Settings.from_dict(json.loads(json.dumps(s.to_dict()))) == s
    with pytest.raises(SettingsError):
        Settings.from_dict({"adults": "2.5"})


def test_corrupt_stored_value_falls_back_to_default(tmp_path):
    path = tmp_path / "planner.db"
    save_settings(Settings(adults=2), path)
    conn = db.connect(path)
    conn.execute("UPDATE settings SET value = '\"lots\"' WHERE key = 'lookback_days'")
    conn.execute("INSERT INTO settings VALUES ('removed_setting', '1')")
    conn.commit()
    conn.close()
    loaded = load_settings(path)
    assert (loaded.adults, loaded.lookback_days) == (2, 40)
