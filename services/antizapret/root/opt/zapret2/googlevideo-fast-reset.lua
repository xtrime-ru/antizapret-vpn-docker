function googlevideo_fast_reset(ctx, desync)
    if not desync.outgoing or not desync.dis.tcp or not desync.track then return end
    local state = desync.track.lua_state
    if bitand(desync.dis.tcp.th_flags, TH_SYN) ~= 0 then state.googlevideo_syn = true end
    if state.googlevideo_syn or desync.l7payload ~= 'tls_client_hello' then return end
    if not replay_first(desync) then return VERDICT_DROP end

    DLOG('googlevideo_fast_reset: cold connection, sending RST to both endpoints')
    local reset = deepcopy(desync.dis)
    reset.payload, reset.tcp.options, reset.tcp.th_flags = nil, nil, TH_RST
    rawsend_dissect(reset, {ifout = desync.ifout})
    dis_reverse(reset)
    rawsend_dissect(reset, {ifout = desync.ifin})
    return VERDICT_DROP
end
