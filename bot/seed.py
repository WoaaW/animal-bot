import asyncio
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from sqlalchemy import select

from .db import SessionLocal, init_db
from .cogs.rolls import IUCN_RARITY, RARITY_ODDS
from .models import Animal

WIKIPEDIA_API = "https://fr.wikipedia.org/w/api.php"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
USER_AGENT = "animal-bot/0.1 (seed-script)"
# Fields filled from Wikipedia/Wikidata unless animals.json sets them explicitly.
WIKI_FIELDS = ("description", "image_url", "wiki_url", "iucn_status", "taxa")
# Wikidata items used as values of P141 ("IUCN conservation status").
# Endangered shows up under two items depending on who edited the species.
IUCN_ITEMS = {
    "Q211005": "LC",
    "Q719675": "NT",
    "Q278113": "VU",
    "Q96377276": "EN",
    "Q11394": "EN",
    "Q219127": "CR",
    "Q239509": "EW",
    "Q237350": "EX",
    "Q3245245": "DD",
}


def _get_json(url: str) -> dict:
    # Wikipedia answers bursts of requests with HTTP 429, even when spaced
    # out by a second, so back off and retry instead of giving up.
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, 5):
        time.sleep(1)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # taxonomy queries can take a while
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 4:
                raise
            wait = int(exc.headers.get("Retry-After") or 5 * attempt)
            print(f"  rate-limited, retrying in {wait}s")
            time.sleep(wait)
    raise AssertionError("unreachable")


# Wikidata ranks (P105) whose taxa go into Animal.taxa: family, order, class.
TAXON_RANKS = ("Q35409", "Q36602", "Q37517")

# TextExtracts returns at most 20 intro extracts per request.
WIKIPEDIA_BATCH = 20
# Longest side of the stored image, in px. Embeds never show it much bigger, and
# originals can weigh several MB, which Discord is slow to load. Wikimedia serves
# thumbnails in fixed size steps (…, 500, 960, 1280…): stick to one of them.
IMAGE_SIZE = 960
# A Wikimedia Commons original: upload.wikimedia.org/wikipedia/commons/a/ab/File.jpg
COMMONS_ORIGINAL = re.compile(r"^https://upload\.wikimedia\.org/wikipedia/commons/([0-9a-f])/([0-9a-f]{2})/([^/?#]+)")


THUMB_HOST = "https://thumb.wikimedia.org"


def thumbnail_url(url: str) -> str:
    """The IMAGE_SIZE thumbnail of a Commons image, served from thumb.wikimedia.org;
    other URLs unchanged except for the API's utm_* tracking query. The API
    hands back the original (on upload.wikimedia.org) when it's smaller than
    IMAGE_SIZE, and Discord fails to show those, while thumbnails from
    thumb.wikimedia.org load fine (even upscaled past the original)."""
    url = url.split("?", 1)[0]
    if url.startswith("https://upload.wikimedia.org/wikipedia/commons/thumb/"):
        return THUMB_HOST + url.removeprefix("https://upload.wikimedia.org")
    match = COMMONS_ORIGINAL.match(url)
    if match is None:
        return url
    a, ab, name = match.groups()
    # SVGs and TIFFs are rendered: their thumbnails are PNG / JPEG files.
    suffix = {".svg": ".png", ".tif": ".jpg", ".tiff": ".jpg"}.get(Path(name).suffix.lower(), "")
    return f"{THUMB_HOST}/wikipedia/commons/thumb/{a}/{ab}/{name}/{IMAGE_SIZE}px-{name}{suffix}"


WIKIDATA_BATCH = 200
# Walking up the taxonomy (P171*) is heavier, so fewer items per query.
TAXA_BATCH = 100


def _batches(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _title(scientific_name: str) -> str:
    # Titles are case-sensitive after the first letter, so sentence-case it.
    return scientific_name[:1].upper() + scientific_name[1:].lower()


def fetch_wikipedia(scientific_names: list[str]) -> dict[str, dict[str, str | None]]:
    """Wikipedia + Wikidata fields for each binomial, batched to keep the
    request count (and so the rate limiting) low. Names not found are absent."""
    # Look up by binomial name: unlike common names it never lands on a
    # disambiguation page, and French Wikipedia redirects it to the species
    # article (e.g. "Inia geoffrensis" -> "Boto").
    results: dict[str, dict[str, str | None]] = {}
    item_ids: dict[str, str] = {}
    for batch in _batches(scientific_names, WIKIPEDIA_BATCH):
        print(f"  fetching Wikipedia ({len(results)}/{len(scientific_names)} done)...")
        pages, renames = _query_pages([_title(n) for n in batch])
        for name in batch:
            title = _title(name)
            # Follow normalization then redirects, as the API reports them.
            title = renames.get(title, title)
            title = renames.get(title, title)
            page = pages.get(title)
            if page is None or page.get("missing"):
                print(f"  warn: no French Wikipedia article for {name}")
                continue
            results[name] = {
                "description": page.get("extract", "").strip() or None,
                # Smaller images come back as the original.
                "image_url": (page.get("thumbnail") or {}).get("source"),
                "wiki_url": page.get("fullurl"),
                "iucn_status": None,
                "taxa": None,
            }
            if item := page.get("pageprops", {}).get("wikibase_item"):
                item_ids[name] = item

    print(f"  fetching IUCN statuses for {len(item_ids)} items...")
    statuses = fetch_iucn_statuses(list(set(item_ids.values())))
    print(f"  fetching taxonomy for {len(item_ids)} items...")
    taxa = fetch_taxa(list(set(item_ids.values())))
    for name, item in item_ids.items():
        results[name]["iucn_status"] = statuses.get(item)
        results[name]["taxa"] = taxa.get(item)
    return results


def _query_pages(titles: list[str]) -> tuple[dict[str, dict], dict[str, str]]:
    """Pages by final title, plus the title renames (normalized/redirected)."""
    params = {
        "action": "query",
        "format": "json",
        "formatversion": 2,
        "redirects": 1,
        "prop": "extracts|pageimages|info|pageprops",
        "exintro": 1,          # only the lead section, before the first heading
        "explaintext": 1,
        "exlimit": "max",
        "piprop": "thumbnail",
        "pithumbsize": IMAGE_SIZE,
        "inprop": "url",
        "ppprop": "wikibase_item",
        "titles": "|".join(titles),
    }
    pages: dict[str, dict] = {}
    renames: dict[str, str] = {}
    cont: dict = {}
    # A batch can come back in several parts ("continue"), each filling in
    # some props; merge them by title.
    while True:
        try:
            data = _get_json(f"{WIKIPEDIA_API}?{urllib.parse.urlencode({**params, **cont})}")
        except Exception as exc:
            print(f"  warn: couldn't fetch Wikipedia batch: {exc}")
            return pages, renames
        query = data.get("query", {})
        for rename in query.get("normalized", []) + query.get("redirects", []):
            renames[rename["from"]] = rename["to"]
        for page in query.get("pages", []):
            pages.setdefault(page["title"], {}).update(page)
        if "continue" not in data:
            return pages, renames
        cont = data["continue"]


def fetch_iucn_statuses(item_ids: list[str]) -> dict[str, str]:
    """IUCN code by Wikidata item id, via SPARQL (one request per batch,
    instead of one API call per item)."""
    statuses: dict[str, str] = {}
    for batch in _batches(item_ids, WIKIDATA_BATCH):
        sparql = (
            "SELECT ?item ?status ?rank WHERE { VALUES ?item { "
            + " ".join(f"wd:{i}" for i in batch)
            + " } ?item p:P141 ?st . ?st ps:P141 ?status ; wikibase:rank ?rank . }"
        )
        query = urllib.parse.urlencode({"query": sparql, "format": "json"})
        try:
            rows = _get_json(f"{WIKIDATA_SPARQL}?{query}")["results"]["bindings"]
        except Exception as exc:
            print(f"  warn: couldn't fetch IUCN statuses: {exc}")
            continue
        # Old assessments stay on the item as "deprecated"; the current one is
        # "preferred" when there are several, otherwise the only "normal" one.
        best: dict[str, tuple[bool, str]] = {}
        for row in rows:
            rank = row["rank"]["value"].rsplit("#", 1)[-1]
            code = IUCN_ITEMS.get(row["status"]["value"].rsplit("/", 1)[-1])
            if rank == "DeprecatedRank" or code is None:
                continue
            item = row["item"]["value"].rsplit("/", 1)[-1]
            preferred = rank == "PreferredRank"
            if item not in best or (preferred and not best[item][0]):
                best[item] = (preferred, code)
        statuses.update({item: code for item, (_, code) in best.items()})
    return statuses


def fetch_taxa(item_ids: list[str]) -> dict[str, str]:
    """"|Family|Order|Class|" by Wikidata item id. Going through the item of
    the French article means synonyms don't matter (unlike matching P225)."""
    taxa: dict[str, set[str]] = {}
    for batch in _batches(item_ids, TAXA_BATCH):
        sparql = (
            "SELECT ?item ?name WHERE { VALUES ?item { "
            + " ".join(f"wd:{i}" for i in batch)
            + " } VALUES ?rank { "
            + " ".join(f"wd:{r}" for r in TAXON_RANKS)
            # "?item wdt:P225 ?sci" looks useless but anchors the items: without
            # it the query planner walks P171* backwards from every taxon of
            # those ranks and times out (60 s) instead of answering in ~1 s.
            + " } ?item wdt:P225 ?sci . ?item wdt:P171* ?taxon . ?taxon wdt:P105 ?rank ; wdt:P225 ?name . }"
        )
        query = urllib.parse.urlencode({"query": sparql, "format": "json"})
        try:
            rows = _get_json(f"{WIKIDATA_SPARQL}?{query}")["results"]["bindings"]
        except Exception as exc:
            print(f"  warn: couldn't fetch taxonomy: {exc}")
            continue
        for row in rows:
            item = row["item"]["value"].rsplit("/", 1)[-1]
            # Some taxa carry an empty P225 next to the real one.
            if name := row["name"]["value"].strip():
                taxa.setdefault(item, set()).add(name)
    return {item: "|" + "|".join(sorted(names)) + "|" for item, names in taxa.items()}


async def seed() -> None:
    await init_db()
    data = json.loads((Path(__file__).parent / "data" / "animals.json").read_text())
    # A bad rarity would only blow up later inside /rescue, so fail fast here.
    bad = [e["name"] for e in data if "rarity" in e and e["rarity"] not in RARITY_ODDS]
    if bad:
        raise SystemExit(f"Unknown rarity for: {', '.join(bad)} (expected one of {list(RARITY_ODDS)})")
    async with SessionLocal() as session:
        existing = {
            a.name: a
            for a in (await session.execute(select(Animal))).scalars().all()
        }
        added = updated = 0

        # Fetch everything up front, in batches: only animals with a field
        # that's neither in the JSON nor already stored, so re-seeding
        # doesn't re-fetch.
        def needs_fetch(entry: dict) -> bool:
            current = existing.get(entry["name"])
            return any(not entry.get(f) and not (current and getattr(current, f)) for f in WIKI_FIELDS)

        to_fetch = [e["scientific_name"] for e in data if needs_fetch(e)]
        fetched_all = fetch_wikipedia(to_fetch) if to_fetch else {}

        for entry in data:
            name = entry["name"]
            current = existing.get(name)
            is_new = current is None
            if is_new:
                # Not added to the session until we know its rarity.
                current = Animal(name=name)

            changed = []
            if current.scientific_name != entry["scientific_name"]:
                current.scientific_name = entry["scientific_name"]
                changed.append("scientific_name")

            # JSON values win; fetched ones only fill fields still empty.
            fetched = fetched_all.get(entry["scientific_name"], {})
            for field in WIKI_FIELDS:
                value = entry.get(field)
                if not value and not getattr(current, field):
                    value = fetched.get(field)
                if field == "image_url":
                    # Also rewrites URLs stored before thumbnail_url existed.
                    value = value or current.image_url
                    value = value and thumbnail_url(value)
                if value and getattr(current, field) != value:
                    setattr(current, field, value)
                    changed.append(field)

            missing = [f for f in WIKI_FIELDS if not getattr(current, f)]
            if missing:
                print(f"  warn: {name} has no {', '.join(missing)}")

            # An explicit rarity wins (domestic animals, DD species...);
            # otherwise it follows the IUCN status.
            rarity = entry.get("rarity") or IUCN_RARITY.get(current.iucn_status)
            if rarity is None:
                print(f"  error: {name} has no usable IUCN status ({current.iucn_status}); set \"rarity\" in animals.json")
                if is_new:
                    continue
            elif current.rarity != rarity:
                if not is_new:
                    print(f"  ~ {name}: {current.rarity} -> {rarity}")
                current.rarity = rarity
                changed.append("rarity")

            if is_new:
                session.add(current)
                print(f"  + {name} ({entry['scientific_name']}, {current.iucn_status}, {current.rarity})")
                added += 1
            else:
                updated += len(changed)

        await session.commit()
        print(f"Seed complete: {added} added, {updated} fields updated.")


if __name__ == "__main__":
    asyncio.run(seed())
