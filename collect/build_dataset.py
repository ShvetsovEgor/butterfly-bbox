import csv
import json
import random
import re
import subprocess
import time
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dataset"
IMAGES = OUT / "images"
UA = "YSDA-butterfly-bbox/1.0 (course dataset; educational)"
API = "https://api.inaturalist.org/v1/observations"
SEED = 2026

# Квоты подобраны под сравнение детекторов взрослых бабочек.
# role=positive: в кадре ждут взрослую бабочку.
# role=negative: бабочку размечать не должны (моль, гусеница, куколка, другое насекомое, растение, гриб).
# Таксон iNaturalist не равен разметке: перед платными заданиями кадры нужно просмотреть.
BUCKETS = [
    ("nymphalidae", "positive", 50, "Nymphalidae", "adult"),
    ("pieridae", "positive", 40, "Pieridae", "adult"),
    ("lycaenidae", "positive", 45, "Lycaenidae", "adult"),
    ("papilionidae", "positive", 35, "Papilionidae", "adult"),
    ("hesperiidae", "positive", 45, "Hesperiidae", "adult"),
    ("riodinidae", "positive", 35, "Riodinidae", "adult"),
    ("moth_geometridae", "negative", 13, "Geometridae", "adult"),
    ("moth_erebidae", "negative", 13, "Erebidae", "adult"),
    ("moth_sphingidae", "negative", 12, "Sphingidae", "adult"),
    ("moth_saturniidae", "negative", 12, "Saturniidae", "adult"),
    ("larva", "negative", 22, "Lepidoptera", "larva"),
    ("pupa", "negative", 13, "Lepidoptera", "pupa"),
    ("dragonfly", "negative", 12, "Odonata", "adult"),
    ("bee", "negative", 10, "Apidae", "adult"),
    ("ladybird", "negative", 8, "Coccinellidae", "adult"),
    ("hoverfly", "negative", 10, "Syrphidae", "adult"),
    ("flower", "negative", 15, "Asteraceae", None),
    ("fungus", "negative", 10, "Fungi", None),
]

LIFE = {"adult": 2, "pupa": 4, "larva": 6}
KNOWN = {
    "Nymphalidae": 47922,
    "Lycaenidae": 47923,
    "Hesperiidae": 47653,
    "Pieridae": 48508,
    "Papilionidae": 47223,
    "Riodinidae": 59166,
    "Geometridae": 49530,
    "Erebidae": 121850,
    "Sphingidae": 47213,
    "Saturniidae": 47918,
    "Lepidoptera": 47157,
    "Odonata": 47792,
    "Apidae": 47221,
    "Coccinellidae": 48486,
    "Syrphidae": 49995,
    "Asteraceae": 47604,
    "Fungi": 47170,
}


def get_json(url: str) -> dict:
    last = None
    for attempt in range(3):
        try:
            proc = subprocess.run(
                ["curl", "-fsS", "--max-time", "50", "-A", UA, url],
                capture_output=True,
                check=True,
            )
            return json.loads(proc.stdout)
        except Exception as exc:
            last = exc
            err = exc.stderr.decode()[:180] if getattr(exc, "stderr", None) else str(exc)
            print(f"retry {attempt} {err}", flush=True)
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(last)


def resolve_taxon(name: str) -> int:
    if name in KNOWN:
        return KNOWN[name]
    url = "https://api.inaturalist.org/v1/taxa?" + urllib.parse.urlencode(
        {"q": name, "is_active": "true", "per_page": 8}
    )
    payload = get_json(url)
    for item in payload["results"]:
        if item["name"].lower() == name.lower():
            return item["id"]
    raise SystemExit(f"taxon not found: {name}")


def species_key(obs: dict) -> str:
    taxon = obs.get("taxon") or {}
    return taxon.get("name") or str(taxon.get("id") or obs["id"])


def original_url(square: str) -> str:
    return re.sub(r"/square\.", "/original.", square)


def large_url(square: str) -> str:
    return re.sub(r"/square\.", "/large.", square)


def fetch_page(taxon_id: int, life: str | None, level: int, page: int) -> list[dict]:
    # 0 строгий отбор, дальше по одному послаблению. Стадию личинки и куколки не снимаем.
    research = level == 0
    wild = level < 2
    keep_life = life in {"larva", "pupa"} or level < 4
    params = {
        "taxon_id": taxon_id,
        "photos": "true",
        "photo_license": "cc0",
        "per_page": 16,
        "page": page,
        "order_by": "id",
        "order": "desc",
        "locale": "en",
    }
    if research:
        params["quality_grade"] = "research"
    if wild:
        params["captive"] = "false"
    if life and keep_life:
        params["term_id"] = 1
        params["term_value_id"] = LIFE[life]
    url = API + "?" + urllib.parse.urlencode(params)
    try:
        payload = get_json(url)
    except Exception as exc:
        print(f"skip page {exc.__class__.__name__}", flush=True)
        return None
    time.sleep(0.4)
    return payload.get("results") or []


def take_photo(obs: dict, used_photos: set[int]) -> dict | None:
    for photo in obs.get("photos") or []:
        if photo.get("id") in used_photos:
            continue
        if (photo.get("license_code") or "").lower() != "cc0":
            continue
        url = photo.get("url") or ""
        if "/square." not in url:
            continue
        return photo
    return None


def collect_bucket(name, role, target, taxon_id, life, used_photos, used_obs, species_n):
    picked = []
    spare = []
    level_used = 0
    rng = random.Random(f"{SEED}-{name}")
    last_level = 3 if life in {"larva", "pupa"} else 4
    for level in range(last_level + 1):
        page_limit = min(3, max(1, (target + 10) // 11))
        for page in range(1, page_limit + 1):
            print(f"  {name} L{level} p{page}", flush=True)
            rows = fetch_page(taxon_id, life, level, page)
            if rows is None:
                continue
            if not rows:
                break
            rng.shuffle(rows)
            for obs in rows:
                if obs["id"] in used_obs:
                    continue
                key = species_key(obs)
                if species_n[key] >= 6:
                    continue
                photo = take_photo(obs, used_photos)
                if photo is None:
                    continue
                taxon = obs.get("taxon") or {}
                item = {
                    "photo_id": photo["id"],
                    "observation_id": obs["id"],
                    "group": name,
                    "role": role,
                    "taxon_id": taxon.get("id"),
                    "taxon_name": taxon.get("name"),
                    "taxon_rank": taxon.get("rank"),
                    "common_name": taxon.get("preferred_common_name") or "",
                    "license_code": photo.get("license_code"),
                    "attribution": photo.get("attribution") or "",
                    "observer_login": (obs.get("user") or {}).get("login") or "",
                    "observation_url": f"https://www.inaturalist.org/observations/{obs['id']}",
                    "original_url": original_url(photo["url"]),
                    "large_url": large_url(photo["url"]),
                    "filter_level": level,
                    "observed_on": obs.get("observed_on") or "",
                    "place_guess": obs.get("place_guess") or "",
                    "species_key": key,
                }
                picked.append(item)
                used_photos.add(photo["id"])
                used_obs.add(obs["id"])
                species_n[key] += 1
                if len(picked) >= target:
                    return picked, spare, level
            level_used = level
            if len(picked) >= target:
                return picked, spare, level
        if len(picked) >= target:
            break
    return picked, spare, level_used


def download_one(item: dict) -> dict:
    last = None
    for url in (item["large_url"], item["original_url"]):
        try:
            proc = subprocess.run(
                ["curl", "-fsS", "--max-time", "40", "-A", UA, url],
                capture_output=True,
                check=True,
            )
            data = proc.stdout
            if data[:3] == b"\xff\xd8\xff":
                ext = ".jpg"
            elif data[:8] == b"\x89PNG\r\n\x1a\n":
                ext = ".png"
            else:
                raise ValueError(f"bad image {len(data)}")
            if len(data) < 4000:
                raise ValueError(f"too small {len(data)}")
            dest = IMAGES / f"{item['photo_id']}{ext}"
            dest.write_bytes(data)
            item["local_path"] = str(dest.relative_to(OUT))
            item["bytes"] = len(data)
            item["variant"] = "large" if url == item["large_url"] else "original"
            width, height = image_size(dest)
            item["width"] = width
            item["height"] = height
            return item
        except Exception as exc:
            last = exc
    item["error"] = str(last)
    return item


def image_size(path: Path) -> tuple[int | None, int | None]:
    try:
        from PIL import Image

        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None, None


def main() -> None:
    IMAGES.mkdir(parents=True, exist_ok=True)
    taxa = {name: resolve_taxon(name) for name in sorted({row[3] for row in BUCKETS})}
    print("taxa", taxa, flush=True)
    used_photos: set[int] = set()
    used_obs: set[int] = set()
    species_n: dict[str, int] = defaultdict(int)
    checkpoint = OUT / "partial.json"
    selected = []
    spares = []
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text())
        selected = saved.get("selected") or []
        spares = saved.get("spares") or []
        for item in selected:
            used_photos.add(item["photo_id"])
            used_obs.add(item["observation_id"])
            species_n[item["species_key"]] += 1
        print(f"resume {len(selected)}", flush=True)
    done_groups = {item["group"] for item in selected}
    for name, role, target, taxon_name, life in BUCKETS:
        if name in done_groups and sum(1 for item in selected if item["group"] == name) >= target:
            print(f"{name} cached", flush=True)
            continue
        picked, spare, level = collect_bucket(
            name, role, target, taxa[taxon_name], life, used_photos, used_obs, species_n
        )
        print(f"{name} {len(picked)}/{target} filter {level} spare {len(spare)}", flush=True)
        selected.extend(picked)
        spares.extend(spare)
        checkpoint.write_text(json.dumps({"selected": selected, "spares": spares}, ensure_ascii=False))

    print("download", len(selected), flush=True)
    done = []
    failed = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(download_one, item) for item in selected]
        for fut in as_completed(futures):
            item = fut.result()
            if item.get("error"):
                failed.append(item)
            else:
                done.append(item)
            if (len(done) + len(failed)) % 40 == 0:
                print(f"saved {len(done)} failed {len(failed)}", flush=True)

    if failed and spares:
        print("replace", len(failed), flush=True)
        for bad in failed:
            species_n[bad["species_key"]] = max(0, species_n[bad["species_key"]] - 1)
        need = len(failed)
        replacements = []
        for item in spares:
            if item["photo_id"] in used_photos or item["observation_id"] in {row["observation_id"] for row in done}:
                continue
            if species_n[item["species_key"]] >= 6:
                continue
            replacements.append(item)
            used_photos.add(item["photo_id"])
            species_n[item["species_key"]] += 1
            if len(replacements) >= need:
                break
        with ThreadPoolExecutor(max_workers=8) as pool:
            for item in pool.map(download_one, replacements):
                if item.get("error"):
                    failed.append(item)
                else:
                    done.append(item)

    done.sort(key=lambda row: (row["role"], row["group"], row["photo_id"]))
    fields = [
        "photo_id",
        "observation_id",
        "group",
        "role",
        "taxon_id",
        "taxon_name",
        "taxon_rank",
        "common_name",
        "license_code",
        "attribution",
        "observer_login",
        "observation_url",
        "original_url",
        "large_url",
        "local_path",
        "width",
        "height",
        "bytes",
        "variant",
        "filter_level",
        "observed_on",
        "place_guess",
    ]
    with (OUT / "metadata.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(done)
    counts = defaultdict(int)
    for row in done:
        counts[row["group"]] += 1
    summary = {
        "photos": len(done),
        "failed": len([item for item in failed if item.get("error")]),
        "by_group": dict(counts),
        "positive": sum(1 for row in done if row["role"] == "positive"),
        "negative": sum(1 for row in done if row["role"] == "negative"),
        "note": "Таксон и стадия iNaturalist не заменяют разметку. Перед запуском заданий кадры нужно просмотреть.",
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
