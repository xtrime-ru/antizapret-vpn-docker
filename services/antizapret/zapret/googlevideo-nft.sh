# Loaded by zapret2 after its standard nftables rules.
zapret_custom_firewall_nft()
{
    [ "$NFQWS2_ENABLE" = 1 ] && [ "$DISABLE_IPV4" != 1 ] && is_postnat || return 0
    case "$NFQWS2_OPT" in
        *--lua-desync=googlevideo_watch_tls:*:restore_window=1*)
            ;;
        *) return 0 ;;
    esac

    local mark=${GOOGLEVIDEO_WINDOW_MARK:-0x10000000}
    local packets=$NFQWS2_TCP_PKT_OUT
    # Classify after NFQUEUE has modified the SYN. The built-in queue handles
    # the first packets, this chain only sends the later ACKs of marked flows.
    nft -f - <<EOF
add table inet antizapret_googlevideo
flush table inet antizapret_googlevideo
add chain inet antizapret_googlevideo postnat { type filter hook postrouting priority srcnat + 2; policy accept; }
add rule inet antizapret_googlevideo postnat meta nfproto ipv4 ct direction original meta mark & $DESYNC_MARK == 0 meta mark & $DESYNC_MARK_POSTNAT != 0 tcp dport 443 tcp flags & (syn | ack) == syn tcp window 1 ct mark set ct mark | $mark counter
add rule inet antizapret_googlevideo postnat meta nfproto ipv4 ct direction original meta mark & $DESYNC_MARK == 0 ct mark & $mark != 0 ct original packets > $packets tcp dport 443 tcp flags & (syn | rst | ack) == ack meta mark set meta mark | $DESYNC_MARK_POSTNAT counter queue flags bypass to $QNUM
EOF
}

zapret_custom_firewall_nft_flush()
{
    nft delete table inet antizapret_googlevideo 2>/dev/null || true
}
