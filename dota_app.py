import bz2
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import requests
import streamlit as st

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Dota 2 Series Analyzer", layout="wide")

OPENDOTA_BASE = "https://api.opendota.com/api"

# Path to the fat JAR — relative to this file
_HERE = os.path.dirname(os.path.abspath(__file__))
JAR_PATH = os.path.join(_HERE, "clarity_parser", "build", "libs", "kill_extractor.jar")

# Team colours
RADIANT_COLOR = "#4caf50"
DIRE_COLOR = "#e05c5c"


# ── Helpers ────────────────────────────────────────────────────────────────────

def towers_from_status(tower_status: int) -> int:
    """Return the number of towers destroyed from an 11-bit bitmask.
    Each bit = 1 means that tower is still standing, so destroyed = 11 - popcount."""
    return 11 - bin(tower_status).count("1")


def parse_match_id(url: str) -> str | None:
    """Extract a Dota 2 match ID from a Dotabuff or OpenDota URL."""
    m = re.search(r"/matches/(\d+)", url)
    return m.group(1) if m else None


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

def fetch_match_uncached(match_id: str) -> dict:
    resp = requests.get(f"{OPENDOTA_BASE}/matches/{match_id}", timeout=20)
    resp.raise_for_status()
    return resp.json()


@st.cache_data(show_spinner=False, ttl=60)
def fetch_match(match_id: str) -> dict:
    return fetch_match_uncached(match_id)


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_heroes() -> dict:
    """Return {hero_id (str) -> npc_name} mapping."""
    resp = requests.get(f"{OPENDOTA_BASE}/heroes", timeout=20)
    resp.raise_for_status()
    heroes = resp.json()
    return {str(h["id"]): h["name"] for h in heroes}


@st.cache_data(show_spinner=False, ttl=60)
def fetch_series_matches(match: dict) -> list[dict]:
    """
    Return all matches in the series, sorted by start_time ascending.
    Tries multiple methods to find matches, as OpenDota's series grouping can be delayed.
    Each item: {"match_id": str, "label": "Map 1", ...}
    """
    league_id = match.get("leagueid")
    series_id = match.get("series_id")
    radiant_team_id = match.get("radiant_team_id")
    dire_team_id = match.get("dire_team_id")
    start_time = match.get("start_time", 0)
    
    found_matches = {} # deduplicate by match_id

    # 1. Primary Method: Fetch by series_id via league matches
    if league_id and series_id:
        try:
            resp = requests.get(f"{OPENDOTA_BASE}/leagues/{league_id}/matches", timeout=10)
            if resp.status_code == 200:
                all_matches = resp.json()
                for m in all_matches:
                    if m.get("series_id") == series_id:
                        found_matches[str(m["match_id"])] = m
        except Exception:
            pass

    # 2. Fallback Method 1: Fetch by series_id via proMatches
    if series_id:
        try:
            resp = requests.get(f"{OPENDOTA_BASE}/proMatches", timeout=10)
            if resp.status_code == 200:
                pro_matches = resp.json()
                for m in pro_matches:
                    if m.get("series_id") == series_id:
                        found_matches[str(m["match_id"])] = m
        except Exception:
            pass

    # 3. Fallback Method 2: SQL Explorer (Head-to-Head within +/- 24 hours)
    # This catches matches where series_id hasn't been assigned yet.
    # Only run when series_id is NOT set — if series_id is known, Methods 1 & 2
    # already handle grouping correctly, and the broad time window would otherwise
    # pull in matches from adjacent series between the same two teams.
    if not series_id and radiant_team_id and dire_team_id and start_time:
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
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                for row in data.get('rows', []):
                    # Filter by league_id if we have one to avoid cross-tournament noise
                    if league_id and row.get('leagueid') and row.get('leagueid') != league_id:
                        continue
                    found_matches[str(row["match_id"])] = row
        except Exception as e:
            pass

    # Ensure the anchor match is always in the list
    match_id_str = str(match["match_id"])
    if match_id_str not in found_matches:
        found_matches[match_id_str] = match

    # Sort and format the results
    series = list(found_matches.values())
    series.sort(key=lambda m: m.get("start_time", 0))
    
    result = []
    for i, m in enumerate(series, start=1):
        # Do a quick check on the cached replay_url to see if it's available
        # It's important not to block here if it's slow, so we use a very short timeout
        has_replay = False
        try:
            quick_check = requests.get(f"{OPENDOTA_BASE}/matches/{m['match_id']}", timeout=3).json()
            has_replay = bool(quick_check.get("replay_url"))
        except Exception:
            pass # default to false if API is slow
            
        label = f"Map {i} ✓" if has_replay else f"Map {i} ⏳"
        btn_type = "primary" if has_replay else "secondary"
        
        result.append({
            "match_id": str(m["match_id"]),
            "label": label,
            "btn_type": btn_type,
            "start_time": m.get("start_time", 0),
        })
    return result


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

def analyse_kills(kills: list[dict], total_expected_kills: int = 0) -> dict:
    """
    kills: list of events sorted by time_f (output of run_kill_extractor).
      type="kill"   → hero kill event; killer_team 2=Radiant, 3=Dire, 0=deny (not counted)
      type="tower"  → tower death — skipped here, handled by analyse_special_events()
      type="roshan" → Roshan death — skipped here, handled by analyse_special_events()
    total_expected_kills: radiant_score + dire_score from OpenDota (authoritative total).
        Clarity emits phantom DOTA_COMBATLOG_DEATH events after the ancient is destroyed;
        those always sort chronologically last.  We stop counting as soon as we hit the
        expected total so phantom events at the tail are never reached.

    Java uses getTargetTeam() flip: killer_team = 5 - targetTeam (2→3, 3→2).
    Deny detection: when attackerTeam == targetTeam in Java, killer_team is set to 0
    and is_deny=true is emitted. These events are skipped here (not credited to anyone).
    Filters: isTargetHero() (proto bool, no string-table) and !isTargetIllusion().
    This correctly counts all kill types — direct hero kills, summon kills (Spirit Bear,
    Warlock Golem, etc.), dominated-creep kills — because in every case the target hero
    still dies and isTargetHero() fires. We never inspect the attacker, so no
    isAttackerHero() illusion-overcounting or summon-undercounting bugs.

    Returns milestone dict including first_blood.
    """
    radiant_k = dire_k = total_k = 0
    first_to: dict[int, dict | None] = {5: None, 10: None, 15: None, 20: None}
    nth_kill: dict[int, dict | None] = {10: None, 20: None, 30: None}
    first_blood: dict | None = None

    for k in kills:
        # Only process hero kill events; tower/roshan/etc. are handled separately.
        if k.get("type") not in ("kill", None):
            continue
        # killer_team: 2 = Radiant, 3 = Dire (derived from getTargetTeam() flip in Java)
        killer_team = k.get("killer_team", 0)
        if killer_team == 2:
            is_radiant = True
        elif killer_team == 3:
            is_radiant = False
        else:
            continue
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

    def _basic_info(replay_err: str | None = None) -> dict:
        return {
            "match_id": match_id,
            "radiant_name": radiant_name,
            "dire_name": dire_name,
            "radiant_win": match.get("radiant_win", False),
            "replay_available": False,
            "replay_error": replay_err,
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

    replay_url = match.get("replay_url")
    if not replay_url:
        request_opendota_parse(match_id)
        return _basic_info()

    # Download & decompress replay, parse kills
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            dem_path = os.path.join(tmpdir, f"{match_id}.dem")
            with st.spinner(f"Downloading replay (~110 MB)..."):
                download_and_decompress_replay(replay_url, dem_path)
            with st.spinner("Parsing replay for kill events..."):
                kills = run_kill_extractor(dem_path)
    except Exception as exc:
        return _basic_info(replay_err=str(exc))

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
        "replay_available": True,
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

    if replay_available:
        total = m["total_kills"]
        st.markdown(
            f"Total kills: "
            f"<span style='color:{RADIANT_COLOR}'>{rn} {m['radiant_kills']}</span> — "
            f"<span style='color:{DIRE_COLOR}'>{dn} {m['dire_kills']}</span> "
            f"({total} total)",
            unsafe_allow_html=True,
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
        st.info("Replay not available yet — kill milestone data will appear once the replay is ready. ⏳")
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
                if _k.get("killer_team", 0) not in (2, 3):
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
    placeholder="https://www.dotabuff.com/matches/8697483686",
    label_visibility="collapsed",
)

if st.button("Analyze", type="primary") and url_input.strip():
    raw = url_input.strip()
    match_id = parse_match_id(raw)
    if not match_id:
        st.error("Could not extract a match ID from that URL. Expected format: https://www.dotabuff.com/matches/XXXXXXXXXX")
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

                maps = fetch_series_matches(match)
                st.session_state.series_matches = maps if maps else [{"match_id": match_id, "label": "Map 1"}]

            except requests.HTTPError as exc:
                st.error(f"HTTP error from OpenDota: {exc}")
            except Exception as exc:
                st.error(f"Error: {exc}")

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
                        data = process_match(m["match_id"])
                        st.session_state.match_analysis = data
                        st.rerun()
                    except FileNotFoundError as exc:
                        st.error(str(exc))
                    except Exception as exc:
                        st.error(f"Error analyzing {m['label']}: {exc}")

# ── Match analysis output ──────────────────────────────────────────────────────

if st.session_state.match_analysis is not None:
    render_match_analysis(st.session_state.match_analysis)

    # Auto-retry every minute until the replay is available
    if not st.session_state.match_analysis.get("replay_available", True):
        match_id = st.session_state.match_analysis["match_id"]
        status = st.empty()
        for remaining in range(60, 0, -1):
            status.info(f"Retrying replay download in {remaining}s... ⏳")
            time.sleep(1)
        status.info("Attempting to download replay... 🔄")
        try:
            new_data = process_match(match_id)
            st.session_state.match_analysis = new_data
        except Exception:
            pass
        st.rerun()
