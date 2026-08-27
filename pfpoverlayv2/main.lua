-- PfpOverlayV2: profile pictures on the scoreboard + goal replay nameplate.
-- Same layout/rendering as v1, but avatars are resolved through Hebnix's
-- built-in tracker.gg lookup by default instead of our own Steam/Xbox/PSN
-- calls. No keys or PSN login required unless manual mode is picked per
-- platform (see settings). v1 stays untouched as a fallback plugin.
--
-- Note: draw.image (scoreboard/nameplate rendering) only reads local
-- files, not urls - only ui.image (settings widgets) can load a url
-- directly. So even a tracker.gg avatar still gets downloaded to
-- assets/cache/ ourselves before it can be drawn on screen.

local plugin = {}

local PLUGIN_DIR = hebnix.plugin_dir()

-- ==========================================
-- Config / overrides
-- ==========================================

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
        hebnix.log("PfpOverlayV2: FAILED to open " .. OVERRIDES_PATH .. " for writing")
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
    return {}
end

-- "tracker" (default) or "manual". epic/switch have no manual method,
-- so they always go through tracker.gg.
local function platform_priority(platform)
    if platform == "steam" then return hebnix.get_string("priority_steam", "tracker") end
    if platform:find("xbox") then return hebnix.get_string("priority_xboxone", "tracker") end
    if platform:find("ps") then return hebnix.get_string("priority_psn", "tracker") end
    return "tracker"
end

local function steam_api_key() return hebnix.get_string("steam_api_key", "") end
local function xbox_api_key() return hebnix.get_string("xbox_api_key", "") end

-- ==========================================
-- Layout (identical to PfpOverlay v1 - ported from the Python
-- rl-pfp-overlay project's layout.py; see v1's main.lua for the full
-- derivation history behind these constants.)
-- ==========================================

local REFERENCE_RESOLUTION = { 2560, 1440 }
local REFERENCE_UI_SCALE = 0.75
local ROW_HEIGHT = 56
local BOX_SIZE = 48
local SLOT_X = 714

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
    [1] = { blue = 655, orange = 794 },
}

local SCOREBOARD_UI_QUAD = {
    x = { 8.0, -766.0, 1284.0 },
    y = { 308.0, -559.0, 799.0 },
    size = { 0.0, 64.0, 0.0 },
}
local ROW_HEIGHT_QUAD = { 88.0, -86.0, 72.0 }

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
local EXTRA_X_NUDGE = {
    blue = { [1] = 1, [2] = 1 },
}

local function quad(coefs, s)
    local result = 0.0
    for _, c in ipairs(coefs) do
        result = result * s + c
    end
    return result
end

local function round_up(value)
    return math.ceil(value)
end

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

local GOAL_NAMEPLATE_DELAY_SECONDS = 3.5
local GOAL_NAMEPLATE_DURATION_SECONDS = 11
local GOAL_NAMEPLATE_REFERENCE_SLOT = { 1047, 1223, 75, 75 }
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

local players = {}
local player_order = {}
local seen = {}
local pending_tracker = {} -- key -> true, players awaiting a hebnix.stats_result

local last_goal = nil -- { scorer_name, scorer_key, timestamp } or nil
local is_replay = false

local function parse_platform(pid)
    local platform, id_part = pid:match("^([^|]+)|([^|]+)")
    if not platform then return "unknown", pid end
    return platform:lower(), id_part
end

local function player_key(pid, name)
    if hebnix.is_bot(pid) then
        return "bot|name:" .. name
    end
    return pid
end

-- ==========================================
-- Avatar resolution via Hebnix's built-in tracker.gg integration
-- ==========================================

-- Only [%w_.-] survive filesystem-safely on Windows.
local function sanitize_filename(s)
    return (s:gsub("[^%w_.-]", "_"))
end

-- draw.image resolves paths against the plugin folder, so avatar_path is
-- stored as "assets/cache/x.png". ui.image resolves against the assets/
-- folder itself, so it needs that prefix stripped.
local function ui_asset_path(path)
    return (path:gsub("^assets/", ""))
end

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

-- avatar url -> {key, source}, for on_http_download_response. source
-- ("tracker" or "manual") keeps the two saved as separate files, so
-- switching fetch mode always shows the right one instead of one
-- silently overwriting the other's cached file on disk.
local download_requests = {}

local function start_avatar_download(key, avatar_url, source)
    local p = players[key]
    if not p then return end
    download_requests[avatar_url] = { key = key, source = source }
    hebnix.http_download_async(avatar_url, avatar_url, {})
    p.status = "downloading avatar image"
end

-- metadata request url -> {pid=key, kind}, for on_http_response
local pending_requests = {}

-- ==========================================
-- Manual per-platform fetching, from v1 - only used when a platform's
-- priority is set to "manual" and its key/npsso is filled in.
-- ==========================================

local function manual_fetch_steam(pid)
    local p = players[pid]
    if not p then return end
    local key = steam_api_key()
    local url = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key="
        .. key .. "&steamids=" .. p.platform_id
    pending_requests[url] = { pid = pid, kind = "metadata" }
    hebnix.http_get_async(url, url, {})
    p.status = "fetching (steam, manual)"
end

local function manual_fetch_xbox(pid)
    local p = players[pid]
    if not p then return end
    local key = xbox_api_key()
    -- needs the gamertag, not a numeric XUID
    local gamertag = p.name:gsub(" ", "%%20")
    local url = "https://api.xbl.io/v2/search/" .. gamertag
    pending_requests[url] = { pid = pid, kind = "metadata" }
    hebnix.http_get_async(url, url, { ["X-Authorization"] = key, ["Accept"] = "application/json" })
    p.status = "fetching (xbox, manual)"
end

-- PSN auth - trades an NPSSO cookie for an access/refresh token pair,
-- then keeps it refreshed. client id/secret below are public constants
-- used by every reverse-engineered PSN tool, not real secrets.
--
-- Needs hebnix.http_get_no_redirect_async: the NPSSO exchange is a GET
-- that 302s with the auth code in the Location header, which must not
-- be auto-followed.
local PSN_OAUTH_CLIENT_ID = "09515159-7237-4370-9b40-3806e67c0891"
local PSN_OAUTH_CLIENT_SECRET = "ucPjka5tntB2KqsP"
local PSN_OAUTH_REDIRECT_URI = "com.scee.psxandroid.scecompcall://redirect"
local PSN_OAUTH_SCOPE = "psn:mobile.v2.core psn:clientapp"
local PSN_OAUTH_AUTHORIZE_URL = "https://ca.account.sony.com/api/authz/v3/oauth/authorize"
local PSN_OAUTH_TOKEN_URL = "https://ca.account.sony.com/api/authz/v3/oauth/token"
-- Legacy PSN profile endpoint - returns avatarUrls among other fields.
local PSN_PROFILE_URL_FMT = "https://us-prof.np.community.playstation.net/userProfile/v1/users/%s/profile2"
local PSN_TOKEN_EXPIRY_MARGIN_SECONDS = 60

-- own file instead of settings.toml since it gets rewritten every refresh
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

-- basic percent-encoding for OAuth query/form values
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

-- only one token refresh/bootstrap runs at a time, everyone else waits
local psn_token_waiters = {}
local psn_token_flow_active = false

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
    hebnix.log("PfpOverlayV2: PSN auth flow failed: " .. reason)
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
    hebnix.http_get_no_redirect_async("psn_authorize", url, { ["Cookie"] = "npsso=" .. npsso })
end

-- call this instead of fetch_psn_profile directly; uses a cached token
-- if valid, else refreshes or does a full NPSSO bootstrap
local function ensure_psn_token_then_fetch(pid)
    if psn_have_valid_access_token() then
        fetch_psn_profile(pid)
        return
    end

    table.insert(psn_token_waiters, pid)
    local p = players[pid]
    if p then p.status = "psn: authenticating..." end

    if psn_token_flow_active then return end
    psn_token_flow_active = true

    local tokens = load_psn_tokens()
    local refresh_token = tokens.refresh_token
    local refresh_expires_at = tokens.refresh_token_expires_at
    local refresh_still_valid = refresh_token ~= nil and (
        refresh_expires_at == nil
        or refresh_expires_at > (os.time() + PSN_TOKEN_EXPIRY_MARGIN_SECONDS)
    )
    if refresh_still_valid then
        psn_start_refresh(refresh_token)
    elseif psn_npsso() ~= "" then
        psn_start_bootstrap(psn_npsso())
    else
        psn_token_flow_failed("no psn_npsso set (Settings > API Keys)")
    end
end

-- handles both refresh and bootstrap responses - same shape, different grant_type
local function handle_psn_token_response(status, body, is_bootstrap)
    if status ~= 200 then
        hebnix.log("PfpOverlayV2: PSN token request failed, status=" .. tostring(status) ..
            " body=" .. tostring(body):sub(1, 300))
        if is_bootstrap then
            psn_token_flow_failed("token exchange failed (HTTP " .. tostring(status) .. ")")
        else
            if psn_npsso() ~= "" then
                hebnix.log("PfpOverlayV2: PSN refresh_token rejected, falling back to NPSSO bootstrap")
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
        refresh_token_expires_at = data.refresh_token_expires_in
            and (now + tonumber(data.refresh_token_expires_in))
            or old_tokens.refresh_token_expires_at,
    }
    save_psn_tokens(tokens)
    psn_token_flow_active = false
    hebnix.log("PfpOverlayV2: PSN access token " ..
        (is_bootstrap and "authenticated fresh via NPSSO" or "refreshed"))
    drain_psn_waiters()
end

-- the NPSSO exchange's redirect lands here with the auth code
local function handle_psn_authorize_redirect(status, location)
    local code = location:match("[?&]code=([^&]+)")
    if not code then
        hebnix.log("PfpOverlayV2: PSN NPSSO exchange failed (status=" .. tostring(status) ..
            " location=" .. tostring(location) .. "). NPSSO is likely expired or invalid - " ..
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

-- fetch the profile now that we have a valid access token - PSN's
-- legacy endpoint keys by online ID (username), not account ID
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
    p.status = "fetching (psn, manual)"
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
        hebnix.log("PfpOverlayV2: " .. req.kind .. " request for " .. p.name .. " failed, status=" ..
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
        local people = data.content and data.content.people
        avatar_url = people and people[1] and people[1].displayPicRaw
    elseif p.platform:find("ps") then
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
        start_avatar_download(req.pid, avatar_url, "manual")
    else
        p.status = "no avatar in response (manual)"
    end
end

local function process_tracker_stats(key, stats)
    local p = players[key]
    if not p or p.status == "override" then return end
    if stats.error then
        p.status = "tracker error: " .. tostring(stats.error)
    elseif stats.not_found then
        p.status = "tracker: profile not found"
    elseif stats.avatar_url and stats.avatar_url ~= "" then
        p.avatar_url = stats.avatar_url
        start_avatar_download(key, stats.avatar_url, "tracker")
    else
        p.status = "no avatar available (tracker.gg has none for this profile)"
    end
end

-- poll every pending tracker lookup once per tick. pending_tracker maps
-- the stats lookup key (hebnix.stats_result's key) to our player key -
-- these differ for test fetches, which use fetch_profile_async's own
-- "platform:identifier" key instead of a real match PrimaryId.
local function poll_tracker_results()
    for stats_key, player_key in pairs(pending_tracker) do
        local result = hebnix.stats_result(stats_key)
        if result ~= nil and result ~= "pending" then
            pending_tracker[stats_key] = nil
            process_tracker_stats(player_key, result)
        end
    end
end

local function resolve_avatar(key)
    local p = players[key]
    if not p then return end

    local overrides = load_overrides()
    local override_key = p.platform .. "|" .. p.platform_id
    if overrides[override_key] then
        p.avatar_path = overrides[override_key]
        p.avatar_url = nil
        p.status = "override"
        return
    end
    p.avatar_path = nil

    local priority = platform_priority(p.platform)
    if priority == "manual" then
        if p.platform == "steam" and steam_api_key() ~= "" then
            manual_fetch_steam(key)
            return
        elseif p.platform:find("xbox") and xbox_api_key() ~= "" then
            manual_fetch_xbox(key)
            return
        elseif p.platform:find("ps") and psn_npsso() ~= "" then
            ensure_psn_token_then_fetch(key)
            return
        end
        hebnix.log("PfpOverlayV2: manual fetch prioritized for " .. p.platform ..
            " but no key/npsso configured, falling back to tracker.gg")
    end

    hebnix.fetch_stats_async(p.raw_pid, p.name)
    pending_tracker[p.raw_pid] = key
    p.status = "fetching (tracker.gg)"
end

function plugin.on_http_download_response(url, status, body)
    local req = download_requests[url]
    if not req then return end
    download_requests[url] = nil
    local p = players[req.key]
    if not p then return end

    if status ~= 200 then
        p.status = "http error " .. tostring(status) .. " (avatar download)"
        return
    end

    local ext = detect_image_ext(url, body)
    local filename = sanitize_filename(p.platform .. "_" .. p.platform_id .. "_" .. req.source) .. "." .. ext
    local rel_path = "assets/cache/" .. filename
    local abs_path = PLUGIN_DIR .. "/assets/cache/" .. filename
    local f, open_err, open_errno = io.open(abs_path, "wb")
    if f then
        f:write(body)
        f:close()
        p.avatar_path = rel_path
        p.status = "resolved (" .. req.source .. ", " .. #body .. " bytes)"
        hebnix.log("PfpOverlayV2: downloaded avatar for " .. p.name .. " -> " .. abs_path ..
            " (" .. #body .. " bytes)")
    else
        p.status = "failed to write avatar file"
        hebnix.log("PfpOverlayV2: FAILED to open " .. abs_path .. " for writing: " ..
            tostring(open_err) .. " (errno=" .. tostring(open_errno) .. ")")
    end
end

local function clear_players()
    players = {}
    player_order = {}
    seen = {}
    pending_tracker = {}
    pending_requests = {}
    download_requests = {}
    last_goal = nil
    is_replay = false
end

local function remove_player(key)
    if not players[key] then return end
    players[key] = nil
    seen[key] = nil
    for i = #player_order, 1, -1 do
        if player_order[i] == key then
            table.remove(player_order, i)
            break
        end
    end
end

-- Drops any disconnected player still shown on team_num - called when
-- someone new joins that team, since that's the replacement showing up.
local function clear_disconnected_ghosts(team_num, except_key)
    local to_remove = {}
    for _, key in ipairs(player_order) do
        local p = players[key]
        if p and p.disconnected and p.team_num == team_num and key ~= except_key then
            table.insert(to_remove, key)
        end
    end
    for _, key in ipairs(to_remove) do remove_player(key) end
end

local function track_player(pid, name, team_num, shortcut)
    if pid == "" and name == "" then return end
    local key = player_key(pid, name)
    if seen[key] then return end
    seen[key] = true
    local is_bot = hebnix.is_bot(pid)
    local platform, platform_id = parse_platform(pid)
    players[key] = {
        name = name, platform = platform, platform_id = platform_id, is_bot = is_bot,
        raw_pid = pid,
        avatar_path = nil, avatar_url = nil, status = is_bot and "bot (no avatar)" or "new",
        team_num = team_num or 0, score = 0, shortcut = shortcut or 0, disconnected = false,
    }
    table.insert(player_order, key)
    if not is_bot then resolve_avatar(key) end
    if team_num then clear_disconnected_ghosts(team_num, key) end
end

local function update_player_state(pid, name, team_num, score, shortcut)
    local key = player_key(pid, name)
    local p = players[key]
    if not p then return end
    p.team_num = team_num or p.team_num
    p.score = score or p.score
    p.shortcut = shortcut or p.shortcut
end

-- ==========================================
-- Callbacks
-- ==========================================

function plugin.on_load()
    hebnix.log("PfpOverlayV2 loaded")
end

function plugin.on_game_event(event_type, event)
    if event_type == "UpdateState" then
        for _, p in ipairs(event.data.Players or {}) do
            local pid = p.PrimaryId or ""
            local name = p.Name or "Unknown"
            if pid ~= "" or name ~= "" then
                track_player(pid, name, p.TeamNum, p.Shortcut)
                update_player_state(pid, name, p.TeamNum, p.Score, p.Shortcut)
            end
        end
        local game = event.data.Game
        if game and game.bReplay ~= nil then
            is_replay = game.bReplay
        end
    elseif event_type == "PlayerLeft" then
        -- keep them tracked but marked disconnected instead of removing
        -- outright - draw_scoreboard_avatars renders them at the bottom
        -- row of the next team size up, same spot RL's own scoreboard
        -- leaves a departed player. clear_disconnected_ghosts drops them
        -- for good once someone new takes their place on that team.
        local pid = event.data.PrimaryId or ""
        local name = event.data.PlayerName or event.data.Name or ""
        local key = player_key(pid, name)
        local p = players[key]
        if p then
            p.disconnected = true
            hebnix.log("PfpOverlayV2: PlayerLeft " .. name .. " (" .. key .. "), marked disconnected")
        end
    elseif event_type == "GoalScored" then
        local scorer_name = event.data.Scorer and event.data.Scorer.Name or ""
        local scorer_key = nil
        for _, key in ipairs(player_order) do
            if players[key].name == scorer_name then
                scorer_key = key
                break
            end
        end
        last_goal = { scorer_name = scorer_name, scorer_key = scorer_key, timestamp = os.time() }
        hebnix.log("PfpOverlayV2: GoalScored by " .. scorer_name .. " (matched key: " .. tostring(scorer_key) .. ")")
    elseif event_type == "GameLeft" or event_type == "MatchEnded" then
        clear_players()
    end
end

local AVATAR_SIZE = 64
local AVATAR_GAP = 8
local AVATAR_START_X = 40
local AVATAR_START_Y = 40

local overlay_visible = false
local toggle_was_pressed = false
local capturing_bind = false

local scoreboard_held = false
local scoreboard_capturing_bind = false

function plugin.on_tick()
    poll_tracker_results()

    local bind = hebnix.get_string("overlay_toggle_bind", "")
    if bind ~= "" then
        local pressed = hebnix.is_bind_pressed(bind)
        if pressed and not toggle_was_pressed then
            overlay_visible = not overlay_visible
        end
        toggle_was_pressed = pressed
    end

    if capturing_bind then
        local status, bind_result = hebnix.capture_bind_result()
        if status == "done" then
            hebnix.set("overlay_toggle_bind", bind_result)
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
            scoreboard_capturing_bind = false
        elseif status == "timeout" then
            scoreboard_capturing_bind = false
        end
    end
end

-- Returns each team's active (still-connected) roster, plus at most one
-- disconnected "ghost" player per team, kept separate from the active
-- list so a departed player never affects the other players' rows.
local function scoreboard_teams()
    local blue, orange = {}, {}
    local blue_ghost, orange_ghost = nil, nil
    for _, pid in ipairs(player_order) do
        local p = players[pid]
        local is_orange = p.team_num == 1
        if p.disconnected then
            if is_orange then orange_ghost = pid else blue_ghost = pid end
        elseif is_orange then
            table.insert(orange, pid)
        else
            table.insert(blue, pid)
        end
    end
    -- same order RL's own scoreboard uses: score descending, and when
    -- scores tie (usually 0-0 early in a match) it falls back to each
    -- player's shortcut id, also descending.
    local function by_score_desc(a, b)
        local pa, pb = players[a], players[b]
        if pa.score ~= pb.score then return pa.score > pb.score end
        return (pa.shortcut or 0) > (pb.shortcut or 0)
    end
    table.sort(blue, by_score_desc)
    table.sort(orange, by_score_desc)
    return blue, orange, blue_ghost, orange_ghost
end

local function draw_debug_stack(draw)
    if not overlay_visible then return end
    if #player_order == 0 then return end
    local y = AVATAR_START_Y
    local goal_age = last_goal and (tostring(os.time() - last_goal.timestamp) .. "s ago (" ..
        last_goal.scorer_name .. ")") or "none"
    draw.text(AVATAR_START_X, AVATAR_START_Y - 36,
        "pfpoverlayv2 debug overlay  —  is_replay=" .. tostring(is_replay) .. "  last_goal=" .. goal_age,
        { color = "#00ff00ff", size = 16 })
    for _, pid in ipairs(player_order) do
        local p = players[pid]
        local tag = hebnix.platform_tag(p.raw_pid or pid)
        if p.avatar_path then
            pcall(function()
                draw.image(p.avatar_path, AVATAR_START_X, y, AVATAR_SIZE, AVATAR_SIZE)
            end)
        end
        local team_label = (p.team_num == 1) and "orange" or "blue"
        local disc_tag = p.disconnected and " [disconnected]" or ""
        draw.text(AVATAR_START_X + AVATAR_SIZE + 8, y + AVATAR_SIZE / 2 - 8,
            tag .. " " .. p.name .. "  [" .. team_label .. " " .. tostring(p.score) .. "]" .. disc_tag ..
                "  —  " .. p.status,
            { color = "#ffffffff", size = 14 })
        y = y + AVATAR_SIZE + AVATAR_GAP
    end
end

-- Draws one team's rows using ONLY that team's own size - a 1-player
-- team next to a 2-player team (unfair exhibition modes, or a real
-- match down to fewer active players) each get their own real layout
-- instead of both being forced to match the bigger side.
local function draw_team_avatars(draw, team_color, list, ghost, w, h)
    local size = #list
    if size > 0 then
        for _, slot in ipairs(get_scoreboard_slots(size, w, h)) do
            if slot.team == team_color then
                local p = players[list[slot.row + 1]]
                if p and p.avatar_path then
                    pcall(function() draw.image(p.avatar_path, slot.x, slot.y, slot.w, slot.h) end)
                end
            end
        end
    end
    if ghost then
        -- one row bigger than the active layout, at the new bottom row -
        -- e.g. a 2v2 that lost a player renders that team like a 3v3
        -- with the departed player pinned to row 3.
        for _, slot in ipairs(get_scoreboard_slots(size + 1, w, h)) do
            if slot.team == team_color and slot.row == size then
                local p = players[ghost]
                if p.avatar_path then
                    pcall(function() draw.image(p.avatar_path, slot.x, slot.y, slot.w, slot.h) end)
                end
            end
        end
    end
end

local function draw_scoreboard_avatars(draw, w, h)
    if not scoreboard_held then return end
    local blue, orange, blue_ghost, orange_ghost = scoreboard_teams()
    if #blue == 0 and #orange == 0 and not blue_ghost and not orange_ghost then return end

    draw_team_avatars(draw, "blue", blue, blue_ghost, w, h)
    draw_team_avatars(draw, "orange", orange, orange_ghost, w, h)
end

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
    draw_debug_stack(draw)
    draw_scoreboard_avatars(draw, w, h)
    draw_goal_nameplate(draw, w, h)
end

-- ==========================================
-- Settings
-- ==========================================

function plugin.on_settings(ui)
    ui.heading("pfp overlay v2")
    ui.label("shows player avatars on the scoreboard and goal replay nameplate.")
    ui.label("avatars come from tracker.gg automatically, no api keys needed.")

    -- ==========================================
    -- quick start - the stuff you need to set up first
    -- ==========================================

    ui.space(10)
    ui.heading("Quick Start")

    ui.space(4)
    ui.heading("Interface scale")
    ui.label("set this to match RL's own options > video > interface scale,")
    ui.label("or the profiles will render in the wrong spot.")
    ui.text_input("rl_ui_scale", "RL interface scale", tostring(REFERENCE_UI_SCALE))

    ui.space(8)
    ui.heading("Scoreboard button")
    ui.label("bind whatever shows RL's scoreboard (hold tab on keyboard,")
    ui.label("view/select on controller).")
    local sb_bind = hebnix.get_string("scoreboard_button", "")
    ui.horizontal(function()
        ui.label("bind: " .. (sb_bind ~= "" and sb_bind or "(none set)"))
        if scoreboard_capturing_bind then
            ui.colored_label("#d35400", "press any key/button...")
        else
            if ui.button("set") then
                if hebnix.capture_bind_async(10) then
                    scoreboard_capturing_bind = true
                end
            end
            if ui.button("clear") then
                hebnix.set("scoreboard_button", "")
            end
        end
    end)

    ui.space(8)
    ui.heading("Avatar overrides")
    ui.label("Force a specific image for one player, by platform + id. handy")
    ui.label("for switch (no avatar api at all) or anyone tracker.gg doesn't")
    ui.label("have a picture for. Drop the image in the assets folder first,")
    ui.label("then reference it below as assets/name.png.")

    if ui.button("open assets folder") then
        hebnix.settings.open_assets()
    end

    ui.space(4)
    local override_platform = ui.combo_box("override_add_platform", "platform",
        { "steam", "xboxone", "epic", "psn", "switch" })
    local override_id = ui.text_input("override_add_id", "player id (steamid64 / gamertag / etc)")
    local override_path = ui.text_input("override_add_path", "image path, e.g. assets/me.png")

    if ui.button("add / update override") then
        local id = override_id:match("^%s*(.-)%s*$")
        local path = override_path:match("^%s*(.-)%s*$")
        if id ~= "" and path ~= "" then
            local key = override_platform .. "|" .. id
            local overrides = load_overrides()
            overrides[key] = path
            write_overrides_file(overrides)
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
                if ui.button("remove") then
                    current_overrides[k] = nil
                    write_overrides_file(current_overrides)
                end
            end)
        end
    end

    ui.space(6)
    if ui.button("open overrides.json") then
        if not read_overrides_file() then write_overrides_file(load_overrides()) end
        hebnix.open_url(OVERRIDES_PATH)
    end
    ui.label(OVERRIDES_PATH)

    -- ==========================================
    -- fetching - api keys, priority, manual test
    -- ==========================================

    ui.space(12)
    ui.heading("Fetching")
    ui.label("Per platform, pick tracker.gg (default, no key needed) or")
    ui.label("manual - this plugin's own key/psn-login pipeline from v1. if")
    ui.label("manual is picked but nothing's filled in, it just falls back")
    ui.label("to tracker.gg for that platform.")

    ui.space(4)
    ui.horizontal(function()
        ui.combo_box("priority_steam", "steam priority", { "tracker", "manual" })
        ui.text_input("steam_api_key", "steam web api key (steamcommunity.com/dev)", "")
    end)
    ui.horizontal(function()
        ui.combo_box("priority_xboxone", "xbox priority", { "tracker", "manual" })
        ui.text_input("xbox_api_key", "xbox api key (xbl.io)", "")
    end)
    ui.horizontal(function()
        ui.combo_box("priority_psn", "psn priority", { "tracker", "manual" })
        ui.text_input("psn_npsso", "psn npsso", "")
    end)
    if psn_have_valid_access_token() then
        ui.colored_label("#2ecc71", "psn (manual): authenticated")
    elseif load_psn_tokens().refresh_token then
        ui.colored_label("#d35400", "psn (manual): token expired, will auto-refresh on next lookup")
    else
        ui.colored_label("#aaaaaa", "psn (manual): not authenticated yet")
    end
    ui.label("One-time psn setup: log into playstation.com in a browser, then")
    ui.label("in that same browser session visit")
    ui.label("https://ca.account.sony.com/api/v1/ssocookie and paste its")
    ui.label("\"npsso\" value above.")

    ui.space(8)
    ui.heading("Test fetch")
    ui.label("Pick a platform + fetch method, type an id, and try it - also")
    ui.label("adds a temporary entry to the tracked players list below.")
    local test_platform = ui.combo_box("test_platform", "platform",
        { "steam", "xboxone", "epic", "psn", "switch" })
    local test_mode = ui.combo_box("test_mode", "fetch via", { "tracker", "manual" })
    local test_identifier = ui.text_input("test_identifier", "steamid64 / gamertag / username / etc")

    if ui.button("test fetch avatar") then
        local identifier = test_identifier:match("^%s*(.-)%s*$")
        if identifier ~= "" then
            local key = "Test|" .. test_platform .. "|" .. identifier
            if not players[key] then
                players[key] = {
                    name = identifier, platform = test_platform, platform_id = identifier,
                    raw_pid = "Test|" .. identifier .. "|0", is_bot = false,
                    avatar_path = nil, avatar_url = nil, status = "new",
                    team_num = 0, score = 0,
                }
                table.insert(player_order, key)
                seen[key] = true
            end
            local p = players[key]
            p.avatar_path = nil
            p.avatar_url = nil
            if test_mode == "manual" then
                if test_platform == "steam" then
                    manual_fetch_steam(key)
                elseif test_platform:find("xbox") then
                    manual_fetch_xbox(key)
                elseif test_platform:find("ps") then
                    ensure_psn_token_then_fetch(key)
                else
                    p.status = "no manual method for " .. test_platform
                end
            else
                local stats_key = hebnix.fetch_profile_async(test_platform, identifier)
                if stats_key then
                    pending_tracker[stats_key] = key
                    p.status = "fetching (tracker.gg)"
                end
            end
        end
    end

    local test_key = "Test|" .. test_platform .. "|" .. test_identifier:match("^%s*(.-)%s*$")
    local test_p = players[test_key]
    if test_p then
        ui.label("status: " .. test_p.status)
        if test_p.avatar_path then
            ui.image(ui_asset_path(test_p.avatar_path), { width = 64, height = 64 })
        elseif test_p.avatar_url then
            ui.image(test_p.avatar_url, { width = 64, height = 64 })
        end
    end

    -- ==========================================
    -- tracked players
    -- ==========================================

    ui.space(12)
    ui.heading("Tracked players")
    if ui.button("re-resolve all") then
        for _, pid in ipairs(player_order) do resolve_avatar(pid) end
    end
    if ui.button("clear tracked players") then clear_players() end

    ui.space(4)
    ui.label("\"copy id\" copies just the player id - pick the matching")
    ui.label("platform separately in the override dropdown above.")
    if #player_order == 0 then
        ui.label("no players tracked yet, join a match.")
    end
    for _, pid in ipairs(player_order) do
        local p = players[pid]
        local tag = hebnix.platform_tag(p.raw_pid or pid)
        local id_str = p.platform_id
        ui.horizontal(function()
            if p.avatar_path then
                ui.image(ui_asset_path(p.avatar_path), { width = 32, height = 32 })
            elseif p.avatar_url then
                ui.image(p.avatar_url, { width = 32, height = 32 })
            end
            local disc_tag = p.disconnected and " [disconnected]" or ""
            ui.label(tag .. " " .. p.name .. disc_tag .. "  —  " .. p.status)
            if ui.button("copy id") then
                ui.copy_to_clipboard(id_str)
            end
        end)
    end

    -- ==========================================
    -- debug overlay - the on-screen list of tracked players + status,
    -- separate from the scoreboard pfps above
    -- ==========================================

    ui.space(12)
    ui.heading("Debug overlay")
    ui.label("An on-screen list of tracked players and their fetch status,")
    ui.label("for troubleshooting - not the scoreboard pfps themselves.")
    local bind = hebnix.get_string("overlay_toggle_bind", "")
    ui.horizontal(function()
        ui.label("toggle bind: " .. (bind ~= "" and bind or "(none — always visible)"))
        if capturing_bind then
            ui.colored_label("#d35400", "press any key/button...")
        else
            if ui.button("set") then
                if hebnix.capture_bind_async(10) then
                    capturing_bind = true
                end
            end
            if ui.button("clear") then
                hebnix.set("overlay_toggle_bind", "")
            end
        end
    end)
    ui.label("currently: " .. (overlay_visible and "visible" or "hidden"))
end

function plugin.on_unload()
    hebnix.log("PfpOverlayV2 unloaded")
end

return plugin
