#!/usr/bin/env python3
"""Alerte tournois de padel (Swiss Tennis Padel / stupaevents).

Usage :
    python alerte_padel.py             # envoie les notifications ntfy
    python alerte_padel.py --dry-run   # affiche seulement, sans envoyer ni mémoriser
"""
import json
import math
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests

API = "https://testbackend.stupaevents.com/ott/v1/get_events"
SITE = "https://swisstennis-padel.stupaevents.com/events/1"
HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en",
    "language": "en",
    "source": "web",
    "tenant": "switz",
    "origin": "https://swisstennis-padel.stupaevents.com",
    "referer": "https://swisstennis-padel.stupaevents.com/",
    "user-agent": "Mozilla/5.0 (alerte-padel perso)",
}
ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
SEEN_FILE = ROOT / "seen.json"
GEO_FILE = ROOT / "geocache.json"
JOURS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
STATUS_INSCRIPTION_OUVERTE = 1


# ---------- Récupération des tournois ----------
def fetch_all(season_id, per_page=50, max_pages=40):
    events, page, total = {}, 1, None
    while page <= max_pages:
        params = {
            "published": "true",
            "page_num": page,
            "per_page": per_page,
            "season_ids": season_id,
            "fetch_part_count": "true",
            "sort_by": "relevance",
        }
        r = requests.get(API, params=params, headers=HEADERS, timeout=30)
        r.raise_for_status()
        j = r.json()
        data = j.get("data") or []
        total = j.get("total", total)
        if not data:
            break
        for ev in data:
            events[ev["id"]] = ev
        if total and len(events) >= total:
            break
        page += 1
        time.sleep(0.5)
    return list(events.values())


def field(ev, hint, section):
    """Valeur d'un champ du formulaire du tournoi (extra_json)."""
    for sec in ev.get("extra_json") or []:
        if sec.get("section") != section:
            continue
        for f in sec.get("section_json_with_values") or []:
            if f.get("field_hint") == hint and f.get("field_value"):
                return str(f["field_value"]).strip()
    return ""


# ---------- Localisation ----------
def place_queries(ev):
    addr = field(ev, "Address", 3)
    zip_org = field(ev, "Zip Code", 5)
    loc = field(ev, "Location", 5)
    m = re.search(r"\b(\d{4})\b", addr)
    zip_ = m.group(1) if m else zip_org
    queries = []
    if m:
        queries.append(addr)
    if zip_ and loc:
        queries.append(f"{zip_} {loc}")
    if loc:
        queries.append(loc)
    if addr and addr not in queries:
        queries.append(addr)
    return queries


def geocode_one(query):
    """Essaie le géocodeur fédéral suisse, puis OpenStreetMap."""
    try:
        r = requests.get(
            "https://api3.geo.admin.ch/rest/services/api/SearchServer",
            params={"searchText": query, "type": "locations", "limit": 1, "sr": 4326},
            timeout=20,
        )
        res = r.json().get("results") or []
        if res:
            a = res[0]["attrs"]
            return float(a["lat"]), float(a["lon"])
    except Exception:
        pass
    try:
        time.sleep(1.1)
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "ch"},
            headers={"user-agent": "alerte-padel perso"},
            timeout=20,
        )
        res = r.json()
        if res:
            return float(res[0]["lat"]), float(res[0]["lon"])
    except Exception:
        pass
    return None


def locate(ev, cache):
    queries = place_queries(ev)
    if not queries:
        return None
    key = queries[0].lower()
    if key in cache:
        return tuple(cache[key]) if cache[key] else None
    for q in queries:
        pos = geocode_one(q)
        if pos:
            cache[key] = list(pos)
            return pos
    cache[key] = None
    return None


def km(a, b):
    """Distance à vol d'oiseau en km (haversine)."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


# ---------- Filtres ----------
def matches(ev, cfg, today):
    if ev.get("is_deleted") or not ev.get("published"):
        return False
    if ev.get("event_status") != STATUS_INSCRIPTION_OUVERTE:
        return False
    if ev.get("event_level") not in cfg["levels"]:
        return False
    start = ev.get("event_start_date")
    if not start or start < today.isoformat():
        return False
    name = (ev.get("name") or "").lower()
    if any(k.lower() in name for k in cfg.get("exclude_keywords", [])):
        return False
    return True


# ---------- Messages ----------
def fmt_date(iso):
    d = date.fromisoformat(iso)
    return f"{JOURS[d.weekday()]} {d.day:02d}.{d.month:02d}"


def describe(ev, dist):
    lieu = field(ev, "Location", 5) or field(ev, "Venue", 3) or "?"
    venue = field(ev, "Venue", 3)
    lignes = [ev["name"]]
    lignes.append(f"{fmt_date(ev['event_start_date'])} - {venue + ', ' if venue else ''}{lieu}")
    lignes.append(f"~{dist:.0f} km" if dist is not None else "distance inconnue")
    desc = (ev.get("event_description") or "").strip().splitlines()
    if desc:
        lignes.append(desc[0].strip())
    if ev.get("registered_participants") is not None:
        lignes.append(f"{ev['registered_participants']} inscrit(s)")
    h = (ev.get("registration_closes_in") or {}).get("total_hours")
    if h is not None:
        lignes.append(f"Fin des inscriptions dans ~{h} h")
    return "\n".join(lignes)


def send_ntfy(topic, title, message, dry_run):
    if dry_run or not topic:
        print(f"--- [{'dry-run' if dry_run else 'NTFY_TOPIC absent'}] {title}\n{message}\n")
        return
    r = requests.post(
        "https://ntfy.sh/",
        json={"topic": topic, "title": title, "message": message, "click": SITE, "tags": ["tennis"]},
        timeout=20,
    )
    r.raise_for_status()


# ---------- Programme principal ----------
def main():
    dry_run = "--dry-run" in sys.argv
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    topic = os.environ.get("NTFY_TOPIC", "")
    today = date.today()
    home = (cfg["home"]["lat"], cfg["home"]["lon"])

    events = fetch_all(cfg["season_id"])
    print(f"{len(events)} tournois récupérés")
    candidats = [e for e in events if matches(e, cfg, today)]

    cache = json.loads(GEO_FILE.read_text(encoding="utf-8")) if GEO_FILE.exists() else {}
    retenus = []
    for ev in candidats:
        pos = locate(ev, cache)
        dist = km(home, pos) if pos else None
        if dist is None and not cfg.get("include_unknown_distance", True):
            continue
        if dist is not None and dist > cfg["max_km"]:
            continue
        retenus.append((ev, dist))
    GEO_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    retenus.sort(key=lambda t: t[0]["event_start_date"])
    print(f"{len(retenus)} tournois dans tes critères")

    premier_passage = not SEEN_FILE.exists()
    seen = json.loads(SEEN_FILE.read_text(encoding="utf-8")) if SEEN_FILE.exists() else {}
    seen = {k: v for k, v in seen.items() if v >= today.isoformat()}  # oublie les tournois passés
    nouveaux = [(e, d) for e, d in retenus if str(e["id"]) not in seen]

    if premier_passage:
        lignes = [f"- {fmt_date(e['event_start_date'])} {e['event_level']} {field(e, 'Location', 5)}" for e, _ in retenus[:8]]
        msg = f"{len(retenus)} tournoi(s) correspondent déjà à tes critères.\n" + "\n".join(lignes)
        send_ntfy(topic, "Alerte padel activée", msg, dry_run)
    elif len(nouveaux) > 5:
        lignes = [f"- {fmt_date(e['event_start_date'])} {e['event_level']} {field(e, 'Location', 5)}" for e, _ in nouveaux]
        send_ntfy(topic, f"{len(nouveaux)} nouveaux tournois", "\n".join(lignes), dry_run)
    else:
        for ev, dist in nouveaux:
            send_ntfy(topic, f"Nouveau tournoi {ev['event_level']}", describe(ev, dist), dry_run)

    if not dry_run:
        for ev, _ in retenus:
            seen[str(ev["id"])] = ev["event_start_date"]
        SEEN_FILE.write_text(json.dumps(seen, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
