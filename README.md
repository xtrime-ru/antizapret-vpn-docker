[English](README.md) | [Русский](README_RU.md)

# AntiZapret VPN in Docker

Antizapret created to redirect only blocked domains to VPN tunnel. Its called split tunneling.
This repo is based on idea from original [AntiZapret LXD image](https://bitbucket.org/anticensority/antizapret-vpn-container/src/master/)

## Table of contents

- [Support and discussions group](#support-and-discussions-group)
- [Features](#features)
- [How it works](#how-it-works)
- [Installation](#installation)
  - [Single Server (Easy)](#single-server-easy)
  - [Docker Swarm, multiple exit nodes (Advanced)](#docker-swarm-multiple-exit-nodes-advanced)
  - [VPN / Hosting block](#vpn--hosting-block)
  - [After installation](#after-installation)
  - [Access admin panels](#access-admin-panels)
    - [HTTPS](#https)
    - [Local network](#local-network)
    - [HTTP](#http)
  - [Update](#update)
    - [Upgrade from v5](#upgrade-from-v5)
  - [Reset](#reset)
- [Documentation](#documentation)
  - [FAQ (Frequently Asked Questions)](#faq-frequently-asked-questions)-
  - [DNS resolving algorithm](#dns-resolving-algorithm)
  - [Adding Domains](#adding-domains)
    - [Adding Domains via rules](#adding-domains-via-rules)
    - [Adding Domains via lists](#adding-domains-via-lists)
    - [Routing a website through VPN for a specific client](#routing-a-website-through-vpn-for-a-specific-client)
  - [Adding IPs/Subnets](#adding-ipssubnets)
  - [SOCKS5 and HTTP(S) Proxy (per-application routing)](#socks5-and-https-proxy-per-application-routing)
    - [How it works](#how-it-works-1)
    - [How to disable HTTPS access from the internet](#how-to-disable-https-access-from-the-internet)
    - [When to use proxy instead of DNS-based routing](#when-to-use-proxy-instead-of-dns-based-routing)
    - [Configuration](#configuration)
    - [Client setup](#client-setup)
    - [Example use cases](#example-use-cases)
  - [HTTP(S) Proxy](#https-proxy)
  - [Using zapret2](#zapret2)
    - [Changing configuration](#changing-configuration)
    - [Strategy selection](#strategy-selection)
  - [Environment Variables](#environment-variables)
  - [DNS](#dns)
    - [Adguard Upstream DNS](#adguard-upstream-dns)
    - [CDN + ECS](#cdn--ecs)
  - [OpenVPN](#openvpn)
    - [Create client certificates](#create-client-certificates)
    - [Enable OpenVPN Data Channel Offload (DCO)](#enable-openvpn-data-channel-offload-dco)
    - [Legacy clients support](#legacy-clients-support)
  - [Amnezia Wireguard](#amnezia-wireguard)
    - [Enable Amnezia Wireguard Kernel Extension](#enable-amnezia-wireguard-kernel-extension)
    - [AmneziaWG Parameters](#amneziawg-parameters)
    - [Amnezia Wireguard Block Size](#amnezia-wireguard-block-size)
  - [Extra information](#extra-information)
  - [Test speed with iperf3](#test-speed-with-iperf3)
- [Credits](#credits)

# Support and discussions group:
https://t.me/antizapret_support

# Features

- Modular design. External and high quality opensource modules/containers are used as builing blocks of our system. 
- User friendly web panels for administration of VPN's and DNS.
- Multiple VPN transports: Wireguard, Amnezia Wireguard, OpenVPN
- AdguardHome as main DNS resolver and blocked domains manager
- Multi-Server Architecture to bypass services geo restrictions. Different domains use different servers as exit nodes.
- Firewall to protect from port scanning
- Support for kernel modules for OpenVPN and Amnezia Wireguard to decrease CPU usage.
- SOCKS5 and HTTP(S) proxies for per-application routing through local or world exit nodes
- Built-in anti-DPI support with [bol-van/zapret2](https://github.com/bol-van/zapret2) for HTTP, TLS, and QUIC traffic. Config bundled from [vernette/ss-zapret2](https://github.com/vernette/ss-zapret2)

# How it works?

1) List of blocked domains downloaded from open registry.
2) List parsed and rules for dns resolver (adguardhome) created.
3) Adguardhome resend requests for blocked domains to python script dnsmap.py.
4) Python script:
   a) resolve real address for domain
   b) create fake address from 14.16.0.0/14 subnet
   c) create iptables rule to forward all packets from fake ip to real ip.
5) Fake IP is sent in DNS response to client
6) VPN tunnels configured with split tunneling. Only traffic to 14.16.0.0/14 subnet is routed through VPN.


# Installation

> [!IMPORTANT]
> Commands must be run as root. Otherwise, config files will have inconsistent rights, and some containers will reboot infinitely.

## Single Server (Easy)

Recommended to use server located in western countries. Some sites will block users from other countries. 

0. Install [Docker Engine](https://docs.docker.com/engine/install/):
   ```bash
   curl -fsSL https://get.docker.com -o get-docker.sh
   sudo sh get-docker.sh
   ```
1. Clone repository and start container:
   ```bash
   git clone https://github.com/xtrime-ru/antizapret-vpn-docker.git antizapret
   cd antizapret
   git checkout v6
   ```
2. Create docker-compose.override.yml with services you need. Minimal example with only wireguard:
```yml
services:
  adguard:
    environment:
      - ADGUARDHOME_PASSWORD=somestrongpassword
  wireguard:
     environment:
        - WIREGUARD_PASSWORD=somestrongpassword
     extends:
        file: services/wireguard/docker-compose.yml
        service: wireguard
```
Find full example in [docker-compose.override.sample.yml](./docker-compose.override.sample.yml)

3. Start services:
```shell
   docker compose up -d
   docker system prune -f
```

## Docker Swarm, multiple exit nodes (Advanced)
Version 5 and 6 comes with ability to forward traffic to different exit nodes for different domains. 
For example, YouTube works best if exit node is close to client and other services require foreign IP to work. 
Docker swarm is used to build unified network between containers.

Its recommended to use local server as manager/primary node for VPN's, DNS and az-local containers.
Foreign server – as secondary/worker node for az-world container.

Most of the domains will be proxied through **local** server for maximum speed and performance. 
Some of the sites, which use geoip to block users, will be proxied through **foreign** server.

0. Repeat steps 0 and 1 from single server installation on **both servers**:
   - Install docker 
   - Checkout project in same location on both servers.
1. [Primary] Create docker-compose.override.yml on primary node and define which services you need. See step 2 from single server installation.
1. [Primary] Change hostnames of servers to az-local and az-world for ease of use: `hostnamectl set-hostname az-local`
1. [Secondary] Change hostnames of servers to az-local and az-world for ease of use: `hostnamectl set-hostname az-world`
1. [Optionally] hub.docker.com can be unreachable on local hostings. Proxy can be used. See instructions: https://dockerhub.timeweb.cloud
    Alternatively images can be build locally on **both servers**: `docker compose build`
1. [Primary]: `docker swarm init --advertise-addr <PRIMARY_SERVER_PUBLIC_IP_ADDRESS>`
1. [Secondary]: Copy command from results  and run it on secondary node: `docker swarm join --token <TOKEN> <MANAGER_IP_ADDRESS>:<PORT>`
1. [Primary]: Inspect swarm `docker node ls`
    ```text
    ID                            HOSTNAME   STATUS    AVAILABILITY   MANAGER STATUS   ENGINE VERSION
    6dzagr08r8d2iidkcumjjz3q7 *   az-local   Ready     Active         Leader           29.0.1
    vspy2m6w4tf7uv4ywgdnzttvr     az-world   Ready     Active                          29.0.1
    ```
1. [Primary] Add labels for nodes `docker node update --label-add location=local az-local && docker node update --label-add location=world az-world`
1. [Primary]: start swarm `   docker compose config | docker run --pull always --rm -i xtrime/antizapret-vpn:6 compose2swarm | docker stack deploy --prune -c - antizapret`
1. [Primary]: Docker Swarm does not support passing host devices to services in the same way as Docker Compose, so VPN containers require DKMS kernel modules:
    - [Enable OpenVPN Data Channel Offload (DCO)](#enable-openvpn-data-channel-offload-dco)
    - [Enable Amnezia Wireguard Kernel Extension](#enable-amnezia-wireguard-kernel-extension)

## VPN / Hosting block
Most providers now block vpn connections to foreign IPs. Obfuscation in Amnezia or OpenVpn not always fix the issue.
For stable vpn operation you can try to connect to VPS inside your country and then proxy traffic to foreign server.

There are two ways:
1. [Recommended] Installation in [docker swarm mode](#docker-swarm-multiple-exit-nodes-advanced)
1. Proxy all traffic via local proxy. See below.

Example of startup script.
Replace <SERVER_IP> with IP address of your server and run it on fresh VPS (ubuntu 24.04 is recommended):

```shell
#!/bin/sh

# Fill with your foreign server ip
export VPN_IP=<SERVER_IP>

echo "net.ipv4.ip_forward=1" >> /etc/sysctl.d/99-sysctl.conf
sysctl -w net.ipv4.ip_forward=1

# DNAT rules
iptables -t nat -A PREROUTING -p tcp ! --dport 22 -j DNAT --to-destination "$VPN_IP"
iptables -t nat -A PREROUTING -p udp ! --dport 22 -j DNAT --to-destination "$VPN_IP"
# MASQUERADE rules
iptables -t nat -A POSTROUTING -p tcp -d "$VPN_IP" -j MASQUERADE
iptables -t nat -A POSTROUTING -p udp -d "$VPN_IP"  -j MASQUERADE

echo iptables-persistent iptables-persistent/autosave_v4 boolean true | sudo debconf-set-selections
echo iptables-persistent iptables-persistent/autosave_v6 boolean false | sudo debconf-set-selections
apt install -y iptables-persistent

```

## After installation
1. Make sure Secure DNS is disabled in your browser settings. 
   In chrome: Navigate to Settings > Privacy and security > Security, scroll to the "Advanced" section, and toggle off "Use secure DNS"
2. Install DKMS modules for openvpn and/or amnezia wireguard (if you use them): 
    - [Enable OpenVPN Data Channel Offload (DCO)](#enable-openvpn-data-channel-offload-dco)
    - [Enable Amnezia Wireguard Kernel Extension](#enable-amnezia-wireguard-kernel-extension)

## Access admin panels

### HTTPS
By default, all container can be accessed via https. For certificated management separate `https` container is used.
If you did not provide domain and email in its env it will generate self-signed certificates

- dashboard: https://%your-server-ip%:443
- adguard: https://%your-server-ip%:1443
- filebrowser: https://%your-server-ip%:2443
- openvpn: https://%your-server-ip%:3443
- wireguard: https://%your-server-ip%:4443
- wireguard-amnezia: https://%your-server-ip%:5443


### Local network
   When you connected to VPN, you can access containers without exposing ports to internet:
- http://adguard.antizapret:3000
- http://dashboard.antizapret:80
- http://wireguard-amnezia.antizapret:51821
- http://wireguard.antizapret:51821
- http://openvpn-ui.antizapret:8080
- http://filebrowser.antizapret:80

### HTTP:
By default, containers don't expose web panels to internet. All web panels are proxied via `https` container.
If you want to expose http to internet, add port forwarding to docker-compose.override.yml.
Example:
```yml
services:
   adguard:
      #...
      ports:
        - "3000:3000/tcp"
```

List of default ports: 

- adguard: http://%your-server-ip%:3000
- dashboard: http://%your-server-ip%:80
- wireguard-amnezia: http://%your-server-ip%:51821
- wireguard: http://%your-server-ip%:51821
- openvpn-ui: http://%your-server-ip%:8080
- filebrowser: http://%your-server-ip%:80

Some containers have same ports. So you need to choose unique external port in docker-compose.override.yml.

## Update

- Single instance
   ```shell
   git pull --rebase
   docker compose down --remove-orphans
   docker compose up -d --remove-orphans
   docker system prune -af
   ```
- Swarm mode: 
   ```shell
   git pull --rebase
   docker pull xtrime/antizapret-vpn:6
   docker compose config | docker run --pull always --rm -i xtrime/antizapret-vpn:6 compose2swarm | docker stack deploy --prune -c - antizapret
   docker system prune -af
   ```

### Upgrade from v5

1. Upgrade containers:
- Docker Compose mode (single server):
   ```shell
   docker compose down --remove-orphans
   git fetch && git checkout v6 && git pull --rebase
   docker compose down --remove-orphans
   docker compose up -d --remove-orphans
   docker system prune -af
   ```
- Swarm mode:
   - master node:
  ```shell
  docker stack rm antizapret && sleep 10
  git fetch && git checkout v6 && git pull --rebase
  docker compose config | docker run --pull always --rm -i xtrime/antizapret-vpn:6 compose2swarm | docker stack deploy --prune -c - antizapret
  docker system prune -af
   ```
  - worker nodes:
  ```shell
  git fetch && git checkout v6 && git pull --rebase
  ```

2. Update clients:
   - Wireguard/Amnezia 
     - Check if your password is longer than 12 symbols. Update if needed in docker-compose.override.yml
     - Download new client configs, or add `14.16.0.0/14` to AllowedIps manually in old configs.
   - OpenVPN 
     - Click save at openvpn-ui server config page: http://openvpn-ui.antizapret:8080/ov/config/ and then restart openvpn server.
     - Install new dkms module on host: `apt remove openvpn-dkms-dco` + https://github.com/xtrime-ru/antizapret-vpn-docker/blob/v6/README.md?tab=readme-ov-file#enable-openvpn-data-channel-offload-dco
   - Socks 
   Replace it with proxy container and rename ENV variables. See example: https://github.com/xtrime-ru/antizapret-vpn-docker/blob/v6/docker-compose.override.sample.yml#L63-L93
   Make sure you use strong password, because now HTTPS proxy accessible from internet.

## Reset:
Remove all settings, vpn configs and return initial state of service:
```shell
docker stack rm antizapret || docker compose down --remove-orphans
rm -rf config/*
git restore config
```

# Documentation

## FAQ (Frequently Asked Questions)

1. How to get VPN configs?
    - OVPN:
        1. https://%your-server-ip%:3443/certificates
        1. Create Certificate
        1. Enter Any Name and leave all other fields as is
        1. Click "Create". New certificate will appear in the list.
        1. Click on certificate name in the list to download it.
    - Wireguard or Amnezia:
        1. Go to https://%your-server-ip%:4443 or https://%your-server-ip%:5443
        2. Click "New"
        3. Enter any name.
        4. Create client
        5. Click download button in the list.
        6. QR Codes dont work for Amnezia Wireguard, because config is too big for QR code
2. Which Amnezia Wireguard client to use?
   A recommended client for Amnezia Wireguard is AmneziaWG:
    - [Android (Google Play)](https://play.google.com/store/apps/details?id=org.amnezia.awg)
    - [iOS (App Store)](https://apps.apple.com/app/amneziawg/id6478942365)
    - [Windows (GitHub)](https://github.com/amnezia-vpn/amneziawg-windows-client/releases)
3. Why don't OpenVPN client connect to server?
   Most providers block openvpn protocol, especially to foreign IPs. 
   The symptoms are: client connects, but after few transferred bytes server stops responding and connection is terminated.
   
   - By default, an openvpn container uses light obfuscation of UDP packets.  
     It works on most clients (including routers) but still can be blocked by providers.  
     See [OBFUSCATE_TYPE](#openvpn) env.  
     Try to change it from default `1` (light) to `2` (strong) or `0` (off).
   - Use cascade connection or swarm mode [cascade](#vpn--hosting-block)
4. Why can VPN connection be slow and have a lot of dropped packets?
   1. First, use reproducible test to detect issues: [Test speed with iperf3](#test-speed-with-iperf3)
   2. Check if CPU on your hosting is not overloaded during iperf test. 
   3. Ensure kernel modules for your VPN are installed and working: [OVPN DCO](#enable-openvpn-data-channel-offload-dco),  [Amnezia Wireguard Kernel Extension](#enable-amnezia-wireguard-kernel-extension). 
   4. Some inexpensive hostings have very slow CPUs, so even with all kernel modules installed, connection speed will not exceed 100 Mbit/s.
   5. Most routers have slow CPUs and provide only 30-60 Mbit/s via openvpn. Try to use Wireguard or Amnezia Wireguard if router supports it or update router to newer model.
   6. By default, new wireguard and openvpn setups use low MTU to ensure stable connection in mobile networks.
      
      First, check if your VPN connection has issues with default MTU.  
      - MacOs: `ping -D -s 1100 youtube.com`
      - Linux: `ping -M -s 1100 youtube.com`
      - Windows: `ping youtube.com -f -l 1100`

      For old setups you need manually reduce MTU in settings:
      - Wireguard/Amezia:
        MTU Must be lower on both server and client.
          1. Go to http://wireguard.antizapret:51821 or http://wireguard-amnezia.antizapret:51821 and click on client config icon.
          1. Lower MTU to 1200 and save. MTU is client-specific.
          1. Download and apply new config to your client.
          1. [Test speed with iperf3](#test-speed-with-iperf3)
      - OpenVPN:
          1. Go to http://openvpn-ui.antizapret:8080/ov/config and add `tun-mtu 1200` and `mssfix 1232` to your server config.
          1. Save Config and restart server.
          1. Add `tun-mtu 1200` and `mssfix 1232` to your client.conf
          1. `tun-mtu 1200` limits packets inside the tunnel, while `mssfix 1232` leaves room for OpenVPN and IPv6/UDP overhead on a 1280-byte path.
   7. If nothing helps, try another hosting and/or [cascade](#vpn--hosting-block)
5. How to debug issues with VPN?
   1. Check if the VPN connection is established and the DNS server is working:
      ```shell
      > nslookup youtube.com
      
      Server:		14.16.0.1
      Address:	14.16.0.1#53
      
      Non-authoritative answer:
      Name:	youtube.com
      Address: 14.16.13.209
      ```
   2. Check if browser dont use DoH/Secure DNS.
   3. Check DIST filters have loaded and have non 0 rule counters: http://adguard.antizapret:3000/#filters
   4. Check DNS resolution steps: http://adguard.antizapret:3000/#logs?response_status=all&search=youtube.com
      Each domain resolved via 2–4 DNS requests.
      See: [DNS resolving algorithm](#dns-resolving-algorithm)

## DNS resolving algorithm

![Preview](./img/chart.png)

1. DNS Request arrives into AdGuardHome
1. Adguard check it with blacklist rules. If domain in blacklist - return 0.0.0.0 and client not able to access domain.
1. Adguard Send DNS request to CoreDNS service.
1. CoreDNS Send DNS request to internal dnsmap.py server (antizapret container) and dnsmap.py sends request back to adguard
1. Adguard receives requests one more time, but now applies rules with `$client=az-local` and real upstream server client (8.8.8.8 by default)
1. If domain in whitelist - adguard will resolve its address and return to dnsmap.py
1. If domain not in whitelist adguard return SERVFAIL
1. dnsmap.py send response to adguard:
   1. If it is valid IP, then replaces it with "internal" IP from `14.16.0.0/15` subnet, add masquerade to iptables and return internal ip to adguard 
   1. If is is SERVFAIL it sends this response to client.
1. If CoreDNS receives SERVFAIL it retries request and send it directly to Adguard. In this case rules with `$client=az-local` do not applied and request processed normally.

**Why so complicated?** 
- Windows and some other clients do not retry to Fallback DNS, even if  SERVFAIL received. So we added CoreDNS for that. 
- Adguard don't allow to redefine upstream in blacklist/whitelist rules. 
  But this rules have regex support and updated automatically, so we want to use them.
  So multiple requests from different clients are made internally.
- Adguard allows different upstreams for different clients. So we can use different DNS for blocked and non blocked domains.

**Example:**  
We requested `youtube.com`, which should be routed via az-local node.
1. DNS request from client to Adguard. Routed to coredns. Response will appear after all following requests are processed.
   ```text
   Status: Processed
   DNS server: coredns:53
   Elapsed: 91 ms
   Served from cache: False
   Response code: NOERROR
   Response: 
   A: 14.16.13.209 (ttl=300)
   A: 14.16.13.207 (ttl=300)
   A: 14.16.13.206 (ttl=300)
   A: 14.16.13.208 (ttl=300)
   ```
2. DNS request from coredns to az-world. And az-world request to Adguard:
   ```text
   Status: Rewritten
   Elapsed: 0.10 ms
   Response code: SERVFAIL
   Rule(s):
   ||*^$dnsrewrite=SERVFAIL,client=az-world
   Custom filtering rules
   ```    
   SERFAIL response means that this domains not routed via az-world.
3. DNS request from coredns to az-local. And az-local request to Adguard:
   ```text
   Status: Processed
   DNS server: 149.112.112.11:53
   Elapsed: 50 ms
   Response code: NOERROR
   Response
   A: 173.194.221.190 (ttl=300)
   A: 173.194.221.91 (ttl=300)
   A: 173.194.221.136 (ttl=300)
   A: 173.194.221.93 (ttl=300)
   ```   
   In this case domain must be served via az-local and excluded from blacklist for az-local client. 
   Adguard cant find this domain in blacklist for az-local and and return real addresses to az-local client.
4. az-local container adds masquerade to iptables and return internal ip to coredns. 
5. coredns send response to adguard and adguard caches it and return to client.


## Adding Domains
There are two ways of adding domains. Via custom rules and via black lists.

### Adding Domains via rules
Open adguard panel: http://adguard.antizapret:3000/#custom_rules
Rules/syntaxes: https://adguard-dns.io/kb/general/dns-filtering-syntax/#basic-examples

By default, adguard rewrite all requests with SERVFAIL. This is a trick to make client retry DNS request to second, local DNS server.
Rules with the dnsrewrite response modifier have higher priority than other rules in AdGuard Home and AdGuard DNS.
To override default rule custom rules must have  `$dnsrewrite` modifier.

To support default adguard filters default SERVFAIL rule applied only to internal requests from client=az-local and client=az-world


Examples:
```
@@||subdomain.host.com^$dnsrewrite,client=az-local
@@||*.host.com^$dnsrewrite,client=az-local
@@||host.com^$dnsrewrite,client=az-world
@@||de^$dnsrewrite,client=az-world

@@/some_.*_regex/$dnsrewrite,client=az-local
```

### Adding Domains via lists
Also you can add any urls to blocklist. http://adguard.antizapret:3000/#dns_blocklist
Need to use adapter, to parse and adapt list in different formats.
 - Add domains for local exit node: `http://az-local.antizapret/list/?url=<ANY_URL>`
 - Add domains for world exit node `http://az-world.antizapret/list/?url=<ANY_URL>`
Supported formats: simple list of domains, adguard format, hosts format, json array of domains, regex list.

### Routing a website through VPN for a specific client

To route a specific website through VPN for only one client:

1. Find the client's internal IP address in the corresponding VPN server panel or in the AdGuard Home query log.
2. Open the AdGuard Home clients page: http://adguard.antizapret:3000/#clients, add the IP address to the client list, and configure the following upstream DNS servers for it:
   ```text
   coredns
   [/*.antizapret/]127.0.0.11
   [/example.com/]udp://coredns.antizapret
   ```
3. Open the AdGuard Home DNS settings: http://adguard.antizapret:3000/#dns and add an upstream for the required domain:
   ```text
   [/example.com/]1.1.1.1
   ```
4. Add `example.com` to `include-hosts-custom.txt`, or add the following rule on the custom filtering rules page: http://adguard.antizapret:3000/#custom_rules
   ```text
   @@||example.com^$dnsrewrite,client=az-local
   ```

After configuration, a regular local query in AdGuard Home returns the website's real IP address, while a query from the specified VPN client returns a rewritten internal IP address whose traffic is routed through the VPN.


Options for adapter: 
 - `url` - download list from url
 - `file` - read local file. Used for include-host-{custom,dist}.txt
 - `filter_custom=1` - filter lists with rules from exclude-hosts-custom.txt.
 - `filter_dist=0` - filter lists with rules from exclude-hosts-dist.txt
 - `format=list` - 'list' or 'json'. Detected automatically.
 - `client=az-local` - name of client to add to rules. Detected automatically.
 - `allow=1` - disable this option, to block domains from list for this exit node.
 - `raw=0` - dont modify rules
 - `suffix=1` - add "$dnsrewrite,client=xxx" to rules
 - `dnsrewrite=SERVFAIL` - set custom dnsrewrite value

## Adding IPs/Subnets
Add ips and subnets to `./config/antizapret/custom/include-ips-custom.txt`. 
Containers periodically check changes in config folder (every 5-10 seconds) and restart/update after any change.

## Adding ASNs
Add ASN numbers (`AS13335` or `13335`) or a part of an organization name (`Cloudflare`) to
`./config/antizapret/custom/include-asn-custom.txt`. Use `include-asn-world-custom.txt` for
the world node. DNS response addresses belonging to these ASNs will be routed through the
corresponding VPN node. Organization names are matched as case-insensitive substrings against
the raw MaxMind organization name. Use `/regex pattern/` to match with a case-insensitive
regular expression instead, for example `/\bg-?core\b/`.

[Online DPI check](https://hyperion-cs.github.io/dpi-checkers/ru/tcp-16-20/)

Trigger update manually: `docker exec $(docker ps -q --filter=name=az | head -n1) doall`

## SOCKS5 and HTTP(S) Proxy (per-application routing)

AntiZapret uses DNS-based split tunneling, which works only for domain-based connections.
If an application connects directly by IP address, DNS interception does not work and traffic is not routed through the VPN tunnel.

`proxy` service is based on [3proxy](https://github.com/3proxy/3proxy) [container](https://github.com/tarampampam/3proxy-docker)
It's a solution for per-application routing.

### How it works

1. Connect to VPN (OpenVPN, WireGuard or Amnezia WireGuard)
2. Configure your application to use SOCKS5 or HTTP/HTTPS proxy via proxy settings or tools like [AntizapretSOCKS5](https://github.com/danayer/AntizapretSOCKS5) (Windows), ProxyBridge, Proxifier or proxy settings in a web browser.
3. All traffic from that application (including direct IP connections) will exit through the selected server node

Two proxy containers are available:
- **`proxy-local.antizapret`** — traffic exits through the **local** server
    - SOCKS5 port: `8118`
    - HTTP port: `8180`
    - HTTPS (local) via `https` container: `https://%your_ip%:8143`
- **`proxy-world.antizapret`** — traffic exits through the **world** server
    - SOCKS5 port: `8118`
    - HTTP port: `8180`
    - HTTPS (world) via `https` container: `https://%your_ip%:8243`

Authentication: Basic (SOCKS5/HTTP/HTTPS) configured via environment variables.
Authentication is required because the HTTPS proxy is accessible from the internet.

### How to disable HTTPS access from the internet

Without HTTPS it's safe to use a proxy with an empty username and password.

There are two options:
- Make https container ENV variables for proxy-local.antizapret and proxy-world.antizapret empty.
- Change the hostname in your docker-compose.override.yml, so caddy/https can't reach them by default proxy-local.antizapret.


### When to use proxy instead of DNS-based routing

| Scenario | DNS routing | Proxy |
|---|---|--------------------------|
| Application connects by domain | ✅ Works | ✅ Works                  |
| Application connects by IP | ❌ Not routed | ✅ Works                  |
| Large number of IPs to route | ❌ OpenVPN push routes limit | ✅ No limit               |
| Per-application exit node selection | ❌ | ✅ Choose local or world per app |

### Configuration

Add proxy services to `docker-compose.override.yml`:
```yml
  proxy-local:
    hostname: proxy-local.antizapret
    extends:
      file: services/proxy/compose.yml
      service: proxy
    environment:
      - PROXY_LOGIN=admin
      - PROXY_PASSWORD=password
    deploy:
      mode: replicated
      replicas: 1
      endpoint_mode: dnsrr
      placement:
        constraints: [ node.labels.location == local ]

  proxy-world:
    hostname: proxy-world.antizapret
    extends:
      file: services/proxy/compose.yml
      service: proxy
    environment:
      - PROXY_LOGIN=admin
      - PROXY_PASSWORD=password
    deploy:
      mode: replicated
      replicas: 1
      endpoint_mode: dnsrr
      placement:
        constraints: [ node.labels.location == world ]
```

> **Note:** `proxy-world` requires [Docker Swarm mode](#docker-swarm-multiple-exit-nodes-advanced) with two nodes.
> On a single server only `proxy-local` will work.

### Client setup

1. Connect to VPN
2. Configure SOCKS5 or HTTP/HTTPS proxy in your application or browser:
    - **Host:** `proxy-local.antizapret` or `proxy-world.antizapret`
    - **SOCKS5 Port:** `1080`
    - **HTTP/HTTPS Port:** `3128`
    - **Username:** value of `PROXY_LOGIN`
    - **Password:** value of `PROXY_PASSWORD`

## zapret2
zapret2 support is based on [bol-van/zapret2](https://github.com/bol-van/zapret2), an anti-DPI toolkit that can modify HTTP, TLS, and QUIC traffic, and uses the Docker packaging from [vernette/ss-zapret2](https://github.com/vernette/ss-zapret2) as the source of the bundled zapret2 files. In this container it runs on antizapret exit-node traffic and can be tuned with the variables below.

It is disabled by default because it can cause problems on some hostings. To enable anti-DPI processing for HTTP, TLS, and QUIC traffic passing through the antizapret exit node, add it to `docker-compose.override.yml`:

```yaml
services:
  az-local:
    environment:
      - ZAPRET_ENABLED=1
```

If you use compose mode and the az-world node also suffers from DPI, enable it there too:
```yaml
services:
  az-world:
    environment:
      - ZAPRET_ENABLED=1
```

On the first start, the default zapret2 config is created at `./config/antizapret/zapret2/zapret.conf`.

### Changing configuration
Edit `NFQWS2_OPT` in this file to tune HTTP, TLS, and QUIC strategies. To disable zapret2 again, set `ZAPRET_ENABLED=0`.


Apply config changes with the command for your deployment mode:

- Compose mode:
```shell
# Docker Compose
docker compose up -d
docker compose restart antizapret
```

- Swarm mode, run on the primary/manager node
```shell
docker compose config | docker run --pull always --rm -i xtrime/antizapret-vpn:6 compose2swarm | docker stack deploy --prune -c - antizapret
docker service update --force antizapret_az-local
docker service update --force antizapret_az-world
```

### Strategy selection
To search for working strategies, stop zapret2, run `blockcheck2.sh`, then start zapret2 again. In Docker Compose mode:

```sh
docker exec $(docker ps -q --filter=name=az-local) sh /opt/zapret2/init.d/sysv/zapret2 stop
docker exec $(docker ps -q --filter=name=az-local) sh /opt/zapret2/blockcheck2.sh
docker exec $(docker ps -q --filter=name=az-local) sh /opt/zapret2/init.d/sysv/zapret2 start
```

For a faster targeted search, pass domains and search options:

```sh
docker exec $(docker ps -q --filter=name=az-local) sh -c 'REPEATS=8 DOMAINS="youtube.com discord.com" /opt/zapret2/blockcheck2.sh'
```


## Environment Variables

You can define these variables in docker-compose.override.yml file for your needs:

### Antizapret:
- `DNS=adguard` - AdGuard host used for DNS-over-HTTPS requests (default: `adguard`; DoH port: `3000`).
- `CLIENT=az-local` - AdGuard ClientID used by dnsmap. Set to `az-world` on the world node.
- `AZ_SUBNET=14.16.0.0/15` - subnet for virtual addresses of blocked hosts. The world node uses `14.18.0.0/15`.
- `ROUTES` - list of VPN containers and their virtual addresses. Used for iperf3 server.
- `DOALL_DISABLED=` - skip run on az-world node.
- `IPTABLES_SAVE_DISABLED=` - skip iptables rules restore on startup and save on shutdown.
- `IPS_URL=` - semicolon-separated URLs with IP prefixes for the local node. The merged result is written to `result/ips.txt`.
- `IPS_WORLD_URL=` - semicolon-separated URLs with IP prefixes for the world node. The merged result is written to `result/ips-world.txt`.
- `ASN_URL=` - semicolon-separated URLs with ASN numbers or organization names for the local node. The merged result is written to `result/asn.txt`.
- `ASN_WORLD_URL=` - semicolon-separated URLs with ASN numbers or organization names for the world node. The merged result is written to `result/asn-world.txt`.
- `ZAPRET_ENABLED=0` - set to `1` to enable zapret2 traffic modification for HTTP, HTTPS, and QUIC traffic passing through the container. 
- `ZAPRET_CONFIG=/opt/zapret2/config/zapret.conf` - path inside the container to the zapret2 configuration file. The default config is created automatically on first start and is persisted at `./config/antizapret/zapret2/zapret.conf`.

### Adguard: 
- `ROUTES` - list of VPN containers and their virtual addresses. Used for unique client addresses in adguard logs
- `ADGUARDHOME_PORT=3000`
- `ADGUARDHOME_USERNAME=admin`
- `ADGUARDHOME_PASSWORD=`
- `ADGUARDHOME_PASSWORD_HASH=` - hashed password, taken from the AdGuardHome.yaml file after the first run using `ADGUARDHOME_PASSWORD`. Dollar sign `$` in hash must be escaped with another dollar sign: `$$`

### CoreDNS: 
- None

### Filebrowser:
- `FILEBROWSER_PORT=admin`
- `FILEBROWSER_PASSWORD=password`

### Https:
- `PROXY_DOMAIN=` - create letsencrypt https certificate for domain. If not set host ip is used for self-signed certificate.
- `PROXY_EMAIL=` - email for letsecnrypt certificate.

### Openvpn
- `ROUTES`
- `OBFUSCATE_TYPE=1` - custom obfuscation level of openvpn protocol.
   - 0 - disable. Regular openvpn client mode, supported by all clients.
   - 1 - light obfuscation. Works with microtic and old keenetic routers
   - 2 - strong obfuscation. Works with most of the clients: openvpn official gui client, asus routers, new keenetic routers, openwrt routers.
- `AZ_SUBNET=14.16.0.0/14` - subnet for virtual blocked ips.

### Openvpn-ui
- `OPENVPN_ADMIN_USERNAME=` - replace default username with your username
- `OPENVPN_ADMIN_PASSWORD=` - replace default password with your password
- `OPENVPN_EXTERNAL_IP` - external ip of your server, by default detected automatically
- `OPENVPN_DNS=14.16.0.1` - DNS address for clients. Must be in `ANTIZAPRET_SUBNET`
- `OPENVPN_LOCAL_IP_RANGE=10.1.165.0` - subnet for ovpn clients. Subnet can be viewed in adguard journal or in ovpn-ui panel

### Wireguard/Wireguard Amnezia
- `ROUTES` 
- `WIREGUARD_PASSWORD=` - password for admin panel (used during initial setup only, change password via web UI afterwards)
- `WIREGUARD_USERNAME=admin` - username for admin panel (used during initial setup only)
- `AZ_SUBNET=14.16.0.0/14` - subnet for virtual blocked ips.
- `WG_DEFAULT_DNS=14.16.0.1` - DNS address for clients. Must be in `ANTIZAPRET_SUBNET`
- `WG_PERSISTENT_KEEPALIVE=25`
- `PORT=51821` - admin panel port
- `INSECURE=true` - allow HTTP access to admin panel
- `DISABLE_IPV6=true` - disable IPv6 support
- `WG_PORT=51820` - wireguard server port
- `EXPERIMENTAL_AWG=true` - enable AmneziaWG support (wireguard-amnezia only)
- `OVERRIDE_AUTO_AWG=awg`- environment variable to force the tunnel type: `awg` to always use AmneziaWG, `wg` to always use standard WireGuard; by default it’s unset and automatic detection is used, useful to override auto-selection and lock the mode.
- `BGP_ENABLE=false` - start bird BGP server. Server will push routes to clients (some routers). Clients will receive route updates without updating wg/awg config.

### SOCKS5 Proxy (depricated, use proxy below)
- `SOCKS_USERNAME` - username for SOCKS5 authentication (omit to disable authentication)
- `SOCKS_PASSWORD` - password for SOCKS5 authentication (omit to disable authentication)

### Proxy (http + socks5)
- `PROXY_LOGIN` - username for HTTP authentication (omitting disable authentication)
- `PROXY_PASSWORD` - password for HTTP authentication (omitting disable authentication)
- `PROXY_PORT=8180` - HTTP port to listen
- `SOCKS_PORT=8118` - SOCKS5 port to listen
- `EXTRA_ACCOUNTS` - Additional login:password pairs. Example: `login:password;login2:password2`
- `EXTRA_CONFIG` - Raw 3proxy config lines injected before proxy/socks directives (empty by default)

## DNS
### Adguard Upstream DNS
Adguard uses Google DNS and Quad9 DNS to resolve unblocked domains. This upstreams support ECS requests (more info below).
Cloudflare DNS do not support ECS and is not recommended for use.  

Source code: [Adguard upstream DNS](./antizapret/root/adguardhome/upstream_dns_file_basis)
After container is started working copy is located here: `./config/adguard/conf/upstream_dns_file_basis`

### CDN + ECS
Some domains can resolve differently, depending on subnet (geoip) of client. In this case using of DNS located on remote server will break some services.
ECS allow to provide client IP in DNS requests to upstream server and get correct results.
Its enabled by default in Adguard and client ip is pointed to Moscow (Yandex Subnet).

If you located in other region, you need to replace `77.88.8.8` with your real ip address on this page `http://your-server-ip:3000/#dns`



## OpenVPN
### Create client certificates:
https://github.com/d3vilh/openvpn-ui?tab=readme-ov-file#generating-ovpn-client-profiles
1) go to `http://%your_ip%:8080/certificates`
2) click "create certificate"
3) enter unique name. Leave all other fields empty
4) click create
5) click on certificate name in list to download ovpn file.

### Enable OpenVPN Data Channel Offload (DCO)
[OpenVPN Data Channel Offload (DCO)](https://openvpn.net/as-docs/openvpn-dco.html) provides performance improvements by moving the data channel handling to the kernel space, where it can be handled more efficiently and with multi-threading.
**tl;dr** it increases speed and reduces CPU usage on a server.

Kernel extensions can be installed only on <u>a host machine</u>, not in a container.

#### Ubuntu 26.04/24.04/22.04/20.04
Ubuntu 26.04 already includes the OpenVPN DCO kernel module in the stock kernel. Installing `ovpn-dkms` from the OpenVPN repository for 26.04 is optional and is needed only to get a newer module version.

```bash
sudo rm -f /etc/apt/sources.list.d/openvpn.list
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://swupdate.openvpn.net/repos/repo-public.gpg | sudo tee /etc/apt/keyrings/openvpn-repo-public.asc > /dev/null
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/openvpn-repo-public.asc] https://build.openvpn.net/debian/openvpn/release/2.7 $(lsb_release -sc) main" | sudo tee /etc/apt/sources.list.d/openvpn-aptrepo.list > /dev/null
sudo apt update
sudo apt install -y ovpn-dkms
```

### Legacy clients support
If your clients do not have GCM ciphers support you can use legacy CBC ciphers.
DCO is incompatible with legacy ciphers and will be disabled. This is also increase CPU load.


## Amnezia Wireguard

### Enable Amnezia Wireguard Kernel Extension

https://github.com/amnezia-vpn/amneziawg-linux-kernel-module?tab=readme-ov-file#ubuntu

#### Ubuntu 26.04
The AmneziaWG kernel module is not available for Ubuntu 26.04 yet. The Ubuntu 24.04 module works on 26.04, so the steps below use the 24.04 package.

1. `sudo add-apt-repository ppa:amnezia/ppa`
2. `sudo sed -i 's/\bresolute\b/noble/g' /etc/apt/sources.list.d/amnezia-ubuntu-ppa-resolute.sources`
3. `sudo apt update`
4. `sudo apt install -y amneziawg`
5. Restart server or `docker compose restart wireguard-amnezia`
6. Check the list of kernel modules `dkms status`,
   and check that bunch of `[kworker/X:X-wg-crypt-wg0]` processes are now running.

#### Ubuntu 24.04
1. `sudo add-apt-repository ppa:amnezia/ppa`
2. `sudo apt install -y amneziawg`
3. restart server or `docker compose restart wireguard-amnezia`
4. check the list of kernel modules `dkms status`, 
   and check that bunch of `[kworker/X:X-wg-crypt-wg0]` processes are now running.

#### Ubuntu 20.04, 22.04
1. Edit `etc/apt/sources.list` and uncomment `deb-src http://archive.ubuntu.com/ubuntu ... main restricted`
2. `sudo apt update`
3. `sudo apt install -y software-properties-common python3-launchpadlib gnupg2 linux-headers-$(uname -r)`
4. install source for kernel `sudo apt-get source linux-image-$(uname -r)`
5. `sudo add-apt-repository ppa:amnezia/ppa`
6. `sudo apt install -y amneziawg`
7. `sudo dkms install -m amneziawg -v 1.0.0`
8. restart server or `docker compose restart wireguard-amnezia`
9. check the list of kernel modules `dkms status`, 
   and check that bunch of `[kworker/X:X-wg-crypt-wg0]` processes are now running.
   
### AmneziaWG Parameters

Parameter descriptions can be found in the [AmneziaWG documentation](https://docs.amnezia.org/documentation/amnezia-wg) and on the kernel module page.

Use [AmneziaWG Config Generator](https://architect.vai-rice.space/) to generate unique AmneziaWG parameters.

All parameters **except I1–I5** will be set automatically at first startup. For instructions on configuring I1–I5, refer to the AmneziaWG documentation.

- If a parameter is **not set**, it will not be included in the configuration.
- If **all AmneziaWG-specific parameters are absent**, AmneziaWG is fully compatible with standard WireGuard.

## Parameter Compatibility Table

| Parameter | Can differ between server and client | Configurable on server | Configurable on client |
|-----------|-------------------------------------|----------------------|----------------------|
| Jc        | ✅ Yes                               | ✅ Yes               | ✅ Yes               |
| Jmin      | ✅ Yes                               | ✅ Yes               | ✅ Yes               |
| Jmax      | ✅ Yes                               | ✅ Yes               | ✅ Yes               |
| S1–S4     | ❌ No, must match                    | ✅ Yes               | ❌ No (copied from server) |
| H1–H4     | ❌ No, must match                    | ✅ Yes               | ❌ No (copied from server) |
| I1–I5     | ✅ Yes                               | ✅ Yes               | ✅ Yes               |

## Notes

- Parameters Jc, Jmin, Jmax, I1–I5 can be configured independently on server and client if needed.
- Parameters S1–S4 and H1–H4 **must match** between server and client; client copies them automatically from the server.
- Use I1–I5 only if you need advanced customization. Otherwise, default automatic values are sufficient.

### Amnezia Wireguard Block Size
Amnezia adds random packets to change signature of wireguard protocol and bypass DPI. 
By default we use `JMIN=20; JMAX=100` for junk packet size in bytes.

Large junk packets can help to bypass DPI, but some firewalls can block them as DDOS attack.
Use env variables to change their size if you have issues with amnezia connection:

```
JC=3
JMIN=20
JMAX=100
```
or
```
JC=2
JMIN=10
JMAX=20
```
Example part of docker-compose.override.yml with JC, JMIN and JMAX:
```yml
  wireguard-amnezia:
    environment:
      - WIREGUARD_PASSWORD=xxxxx
      - JC=2
      - JMIN=10
      - JMAX=20
    extends:
      file: services/wireguard/docker-compose.yml
      service: wireguard-amnezia
```
Settings/env variables are saved in ./config/wireguard_amnezia/ folder. To update them remove folder and run container again.
This will also remove all existing clients/certificates.
```shell
docker compose down && rm -rf ./config/wireguard_amnezia/ && docker compose up -d
```


## Extra information
- [OpenWrt setup guide](./docs/guide_OpenWrt.md) - how to setup OpenWrt router with this solution to keep LAN clients happy.
- [Keenetic setup guide](./docs/guide_Keenetic.md) - instructions for configuring the server and connecting Keenetic routers to it [(на русском языке)](./docs/guide_Keenetic_RU.md)

## Test speed with iperf3
iperf3 server is included in antizapret-vpn container.
1. Connect to VPN
2. Use iperf3 client on your phone or computer to check upload/download speed.
    Example 10 threads for 10 seconds and report result every second:
    ```shell
    # local node
    iperf3 -c az-local.antizapret -i1 -t10 -P10
    iperf3 -c az-local.antizapret -i1 -t10 -P10 -R
   
   # world node
    iperf3 -c az-world.antizapret -i1 -t10 -P10
    iperf3 -c az-world.antizapret -i1 -t10 -P10 -R
    ```

# Credits
- [ProstoVPN](https://antizapret.prostovpn.org) — the original project
- [AntiZapret VPN Container](https://bitbucket.org/anticensority/antizapret-vpn-container/src/master/) — source code of the LXD-based container
- [AntiZapret PAC Generator](https://bitbucket.org/anticensority/antizapret-pac-generator-light/src/master/) — proxy auto-configuration generator to bypass censorship of Russian Federation
- [WireGuard VPN](https://github.com/wg-easy/wg-easy) — used for Wireguard integration
- [OpenVPN](https://github.com/d3vilh/openvpn-ui) - used for OpenVPN integration
- [AdGuardHome](https://github.com/AdguardTeam/AdGuardHome) - DNS resolver
- [filebrowser](https://github.com/filebrowser/filebrowser) - web file browser & editor
- [lighttpd](https://github.com/lighttpd/lighttpd1.4) - web server for unified dashboard
- [caddy](https://github.com/caddyserver/caddy) - reverse proxy
- [No Thought Is a Crime](https://ntc.party) — a forum about technical, political and economical aspects of internet censorship in different countries
- [Dante](https://www.inet.no/dante/) - SOCKS5 proxy server for per-application routing
