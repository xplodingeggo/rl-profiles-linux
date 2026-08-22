-- PfpOverlay: shows Steam/Xbox/PSN profile info for everyone in your
-- match. Lua port of the rl-pfp-overlay Python/Windows project's
-- resolver, scaffolded against Hebnix's plugin API.
--
-- STATUS: Full pipeline wired up — Steam/Xbox metadata lookup, avatar
-- image download, and rendering. Metadata (JSON) fetches use
-- hebnix.http_get_async + plugin.on_http_response as normal. Avatar
-- image bytes use hebnix.http_download_async + on_http_download_response
-- instead — http_get_async's response body goes through Rust's
-- res.text(), which corrupts binary data (confirmed against the host
-- source); http_download_async is a small patch added to fix this (see
-- send_req_bytes/PluginHttpDownloadRes in lua_api.rs) by using
-- res.bytes() so the image survives intact. Requires a Hebnix build
-- with that patch — plain http_get_async will NOT work for downloading
-- avatars, only for the JSON lookups.
-- Rendering uses on_overlay/draw.image (confirmed with the Hebnix
-- owner as the right mechanism for exact pixel placement, same as
-- RankViewer's rank icons); images MUST be a relative path under this
-- plugin's own assets/ folder — draw.image silently rejects absolute
-- paths (logged by the host, no Lua error). Downloaded avatars get
-- written to assets/cache/<platform>_<id>.<ext> for that reason.
-- avatar_overrides_json still works too, but its paths must ALSO be
-- relative to assets/ now (e.g. "assets/me.png"), not an absolute
-- filesystem path.
-- Scoreboard positioning is now ported from the Python project's
-- layout.py (see the Layout section below) — avatars render at RL's
-- real scoreboard slot positions, sorted by score within each team,
-- while the configurable scoreboard_button bind is held (matching how
-- RL's own scoreboard reveal works). rl_ui_scale must match RL's
-- Options > Video > Interface Scale exactly for positions to land
-- correctly. A separate top-left debug stack (draw_debug_stack) still
-- exists behind overlay_toggle_bind for troubleshooting the resolver
-- pipeline independent of position calibration.
-- PSN now works too, via NPSSO-based OAuth (see the PSN auth section
-- below) — requires hebnix.http_get_no_redirect_async, a small patch
-- for reading a redirect's Location header without following it (see
-- http_client_no_redirect/send_req_location/PluginHttpRedirectRes in
-- lua_api.rs, plus on_http_redirect_response in manager.rs/app.rs).

local plugin = {}

-- io.open resolves relative paths against the process's CWD, which the
-- host never chdir()s — it isn't reliably anything in particular (confirmed:
-- draw.image/ui.image resolve paths themselves via base_dir/plugins/<slug>,
-- completely independent of process CWD, which is why those always worked
-- while io.open never did). hebnix.plugin_dir() is a small patch added
-- alongside http_download_async that exposes that same absolute path to
-- Lua, so io.open writes can be anchored correctly regardless of how/where
-- the process was launched from. Requires a Hebnix build with that patch.
local PLUGIN_DIR = hebnix.plugin_dir()

-- ==========================================
-- Config
-- ==========================================

local function steam_api_key() return hebnix.get_string("steam_api_key", "") end
local function xbox_api_key() return hebnix.get_string("xbox_api_key", "") end

-- Avatar overrides now live in a real standalone JSON file next to
-- main.lua (not buried in a string field inside plugins/config/<slug>/
-- settings.toml) so "open the overrides file" has an actual honest
-- file to open, and so it's directly editable in any text editor.
local OVERRIDES_PATH = PLUGIN_DIR .. "/overrides.json"

local function read_overrides_file()
    local f = io.open(OVERRIDES_PATH, "r")
    if not f then return nil end
    local content = f:read("*a")
    f:close()
    local ok, decoded = pcall(hebnix.json_decode, content)
    if ok and type(decoded) == "table" then return decoded end
    return nil
end

-- Hand-rolled pretty printer (one "key": "value" per line, keys sorted)
-- instead of hebnix.json_encode's compact single-line output — the whole
-- point of this being a real file is that it's pleasant to open and
-- hand-edit, and hebnix.json_encode is still used per-field to get
-- correct JSON string escaping for keys/values.
local function write_overrides_file(tbl)
    local keys = {}
    for k in pairs(tbl) do table.insert(keys, k) end
    table.sort(keys)
    local lines = {}
    for i, k in ipairs(keys) do
        local ok_k, jk = pcall(hebnix.json_encode, k)
        local ok_v, jv = pcall(hebnix.json_encode, tbl[k])
        if ok_k and ok_v then
            lines[#lines + 1] = "  " .. jk .. ": " .. jv .. (i < #keys and "," or "")
        end
    end
    local f = io.open(OVERRIDES_PATH, "w")
    if not f then
        hebnix.log("PfpOverlay: FAILED to open " .. OVERRIDES_PATH .. " for writing")
        return false
    end
    if #lines == 0 then
        f:write("{}\n")
    else
        f:write("{\n" .. table.concat(lines, "\n") .. "\n}\n")
    end
    f:close()
    return true
end

local function load_overrides()
    local from_file = read_overrides_file()
    if from_file then return from_file end
    -- One-time migration: earlier versions of this plugin stored
    -- overrides as a JSON string in the settings.toml store instead of
    -- a real file. Fall back to that the first time overrides.json
    -- doesn't exist yet, and write it out as a real file from then on.
    local raw = hebnix.get_string("avatar_overrides_json", "{}")
    local ok, decoded = pcall(hebnix.json_decode, raw)
    if ok and type(decoded) == "table" and next(decoded) ~= nil then
        write_overrides_file(decoded)
        hebnix.log("PfpOverlay: migrated avatar_overrides_json setting -> " .. OVERRIDES_PATH)
        return decoded
    end
    return {}
end

-- ==========================================
-- Layout (ported from the Python rl-pfp-overlay project's layout.py) —
-- scoreboard slot positions, calibrated at REFERENCE_RESOLUTION /
-- REFERENCE_UI_SCALE and scaled via per-axis quadratic (occasionally
-- cubic) fit curves. Only the Windows-calibrated constants are ported
-- (no RL_LINUX_CALIBRATION toggle — not relevant here). Unlike the
-- Python version, this doesn't need a separate window-geometry offset
-- step: Hebnix's on_overlay already hands draw coordinates directly in
-- the game's own screen space (confirmed w/h match the real display
-- resolution), so REFERENCE_RESOLUTION-relative scaling is all that's
-- needed. See the original layout.py for the full derivation history
-- behind every constant below — they're the product of extensive
-- real-hardware box_probe calibration, not guesses.
-- ==========================================

local REFERENCE_RESOLUTION = { 2560, 1440 }
local REFERENCE_UI_SCALE = 0.75
local ROW_HEIGHT = 56
local BOX_SIZE = 48
local SLOT_X = 714

-- RL's "Interface Scale" video setting can't be read from the OS — it's
-- a config value the user sets to match, same as the Python version's
-- rl_ui_scale. Falls back to REFERENCE_UI_SCALE (no correction) if unset.
local function ui_scale()
    local raw = hebnix.get_string("rl_ui_scale", "")
    local value = tonumber(raw)
    if not value or value <= 0 then return REFERENCE_UI_SCALE end
    return value
end

local SCOREBOARD_LAYOUTS = {
    [4] = { blue = 419, orange = 796 },
    [3] = { blue = 528, orange = 792 },
    [2] = { blue = 591, orange = 793 },
    [1] = { blue = 655, orange = 798 },
}

local SCOREBOARD_UI_QUAD = {
    x = { 8.0, -766.0, 1284.0 },
    y = { 308.0, -559.0, 799.0 },
    size = { 0.0, 64.0, 0.0 },
}
local ROW_HEIGHT_QUAD = { 88.0, -86.0, 72.0 }

-- Per (team, team_size) extra delta layered on top of the shared y-curve
-- above — neither team's row0 scales with UI scale exactly the way a
-- single shared curve predicts. 3v3 blue is a 4-coefficient CUBIC (not
-- the usual quadratic) — see layout.py's EXTRA_Y_QUAD comment for why.
local EXTRA_Y_QUAD = {
    blue = {
        [1] = { -144.0, 180.0, -54.0 },
        [2] = { -304.0, 380.0, -114.0 },
        [3] = { 483.83838383838383, -1552.6363636363637, 1366.2373737373737, -355.43939393939394 },
        [4] = { -256.0, 320.0, -96.0 },
    },
    orange = {
        [1] = { 192.0, -240.0, 72.0 },
        [2] = { 232.0, -290.0, 87.0 },
        [3] = { 240.0, -300.0, 90.0 },
        [4] = { 216.0, -270.0, 81.0 },
    },
}
-- Flat (not scale-dependent) x nudge — only s=0.75 measurements exist.
local EXTRA_X_NUDGE = {
    blue = { [1] = 1, [2] = 1 },
}

-- Evaluates a polynomial via Horner's method, highest-order-first
-- coefficients. Accepts both the usual 3-tuple quadratics and the one
-- 4-tuple cubic (EXTRA_Y_QUAD.blue[3]).
local function quad(coefs, s)
    local result = 0.0
    for _, c in ipairs(coefs) do
        result = result * s + c
    end
    return result
end

-- Any fraction above .0 rounds UP — matches an in-game rounding quirk
-- found while calibrating at 65% UI scale.
local function round_up(value)
    return math.ceil(value)
end

-- Scale a reference-layout (x, y, w, h) slot — measured at
-- REFERENCE_RESOLUTION/REFERENCE_UI_SCALE — to the actual screen size
-- and configured rl_ui_scale.
local function scale_slot(x, y, w, h, ui_quad, screen_w, screen_h)
    local s = ui_scale()
    local dx = quad(ui_quad.x, s) - quad(ui_quad.x, REFERENCE_UI_SCALE)
    local dy = quad(ui_quad.y, s) - quad(ui_quad.y, REFERENCE_UI_SCALE)
    local dsize = quad(ui_quad.size, s) - quad(ui_quad.size, REFERENCE_UI_SCALE)
    x = x + dx
    y = y + dy
    w = math.max(1.0, w + dsize)
    h = math.max(1.0, h + dsize)

    local res_scale_x = screen_w / REFERENCE_RESOLUTION[1]
    local res_scale_y = screen_h / REFERENCE_RESOLUTION[2]

    return round_up(x * res_scale_x), round_up(y * res_scale_y),
        math.max(1, round_up(w * res_scale_x)), math.max(1, round_up(h * res_scale_y))
end

local function scaled_row_height()
    return quad(ROW_HEIGHT_QUAD, ui_scale())
end

-- Builds the {team, row, x, y, w, h} slot list for a given team size
-- (1-4), scaled to the actual screen dimensions.
local function get_scoreboard_slots(team_size, screen_w, screen_h)
    team_size = math.max(1, math.min(4, team_size))
    local layout = SCOREBOARD_LAYOUTS[team_size]
    local row_height = scaled_row_height()
    local slots = {}
    for _, team in ipairs({ "blue", "orange" }) do
        local row0_x = SLOT_X + ((EXTRA_X_NUDGE[team] or {})[team_size] or 0)
        local row0_y = layout[team]
        local extra_y = (EXTRA_Y_QUAD[team] or {})[team_size]
        if extra_y then
            row0_y = row0_y + quad(extra_y, ui_scale())
        end
        for row = 0, team_size - 1 do
            local x, y, w, h = scale_slot(row0_x, row0_y + row * row_height, BOX_SIZE, BOX_SIZE,
                SCOREBOARD_UI_QUAD, screen_w, screen_h)
            table.insert(slots, { team = team, row = row, x = x, y = y, w = w, h = h })
        end
    end
    return slots
end

-- Goal-scored nameplate slot — the "SCORED BY" nameplate RL shows during
-- a goal replay. Ported from layout.py's GOAL_NAMEPLATE_* constants
-- (Windows calibration only, same as the scoreboard section above).
local GOAL_NAMEPLATE_DELAY_SECONDS = 3.5 -- confirmed via real testing to match RL's replay timing
local GOAL_NAMEPLATE_DURATION_SECONDS = 11 -- how long to show it after GoalScored, once the delay has elapsed
local GOAL_NAMEPLATE_REFERENCE_SLOT = { 1047, 1223, 75, 75 } -- x, y, w, h at REFERENCE_RESOLUTION/REFERENCE_UI_SCALE (1044+3 x-nudge, 1220+3 y-nudge baked in)
local NAMEPLATE_EXTRA_Y_QUAD = { -306.0, 382.5, -114.75 }
local NAMEPLATE_UI_QUAD = {
    x = { 0.0, -312.0, 234.0 },
    y = { 304.0, -671.0, 1516.0 },
    size = { 0.0, 100.0, 0.0 },
}

local function get_goal_nameplate_slot(screen_w, screen_h)
    local x, y, w, h = GOAL_NAMEPLATE_REFERENCE_SLOT[1], GOAL_NAMEPLATE_REFERENCE_SLOT[2],
        GOAL_NAMEPLATE_REFERENCE_SLOT[3], GOAL_NAMEPLATE_REFERENCE_SLOT[4]
    y = y + quad(NAMEPLATE_EXTRA_Y_QUAD, ui_scale())
    return scale_slot(x, y, w, h, NAMEPLATE_UI_QUAD, screen_w, screen_h)
end

-- ==========================================
-- Player tracking
-- ==========================================

-- players[key] = {
--   name, platform, platform_id, is_bot,
--   raw_pid (the real PrimaryId as reported by the game — needed for
--   hebnix.platform_tag()/is_bot() calls, since key itself is a
--   synthetic name-based id for bots, see player_key()),
--   avatar_path (local file, ready to render),
--   avatar_url (resolved but not downloaded yet),
--   status (human-readable state, shown in settings/window for debugging)
--   team_num, score (refreshed every UpdateState tick — drive scoreboard
--   row assignment, same "highest score first" sort RL's real scoreboard
--   uses)
-- }
-- Table is keyed by player_key(pid, name), NOT raw pid — bots all share
-- the same (or an empty) PrimaryId, so raw pid can't be used as a
-- unique key for them. See player_key() below.
local players = {}
local player_order = {}
local seen = {}
local pending_requests = {} -- http url -> pid, to correlate on_http_response

-- Goal-scored nameplate state — ported from rl_stats_bridge.py's
-- LastGoal + is_replay. is_replay tracks Game.bReplay straight from
-- UpdateState (see plugin.on_game_event below): it flips true the
-- instant RL enters the goal replay camera and flips back to false the
-- instant it ends — WHETHER that's the replay running its natural
-- course OR every player skipping it early. That's exactly what makes
-- checking is_replay a skip-detector for free: there's no separate
-- "was it skipped" event to listen for, we just stop trusting the
-- delay/duration timer the moment is_replay goes false.
local last_goal = nil -- { scorer_name, scorer_key, timestamp } or nil
local is_replay = false

-- Confirmed via real gameplay logging: Hebnix player ids look like
-- "Epic|61a21e5cbca9481e8b19b944f792d778|0" — platform, then id, then a
-- sub-id, ALL separated by "|" (not "/" like the Python project's
-- README example suggested). Take just the middle segment.
local function parse_platform(pid)
    local platform, id_part = pid:match("^([^|]+)|([^|]+)")
    if not platform then return "unknown", pid end
    return platform:lower(), id_part
end

-- Bots' PrimaryId collides across every bot in the match (hebnix.is_bot
-- flags it as empty/"unknown"/no "|" — confirmed against
-- hebnix_sdk::utils::platforms::is_bot; same root cause as the Python
-- rl-pfp-overlay project's rl_stats_bridge.py Player.key(), which hit
-- this because RL reports platform "Unknown" + uid "0" for EVERY bot).
-- Keying the players table by raw pid would let one bot's row silently
-- overwrite another's. Bot names are unique within a match, so fall
-- back to a name-based key for bots only — real players keep using
-- their (unique) pid as before.
local function player_key(pid, name)
    if hebnix.is_bot(pid) then
        return "bot|name:" .. name
    end
    return pid
end

-- ==========================================
-- PSN auth — ported from the Python rl-pfp-overlay project's
-- pfp_resolver.py PSN OAuth section. Unlike Steam (an app API key) or
-- Xbox (a static xbl.io key), PSN requires authenticating AS your own
-- PSN account: exchange a one-time NPSSO cookie for an access/refresh
-- token pair, then keep that token fresh automatically. Client
-- id/secret/redirect_uri/scope below are the same public constants
-- every reverse-engineered PSN tool (psnawp, PlayStation-Trophies,
-- etc.) uses to identify itself as a PlayStation client — they are not
-- secret, and are not this account's credentials.
--
-- The one piece this needed that Hebnix's plugin API didn't previously
-- have: exchanging the NPSSO for an auth code is a GET that
-- 302-redirects with the code IN the Location header, which you must
-- NOT follow (following it just loads a dead custom-scheme URL) —
-- hebnix.http_get_async always follows redirects via its shared
-- client's default policy, silently discarding that header. Requires a
-- Hebnix build with hebnix.http_get_no_redirect_async (a small patch,
-- see http_client_no_redirect/send_req_location in lua_api.rs) +
-- plugin.on_http_redirect_response below — plain http_get_async cannot
-- do this exchange at all.
-- ==========================================

local PSN_OAUTH_CLIENT_ID = "09515159-7237-4370-9b40-3806e67c0891"
local PSN_OAUTH_CLIENT_SECRET = "ucPjka5tntB2KqsP"
local PSN_OAUTH_REDIRECT_URI = "com.scee.psxandroid.scecompcall://redirect"
local PSN_OAUTH_SCOPE = "psn:mobile.v2.core psn:clientapp"
local PSN_OAUTH_AUTHORIZE_URL = "https://ca.account.sony.com/api/authz/v3/oauth/authorize"
local PSN_OAUTH_TOKEN_URL = "https://ca.account.sony.com/api/authz/v3/oauth/token"
-- Legacy PSN profile endpoint — returns avatarUrls among other fields.
local PSN_PROFILE_URL_FMT = "https://us-prof.np.community.playstation.net/userProfile/v1/users/%s/profile2"
local PSN_TOKEN_EXPIRY_MARGIN_SECONDS = 60

-- PSN tokens live in their own real file (same reasoning as
-- overrides.json) rather than settings.toml — they're rewritten
-- automatically every refresh, which would otherwise mean silently
-- churning the plugin's whole settings file.
local PSN_TOKEN_PATH = PLUGIN_DIR .. "/psn_tokens.json"

local function psn_npsso() return hebnix.get_string("psn_npsso", "") end

local function load_psn_tokens()
    local f = io.open(PSN_TOKEN_PATH, "r")
    if not f then return {} end
    local content = f:read("*a")
    f:close()
    local ok, decoded = pcall(hebnix.json_decode, content)
    if ok and type(decoded) == "table" then return decoded end
    return {}
end

local function save_psn_tokens(tokens)
    local ok, encoded = pcall(hebnix.json_encode, tokens)
    if not ok then return end
    local f = io.open(PSN_TOKEN_PATH, "w")
    if not f then return end
    f:write(encoded)
    f:close()
end

local function psn_have_valid_access_token()
    local tokens = load_psn_tokens()
    return tokens.access_token ~= nil and tokens.access_token_expires_at ~= nil
        and tokens.access_token_expires_at > (os.time() + PSN_TOKEN_EXPIRY_MARGIN_SECONDS)
end

-- Minimal percent-encoding — Hebnix exposes no url_encode, and this is
-- the only place one's needed (OAuth query/form values: the redirect
-- URI, scope string, auth code, refresh token).
local function url_encode(s)
    return (tostring(s):gsub("[^%w%-%.%_%~]", function(c)
        return string.format("%%%02X", string.byte(c))
    end))
end

local function psn_form_encode(form)
    local parts = {}
    for k, v in pairs(form) do
        table.insert(parts, url_encode(k) .. "=" .. url_encode(v))
    end
    return table.concat(parts, "&")
end

local function psn_basic_auth_header()
    return "Basic " .. hebnix.base64_encode(PSN_OAUTH_CLIENT_ID .. ":" .. PSN_OAUTH_CLIENT_SECRET)
end

-- Single-flight guard + waiter queue — same purpose as the Python
-- resolver's asyncio.Lock: N players resolving PSN avatars at once
-- must not kick off N concurrent refresh/bootstrap requests against
-- the same shared account token. Whoever asks first starts the flow;
-- everyone else queues behind it and gets served once it lands.
local psn_token_waiters = {}
local psn_token_flow_active = false

-- Forward-declared: fetch_psn_profile needs pending_requests (below)
-- and is itself needed by ensure_psn_token_then_fetch, which is
-- defined before resolve_avatar/pending_requests' full context reads
-- naturally — see the assignment further down.
local fetch_psn_profile

local function drain_psn_waiters()
    local waiters = psn_token_waiters
    psn_token_waiters = {}
    for _, pid in ipairs(waiters) do
        fetch_psn_profile(pid)
    end
end

local function psn_token_flow_failed(reason)
    psn_token_flow_active = false
    local waiters = psn_token_waiters
    psn_token_waiters = {}
    for _, pid in ipairs(waiters) do
        local p = players[pid]
        if p then p.status = "psn auth failed: " .. reason end
    end
    hebnix.log("PfpOverlay: PSN auth flow failed: " .. reason)
end

local function psn_start_refresh(refresh_token)
    local body = psn_form_encode({
        grant_type = "refresh_token",
        refresh_token = refresh_token,
        scope = PSN_OAUTH_SCOPE,
    })
    hebnix.http_post_async("psn_token_refresh", PSN_OAUTH_TOKEN_URL, body, {
        ["Authorization"] = psn_basic_auth_header(),
        ["Content-Type"] = "application/x-www-form-urlencoded",
    })
end

local function psn_start_bootstrap(npsso)
    local query = "access_type=offline&client_id=" .. url_encode(PSN_OAUTH_CLIENT_ID) ..
        "&response_type=code&scope=" .. url_encode(PSN_OAUTH_SCOPE) ..
        "&redirect_uri=" .. url_encode(PSN_OAUTH_REDIRECT_URI)
    local url = PSN_OAUTH_AUTHORIZE_URL .. "?" .. query
    -- The NPSSO is sent as a cookie, exactly like a real browser session
    -- that's already logged into playstation.com would send it.
    hebnix.http_get_no_redirect_async("psn_authorize", url, { ["Cookie"] = "npsso=" .. npsso })
end

-- Entry point: call this instead of fetch_psn_profile directly whenever
-- PSN needs resolving. Proceeds immediately if a cached token is still
-- valid; otherwise queues the pid and (if no flow is already running)
-- starts a refresh, falling back to a full NPSSO bootstrap.
local function ensure_psn_token_then_fetch(pid)
    if psn_have_valid_access_token() then
        fetch_psn_profile(pid)
        return
    end

    table.insert(psn_token_waiters, pid)
    local p = players[pid]
    if p then p.status = "psn: authenticating..." end

    if psn_token_flow_active then return end -- already in flight, just wait
    psn_token_flow_active = true

    local tokens = load_psn_tokens()
    local refresh_token = tokens.refresh_token
    local refresh_expires_at = tokens.refresh_token_expires_at
    local refresh_still_valid = refresh_token ~= nil and (
        refresh_expires_at == nil -- unknown expiry — try anyway
        or refresh_expires_at > (os.time() + PSN_TOKEN_EXPIRY_MARGIN_SECONDS)
    )
    if refresh_still_valid then
        psn_start_refresh(refresh_token)
    elseif psn_npsso() ~= "" then
        psn_start_bootstrap(psn_npsso())
    else
        psn_token_flow_failed("no psn_npsso set (Settings > PSN)")
    end
end

-- Handles BOTH psn_token_refresh and psn_token_bootstrap responses —
-- same endpoint, different grant_type, same response shape.
local function handle_psn_token_response(status, body, is_bootstrap)
    if status ~= 200 then
        hebnix.log("PfpOverlay: PSN token request failed, status=" .. tostring(status) ..
            " body=" .. tostring(body):sub(1, 300))
        if is_bootstrap then
            psn_token_flow_failed("token exchange failed (HTTP " .. tostring(status) .. ")")
        else
            -- Refresh rejected — fall back to a full NPSSO bootstrap
            -- before giving up, same as the Python resolver.
            if psn_npsso() ~= "" then
                hebnix.log("PfpOverlay: PSN refresh_token rejected, falling back to NPSSO bootstrap")
                psn_start_bootstrap(psn_npsso())
            else
                psn_token_flow_failed("refresh rejected and no psn_npsso set")
            end
        end
        return
    end

    local ok, data = pcall(hebnix.json_decode, body)
    if not ok or type(data) ~= "table" or not data.access_token then
        psn_token_flow_failed("bad token response")
        return
    end

    local now = os.time()
    local old_tokens = load_psn_tokens()
    local tokens = {
        access_token = data.access_token,
        access_token_expires_at = now + (tonumber(data.expires_in) or 3600),
        refresh_token = data.refresh_token or old_tokens.refresh_token,
        -- PSN typically omits refresh_token_expires_in on a plain
        -- refresh (only present on the initial NPSSO bootstrap) — if
        -- absent, keep whatever expiry we already had cached rather
        -- than nuking a still-valid one.
        refresh_token_expires_at = data.refresh_token_expires_in
            and (now + tonumber(data.refresh_token_expires_in))
            or old_tokens.refresh_token_expires_at,
    }
    save_psn_tokens(tokens)
    psn_token_flow_active = false
    hebnix.log("PfpOverlay: PSN access token " ..
        (is_bootstrap and "authenticated fresh via NPSSO" or "refreshed"))
    drain_psn_waiters()
end

-- Step 1 -> 2 handoff: the NPSSO exchange's redirect lands here (via
-- plugin.on_http_redirect_response), carrying the auth code we then
-- exchange for real tokens.
local function handle_psn_authorize_redirect(status, location)
    local code = location:match("[?&]code=([^&]+)")
    if not code then
        hebnix.log("PfpOverlay: PSN NPSSO exchange failed (status=" .. tostring(status) ..
            " location=" .. tostring(location) .. "). NPSSO is likely expired or invalid — " ..
            "get a fresh one by logging into playstation.com in a browser, then visiting " ..
            "https://ca.account.sony.com/api/v1/ssocookie in the same browser session, and " ..
            "updating psn_npsso in this plugin's settings.")
        psn_token_flow_failed("npsso exchange failed (expired/invalid npsso?)")
        return
    end
    code = code:gsub("%%(%x%x)", function(h) return string.char(tonumber(h, 16)) end)
    local body = psn_form_encode({
        grant_type = "authorization_code",
        code = code,
        redirect_uri = PSN_OAUTH_REDIRECT_URI,
    })
    hebnix.http_post_async("psn_token_bootstrap", PSN_OAUTH_TOKEN_URL, body, {
        ["Authorization"] = psn_basic_auth_header(),
        ["Content-Type"] = "application/x-www-form-urlencoded",
    })
end

function plugin.on_http_redirect_response(req_id, status, location)
    if req_id ~= "psn_authorize" then return end
    handle_psn_authorize_redirect(status, location)
end

-- Step 4: fetch the actual profile (avatarUrls) now that a valid access
-- token exists. PSN's legacy profile endpoint keys by online ID
-- (username), not numeric account ID — use the in-game display name,
-- same as the Xbox gamertag branch below assumes for that platform.
fetch_psn_profile = function(pid)
    local p = players[pid]
    if not p then return end
    local access_token = load_psn_tokens().access_token
    if not access_token then
        p.status = "psn: no access token"
        return
    end
    local username = url_encode(p.name)
    local url = string.format(PSN_PROFILE_URL_FMT, username) .. "?fields=avatarUrls"
    pending_requests[url] = { pid = pid, kind = "psn_profile" }
    hebnix.http_get_async(url, url, { ["Authorization"] = "Bearer " .. access_token })
    p.status = "fetching (psn)"
end

local function resolve_avatar(pid)
    local p = players[pid]
    if not p then return end

    local overrides = load_overrides()
    local override_key = p.platform .. "|" .. p.platform_id
    hebnix.log("PfpOverlay: resolve_avatar override_key=" .. override_key ..
        " overrides_json=" .. hebnix.get_string("avatar_overrides_json", "{}") ..
        " match=" .. tostring(overrides[override_key]))
    if overrides[override_key] then
        p.avatar_path = overrides[override_key]
        p.avatar_url = nil
        p.status = "override"
        return
    end
    p.avatar_path = nil

    if p.platform == "steam" then
        local key = steam_api_key()
        if key == "" then
            p.status = "no steam_api_key set"
            return
        end
        local url = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key="
            .. key .. "&steamids=" .. p.platform_id
        pending_requests[url] = { pid = pid, kind = "metadata" }
        hebnix.http_get_async(url, url, {})
        p.status = "fetching (steam)"
    elseif p.platform:find("xbox") then
        local key = xbox_api_key()
        if key == "" then
            p.status = "no xbox_api_key set"
            return
        end
        -- Xbox needs the gamertag, not a numeric XUID — use the display
        -- name from the stats event, same as the Python resolver did.
        -- NOTE: the old /v2/player/gamertag/{gamertag} route 404s. The
        -- api.xbl.io host serves paths WITHOUT an /api prefix (that
        -- prefix is only for the alternate xbl.io/api/v2/... host,
        -- confirmed by the server's own 404 error message) — the right
        -- route here is /v2/search/{gamertag}.
        local gamertag = p.name:gsub(" ", "%%20")
        local url = "https://api.xbl.io/v2/search/" .. gamertag
        pending_requests[url] = { pid = pid, kind = "metadata" }
        hebnix.http_get_async(url, url, { ["X-Authorization"] = key, ["Accept"] = "application/json" })
        p.status = "fetching (xbox)"
    elseif p.platform:find("ps") then
        -- "ps4"/"ps5"/"psn"/"psvita" etc — see the PSN auth section
        -- above for the full flow. ensure_psn_token_then_fetch manages
        -- its own status updates (authenticating / fetching / failed).
        ensure_psn_token_then_fetch(pid)
    elseif p.platform == "epic" or p.platform == "switch" then
        p.status = "unsupported platform (RL default pic)"
    else
        p.status = "unsupported platform: " .. p.platform
    end
end

-- Only [%w_.-] survive filesystem-safely on Windows — pid platform_ids
-- can contain arbitrary characters depending on platform.
local function sanitize_filename(s)
    return (s:gsub("[^%w_.-]", "_"))
end

-- Guess an image extension from the URL first (cheap, usually right),
-- falling back to the response body's magic bytes if the URL has none.
local function detect_image_ext(url, body)
    local ext = url:match("%.([%a]+)%??")
    if ext then
        ext = ext:lower()
        if ext == "jpg" or ext == "jpeg" then return "jpg" end
        if ext == "png" then return "png" end
    end
    if body and #body >= 4 then
        local b1, b2, b3 = string.byte(body, 1, 3)
        if b1 == 255 and b2 == 216 and b3 == 255 then return "jpg" end
        if b1 == 137 and b2 == 80 and b3 == 78 then return "png" end
    end
    return "png"
end

-- download_requests keyed separately from pending_requests (metadata/JSON)
-- since downloads go through the byte-safe http_download_async callback.
local download_requests = {} -- req_id -> pid

local function start_avatar_download(pid, avatar_url)
    local p = players[pid]
    if not p then return end
    download_requests[avatar_url] = pid
    -- Patched Hebnix build: hebnix.http_download_async uses res.bytes()
    -- instead of res.text(), so binary image data survives intact —
    -- http_get_async's text-based body corrupts it. See
    -- plugin.on_http_download_response below.
    hebnix.http_download_async(avatar_url, avatar_url, {})
    p.status = "downloading avatar image"
end

function plugin.on_http_response(url, status, body)
    if url == "psn_token_refresh" or url == "psn_token_bootstrap" then
        handle_psn_token_response(status, body, url == "psn_token_bootstrap")
        return
    end
    local req = pending_requests[url]
    if not req then return end
    pending_requests[url] = nil
    local p = players[req.pid]
    if not p then return end

    if status ~= 200 then
        p.status = "http error " .. tostring(status) .. " (" .. req.kind .. ")"
        hebnix.log("PfpOverlay: " .. req.kind .. " request for " .. p.name .. " failed, status=" ..
            tostring(status) .. " body=" .. tostring(body):sub(1, 500))
        return
    end

    local ok, data = pcall(hebnix.json_decode, body)
    if not ok or type(data) ~= "table" then
        p.status = "bad json response"
        return
    end

    local avatar_url = nil
    if p.platform == "steam" then
        local arr = data.response and data.response.players
        avatar_url = arr and arr[1] and arr[1].avatarfull
    elseif p.platform:find("xbox") then
        -- Confirmed via a real /v2/search/{gamertag} response:
        -- {"content":{"people":[{"displayPicRaw":"https://...","gamertag":"...",...}]}}
        local people = data.content and data.content.people
        avatar_url = people and people[1] and people[1].displayPicRaw
    elseif p.platform:find("ps") then
        -- {"profile": {"avatarUrls": [{"size": "m"/"l"/"xl", "avatarUrl": url}, ...]}}
        -- — prefer the largest available, matching the Python resolver.
        local urls = data.profile and data.profile.avatarUrls
        if urls then
            local by_size = {}
            for _, entry in ipairs(urls) do
                if entry.size and entry.avatarUrl then by_size[entry.size] = entry.avatarUrl end
            end
            avatar_url = by_size.xl or by_size.l or by_size.m
        end
    end

    if avatar_url then
        p.avatar_url = avatar_url
        start_avatar_download(req.pid, avatar_url)
    else
        p.status = "no avatar in response"
    end
end

function plugin.on_http_download_response(url, status, body)
    local pid = download_requests[url]
    if not pid then return end
    download_requests[url] = nil
    local p = players[pid]
    if not p then return end

    if status ~= 200 then
        p.status = "http error " .. tostring(status) .. " (download)"
        return
    end

    local ext = detect_image_ext(url, body)
    local filename = sanitize_filename(p.platform .. "_" .. p.platform_id) .. "." .. ext
    -- rel_path is what draw.image needs. abs_path (built from
    -- hebnix.plugin_dir()) is what io.open needs — see PLUGIN_DIR above.
    local rel_path = "assets/cache/" .. filename
    local abs_path = PLUGIN_DIR .. "/assets/cache/" .. filename
    local f, open_err, open_errno = io.open(abs_path, "wb")
    if f then
        f:write(body)
        f:close()
        p.avatar_path = rel_path
        p.status = "resolved (downloaded, " .. #body .. " bytes)"
        hebnix.log("PfpOverlay: downloaded avatar for " .. p.name .. " -> " .. abs_path ..
            " (" .. #body .. " bytes)")
    else
        p.status = "failed to write avatar file"
        hebnix.log("PfpOverlay: FAILED to open " .. abs_path .. " for writing: " ..
            tostring(open_err) .. " (errno=" .. tostring(open_errno) .. ")")
    end
end

local function clear_players()
    players = {}
    player_order = {}
    seen = {}
    pending_requests = {}
    download_requests = {}
    last_goal = nil
    is_replay = false
end

local function track_player(pid, name)
    if pid == "" and name == "" then return end
    local key = player_key(pid, name)
    if seen[key] then return end
    seen[key] = true
    local is_bot = hebnix.is_bot(pid)
    local platform, platform_id = parse_platform(pid)
    hebnix.log(string.format(
        "PfpOverlay: tracked %s -> raw_pid=%q key=%q is_bot=%s parsed platform=%q platform_id=%q",
        name, pid, key, tostring(is_bot), platform, platform_id))
    players[key] = {
        name = name, platform = platform, platform_id = platform_id, is_bot = is_bot,
        raw_pid = pid, -- for hebnix.platform_tag()/etc — key is a synthetic name-based id for bots, not a real PrimaryId
        avatar_path = nil, avatar_url = nil, status = is_bot and "bot (no avatar)" or "new",
        team_num = 0, score = 0,
    }
    table.insert(player_order, key)
    -- Bots have no real platform account to look up — RL just shows its
    -- own default bot icon for them. Resolving would only waste an HTTP
    -- call classifying "unknown" as an unsupported platform.
    if not is_bot then resolve_avatar(key) end
end

-- Refreshed every UpdateState tick (score/team can change mid-match,
-- unlike identity fields which are only ever set once in track_player).
local function update_player_state(pid, name, team_num, score)
    local key = player_key(pid, name)
    local p = players[key]
    if not p then return end
    p.team_num = team_num or p.team_num
    p.score = score or p.score
end

-- ==========================================
-- Callbacks
-- ==========================================

function plugin.on_load()
    hebnix.log("PfpOverlay loaded")
end

function plugin.on_game_event(event_type, event)
    if event_type == "UpdateState" then
        for _, p in ipairs(event.data.Players or {}) do
            local pid = p.PrimaryId or ""
            local name = p.Name or "Unknown"
            -- Bots ARE tracked now (they occupy real scoreboard rows in
            -- game) — only truly empty entries (no pid, no name) are
            -- skipped. See player_key()/track_player() for how bots
            -- avoid colliding under one shared key.
            if pid ~= "" or name ~= "" then
                track_player(pid, name)
                update_player_state(pid, name, p.TeamNum, p.Score)
            end
        end
        -- Same signal rl_stats_bridge.py uses: Game.bReplay flips false
        -- the instant the goal replay ends, by timeout OR by everyone
        -- skipping it — no separate "skipped" event exists to listen for.
        local game = event.data.Game
        if game and game.bReplay ~= nil then
            is_replay = game.bReplay
        end
    elseif event_type == "GoalScored" then
        -- Scorer only carries a display Name (no PrimaryId) — match it
        -- to a tracked player by name, same limitation the Python
        -- resolver had (GoalScored's own comment: "Shortcut numbers
        -- aren't in our roster keys, so name match is the best we've
        -- got from this event alone").
        local scorer_name = event.data.Scorer and event.data.Scorer.Name or ""
        local scorer_key = nil
        for _, key in ipairs(player_order) do
            if players[key].name == scorer_name then
                scorer_key = key
                break
            end
        end
        -- os.time() (whole seconds) instead of a sub-second clock —
        -- Hebnix's Lua API exposes no monotonic/wall-clock timer, and
        -- os.clock() measures CPU time (not real elapsed time), which
        -- would drift against GOAL_NAMEPLATE_DELAY/DURATION_SECONDS
        -- during idle ticks. ~1s granularity is negligible against an
        -- 11s display window.
        last_goal = { scorer_name = scorer_name, scorer_key = scorer_key, timestamp = os.time() }
        hebnix.log("PfpOverlay: GoalScored by " .. scorer_name .. " (matched key: " .. tostring(scorer_key) .. ")")
    elseif event_type == "GameLeft" or event_type == "MatchEnded" then
        clear_players()
    end
end

-- ==========================================
-- In-game overlay (on_overlay/draw) — the real rendering surface,
-- confirmed with the owner: draw.image places an image at an exact
-- pixel coordinate, same mechanism RankViewer uses for its rank icons.
-- ==========================================

local AVATAR_SIZE = 64
local AVATAR_GAP = 8
local AVATAR_START_X = 40
local AVATAR_START_Y = 40
local logged_overlay_call = false
local logged_overlay_call2 = false

-- Toggle-bind so the debug stack can be brought back manually for
-- troubleshooting (e.g. resolver status per player) without needing to
-- restart the game. Off by default now that real scoreboard-position
-- rendering (draw_scoreboard_avatars) exists — this is just a debug
-- aid, not the main experience. Same capture pattern InGameRanks uses.
local overlay_visible = false
local toggle_was_pressed = false
local capturing_bind = false

-- Scoreboard-hold bind — matches RL's own scoreboard reveal (hold TAB /
-- View button): while held, avatars render at the REAL calibrated
-- scoreboard positions. Separate from overlay_toggle_bind above, which
-- is a manual show/hide override for the top-left debug stack, not
-- tied to the actual scoreboard state. Forgoing DirectInput fallback
-- for now — Hebnix's own bind system (hebnix.capture_bind_async /
-- is_bind_pressed) is expected to cover XInput + keyboard; any gap for
-- non-XInput controllers is Hebnix's own concern, not this plugin's
-- (the Python version's win_controller.py DInput/HID fallback chain
-- has no equivalent here on purpose).
local scoreboard_held = false
local scoreboard_capturing_bind = false

function plugin.on_tick()
    local bind = hebnix.get_string("overlay_toggle_bind", "")
    if bind ~= "" then
        local pressed = hebnix.is_bind_pressed(bind)
        if pressed and not toggle_was_pressed then
            overlay_visible = not overlay_visible
            hebnix.log("PfpOverlay: toggle bind pressed, overlay_visible=" .. tostring(overlay_visible))
        end
        toggle_was_pressed = pressed
    end

    if capturing_bind then
        local status, bind_result = hebnix.capture_bind_result()
        if status == "done" then
            hebnix.set("overlay_toggle_bind", bind_result)
            hebnix.log("PfpOverlay: overlay_toggle_bind set to " .. tostring(bind_result))
            capturing_bind = false
        elseif status == "timeout" then
            capturing_bind = false
        end
    end

    local sb_bind = hebnix.get_string("scoreboard_button", "")
    scoreboard_held = sb_bind ~= "" and hebnix.is_bind_pressed(sb_bind)

    if scoreboard_capturing_bind then
        local status, bind_result = hebnix.capture_bind_result()
        if status == "done" then
            hebnix.set("scoreboard_button", bind_result)
            hebnix.log("PfpOverlay: scoreboard_button set to " .. tostring(bind_result))
            scoreboard_capturing_bind = false
        elseif status == "timeout" then
            scoreboard_capturing_bind = false
        end
    end
end

-- Sorted, per-team player lists for scoreboard row assignment — RL's
-- real scoreboard sorts by score descending within each team. TeamNum
-- 0 = blue, 1 = orange (standard RL convention).
local function scoreboard_teams()
    local blue, orange = {}, {}
    for _, pid in ipairs(player_order) do
        local p = players[pid]
        if p.team_num == 1 then
            table.insert(orange, pid)
        else
            table.insert(blue, pid)
        end
    end
    local function by_score_desc(a, b) return players[a].score > players[b].score end
    table.sort(blue, by_score_desc)
    table.sort(orange, by_score_desc)
    return blue, orange
end

-- Debug stack — top-left, only up to (and including) how the avatar
-- image itself is loading, independent of scoreboard-position
-- calibration. Kept for troubleshooting; off by default now that real
-- positioning exists.
local function draw_debug_stack(draw)
    if not overlay_visible then return end
    if #player_order == 0 then return end
    local y = AVATAR_START_Y
    local goal_age = last_goal and (tostring(os.time() - last_goal.timestamp) .. "s ago (" ..
        last_goal.scorer_name .. ")") or "none"
    draw.text(AVATAR_START_X, AVATAR_START_Y - 36,
        "PfpOverlay debug stack  —  is_replay=" .. tostring(is_replay) .. "  last_goal=" .. goal_age,
        { color = "#00ff00ff", size = 16 })
    for _, pid in ipairs(player_order) do
        local p = players[pid]
        local tag = hebnix.platform_tag(p.raw_pid or pid)
        if p.avatar_path then
            local ok, err = pcall(function()
                draw.image(p.avatar_path, AVATAR_START_X, y, AVATAR_SIZE, AVATAR_SIZE)
            end)
            if not logged_overlay_call2 then
                hebnix.log("PfpOverlay: draw.image(" .. p.avatar_path .. ") ok=" .. tostring(ok) ..
                    " err=" .. tostring(err))
                logged_overlay_call2 = true
            end
        end
        local team_label = (p.team_num == 1) and "orange" or "blue"
        draw.text(AVATAR_START_X + AVATAR_SIZE + 8, y + AVATAR_SIZE / 2 - 8,
            tag .. " " .. p.name .. "  [" .. team_label .. " " .. tostring(p.score) .. "]  —  " .. p.status,
            { color = "#ffffffff", size = 14 })
        y = y + AVATAR_SIZE + AVATAR_GAP
    end
end

-- Real scoreboard-position rendering — visible only while the
-- scoreboard bind is held, matching how RL's own scoreboard works.
local function draw_scoreboard_avatars(draw, w, h)
    if not scoreboard_held then return end
    local blue, orange = scoreboard_teams()
    local team_size = math.max(#blue, #orange)
    if team_size == 0 then return end

    local slots = get_scoreboard_slots(team_size, w, h)
    for _, slot in ipairs(slots) do
        local list = (slot.team == "blue") and blue or orange
        local pid = list[slot.row + 1] -- Lua 1-indexed, row is 0-based
        if pid then
            local p = players[pid]
            if p.avatar_path then
                pcall(function()
                    draw.image(p.avatar_path, slot.x, slot.y, slot.w, slot.h)
                end)
            end
        end
    end
end

-- Goal-scored nameplate — shown at the "SCORED BY" nameplate position
-- during the goal replay. Ported from win_overlay.py's _tick(): only
-- rendered once GOAL_NAMEPLATE_DELAY_SECONDS has elapsed since the
-- goal (matches when RL's own nameplate animates in) AND is_replay is
-- still true. That second condition is the skip detector — if every
-- player skips the replay early, Game.bReplay flips false immediately
-- (see plugin.on_game_event's UpdateState handling) and this stops
-- drawing right away instead of sitting on screen for the rest of the
-- configured duration over normal gameplay.
local function draw_goal_nameplate(draw, w, h)
    if not last_goal then return end
    local elapsed = os.time() - last_goal.timestamp
    local in_delay_window = elapsed >= GOAL_NAMEPLATE_DELAY_SECONDS
        and elapsed < (GOAL_NAMEPLATE_DELAY_SECONDS + GOAL_NAMEPLATE_DURATION_SECONDS)
    if not (in_delay_window and is_replay) then return end

    local p = last_goal.scorer_key and players[last_goal.scorer_key]
    if not p or not p.avatar_path then return end

    local x, y, w2, h2 = get_goal_nameplate_slot(w, h)
    pcall(function()
        draw.image(p.avatar_path, x, y, w2, h2)
    end)
end

function plugin.on_overlay(draw, w, h)
    if not logged_overlay_call then
        logged_overlay_call = true
        hebnix.log("PfpOverlay: on_overlay FIRED, w=" .. tostring(w) .. " h=" .. tostring(h) ..
            " player_count=" .. tostring(#player_order))
    end
    draw_debug_stack(draw)
    draw_scoreboard_avatars(draw, w, h)
    draw_goal_nameplate(draw, w, h)
end

-- ==========================================
-- Settings
-- ==========================================

function plugin.on_settings(ui)
    ui.heading("PFP Overlay — Steam / Xbox / PSN")
    ui.label("Avatars render at RL's real scoreboard positions while the")
    ui.label("scoreboard button below is held. Set your Interface Scale")
    ui.label("to match RL exactly, or positions will be off.")

    ui.space(8)
    ui.heading("API Keys")
    ui.text_input("steam_api_key", "Steam Web API key (steamcommunity.com/dev)", "")
    ui.text_input("xbox_api_key", "Xbox API key (xbl.io)", "")

    ui.space(8)
    ui.heading("PSN")
    ui.label("PSN authenticates as your own PSN account rather than using an")
    ui.label("app API key. One-time setup: log into playstation.com in a")
    ui.label("browser, then in the SAME browser session visit")
    ui.label("https://ca.account.sony.com/api/v1/ssocookie — paste the")
    ui.label("\"npsso\" value it returns below. After that, this plugin")
    ui.label("refreshes its own PSN session automatically; you only need to")
    ui.label("repeat this if the NPSSO itself expires (rare).")
    ui.text_input("psn_npsso", "PSN NPSSO")
    if psn_have_valid_access_token() then
        ui.colored_label("#2ecc71", "PSN: authenticated")
    elseif load_psn_tokens().refresh_token then
        ui.colored_label("#d35400", "PSN: token expired, will auto-refresh on next lookup")
    else
        ui.colored_label("#aaaaaa", "PSN: not authenticated yet")
    end

    ui.space(4)
    ui.label("PSN lookups use the player's online ID (display name).")
    ui.text_input("test_psn_username", "PSN username to test")
    if ui.button("Test download my PSN avatar") then
        local username = hebnix.get_string("test_psn_username", "")
        if username ~= "" then
            local pid = "TestPSN|" .. username
            if not players[pid] then
                players[pid] = {
                    name = username, platform = "psn", platform_id = username,
                    avatar_path = nil, avatar_url = nil, status = "new",
                    team_num = 0, score = 0,
                }
                table.insert(player_order, pid)
                seen[pid] = true
            end
            resolve_avatar(pid)
        end
    end

    ui.space(8)
    ui.heading("Interface Scale")
    ui.label("Must match RL's Options > Video > Interface Scale exactly")
    ui.label("(e.g. 0.75) — needed for avatars to land on the right pixels.")
    ui.text_input("rl_ui_scale", "RL Interface Scale", tostring(REFERENCE_UI_SCALE))

    ui.space(8)
    ui.heading("Scoreboard Button")
    ui.label("Bind this to whatever shows RL's own scoreboard (hold TAB on")
    ui.label("keyboard, View/Select on controller) — avatars only render at")
    ui.label("scoreboard positions while this is held.")
    local sb_bind = hebnix.get_string("scoreboard_button", "")
    ui.horizontal(function()
        ui.label("Scoreboard bind: " .. (sb_bind ~= "" and sb_bind or "(none set)"))
        if scoreboard_capturing_bind then
            ui.colored_label("#d35400", "Press any key/button...")
        else
            if ui.button("Set") then
                if hebnix.capture_bind_async(10) then
                    scoreboard_capturing_bind = true
                end
            end
            if ui.button("Clear") then
                hebnix.set("scoreboard_button", "")
                hebnix.log("PfpOverlay: scoreboard_button cleared")
            end
        end
    end)

    ui.space(8)
    ui.heading("Manual Test (no live match needed)")
    ui.label("Find your SteamID64 at steamid.io if you don't know it.")
    ui.text_input("test_steam_id", "SteamID64 to test", "")
    if ui.button("Test download my Steam avatar") then
        local test_id = hebnix.get_string("test_steam_id", "")
        if test_id ~= "" then
            local pid = "TestSteam|" .. test_id
            if not players[pid] then
                players[pid] = {
                    name = "TestSteamUser", platform = "steam", platform_id = test_id,
                    avatar_path = nil, avatar_url = nil, status = "new",
                    team_num = 0, score = 0,
                }
                table.insert(player_order, pid)
                seen[pid] = true
            end
            resolve_avatar(pid)
        end
    end

    ui.space(4)
    ui.label("Xbox lookup uses your gamertag, not an ID.")
    ui.text_input("test_xbox_gamertag", "Xbox gamertag to test", "")
    if ui.button("Test download my Xbox avatar") then
        local gamertag = hebnix.get_string("test_xbox_gamertag", "")
        if gamertag ~= "" then
            local pid = "TestXbox|" .. gamertag
            if not players[pid] then
                players[pid] = {
                    name = gamertag, platform = "xboxone", platform_id = gamertag,
                    avatar_path = nil, avatar_url = nil, status = "new",
                    team_num = 1, score = 0,
                }
                table.insert(player_order, pid)
                seen[pid] = true
            end
            resolve_avatar(pid)
        end
    end

    ui.space(8)
    ui.heading("Avatar Overrides")
    ui.label("Force a specific local image for one player, by platform + ID —")
    ui.label("useful for platforms Hebnix can't resolve automatically (PSN,")
    ui.label("Epic, Switch), or to override someone's real avatar entirely.")
    ui.label("The image must already exist under this plugin's own assets/")
    ui.label("folder (draw.image can't load paths outside it) — drop your")
    ui.label("image there first, then reference it below as assets/name.png.")

    ui.space(4)
    local override_platform = ui.combo_box("override_add_platform", "Platform",
        { "steam", "xboxone", "epic", "psn", "switch" })
    local override_id = ui.text_input("override_add_id", "Player ID (SteamID64 / gamertag / etc)")
    local override_path = ui.text_input("override_add_path", "Image path, e.g. assets/me.png")

    if ui.button("Add / Update override") then
        local id = override_id:match("^%s*(.-)%s*$")
        local path = override_path:match("^%s*(.-)%s*$")
        if id == "" or path == "" then
            hebnix.log("PfpOverlay: override add ignored — both ID and image path are required")
        else
            local key = override_platform .. "|" .. id
            local overrides = load_overrides()
            overrides[key] = path
            write_overrides_file(overrides)
            hebnix.log("PfpOverlay: override set " .. key .. " -> " .. path)
        end
    end

    ui.space(6)
    local current_overrides = load_overrides()
    local override_keys = {}
    for k in pairs(current_overrides) do table.insert(override_keys, k) end
    table.sort(override_keys)
    if #override_keys == 0 then
        ui.label("No overrides yet.")
    else
        for _, k in ipairs(override_keys) do
            ui.horizontal(function()
                ui.label(k .. "  ->  " .. current_overrides[k])
                -- egui auto-disambiguates same-label widgets by call
                -- order within a frame (no ImGui-style "##id" needed —
                -- that syntax isn't special here and would show up as
                -- literal text in the button).
                if ui.button("Remove") then
                    current_overrides[k] = nil
                    write_overrides_file(current_overrides)
                end
            end)
        end
    end

    ui.space(6)
    if ui.button("Open overrides.json") then
        if not read_overrides_file() then write_overrides_file(load_overrides()) end
        hebnix.open_url(OVERRIDES_PATH)
    end
    ui.label(OVERRIDES_PATH)

    ui.space(8)
    ui.heading("Overlay Toggle Bind")
    local bind = hebnix.get_string("overlay_toggle_bind", "")
    ui.horizontal(function()
        ui.label("Toggle key/button: " .. (bind ~= "" and bind or "(none — always visible)"))
        if capturing_bind then
            ui.colored_label("#d35400", "Press any key/button...")
        else
            if ui.button("Set") then
                if hebnix.capture_bind_async(10) then
                    capturing_bind = true
                end
            end
            if ui.button("Clear") then
                hebnix.set("overlay_toggle_bind", "")
                hebnix.log("PfpOverlay: overlay_toggle_bind cleared")
            end
        end
    end)
    ui.label("Currently: overlay is " .. (overlay_visible and "VISIBLE" or "HIDDEN"))

    ui.space(8)
    if ui.button("Re-resolve all tracked players") then
        for _, pid in ipairs(player_order) do resolve_avatar(pid) end
    end
    if ui.button("Clear tracked players") then clear_players() end

    ui.space(10)
    ui.heading("Tracked Players")
    if #player_order == 0 then
        ui.label("No players tracked yet — join a match.")
    end
    for _, pid in ipairs(player_order) do
        local p = players[pid]
        local tag = hebnix.platform_tag(p.raw_pid or pid)
        ui.label(tag .. " " .. p.name .. "  —  " .. p.status)
    end
end

function plugin.on_unload()
    hebnix.log("PfpOverlay unloaded")
end

return plugin
