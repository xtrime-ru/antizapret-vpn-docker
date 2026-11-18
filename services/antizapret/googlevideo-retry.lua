local retries = {}
local remembered = {}

local function seq_after(a, b)
    local delta = (a - b) % 0x100000000
    return delta > 0 and delta < 0x80000000
end

local function remove_retry(timer, ip)
    local pending = retries[ip]
    pending[timer] = nil
    if not next(pending) then retries[ip] = nil end
end

local function allow_retry(flow)
    local timer = "googlevideo-retry:" .. flow.timer
    local pending = retries[flow.ip] or {}
    pending[timer] = true
    retries[flow.ip] = pending
    timer_set(timer, remove_retry, 15000, true, flow.ip)
end

local function take_retry(ip)
    local pending = retries[ip]
    if not pending then return false end
    local timer = next(pending)
    timer_del(timer)
    remove_retry(timer, ip)
    return true
end

local function forget_ip(_, ip)
    remembered[ip] = nil
end

local function learn_retry(desync, state, ack)
    if desync.l7payload == "tls_client_hello" then
        state.googlevideo_hello_ack = state.googlevideo_hello_ack or ack
        return
    end
    local initial_ack = state.googlevideo_hello_ack
    if not initial_ack or not seq_after(ack, initial_ack) or desync.dis.payload:byte() == 21 then return end

    -- The client has received server data and sent its next TLS flight.
    state.googlevideo_trial = false
    local ip = host_ip(desync)
    local ttl = tonumber(desync.arg.cache_ms) or 86400000
    if remembered[ip] or ttl <= 0 then return end
    timer_set("googlevideo-cache:" .. ip, forget_ip, ttl, true, ip)
    remembered[ip] = true
end

local function reset_stalled_flow(_, flow)
    local reset = flow.reset
    dis_reverse(reset)
    if not rawsend_dissect(reset, {ifout = flow.ifin}) then return end

    allow_retry(flow)
    dis_reverse(reset)
    rawsend_dissect(reset, {ifout = flow.ifout, fwmark = flow.fwmark})
end

local function watch_client_hello(desync)
    local reset = deepcopy(desync.dis)
    reset.payload, reset.tcp.options, reset.tcp.th_flags = nil, nil, TH_RST
    local watch = {
        timer = desync_timer_name(desync), reset = reset,
        ip = host_ip(desync),
        ifin = desync.ifin, ifout = desync.ifout, fwmark = desync.fwmark,
    }
    timer_set(watch.timer, reset_stalled_flow, tonumber(desync.arg.timeout_ms) or 1200, true, watch)
    return watch
end

function googlevideo_watch_tls(_, desync)
    if not desync.track or not desync.dis.tcp then return end
    local state = desync.track.lua_state
    if state.googlevideo_watch == false then return end
    local tcp = desync.dis.tcp
    local closed = bitand(tcp.th_flags, TH_FIN + TH_RST) ~= 0
    local has_data = #(desync.dis.payload or "") > 0
    local watch = state.googlevideo_watch
    if closed or (not desync.outgoing and has_data and not state.googlevideo_retry) then
        if watch then timer_del(watch.timer) end
        state.googlevideo_watch = false
    elseif state.googlevideo_retry then
        if state.googlevideo_trial and desync.outgoing and has_data and bitand(tcp.th_flags, TH_ACK) ~= 0 then
            learn_retry(desync, state, tcp.th_ack)
        end
    elseif watch then
        -- Advance the reset sequence on ACKs, including across the TCP wraparound.
        if not desync.outgoing and bitand(tcp.th_flags, TH_ACK) ~= 0
            and seq_after(tcp.th_ack, watch.reset.tcp.th_seq) then
            watch.reset.tcp.th_seq = tcp.th_ack
        end
    elseif desync.outgoing and desync.l7payload == "tls_client_hello" and replay_first(desync) then
        state.googlevideo_watch = watch_client_hello(desync)
    end
end

function googlevideo_retry_window(ctx, desync)
    if not desync.outgoing or not desync.track or not desync.dis.tcp then return end
    local state = desync.track.lua_state
    if state.googlevideo_retry == nil and bitand(desync.dis.tcp.th_flags, TH_SYN) ~= 0 then
        local ip = host_ip(desync)
        state.googlevideo_trial = not remembered[ip] and take_retry(ip)
        state.googlevideo_retry = remembered[ip] or state.googlevideo_trial
    end
    if state.googlevideo_retry then return wssize(ctx, desync) end
end
