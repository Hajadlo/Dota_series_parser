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


# ── Helpers ────────────────────────────────────────────────────────────────────

def towers_from_status(tower_status: int) -> int:
    """Return the number of towers destroyed from an 11-bit bitmask.
    Each bit = 1 means that tower is still standing, so destroyed = 11 - popcount."""
    return 11 - bin(tower_status).count("1")


def parse_match_id(url: str) -> str | None:
    """Extract a Dota 2 match ID from a URL or accept a raw numeric match ID."""
    raw = url.strip()
    if raw.isdigit():
        return raw
    m = re.search(r"/matches/(\d+)", raw)
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

    @steam_client.on("logged_on")
    def _on_logged_on():
        dota_client.launch()

    @steam_client.on("error")
    def _on_steam_error(result):
        if not response.ready():
            response.set(("steam_error", result, None))

    @dota_client.on("ready")
    def _on_gc_ready():
        dota_client.request_match_details(int(match_id))

    @dota_client.on("match_details")
    def _on_match_details(returned_match_id, eresult, match):
        if int(returned_match_id) == int(match_id) and not response.ready():
            response.set(("match_details", eresult, match))

    login_result = steam_client.login(username, password)
    if login_result != EResult.OK:
        return {}

    runner = gevent.spawn(steam_client.run_forever)
    try:
        kind, status, match = response.get(timeout=45)
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
    if replay_url:
        return replay_url, "OpenDota", None

    if queue_opendota_parse:
        request_opendota_parse(match_id)

    replay_url = build_valve_replay_url(
        match_id,
        match_data.get("cluster"),
        match_data.get("replay_salt"),
    )
    if replay_url and replay_url_exists(replay_url):
        return replay_url, "Valve CDN (via OpenDota match data)", None

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

    # 3. Fallback Method 2: SQL Explorer (Head-to-Head within +/- 24 hours)
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

    # Ensure the anchor match is always in the list
    match_id_str = str(match["match_id"])
    if match_id_str not in found_matches:
        found_matches[match_id_str] = match

    # Sort and format the results
    series = list(found_matches.values())
    series.sort(key=lambda m: m.get("start_time", 0))

    result = []
    for i, m in enumerate(series, start=1):
        # Prefer OpenDota's replay_url, but fall back to a direct Valve replay URL
        # when OpenDota hasn't exposed replay_url yet.
        has_replay = False
        try:
            quick_match = fetch_match(str(m["match_id"]))
            replay_url, _, _ = resolve_replay_url(str(m["match_id"]), quick_match)
            has_replay = bool(replay_url)
        except Exception:
            pass  # default to false if APIs are slow

        label = f"Map {i} ✓" if has_replay else f"Map {i} ⏳"
        btn_type = "primary" if has_replay else "secondary"

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
    Roshan totals  — radiant_roshans / dire_roshans by killer_team.

    Returns dict with first_tower, first_barracks, first_aegis,
    radiant_towers, dire_towers, radiant_roshans, dire_roshans.
    """
    first_tower: dict | None = None
    first_barracks: dict | None = None
    first_aegis: dict | None = None
    first_tormentor: dict | None = None
    radiant_towers = 0
    dire_towers = 0
    radiant_roshans = 0
    dire_roshans = 0

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
            if first_barracks is None:
                lost = e.get("lost_team", 0)
                got = 3 if lost == 2 else (2 if lost == 3 else 0)
                first_barracks = {**e, "got_team": got, "is_radiant": got == 2}
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
            if first_tormentor is None:
                kt = e.get("killer_team", 0)
                first_tormentor = {**e, "is_radiant": kt == 2}

    return {
        "first_tower": first_tower,
        "first_barracks": first_barracks,
        "first_aegis": first_aegis,
        "first_tormentor": first_tormentor,
        "radiant_towers": radiant_towers,
        "dire_towers": dire_towers,
        "radiant_roshans": radiant_roshans,
        "dire_roshans": dire_roshans,
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

    # Megacreeps from barracks bitmask (6 bits; 0 = all barracks gone = opponent has megas)
    bs_radiant = int(match.get("barracks_status_radiant", 63))
    bs_dire    = int(match.get("barracks_status_dire",    63))
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

    # Download & decompress replay, parse kills
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            dem_path = os.path.join(tmpdir, f"{match_id}.dem")
            with st.spinner(f"Downloading replay (~110 MB)..."):
                download_and_decompress_replay(replay_url, dem_path)
            with st.spinner("Parsing replay for kill events..."):
                kills = run_kill_extractor(dem_path)
    except Exception as exc:
        return _basic_info(replay_err=str(exc), replay_source=replay_source)

    milestones = analyse_kills(kills, radiant_score + dire_score)
    special = analyse_special_events(kills)
    milestones["first_tower"] = special["first_tower"]
    milestones["first_barracks"] = special["first_barracks"]
    milestones["first_aegis"] = special["first_aegis"]
    milestones["radiant_towers"] = special["radiant_towers"]
    milestones["dire_towers"] = special["dire_towers"]
    milestones["radiant_roshans"] = special["radiant_roshans"]
    milestones["dire_roshans"] = special["dire_roshans"]
    milestones["first_tormentor"] = special["first_tormentor"]

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
    }


# ── Render ─────────────────────────────────────────────────────────────────────

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
    tc1, tc2 = st.columns(2)
    with tc1:
        st.markdown(
            f"Total Towers: **{total_t}** "
            f"(<span style='color:{RADIANT_COLOR}'>{rad_t}</span>/"
            f"<span style='color:{DIRE_COLOR}'>{dir_t}</span>)",
            unsafe_allow_html=True,
        )

    if not replay_available:
        rad_megas = data.get("radiant_megas", False)
        dire_megas = data.get("dire_megas", False)
        if rad_megas or dire_megas:
            if rad_megas and dire_megas:
                megas_label = (
                    f"<span style='color:{RADIANT_COLOR}'>{rn}</span> & "
                    f"<span style='color:{DIRE_COLOR}'>{dn}</span>"
                )
            elif rad_megas:
                megas_label = f"<span style='color:{RADIANT_COLOR}'>{rn}</span>"
            else:
                megas_label = f"<span style='color:{DIRE_COLOR}'>{dn}</span>"
        else:
            megas_label = "None"
        with tc2:
            st.markdown(f"Megacreeps: **{megas_label}**", unsafe_allow_html=True)
        st.info(data.get("replay_error") or "Replay not available yet — kill milestone data will appear once the replay is ready. ⏳")
        return

    # Roshan totals (replay only)
    rad_r = m.get("radiant_roshans", 0)
    dir_r = m.get("dire_roshans", 0)
    total_r = rad_r + dir_r
    with tc2:
        st.markdown(
            f"Total Roshans: **{total_r}** "
            f"(<span style='color:{RADIANT_COLOR}'>{rad_r}</span>/"
            f"<span style='color:{DIRE_COLOR}'>{dir_r}</span>)",
            unsafe_allow_html=True,
        )

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
