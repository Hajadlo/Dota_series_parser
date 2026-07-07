import bz2
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import gevent
import requests
import streamlit as st
from dota2.client import Dota2Client
from gevent.event import AsyncResult
from steam.client import SteamClient
from steam.enums import EResult
from steam.enums.emsg import EMsg

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Dota 2 Series Analyzer", layout="wide")

# OpenDota base URL is overridable for local testing of outage scenarios.
OPENDOTA_BASE = os.environ.get(
    "OPENDOTA_BASE_OVERRIDE",
    "https://api.opendota.com/api",
)
STEAM_API_BASE = "https://api.steampowered.com"
VALVE_REPLAY_URL = "http://replay{cluster}.valve.net/570/{match_id}_{replay_salt}.dem.bz2"

# Path to the fat JAR — relative to this file
_HERE = os.path.dirname(os.path.abspath(__file__))
JAR_PATH = os.path.join(_HERE, "clarity_parser", "build", "libs", "kill_extractor.jar")


def load_local_env() -> None:
    env_path = os.path.join(_HERE, ".env")
    if not os.path.isfile(env_path):
        return

    try:
        with open(env_path, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key or key in os.environ:
                    continue
                value = value.strip().strip('"').strip("'")
                os.environ[key] = value
    except OSError:
        pass


load_local_env()

# Team colours
RADIANT_COLOR = "#4caf50"
DIRE_COLOR = "#e05c5c"

# Interval-market colours (trader convention: Home & Under = blue, Away & Over = orange)
HOME_UNDER_COLOR = "#4c8bf5"
AWAY_OVER_COLOR = "#ff9800"

# Interval markets are defined for game-clock windows [0-10), [10-20), ... [80-90).
INTERVAL_SECONDS = 600
MAX_INTERVALS = 9  # 0-10 .. 80-90

# Power-rune markets are defined every 2 minutes from 6:00. Keep known labels
# exactly as the book strings. Unknown future river-rune enum ids are surfaced as
# rune_<id> by the Java extractor and appended dynamically when observed.
RUNE_SPAWN_SECONDS = 120
RUNE_FIRST_SPAWN_SECONDS = 360
RUNE_TYPES = [
    "arcane",
    "double_damage",
    "haste",
    "illusion",
    "invisibility",
    "regeneration",
    "shield",
]
EXCLUDED_RUNE_TYPES = {"bounty", "water", "wisdom"}
RUNE_SIDES = ["top", "bot"]

# Map Networth Leader At Time markets settle at fixed game-clock snapshots.
NETWORTH_LEADER_SECONDS = (300, 600, 900)


def _normalize_rune_type(value) -> str:
    return str(value or "").strip().lower()


def _is_rune_type_label(value) -> bool:
    rune_type = _normalize_rune_type(value)
    return (
        bool(rune_type)
        and rune_type not in EXCLUDED_RUNE_TYPES
        and (rune_type in RUNE_TYPES or re.fullmatch(r"rune_\d+", rune_type) is not None)
    )


def _rune_type_options(spawns: list[dict] | None = None, extra: list | None = None) -> list[str]:
    """Known rune labels plus any observed future rune labels, preserving order."""
    options = list(RUNE_TYPES)
    for value in list(extra or []) + [s.get("rune_type") for s in (spawns or [])]:
        rune_type = _normalize_rune_type(value)
        if _is_rune_type_label(rune_type) and rune_type not in options:
            options.append(rune_type)
    return options


# ── Helpers ────────────────────────────────────────────────────────────────────

def towers_from_status(tower_status: int) -> int:
    """Return the number of towers destroyed from an 11-bit bitmask.
    Each bit = 1 means that tower is still standing, so destroyed = 11 - popcount."""
    return 11 - bin(tower_status).count("1")


def barracks_from_status(barracks_status: int) -> int:
    """Return the number of barracks destroyed from a 6-bit bitmask.
    Each bit = 1 means that barracks is still standing, so destroyed = 6 - popcount."""
    return 6 - bin(barracks_status).count("1")


def parse_match_id(url: str) -> str | None:
    """Extract a Dota 2 match ID from a URL or accept a raw numeric match ID."""
    raw = url.strip()
    if raw.isdigit():
        return raw
    m = re.search(r"/matches/(\d+)", raw)
    if m:
        return m.group(1)
    m = re.search(r"/570/(\d+)_\d+\.dem\.bz2", raw)
    return m.group(1) if m else None


_SECRET_QUERY_PARAM_RE = re.compile(
    r"(?i)([?&])(api_key|key|access_token|token|password)=[^&#\s]+"
)


def safe_error_str(exc: object) -> str:
    """Redact common secret-bearing query params from an exception's string form.

    Streamlit renders `st.error(f\"... {exc}\")` directly in the public UI, and
    `requests.HTTPError` messages include the full URL — which may contain
    `?api_key=...`. This strips those values before display.
    """
    return _SECRET_QUERY_PARAM_RE.sub(r"\1\2=***", str(exc))


def team_span(name: str, is_radiant: bool) -> str:
    color = RADIANT_COLOR if is_radiant else DIRE_COLOR
    return f"<span style='color:{color}'>{name}</span>"


def result_label(event: dict | None, radiant_name: str, dire_name: str) -> str:
    if event is None:
        return "N/A"
    name = radiant_name if event["is_radiant"] else dire_name
    color = RADIANT_COLOR if event["is_radiant"] else DIRE_COLOR
    return f"<span style='color:{color}'>{name}</span>"


# ── OpenDota API calls ─────────────────────────────────────────────────────────

def _opendota_get_with_retry(url: str, *, timeout: float = 10.0):
    """GET against OpenDota with one quick retry on transient network errors.

    A single short backoff covers brief blips without prolonging UI waits when
    OpenDota is genuinely down — in that case we want to fall through to the
    Steam fallback chain quickly.
    """
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            return requests.get(url, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(0.5)
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("OpenDota request failed without raising")  # defensive


def fetch_match_uncached(match_id: str) -> dict:
    resp = _opendota_get_with_retry(
        f"{OPENDOTA_BASE}/matches/{match_id}",
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def normalize_match_payload(match_id: str, match: dict, source: str) -> dict:
    normalized = dict(match)
    normalized["match_id"] = str(normalized.get("match_id") or match_id)

    radiant_name = normalized.get("radiant_name") or normalized.get("radiant_team_name")
    dire_name = normalized.get("dire_name") or normalized.get("dire_team_name")
    if radiant_name and not isinstance(normalized.get("radiant_team"), dict):
        normalized["radiant_team"] = {"name": radiant_name}
    if dire_name and not isinstance(normalized.get("dire_team"), dict):
        normalized["dire_team"] = {"name": dire_name}

    normalized["_match_source"] = source
    return normalized


@st.cache_data(show_spinner=False, ttl=60)
def fetch_match(match_id: str) -> dict:
    try:
        return normalize_match_payload(match_id, fetch_match_uncached(match_id), "OpenDota")
    except Exception as opendota_exc:
        valve_match = fetch_valve_match_details(match_id)
        if valve_match:
            normalized = normalize_match_payload(match_id, valve_match, "Valve")
            normalized["_opendota_error"] = str(opendota_exc)
            return normalized
        raise


def get_setting(name: str) -> str:
    try:
        value = st.secrets.get(name)
        if value:
            return str(value).strip()
    except Exception:
        pass
    return os.environ.get(name, "").strip()


def has_valve_fallback_credentials() -> bool:
    return bool(get_setting("STEAM_API_KEY")) or (
        bool(get_setting("BOT_STEAM_USERNAME"))
        and bool(get_setting("BOT_STEAM_PASSWORD"))
    )


def build_valve_replay_url(
    match_id: str,
    cluster: int | str | None,
    replay_salt: int | str | None,
) -> str | None:
    if cluster in (None, "", 0, "0") or replay_salt in (None, "", 0, "0"):
        return None
    return VALVE_REPLAY_URL.format(
        cluster=cluster,
        match_id=match_id,
        replay_salt=replay_salt,
    )


def gc_match_to_dict(match) -> dict:
    tower_status = list(getattr(match, "tower_status", []))
    barracks_status = list(getattr(match, "barracks_status", []))

    return {
        "match_id": str(getattr(match, "match_id", 0)),
        "duration": int(getattr(match, "duration", 0)),
        "start_time": int(getattr(match, "startTime", 0)),
        "cluster": int(getattr(match, "cluster", 0)),
        "replay_salt": int(getattr(match, "replay_salt", 0)),
        "leagueid": int(getattr(match, "leagueid", 0)),
        "series_id": int(getattr(match, "series_id", 0)),
        "radiant_team_id": int(getattr(match, "radiant_team_id", 0)),
        "dire_team_id": int(getattr(match, "dire_team_id", 0)),
        "radiant_name": getattr(match, "radiant_team_name", "") or "Radiant",
        "dire_name": getattr(match, "dire_team_name", "") or "Dire",
        "radiant_score": int(getattr(match, "radiant_team_score", 0)),
        "dire_score": int(getattr(match, "dire_team_score", 0)),
        "radiant_win": int(getattr(match, "match_outcome", 0)) == 2,
        "tower_status_radiant": int(tower_status[0]) if len(tower_status) > 0 else 2047,
        "tower_status_dire": int(tower_status[1]) if len(tower_status) > 1 else 2047,
        "barracks_status_radiant": int(barracks_status[0]) if len(barracks_status) > 0 else 63,
        "barracks_status_dire": int(barracks_status[1]) if len(barracks_status) > 1 else 63,
    }


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_gc_match_details(match_id: str) -> dict:
    username = get_setting("BOT_STEAM_USERNAME")
    password = get_setting("BOT_STEAM_PASSWORD")
    if not username or not password:
        return {}

    steam_client = SteamClient()
    dota_client = Dota2Client(steam_client)
    sentry_dir = os.path.join(_HERE, "sentry")
    os.makedirs(sentry_dir, exist_ok=True)
    steam_client.set_credential_location(sentry_dir)
    response = AsyncResult()
    requested = {"done": False}

    def _request_match_details_once():
        if requested["done"] or response.ready():
            return
        requested["done"] = True
        dota_client.request_match_details(int(match_id))

    @steam_client.on("logged_on")
    def _on_logged_on():
        # Start the Dota GC handshake as soon as Steam login completes. We also
        # call launch() after spawning run_forever below, because steam-client
        # can emit logged_on before the greenlet is fully pumping messages.
        dota_client.launch()

    @steam_client.on("disconnected")
    def _on_disconnected():
        if not response.ready():
            response.set(("steam_disconnected", None, None))

    @steam_client.on(EMsg.ClientLoggedOff)
    def _on_logged_off(_msg):
        if not response.ready():
            response.set(("steam_logged_off", None, None))

    @steam_client.on("error")
    def _on_steam_error(result):
        if not response.ready():
            response.set(("steam_error", result, None))

    @dota_client.on("ready")
    def _on_gc_ready():
        _request_match_details_once()

    @dota_client.on("match_details")
    def _on_match_details(returned_match_id, eresult, match):
        if int(returned_match_id) == int(match_id) and not response.ready():
            response.set(("match_details", eresult, match))

    login_result = steam_client.login(username, password)
    if login_result != EResult.OK:
        return {}

    runner = gevent.spawn(steam_client.run_forever)
    try:
        gevent.sleep(0.5)
        dota_client.launch()
        if dota_client.ready:
            _request_match_details_once()

        kind, status, match = response.get(timeout=75)
        if kind != "match_details" or status != EResult.OK or match is None:
            return {}
        return gc_match_to_dict(match)
    except Exception:
        return {}
    finally:
        try:
            steam_client.disconnect()
        except Exception:
            pass
        gevent.sleep(0.5)
        try:
            runner.kill(block=False)
        except Exception:
            pass


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_valve_match_details(match_id: str) -> dict:
    api_key = get_setting("STEAM_API_KEY")
    if api_key:
        try:
            resp = requests.get(
                f"{STEAM_API_BASE}/IDOTA2Match_570/GetMatchDetails/v1",
                params={"key": api_key, "match_id": match_id},
                timeout=10,
            )
            if resp.status_code == 200:
                result = resp.json().get("result", {})
                if isinstance(result, dict) and isinstance(result.get("match"), dict):
                    result = result["match"]
                if isinstance(result, dict) and result:
                    return result
        except Exception:
            pass

    return fetch_gc_match_details(match_id)


@st.cache_data(show_spinner=False, ttl=60)
def replay_url_exists(replay_url: str) -> bool:
    try:
        resp = requests.head(replay_url, allow_redirects=True, timeout=10)
        if resp.status_code == 200:
            return True
        if resp.status_code not in (403, 405):
            return False
    except Exception:
        pass

    try:
        resp = requests.get(
            replay_url,
            headers={"Range": "bytes=0-0"},
            stream=True,
            timeout=10,
        )
        try:
            return resp.status_code in (200, 206)
        finally:
            resp.close()
    except Exception:
        return False


def resolve_replay_url(
    match_id: str,
    match: dict | None = None,
    *,
    queue_opendota_parse: bool = False,
) -> tuple[str | None, str | None, str | None]:
    match_data = match or fetch_match(match_id)

    replay_url = match_data.get("replay_url")
    if replay_url and replay_url_exists(replay_url):
        return replay_url, "OpenDota", None

    if queue_opendota_parse:
        request_opendota_parse(match_id)

    replay_url = build_valve_replay_url(
        match_id,
        match_data.get("cluster"),
        match_data.get("replay_salt"),
    )
    if replay_url:
        if replay_url_exists(replay_url):
            return replay_url, "Valve CDN (via OpenDota match data)", None
        # If OpenDota already gave replay coordinates, the authenticated
        # Valve/GC fallback is very unlikely to discover a different URL; it
        # normally returns the same cluster/salt and can block for ~45s. Fail
        # fast so the UI can render match basics instead of sitting on a
        # spinner when the CDN returns 5xx for this replay.
        return (
            None,
            None,
            "Replay metadata exists, but Valve CDN is not serving the replay right now. Try again later.",
        )

    valve_match = fetch_valve_match_details(match_id)
    replay_url = build_valve_replay_url(
        match_id,
        valve_match.get("cluster"),
        valve_match.get("replay_salt"),
    )
    if replay_url and replay_url_exists(replay_url):
        return replay_url, "Valve CDN", None

    if not has_valve_fallback_credentials():
        return (
            None,
            None,
            "Replay not available yet. OpenDota has no replay URL and no Valve fallback credentials are configured.",
        )

    return (
        None,
        None,
        "Replay not available yet. Valve does not expose a downloadable replay for this match yet.",
    )


def _load_heroes_fallback_snapshot() -> dict:
    """Load the bundled hero-id → npc_name snapshot used when OpenDota is down.

    Returns an empty dict if the file is missing or unreadable; callers should
    treat that as a non-fatal degraded state (hero names render as raw npc_*
    strings rather than crashing).
    """
    fallback_path = os.path.join(_HERE, "heroes_fallback.json")
    try:
        with open(fallback_path, encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_heroes() -> dict:
    """Return {hero_id (str) -> npc_name} mapping.

    Falls back to a bundled static snapshot when OpenDota is unreachable —
    hero IDs/names change roughly twice a year, so a snapshot is acceptable
    while the API is down.
    """
    try:
        resp = _opendota_get_with_retry(f"{OPENDOTA_BASE}/heroes", timeout=5)
        resp.raise_for_status()
        heroes = resp.json()
        return {str(h["id"]): h["name"] for h in heroes}
    except Exception:
        snapshot = _load_heroes_fallback_snapshot()
        if snapshot:
            try:
                st.sidebar.warning(
                    "Hero data is from a bundled snapshot — OpenDota /heroes is unreachable."
                )
            except Exception:
                # st.sidebar may not be ready when called outside the script run.
                pass
            return snapshot
        raise


@st.cache_data(show_spinner=False, ttl=60)
def fetch_series_matches(match: dict) -> tuple[list[dict], bool]:
    """
    Return (matches, degraded) where matches are all matches in the series,
    sorted by start_time ascending, and `degraded` is True when every OpenDota
    series-discovery method failed (network/5xx) so we're returning only the
    anchor match. Tries multiple methods because OpenDota's series grouping
    can be delayed. Each match item: {"match_id": str, "label": "Map 1", ...}
    """
    league_id = match.get("leagueid")
    series_id = match.get("series_id")
    radiant_team_id = match.get("radiant_team_id")
    dire_team_id = match.get("dire_team_id")
    start_time = match.get("start_time", 0)

    found_matches: dict[str, dict] = {}  # deduplicate by match_id
    methods_attempted = 0
    methods_failed = 0

    # 1. Primary Method: Fetch by series_id via league matches
    if league_id and series_id:
        methods_attempted += 1
        try:
            resp = _opendota_get_with_retry(
                f"{OPENDOTA_BASE}/leagues/{league_id}/matches",
                timeout=10,
            )
            if resp.status_code == 200:
                all_matches = resp.json()
                for m in all_matches:
                    if m.get("series_id") == series_id:
                        found_matches[str(m["match_id"])] = m
            else:
                methods_failed += 1
        except Exception:
            methods_failed += 1

    # 2. Fallback Method 1: Fetch by series_id via proMatches
    if series_id:
        methods_attempted += 1
        try:
            resp = _opendota_get_with_retry(f"{OPENDOTA_BASE}/proMatches", timeout=10)
            if resp.status_code == 200:
                pro_matches = resp.json()
                for m in pro_matches:
                    if m.get("series_id") == series_id:
                        found_matches[str(m["match_id"])] = m
            else:
                methods_failed += 1
        except Exception:
            methods_failed += 1

    # 3. Valve Steam Web API GetMatchHistory filtered by league — real-time,
    # unlike OpenDota's aggregate tables (proMatches / league matches / SQL
    # explorer), which can lag behind by days for fresh matches. Entries
    # include series_id, so grouping is exact when the anchor has one;
    # otherwise fall back to head-to-head teams within ±8 h.
    steam_api_key = get_setting("STEAM_API_KEY")
    if steam_api_key and league_id:
        methods_attempted += 1
        try:
            resp = requests.get(
                f"{STEAM_API_BASE}/IDOTA2Match_570/GetMatchHistory/v1",
                params={
                    "key": steam_api_key,
                    "league_id": league_id,
                    "matches_requested": 100,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                entries = (resp.json().get("result") or {}).get("matches") or []
                for m in entries:
                    if series_id:
                        same_series = m.get("series_id") == series_id
                    else:
                        same_series = (
                            radiant_team_id
                            and dire_team_id
                            and {m.get("radiant_team_id"), m.get("dire_team_id")}
                            == {radiant_team_id, dire_team_id}
                            and abs(m.get("start_time", 0) - start_time) <= 28800
                        )
                    if same_series:
                        # OpenDota entries win — they may carry replay_url /
                        # cluster / replay_salt used for the replay hint.
                        found_matches.setdefault(str(m["match_id"]), m)
            else:
                methods_failed += 1
        except Exception:
            methods_failed += 1

    # 4. Fallback Method: SQL Explorer (Head-to-Head within +/- 24 hours)
    # This catches matches where series_id hasn't been assigned yet.
    # Only run when series_id is NOT set — if series_id is known, Methods 1 & 2
    # already handle grouping correctly, and the broad time window would otherwise
    # pull in matches from adjacent series between the same two teams.
    if not series_id and radiant_team_id and dire_team_id and start_time:
        methods_attempted += 1
        try:
            import urllib.parse
            min_time = start_time - 28800  # ±8 h covers any BO5 span (~6 h max)
            max_time = start_time + 28800  # while excluding back-to-back series (22 h+ apart)
            sql = f'''
            SELECT match_id, start_time, leagueid, series_id
            FROM matches
            WHERE ((radiant_team_id = {radiant_team_id} AND dire_team_id = {dire_team_id})
               OR (radiant_team_id = {dire_team_id} AND dire_team_id = {radiant_team_id}))
              AND start_time >= {min_time} AND start_time <= {max_time}
            ORDER BY start_time ASC
            '''
            url = f"{OPENDOTA_BASE}/explorer?sql={urllib.parse.quote(sql)}"
            resp = _opendota_get_with_retry(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                for row in data.get('rows', []):
                    # Filter by league_id if we have one to avoid cross-tournament noise
                    if league_id and row.get('leagueid') and row.get('leagueid') != league_id:
                        continue
                    found_matches[str(row["match_id"])] = row
            else:
                methods_failed += 1
        except Exception:
            methods_failed += 1

    # Degraded if at least one method was attempted and all of them failed.
    # When all matches are still found through one method, no degradation is signalled.
    degraded = methods_attempted > 0 and methods_failed == methods_attempted

    # Ensure the anchor match is always in the list — and always use its full
    # /matches payload, which carries cluster/replay_salt for the replay hint
    # (list-endpoint and GetMatchHistory entries don't).
    match_id_str = str(match["match_id"])
    found_matches[match_id_str] = match

    # Sort and format the results
    series = list(found_matches.values())
    series.sort(key=lambda m: m.get("start_time", 0))

    # When the anchor says it's part of a multi-map series (series_type 1=Bo3,
    # 2=Bo5) but no sibling matches could be discovered, the map number is
    # unknown — don't mislabel it "Map 1".
    map_number_unknown = (
        len(series) == 1 and bool(series_id) and bool(match.get("series_type"))
    )

    result = []
    for i, m in enumerate(series, start=1):
        # Keep series discovery responsive: this runs under the initial
        # "Fetching match info..." spinner, so do not call resolve_replay_url()
        # here. That helper can fall through to the authenticated Valve/GC
        # fallback and block for ~45s per map when Valve CDN returns 5xx.
        # The actual replay availability/download is checked later when the
        # user selects a map via process_match().
        replay_hint = bool(
            m.get("replay_url")
            or build_valve_replay_url(
                str(m["match_id"]),
                m.get("cluster"),
                m.get("replay_salt"),
            )
        )

        map_name = "Map ?" if map_number_unknown else f"Map {i}"
        label = f"{map_name} ✓" if replay_hint else f"{map_name} ⏳"
        btn_type = "primary" if replay_hint else "secondary"

        result.append({
            "match_id": str(m["match_id"]),
            "label": label,
            "btn_type": btn_type,
            "start_time": m.get("start_time", 0),
        })
    return result, degraded


# ── OpenDota parse trigger ─────────────────────────────────────────────────────

def request_opendota_parse(match_id: str) -> None:
    """Ask OpenDota to fetch and parse this match. Fire-and-forget."""
    try:
        requests.post(f"{OPENDOTA_BASE}/request/{match_id}", timeout=10)
    except Exception:
        pass


# ── Replay parsing ─────────────────────────────────────────────────────────────

def download_and_decompress_replay(replay_url: str, dest_path: str) -> None:
    """Download .dem.bz2, decompress to dest_path (.dem)."""
    resp = requests.get(replay_url, stream=True, timeout=120)
    resp.raise_for_status()
    decompressor = bz2.BZ2Decompressor()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192 * 4):
            if chunk:
                f.write(decompressor.decompress(chunk))


def _find_java() -> str:
    # 1. PATH (works on Linux, macOS, and Windows)
    found = shutil.which("java")
    if found:
        return found
    # 2. JAVA_HOME env var
    java_home = os.environ.get("JAVA_HOME", "")
    if java_home:
        for exe in ("bin/java", r"bin\java.exe"):
            candidate = os.path.join(java_home, exe)
            if os.path.isfile(candidate):
                return candidate
    # 3. Common Linux/macOS paths
    linux_patterns = [
        "/usr/lib/jvm/*/bin/java",
        "/usr/local/lib/jvm/*/bin/java",
        os.path.join(os.path.expanduser("~"), ".jdks", "*", "bin", "java"),
    ]
    for pattern in linux_patterns:
        matches = sorted(glob.glob(pattern), reverse=True)
        if matches:
            return matches[0]
    # 4. Common Windows install paths (newest version first)
    windows_patterns = [
        os.path.join(os.path.expanduser("~"), ".jdks", "*", "bin", "java.exe"),
        r"C:\Program Files\Eclipse Adoptium\jdk*\bin\java.exe",
        r"C:\Program Files\Java\jdk*\bin\java.exe",
        r"C:\Program Files\Microsoft\jdk*\bin\java.exe",
        r"C:\Program Files\BellSoft\LibericaJDK*\bin\java.exe",
    ]
    for pattern in windows_patterns:
        matches = sorted(glob.glob(pattern), reverse=True)
        if matches:
            return matches[0]
    raise FileNotFoundError(
        "Could not find java. Set the JAVA_HOME environment variable "
        "or add the JDK bin directory to PATH."
    )


def run_kill_extractor(dem_path: str) -> list[dict]:
    """
    Run KillExtractor.jar against the .dem file.
    Returns list of kill dicts sorted by time_f (float seconds).
    """
    if not os.path.isfile(JAR_PATH):
        raise FileNotFoundError(
            f"JAR not found at {JAR_PATH}.\n"
            "Please build it in IntelliJ: Gradle panel → Tasks → build → jar"
        )
    java_exe = _find_java()
    result = subprocess.run(
        [java_exe, "-jar", JAR_PATH, dem_path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"KillExtractor failed:\n{result.stderr[:2000]}")

    kills = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            kills.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    kills.sort(key=lambda k: k.get("time_f", k.get("time", 0)))
    return kills


def run_gold_extractor(dem_path: str, target_seconds: tuple[int, ...] = NETWORTH_LEADER_SECONDS) -> list[dict]:
    """Run GoldExtractor against the .dem file and return networth snapshots."""
    if not os.path.isfile(JAR_PATH):
        raise FileNotFoundError(
            f"JAR not found at {JAR_PATH}.\n"
            "Please build it in IntelliJ: Gradle panel → Tasks → build → jar"
        )
    java_exe = _find_java()
    result = subprocess.run(
        [java_exe, "-cp", JAR_PATH, "kills.GoldExtractor", dem_path, *[str(x) for x in target_seconds]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"GoldExtractor failed:\n{result.stderr[:2000]}")

    snapshots = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            snapshots.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    snapshots.sort(key=lambda k: k.get("target_time", k.get("clock", 0)))
    return snapshots


# ── Kill milestone analysis ────────────────────────────────────────────────────

def is_countable_hero_kill(event: dict) -> bool:
    """True only for real hero deaths that should count toward kill-score milestones.

    Clarity's `isTargetHero()` can still emit Lone Druid Spirit Bear deaths as "kill"
    events in some replays, so we also require the target unit name to be a real hero
    npc (`npc_dota_hero_*`). This keeps scoreboard-based caps aligned with OpenDota's
    authoritative radiant_score/dire_score totals.
    """
    if event.get("type") not in ("kill", None):
        return False
    if event.get("is_deny"):
        return False
    if event.get("killer_team", 0) not in (2, 3):
        return False
    target = event.get("target") or ""
    return target.startswith("npc_dota_hero_")


def analyse_kills(kills: list[dict], total_expected_kills: int = 0) -> dict:
    """
    kills: list of events sorted by time_f (output of run_kill_extractor).
      type="kill"   → hero kill event; killer_team 2=Radiant, 3=Dire, 0=deny (not counted)
      type="tower"  → tower death — skipped here, handled by analyse_special_events()
      type="roshan" → Roshan death — skipped here, handled by analyse_special_events()
    total_expected_kills: radiant_score + dire_score from OpenDota (authoritative total).
        Clarity emits phantom DOTA_COMBATLOG_DEATH events after the ancient is destroyed;
        those always sort chronologically last. We stop counting as soon as we hit the
        expected total so phantom tail events are never reached.

    Java uses getTargetTeam() flip: killer_team = 5 - targetTeam (2→3, 3→2).
    Deny detection: when attackerTeam == targetTeam in Java, killer_team is set to 0
    and is_deny=true is emitted. These events are skipped here (not credited to anyone).
    We also validate that the target name is an actual hero npc (`npc_dota_hero_*`),
    because replay combat logs can occasionally label non-hero units such as Spirit Bear
    as target-hero kills.

    Returns milestone dict including first_blood.
    """
    radiant_k = dire_k = total_k = 0
    first_to: dict[int, dict | None] = {5: None, 10: None, 15: None, 20: None}
    nth_kill: dict[int, dict | None] = {10: None, 20: None, 30: None}
    first_blood: dict | None = None

    for k in kills:
        if not is_countable_hero_kill(k):
            continue
        # killer_team: 2 = Radiant, 3 = Dire (derived from getTargetTeam() flip in Java)
        killer_team = k.get("killer_team", 0)
        if killer_team == 2:
            is_radiant = True
        else:
            is_radiant = False
        total_k += 1
        event = {**k, "is_radiant": is_radiant}

        if first_blood is None:
            first_blood = event

        if is_radiant:
            radiant_k += 1
            for threshold in (5, 10, 15, 20):
                if radiant_k == threshold and first_to[threshold] is None:
                    first_to[threshold] = event
        else:
            dire_k += 1
            for threshold in (5, 10, 15, 20):
                if dire_k == threshold and first_to[threshold] is None:
                    first_to[threshold] = event

        for milestone in (10, 20, 30):
            if total_k == milestone:
                nth_kill[milestone] = event

        # Stop once we've reached the authoritative total — phantom post-game events
        # are chronologically last and must never be counted.
        if total_expected_kills > 0 and total_k >= total_expected_kills:
            break

    return {
        "radiant_kills": radiant_k,
        "dire_kills": dire_k,
        "total_kills": total_k,
        "first_to": first_to,
        "nth_kill": nth_kill,
        "first_blood": first_blood,
    }


def analyse_interval_kills(kills: list[dict], total_expected_kills: int = 0) -> dict:
    """Bucket countable hero kills into 10-minute game-clock intervals.

    Interval semantics (interval markets spec):
      * Interval i covers game clock [i*600, (i+1)*600) seconds — i.e. 0-10 is
        0:00.000 up to but excluding 10:00.000 (a kill at exactly 10:00 belongs
        to 10-20).
      * Pre-horn kills (negative game clock) are EXCLUDED from every interval,
        but still consume the authoritative-total cap (they are real match kills).
      * Kills at/after 90:00 belong to no interval (markets only defined to 90).

    Uses the same countable-kill walk and phantom-kill cap as analyse_kills(),
    so interval sums always reconcile with the headline kill counts.

    Returns {
      "intervals": [{"radiant_kills": int, "dire_kills": int}] * 9,
      "pre_horn_kills": int,   # countable kills before 0:00, excluded from intervals
      "post_90_kills": int,    # countable kills at/after 90:00, excluded from intervals
      "last_kill_time": float, # game-clock time of last counted kill (fallback when duration missing)
    }
    """
    intervals = [{"radiant_kills": 0, "dire_kills": 0} for _ in range(MAX_INTERVALS)]
    pre_horn = 0
    post_90 = 0
    last_kill_time = 0.0
    total_k = 0

    for k in kills:
        if not is_countable_hero_kill(k):
            continue
        total_k += 1
        t = float(k.get("time_f", k.get("time", 0)))
        last_kill_time = max(last_kill_time, t)
        if t < 0:
            pre_horn += 1
        elif t >= MAX_INTERVALS * INTERVAL_SECONDS:
            post_90 += 1
        else:
            idx = int(t // INTERVAL_SECONDS)
            if k.get("killer_team", 0) == 2:
                intervals[idx]["radiant_kills"] += 1
            else:
                intervals[idx]["dire_kills"] += 1

        # Same phantom-kill guard as analyse_kills(): stop at the authoritative total.
        if total_expected_kills > 0 and total_k >= total_expected_kills:
            break

    return {
        "intervals": intervals,
        "pre_horn_kills": pre_horn,
        "post_90_kills": post_90,
        "last_kill_time": last_kill_time,
    }


def analyse_runes(events: list[dict], duration: int = 0) -> dict:
    """Bucket power-rune events to their nearest even-minute Spawn Time.

    The Java extractor emits rune events at the entity-observation time, which can
    drift slightly from the exact 6:00/8:00/... spawn clock. We accept only events
    within ±3s of the nearest 2-minute boundary, clamp the first allowed Spawn Time
    to 6:00, and keep anomaly metadata for UI warnings.

    `duration` (authoritative match length, seconds) bounds the expected Spawn
    Times for gap detection; when missing we fall back to the last observed rune
    event (never non-rune events — clarity emits phantom deaths post-ancient).

    Every observed spawn is kept — rune markets run for the whole game, with no
    upper Spawn Time bound (spawns past 40:00 are still marketed).
    """
    by_minute: dict[int, list[dict]] = {}
    ignored: list[dict] = []
    max_rune_time = 0.0

    for e in events or []:
        if e.get("type") != "rune":
            continue
        try:
            t = float(e.get("time_f", e.get("time", 0)) or 0)
        except (TypeError, ValueError):
            t = 0.0
        max_rune_time = max(max_rune_time, t)

        nearest = int(round(t / RUNE_SPAWN_SECONDS) * RUNE_SPAWN_SECONDS)
        nearest = max(RUNE_FIRST_SPAWN_SECONDS, nearest)
        drift = t - nearest
        rune_type = _normalize_rune_type(e.get("rune_type", ""))
        side = str(e.get("side", "")).strip().lower()
        if abs(drift) > 3 or not _is_rune_type_label(rune_type) or side not in RUNE_SIDES:
            ignored.append({**e, "nearest_minute": nearest // 60, "drift": drift})
            continue

        minute = nearest // 60
        by_minute.setdefault(minute, []).append({
            "minute": minute,
            "rune_type": rune_type,
            "side": side,
            "time_f": t,
            "drift": drift,
        })

    spawns: list[dict] = []
    duplicates: list[dict] = []
    for minute in sorted(by_minute):
        bucket = sorted(by_minute[minute], key=lambda r: abs(float(r.get("drift", 0))))
        spawns.append(bucket[0])
        if len(bucket) > 1:
            duplicates.append({"minute": minute, "spawns": bucket})

    unknown_gaps: list[int] = []
    # Expected Spawn Times run through the authoritative duration when we have
    # it; a spawn landing within the final 3s of the game is not expected.
    horizon = float(duration) if duration and duration > 0 else max_rune_time
    if horizon >= RUNE_FIRST_SPAWN_SECONDS:
        last_expected = int(max(horizon - 3, 0) // RUNE_SPAWN_SECONDS) * RUNE_SPAWN_SECONDS
        for sec in range(RUNE_FIRST_SPAWN_SECONDS, last_expected + 1, RUNE_SPAWN_SECONDS):
            minute = sec // 60
            if minute not in by_minute:
                unknown_gaps.append(minute)

    return {
        "spawns": spawns,
        "duplicates": duplicates,
        "unknown_gaps": unknown_gaps,
        "ignored": ignored,
    }


def analyse_networth_leaders(snapshots: list[dict], duration: int = 0) -> dict:
    """Normalize GoldExtractor snapshots for Map Networth Leader At Time markets."""
    by_minute: dict[int, list[dict]] = {}
    ignored: list[dict] = []

    for row in snapshots or []:
        try:
            target_time = int(round(float(row.get("target_time", 0) or 0)))
            clock = float(row.get("clock", target_time) or target_time)
            radiant_networth = int(row.get("radiant_networth", 0) or 0)
            dire_networth = int(row.get("dire_networth", 0) or 0)
        except (TypeError, ValueError):
            ignored.append(row)
            continue

        if target_time not in NETWORTH_LEADER_SECONDS:
            ignored.append(row)
            continue

        if radiant_networth > dire_networth:
            leader_team = 2
        elif dire_networth > radiant_networth:
            leader_team = 3
        else:
            leader_team = 0

        minute = target_time // 60
        by_minute.setdefault(minute, []).append({
            "minute": minute,
            "target_time": target_time,
            "time_f": clock,
            "drift": clock - target_time,
            "radiant_networth": radiant_networth,
            "dire_networth": dire_networth,
            "networth_diff": radiant_networth - dire_networth,
            "leader_team": leader_team,
        })

    snapshots_out: list[dict] = []
    duplicates: list[dict] = []
    for minute in sorted(by_minute):
        bucket = sorted(by_minute[minute], key=lambda r: abs(float(r.get("drift", 0))))
        snapshots_out.append(bucket[0])
        if len(bucket) > 1:
            duplicates.append({"minute": minute, "snapshots": bucket})

    unknown_gaps = []
    for sec in NETWORTH_LEADER_SECONDS:
        if duration and duration < sec:
            continue
        minute = sec // 60
        if minute not in by_minute:
            unknown_gaps.append(minute)

    return {
        "snapshots": snapshots_out,
        "duplicates": duplicates,
        "unknown_gaps": unknown_gaps,
        "ignored": ignored,
    }


def analyse_special_events(events: list[dict]) -> dict:
    """
    Scan sorted events for first tower death, first barracks death, first Aegis pickup,
    and totals for towers and Roshans destroyed by each team.

    First Tower    — credited to the team that did NOT lose the tower.
    First Barracks — credited to the team that did NOT lose the barracks.
    First Aegis    — credited to the team whose hero picked up the Aegis (detected
                     via entity inventory change on CDOTA_Item_Aegis).
    Tower totals   — radiant_towers = towers destroyed BY Radiant (Dire towers that fell).
                     dire_towers    = towers destroyed BY Dire   (Radiant towers that fell).
    Barracks totals — radiant_barracks = barracks destroyed BY Radiant (Dire barracks that fell).
                     dire_barracks    = barracks destroyed BY Dire   (Radiant barracks that fell).
    Roshan totals  — radiant_roshans / dire_roshans by killer_team.
    Tormentor totals — radiant_tormentors / dire_tormentors by killer_team.

    Returns dict with first_tower, first_barracks, first_aegis,
    radiant_towers, dire_towers, radiant_barracks, dire_barracks,
    radiant_roshans, dire_roshans, radiant_tormentors, dire_tormentors.
    """
    first_tower: dict | None = None
    first_barracks: dict | None = None
    first_aegis: dict | None = None
    first_tormentor: dict | None = None
    radiant_towers = 0
    dire_towers = 0
    radiant_barracks = 0
    dire_barracks = 0
    radiant_roshans = 0
    dire_roshans = 0
    radiant_tormentors = 0
    dire_tormentors = 0

    for e in events:
        etype = e.get("type")
        if etype == "tower":
            lost = e.get("lost_team", 0)
            got = 3 if lost == 2 else (2 if lost == 3 else 0)
            if first_tower is None:
                first_tower = {**e, "got_team": got, "is_radiant": got == 2}
            if got == 2:
                radiant_towers += 1
            elif got == 3:
                dire_towers += 1
        elif etype == "barracks":
            lost = e.get("lost_team", 0)
            got = 3 if lost == 2 else (2 if lost == 3 else 0)
            if first_barracks is None:
                first_barracks = {**e, "got_team": got, "is_radiant": got == 2}
            if got == 2:
                radiant_barracks += 1
            elif got == 3:
                dire_barracks += 1
        elif etype == "aegis":
            if first_aegis is None:
                kt = e.get("killer_team", 0)
                first_aegis = {**e, "is_radiant": kt == 2}
        elif etype == "roshan":
            kt = e.get("killer_team", 0)
            if kt == 2:
                radiant_roshans += 1
            elif kt == 3:
                dire_roshans += 1
        elif etype == "tormentor":
            kt = e.get("killer_team", 0)
            if first_tormentor is None:
                first_tormentor = {**e, "is_radiant": kt == 2}
            if kt == 2:
                radiant_tormentors += 1
            elif kt == 3:
                dire_tormentors += 1

    return {
        "first_tower": first_tower,
        "first_barracks": first_barracks,
        "first_aegis": first_aegis,
        "first_tormentor": first_tormentor,
        "radiant_towers": radiant_towers,
        "dire_towers": dire_towers,
        "radiant_barracks": radiant_barracks,
        "dire_barracks": dire_barracks,
        "radiant_roshans": radiant_roshans,
        "dire_roshans": dire_roshans,
        "radiant_tormentors": radiant_tormentors,
        "dire_tormentors": dire_tormentors,
    }


# ── Full match pipeline ────────────────────────────────────────────────────────

def process_match(match_id: str) -> dict:
    """
    Fetch match data, download & parse replay, compute kill milestones.
    Returns everything needed for rendering.
    If no replay is available yet, returns basic match info immediately
    with replay_available=False and milestones=None.
    """
    match = fetch_match(match_id)

    radiant_name = (
        (match.get("radiant_team") or {}).get("name")
        or match.get("radiant_name")
        or "Radiant"
    )
    dire_name = (
        (match.get("dire_team") or {}).get("name")
        or match.get("dire_name")
        or "Dire"
    )

    radiant_score = int(match.get("radiant_score", 0))
    dire_score    = int(match.get("dire_score", 0))
    duration      = int(match.get("duration", 0))

    # Tower counts from API bitmask (available without replay)
    ts_radiant = int(match.get("tower_status_radiant", 2047))
    ts_dire    = int(match.get("tower_status_dire",    2047))
    # radiant_towers = towers destroyed BY Radiant (i.e. Dire towers that fell)
    api_radiant_towers = towers_from_status(ts_dire)
    api_dire_towers    = towers_from_status(ts_radiant)

    # Barracks counts + megacreeps from barracks bitmask (6 bits; bit = 1 means
    # still standing; 0 = all barracks gone = opponent has megas)
    bs_radiant = int(match.get("barracks_status_radiant", 63))
    bs_dire    = int(match.get("barracks_status_dire",    63))
    # radiant_barracks = barracks destroyed BY Radiant (i.e. Dire barracks that fell)
    api_radiant_barracks = barracks_from_status(bs_dire)
    api_dire_barracks    = barracks_from_status(bs_radiant)
    # radiant_megas = True when Dire barracks all fell (Radiant earned megas)
    api_radiant_megas = bs_dire == 0
    api_dire_megas    = bs_radiant == 0

    match_source = match.get("_match_source", "OpenDota")

    def _basic_info(
        replay_err: str | None = None,
        replay_source: str | None = None,
    ) -> dict:
        return {
            "match_id": match_id,
            "radiant_name": radiant_name,
            "dire_name": dire_name,
            "radiant_win": match.get("radiant_win", False),
            "match_source": match_source,
            "replay_available": False,
            "replay_error": replay_err,
            "replay_source": replay_source,
            "milestones": None,
            "raw_kills": [],
            "total_expected_kills": radiant_score + dire_score,
            "radiant_score": radiant_score,
            "dire_score": dire_score,
            "duration": duration,
            "radiant_towers": api_radiant_towers,
            "dire_towers": api_dire_towers,
            "radiant_barracks": api_radiant_barracks,
            "dire_barracks": api_dire_barracks,
            "radiant_megas": api_radiant_megas,
            "dire_megas": api_dire_megas,
        }

    replay_url, replay_source, replay_err = resolve_replay_url(
        match_id,
        match,
        queue_opendota_parse=True,
    )
    if not replay_url:
        return _basic_info(replay_err=replay_err)

    # Download & decompress replay, parse replay-derived markets
    gold_snapshots = []
    gold_error = None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            dem_path = os.path.join(tmpdir, f"{match_id}.dem")
            with st.spinner(f"Downloading replay (~110 MB)..."):
                download_and_decompress_replay(replay_url, dem_path)
            with st.spinner("Parsing replay for kill events..."):
                kills = run_kill_extractor(dem_path)
            try:
                with st.spinner("Parsing replay for networth snapshots..."):
                    gold_snapshots = run_gold_extractor(dem_path)
            except Exception as exc:
                gold_error = str(exc)
    except Exception as exc:
        return _basic_info(replay_err=str(exc), replay_source=replay_source)

    milestones = analyse_kills(kills, radiant_score + dire_score)
    special = analyse_special_events(kills)
    milestones["first_tower"] = special["first_tower"]
    milestones["first_barracks"] = special["first_barracks"]
    milestones["first_aegis"] = special["first_aegis"]
    milestones["radiant_towers"] = special["radiant_towers"]
    milestones["dire_towers"] = special["dire_towers"]
    milestones["radiant_barracks"] = special["radiant_barracks"]
    milestones["dire_barracks"] = special["dire_barracks"]
    milestones["radiant_roshans"] = special["radiant_roshans"]
    milestones["dire_roshans"] = special["dire_roshans"]
    milestones["radiant_tormentors"] = special["radiant_tormentors"]
    milestones["dire_tormentors"] = special["dire_tormentors"]
    milestones["first_tormentor"] = special["first_tormentor"]
    milestones["interval_kills"] = analyse_interval_kills(kills, radiant_score + dire_score)
    milestones["runes"] = analyse_runes(kills, duration)
    milestones["networth_leaders"] = analyse_networth_leaders(gold_snapshots, duration)
    if gold_error:
        milestones["networth_leader_error"] = gold_error

    return {
        "match_id": match_id,
        "radiant_name": radiant_name,
        "dire_name": dire_name,
        "radiant_win": match.get("radiant_win", False),
        "match_source": match_source,
        "replay_available": True,
        "replay_source": replay_source,
        "milestones": milestones,
        "raw_kills": kills,
        "total_expected_kills": radiant_score + dire_score,
        "duration": duration,
        "radiant_megas": api_radiant_megas,
        "dire_megas": api_dire_megas,
    }


# ── Render ─────────────────────────────────────────────────────────────────────


def _fmt_networth(value: int) -> str:
    return f"{int(value):,}"


def render_home_away_selector(data: dict) -> dict | None:
    """Shared Home/Away selector for trader-perspective markets."""
    rn = data["radiant_name"]
    dn = data["dire_name"]

    st.divider()
    st.markdown("## Home/Away Markets")
    st.caption("Networth and interval markets use the trader-assigned Home/Away perspective.")

    radiant_label = f"{rn} (Radiant)"
    dire_label = f"{dn} (Dire)"
    choice = st.radio(
        "Which team is **Home**?",
        [radiant_label, dire_label],
        index=None,
        horizontal=True,
        key=f"home_team_{data['match_id']}",
    )
    if choice is None:
        st.info("Select the Home team above to reveal Home/Away market resolutions.")
        return None

    home_is_radiant = choice == radiant_label
    home_name, away_name = (rn, dn) if home_is_radiant else (dn, rn)
    st.markdown(
        f"{_chip(f'Home = {home_name}', HOME_UNDER_COLOR)} · "
        f"{_chip(f'Away = {away_name}', AWAY_OVER_COLOR)}",
        unsafe_allow_html=True,
    )
    return {
        "home_is_radiant": home_is_radiant,
        "home_name": home_name,
        "away_name": away_name,
    }


def render_match_analysis(data: dict) -> None:
    rn = data["radiant_name"]
    dn = data["dire_name"]
    rw = data["radiant_win"]
    m = data["milestones"]
    replay_available = data.get("replay_available", True)

    # Header
    st.divider()
    h1, h2, h3 = st.columns([5, 1, 5])
    with h1:
        result = "WIN" if rw else "LOSS"
        st.markdown(
            f"<span style='color:{RADIANT_COLOR}'>**{rn}**</span> · {result if rw else 'LOSS'}",
            unsafe_allow_html=True,
        )
    with h2:
        st.markdown("<div style='text-align:center'>vs</div>", unsafe_allow_html=True)
    with h3:
        st.markdown(
            f"<span style='color:{DIRE_COLOR}'>**{dn}**</span> · {'LOSS' if rw else 'WIN'}",
            unsafe_allow_html=True,
        )

    source_bits = []
    match_source = data.get("match_source")
    if match_source:
        source_bits.append(f"Match data: {match_source}")
    replay_source = data.get("replay_source")
    if replay_source:
        source_bits.append(f"Replay: {replay_source}")
    if source_bits:
        st.caption(" · ".join(source_bits))

    if replay_available:
        total = m["total_kills"]
        st.markdown(
            f"Total kills: "
            f"<span style='color:{RADIANT_COLOR}'>{rn} {m['radiant_kills']}</span> — "
            f"<span style='color:{DIRE_COLOR}'>{dn} {m['dire_kills']}</span> "
            f"({total} total)",
            unsafe_allow_html=True,
        )
        raw_kills = data.get("raw_kills", [])
        excluded_non_hero = [
            k for k in raw_kills
            if k.get("type") in ("kill", None)
            and not k.get("is_deny")
            and k.get("killer_team", 0) in (2, 3)
            and not ((k.get("target") or "").startswith("npc_dota_hero_"))
        ]
        if excluded_non_hero:
            st.warning(
                f"Excluded {len(excluded_non_hero)} non-hero death event(s) from replay kill milestones "
                f"(for example Spirit Bear deaths)."
            )
    else:
        rs = data.get("radiant_score", 0)
        ds = data.get("dire_score", 0)
        st.markdown(
            f"Total kills: "
            f"<span style='color:{RADIANT_COLOR}'>{rn} {rs}</span> — "
            f"<span style='color:{DIRE_COLOR}'>{dn} {ds}</span> "
            f"({rs + ds} total)",
            unsafe_allow_html=True,
        )

    duration_secs = data.get("duration", 0)
    if duration_secs:
        st.markdown(f"Duration: **{duration_secs // 60}:{duration_secs % 60:02d}**")

    # Tower totals — available from API bitmask even without a replay
    rad_t = data.get("radiant_towers", 0) if not replay_available else m.get("radiant_towers", 0)
    dir_t = data.get("dire_towers", 0)    if not replay_available else m.get("dire_towers", 0)
    total_t = rad_t + dir_t
    tc1, tc2, tc3, tc4 = st.columns(4)
    with tc1:
        st.markdown(
            f"Total Towers: **{total_t}** "
            f"(<span style='color:{RADIANT_COLOR}'>{rad_t}</span>/"
            f"<span style='color:{DIRE_COLOR}'>{dir_t}</span>)",
            unsafe_allow_html=True,
        )

    # Barracks totals — available from API bitmask even without a replay
    rad_b = data.get("radiant_barracks", 0) if not replay_available else m.get("radiant_barracks", 0)
    dir_b = data.get("dire_barracks", 0)    if not replay_available else m.get("dire_barracks", 0)
    total_b = rad_b + dir_b
    with tc2:
        st.markdown(
            f"Total Barracks: **{total_b}** "
            f"(<span style='color:{RADIANT_COLOR}'>{rad_b}</span>/"
            f"<span style='color:{DIRE_COLOR}'>{dir_b}</span>)",
            unsafe_allow_html=True,
        )

    # Megacreeps — available from API bitmask, shown in both replay and no-replay paths
    rad_megas = data.get("radiant_megas", False)
    dire_megas = data.get("dire_megas", False)
    if rad_megas and dire_megas:
        megas_label = (
            f"<span style='color:{RADIANT_COLOR}'>{rn}</span> & "
            f"<span style='color:{DIRE_COLOR}'>{dn}</span>"
        )
    elif rad_megas:
        megas_label = f"<span style='color:{RADIANT_COLOR}'>{rn}</span>"
    elif dire_megas:
        megas_label = f"<span style='color:{DIRE_COLOR}'>{dn}</span>"
    else:
        megas_label = "None"

    if not replay_available:
        with tc3:
            st.markdown(f"Megacreeps: **{megas_label}**", unsafe_allow_html=True)
        st.info(data.get("replay_error") or "Replay not available yet — kill milestone data will appear once the replay is ready. ⏳")
        return

    # Roshan totals (replay only)
    rad_r = m.get("radiant_roshans", 0)
    dir_r = m.get("dire_roshans", 0)
    total_r = rad_r + dir_r
    with tc3:
        st.markdown(
            f"Total Roshans: **{total_r}** "
            f"(<span style='color:{RADIANT_COLOR}'>{rad_r}</span>/"
            f"<span style='color:{DIRE_COLOR}'>{dir_r}</span>)",
            unsafe_allow_html=True,
        )

    # Tormentor totals (replay only)
    rad_tm = m.get("radiant_tormentors", 0)
    dir_tm = m.get("dire_tormentors", 0)
    total_tm = rad_tm + dir_tm
    with tc4:
        st.markdown(
            f"Total Tormentors: **{total_tm}** "
            f"(<span style='color:{RADIANT_COLOR}'>{rad_tm}</span>/"
            f"<span style='color:{DIRE_COLOR}'>{dir_tm}</span>)",
            unsafe_allow_html=True,
        )
    st.markdown(f"Megacreeps: **{megas_label}**", unsafe_allow_html=True)

    st.divider()

    # Nth kill row
    st.markdown("**Nth Kill (who scored it)**")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown(
            f"**10th Kill:** {result_label(m['nth_kill'][10], rn, dn)}",
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            f"**20th Kill:** {result_label(m['nth_kill'][20], rn, dn)}",
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            f"**30th Kill:** {result_label(m['nth_kill'][30], rn, dn)}",
            unsafe_allow_html=True,
        )

    st.divider()

    # First to N kills row
    st.markdown("**First to Reach N Kills**")
    c1, c2, c3, c4 = st.columns(4)
    for col, threshold in zip((c1, c2, c3, c4), (5, 10, 15, 20)):
        with col:
            st.markdown(
                f"**First to {threshold}:** {result_label(m['first_to'][threshold], rn, dn)}",
                unsafe_allow_html=True,
            )

    st.divider()

    # Notable firsts row (order: First Blood, First Tower, First Tormentor, First Barracks, First Aegis)
    st.markdown("**Notable Firsts**")
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.markdown(
            f"**First Blood:** {result_label(m.get('first_blood'), rn, dn)}",
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            f"**First Tower:** {result_label(m.get('first_tower'), rn, dn)}",
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            f"**First Tormentor:** {result_label(m.get('first_tormentor'), rn, dn)}",
            unsafe_allow_html=True,
        )
    with c4:
        st.markdown(
            f"**First Barracks:** {result_label(m.get('first_barracks'), rn, dn)}",
            unsafe_allow_html=True,
        )
    with c5:
        st.markdown(
            f"**First Aegis:** {result_label(m.get('first_aegis'), rn, dn)}",
            unsafe_allow_html=True,
        )

    # ── Home/Away markets ─────────────────────────────────────────────────────
    home_context = render_home_away_selector(data)
    if home_context is not None:
        render_networth_leader_markets(data, home_context)
        render_interval_markets(data, home_context)

    # ── Rune markets ───────────────────────────────────────────────────────────
    render_rune_markets(data)

    # ── Debug: raw kill events ─────────────────────────────────────────────────
    raw_kills = data.get("raw_kills", [])
    expected = data.get("total_expected_kills", 0)
    if raw_kills:
        with st.expander(f"Debug: raw kill events ({len(raw_kills)} emitted by JAR, cap = {expected})"):
            def _short(name: str) -> str:
                """Strip npc_dota_hero_ / npc_dota_ prefix for readability."""
                for pfx in ("npc_dota_hero_", "npc_dota_"):
                    if name.startswith(pfx):
                        return name[len(pfx):]
                return name

            def _team_label(t: int) -> str:
                return {2: "Radiant", 3: "Dire"}.get(t, f"neutral({t})")

            def _mm_ss(secs: float) -> str:
                s = int(secs)
                sign = "-" if s < 0 else ""
                s = abs(s)
                return f"{sign}{s // 60}:{s % 60:02d}"

            # Walk the list as analyse_kills() does to mark which were counted
            counted_set: set[int] = set()
            deny_set: set[int] = set()
            tower_set: set[int] = set()
            barracks_set: set[int] = set()
            roshan_set: set[int] = set()
            tormentor_set: set[int] = set()
            aegis_set: set[int] = set()
            _total = 0
            for _i, _k in enumerate(raw_kills):
                etype = _k.get("type")
                if etype == "tower":
                    tower_set.add(_i)
                    continue
                if etype == "barracks":
                    barracks_set.add(_i)
                    continue
                if etype == "roshan":
                    roshan_set.add(_i)
                    continue
                if etype == "tormentor":
                    tormentor_set.add(_i)
                    continue
                if etype == "aegis":
                    aegis_set.add(_i)
                    continue
                if _k.get("is_deny"):
                    deny_set.add(_i)
                    continue
                if not is_countable_hero_kill(_k):
                    continue
                _total += 1
                counted_set.add(_i)
                if expected > 0 and _total >= expected:
                    break

            rows = []
            _seq = 0
            for i, k in enumerate(raw_kills):
                kt = k.get("killer_team", 0)
                is_deny = i in deny_set
                is_tower = i in tower_set
                is_barracks = i in barracks_set
                is_roshan = i in roshan_set
                is_tormentor = i in tormentor_set
                is_aegis = i in aegis_set
                is_counted = i in counted_set
                if is_counted:
                    _seq += 1
                att_raw = k.get("attacker_team_raw", -1)

                if is_tower:
                    lost = k.get("lost_team", 0)
                    got = 3 if lost == 2 else (2 if lost == 3 else 0)
                    credited = f"{_team_label(got)} (tower)"
                elif is_barracks:
                    lost = k.get("lost_team", 0)
                    got = 3 if lost == 2 else (2 if lost == 3 else 0)
                    credited = f"{_team_label(got)} (barracks)"
                elif is_roshan:
                    credited = f"{_team_label(kt)} (roshan)"
                elif is_tormentor:
                    credited = f"{_team_label(kt)} (tormentor)"
                elif is_aegis:
                    credited = f"{_team_label(kt)} (aegis)"
                elif is_deny:
                    credited = "deny"
                elif kt in (2, 3):
                    credited = _team_label(kt)
                else:
                    credited = f"? ({kt})"

                counted_label = (
                    "yes" if is_counted else
                    "DENY" if is_deny else
                    "TOWER" if is_tower else
                    "BARRACKS" if is_barracks else
                    "ROSHAN" if is_roshan else
                    "TORMENTOR" if is_tormentor else
                    "AEGIS" if is_aegis else
                    "DROPPED"
                )

                rows.append({
                    "#": _seq if is_counted else "",
                    "time": _mm_ss(k.get("time_f", k.get("time", 0))),
                    "target": _short(k.get("target", "")),
                    "attacker": _short(k.get("attacker", "")),
                    "att_team": _team_label(att_raw),
                    "credited_to": credited,
                    "counted": counted_label,
                })

            st.dataframe(rows, use_container_width=True)


# ── Interval markets rendering ─────────────────────────────────────────────────

def _fmt_clock(secs: float) -> str:
    s = int(secs)
    sign = "-" if s < 0 else ""
    s = abs(s)
    return f"{sign}{s // 60}:{s % 60:02d}"


def _chip(text: str, color: str) -> str:
    return f"<span style='color:{color};font-weight:600'>{text}</span>"


# Dark card palette mimicking the resolving tool (winner cell = green).
_IM_CARD_BG = "#141b26"
_IM_BORDER = "#2c3a4a"
_IM_HEADER_BG = "#22303f"
_IM_LABEL_BG = "#101720"
_IM_CELL_BG = "#2a3949"
_IM_WIN_BG = "#2e6b50"
_IM_TEXT = "#c9d4e0"
_IM_DIM = "#93a4b5"

# Hint-verification cell colours
_HINT_OK_BG = "#1e4636"        # bland green — correct hints, low visual priority
_HINT_OK_TEXT = "#7fae97"
_HINT_EXPECTED_BG = "#fdd835"  # yellow — the selection that actually won when the hint is wrong

# Cell render states → (background, text colour, font weight)
_IM_CELL_STYLES = {
    "off":        (_IM_CELL_BG, _IM_DIM, "400"),
    "win":        (_IM_WIN_BG, "#eaf5ee", "600"),
    "hint_ok":    (_HINT_OK_BG, _HINT_OK_TEXT, "600"),
    "hint_wrong": (_IM_CELL_BG, _IM_DIM, "400"),
    "expected":   (_HINT_EXPECTED_BG, "#1c2430", "700"),
}


def _im_card(title: str, rows: list[tuple[str, list[tuple[str, str]]]]) -> str:
    """Build an HTML market card that mimics the trading tool's resolving view:
    a dark card with a header bar, one row per line, the line value in the
    left label column, one cell per selection.

    rows: [(label, [(selection_text, state), ...]), ...] where state is a key
    of _IM_CELL_STYLES ('win' = green winner in the split view; 'hint_ok' /
    'hint_wrong' / 'expected' belong to the hint-verification view).
    """
    parts = [
        f"<div style='background:{_IM_CARD_BG};border:1px solid {_IM_BORDER};"
        f"border-radius:6px;overflow:hidden;margin-bottom:10px;'>"
        f"<div style='background:{_IM_HEADER_BG};padding:5px 10px;color:{_IM_TEXT};"
        f"font-weight:600;font-size:0.8rem;'>{title}</div>"
        f"<table style='width:100%;border-collapse:collapse;font-size:0.78rem;"
        f"table-layout:fixed;'>"
    ]
    for label, cells in rows:
        parts.append("<tr>")
        parts.append(
            f"<td style='background:{_IM_LABEL_BG};color:{_IM_TEXT};padding:6px 8px;"
            f"width:26%;border-bottom:2px solid {_IM_CARD_BG};white-space:nowrap;"
            f"font-weight:600;'>{label}</td>"
        )
        for text, state in cells:
            bg, color, weight = _IM_CELL_STYLES.get(state, _IM_CELL_STYLES["off"])
            parts.append(
                f"<td style='background:{bg};color:{color};padding:6px 8px;"
                f"text-align:center;font-weight:{weight};overflow-wrap:break-word;"
                f"border-bottom:2px solid {_IM_CARD_BG};"
                f"border-left:2px solid {_IM_CARD_BG};'>{text}</td>"
            )
        parts.append("</tr>")
    parts.append("</table></div>")
    return "".join(parts)


def _ou_rows(result: int, label_suffix: str = "") -> list[tuple[str, list[tuple[str, bool]]]]:
    """The 4 half-kill O/U lines around `result` as card rows (under | over).

    Lines below the result settle Over, above settle Under. Lines below the
    minimum book line (0.5) do not exist and are dropped.
    """
    rows = []
    for off in (-1.5, -0.5, 0.5, 1.5):
        line = result + off
        if line < 0.5:
            continue
        rows.append((
            f"{line:g}{label_suffix}",
            [
                ("under", "win" if line > result else "off"),
                ("over", "win" if line < result else "off"),
            ],
        ))
    return rows


def _handicap_rows(
    margin: int, home_name: str, away_name: str
) -> list[tuple[str, list[tuple[str, bool]]]]:
    """The 4 handicap lines around the split, from the HOME perspective.

    A line L (applied to Home) settles Home when margin + L > 0. The split
    sits at L = -margin. Rows are ordered top-down like the trading tool
    (line closest to even first).
    """
    split = -margin
    rows = []
    for line in (split + 1.5, split + 0.5, split - 0.5, split - 1.5):
        home_wins = margin + line > 0
        rows.append((
            f"{line:+g}",
            [
                (home_name, "win" if home_wins else "off"),
                (away_name, "off" if home_wins else "win"),
            ],
        ))
    return rows


def _render_interval_cards(
    header: str,
    sub: str | None,
    home_k: int,
    away_k: int,
    home_name: str,
    away_name: str,
) -> None:
    """Render one interval as a vertical stack of the 5 market cards."""
    total = home_k + away_k
    margin = home_k - away_k

    st.markdown(f"#### {header}")
    if sub:
        st.caption(sub)
    st.markdown(
        f"{_chip(f'{home_name} {home_k}', HOME_UNDER_COLOR)} — "
        f"{_chip(f'{away_name} {away_k}', AWAY_OVER_COLOR)} ({total} total)",
        unsafe_allow_html=True,
    )

    html = [
        _im_card("Total Kills", _ou_rows(total)),
        _im_card("Kills Handicap", _handicap_rows(margin, home_name, away_name)),
        _im_card("Team Total Kills", _ou_rows(home_k, ", home") + _ou_rows(away_k, ", away")),
        _im_card(
            "Total Kills Parity",
            [(str(total), [
                ("odd", "win" if total % 2 == 1 else "off"),
                ("even", "win" if total % 2 == 0 else "off"),
            ])],
        ),
        _im_card(
            "Kills Winner (3-way)",
            [("3-way", [
                (home_name, "win" if margin > 0 else "off"),
                ("draw", "win" if margin == 0 else "off"),
                (away_name, "win" if margin < 0 else "off"),
            ])],
        ),
    ]
    st.markdown("".join(html), unsafe_allow_html=True)


# ── Interval-market hint verification (Dotabuff copypaste) ────────────────────

# Only these markets are verified — every other marketName in the paste is ignored.
_INTERVAL_HINT_MARKETS = {
    "map interval total kills": "Total Kills",
    "map interval team total kills": "Team Total Kills",
    "map interval kills handicap": "Kills Handicap",
    "map interval total kills parity": "Parity",
    "map interval kills winner": "Winner",
}
_RUNE_HINT_MARKETS = {
    "map rune type at time": "Rune Type At Time",
    "map rune spawn side at time": "Rune Spawn Side At Time",
}
_HINT_MARKET_ORDER = ["Total Kills", "Kills Handicap", "Team Total Kills", "Parity", "Winner"]
_HINT_CARD_TITLES = {
    "Total Kills": "Total Kills",
    "Kills Handicap": "Kills Handicap",
    "Team Total Kills": "Team Total Kills",
    "Parity": "Total Kills Parity",
    "Winner": "Kills Winner (3-way)",
}
_RUNE_HINT_MARKET_ORDER = ["Rune Type At Time", "Rune Spawn Side At Time"]
_RUNE_HINT_CARD_TITLES = {
    "Rune Type At Time": "Rune Type At Time",
    "Rune Spawn Side At Time": "Rune Spawn Side At Time",
}


def _parse_hint_paste(raw: str) -> tuple[list[dict], bool]:
    """Parse the pasted hint blob. Returns (entries, recovered).

    Tries strict JSON first. Dotabuff copypastes are often truncated mid-array,
    so on failure fall back to extracting every balanced top-level {...} object
    (recovered=True signals the paste was not clean JSON).
    """
    raw = raw.strip()
    if not raw:
        return [], False
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            parsed = [parsed]
        if isinstance(parsed, list):
            return [e for e in parsed if isinstance(e, dict)], False
    except json.JSONDecodeError:
        pass

    entries: list[dict] = []
    depth = 0
    start: int | None = None
    in_str = False
    escape = False
    for i, ch in enumerate(raw):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    obj = json.loads(raw[start:i + 1])
                    if isinstance(obj, dict):
                        entries.append(obj)
                except json.JSONDecodeError:
                    pass
                start = None
    # Keep only top-level hint objects, not nested params dicts.
    entries = [e for e in entries if "marketName" in e]
    return entries, True


def _parse_interval_param(value) -> int | None:
    """'0-10' / '00-10' / '80-90' → interval index 0..8, else None."""
    m = re.fullmatch(r"\s*0*(\d{1,2})\s*-\s*0*(\d{1,2})\s*", str(value or ""))
    if not m:
        return None
    start, end = int(m.group(1)), int(m.group(2))
    if end - start != 10 or start % 10 != 0 or start >= MAX_INTERVALS * 10:
        return None
    return start // 10


def _parse_rune_time_param(value) -> int | None:
    """Parse a Dotabuff rune time param into a Spawn Time minute (6, 8, ...)."""
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    m = re.fullmatch(r"0*(\d{1,3})m", raw)
    if m:
        minute = int(m.group(1))
    else:
        m = re.fullmatch(r"0*(\d{1,3}):([0-5]\d)", raw)
        if m:
            minute = int(m.group(1)) if int(m.group(2)) == 0 else -1
        elif re.fullmatch(r"\d+", raw):
            n = int(raw)
            minute = n // 60 if n >= 100 else n
        else:
            return None
    if minute < 6 or minute % 2 != 0:
        return None
    return minute


_RUNE_TIME_PARAM_KEYS = ("time", "spawntime", "spawn_time", "runetime", "rune_time", "minute")


def _parse_rune_time_from_params(params: dict) -> int | None:
    """Prefer explicit time-like keys; never read mapOrder (an even mapOrder ≥ 6
    would otherwise parse as a Spawn Time and resolve against the wrong row)."""
    items = sorted(
        (params or {}).items(),
        key=lambda kv: 0 if str(kv[0]).strip().lower() in _RUNE_TIME_PARAM_KEYS else 1,
    )
    for key, value in items:
        if str(key).strip().lower() == "maporder":
            continue
        minute = _parse_rune_time_param(value)
        if minute is not None:
            return minute
    return None


def _rune_hint_card_row(market_label: str, entry: dict, spawn: dict) -> tuple | None:
    raw_hint = entry.get("hint")
    hint = "" if raw_hint is None else str(raw_hint).strip().lower()
    if market_label == "Rune Type At Time":
        actual = _normalize_rune_type(spawn.get("rune_type", ""))
        options = [(x, x) for x in _rune_type_options([spawn], extra=[hint] if hint else None)]
    elif market_label == "Rune Spawn Side At Time":
        actual = str(spawn.get("side", "")).strip().lower()
        options = [(x, x) for x in RUNE_SIDES]
    else:
        return None
    keys = [key for key, _ in options]
    if (hint and hint not in keys) or actual not in keys:
        return None

    # With no hint (feed sends hint: null), only the parser's result is
    # highlighted — yellow 'expected', same colour as "what actually won".
    cells = []
    for key, text in options:
        if hint and key == hint == actual:
            state = "hint_ok"
        elif hint and key == hint:
            state = "hint_wrong"
        elif key == actual:
            state = "expected"
        else:
            state = "off"
        cells.append((text, state))
    verdict = ("correct" if hint == actual else "wrong") if hint else "no_hint"
    minute = int(spawn.get("minute", 0))
    label = f"{minute}m" + ("" if hint else " · no hint")
    return ((minute, 0), label, cells, verdict)

def _hint_card_row(
    market_label: str,
    entry: dict,
    home_k: int,
    away_k: int,
    home_name: str,
    away_name: str,
) -> tuple | None:
    """Build one verification-card row for a pasted hint.

    Returns (sort_key, label, cells, verdict) or None when the hint is
    unreadable. verdict ∈ 'correct' | 'wrong' | 'push' | 'no_hint'. Cells use
    _IM_CELL_STYLES states: the hinted selection renders bland green when
    correct and default grey when wrong; when wrong, the selection that actually
    won is highlighted yellow ('expected'). Entries with hint: null (the feed
    now sends hintless markets) get only the parser's result in yellow.
    """
    params = entry.get("params") or {}
    raw_hint = entry.get("hint")
    hint = "" if raw_hint is None else str(raw_hint).strip().upper()
    has_hint = bool(hint)
    total = home_k + away_k
    margin = home_k - away_k

    def _cells(options: list[tuple[str, str]], actual: str | None) -> list[tuple[str, str]]:
        out = []
        for key, text in options:
            if actual is None:  # push — no winning selection
                state = "off"
            elif has_hint and key == hint == actual:
                state = "hint_ok"
            elif has_hint and key == hint:
                state = "hint_wrong"
            elif key == actual:
                state = "expected"
            else:
                state = "off"
            out.append((text, state))
        return out

    def _verdict(actual: str | None) -> str:
        if not has_hint:
            return "no_hint"
        if actual is None:
            return "push"
        return "correct" if actual == hint else "wrong"

    def _label(base: str, actual: str | None) -> str:
        if not has_hint:
            return base + " · no hint" + (" · push" if actual is None else "")
        return base + (" · push" if actual is None else "")

    ou_options = [("UNDER", "under"), ("OVER", "over")]

    if market_label == "Total Kills":
        t = params.get("threshold")
        if not isinstance(t, (int, float)) or (has_hint and hint not in ("OVER", "UNDER")):
            return None
        actual = None if total == t else ("OVER" if total > t else "UNDER")
        return ((0, t), _label(f"{t:g}", actual), _cells(ou_options, actual), _verdict(actual))

    if market_label == "Team Total Kills":
        t = params.get("threshold")
        side = str(params.get("side") or params.get("team") or "").strip().upper()
        if (
            not isinstance(t, (int, float))
            or side not in ("HOME", "AWAY")
            or (has_hint and hint not in ("OVER", "UNDER"))
        ):
            return None
        kills = home_k if side == "HOME" else away_k
        actual = None if kills == t else ("OVER" if kills > t else "UNDER")
        label = _label(f"{t:g}, {side.lower()}", actual)
        return ((0 if side == "HOME" else 1, t), label, _cells(ou_options, actual), _verdict(actual))

    if market_label == "Kills Handicap":
        h = params.get("handicap")
        if not isinstance(h, (int, float)) or (has_hint and hint not in ("HOME", "AWAY")):
            return None
        # The hint export carries the NEGATED book line: a pasted handicap of
        # -3.5 is the +3.5 Home line in the resolving tool. Flip the sign to
        # recover the real line, then settle Home iff margin + line > 0
        # (verified against match 8885183102 vs the resolving tool).
        line = -h
        adj = margin + line  # real line applied to Home
        actual = None if adj == 0 else ("HOME" if adj > 0 else "AWAY")
        label = _label(f"{line:+g}", actual)
        options = [("HOME", home_name), ("AWAY", away_name)]
        # Descending line value = closest-to-even first, like the trading tool.
        return ((0, -line), label, _cells(options, actual), _verdict(actual))

    if market_label == "Parity":
        if has_hint and hint not in ("ODD", "EVEN"):
            return None
        actual = "EVEN" if total % 2 == 0 else "ODD"
        options = [("ODD", "odd"), ("EVEN", "even")]
        return ((0, 0), _label(str(total), actual), _cells(options, actual), _verdict(actual))

    if market_label == "Winner":
        if has_hint and hint not in ("HOME", "AWAY", "DRAW"):
            return None
        actual = "HOME" if margin > 0 else ("AWAY" if margin < 0 else "DRAW")
        options = [("HOME", home_name), ("DRAW", "draw"), ("AWAY", away_name)]
        return ((0, 0), _label("3-way", actual), _cells(options, actual), _verdict(actual))

    return None


def _render_hint_checker(
    match_id: str,
    intervals: list[dict],
    home_is_radiant: bool,
    last_idx: int,
    duration: int,
    home_name: str,
    away_name: str,
    rune_data: dict | None = None,
) -> None:
    """Paste-box that cross-checks Dotabuff interval and rune-market hints."""
    st.markdown("### Verify Dotabuff hints")
    raw = st.text_area(
        "Paste the hint export here (Map Interval and Map Rune markets are checked; everything else is ignored)",
        key=f"hint_paste_{match_id}",
        height=170,
        placeholder='[\n  {\n    "marketName": "Map Interval Kills Handicap",\n    "hint": "HOME",\n    "params": { "mapOrder": 1, "handicap": -12.5, "interval": "0-10" }\n  },\n  {\n    "marketName": "Map Rune Type At Time",\n    "hint": "HASTE",\n    "params": { "mapOrder": 1, "time": "10m" }\n  }\n]',
    )
    if not raw.strip():
        return

    entries, recovered = _parse_hint_paste(raw)
    if not entries:
        st.error("Could not read any hint objects from the paste.")
        return
    if recovered:
        st.caption(
            f"Paste was not clean JSON (probably truncated) — recovered {len(entries)} hint object(s)."
        )

    interval_entries = []
    rune_entries = []
    ignored = 0
    for e in entries:
        key = re.sub(r"\s+", " ", str(e.get("marketName", "")).strip().lower())
        interval_label = _INTERVAL_HINT_MARKETS.get(key)
        rune_label = _RUNE_HINT_MARKETS.get(key)
        if interval_label is not None:
            interval_entries.append((interval_label, e))
        elif rune_label is not None:
            rune_entries.append((rune_label, e))
        else:
            ignored += 1

    if not interval_entries and not rune_entries:
        st.warning(f"No interval/rune-market hints found in the paste ({ignored} other hint(s) ignored).")
        return

    # Interval hints must drive the map-order choice exactly as before rune
    # support existed (a rune-only mapOrder must never change which map's
    # interval hints get verified); rune entries only decide when there are
    # no interval entries at all.
    map_order_source = interval_entries if interval_entries else rune_entries
    map_orders = sorted(
        {
            (e.get("params") or {}).get("mapOrder")
            for _, e in map_order_source
            if isinstance((e.get("params") or {}).get("mapOrder"), int)
        }
    )
    map_order = map_orders[0] if map_orders else None
    if len(map_orders) > 1:
        map_order = st.selectbox(
            "The paste contains several maps — which mapOrder is THIS replay?",
            map_orders,
            key=f"hint_map_order_{match_id}",
        )

    per_interval: dict[int, dict[str, list]] = {}
    per_rune_market: dict[str, list] = {}
    rune_by_minute = {
        int(spawn.get("minute")): spawn
        for spawn in (rune_data or {}).get("spawns", [])
        if spawn.get("minute") is not None
    }
    counts = {"wrong": 0, "correct": 0, "push": 0, "no_hint": 0}
    not_reached = 0
    unreadable = 0
    seen: set[tuple] = set()

    for label, e in interval_entries:
        params = e.get("params") or {}
        if map_order is not None and params.get("mapOrder") not in (None, map_order):
            continue
        idx = _parse_interval_param(params.get("interval"))
        if idx is None:
            unreadable += 1
            continue
        if idx > last_idx:
            not_reached += 1
            continue

        bucket = intervals[idx]
        home_k = bucket["radiant_kills"] if home_is_radiant else bucket["dire_kills"]
        away_k = bucket["dire_kills"] if home_is_radiant else bucket["radiant_kills"]
        row = _hint_card_row(label, e, home_k, away_k, home_name, away_name)
        if row is None:
            unreadable += 1
            continue
        sort_key, row_label, cells, verdict = row
        dedupe_key = ("interval", idx, label, row_label, str(e.get("hint", "")).strip().upper())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        counts[verdict] += 1
        per_interval.setdefault(idx, {}).setdefault(label, []).append((sort_key, row_label, cells))

    for label, e in rune_entries:
        params = e.get("params") or {}
        if map_order is not None and params.get("mapOrder") not in (None, map_order):
            continue
        minute = _parse_rune_time_from_params(params)
        if minute is None:
            unreadable += 1
            continue
        spawn = rune_by_minute.get(minute)
        if not spawn:
            # A Spawn Time past the end of the game is a not-reached market,
            # not an unreadable hint (mirrors the interval `idx > last_idx` path).
            if duration > 0 and minute * 60 > duration:
                not_reached += 1
            else:
                unreadable += 1
            continue
        row = _rune_hint_card_row(label, e, spawn)
        if row is None:
            unreadable += 1
            continue
        sort_key, row_label, cells, verdict = row
        dedupe_key = ("rune", minute, label, row_label, str(e.get("hint", "")).strip().lower())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        counts[verdict] += 1
        per_rune_market.setdefault(label, []).append((sort_key, row_label, cells))

    if not per_interval and not per_rune_market:
        st.warning(
            "No verifiable interval/rune-market hints for this map "
            f"({not_reached} not reached · {unreadable} unreadable · {ignored} other ignored)."
        )
        return

    summary = f"**{counts['wrong']} incorrect** · {counts['correct']} correct"
    if counts["push"]:
        summary += f" · {counts['push']} push"
    if counts["no_hint"]:
        summary += f" · {counts['no_hint']} without hint (parser result shown in yellow)"
    skipped_bits = []
    if not_reached:
        skipped_bits.append(
            f"{not_reached} hint(s) skipped — market not reached "
            f"(game ended {_fmt_clock(duration)})"
        )
    if unreadable:
        skipped_bits.append(f"{unreadable} unreadable")
    if ignored:
        skipped_bits.append(f"{ignored} other ignored")
    if skipped_bits:
        summary += " · " + " · ".join(skipped_bits)
    if counts["wrong"]:
        st.error(summary)
    else:
        st.success(summary)
    st.markdown(
        f"{_chip('grey = wrong JSON selection / inactive', _IM_DIM)} · "
        f"{_chip('green = correct hint', '#4f9d77')} · "
        f"{_chip('yellow = what actually won / parser result for hintless markets', _HINT_EXPECTED_BG)}",
        unsafe_allow_html=True,
    )

    shown = sorted(per_interval)
    PER_ROW = 4
    for chunk_start in range(0, len(shown), PER_ROW):
        chunk = shown[chunk_start:chunk_start + PER_ROW]
        cols = st.columns(PER_ROW, gap="medium")
        for col, idx in zip(cols, chunk):
            bucket = intervals[idx]
            home_k = bucket["radiant_kills"] if home_is_radiant else bucket["dire_kills"]
            away_k = bucket["dire_kills"] if home_is_radiant else bucket["radiant_kills"]

            header = f"Interval {idx * 10}-{(idx + 1) * 10}"
            is_partial = (
                idx == last_idx
                and duration < (idx + 1) * INTERVAL_SECONDS
                and duration < MAX_INTERVALS * INTERVAL_SECONDS
            )
            sub = f"⚠️ partial interval — game ended {_fmt_clock(duration)}, resolved as-is" if is_partial else None

            with col:
                st.markdown(f"#### {header}")
                if sub:
                    st.caption(sub)
                st.markdown(
                    f"{_chip(f'{home_name} {home_k}', HOME_UNDER_COLOR)} — "
                    f"{_chip(f'{away_name} {away_k}', AWAY_OVER_COLOR)} "
                    f"({home_k + away_k} total)",
                    unsafe_allow_html=True,
                )
                html = []
                for market_label in _HINT_MARKET_ORDER:
                    market_rows = per_interval[idx].get(market_label)
                    if not market_rows:
                        continue
                    market_rows.sort(key=lambda r: r[0])
                    html.append(_im_card(
                        _HINT_CARD_TITLES[market_label],
                        [(row_label, cells) for _, row_label, cells in market_rows],
                    ))
                st.markdown("".join(html), unsafe_allow_html=True)

    if per_rune_market:
        cols = st.columns([2, 1], gap="medium")
        for col, market_label in zip(cols, _RUNE_HINT_MARKET_ORDER):
            market_rows = per_rune_market.get(market_label)
            if not market_rows:
                continue
            market_rows.sort(key=lambda r: r[0])
            with col:
                st.markdown(_im_card(
                    _RUNE_HINT_CARD_TITLES[market_label],
                    [(row_label, cells) for _, row_label, cells in market_rows],
                ), unsafe_allow_html=True)

def render_networth_leader_markets(data: dict, home_context: dict) -> None:
    """Render Map Networth Leader At Time using Home/Away labels."""
    m = data.get("milestones") or {}
    networth_data = m.get("networth_leaders") or {}
    snapshots = networth_data.get("snapshots") or []
    gaps = networth_data.get("unknown_gaps") or []
    if not snapshots and not gaps and not m.get("networth_leader_error"):
        return

    home_is_radiant = bool(home_context.get("home_is_radiant"))
    home_team = 2 if home_is_radiant else 3
    away_team = 3 if home_is_radiant else 2
    home_name = home_context["home_name"]
    away_name = home_context["away_name"]

    by_minute = {
        int(row.get("minute")): row
        for row in snapshots
        if row.get("minute") is not None
    }
    minutes = sorted(set(by_minute) | {int(minute) for minute in gaps})

    rows = []
    for minute in minutes:
        row = by_minute.get(minute)
        if row is None:
            comparison = (
                "<div style='display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);"
                "gap:8px;align-items:center;'>"
                f"<div style='background:{_IM_CELL_BG};border-left:3px solid {HOME_UNDER_COLOR};"
                f"border-radius:6px;padding:7px 9px;color:{_IM_TEXT};'>"
                f"<div style='font-size:0.68rem;color:{_IM_DIM};text-transform:uppercase;'>Home</div>"
                f"<div style='font-weight:650;'>{home_name}</div>"
                f"<div style='font-size:0.72rem;color:{_IM_DIM};'>unknown</div></div>"
                f"<div style='background:{_IM_LABEL_BG};color:{_IM_DIM};border:1px solid {_IM_BORDER};"
                "border-radius:999px;padding:5px 10px;text-align:center;white-space:nowrap;font-weight:700;'>"
                "unknown<br><span style='font-size:0.62rem;font-weight:500;'>diff</span></div>"
                f"<div style='background:{_IM_CELL_BG};border-left:3px solid {AWAY_OVER_COLOR};"
                f"border-radius:6px;padding:7px 9px;color:{_IM_TEXT};'>"
                f"<div style='font-size:0.68rem;color:{_IM_DIM};text-transform:uppercase;'>Away</div>"
                f"<div style='font-weight:650;'>{away_name}</div>"
                f"<div style='font-size:0.72rem;color:{_IM_DIM};'>unknown</div></div>"
                "</div>"
            )
            rows.append((f"{minute}m", comparison))
            continue

        radiant_nw = int(row.get("radiant_networth", 0) or 0)
        dire_nw = int(row.get("dire_networth", 0) or 0)
        leader_team = int(row.get("leader_team", 0) or 0)
        home_nw = radiant_nw if home_is_radiant else dire_nw
        away_nw = dire_nw if home_is_radiant else radiant_nw
        diff = home_nw - away_nw
        if diff > 0:
            diff_value = f"+{_fmt_networth(diff)}"
            diff_label = "Home lead"
        elif diff < 0:
            diff_value = f"+{_fmt_networth(abs(diff))}"
            diff_label = "Away lead"
        else:
            diff_value = "0"
            diff_label = "Tie"

        home_bg = _IM_WIN_BG if leader_team == home_team else _IM_CELL_BG
        away_bg = _IM_WIN_BG if leader_team == away_team else _IM_CELL_BG
        diff_bg = _IM_WIN_BG if leader_team in (home_team, away_team) else _IM_LABEL_BG
        comparison = (
            "<div style='display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);"
            "gap:8px;align-items:center;'>"
            f"<div style='background:{home_bg};border-left:3px solid {HOME_UNDER_COLOR};"
            f"border-radius:6px;padding:7px 9px;color:{_IM_TEXT};min-width:0;'>"
            f"<div style='font-size:0.68rem;color:{_IM_DIM};text-transform:uppercase;'>Home</div>"
            f"<div style='font-weight:650;overflow-wrap:break-word;'>{home_name}</div>"
            f"<div style='font-size:0.8rem;font-weight:700;'>{_fmt_networth(home_nw)}</div></div>"
            f"<div style='background:{diff_bg};color:#eaf5ee;border:1px solid {_IM_BORDER};"
            "border-radius:999px;padding:6px 12px;text-align:center;white-space:nowrap;"
            "box-shadow:0 0 0 2px rgba(0,0,0,0.18);'>"
            f"<div style='font-size:0.88rem;font-weight:800;line-height:1.05;'>{diff_value}</div>"
            f"<div style='font-size:0.62rem;font-weight:600;color:{_IM_DIM};text-transform:uppercase;'>{diff_label}</div></div>"
            f"<div style='background:{away_bg};border-left:3px solid {AWAY_OVER_COLOR};"
            f"border-radius:6px;padding:7px 9px;color:{_IM_TEXT};min-width:0;'>"
            f"<div style='font-size:0.68rem;color:{_IM_DIM};text-transform:uppercase;'>Away</div>"
            f"<div style='font-weight:650;overflow-wrap:break-word;'>{away_name}</div>"
            f"<div style='font-size:0.8rem;font-weight:700;'>{_fmt_networth(away_nw)}</div></div>"
            "</div>"
        )
        rows.append((f"{minute}m", comparison))

    st.markdown("### Map Networth Leader At Time")
    st.caption("Team net worth snapshots at 5:00, 10:00, and 15:00 game clock — green = leader.")
    if rows:
        parts = [
            f"<div style='background:{_IM_CARD_BG};border:1px solid {_IM_BORDER};"
            f"border-radius:6px;overflow:hidden;margin-bottom:10px;'>"
            f"<div style='background:{_IM_HEADER_BG};padding:5px 10px;color:{_IM_TEXT};"
            f"font-weight:600;font-size:0.8rem;'>Map Networth Leader At Time</div>"
            f"<table style='width:100%;border-collapse:collapse;font-size:0.78rem;'>"
        ]
        for label, comparison in rows:
            parts.append("<tr>")
            parts.append(
                f"<td style='background:{_IM_LABEL_BG};color:{_IM_TEXT};padding:6px 8px;"
                f"width:14%;border-bottom:2px solid {_IM_CARD_BG};white-space:nowrap;"
                f"font-weight:700;vertical-align:middle;'>{label}</td>"
            )
            parts.append(
                f"<td style='background:{_IM_CELL_BG};padding:6px 8px;"
                f"border-bottom:2px solid {_IM_CARD_BG};border-left:2px solid {_IM_CARD_BG};'>"
                f"{comparison}</td>"
            )
            parts.append("</tr>")
        parts.append("</table></div>")
        st.markdown("".join(parts), unsafe_allow_html=True)

    warnings = []
    if gaps:
        warnings.append("missing networth snapshot for " + ", ".join(f"{int(minute)}m" for minute in gaps))
    duplicates = networth_data.get("duplicates") or []
    if duplicates:
        warnings.append(
            "duplicate networth snapshots at "
            + ", ".join(f"{int(item.get('minute', 0))}m" for item in duplicates)
        )
    ignored = networth_data.get("ignored") or []
    if ignored:
        warnings.append(f"{len(ignored)} networth snapshot(s) ignored")
    if m.get("networth_leader_error"):
        warnings.append("GoldExtractor failed: " + str(m.get("networth_leader_error"))[:300])
    if warnings:
        st.caption("Networth warning: " + " · ".join(warnings))


def render_interval_markets(data: dict, home_context: dict | None = None) -> None:
    """Render the Interval Markets section (5 markets per 10-minute interval).

    The user must pick which team is Home before any resolution is shown —
    there is deliberately no Radiant=Home default.
    """
    m = data.get("milestones") or {}
    interval_data = m.get("interval_kills")
    if not interval_data:
        return

    if home_context is None:
        home_context = render_home_away_selector(data)
    if home_context is None:
        return
    home_is_radiant = bool(home_context.get("home_is_radiant"))
    home_name = home_context["home_name"]
    away_name = home_context["away_name"]

    st.divider()
    st.markdown("## Interval Markets")

    duration = int(data.get("duration", 0))
    if duration <= 0:
        # Duration missing from the API — fall back to the last counted kill.
        duration = int(interval_data.get("last_kill_time", 0)) + 1

    max_span = MAX_INTERVALS * INTERVAL_SECONDS
    last_idx = min((max(duration - 1, 0)) // INTERVAL_SECONDS, MAX_INTERVALS - 1)
    intervals = interval_data["intervals"]

    # The parsed all-interval view is collapsed by default — day to day the
    # hint checker below is the primary surface; open this to see every market.
    with st.expander("Parsed interval markets (every interval, from the replay)", expanded=False):
        st.caption(
            "10-minute game-clock windows (0-10 = 0:00-9:59, 10-20 = 10:00-19:59, ...). "
            "Pre-horn kills (negative clock) are excluded. Green cell = winning selection; "
            "handicap lines are from the Home perspective."
        )

        pre_horn = interval_data.get("pre_horn_kills", 0)
        if pre_horn:
            st.caption(f"{pre_horn} pre-horn kill(s) excluded from all intervals.")
        post_90 = interval_data.get("post_90_kills", 0)
        if post_90:
            st.warning(
                f"{post_90} kill(s) at/after 90:00 ignored — interval markets are only defined up to 90."
            )

        shown = list(range(last_idx + 1))

        # Lay intervals out horizontally, up to 4 per row, so traders can scan
        # across the game the same way the resolving tool lists markets.
        PER_ROW = 4
        for chunk_start in range(0, len(shown), PER_ROW):
            chunk = shown[chunk_start:chunk_start + PER_ROW]
            cols = st.columns(PER_ROW, gap="medium")
            for col, idx in zip(cols, chunk):
                bucket = intervals[idx]
                home_k = bucket["radiant_kills"] if home_is_radiant else bucket["dire_kills"]
                away_k = bucket["dire_kills"] if home_is_radiant else bucket["radiant_kills"]

                header = f"Interval {idx * 10}-{(idx + 1) * 10}"
                is_partial = (
                    idx == last_idx
                    and duration < (idx + 1) * INTERVAL_SECONDS
                    and duration < max_span
                )
                sub = f"⚠️ partial interval — game ended {_fmt_clock(duration)}, resolved as-is" if is_partial else None

                with col:
                    _render_interval_cards(header, sub, home_k, away_k, home_name, away_name)

    _render_hint_checker(
        data["match_id"],
        intervals,
        home_is_radiant,
        last_idx,
        duration,
        home_name,
        away_name,
        (data.get("milestones") or {}).get("runes"),
    )


def render_rune_markets(data: dict) -> None:
    """Render power-rune markets by Spawn Time, independent of Home/Away."""
    m = data.get("milestones") or {}
    rune_data = m.get("runes")
    if not rune_data:
        return

    spawns = rune_data.get("spawns") or []
    gaps = rune_data.get("unknown_gaps") or []
    if not spawns and not gaps:
        return

    by_minute = {
        int(spawn.get("minute")): spawn
        for spawn in spawns
        if spawn.get("minute") is not None
    }
    minutes = sorted(set(by_minute) | {int(minute) for minute in gaps})
    if not minutes:
        return

    rune_type_options = _rune_type_options(spawns)
    type_rows = []
    side_rows = []
    for minute in minutes:
        spawn = by_minute.get(minute) or {}
        rune_type = str(spawn.get("rune_type", "")).strip().lower()
        side = str(spawn.get("side", "")).strip().lower()
        label = f"{minute}m"
        type_rows.append((
            label,
            [(value, "win" if value == rune_type else "off") for value in rune_type_options],
        ))
        side_rows.append((
            label,
            [(value, "win" if value == side else "off") for value in RUNE_SIDES],
        ))

    st.divider()
    st.markdown("## Rune Markets")

    # Collapsed by default — the hint checker is the primary surface; open
    # this to see every parsed spawn.
    with st.expander("Parsed rune spawns (every spawn, from the replay)", expanded=False):
        st.caption(
            "Power rune spawns every 2 minutes from 6:00 — one randomly chosen river spot. "
            "green = what spawned"
        )

        warnings = []
        if gaps:
            warnings.append("missing observed spawn for " + ", ".join(f"{int(minute)}m" for minute in gaps))
        duplicates = rune_data.get("duplicates") or []
        if duplicates:
            warnings.append(
                "duplicate rune events at "
                + ", ".join(f"{int(item.get('minute', 0))}m" for item in duplicates)
            )
        ignored = rune_data.get("ignored") or []
        if ignored:
            warnings.append(f"{len(ignored)} rune event(s) ignored due to bad label or >3s drift")
        if warnings:
            st.caption("Rune anomaly warning: " + " · ".join(warnings))

        c1, c2 = st.columns([2, 1], gap="medium")
        with c1:
            st.markdown(_im_card("Rune Type At Time", type_rows), unsafe_allow_html=True)
        with c2:
            st.markdown(_im_card("Rune Spawn Side At Time", side_rows), unsafe_allow_html=True)


# ── Session state defaults ─────────────────────────────────────────────────────

for _key, _default in [
    ("series_matches", None),   # list of map dicts
    ("radiant_name", None),
    ("dire_name", None),
    ("match_analysis", None),   # rendered match data
    ("anchor_match_id", None),  # match ID used to discover the series
]:
    if _key not in st.session_state:
        st.session_state[_key] = _default


# ── UI ─────────────────────────────────────────────────────────────────────────

st.title("Dota 2 Series Analyzer")

url_input = st.text_input(
    "url",
    placeholder="https://www.dotabuff.com/matches/8697483686 or 8697483686",
    label_visibility="collapsed",
)

if st.button("Analyze", type="primary") and url_input.strip():
    raw = url_input.strip()
    match_id = parse_match_id(raw)
    if not match_id:
        st.error("Could not extract a match ID. Provide either a Dotabuff/OpenDota match URL or a raw numeric match ID.")
    else:
        with st.spinner("Fetching match info..."):
            try:
                match = fetch_match(match_id)
                league_id = match.get("leagueid")
                series_id = match.get("series_id")

                rn = (
                    (match.get("radiant_team") or {}).get("name")
                    or match.get("radiant_name")
                    or "Radiant"
                )
                dn = (
                    (match.get("dire_team") or {}).get("name")
                    or match.get("dire_name")
                    or "Dire"
                )
                st.session_state.radiant_name = rn
                st.session_state.dire_name = dn
                st.session_state.anchor_match_id = match_id
                st.session_state.match_analysis = None
                st.session_state.replay_retry_count = 0

                if match.get("_match_source") == "Valve":
                    st.info(
                        "OpenDota timed out, so this run is using Valve match details. "
                        "Series discovery is temporarily limited to the selected match."
                    )
                    st.session_state.series_matches = [{"match_id": match_id, "label": "Map 1"}]
                else:
                    maps, series_degraded = fetch_series_matches(match)
                    if series_degraded:
                        st.info(
                            "Series detection unavailable (OpenDota series endpoints failed) — "
                            "analyzing the requested match only."
                        )
                    st.session_state.series_matches = maps if maps else [{"match_id": match_id, "label": "Map 1"}]

            except requests.HTTPError as exc:
                st.error(f"HTTP error from OpenDota: {safe_error_str(exc)}")
            except Exception as exc:
                # Any exception escaping fetch_match means OpenDota failed AND
                # the Valve fallback chain didn't yield a result. Tailor the
                # message to whether credentials are even configured.
                if not has_valve_fallback_credentials():
                    st.error(
                        "OpenDota is unreachable and no Valve fallback credentials are configured. "
                        "Add Steam credentials via Streamlit secrets or copy .env.example to .env."
                    )
                else:
                    st.error(
                        f"OpenDota is unreachable and the Valve fallback chain also failed. "
                        f"Try again later. Details: {safe_error_str(exc)}"
                    )

# ── Series map picker ──────────────────────────────────────────────────────────

if st.session_state.series_matches is not None:
    rn = st.session_state.radiant_name or "Radiant"
    dn = st.session_state.dire_name or "Dire"
    st.divider()
    st.markdown(
        f"### <span style='color:{RADIANT_COLOR}'>{rn}</span> vs "
        f"<span style='color:{DIRE_COLOR}'>{dn}</span>",
        unsafe_allow_html=True,
    )

    maps = st.session_state.series_matches
    if maps:
        st.markdown("**Select a map to analyze:**")
        btn_cols = st.columns(len(maps))
        for i, m in enumerate(maps):
            with btn_cols[i]:
                # Streamlit types checking is strict, so we explicitly handle it
                is_primary = m.get("btn_type") == "primary"
                if st.button(m["label"], type="primary" if is_primary else "secondary", key=f"map_btn_{m['match_id']}"):
                    try:
                        st.session_state.replay_retry_count = 0
                        data = process_match(m["match_id"])
                        st.session_state.match_analysis = data
                        st.rerun()
                    except FileNotFoundError as exc:
                        st.error(safe_error_str(exc))
                    except Exception as exc:
                        st.error(f"Error analyzing {m['label']}: {safe_error_str(exc)}")

# ── Match analysis output ──────────────────────────────────────────────────────

if st.session_state.match_analysis is not None:
    render_match_analysis(st.session_state.match_analysis)

    # Auto-retry every minute until the replay is available, capped to avoid
    # hanging the UI indefinitely when APIs stay down.
    MAX_REPLAY_RETRIES = 5
    if not st.session_state.match_analysis.get("replay_available", True):
        match_id = st.session_state.match_analysis["match_id"]
        retries_done = st.session_state.get("replay_retry_count", 0)
        if retries_done >= MAX_REPLAY_RETRIES:
            st.error(
                f"Replay still not available after {MAX_REPLAY_RETRIES} attempts. "
                "OpenDota and Valve fallbacks have been exhausted — try again later."
            )
        else:
            status = st.empty()
            for remaining in range(60, 0, -1):
                status.info(
                    f"Retrying replay download in {remaining}s... "
                    f"(attempt {retries_done + 1}/{MAX_REPLAY_RETRIES}) ⏳"
                )
                time.sleep(1)
            status.info("Attempting to download replay... 🔄")
            try:
                new_data = process_match(match_id)
                st.session_state.match_analysis = new_data
                if new_data.get("replay_available", False):
                    st.session_state.replay_retry_count = 0
                else:
                    st.session_state.replay_retry_count = retries_done + 1
            except Exception as exc:
                st.session_state.replay_retry_count = retries_done + 1
                st.warning(f"Retry failed: {safe_error_str(exc)}")
            st.rerun()
