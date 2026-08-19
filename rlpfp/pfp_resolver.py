#!/usr/bin/env python3
"""
Layer 3: PFP Resolver

Fetches and caches player profile picture URLs from public APIs:
  - Steam: Official Steam Web API (need free API key)
  - PSN: Sony's own (unofficial/reverse-engineered) PSN API. Unlike
    Steam, this requires being authenticated AS a real PSN account —
    see the "PSN auth" section below for how that works.
  - Xbox: xboxapi.com (free community API, rate-limited)
  - Epic / Switch: returns None — RL's built-in default picture is used
    for these platforms, so we skip overlaying anything.

Config file (~/.config/rl-pfp-overlay/config.json):
  {
    "steam_api_key": "your_steam_web_api_key",
    "psn_npsso": "your_64_char_npsso",
    "xbox_api_key": "your_openxbl_api_key",
    "avatar_overrides": {
      "epic|61a21e5cbca9481e8b19b944f792d778": "~/.config/rl-pfp-overlay/my_avatar.png"
    }
  }
  All fields are optional — omit whichever platform you're not using.
  `avatar_overrides` forces a specific image for a specific platform +
  account ID, regardless of what platform avatar would otherwise be
  fetched — the main use case is Epic (no public avatar API, so it
  otherwise falls back to a generic placeholder), but it works for any
  platform. Key format "{platform}|{platform_id}" (platform lowercase),
  value is a path to a local image (~/ is expanded). See `rl-pfp config`.
  `psn_npsso` is only needed ONCE to bootstrap PSN auth (or again if the
  resulting refresh token ever fully expires, ~2 months). Day-to-day,
  the resolver refreshes its own PSN access token automatically using
  the cached refresh token.
  `xbox_api_key` is a static OpenXBL (xbl.io) key — sign in once at
  xbl.io with a Microsoft account, no per-run auth dance needed.
  Environment variables (STEAM_API_KEY, PSN_NPSSO, XBOX_API_KEY) still
  work and take priority over the config file, for backwards compat.

Caching:
  ~/.cache/rl-pfp-overlay/
    steam_76561198123456789.png
    psn_username.png
    xbox_gamertag.png
    psn_tokens.json   <- auto-managed PSN OAuth token cache, NOT
                          user-edited (separate from config.json so
                          your config edits never get overwritten by
                          the auto-refresh logic)

Usage:
  resolver = PFPResolver(steam_api_key="your_api_key")
  url = await resolver.get_pfp("Steam", "123456789")  # returns cached path or URL
"""

import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import aiohttp

from .config import CACHE_DIR, CONFIG_DIR, CONFIG_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
log = logging.getLogger("pfp-resolver")

CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days

# Auto-managed PSN OAuth token cache — access_token, refresh_token, and
# their expiry timestamps. Never edit this by hand; it's rewritten
# every time the resolver bootstraps or refreshes.
PSN_TOKEN_CACHE_PATH = CACHE_DIR / "psn_tokens.json"

# --- PSN OAuth constants ------------------------------------------------
# These are NOT secrets we invented — they're the public client
# id/secret pair used by Sony's own PS App (reverse-engineered by the
# community, e.g. andshrew/PlayStation-Trophies and psnawp). Every
# PSN-auth tool uses these same values; it's how the "authenticate as
# your own PSN account" flow identifies itself as a PlayStation client.
PSN_OAUTH_CLIENT_ID = "09515159-7237-4370-9b40-3806e67c0891"
PSN_OAUTH_CLIENT_SECRET = "ucPjka5tntB2KqsP"
PSN_OAUTH_REDIRECT_URI = "com.scee.psxandroid.scecompcall://redirect"
PSN_OAUTH_SCOPE = "psn:mobile.v2.core psn:clientapp"
PSN_OAUTH_AUTHORIZE_URL = "https://ca.account.sony.com/api/authz/v3/oauth/authorize"
PSN_OAUTH_TOKEN_URL = "https://ca.account.sony.com/api/authz/v3/oauth/token"
# Legacy PSN profile endpoint — returns avatarUrls among other fields.
PSN_PROFILE_URL = "https://us-prof.np.community.playstation.net/userProfile/v1/users/{username}/profile2"
# Refresh a bit before the real expiry to avoid using a token that
# expires mid-request.
PSN_TOKEN_EXPIRY_MARGIN_SECONDS = 60

# --- Xbox (OpenXBL / xbl.io) --------------------------------------------
# Much simpler than PSN: a static API key from xbl.io (you sign in once
# with a Microsoft account on their site to get it), no OAuth dance or
# token refresh needed on our end. Free tier: 150 requests/hour.
XBOX_PROFILE_URL = "https://api.xbl.io/v2/player/gamertag/{gamertag}"

# Per-account overrides: use a specific custom image for a specific
# player (by platform + platform_id), instead of the generic
# placeholder or a fetched avatar. Useful for your own account, since
# Epic has no public avatar API — or for anyone you'd rather show a
# fixed image for regardless of platform.
#
# Configured via config.json's "avatar_overrides" dict, not here —
# see `rl-pfp config`. Key format: "{platform}|{platform_id}" (platform
# lowercase). Value: absolute path to a local image file (PNG
# recommended — GTK4's Gtk.Picture doesn't animate GIFs, so an animated
# GIF here will only ever show its first frame unless the overlay adds
# manual frame cycling).


def _load_config() -> dict:
    """Load ~/.config/rl-pfp-overlay/config.json, or {} if missing/invalid."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception as e:
        log.warning("Failed to parse %s: %s", CONFIG_PATH, e)
        return {}


class PFPResolver:
    def __init__(self, steam_api_key: Optional[str] = None):
        self.config = _load_config()

        # Env var > explicit constructor arg > config file, for
        # backwards compatibility with the old STEAM_API_KEY workflow.
        self.steam_api_key = (
            os.getenv("STEAM_API_KEY") or steam_api_key or self.config.get("steam_api_key")
        )
        self.psn_npsso = os.getenv("PSN_NPSSO") or self.config.get("psn_npsso")
        self.xbox_api_key = os.getenv("XBOX_API_KEY") or self.config.get("xbox_api_key")

        # Per-account avatar overrides — see `rl-pfp config`. Guard against
        # a malformed config.json (e.g. a typo turning this into a string)
        # rather than crashing resolver init over it.
        raw_overrides = self.config.get("avatar_overrides", {})
        self.avatar_overrides = raw_overrides if isinstance(raw_overrides, dict) else {}

        # Epic placeholder can be turned off in `rl-pfp config` for anyone
        # who'd rather Epic players fall back to RL's built-in default
        # picture than see the generic placeholder.
        self.epic_placeholder_disabled = bool(self.config.get("epic_placeholder_disabled"))

        self.session: Optional[aiohttp.ClientSession] = None
        self.cache_dir = CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        # In-memory guard so concurrent get_pfp() calls for multiple PSN
        # players don't all race to bootstrap/refresh at once.
        self._psn_token_lock = asyncio.Lock()

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            }
        )
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    def _is_cache_valid(self, cache_path: Path) -> bool:
        """Check if cached file exists and hasn't expired."""
        if not cache_path.exists():
            return False
        age = time.time() - cache_path.stat().st_mtime
        return age < CACHE_TTL_SECONDS

    async def _fetch_with_timeout(self, url: str, timeout: int = 5) -> Optional[str]:
        """Fetch URL with timeout, return response text or None."""
        if not self.session:
            return None
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 200:
                    return await resp.text()
        except Exception as e:
            log.warning("Failed to fetch %s: %s", url, e)
        return None

    async def _download_and_cache(self, url: str, cache_path: Path) -> Optional[str]:
        """Download image from URL, save to cache, return local path or URL."""
        if not self.session:
            return url
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    cache_path.write_bytes(data)
                    log.info("Cached %s (%d bytes)", cache_path.name, len(data))
                    return str(cache_path)
                else:
                    log.warning(
                        "Download failed for %s: HTTP %d", url, resp.status
                    )
        except Exception as e:
            log.warning("Failed to download/cache %s: %s", url, e)
        # Return None on failure, not the raw URL — the overlay loads
        # avatar_path as a local file path (Gdk.Texture.new_from_filename),
        # so handing back an http:// URL here would silently fail there
        # instead of here, making it much harder to diagnose.
        return None

    async def get_steam_pfp(self, steam_id: str) -> Optional[str]:
        """Fetch Steam profile picture via Steam Web API."""
        if not self.steam_api_key:
            log.warning("Steam API key not configured, skipping Steam avatar")
            return None

        cache_path = self.cache_dir / f"steam_{steam_id}.png"
        if self._is_cache_valid(cache_path):
            log.debug("Steam cache hit: %s", steam_id)
            return str(cache_path)

        url = (
            f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
            f"?key={self.steam_api_key}&steamids={steam_id}"
        )
        resp_text = await self._fetch_with_timeout(url)
        if not resp_text:
            return None

        try:
            data = json.loads(resp_text)
            players = data.get("response", {}).get("players", [])
            if players:
                avatar_url = players[0].get("avatarfull")
                if avatar_url:
                    return await self._download_and_cache(avatar_url, cache_path)
        except Exception as e:
            log.error("Failed to parse Steam API response: %s", e)
        return None

    # --- PSN OAuth ------------------------------------------------------

    def _load_psn_tokens(self) -> dict:
        if not PSN_TOKEN_CACHE_PATH.exists():
            return {}
        try:
            return json.loads(PSN_TOKEN_CACHE_PATH.read_text())
        except Exception as e:
            log.warning("Failed to parse PSN token cache: %s", e)
            return {}

    def _save_psn_tokens(self, tokens: dict) -> None:
        try:
            PSN_TOKEN_CACHE_PATH.write_text(json.dumps(tokens, indent=2))
        except Exception as e:
            log.warning("Failed to write PSN token cache: %s", e)

    async def _psn_bootstrap(self, npsso: str) -> Optional[dict]:
        """First-time (or NPSSO-refresh) auth: exchange an NPSSO for a
        fresh access_token + refresh_token pair."""
        if not self.session:
            return None

        # Step 1: exchange the NPSSO cookie for a short-lived auth code.
        # This is a GET that 302-redirects with ?code=... in the Location
        # header — we deliberately don't follow the redirect.
        try:
            async with self.session.get(
                PSN_OAUTH_AUTHORIZE_URL,
                params={
                    "access_type": "offline",
                    "client_id": PSN_OAUTH_CLIENT_ID,
                    "response_type": "code",
                    "scope": PSN_OAUTH_SCOPE,
                    "redirect_uri": PSN_OAUTH_REDIRECT_URI,
                },
                cookies={"npsso": npsso},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                location = resp.headers.get("Location", "")
        except Exception as e:
            log.error("PSN NPSSO exchange failed (network error): %s", e)
            return None

        if "code=" not in location:
            log.error(
                "PSN NPSSO exchange failed — NPSSO is likely expired or "
                "invalid. Get a fresh one by logging into playstation.com "
                "in a browser, then visiting "
                "https://ca.account.sony.com/api/v1/ssocookie in the same "
                "browser session, and updating psn_npsso in %s",
                CONFIG_PATH,
            )
            return None

        from urllib.parse import parse_qs, urlparse
        auth_code = parse_qs(urlparse(location).query).get("code", [None])[0]
        if not auth_code:
            log.error("PSN NPSSO exchange: couldn't parse code from redirect")
            return None

        # Step 2: exchange the auth code for access + refresh tokens.
        return await self._psn_token_request({
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": PSN_OAUTH_REDIRECT_URI,
        })

    async def _psn_refresh(self, refresh_token: str) -> Optional[dict]:
        """Use a cached refresh_token to get a new access_token (and
        usually a new refresh_token too) without touching the NPSSO."""
        return await self._psn_token_request({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": PSN_OAUTH_SCOPE,
        })

    async def _psn_token_request(self, form_data: dict) -> Optional[dict]:
        """Shared POST to the token endpoint for both bootstrap and
        refresh — same endpoint, different grant_type."""
        if not self.session:
            return None

        basic = base64.b64encode(
            f"{PSN_OAUTH_CLIENT_ID}:{PSN_OAUTH_CLIENT_SECRET}".encode()
        ).decode()

        try:
            async with self.session.post(
                PSN_OAUTH_TOKEN_URL,
                data=form_data,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.error("PSN token request failed: HTTP %d %s", resp.status, body[:200])
                    return None
                data = await resp.json()
        except Exception as e:
            log.error("PSN token request failed (network/parse error): %s", e)
            return None

        now = time.time()
        tokens = {
            "access_token": data.get("access_token"),
            "access_token_expires_at": now + float(data.get("expires_in", 3600)),
            "refresh_token": data.get("refresh_token"),
            # PSN typically doesn't return refresh_token_expires_in on a
            # plain refresh (only on the initial NPSSO bootstrap) — if
            # absent, keep whatever we already had cached rather than
            # nuking a still-valid expiry.
            "refresh_token_expires_at": (
                now + float(data["refresh_token_expires_in"])
                if "refresh_token_expires_in" in data
                else self._load_psn_tokens().get("refresh_token_expires_at")
            ),
        }
        if not tokens["access_token"]:
            log.error("PSN token response missing access_token: %s", data)
            return None
        return tokens

    async def _get_psn_access_token(self) -> Optional[str]:
        """Return a valid PSN access token, transparently refreshing or
        bootstrapping as needed. This is the only method other code
        should call — it hides the whole refresh/bootstrap dance."""
        async with self._psn_token_lock:
            tokens = self._load_psn_tokens()
            now = time.time()

            # Cached access token still good?
            if (
                tokens.get("access_token")
                and tokens.get("access_token_expires_at", 0) > now + PSN_TOKEN_EXPIRY_MARGIN_SECONDS
            ):
                return tokens["access_token"]

            # Access token stale/missing — try the refresh token first,
            # since it doesn't require the (manually-copied) NPSSO.
            refresh_token = tokens.get("refresh_token")
            refresh_expires_at = tokens.get("refresh_token_expires_at")
            refresh_still_valid = (
                refresh_expires_at is None  # unknown expiry — try anyway
                or refresh_expires_at > now + PSN_TOKEN_EXPIRY_MARGIN_SECONDS
            )
            if refresh_token and refresh_still_valid:
                new_tokens = await self._psn_refresh(refresh_token)
                if new_tokens:
                    # Refresh responses sometimes omit refresh_token —
                    # keep the old one in that case.
                    if not new_tokens.get("refresh_token"):
                        new_tokens["refresh_token"] = refresh_token
                    self._save_psn_tokens(new_tokens)
                    log.info("PSN access token refreshed")
                    return new_tokens["access_token"]
                log.warning("PSN refresh_token rejected — falling back to NPSSO bootstrap")

            # Last resort: full NPSSO bootstrap. Requires psn_npsso to be
            # set in config.json or PSN_NPSSO env var.
            if self.psn_npsso:
                new_tokens = await self._psn_bootstrap(self.psn_npsso)
                if new_tokens:
                    self._save_psn_tokens(new_tokens)
                    log.info("PSN authenticated fresh via NPSSO")
                    return new_tokens["access_token"]

            log.warning(
                "No valid PSN token available — set psn_npsso in %s "
                "(one-time; see docstring at top of this file for how "
                "to get one). PSN avatars will be skipped.",
                CONFIG_PATH,
            )
            return None

    async def get_psn_pfp(self, psn_username: str) -> Optional[str]:
        """Fetch PSN profile picture via Sony's own profile API,
        authenticated as your own PSN account (see docstring)."""
        cache_path = self.cache_dir / f"psn_{psn_username}.png"
        if self._is_cache_valid(cache_path):
            log.debug("PSN cache hit: %s", psn_username)
            return str(cache_path)

        access_token = await self._get_psn_access_token()
        if not access_token:
            return None

        if not self.session:
            return None

        url = PSN_PROFILE_URL.format(username=psn_username)
        try:
            async with self.session.get(
                url,
                params={"fields": "avatarUrls"},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    log.warning(
                        "PSN profile lookup failed for %s: HTTP %d",
                        psn_username, resp.status,
                    )
                    return None
                data = await resp.json()
        except Exception as e:
            log.warning("PSN profile lookup failed for %s: %s", psn_username, e)
            return None

        avatar_urls = data.get("profile", {}).get("avatarUrls", [])
        if not avatar_urls:
            log.info("PSN profile for %s has no avatarUrls", psn_username)
            return None

        # avatarUrls is a list of {"size": "m"/"l"/"xl", "avatarUrl": url}
        # — prefer the largest available.
        size_priority = {"xl": 0, "l": 1, "m": 2}
        avatar_urls.sort(key=lambda a: size_priority.get(a.get("size"), 99))
        avatar_url = avatar_urls[0].get("avatarUrl")
        if not avatar_url:
            return None

        return await self._download_and_cache(avatar_url, cache_path)

    async def get_xbox_pfp(self, xbox_gamertag: str) -> Optional[str]:
        """Fetch Xbox gamerpic via OpenXBL (xbl.io) — a static API key,
        no OAuth/refresh dance needed. See config docstring at top of
        this file for how to get one."""
        if not self.xbox_api_key:
            log.warning("xbox_api_key not configured, skipping Xbox avatar")
            return None

        cache_path = self.cache_dir / f"xbox_{xbox_gamertag}.png"
        if self._is_cache_valid(cache_path):
            log.debug("Xbox cache hit: %s", xbox_gamertag)
            return str(cache_path)

        if not self.session:
            return None

        url = XBOX_PROFILE_URL.format(gamertag=xbox_gamertag)
        try:
            async with self.session.get(
                url,
                headers={
                    "X-Authorization": self.xbox_api_key,
                    "Accept": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 429:
                    log.warning("OpenXBL rate limit hit (429) for %s", xbox_gamertag)
                    return None
                if resp.status != 200:
                    log.warning(
                        "OpenXBL lookup failed for %s: HTTP %d",
                        xbox_gamertag, resp.status,
                    )
                    return None
                data = await resp.json()
        except Exception as e:
            log.warning("OpenXBL lookup failed for %s: %s", xbox_gamertag, e)
            return None

        avatar_url = data.get("profilePicture")
        if not avatar_url:
            log.info("OpenXBL profile for %s has no profilePicture", xbox_gamertag)
            return None

        return await self._download_and_cache(avatar_url, cache_path)

    async def get_pfp(
        self, platform: str, platform_id: str, username: str = ""
    ) -> Optional[str]:
        """
        Fetch PFP for a player.

        Args:
            platform: "Steam", "PSN", "Xbox" (Epic/Switch use RL's built-in defaults)
            platform_id: platform-specific ID (e.g., "123456789" for Steam)
            username: optional display name (used for PSN/Xbox lookup if platform_id is unavailable)

        Returns:
            Local cache path (str) or direct URL (str), or None if not supported.
        """
        platform = platform.lower()

        # Per-account override — check this before any platform-specific
        # logic, so it works regardless of platform (Steam, Epic, etc.).
        override_key = f"{platform}|{platform_id}"
        if override_key in self.avatar_overrides:
            override_path = Path(self.avatar_overrides[override_key]).expanduser()
            if override_path.exists():
                return str(override_path)
            else:
                log.warning(
                    "Custom avatar override configured for %s but file not found: %s",
                    override_key, override_path,
                )

        # The Stats API sends platform-specific strings like "PS4", "PS5",
        # "XboxOne", "Switch", etc — not generic "psn"/"xbox" labels. Match
        # on substring so we catch all known variants.
        if platform == "steam":
            return await self.get_steam_pfp(platform_id)
        elif "ps" in platform:  # "PS4", "PS5", "PSN", "PSVita", etc.
            # PSN username, not a numeric ID — same pattern as Xbox below.
            return await self.get_psn_pfp(username or platform_id)
        elif "xbox" in platform:  # "Xbox", "XboxOne", "XboxSeriesX", etc.
            # Xbox typically needs gamertag, not a numeric ID.
            return await self.get_xbox_pfp(username or platform_id)
        elif "epic" in platform:  # "Epic", "EpicGames", etc.
            if self.epic_placeholder_disabled:
                # Opted out via `rl-pfp config` — fall back to RL's own
                # default picture instead of the generic placeholder.
                return None
            # Epic has no public avatar API. Return a placeholder so Epic
            # players show *something* instead of blank.
            placeholder = self.cache_dir / "epic_placeholder.png"
            if placeholder.exists():
                return str(placeholder)
            else:
                log.warning("Epic placeholder not found at %s", placeholder)
                return None
        else:
            # Switch and others use RL's built-in defaults; skip overlay.
            return None


async def main():
    """Demo: resolve a few test players."""
    # Note: set STEAM_API_KEY environment variable or pass it directly.
    import os

    steam_key = os.getenv("STEAM_API_KEY")
    if not steam_key:
        log.warning("STEAM_API_KEY not set; Steam avatars will be skipped")

    async with PFPResolver(steam_api_key=steam_key) as resolver:
        # Test Steam
        steam_result = await resolver.get_pfp("Steam", "76561198079681869")
        log.info("Steam result: %s", steam_result)

        # Test PSN (requires psn_npsso set in config.json or PSN_NPSSO env
        # var on first run; auto-refreshes after that)
        psn_result = await resolver.get_pfp("PSN", "", username="trophies")
        log.info("PSN result: %s", psn_result)

        # Test Epic (uses RL built-in defaults, skipped)
        epic_result = await resolver.get_pfp("Epic", "test_epic_id")
        log.info("Epic result (skipped, RL defaults): %s", epic_result)

        # Test Switch (uses RL built-in defaults, skipped)
        switch_result = await resolver.get_pfp("Switch", "test_switch_id")
        log.info("Switch result (skipped, RL defaults): %s", switch_result)


if __name__ == "__main__":
    asyncio.run(main())
