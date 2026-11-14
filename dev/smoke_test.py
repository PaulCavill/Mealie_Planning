"""Smoke test: drive a running fake Mealie through the real mealie_client.

    python dev/fake_mealie.py --port 9925 &
    python dev/smoke_test.py [base_url]

Resets the server state before and after. Needs Pillow for the image-upload check.
"""
import sys, json, io, os, requests
from pathlib import Path
from datetime import date, timedelta
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mealie_client import MealieClient
B = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9925"
requests.post(B + "/__reset").raise_for_status()
c = MealieClient(base_url=B, token="dev")
def ok(label, cond, extra=""):
    print(("PASS" if cond else "FAIL"), label, extra); assert cond, label

# auth
ok("401 without auth", requests.get(B + "/api/recipes").status_code == 401)
ok("401 empty bearer", requests.get(B + "/api/recipes", headers={"Authorization": "Bearer "}).status_code == 401)
os.environ.update(MEALIE_USERNAME="u", MEALIE_PASSWORD="p")
ok("login", MealieClient(base_url=B)._token.startswith("fake-"))

# paging
env = c.get("/api/recipes", page=1, perPage=10)
ok("envelope", {"page","per_page","total","total_pages","items"} <= set(env) and env["total_pages"] == -(-env["total"]//10), f"total={env['total']}")
seen = []
p = 1
while True:
    items = c.get("/api/recipes", page=p, perPage=10)["items"]
    if not items: break
    seen += items; p += 1
ok("paged through all, no dupes", len(seen) == env["total"] == len({r["id"] for r in seen}), f"{len(seen)} in {p-1} pages")
ok("client list_recipes walks all pages", len(c.list_recipes(per_page=7)) == env["total"])
dinners = c.list_dinner_recipes()
ok("dinner filter", 0 < len(dinners) < len(seen) and all(any(t["name"]=="dinner" for t in r["tags"]) for r in dinners), str(len(dinners)))
unrated = [r for r in dinners if not any(t["name"].startswith("effort-") for t in r["tags"])]
ok("unrated dinners", len(unrated) == 6)
ok("summary fields", all(k in dinners[0] for k in ("id","slug","name","description","recipeServings","tags","image","orgURL")))
ok("search", any(r["name"] == "Butter Chicken" for r in c.search_recipes("butter chicken")))

# get / tags
r = c.get_recipe("butter-chicken")
ok("get recipe", r["name"] == "Butter Chicken" and len(r["recipeIngredient"]) >= 6 and r["recipeInstructions"])
r2 = c.add_tag("overnight-oats", "dinner"); ok("add existing tag", any(t["name"]=="dinner" for t in r2["tags"]))
r2 = c.add_tag("overnight-oats", "brand-new-tag"); ok("add new tag", any(t["name"]=="brand-new-tag" for t in r2["tags"]))
c.set_effort_tag(unrated[0]["slug"], 4); c.set_effort_tag(unrated[0]["slug"], 2)
tags = [t["name"] for t in c.get_recipe(unrated[0]["slug"])["tags"]]
ok("set effort tag", tags.count("effort-2") == 1 and "effort-4" not in tags and "dinner" in tags, str(tags))

# create
n = c.create_recipe("Test Recipe"); ok("create_recipe", n["slug"] == "test-recipe")
n2 = c.create_recipe("Test Recipe"); ok("dup slug suffixed", n2["slug"] == "test-recipe-1")
s = c.scrape_recipe("https://example.com/recipes/creamy-tuscan-chicken"); ok("scrape", s["name"] == "Creamy Tuscan Chicken" and s["orgURL"])
ok("test_scrape", c.test_scrape("https://example.com/x/pork-ramen")["name"] == "Pork Ramen")
schema = {"@type":"Recipe","name":"Verify Curry","description":"d","recipeYield":"4","recipeCuisine":"Indian",
  "recipeIngredient":["500g chicken breast, diced","2 tbsp olive oil","1/2 teaspoon dragonfruit powder","1 1/2 cups basmati rice","3 smidgens saffron","Salt, to taste"],
  "recipeInstructions":[{"@type":"HowToStep","text":"Cook."},"Eat."]}
slug = c.post("/api/recipes/create/html-or-json", json={"data": json.dumps(schema)})
ok("html-or-json returns slug str", slug == "verify-curry", repr(slug))
full = c.get_recipe(slug)
ok("imported is note-only", all(i["food"] is None and i["note"] for i in full["recipeIngredient"]) and full["recipeServings"] == 4 and len(full["recipeInstructions"]) == 2)

# parser + update ingredients
pr = c.post("/api/parser/ingredients", json={"parser":"nlp","ingredients":["500g chicken breast, diced","2 tbsp unobtainium"]})
ok("parser shape", pr[0]["input"] and "average" in pr[0]["confidence"] and pr[0]["ingredient"]["quantity"] == 500 and pr[0]["ingredient"]["unit"]["id"] and pr[0]["ingredient"]["note"] == "diced" and pr[1]["ingredient"]["food"]["id"] is None, json.dumps(pr[1]["ingredient"]["food"]))
nf, nu = c.get("/api/foods", perPage=-1)["total"], c.get("/api/units", perPage=-1)["total"]
c.update_recipe_ingredients(slug)
full = c.get_recipe(slug)
for i in full["recipeIngredient"]: print("    ", i["quantity"], (i["unit"] or {}).get("name"), "|", (i["food"] or {}).get("name"), "|", i["note"], "=>", i["display"])
nf2, nu2 = c.get("/api/foods", perPage=-1)["total"], c.get("/api/units", perPage=-1)["total"]
ok("ingredients structured, foods created", all(i["food"] and i["food"]["id"] for i in full["recipeIngredient"]) and nf2 > nf, f"foods {nf}->{nf2} units {nu}->{nu2}")
ok("foods fit in perPage=200", nf2 <= 200)
raw = next(r for r in c.list_recipes(per_page=200) if r["name"] == "Vegetable Lasagne")
c.update_recipe_ingredients(raw["slug"])
ok("seeded raw recipe parsed (unit create path)", all(i["food"] for i in c.get_recipe(raw["slug"])["recipeIngredient"]) and c.get("/api/units", perPage=-1)["total"] >= nu2)
bad = c.get_recipe(slug); bad["recipeIngredient"][0]["food"] = {"id": None, "name": "ghost"}
ok("PUT with unknown food rejected", requests.put(f"{B}/api/recipes/{slug}", headers={"Authorization": "Bearer dev"}, json=bad).status_code == 400)

# meal plans
today = date.today()
past = c.list_meal_plans(today - timedelta(days=21), today - timedelta(days=1))
ok("seeded past plans", len([e for e in past if e["entryType"]=="dinner"]) == 21 and any(e["recipe"] is None and e["title"] for e in past), str(len(past)))
fut = c.list_meal_plans(today, today + timedelta(days=13))
ok("seeded future plans", len(fut) == 3 and all(e["recipe"]["id"] for e in fut), str([e["date"] for e in fut]))
d = today + timedelta(days=20)
e = c.add_meal_plan_entry(d, "dinner", recipe_id=r["id"])
ok("add mealplan", e["id"] and e["recipe"]["slug"] == "butter-chicken" and e["date"] == d.isoformat())
e2 = c.add_meal_plan_entry(d, "dinner", title="Out", note="n")
ok("listed", len(c.list_meal_plans(d, d)) == 2)
c.delete_meal_plan_entry(e["id"]); c.delete_meal_plan_entry(e2["id"])
ok("deleted", c.list_meal_plans(d, d) == [])
c.get_this_week(); c.get_upcoming(7)

# shopping
lists = c.list_shopping_lists(); ok("seeded list", len(lists) == 1 and lists[0]["name"] == "Weekly shop")
ok("seeded list items", len(c.get_shopping_list(lists[0]["id"])["listItems"]) > 5)
sl = c.create_shopping_list("Verify list"); lid = sl["id"]
bc, tacos = c.get_recipe("butter-chicken"), c.get_recipe("beef-tacos")
out = c.add_recipe_to_list(lid, bc["id"], 1.0)
n1 = len(c.get_list_items(lid))
ok("add recipe -> items", n1 == len(bc["recipeIngredient"]) == len(out["listItems"]), str(n1))
c.add_recipe_to_list(lid, bc["id"], 0.5)
items = c.get_list_items(lid)
chick = next(i for i in items if (i.get("food") or {}).get("name") == "chicken thighs")
ok("same recipe merges", len(items) == n1 and chick["quantity"] == 900, chick["display"])
c.add_recipe_to_list(lid, tacos["id"], 1.0)
items = c.get_list_items(lid)
onion = next(i for i in items if (i.get("food") or {}).get("name") == "onion")
ok("cross-recipe merge on food+unit", onion["quantity"] == 2.5 and len(onion["recipeReferences"]) == 2 and len(items) < n1 + len(tacos["recipeIngredient"]), f"{len(items)} items; {onion['display']}")
res = c.add_list_item(lid, "Batteries", 2); ok("add item", res["createdItems"][0]["note"] == "Batteries")
before = len(c.get_list_items(lid))
c.delete_list_item(res["createdItems"][0]["id"]); ok("delete item", len(c.get_list_items(lid)) == before - 1)
ok("items scoped to list", all(i["shoppingListId"] == lid for i in c.get_list_items(lid)))
c.delete_shopping_list(lid); ok("delete list", len(c.list_shopping_lists()) == 1 and c.get_list_items(lid) == [])

# images
for name in ("original.webp", "min-original.webp", "tiny-original.webp"):
    resp = requests.get(f"{B}/api/media/recipes/{bc['id']}/images/{name}")
    ok(f"placeholder {name}", resp.status_code == 200 and resp.headers["content-type"].startswith("image/svg+xml"))
from PIL import Image
buf = io.BytesIO(); Image.new("RGB", (40, 30), (200, 80, 40)).save(buf, "PNG")
up = getattr(c, "upload_recipe_image", None) or c._upload_image_bytes
up("butter-chicken", buf.getvalue(), "image/png"); ok("upload (%s)" % up.__name__, True)
resp = requests.get(f"{B}/api/media/recipes/{bc['id']}/images/min-original.webp")
ok("uploaded image served", resp.headers["content-type"] == "image/png" and resp.content == buf.getvalue())
ok("image key changed", c.get_recipe("butter-chicken")["image"] != bc["image"])

# reset
a = requests.post(B + "/__reset").json(); ids1 = [r["id"] for r in c.list_recipes(per_page=200)]
requests.post(B + "/__reset"); ids2 = [r["id"] for r in c.list_recipes(per_page=200)]
ok("reset deterministic", ids1 == ids2 and len(ids1) == 56 and len(c.list_shopping_lists()) == 1, str(a))
print("ALL OK")
