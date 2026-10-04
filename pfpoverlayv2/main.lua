-- PfpOverlayV2: profile pictures on the scoreboard + goal replay nameplate.
-- No keys or PSN login required unless manual mode is picked per
-- platform (see settings). v1 stays untouched as a fallback plugin.
--
-- Note: draw.image (scoreboard/nameplate rendering) only reads local
-- files, not urls - only ui.image (settings widgets) can load a url
-- directly. So even a hebnix-resolved avatar still gets downloaded to
-- assets/cache/ ourselves before it can be drawn on screen.

local plugin = {}

local PLUGIN_DIR = hebnix.plugin_dir()

-- only logs when "debug logs" is on in settings, so the console stays quiet
local function dlog(msg)
    if hebnix.get_bool("debug_logs", false) then hebnix.log(msg) end
end

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
        dlog("PfpOverlayV2: FAILED to open " .. OVERRIDES_PATH .. " for writing")
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

-- "hebnix" (default) or "manual". epic has no manual method, so it
-- always goes through hebnix.
local function platform_priority(platform)
    if platform == "steam" then return hebnix.get_string("priority_steam", "hebnix") end
    if platform:find("xbox") then return hebnix.get_string("priority_xboxone", "hebnix") end
    if platform:find("ps") then return hebnix.get_string("priority_psn", "hebnix") end
    return "hebnix"
end

local function steam_api_key() return hebnix.get_string("steam_api_key", "") end
local function xbox_api_key() return hebnix.get_string("xbox_api_key", "") end

-- ==========================================
-- Match state, same as the PlatformDisplay plugin - which playlist is
-- up (mutator strip, private/offline matches), whether a goal replay is
-- on, and the first tab-open after a match starts, which lands a few
-- pixels off from every later one.
-- ==========================================

local PRIVATE_PLAYLIST = 6
local TOURNAMENT_PLAYLIST = 34

local in_match = false
local match_ended = false
local match_guid = nil
local is_replay = false
local my_id = nil
local freeplay = false
local offline = false
local current_playlist = nil
local mutators = {}
local mutator_count = 0
local match_log_key = nil
local first_tab_pending = true
local in_first_open = false
local shown_first_open = false
local scoreboard_was_held = false

local function shows_mutator_strip()
    if current_playlist == TOURNAMENT_PLAYLIST then return true end
    return mutator_count > 0
end

-- players who left only stay on the board in matchmade games, private
-- and offline matches drop them straight away
local function matchmade()
    if offline then return false end
    return current_playlist ~= nil and current_playlist ~= PRIVATE_PLAYLIST
end

-- ==========================================
-- Layout (identical to PfpOverlay v1 - ported from the Python
-- rl-pfp-overlay project's layout.py; see v1's main.lua for the full
-- derivation history behind these constants.)
-- ==========================================

local REFERENCE_RESOLUTION = { 2560, 1440 }
local REFERENCE_UI_SCALE = 0.75

-- Interface Scale, read straight from the .save profile (GameplaySettingsSave_TA.UIScale)
-- instead of asking the user to type it in. Refreshed on a timer via the
-- non-blocking save_summary_async - decrypt + parse is real work, doing it
-- synchronously on the ui thread every 5s was stalling the whole app.
local detected_ui_scale = nil
local last_ui_scale_check = 0
local ui_scale_pending_key = nil

local function refresh_detected_ui_scale()
    if ui_scale_pending_key then
        local summary = hebnix.save_summary_result(ui_scale_pending_key)
        if summary == nil or summary == "pending" then return end
        ui_scale_pending_key = nil
        if type(summary) == "table" and summary.ui_scale and summary.ui_scale > 0 then
            detected_ui_scale = summary.ui_scale
        elseif not detected_ui_scale then
            -- couldn't read the save file and nothing was ever detected:
            -- switch to a manual 100% so the profiles at least land
            -- somewhere sensible, and tell the user why in the settings.
            local why = type(summary) == "table" and summary.error or "no interface scale in save data"
            dlog("PfpOverlayV2: save data fetch failed (" .. tostring(why) .. "), auto-detect off, scale 100%")
            hebnix.set("ui_scale_auto_detect", false)
            hebnix.set("rl_ui_scale_percent", "100")
            hebnix.set("ui_scale_autodisabled", true)
        end
        return
    end

    if not hebnix.get_bool("ui_scale_auto_detect", true) then return end
    if os.time() - last_ui_scale_check < 5 then return end
    last_ui_scale_check = os.time()
    hebnix.clear_save_summary_cache()
    ui_scale_pending_key = hebnix.load_save_summary_async()
end

local function ui_scale()
    if hebnix.get_bool("ui_scale_auto_detect", true) and detected_ui_scale then
        return detected_ui_scale
    end
    -- manual field is a percentage ("75" = 75%, same as RL's own slider)
    local raw = hebnix.get_string("rl_ui_scale_percent", "")
    if raw == "" then
        -- field from before it was a percentage held a decimal (0.75)
        local old = tonumber(hebnix.get_string("rl_ui_scale", ""))
        if old and old > 0 then return old end
        return REFERENCE_UI_SCALE
    end
    local value = tonumber((raw:gsub("%s*%%%s*$", "")))
    if not value or value <= 0 then return REFERENCE_UI_SCALE end
    return value / 100
end

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

-- Scoreboard layout, same as the PlatformDisplay plugin's sb_layout.
-- Units are 1080p scoreboard pixels, scaled by the resolution and the
-- interface scale.
local SB = {
    left = 537,
    blue_bottom = 67,
    orange_top = 43,
    banner_distance = 57,
    board_w = 1033,
    board_h = 548,
    imbalance = 32,
    y_offcenter = 32,
}

local MUTATOR_EDGE = 1030
local REPLAY_SHIFT = 0
local X_OFFSET = -35
local X_OFFSET_FIRST = -35

local ICON_COL = -530.5
local ICON_PX = 100
local IMAGE_SCALE = 0.48
local GHOST_OPACITY = 0.4
-- tuned in a 1v0: full imbalance (32) needed a -27 y nudge
local EMPTY_TEAM_SHIFT = 5
-- both teams have players (1v1, 2v2, 2v1...), sat 1 unit low
local FULL_TEAMS_SHIFT = -1

local function sb_layout(w, h, scale_mult, x_offset, mutator_edge, blues, oranges, replay_shift)
    local scale
    if w / h > 1.5 then
        scale = 0.507 * h / SB.board_h
    else
        scale = 0.615 * w / SB.board_w
    end
    local s = scale * scale_mult

    local cx = w / 2
    if mutator_edge > 0 then
        local strip_cx = w - mutator_edge * s
        if strip_cx < cx then cx = strip_cx end
    end
    cx = cx + x_offset * s
    cx = cx - replay_shift * s

    local cy = h / 2 + SB.y_offcenter * s

    -- an empty team still holds about one row on rl's board, but sits
    -- EMPTY_TEAM_SHIFT units lower than a real 1-player row would
    local difference = blues - oranges
    local lopsided = (blues == 0) ~= (oranges == 0)
    local sign = difference >= 0 and 1 or -1
    cy = cy + SB.imbalance * (difference - (lopsided and sign or 0)) * s
    if lopsided then
        cy = cy + sign * EMPTY_TEAM_SHIFT * s
    else
        cy = cy + FULL_TEAMS_SHIFT * s
    end

    return {
        scale = s,
        centre = cx,
        size = ICON_PX * IMAGE_SCALE * s,
        blue_y = cy + (-SB.blue_bottom + 6 * (4 - blues) - SB.banner_distance * blues + 9) * s,
        orange_y = cy + SB.orange_top * s,
        separation = SB.banner_distance * s,
    }
end

local GOAL_NAMEPLATE_DELAY_SECONDS = 3.5
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
    x = x + hebnix.get_number("goal_nameplate_x_nudge", 0)
    y = y + hebnix.get_number("goal_nameplate_y_nudge", 0)
    return scale_slot(x, y, w, h, NAMEPLATE_UI_QUAD, screen_w, screen_h)
end

-- ==========================================
-- Player tracking
-- ==========================================

-- players[key] holds both the avatar lookup state and the scoreboard
-- roster fields (order, team, ghost, left, score, shortcut). roster
-- handling is the PlatformDisplay plugin's: players who leave stay as
-- dimmed ghosts in matchmade games until someone takes their slot.
local players = {}
local player_order = {}
local roster_seq = 0
local pending_tracker = {} -- key -> true, players awaiting a hebnix.stats_result

local last_goal = nil -- { scorer_name, scorer_key, timestamp } or nil

local function refresh_match_playlist()
    if not match_log_key then
        hebnix.clear_launch_log()
        match_log_key = hebnix.parse_launch_log_async(false)
        return
    end
    local info = hebnix.launch_log_result(match_log_key)
    if type(info) ~= "table" then return end
    match_log_key = nil
    if type(info.session) == "table" then my_id = info.session.primary_id end
    if type(info.game) ~= "table" then return end
    current_playlist = tonumber(info.game.playlist_id)
    offline = info.game.offline == true
    mutators = info.game.mutators or {}
    freeplay = false
    for _, tag in ipairs(mutators) do
        if tag == "Freeplay" then freeplay = true end
    end
    mutator_count = math.max(tonumber(info.game.mutator_count) or 0, #mutators)
end

local function parse_platform(pid)
    local platform, id_part = pid:match("^([^|]+)|([^|]+)")
    if not platform then return "unknown", pid end
    return platform:lower(), id_part
end

local function is_bot_id(pid)
    return pid == "" or hebnix.is_bot(pid)
end

-- real players are keyed by their platform id. bots (and anyone without
-- one) have no id, so they use the per-match shortcut instead of the name:
-- two bots or players can share a name, never a shortcut. name is only
-- the last resort when the event carries no shortcut at all.
local function player_key(pid, name, shortcut)
    if is_bot_id(pid) then
        if shortcut then return "bot|sc:" .. tostring(math.floor(shortcut)) end
        return "bot|name:" .. name
    end
    return pid
end

-- ==========================================
-- Avatar resolution via Hebnix's built-in avatar-lookup integration
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
        if ext == "gif" then return "gif" end
    end
    if body and #body >= 4 then
        local b1, b2, b3 = string.byte(body, 1, 3)
        if b1 == 255 and b2 == 216 and b3 == 255 then return "jpg" end
        if b1 == 137 and b2 == 80 and b3 == 78 then return "png" end
        if b1 == 71 and b2 == 73 and b3 == 70 then return "gif" end
    end
    return "png"
end

-- avatar url -> {key, source}, for on_http_download_response. source
-- ("hebnix" or "manual") keeps the two saved as separate files, so
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
-- Local identity - who's actually running the client, read from
-- Hebnix's own launch log instead of trusting anything the plugin
-- could otherwise be told. Used to auto-fill "own id" fields and to
-- lock CDN uploads to the account actually playing.
-- ==========================================

local local_epic_id = nil
local last_identity_check = 0
local identity_pending_key = nil

local function refresh_local_identity()
    if identity_pending_key then
        local log = hebnix.launch_log_result(identity_pending_key)
        if log == nil or log == "pending" then return end
        identity_pending_key = nil
        local session = log and log.session
        if type(session) ~= "table" then return end
        local primary_id = session.primary_id and string.lower(tostring(session.primary_id)) or ""
        if primary_id:match("^epic|") then
            local_epic_id = primary_id:match("^epic|([^|]+)")
        elseif session.epic_id and tostring(session.epic_id) ~= "" then
            local_epic_id = string.lower(tostring(session.epic_id))
        end
        return
    end

    if os.time() - last_identity_check < 5 then return end
    last_identity_check = os.time()
    hebnix.clear_launch_log()
    identity_pending_key = hebnix.parse_launch_log_async(false)
end

-- ==========================================
-- CDN avatar uploads (pubapi.hebnix.com) - lets epic players, who have
-- no public avatar api, contribute their own avatar to a shared CDN
-- other clients can look up. Locked to local_epic_id so this can only
-- ever upload the account actually running the client - overrides.json
-- (below) stays the place for setting anyone else's avatar. Uses
-- hebnix.http_multipart_post_async, which sends the file as raw bytes
-- (not base64), so this costs exactly the file's own size on the wire.
-- ==========================================

local CDN_UPLOAD_URL = "https://pubapi.hebnix.com/rocket-profiles/upload"
local CDN_LOOKUP_URL = "https://pubapi.hebnix.com/rocket-profiles/images"

-- req_id -> platform_id, for on_http_upload_response
local pending_uploads = {}
-- req_id -> asset path, so a successful upload can be remembered
local pending_upload_paths = {}
-- platform_id -> "uploading" | "ok" | "error: ..." for the settings ui
local upload_status = {}

-- image_path may be given relative to the plugin dir (e.g.
-- "assets/me.png", matching how overrides are entered) or already
-- absolute - http_multipart_post_async reads straight off disk so it
-- needs a real filesystem path either way.
local function resolve_asset_path(path)
    if path:match("^%a:[\\/]") or path:match("^[\\/]") then return path end
    return PLUGIN_DIR .. "/" .. path
end

function plugin.upload_profile_image(image_path)
    if not local_epic_id or image_path == "" then return end
    local abs_path = resolve_asset_path(image_path)
    local req_id = "cdn_upload:" .. local_epic_id .. ":" .. tostring(os.time())
    pending_uploads[req_id] = local_epic_id
    pending_upload_paths[req_id] = image_path
    upload_status[local_epic_id] = "uploading"
    hebnix.http_multipart_post_async(
        req_id,
        CDN_UPLOAD_URL,
        { platform_id = local_epic_id },
        { image = abs_path },
        {}
    )
end

-- the cdn drops images that go unrequested for a few hours, so after a
-- reload the last uploaded avatar gets put back once the epic id is known
local reupload_pending = false
-- uploading too often can get blocked, so a reload only re-uploads once the
-- last upload (or the last re-upload attempt) is at least this old
local REUPLOAD_MIN_AGE_SECONDS = 90 * 60

local function reupload_last_avatar()
    if not reupload_pending or not local_epic_id then return end
    reupload_pending = false
    local path = hebnix.get_string("last_uploaded_avatar", "")
    if path == "" or hebnix.get_string("last_uploaded_avatar_id", "") ~= local_epic_id then
        -- nothing uploaded yet with this account, use the image picked in settings
        path = hebnix.get_string("cdn_upload_asset", "")
        if path == "" then return end
        if not path:match("^assets[\\/]") then path = "assets/" .. path end
    end
    local last = math.max(hebnix.get_number("last_uploaded_at", 0),
        hebnix.get_number("last_reupload_attempt_at", 0))
    local age = os.time() - last
    local age_min = math.floor(age / 60)
    local wait_min = math.floor(REUPLOAD_MIN_AGE_SECONDS / 60)
    if age < REUPLOAD_MIN_AGE_SECONDS then
        dlog(string.format(
            "PfpOverlayV2: not re-uploading avatar, last upload was %d min ago (waits %d min)",
            age_min, wait_min))
        return
    end
    dlog(string.format(
        "PfpOverlayV2: re-uploading last avatar %s, last upload was %d min ago",
        path, age_min))
    hebnix.set("last_reupload_attempt_at", os.time())
    plugin.upload_profile_image(path)
end

function plugin.on_http_upload_response(req_id, status, body)
    local platform_id = pending_uploads[req_id]
    if not platform_id then return end
    pending_uploads[req_id] = nil
    local uploaded_path = pending_upload_paths[req_id]
    pending_upload_paths[req_id] = nil

    if status == 200 or status == 201 then
        upload_status[platform_id] = "ok"
        if uploaded_path then
            hebnix.set("last_uploaded_avatar", uploaded_path)
            hebnix.set("last_uploaded_avatar_id", platform_id)
            hebnix.set("last_uploaded_at", os.time())
        end
        local ok, data = pcall(hebnix.json_decode, body)
        local image_url = (ok and type(data) == "table") and data.image or nil
        dlog("PfpOverlayV2: CDN upload for " .. platform_id .. " succeeded" ..
            (image_url and (" -> " .. image_url) or ""))
    else
        upload_status[platform_id] = "error: http " .. tostring(status)
        dlog("PfpOverlayV2: CDN upload for " .. platform_id .. " failed, status=" ..
            tostring(status) .. " body=" .. tostring(body):sub(1, 500))
    end
end

-- CDN lookup - batched, since the api takes a list of platform_ids in one
-- call rather than one request per player. epic players land here (see
-- process_tracker_stats) queued up; a periodic flush (on_tick) fires one
-- POST per batch instead of one per player.
local CDN_LOOKUP_REQ_ID = "cdn_lookup"
local pending_epic_lookups = {} -- platform_id -> player key, queued since the last flush
local epic_lookups_in_flight = nil -- platform_id -> player key, sent but no response yet

local function flush_epic_lookups()
    if epic_lookups_in_flight or next(pending_epic_lookups) == nil then return end

    -- move (not copy) so anything queued after this point while the
    -- request is in flight starts a fresh batch on the next flush,
    -- instead of getting swept into this response's accounting
    epic_lookups_in_flight = pending_epic_lookups
    pending_epic_lookups = {}

    local ids = {}
    for platform_id in pairs(epic_lookups_in_flight) do
        table.insert(ids, platform_id)
    end

    local body = hebnix.json_encode({ platform_ids = ids })
    dlog("PfpOverlayV2: CDN lookup request body=" .. tostring(body))
    hebnix.http_post_async(CDN_LOOKUP_REQ_ID, CDN_LOOKUP_URL, body,
        { ["Content-Type"] = "application/json" })
end

-- pubapi's own json responses (both /upload and /images) return image urls
-- shaped ".../rocket-profiles/files/<name>", but the file is actually
-- served at ".../files/rocket-profiles/<name>" (segments swapped) -
-- confirmed against a real working link. server-side inconsistency in
-- their response, not ours; this just corrects it before downloading.
local function fix_cdn_image_url(url)
    return (url:gsub("/rocket%-profiles/files/", "/files/rocket-profiles/"))
end

local function handle_cdn_lookup_response(status, body)
    local requested = epic_lookups_in_flight or {}
    epic_lookups_in_flight = nil

    dlog("PfpOverlayV2: CDN lookup response status=" .. tostring(status) ..
        " body=" .. tostring(body):sub(1, 1000))

    if status ~= 200 then
        for platform_id, key in pairs(requested) do
            local p = players[key]
            if p and p.status ~= "override" then
                p.status = "no avatar available (cdn lookup failed)"
            end
        end
        return
    end

    local ok, data = pcall(hebnix.json_decode, body)
    if not ok then
        dlog("PfpOverlayV2: CDN lookup response failed to json_decode: " .. tostring(data))
    end
    local images = (ok and type(data) == "table") and data.images or {}
    local found = {}
    for _, entry in ipairs(images) do
        local key = requested[entry.platform_id]
        if key then
            found[entry.platform_id] = true
            local p = players[key]
            if p and p.status ~= "override" then
                local image_url = fix_cdn_image_url(entry.image)
                p.avatar_url = image_url
                start_avatar_download(key, image_url, "cdn")
            end
        end
    end

    for platform_id, key in pairs(requested) do
        if not found[platform_id] then
            local p = players[key]
            if p and p.status ~= "override" then
                p.status = "no avatar available (not on cdn)"
            end
        end
    end
end

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
    dlog("PfpOverlayV2: PSN auth flow failed: " .. reason)
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
        dlog("PfpOverlayV2: PSN token request failed, status=" .. tostring(status) ..
            " body=" .. tostring(body):sub(1, 300))
        if is_bootstrap then
            psn_token_flow_failed("token exchange failed (HTTP " .. tostring(status) .. ")")
        else
            if psn_npsso() ~= "" then
                dlog("PfpOverlayV2: PSN refresh_token rejected, falling back to NPSSO bootstrap")
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
    dlog("PfpOverlayV2: PSN access token " ..
        (is_bootstrap and "authenticated fresh via NPSSO" or "refreshed"))
    drain_psn_waiters()
end

-- the NPSSO exchange's redirect lands here with the auth code
local function handle_psn_authorize_redirect(status, location)
    local code = location:match("[?&]code=([^&]+)")
    if not code then
        dlog("PfpOverlayV2: PSN NPSSO exchange failed (status=" .. tostring(status) ..
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
    if url == CDN_LOOKUP_REQ_ID then
        handle_cdn_lookup_response(status, body)
        return
    end
    local req = pending_requests[url]
    if not req then return end
    pending_requests[url] = nil
    local p = players[req.pid]
    if not p then return end

    if status ~= 200 then
        p.status = "http error " .. tostring(status) .. " (" .. req.kind .. ")"
        dlog("PfpOverlayV2: " .. req.kind .. " request for " .. p.name .. " failed, status=" ..
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
        p.status = "hebnix error: " .. tostring(stats.error)
    elseif stats.not_found and p.platform ~= "epic" then
        p.status = "hebnix: profile not found"
    elseif stats.avatar_url and stats.avatar_url ~= "" then
        p.avatar_url = stats.avatar_url
        start_avatar_download(key, stats.avatar_url, "hebnix")
    elseif p.platform == "epic" then
        -- hebnix has no epic avatars at all, so this (not just
        -- not_found) is the common case for epic players - falls
        -- through to the CDN lookup automatically, same "hebnix"
        -- fetch mode covers both.
        pending_epic_lookups[p.platform_id] = key
        p.status = "checking cdn for avatar"
    else
        p.status = "no avatar available (hebnix has none for this profile)"
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
        dlog("PfpOverlayV2: manual fetch prioritized for " .. p.platform ..
            " but no key/npsso configured, falling back to hebnix")
    end

    hebnix.fetch_stats_async(p.raw_pid, p.name)
    pending_tracker[p.raw_pid] = key
    p.status = "fetching (hebnix)"
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
        dlog("PfpOverlayV2: downloaded avatar for " .. p.name .. " -> " .. abs_path ..
            " (" .. #body .. " bytes)")
    else
        p.status = "failed to write avatar file"
        dlog("PfpOverlayV2: FAILED to open " .. abs_path .. " for writing: " ..
            tostring(open_err) .. " (errno=" .. tostring(open_errno) .. ")")
    end
end

local function clear_players()
    players = {}
    player_order = {}
    roster_seq = 0
    pending_tracker = {}
    pending_requests = {}
    download_requests = {}
    pending_epic_lookups = {}
    epic_lookups_in_flight = nil
    last_goal = nil
    in_match = false
    match_guid = nil
    is_replay = false
    current_playlist = nil
    offline = false
    freeplay = false
    mutators = {}
    mutator_count = 0
    match_log_key = nil
    first_tab_pending = true
    in_first_open = false
    scoreboard_was_held = false
end

local function remove_player(key)
    if not players[key] then return end
    players[key] = nil
    for i = #player_order, 1, -1 do
        if player_order[i] == key then
            table.remove(player_order, i)
            break
        end
    end
end

-- drops everyone on the scoreboard roster, test fetches stay
local function reset_roster()
    local keys = {}
    for _, key in ipairs(player_order) do
        if not players[key].test then table.insert(keys, key) end
    end
    for _, key in ipairs(keys) do remove_player(key) end
    roster_seq = 0
end

local function roster_entries()
    local list = {}
    for _, key in ipairs(player_order) do
        local p = players[key]
        if p and not p.test then table.insert(list, { key = key, entry = p }) end
    end
    return list
end

local function add_player(key, pid, name, is_bot)
    local platform, platform_id = parse_platform(pid)
    roster_seq = roster_seq + 1
    players[key] = {
        name = name, platform = platform, platform_id = platform_id, is_bot = is_bot,
        raw_pid = pid,
        avatar_path = nil, avatar_url = nil, status = is_bot and "bot (no avatar)" or "new",
        order = roster_seq, awaiting_slot = true,
        team = -1, score = 0, shortcut = nil, ghost = false, left = false,
    }
    table.insert(player_order, key)
    if not is_bot then resolve_avatar(key) end
    return players[key]
end

local function update_players(data)
    local source = type(data) == "table" and data.Players or nil
    if type(source) ~= "table" then return end

    local game = data.Game
    if type(game) == "table" and game.bReplay ~= nil then
        is_replay = game.bReplay == true
    end

    -- nobody from the last update is still here, it's a different match
    local overlap, had = 0, #roster_entries() > 0
    for _, player in ipairs(source) do
        local key = player_key(tostring(player.PrimaryId or ""), tostring(player.Name or "Unknown"), tonumber(player.Shortcut))
        if players[key] and not players[key].test then overlap = overlap + 1 end
    end
    if had and overlap == 0 then reset_roster() end

    local present = {}
    for _, player in ipairs(source) do
        local pid = tostring(player.PrimaryId or "")
        local name = tostring(player.Name or "Unknown")
        if pid ~= "" or name ~= "" then
            local is_bot = is_bot_id(pid)
            local key = player_key(pid, name, tonumber(player.Shortcut))
            present[key] = true

            local p = players[key]
            if not p or p.test then p = add_player(key, pid, name, is_bot) end
            p.name = name
            p.score = tonumber(player.Score) or 0
            p.shortcut = tonumber(player.Shortcut)

            local reported = tonumber(player.TeamNum) or -1
            if reported == 0 or reported == 1 then
                p.team = reported
                p.ghost = p.left or false
            else
                p.ghost = true
            end
        end
    end

    for _, item in ipairs(roster_entries()) do
        if not present[item.key] then
            if item.entry.is_bot then remove_player(item.key) else item.entry.ghost = true end
        end
    end

    if not matchmade() then
        for _, item in ipairs(roster_entries()) do
            if item.entry.ghost then remove_player(item.key) end
        end
    end

    -- someone new on a team takes the slot of that team's oldest ghost
    local claiming = {}
    for _, item in ipairs(roster_entries()) do
        if item.entry.awaiting_slot and (item.entry.team == 0 or item.entry.team == 1) then
            table.insert(claiming, item)
        end
    end
    table.sort(claiming, function(a, b) return a.entry.order < b.entry.order end)
    for _, arrival in ipairs(claiming) do
        arrival.entry.awaiting_slot = nil
        local oldest, oldest_key = nil, nil
        for _, item in ipairs(roster_entries()) do
            local e = item.entry
            if e.ghost and e.team == arrival.entry.team and item.key ~= arrival.key
                and (not oldest or e.order < oldest.order) then
                oldest, oldest_key = e, item.key
            end
        end
        if oldest_key then remove_player(oldest_key) end
    end

    in_match = #roster_entries() > 0
end

local function mark_left(data)
    if type(data) ~= "table" then return end
    local pid = tostring(data.PrimaryId or "")
    local name = tostring(data.PlayerName or data.Name or "")
    local key = player_key(pid, name, tonumber(data.Shortcut))
    local p = players[key]
    if not p and is_bot_id(pid) and not tonumber(data.Shortcut) then
        -- no id or shortcut in the event: fall back to a bot with that name
        for _, k in ipairs(player_order) do
            local e = players[k]
            if e and e.is_bot and not e.left and e.name == name then
                key, p = k, e
                break
            end
        end
    end
    if p and not p.test then
        p.left = true
        p.ghost = true
        dlog("PfpOverlayV2: PlayerLeft " .. name .. " (" .. key .. "), kept as ghost")
    end
end

local function spectating()
    local list = roster_entries()
    if not my_id or my_id == "" or #list == 0 then return false end
    for _, item in ipairs(list) do
        local p = item.entry
        if p.raw_pid == my_id and (p.team == 0 or p.team == 1) then return false end
    end
    return true
end

-- same order RL's own scoreboard uses: team, then score descending,
-- then shortcut id descending
local function sorted_roster()
    local sorted = {}
    for _, item in ipairs(roster_entries()) do table.insert(sorted, item.entry) end
    local function team_rank(t) return t == 0 and 0 or (t == 1 and 1 or 2) end
    table.sort(sorted, function(a, b)
        if a.team ~= b.team then return team_rank(a.team) < team_rank(b.team) end
        if a.score ~= b.score then return a.score > b.score end
        if a.shortcut and b.shortcut and a.shortcut ~= b.shortcut then
            return a.shortcut > b.shortcut
        end
        if a.raw_pid ~= b.raw_pid then return a.raw_pid > b.raw_pid end
        return a.order < b.order
    end)
    return sorted
end

-- ==========================================
-- Callbacks
-- ==========================================

local avatar_assets = {}

local function refresh_avatar_assets()
    avatar_assets = hebnix.list_assets() or {}
end

function plugin.on_load()
    hebnix.log("PfpOverlayV2 loaded")
    hebnix.refresh_action_binds()
    refresh_avatar_assets()
    reupload_pending = true
end

function plugin.on_game_event(event_type, event)
    local data = event and event.data or {}
    local guid = event and (event.match_guid or event.MatchGuid)
    if guid and guid ~= "" and guid ~= match_guid then
        match_guid = guid
        reset_roster()
    end

    if event_type == "UpdateState" then
        update_players(data)
    elseif event_type == "PlayerLeft" then
        mark_left(data)
    elseif event_type == "GoalScored" then
        local scorer_name = data.Scorer and data.Scorer.Name or ""
        local scorer_key = nil
        local scorer = type(data.Scorer) == "table" and data.Scorer or {}
        local scorer_pid = tostring(scorer.PrimaryId or "")
        local scorer_sc = tonumber(scorer.Shortcut)
        local scorer_team = tonumber(scorer.TeamNum)
        -- platform id first, then the per-match shortcut, name last (names
        -- repeat between players, so it also has to agree on team)
        if scorer_pid ~= "" and players[scorer_pid] then scorer_key = scorer_pid end
        if not scorer_key and scorer_sc then
            for _, key in ipairs(player_order) do
                if players[key].shortcut == scorer_sc then
                    scorer_key = key
                    break
                end
            end
        end
        if not scorer_key then
            local fallback = nil
            for _, key in ipairs(player_order) do
                local e = players[key]
                if e.name == scorer_name then
                    if scorer_team == nil or e.team == scorer_team then
                        scorer_key = key
                        break
                    end
                    fallback = fallback or key
                end
            end
            scorer_key = scorer_key or fallback
        end
        last_goal = { scorer_name = scorer_name, scorer_key = scorer_key, timestamp = os.time() }
        dlog("PfpOverlayV2: GoalScored by " .. scorer_name .. " (matched key: " .. tostring(scorer_key) .. ")")
    elseif event_type == "MatchCreated" or event_type == "MatchInitialized" then
        in_match = true
        match_ended = false
        current_playlist = nil
        offline = false
        mutators = {}
        mutator_count = 0
        match_log_key = nil
        reset_roster()
        first_tab_pending = true
        in_first_open = false
    elseif event_type == "RoundStarted" or event_type == "CountdownBegin" then
        in_match = true
        match_ended = false
    elseif event_type == "GoalReplayStart" then
        is_replay = true
    elseif event_type == "GoalReplayEnd" then
        is_replay = false
    elseif event_type == "MatchEnded" then
        match_ended = true
    elseif event_type == "GameLeft" or event_type == "MatchDestroyed" then
        match_ended = false
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

-- fade the scoreboard out instead of hard-cutting it when the button's
-- released, same idea as ingame_rank's vanish animation
local SCOREBOARD_FADE_SECONDS = 0.3
local scoreboard_fade_was_held = false
local scoreboard_fade_from = nil

-- 1.0 while held, ramps down to 0 over SCOREBOARD_FADE_SECONDS after
-- release, nil once the fade's finished (or it was never shown yet) -
-- callers use nil to mean "don't draw". scoreboard_fade_was_held is its
-- own flag, separate from scoreboard_was_held above (that one drives the
-- first-tab-open detection in on_tick, this one just tracks the fade).
local function scoreboard_fade_opacity()
    if scoreboard_held then
        scoreboard_fade_was_held = true
        scoreboard_fade_from = nil
        return 1.0
    end
    if scoreboard_fade_was_held then
        scoreboard_fade_was_held = false
        scoreboard_fade_from = hebnix.monotonic_seconds()
    end
    if not scoreboard_fade_from then return nil end
    local elapsed = hebnix.monotonic_seconds() - scoreboard_fade_from
    if elapsed >= SCOREBOARD_FADE_SECONDS then
        scoreboard_fade_from = nil
        return nil
    end
    return 1.0 - elapsed / SCOREBOARD_FADE_SECONDS
end

-- bumped each time "use own id" forces a fresh value into the override
-- id text_input (see on_settings)
local override_id_gen = 0

function plugin.on_tick()
    poll_tracker_results()
    refresh_local_identity()
    reupload_last_avatar()
    refresh_detected_ui_scale()
    flush_epic_lookups()

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

    if hebnix.get_bool("scoreboard_auto_detect", true) then
        scoreboard_held = hebnix.is_action_pressed("togglescoreboard")
    else
        local sb_bind = hebnix.get_string("scoreboard_button", "")
        scoreboard_held = sb_bind ~= "" and hebnix.is_bind_pressed(sb_bind)
    end

    if scoreboard_capturing_bind then
        local status, bind_result = hebnix.capture_bind_result()
        if status == "done" then
            hebnix.set("scoreboard_button", bind_result)
            scoreboard_capturing_bind = false
        elseif status == "timeout" then
            scoreboard_capturing_bind = false
        end
    end

    if in_match and not current_playlist then refresh_match_playlist() end

    if in_match then
        if scoreboard_held and not scoreboard_was_held then
            in_first_open = first_tab_pending
            first_tab_pending = false
        elseif not scoreboard_held then
            in_first_open = false
        end
        scoreboard_was_held = scoreboard_held
    end
end

-- what the scoreboard pass did last frame, for the debug overlay
local sb_gate = "not drawn yet"
local sb_screen = nil

-- Avatars are drawn over a black box the same size as the slot, so
-- transparent pixels (PNGs with alpha, round PSN avatars, GIF gaps) show
-- black instead of the scoreboard behind them.
local function draw_avatar(draw, path, x, y, w, h, alpha)
    alpha = alpha or 1
    local a = math.max(0, math.min(255, math.floor(255 * alpha + 0.5)))
    draw.rect(x, y, w, h, { color = string.format("#000000%02x", a), filled = true })
    if alpha < 1 then
        draw.image(path, x, y, w, h, { opacity = alpha })
    else
        draw.image(path, x, y, w, h)
    end
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
    -- everything draw_scoreboard_avatars checks before drawing
    draw.text(AVATAR_START_X, AVATAR_START_Y - 18,
        "in_match=" .. tostring(in_match) .. "  match_ended=" .. tostring(match_ended)
            .. "  freeplay=" .. tostring(freeplay) .. "  scoreboard_held=" .. tostring(scoreboard_held)
            .. "  playlist=" .. tostring(current_playlist) .. "  ui_scale=" .. tostring(ui_scale())
            .. "  screen=" .. (sb_screen or "?") .. "  scoreboard: " .. sb_gate,
        { color = "#00ff00ff", size = 16 })
    for _, pid in ipairs(player_order) do
        local p = players[pid]
        local tag = hebnix.platform_tag(p.raw_pid or pid)
        if p.avatar_path then
            pcall(function()
                draw_avatar(draw, p.avatar_path, AVATAR_START_X, y, AVATAR_SIZE, AVATAR_SIZE)
            end)
        end
        local team_label = p.team == 1 and "orange" or (p.team == 0 and "blue" or "none")
        local disc_tag = p.ghost and " [ghost]" or ""
        draw.text(AVATAR_START_X + AVATAR_SIZE + 8, y + AVATAR_SIZE / 2 - 8,
            tag .. " " .. p.name .. "  [" .. team_label .. " " .. tostring(p.score) .. "]" .. disc_tag ..
                "  —  " .. p.status,
            { color = "#ffffffff", size = 14 })
        if not p.test then
            draw.text(AVATAR_START_X + AVATAR_SIZE + 8, y + AVATAR_SIZE / 2 + 8,
                "scoreboard: " .. (p.sb_info or "not drawn"),
                { color = "#ffff00ff", size = 14 })
        end
        y = y + AVATAR_SIZE + AVATAR_GAP
    end
end

local last_layout = nil

local function last_layout_readout()
    if not last_layout then return "No frame drawn yet, hold the scoreboard once." end
    return string.format(
        "%dv%d, %d ghost, %d listed, 1 unit = %.2f px, pfp %.0f px, blue row1 %.0f%s%s",
        last_layout.blues or 0, last_layout.oranges or 0,
        last_layout.ghosts or 0, #roster_entries(),
        last_layout.scale, last_layout.size, last_layout.blue_y,
        last_layout.first_open and " (first open)" or "",
        last_layout.replay and " (replay)" or "")
        .. (last_layout.watching and " (spectating)" or "")
end

-- same drawing as the PlatformDisplay plugin, with each player's pfp in
-- place of the platform icon
-- soft dark edge around a pfp, built from a few thin rings that fade toward
-- the middle so it also bleeds a little way into the image itself
local VIGNETTE_RINGS = 8
local VIGNETTE_DEPTH = 0.3 -- of the pfp size

local function draw_vignette(draw, x, y, size, strength, alpha)
    local depth = size * VIGNETTE_DEPTH
    local thick = depth / VIGNETTE_RINGS
    for i = 0, VIGNETTE_RINGS - 1 do
        local fade = (1 - i / VIGNETTE_RINGS) ^ 1.5
        local a = math.floor(255 * strength * fade * alpha + 0.5)
        if a > 0 then
            local inset = i * thick + thick / 2
            draw.rect(x + inset, y + inset, size - inset * 2, size - inset * 2,
                { color = string.format("#000000%02x", math.min(a, 255)), border = thick + 0.5 })
        end
    end
end

local function draw_scoreboard_avatars(draw, w, h)
    sb_screen = string.format("%dx%d", w, h)
    for _, key in ipairs(player_order) do players[key].sb_info = nil end
    if not in_match then sb_gate = "skipped, not in match" return end
    if match_ended then sb_gate = "skipped, match ended" return end
    if freeplay then sb_gate = "skipped, freeplay" return end
    local list = sorted_roster()
    if #list == 0 then sb_gate = "skipped, empty roster" return end

    if scoreboard_held then shown_first_open = in_first_open end
    local opacity = scoreboard_fade_opacity()
    if not opacity then sb_gate = "hidden, scoreboard not held" return end
    sb_gate = "drawing"

    local hide_self = hebnix.get_bool("scoreboard_hide_self", false)
    local show_ghosts = hebnix.get_bool("scoreboard_show_ghosts", true)
    local vignette = hebnix.get_bool("scoreboard_vignette", false)
        and hebnix.get_number("scoreboard_vignette_strength", 60) / 100 or 0
    local scale_mult = ui_scale() * hebnix.get_number("scoreboard_display_scale", 100) / 100

    local blues, oranges, ghosts = 0, 0, 0
    for _, p in ipairs(list) do
        if p.team == 0 then
            blues = blues + 1
        elseif p.team == 1 then
            oranges = oranges + 1
        end
        if p.ghost then ghosts = ghosts + 1 end
    end

    local mutator_edge = shows_mutator_strip()
        and hebnix.get_number("scoreboard_mutator_edge", MUTATOR_EDGE) or 0
    local x_offset = shown_first_open
        and hebnix.get_number("scoreboard_x_offset_first", X_OFFSET_FIRST)
        or hebnix.get_number("scoreboard_x_offset", X_OFFSET)
    local replay_shift = is_replay
        and hebnix.get_number("scoreboard_replay_shift", REPLAY_SHIFT) or 0
    local layout = sb_layout(w, h, scale_mult, x_offset, mutator_edge, blues, oranges, replay_shift)
    local y_nudge = hebnix.get_number("scoreboard_y_nudge", 0) * layout.scale
    local pfp_x = layout.centre
        + hebnix.get_number("scoreboard_icon_x", ICON_COL) * layout.scale
    layout.first_open = shown_first_open
    layout.replay = is_replay
    layout.blues = blues
    layout.oranges = oranges
    layout.ghosts = ghosts
    layout.watching = spectating()
    last_layout = layout

    local blue_row, orange_row = -1, -1
    for _, p in ipairs(list) do
        if p.team == 0 then
            blue_row = blue_row + 1
        elseif p.team == 1 then
            orange_row = orange_row + 1
        else
            p.sb_info = "skipped, no team"
            goto continue
        end
        if p.is_bot then
            p.sb_info = "skipped, bot"
        elseif not p.avatar_path then
            p.sb_info = "skipped, no avatar yet"
        elseif p.ghost and not show_ghosts then
            p.sb_info = "skipped, ghost hidden"
        elseif hide_self and my_id ~= nil and p.raw_pid == my_id then
            p.sb_info = "skipped, own pfp hidden"
        end
        if not p.is_bot and p.avatar_path
            and (show_ghosts or not p.ghost)
            and not (hide_self and my_id ~= nil and p.raw_pid == my_id) then
            local y = y_nudge + (p.team == 0
                and layout.blue_y + layout.separation * blue_row
                or layout.orange_y + layout.separation * orange_row)
            local alpha = p.ghost and opacity * GHOST_OPACITY or opacity
            local ok, err = pcall(function()
                draw_avatar(draw, p.avatar_path, pfp_x, y, layout.size, layout.size, alpha)
                if vignette > 0 then
                    draw_vignette(draw, pfp_x, y, layout.size, vignette, alpha)
                end
            end)
            p.sb_info = string.format("x=%.0f y=%.0f size=%.0f alpha=%.2f%s",
                pfp_x, y, layout.size, alpha, ok and "" or ("  draw failed: " .. tostring(err)))
        end
        ::continue::
    end
end

local function draw_goal_nameplate(draw, w, h)
    if not last_goal then return end
    -- stays up for the whole replay and goes away when it ends or is skipped
    -- (GoalReplayEnd), replays aren't a fixed length
    local elapsed = os.time() - last_goal.timestamp
    if not (is_replay and elapsed >= GOAL_NAMEPLATE_DELAY_SECONDS) then return end

    local p = last_goal.scorer_key and players[last_goal.scorer_key]
    if not p or not p.avatar_path then return end

    local x, y, w2, h2 = get_goal_nameplate_slot(w, h)
    pcall(function()
        draw_avatar(draw, p.avatar_path, x, y, w2, h2)
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
    ui.checkbox("debug_logs", "debug logs (spams the console, off by default)", false)
    ui.space(6)
    ui.heading("Interface Scale")
    ui.label("Must match RL's own options > video > interface scale, or the")
    ui.label("profiles will render in the wrong spot.")
    local ui_scale_auto_detect = ui.checkbox("ui_scale_auto_detect",
        "auto-detect from RL's own save file (recommended)", true)
    if ui_scale_auto_detect then
        hebnix.set("ui_scale_autodisabled", false)
        if detected_ui_scale then
            ui.label(string.format("detected: %g%%", math.floor(detected_ui_scale * 1000 + 0.5) / 10))
        else
            ui.colored_label("#d35400", "couldn't read interface scale from RL's save file yet.")
        end
    else
        if hebnix.get_bool("ui_scale_autodisabled", false) then
            ui.colored_label("#d35400",
                "couldn't read RL's save data, so auto-detect was turned off and the scale set to 100%.")
            ui.colored_label("#d35400",
                "set the percentage below to match RL, or tick auto-detect to try again.")
        end
        if hebnix.get_string("rl_ui_scale_percent", "") == "" then
            local old = tonumber(hebnix.get_string("rl_ui_scale", ""))
            hebnix.set("rl_ui_scale_percent",
                string.format("%g", math.floor(((old and old > 0) and old or REFERENCE_UI_SCALE) * 1000 + 0.5) / 10))
        end
        ui.text_input("rl_ui_scale_percent", "RL interface scale (%)", "75")
        ui.label("type a percentage like RL's slider: 75 = 75%, 100 = 100% (not 0.75 / 1.0).")
        local pct = tonumber((hebnix.get_string("rl_ui_scale_percent", ""):gsub("%s*%%%s*$", "")))
        if not pct or pct <= 0 then
            ui.colored_label("#c0392b", "not a valid percentage, using 75%.")
        elseif pct < 50 or pct > 100 then
            ui.colored_label("#d35400", "RL's interface scale only goes from 50% to 100% - double check this.")
        end
    end

    ui.space(8)
    ui.heading("Scoreboard button")
    ui.label("Avatars only render at scoreboard positions while this is held.")
    local auto_detect = ui.checkbox("scoreboard_auto_detect",
        "auto-detect from RL's own bindings (recommended)", true)
    if auto_detect then
        local detected = hebnix.get_action_binds("togglescoreboard")
        if #detected == 0 then
            ui.colored_label("#d35400", "no scoreboard bind found in your RL settings yet.")
        else
            ui.horizontal(function()
                ui.label("detected:")
                for _, b in ipairs(detected) do
                    ui.bind_icon(b, {height = 20})
                    ui.label(b .. " " .. hebnix.bind_type_label(b))
                end
            end)
        end
        if ui.button("refresh from RL settings") then
            hebnix.refresh_action_binds()
        end
    else
        ui.label("bind whatever shows RL's scoreboard (hold tab on keyboard,")
        ui.label("view/select on controller).")
        local sb_bind = hebnix.get_string("scoreboard_button", "")
        ui.horizontal(function()
            ui.label("bind: " .. (sb_bind ~= "" and sb_bind or "(none set)"))
            if sb_bind ~= "" then
                ui.bind_icon(sb_bind, {height = 20})
                ui.label(hebnix.bind_type_label(sb_bind))
            end
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
    end

    ui.space(4)
    ui.checkbox("scoreboard_show_ghosts", "Show players who left, dimmed", true)
    ui.checkbox("scoreboard_hide_self", "Hide my own pfp", false)
    ui.checkbox("scoreboard_vignette", "Vignette around pfps (dark soft edge)", false)
    if hebnix.get_bool("scoreboard_vignette", false) then
        ui.slider("scoreboard_vignette_strength", "Vignette strength", 10, 100, 60)
    end

    ui.space(12)
    ui.heading("Epic Avatar Uploading")
    ui.label("Choose an image from this plugin's assets folder and upload it")
    ui.label("as the avatar for the Epic account currently running Rocket League.")
    if local_epic_id then
        ui.label("Detected Epic ID: " .. local_epic_id)
    else
        ui.colored_label("#aaaaaa",
            "No Epic ID detected - uploading is only available while playing on Epic.")
    end
    local cdn_upload_asset = ui.combo_box("cdn_upload_asset", "Avatar image", avatar_assets)
    ui.horizontal(function()
        if ui.button("Open Assets Folder") then
            hebnix.open_path(PLUGIN_DIR .. "/assets")
        end
        if ui.button("Refresh Assets Folder") then
            refresh_avatar_assets()
        end
    end)
    if #avatar_assets == 0 then
        ui.colored_label("#aaaaaa", "No assets found. Add an image, then refresh the list.")
    end
    if local_epic_id and cdn_upload_asset and cdn_upload_asset ~= ""
        and ui.button("Upload Epic Avatar") then
        local path = cdn_upload_asset
        if not path:match("^assets[\\/]") then path = "assets/" .. path end
        plugin.upload_profile_image(path)
    end
    if local_epic_id and upload_status[local_epic_id] then
        ui.label("Status: " .. upload_status[local_epic_id])
    end

    ui.space(12)
    ui.collapsing("Overrides", function(ui)
    ui.label("Force a specific image for one player, by platform + id. handy")
    ui.label("for anyone hebnix doesn't have a picture for. Drop the image")
    ui.label("in the assets folder, refresh the list, then pick it below.")

    ui.space(4)
    local override_platform = ui.combo_box("override_add_platform", "platform",
        { "steam", "xboxone", "epic", "psn" })

    -- ui.text_input's displayed value lives on the host side, keyed by
    -- name - the only way to force a fresh value into it from lua is to
    -- give it a key it hasn't seen before, which pulls the value we just
    -- wrote instead of whatever the widget already has buffered.
    local override_id_key = "override_add_id_" .. tostring(override_id_gen)
    local override_id = ui.text_input(override_id_key, "player id (steamid64 / gamertag / etc)")
    ui.horizontal(function()
        if override_platform == "epic" and local_epic_id then
            if ui.button("use own id") then
                override_id_gen = override_id_gen + 1
                hebnix.set("override_add_id_" .. tostring(override_id_gen), local_epic_id)
            end
        end
    end)
    local override_asset = ui.combo_box("override_add_asset", "Avatar image", avatar_assets)
    ui.horizontal(function()
        if ui.button("Open Assets Folder") then
            hebnix.open_path(PLUGIN_DIR .. "/assets")
        end
        if ui.button("Refresh Assets Folder") then
            refresh_avatar_assets()
        end
    end)
    if #avatar_assets == 0 then
        ui.colored_label("#aaaaaa", "No assets found. Add an image, then refresh the list.")
    end

    if ui.button("add / update override") then
        local id = override_id:match("^%s*(.-)%s*$")
        local path = (override_asset or ""):match("^%s*(.-)%s*$")
        if path ~= "" and not path:match("^assets[\\/]") then path = "assets/" .. path end
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
    ui.heading("Data Sources")
    ui.label("Per platform, pick hebnix (default, no key needed) or")
    ui.label("manual - this plugin's own key/psn-login pipeline from v1. if")
    ui.label("manual is picked but nothing's filled in, it just falls back")
    ui.label("to hebnix for that platform.")

    ui.space(4)
    ui.horizontal(function()
        ui.combo_box("priority_steam", "steam priority", { "hebnix", "manual" })
        ui.text_input("steam_api_key", "steam web api key (steamcommunity.com/dev)", "")
    end)
    ui.horizontal(function()
        ui.combo_box("priority_xboxone", "xbox priority", { "hebnix", "manual" })
        ui.text_input("xbox_api_key", "xbox api key (xbl.io)", "")
    end)
    ui.horizontal(function()
        ui.combo_box("priority_psn", "psn priority", { "hebnix", "manual" })
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

    end)

    ui.space(12)
    ui.collapsing("Testing", function(ui)
    ui.label("Pick a platform + fetch method, type an id, and try it - also")
    ui.label("adds a temporary entry to the tracked players list below.")
    local test_platform = ui.combo_box("test_platform", "platform",
        { "steam", "xboxone", "epic", "psn" })
    -- epic has no manual method and no direct cdn option here - "hebnix"
    -- already falls through to the cdn lookup automatically for epic,
    -- same as the real (non-test) fetch path does.
    local test_mode = ui.combo_box("test_mode", "fetch via", { "hebnix", "manual" })
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
                    test = true, team = -1, score = 0,
                }
                table.insert(player_order, key)
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
                    p.status = "fetching (hebnix)"
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

    end)

    ui.space(12)
    ui.collapsing("Debugging", function(ui)
    ui.heading("Tracked Players")
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
            local disc_tag = p.ghost and " [ghost]" or ""
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
    ui.colored_label("#aaaaaa", "Debug overlay")
    ui.label("An on-screen list of tracked players and their fetch status,")
    ui.label("for troubleshooting - not the scoreboard pfps themselves.")
    local bind = hebnix.get_string("overlay_toggle_bind", "")
    ui.horizontal(function()
        ui.label("toggle bind: " .. (bind ~= "" and bind or "(none — always visible)"))
        if bind ~= "" then
            ui.bind_icon(bind, {height = 20})
            ui.label(hebnix.bind_type_label(bind))
        end
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

    -- ==========================================
    -- advanced alignment - nudges for cases the built-in layout doesn't
    -- cover on its own. down here since it's rarely touched once set.
    -- ==========================================

    ui.space(12)
    ui.colored_label("#aaaaaa", "Advanced alignment")
    ui.label("Same sliders as the PlatformDisplay plugin.")
    ui.label("Mutators: " .. mutator_count
        .. (#mutators > 0 and " (" .. table.concat(mutators, ", ") .. ")" or "")
        .. (shows_mutator_strip() and ", board shifted" or ""))
    ui.label("Display scale has no home in the save, match it to the game.")
    ui.slider("scoreboard_display_scale", "Display scale", 90, 100, 100)
    ui.label("Below are 1080p pixels, they scale with the resolution.")
    ui.label("Set X offset in a plain match first, the strip edge only bites when it overlaps.")
    ui.slider("scoreboard_x_offset", "X offset", -250, 250, X_OFFSET)
    ui.slider("scoreboard_x_offset_first", "X offset, first tab", -250, 250, X_OFFSET_FIRST)
    ui.slider("scoreboard_mutator_edge", "Mutator strip edge", 800, 1300, MUTATOR_EDGE)
    ui.slider("scoreboard_replay_shift", "Replay board shift", -250, 250, REPLAY_SHIFT)
    ui.slider("scoreboard_icon_x", "Pfp column", -700, -300, ICON_COL)
    ui.slider("scoreboard_y_nudge", "Y nudge", -100, 100, 0)
    ui.label(last_layout_readout())
    ui.slider("goal_nameplate_x_nudge", "Goal replay nameplate X nudge", -250, 250, 0)
    ui.slider("goal_nameplate_y_nudge", "Goal replay nameplate Y nudge", -250, 250, 0)
    end)
end

function plugin.on_unload()
    dlog("PfpOverlayV2 unloaded")
end

return plugin
