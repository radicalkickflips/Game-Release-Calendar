#!/usr/bin/env python3
"""
IGDB -> ICS sync script for upcoming game releases.

Generates an .ics calendar file of upcoming game releases for a fixed
set of platforms (PS5, PC, Xbox Series X|S, Switch, Switch 2), pulled
live from IGDB. Designed to be run on a schedule (cron, GitHub Actions,
etc.) with the output file hosted somewhere Google Calendar can
subscribe to by URL (Settings > Add calendar > From URL).

Setup:
  1. Create a Twitch developer app: https://dev.twitch.tv/console/apps
     (Category: "Application Integration" is fine. IGDB rides on Twitch's
     OAuth system even though it has nothing to do with Twitch itself.)
  2. Set environment variables:
       IGDB_CLIENT_ID=<your client id>
       IGDB_CLIENT_SECRET=<your client secret>
  3. pip install requests --break-system-packages
  4. python igdb_calendar_sync.py --output releases.ics

IGDB API docs: https://api-docs.igdb.com/
"""

import os
import sys
import re
import argparse
import time
import hashlib
from datetime import datetime, timedelta, timezone

import requests

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
IGDB_BASE_URL = "https://api.igdb.com/v4"

# Must match IGDB's platform.name field exactly. The script resolves
# these to IDs at runtime rather than hardcoding IDs, since IDs aren't
# guaranteed stable/known in advance (especially for newer platforms).
TARGET_PLATFORM_NAMES = [
    "PlayStation 5",
    "PC (Microsoft Windows)",
    "Xbox Series X|S",
    "Nintendo Switch",
    "Nintendo Switch 2",
]

# --- Importance filtering ---------------------------------------------
#
# A release is kept if EITHER:
#   1. Its IGDB "hypes" count (pre-release follows) is >= HYPE_THRESHOLD, or
#   2. One of its developers/publishers matches MAJOR_COMPANIES below.
#
# This is a hybrid filter: the hype threshold automatically catches
# breakout indie hits (a future Palworld/Balatro would clear the bar on
# its own), while the allowlist guarantees you never miss a major
# publisher's release even if IGDB's community hype is modest.
#
# Edit MAJOR_COMPANIES freely -- entries are matched as whole words,
# case-insensitively, against IGDB company names (so "Rare" matches
# "Rare Ltd" but won't false-positive on "Prepare Studios").
HYPE_THRESHOLD_DEFAULT = 15

MAJOR_COMPANIES = [
    # --- First party: platform holders + their internal studios ---
    "Nintendo",
    "Sony Interactive Entertainment",
    "PlayStation Studios",
    "Santa Monica Studio",
    "Naughty Dog",
    "Insomniac Games",
    "Guerrilla",
    "Sucker Punch",
    "Bend Studio",
    "Firesprite",
    "Housemarque",
    "Bluepoint Games",
    "Media Molecule",
    "Team Asobi",
    "Bungie",
    "Valve",
    "Xbox Game Studios",
    "Halo Studios",
    "343 Industries",
    "Bethesda Game Studios",
    "Bethesda Softworks",
    "id Software",
    "Obsidian Entertainment",
    "inXile Entertainment",
    "Ninja Theory",
    "The Coalition",
    "Turn 10 Studios",
    "Playground Games",
    "Rare",
    "Mojang Studios",
    "ZeniMax",
    "Arkane Studios",
    "MachineGames",
    "Compulsion Games",
    "Double Fine",
    "Undead Labs",
    "World's Edge",
    "Tango Gameworks",

    # --- Major third party (generous on purpose) ---
    "Electronic Arts",
    "EA Sports",
    "Ubisoft",
    "Activision",
    "Blizzard Entertainment",
    "King",
    "Take-Two",
    "Rockstar Games",
    "2K",
    "Bandai Namco",
    "Square Enix",
    "Capcom",
    "Sega",
    "Atlus",
    "Konami",
    "Warner Bros. Games",
    "Warner Bros Games",
    "NetEase",
    "Tencent",
    "miHoYo",
    "HoYoverse",
    "Epic Games",
    "CD Projekt",
    "FromSoftware",
    "Arc System Works",
    "Riot Games",
    "Amazon Games",
    "NCSoft",
    "Krafton",
    "Koei Tecmo",
    "Level-5",
    "Focus Entertainment",
    "Paradox Interactive",
    "THQ Nordic",
    "Embracer",
    "Plaion",
    "505 Games",
    "Marvelous",
    "Spike Chunsoft",
    "Nexon",
    "Netmarble",
    "Supercell",
    "Garena",
    "PlatinumGames",
    "Respawn Entertainment",
    "Techland",
    "Kadokawa",

    # --- Notable / highly-followed indie publishers (curated, not exhaustive) ---
    "Devolver Digital",
    "Annapurna Interactive",
    "Team17",
    "Coffee Stain",
    "Raw Fury",
    "11 bit studios",
    "Thunderful",
    "Kepler Interactive",
    "Hooded Horse",
    "Chucklefish",
    "Private Division",
    "tinyBuild",
    "Modus Games",
    "Fellow Traveller",
    "Whitethorn Games",
    "Skybound Games",
    "Panic Inc",
    "Yacht Club Games",
    "Team Cherry",
    "ConcernedApe",
    "Curve Games",
]

# IGDB game "category" values that are structural noise regardless of
# publisher/hype -- mods, packs, updates, and forks aren't really new
# releases in the sense you care about tracking.
EXCLUDED_CATEGORIES = {5, 12, 13, 14}  # mod, fork, pack, update


def get_access_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(
        TWITCH_TOKEN_URL,
        params={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def igdb_query(endpoint: str, query: str, client_id: str, token: str) -> list:
    resp = requests.post(
        f"{IGDB_BASE_URL}/{endpoint}",
        headers={"Client-ID": client_id, "Authorization": f"Bearer {token}"},
        data=query,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def resolve_platform_ids(names: list, client_id: str, token: str) -> dict:
    """Map platform names -> IGDB platform IDs by fetching the full list
    and matching on exact name, rather than hardcoding IDs."""
    all_platforms = []
    offset = 0
    while True:
        batch = igdb_query(
            "platforms",
            f"fields id,name; limit 500; offset {offset};",
            client_id,
            token,
        )
        all_platforms.extend(batch)
        if len(batch) < 500:
            break
        offset += 500

    by_name = {p["name"]: p["id"] for p in all_platforms}
    resolved = {}
    missing = []
    for name in names:
        if name in by_name:
            resolved[name] = by_name[name]
        else:
            missing.append(name)

    if missing:
        print(f"WARNING: no exact IGDB platform match for: {missing}", file=sys.stderr)
        for name in missing:
            key = name.split()[0].lower()
            candidates = [p["name"] for p in all_platforms if key in p["name"].lower()]
            print(f"  {name} -> similar names found: {candidates}", file=sys.stderr)

    return resolved


def fetch_upcoming_games(platform_ids: list, start_ts: int, end_ts: int, client_id: str, token: str) -> list:
    """Fetch games with >=1 release date in range on a target platform.
    Returns full game records; caller filters the release_dates array."""
    platform_ids_str = ",".join(str(i) for i in platform_ids)
    games = []
    offset = 0
    while True:
        query = (
            "fields name, summary, url, hypes, category, "
            "involved_companies.company.name, involved_companies.publisher, "
            "involved_companies.developer, "
            "release_dates.date, release_dates.platform, release_dates.human; "
            f"where release_dates.platform = ({platform_ids_str}) "
            f"& release_dates.date >= {start_ts} & release_dates.date <= {end_ts}; "
            f"sort release_dates.date asc; limit 500; offset {offset};"
        )
        batch = igdb_query("games", query, client_id, token)
        games.extend(batch)
        if len(batch) < 500:
            break
        offset += 500
        time.sleep(0.3)  # stay comfortably under rate limits

    return games


def _company_matches(major: str, name: str) -> bool:
    """Whole-word, case-insensitive match so short entries like 'Rare' or
    'King' match 'Rare Ltd' / 'King' but not 'Prepare Studios' or
    'Kingfisher Games'."""
    pattern = r"\b" + re.escape(major.lower()) + r"\b"
    return re.search(pattern, name.lower()) is not None


def is_important(game: dict, hype_threshold: int) -> bool:
    """A game passes if its IGDB hype count clears the threshold, or if
    any involved company matches the MAJOR_COMPANIES allowlist."""
    hypes = game.get("hypes") or 0
    if hypes >= hype_threshold:
        return True

    for ic in game.get("involved_companies", []) or []:
        company = ic.get("company") or {}
        name = company.get("name", "")
        if any(_company_matches(major, name) for major in MAJOR_COMPANIES):
            return True

    return False


def build_events(games: list, platform_ids: dict, start_ts: int, end_ts: int, hype_threshold: int) -> list:
    """One event per (game, date), combining all matching target platforms
    that release this game on that day into a single event. Games that
    don't clear the importance filter (see is_important) are skipped."""
    id_to_name = {v: k for k, v in platform_ids.items()}
    events = {}
    skipped_noise, skipped_unimportant = 0, 0

    for game in games:
        if game.get("category") in EXCLUDED_CATEGORIES:
            skipped_noise += 1
            continue
        if not is_important(game, hype_threshold):
            skipped_unimportant += 1
            continue

        for rd in game.get("release_dates", []):
            plat_id = rd.get("platform")
            date = rd.get("date")
            if plat_id not in id_to_name or date is None:
                continue
            if not (start_ts <= date <= end_ts):
                continue
            key = (game["id"], date)
            if key not in events:
                events[key] = {
                    "name": game.get("name", "Unknown Game"),
                    "date": date,
                    "platforms": set(),
                    "summary": game.get("summary", ""),
                    "url": game.get("url", ""),
                }
            events[key]["platforms"].add(id_to_name[plat_id])

    print(
        f"Filtered out {skipped_noise} structural-noise entries and "
        f"{skipped_unimportant} below the importance bar."
    )
    return sorted(events.values(), key=lambda e: e["date"])


def escape_ics(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold_line(line: str) -> str:
    """RFC 5545 requires content lines to be folded at 75 octets."""
    if len(line) <= 75:
        return line
    parts = []
    while len(line) > 75:
        parts.append(line[:75])
        line = " " + line[75:]
    parts.append(line)
    return "\r\n".join(parts)


def build_ics(events: list) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Overworld IGDB Sync//EN",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:Upcoming Game Releases",
    ]
    now_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for ev in events:
        date_str = datetime.fromtimestamp(ev["date"], tz=timezone.utc).strftime("%Y%m%d")
        platforms_str = ", ".join(sorted(ev["platforms"]))
        uid_source = f"{ev['name']}|{date_str}".encode("utf-8")
        uid = hashlib.sha1(uid_source).hexdigest() + "@overworld-igdb-sync"

        summary = f"{ev['name']} ({platforms_str})"
        description_parts = []
        if ev["summary"]:
            description_parts.append(ev["summary"])
        if ev["url"]:
            description_parts.append(ev["url"])
        description = "\\n\\n".join(escape_ics(p) for p in description_parts)

        lines.append("BEGIN:VEVENT")
        lines.append(fold_line(f"UID:{uid}"))
        lines.append(f"DTSTAMP:{now_stamp}")
        lines.append(f"DTSTART;VALUE=DATE:{date_str}")
        lines.append(fold_line(f"SUMMARY:{escape_ics(summary)}"))
        if description:
            lines.append(fold_line(f"DESCRIPTION:{description}"))
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def main():
    parser = argparse.ArgumentParser(description="Sync upcoming game releases from IGDB into an .ics file.")
    parser.add_argument("--output", default="releases.ics", help="Output .ics file path")
    parser.add_argument("--days-back", type=int, default=0, help="Include releases from N days in the past")
    parser.add_argument("--days-ahead", type=int, default=400, help="Include releases up to N days in the future")
    parser.add_argument(
        "--hype-threshold",
        type=int,
        default=HYPE_THRESHOLD_DEFAULT,
        help="Minimum IGDB hype count to include a game (ignored for MAJOR_COMPANIES matches)",
    )
    parser.add_argument(
        "--no-filter",
        action="store_true",
        help="Disable importance filtering entirely (include every release, like the original unfiltered version)",
    )
    args = parser.parse_args()

    client_id = os.environ.get("IGDB_CLIENT_ID")
    client_secret = os.environ.get("IGDB_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("ERROR: set IGDB_CLIENT_ID and IGDB_CLIENT_SECRET environment variables.", file=sys.stderr)
        sys.exit(1)

    token = get_access_token(client_id, client_secret)

    now = datetime.now(timezone.utc)
    start_ts = int((now - timedelta(days=args.days_back)).timestamp())
    end_ts = int((now + timedelta(days=args.days_ahead)).timestamp())

    platform_ids = resolve_platform_ids(TARGET_PLATFORM_NAMES, client_id, token)
    if not platform_ids:
        print("ERROR: none of the target platforms could be resolved to IGDB IDs.", file=sys.stderr)
        sys.exit(1)

    print(f"Resolved platforms: {platform_ids}")

    games = fetch_upcoming_games(list(platform_ids.values()), start_ts, end_ts, client_id, token)
    print(f"Fetched {len(games)} candidate games from IGDB.")

    hype_threshold = 10**9 if args.no_filter else args.hype_threshold
    if args.no_filter:
        # Effectively disables the hype gate; category cleanup still runs.
        # Force every game through the MAJOR_COMPANIES-or-hype check by
        # treating everything as if it matched a major company instead.
        for g in games:
            g["hypes"] = 10**9

    events = build_events(games, platform_ids, start_ts, end_ts, hype_threshold)
    print(f"Built {len(events)} calendar events.")

    ics_content = build_ics(events)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(ics_content)

    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
