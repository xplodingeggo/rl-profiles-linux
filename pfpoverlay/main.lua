-- PfpOverlay: shows Steam/Xbox (PSN pending) profile info for everyone
-- in your match. Lua port of the rl-pfp-overlay Python/Windows
-- project's resolver, scaffolded against Hebnix's plugin API.
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
-- PSN is skipped entirely pending an answer on reading redirect
-- Location headers for its OAuth flow.

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

local function load_overrides()
    local raw = hebnix.get_string("avatar_overrides_json", "{}")
    local ok, decoded = pcall(hebnix.json_decode, raw)
    if ok and type(decoded) == "table" then return decoded end
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
    draw.text(AVATAR_START_X, AVATAR_START_Y - 20, "PfpOverlay debug stack",
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

function plugin.on_overlay(draw, w, h)
    if not logged_overlay_call then
        logged_overlay_call = true
        hebnix.log("PfpOverlay: on_overlay FIRED, w=" .. tostring(w) .. " h=" .. tostring(h) ..
            " player_count=" .. tostring(#player_order))
    end
    draw_debug_stack(draw)
    draw_scoreboard_avatars(draw, w, h)
end

-- ==========================================
-- Settings
-- ==========================================

function plugin.on_settings(ui)
    ui.heading("PFP Overlay — Steam / Xbox")
    ui.label("Avatars render at RL's real scoreboard positions while the")
    ui.label("scoreboard button below is held. Set your Interface Scale")
    ui.label("to match RL exactly, or positions will be off.")
    ui.label("PSN is not implemented yet — pending an answer on Hebnix's")
    ui.label("networking API for the PSN OAuth redirect flow.")

    ui.space(8)
    ui.heading("API Keys")
    ui.text_input("steam_api_key", "Steam Web API key (steamcommunity.com/dev)", "")
    ui.text_input("xbox_api_key", "Xbox API key (xbl.io)", "")

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
    ui.heading("Avatar Overrides (local files — use these to test rendering)")
    ui.label("JSON object: \"platform|id\" -> absolute image path.")
    ui.label("Example: {\"steam|76561198210031575\": \"C:/Users/you/Pictures/me.png\"}")
    ui.text_input("avatar_overrides_json", "Overrides JSON", "{}")

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
