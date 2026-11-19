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

local function remember_retry(desync)
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

local function retry_tls(desync, state)
    local tcp = desync.dis.tcp
    local restore = desync.arg.restore_window == "1" and state.googlevideo_window_factor
    if not desync.outgoing then
        if restore and bitand(tcp.th_flags, TH_SYN + TH_ACK) == TH_SYN + TH_ACK then
            state.googlevideo_peer_scale = find_tcp_option(tcp.options, TCP_KIND_SCALE) ~= nil
        end
        return
    end
    if bitand(tcp.th_flags, TH_ACK) == 0 or not (restore or state.googlevideo_trial) then return end

    if not state.googlevideo_tls_progress then
        if desync.l7payload == "tls_client_hello" then
            state.googlevideo_hello_ack = state.googlevideo_hello_ack or tcp.th_ack
            return
        end
        local ack = state.googlevideo_hello_ack
        local record = (desync.dis.payload or ""):byte()
        if not ack or not record or record == 21 or not seq_after(tcp.th_ack, ack) then return end

        -- The client acknowledged server data and sent its next TLS flight.
        state.googlevideo_tls_progress = true
        if state.googlevideo_trial then
            state.googlevideo_trial = false
            remember_retry(desync)
        end
    end

    if restore and state.googlevideo_peer_scale then
        local window = math.min(65535, tcp.th_win * restore)
        if window ~= tcp.th_win then
            tcp.th_win = window
            return VERDICT_MODIFY
        end
    end
end

function googlevideo_watch_tls(_, desync)
    if not desync.track or not desync.dis.tcp then return end
    local state = desync.track.lua_state
    if state.googlevideo_watch == false then return end
    local tcp = desync.dis.tcp
    if state.googlevideo_retry then
        -- A client can still receive data after sending FIN.
        if bitand(tcp.th_flags, TH_RST) == 0 then return retry_tls(desync, state) end
        return
    end

    local has_data = #(desync.dis.payload or "") > 0
    local closed = bitand(tcp.th_flags, TH_FIN + TH_RST) ~= 0
    local watch = state.googlevideo_watch
    if closed or (not desync.outgoing and has_data) then
        if watch then timer_del(watch.timer) end
        state.googlevideo_watch = false
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
    local tcp = desync.dis.tcp
    if state.googlevideo_retry == nil and bitand(tcp.th_flags, TH_SYN + TH_ACK) == TH_SYN then
        local ip = host_ip(desync)
        state.googlevideo_trial = not remembered[ip] and take_retry(ip)
        state.googlevideo_retry = remembered[ip] or state.googlevideo_trial
        if state.googlevideo_retry then
            local scale = tonumber(desync.arg.scale)
            local option = scale and find_tcp_option(tcp.options, TCP_KIND_SCALE)
            if option then
                local original_scale = math.min(u8(tcp.options[option].data), 14)
                if original_scale > scale then
                    state.googlevideo_window_factor = 2 ^ (original_scale - scale)
                end
            end
        end
    end
    if state.googlevideo_retry then return wssize(ctx, desync) end
end

function googlevideo_watch_quic(ctx, desync)
    if not desync.track or not desync.dis.ip or not desync.dis.udp then return end
    local state = desync.track.lua_state
    local status = state.googlevideo_quic
    local initial = desync.l7payload == "quic_initial"
    if status == "rejected" then
        if desync.outgoing and initial then
            execution_plan_cancel(ctx)
            return VERDICT_DROP
        end
        return
    end

    if not desync.outgoing then
        if status then timer_del(status) end
        state.googlevideo_quic = false
        return
    end
    if status ~= nil or not initial or not replay_first(desync) then return end

    local raw = raw_packet(ctx)
    if not raw or #raw < 28 then return end
    -- Quote the post-NAT packet for conntrack, within the 576-byte IPv4 ICMP limit.
    local reply = {
        ip = deepcopy(desync.dis.ip),
        icmp = {icmp_type = 3, icmp_code = 3, icmp_data = 0},
        payload = raw:sub(1, 576 - 20 - 8),
    }
    reply.ip.ip_src, reply.ip.ip_dst = reply.ip.ip_dst, reply.ip.ip_src
    fix_ip_proto(reply)
    local timer = desync_timer_name(desync)
    local ifin = desync.ifin
    state.googlevideo_quic = timer
    timer_set(timer, function()
        state.googlevideo_quic = rawsend_dissect(reply, {ifout = ifin}) and "rejected" or false
    end, 1000, true)
end
