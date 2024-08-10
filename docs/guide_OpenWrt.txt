[OpenWRT](https://openwrt.org/) guide:

1. [Install needed packages](https://openwrt.org/docs/guide-user/services/vpn/openvpn/client-luci#install_needed_packages)
2. [Upload an OpenVPN config file](https://openwrt.org/docs/guide-user/services/vpn/openvpn/client-luci#b_upload_a_openvpn_config_file)

**NOTE:** Change `dev tun` to a dedicated interface name like `dev tun1`.

3. Add the `antizapret` interface to `/etc/config/network`:
    ```
    config interface 'antizapret'
        option proto 'none'
        option device 'tun1'
        option defaultroute '0'
        option delegate '0'
    ```
NOTE: tun1 - your TUN interface from [Step 2].

4. Add `antizapret` firewall zones configuration to `/etc/config/firewall`:
    ```
    config zone
        option name 'antizapret'
        option input 'REJECT'
        option output 'ACCEPT'
        option forward 'ACCEPT'
        list network 'antizapret'
        option mtu_fix '1'
        option masq '1'

    config forwarding
        option src 'lan'
        option dest 'antizapret'

    config forwarding
        option src 'antizapret'
        option dest 'wan'
    ```

5. Add the DNS intercept rule to `/etc/config/firewall`:
    ```
    config redirect 'dns_int'
        option name 'Intercept-DNS'
        option proto 'tcp udp'
        option src 'lan'
        option src_dport '53'
        option src_ip '!192.168.4.3'
        option target 'DNAT'
        option dest_ip '192.168.4.3'
        option dest 'lan'
    ```

**NOTE:** `192.168.4.3` is your DNS server IP address.

It may be a router address if you don't have any dedicated DNS resolvers (something like PiHole) in your network.

6. Add the DoT disable rule to `/etc/config/firewall`:
    ```
    config rule 'dot_deny'
        option name 'Deny-DoT'
        option src 'lan'
        option dest 'wan'
        option dest_port '853'
        option proto 'tcp udp'
        option target 'REJECT'
        option enabled '0'
    ```
NOTE: Those 2 rules may make your clients angry, because this is a real DNS hijack.

But it's on good purpose for now)

See the obsolete [DNS hijacking](https://openwrt.org/docs/guide-user/firewall/fw3_configurations/intercept_dns#dns_hijacking) wiki for details.

7. Add the IPv6 rule to `/etc/config/firewall`:
    ```
    config rule
        option name 'Reject-IPv6'
        option family 'ipv6'
        list proto 'all'
        option src '*'
        option dest '*'
        option target 'REJECT'
    ```
NOTE: This should completely disable IPv6 traffic forwarding to prevent DNS and traffic leaks because [the current solution](https://github.com/xtrime-ru/antizapret-vpn-docker) does not support IPv6.

Feel free to disable IPv6 on individual interfaces too.

8. Disable DNS Rebind Protection in `/etc/config/dhcp`:
    ```
    config dnsmasq
        option rebind_protection '0'
    ```

9. Configure your upstream resolver to use antizapret DNS:

    * **Bare OpenWRT:**
        Set server in `/etc/config/dhcp`:
        ```
        config dnsmasq
            list server '192.168.100.1'
        ```

    * **PiHole:**
        CLI: Set server in `/etc/pihole/setupVars.conf`:
        ```
        PIHOLE_DNS_1=192.168.100.1
        ```
		Make sure there are no other PIHOLE_DNS_* records

        GUI: Go to [PiHole Admin](http://192.168.4.3/admin/settings.php?tab=dns), disable all predefined "Upstream DNS Servers," and set "Custom 1 (IPv4)" to `192.168.100.1`.

    * **Other resolvers:**
        Check your resolver manual.
