import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
# ForexFactory blocks clients that poll this feed too often
REFRESH_SECONDS = 4 * 3600
RETRY_SECONDS = 15 * 60
IMPACTS = {"high": {"High"}, "high_medium": {"High", "Medium"}}

_lock = threading.Lock()
_last_attempt = 0.0


def parse_countries(text: str) -> set[str]:
    return {c.strip().upper() for c in str(text).split(",") if c.strip()}


def validate_countries(text: str) -> str:
    countries = parse_countries(text)
    if not countries or not all(re.fullmatch(r"[A-Z]{3}", c) for c in countries):
        raise ValueError("Países: use códigos de moeda separados por vírgula, como USD ou USD,EUR")
    return ",".join(sorted(countries))


def _read_cache(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _parse(raw: dict) -> dict | None:
    try:
        when = datetime.fromisoformat(raw["date"])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "time": int(when.timestamp()),
        "title": raw.get("title", ""),
        "country": raw.get("country", ""),
        "impact": raw.get("impact", ""),
        "forecast": raw.get("forecast", ""),
        "previous": raw.get("previous", ""),
    }


def load_events(cache_file: Path) -> tuple[list[dict], float]:
    global _last_attempt
    with _lock:
        cache = _read_cache(cache_file)
        fetched_at = cache["fetched_at"] if cache else 0.0
        now = time.time()
        if now - fetched_at > REFRESH_SECONDS and now - _last_attempt > RETRY_SECONDS:
            _last_attempt = now
            try:
                resp = requests.get(FEED_URL, timeout=15, headers={"User-Agent": "robo-trader"})
                resp.raise_for_status()
                cache = {"fetched_at": now, "events": resp.json()}
                cache_file.write_text(json.dumps(cache), encoding="utf-8")
            except (requests.RequestException, ValueError, OSError):
                pass
        if not cache:
            return [], 0.0
        events = [e for e in (_parse(r) for r in cache["events"]) if e]
        return sorted(events, key=lambda e: e["time"]), cache["fetched_at"]


def relevant(events: list[dict], countries: set[str], impact: str) -> list[dict]:
    allowed = IMPACTS.get(impact, IMPACTS["high"])
    return [e for e in events if e["country"] in countries and e["impact"] in allowed]


def blocking_event(events: list[dict], now: float, before_min: int, after_min: int) -> dict | None:
    for e in events:
        if e["time"] - before_min * 60 <= now <= e["time"] + after_min * 60:
            return e
    return None
