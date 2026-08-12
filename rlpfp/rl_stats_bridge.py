#!/usr/bin/env python3
"""
Rocket League Stats API bridge.

Connects to the local TCP socket the RL client exposes when the Stats API
is enabled, parses the JSON event stream, tracks the current match's
player roster, and serves it over a small local HTTP API so the overlay
process can poll it without touching the game socket directly.

Requires (before running):
  1. Enable the Stats API in RL:
       <RL Install Dir>\\TAGame\\Config\\TAStatsAPI.ini
       [TAGame.MatchStatsExporter_TA]
       PacketSendRate=2
       Port=49123
       WebPort=0        ; we're using the TCP port, not the websocket
     (Restart the client after editing.)

  2. pip install aiohttp --break-system-packages

Run:
  python3 rl_stats_bridge.py

Then poll:
  curl http://127.0.0.1:9090/current-lobby
  curl http://127.0.0.1:9090/pfp-cache-status
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from aiohttp import web

from .pfp_resolver import PFPResolver
import argparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("rl-stats-bridge")

# aiohttp logs every single HTTP request (the overlay polls us constantly)
# at INFO level by default, which floods the terminal. Silence it unless
# --verbose is passed.
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

RL_STATS_HOST = "127.0.0.1"
RL_STATS_PORT = 49123          # must match Port= in TAStatsAPI.ini
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 9090

RECONNECT_DELAY_SECONDS = 5


@dataclass
class Player:
    name: str
    platform: str
    uid: str
    splitscreen: int
    team_num: Optional[int] = None
    score: int = 0  # updated from UpdateState snapshots
    avatar_path: Optional[str] = None  # local cache path, filled in by PFP worker

    def key(self) -> str:
        # Bots (and possibly some edge cases) report platform "Unknown"
        # with uid "0" for EVERY bot in the match — meaning multiple bots
        # would all generate the identical key and silently overwrite each
        # other in the players dict. Fall back to using the player's name
        # in that case, since bot names are unique within a match.
        if self.platform == "Unknown" and self.uid in ("0", "", None):
            return f"Unknown|name:{self.name}|{self.splitscreen}"
        return f"{self.platform}|{self.uid}|{self.splitscreen}"


@dataclass
class LastGoal:
    scorer_name: str
    scorer_key: Optional[str]  # Player.key() if we could match them, else None
    timestamp: float

    def to_json(self) -> dict:
        return {
            "scorer_name": self.scorer_name,
            "scorer_key": self.scorer_key,
            "timestamp": self.timestamp,
        }


@dataclass
class LobbyState:
    match_guid: Optional[str] = None
    players: dict[str, Player] = field(default_factory=dict)
    updated: float = field(default_factory=time.time)
    scoreboard_visible: bool = False  # manually toggled for now (Layer 6 will drive this)
    last_goal: Optional[LastGoal] = None
    is_replay: bool = False  # from Game.bReplay in UpdateState — true during goal replays

    def to_json(self) -> dict:
        return {
            "match_guid": self.match_guid,
            "players": [asdict(p) for p in self.players.values()],
            "updated": self.updated,
            "scoreboard_visible": self.scoreboard_visible,
            "last_goal": self.last_goal.to_json() if self.last_goal else None,
            "is_replay": self.is_replay,
        }


# Single shared lobby state, mutated by the socket listener,
# read by the HTTP handlers.
lobby = LobbyState()

# PFP resolver instance (initialized on startup)
pfp_resolver: Optional[PFPResolver] = None

# Track which players we've queued for PFP fetching (to avoid hammering APIs)
pfp_fetch_queue: set[str] = set()


def parse_primary_id(primary_id: str) -> tuple[str, str, int]:
    """
    'Steam|123|0' -> ('Steam', '123', 0)
    Falls back gracefully if the format is ever unexpected.
    """
    parts = primary_id.split("|")
    platform = parts[0] if len(parts) > 0 else "Unknown"
    uid = parts[1] if len(parts) > 1 else ""
    try:
        splitscreen = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        splitscreen = 0
    return platform, uid, splitscreen


def handle_event(msg: dict) -> None:
    event = msg.get("Event")
    data = msg.get("Data", {})

    # Stats API sometimes sends Data as a JSON string instead of an object.
    # Decode it if needed.
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            log.warning("Skipping event %s: couldn't decode Data as JSON", event)
            return

    if not isinstance(data, dict):
        log.warning("Skipping event %s: data is %s after decode", event, type(data).__name__)
        return

    # Detect a new match by MatchGuid changing, and clear stale players
    # from the previous session automatically. Relying only on
    # MatchDestroyed firing isn't safe — e.g. reopening freeplay doesn't
    # always send it cleanly, which was silently leaving leftover players
    # from an earlier session in lobby.players, throwing off row sorting
    # in the very next session (a real bug, not a calibration issue).
    incoming_guid = data.get("MatchGuid")
    if incoming_guid and lobby.match_guid and incoming_guid != lobby.match_guid:
        log.info(
            "New match detected (MatchGuid changed %s -> %s) — clearing %d stale player(s)",
            lobby.match_guid, incoming_guid, len(lobby.players),
        )
        lobby.players.clear()
        lobby.last_goal = None

    # ONE-TIME raw dump of a full UpdateState event to disk, so we can see
    # the exact real field names (e.g. for score) instead of guessing.
    # Check /tmp/rl_updatestate_dump.json after this fires once.
    if event == "UpdateState" and not hasattr(handle_event, "_dumped_raw"):
        try:
            with open("/tmp/rl_updatestate_dump.json", "w") as f:
                json.dump(msg, f, indent=2)
            log.info("Dumped raw UpdateState event to /tmp/rl_updatestate_dump.json")
        except Exception as e:
            log.warning("Failed to dump raw UpdateState: %s", e)
        handle_event._dumped_raw = True

    if event == "PlayerJoined":
        platform, uid, split = parse_primary_id(data.get("PrimaryId", ""))
        player = Player(
            name=data.get("PlayerName", ""),
            platform=platform,
            uid=uid,
            splitscreen=split,
        )
        lobby.players[player.key()] = player
        lobby.match_guid = data.get("MatchGuid") or lobby.match_guid
        lobby.updated = time.time()
        log.info("PlayerJoined: %s (%s)", player.name, player.key())
        # Queue for PFP fetch
        pfp_fetch_queue.add(player.key())
        log.info("QUEUED FOR PFP: %s (queue size now: %d)", player.key(), len(pfp_fetch_queue))

    elif event == "PlayerLeft":
        platform, uid, split = parse_primary_id(data.get("PrimaryId", ""))
        name = data.get("PlayerName", "")
        # Match the same fallback key logic as Player.key() — bots share
        # platform=Unknown/uid=0, so we must key by name in that case too,
        # or PlayerLeft would remove the wrong (or no) bot.
        if platform == "Unknown" and uid in ("0", "", None):
            key = f"Unknown|name:{name}|{split}"
        else:
            key = f"{platform}|{uid}|{split}"
        removed = lobby.players.pop(key, None)
        lobby.updated = time.time()
        if removed:
            log.info("PlayerLeft: %s (%s)", removed.name, key)

    elif event == "UpdateState":
        # Periodic full snapshot — reconcile team numbers and score, and
        # pick up anyone we might have missed a PlayerJoined for (e.g.
        # bridge started mid-match).
        for p in data.get("Players", []):
            platform, uid, split = parse_primary_id(p.get("PrimaryId", ""))
            name = p.get("Name", "")
            if platform == "Unknown" and uid in ("0", "", None):
                key = f"Unknown|name:{name}|{split}"
            else:
                key = f"{platform}|{uid}|{split}"

            # We don't know the exact field name the Stats API uses for
            # score without seeing a real payload, so try the common
            # candidates. Log the raw keys once so we can confirm/adjust.
            score = (
                p.get("Score")
                or p.get("MatchScore")
                or p.get("TotalScore")
                or p.get("score")
                or 0
            )
            if not hasattr(handle_event, "_logged_player_keys"):
                log.info("UpdateState player payload keys: %s", list(p.keys()))
                handle_event._logged_player_keys = True

            existing = lobby.players.get(key)
            if existing:
                existing.team_num = p.get("TeamNum")
                existing.score = score
            else:
                lobby.players[key] = Player(
                    name=p.get("Name", ""),
                    platform=platform,
                    uid=uid,
                    splitscreen=split,
                    team_num=p.get("TeamNum"),
                    score=score,
                )
        lobby.match_guid = data.get("MatchGuid") or lobby.match_guid

        # Track replay state directly from the game snapshot — this is a
        # much more reliable signal than trying to detect the "A to skip"
        # keypress ourselves (which the Stats API doesn't expose anyway,
        # and which requires every player to press it, not just us). This
        # flips to False the instant the replay ends, however it ends.
        game = data.get("Game", {})
        if isinstance(game, dict) and "bReplay" in game:
            lobby.is_replay = bool(game["bReplay"])

        lobby.updated = time.time()

    elif event == "MatchDestroyed":
        log.info("MatchDestroyed — clearing lobby state")
        lobby.players.clear()
        lobby.match_guid = None
        lobby.updated = time.time()

    elif event == "GoalScored":
        scorer = data.get("Scorer", {})
        scorer_name = scorer.get("Name", "")
        # Try to match the scorer to a known player by display name so the
        # overlay can look up their cached avatar. Shortcut numbers aren't
        # in our roster keys, so name match is the best we've got from
        # this event alone.
        scorer_key = None
        for key, p in lobby.players.items():
            if p.name == scorer_name:
                scorer_key = key
                break
        lobby.last_goal = LastGoal(
            scorer_name=scorer_name,
            scorer_key=scorer_key,
            timestamp=time.time(),
        )
        lobby.updated = time.time()
        log.info("GoalScored by %s (matched key: %s)", scorer_name, scorer_key)

    # Other events (ball hits, boost pickups, etc.) are ignored —
    # this bridge only cares about who's in the lobby and goal timing.


async def stats_api_listener() -> None:
    """
    Connects to the RL Stats API TCP socket and feeds parsed JSON
    messages to handle_event(). Reconnects automatically if RL isn't
    running yet or the connection drops (e.g. between matches).

    Framing note: the docs don't specify a delimiter between messages,
    so we use a streaming JSON decoder that pulls one complete object
    at a time out of the buffer, however it's separated (newline,
    back-to-back, or otherwise).
    """
    decoder = json.JSONDecoder()

    while True:
        try:
            log.info("Connecting to Stats API at %s:%s ...", RL_STATS_HOST, RL_STATS_PORT)
            reader, writer = await asyncio.open_connection(RL_STATS_HOST, RL_STATS_PORT)
            log.info("Connected.")

            buffer = ""
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    log.warning("Stats API connection closed by RL.")
                    break

                buffer += chunk.decode("utf-8", errors="ignore")
                buffer = buffer.lstrip()

                # Pull as many complete JSON objects out of the buffer
                # as are currently available.
                while buffer:
                    try:
                        obj, idx = decoder.raw_decode(buffer)
                    except json.JSONDecodeError:
                        # Not enough data yet for a full object — wait
                        # for more to arrive.
                        break

                    try:
                        handle_event(obj)
                    except Exception:
                        log.exception("Error handling event: %r", obj)

                    buffer = buffer[idx:].lstrip()

        except (ConnectionRefusedError, OSError) as e:
            log.warning("Stats API not reachable (%s). Is RL running with the API enabled?", e)
        except Exception:
            log.exception("Unexpected error in Stats API listener")

        log.info("Reconnecting in %ss...", RECONNECT_DELAY_SECONDS)
        await asyncio.sleep(RECONNECT_DELAY_SECONDS)


# --- HTTP bridge -------------------------------------------------------

routes = web.RouteTableDef()


@routes.get("/current-lobby")
async def current_lobby(_request: web.Request) -> web.Response:
    return web.json_response(lobby.to_json())


@routes.get("/pfp-cache-status")
async def pfp_cache_status(_request: web.Request) -> web.Response:
    if not pfp_resolver:
        return web.json_response({"error": "PFP resolver not initialized"})
    
    cache_dir = pfp_resolver.cache_dir
    cached_files = list(cache_dir.glob("*.png")) if cache_dir.exists() else []
    
    return web.json_response({
        "cached_count": len(cached_files),
        "cache_dir": str(cache_dir),
        "fetch_queue_size": len(pfp_fetch_queue),
        "cached_files": [f.name for f in cached_files[:10]],  # Show first 10
    })


@routes.get("/scoreboard-visible")
async def get_scoreboard_visible(_request: web.Request) -> web.Response:
    return web.json_response({"visible": lobby.scoreboard_visible})


@routes.post("/scoreboard-visible")
async def set_scoreboard_visible(request: web.Request) -> web.Response:
    """
    Manual toggle for now — POST {"visible": true|false}.
    Stands in for real L3/controller-press detection until Layer 6
    (input listener) exists. Lets you test scoreboard overlay rendering
    with e.g.:
      curl -X POST http://127.0.0.1:9090/scoreboard-visible -d '{"visible": true}'
    """
    try:
        body = await request.json()
        lobby.scoreboard_visible = bool(body.get("visible", False))
        lobby.updated = time.time()
    except Exception:
        return web.json_response({"error": "expected JSON body {\"visible\": bool}"}, status=400)
    return web.json_response({"visible": lobby.scoreboard_visible})


async def pfp_fetch_worker() -> None:
    """Background task: process PFP fetch queue."""
    global pfp_resolver
    if not pfp_resolver:
        return
    
    while True:
        if pfp_fetch_queue:
            player_key = pfp_fetch_queue.pop()
            player = lobby.players.get(player_key)
            if player:
                try:
                    pfp_path = await pfp_resolver.get_pfp(
                        player.platform, player.uid, player.name
                    )
                    if pfp_path:
                        player.avatar_path = pfp_path
                        lobby.updated = time.time()
                        log.info("Fetched PFP for %s: %s", player.name, pfp_path[:60])
                    else:
                        log.warning(
                            "No PFP for %s (platform=%s, uid=%s) — see pfp-resolver "
                            "warnings above for the reason",
                            player.name, player.platform, player.uid,
                        )
                except Exception as e:
                    log.error("Failed to fetch PFP for %s: %s", player.name, e)
        await asyncio.sleep(0.5)


async def run_http_server() -> None:
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, BRIDGE_HOST, BRIDGE_PORT)
    await site.start()
    log.info("HTTP bridge listening on http://%s:%s", BRIDGE_HOST, BRIDGE_PORT)
    # Run forever
    await asyncio.Event().wait()


async def main() -> None:
    global pfp_resolver

    parser = argparse.ArgumentParser(description="RL Stats API bridge")
    parser.add_argument(
        "--verbose", action="store_true",
        help="Show per-request aiohttp access logs (noisy — off by default)",
    )
    args = parser.parse_args()
    if args.verbose:
        logging.getLogger("aiohttp.access").setLevel(logging.INFO)

    steam_key = os.getenv("STEAM_API_KEY")
    if not steam_key:
        log.warning(
            "STEAM_API_KEY not set. Steam avatars will be skipped. "
            "Get a free key at https://steamcommunity.com/dev"
        )
    
    pfp_resolver = PFPResolver(steam_api_key=steam_key)
    await pfp_resolver.__aenter__()
    
    try:
        await asyncio.gather(
            stats_api_listener(),
            run_http_server(),
            pfp_fetch_worker(),
        )
    finally:
        await pfp_resolver.__aexit__(None, None, None)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
