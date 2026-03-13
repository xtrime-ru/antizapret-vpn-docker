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
  - [After installation](#after-installation)
  - [Access admin panels](#access-admin-panels)
    - [HTTPS](#https)
    - [Local network](#local-network)
    - [HTTP](#http)
  - [Update](#update)
    - [Upgrade from v5](#upgrade-from-v5)
  - [Reset](#reset)
- [Documentation](#documentation)
  - [DNS resolving algorithm](#dns-resolving-algorithm)
  - [Adding Domains](#adding-domains)
    - [Adding Domains via rules](#adding-domains-via-rules)
    - [Adding Domains via lists](#adding-domains-via-lists)
  - [Adding IPs/Subnets](#adding-ipssubnets)
  - [SOCKS5 Proxy (per-application routing)](#socks5-proxy-per-application-routing)
    - [How it works](#how-it-works-1)
    - [When to use Dante instead of DNS-based routing](#when-to-use-dante-instead-of-dns-based-routing)
    - [Configuration](#configuration)
    - [Client setup](#client-setup)
    - [Example use cases](#example-use-cases)     
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
    - [VPN / Hosting block](#vpn--hosting-block)
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
- SOCKS5 proxy (Dante) for per-application routing through local or world exit nodes

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
Foreign server - as secondary/worker node for az-world container.

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
1. [Primary, Secondary]: create config folders on **both nodes**: ```docker compose pull; docker compose up -d; sleep 60; docker compose down;```
1. [Primary]: start swarm `docker compose config | docker run --rm -i xtrime/antizapret-vpn:6 compose2swarm | docker stack deploy --prune -c - antizapret `

## After installation
1. If you use  openvpn client on your microtic router and having issues with UDP connection, try to reduce [OBFUSCATE_TYPE](#openvpn) env variable from default `2` (strong) to `1` (light) or `0` (off).
2. Make sure Secure DNS is disabled in your browser settings. 
   In chrome: Navigate to Settings > Privacy and security > Security, scroll to the "Advanced" section, and toggle off "Use secure DNS"
3. Install DKMS modules for openvpn and/or amnezia wireguard (if you use them): 
    - [Enable OpenVPN Data Channel Offload (DCO)](#enable-openvpn-data-channel-offload-dco)
    - [Enable Amnezia Wireguard Kernel Extension](#enable-amnezia-wireguard-kernel-extension)

## Access admin panels

### HTTPS
By default, all container can be accessed via https. For certificated management separate `https` container is used.
If you did not provide domain and email in its env it will generate self-signed certificates

- dashboard: https://<your-server-ip>:443
- adguard: https://<your-server-ip>:1443
- filebrowser: https://<your-server-ip>:2443
- openvpn: https://<your-server-ip>:3443
- wireguard: https://<your-server-ip>:4443
- wireguard-amnezia: https://<your-server-ip>:5443


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

- adguard: http://<your-server-ip>:3000
- dashboard: http://<your-server-ip>:80
- wireguard-amnezia: http://<your-server-ip>:51821
- wireguard: http://<your-server-ip>:51821
- openvpn-ui: http://<your-server-ip>:8080
- filebrowser: http://<your-server-ip>:80

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
   docker compose config | docker run --rm -i xtrime/antizapret-vpn:6 compose2swarm | docker stack deploy --prune -c - antizapret
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
   ```shell
   docker stack rm antizapret && sleep 10
   git fetch && git checkout v6 && git pull --rebase
   docker pull xtrime/antizapret-vpn:6
   docker compose config | docker run --rm -i xtrime/antizapret-vpn:6 compose2swarm | docker stack deploy --prune -c - antizapret
   docker system prune -af
   ```
2. Update clients:
   - Wireguard/Amnezia - need to download new client configs, or add `14.16.0.0/14` to AllowedIps manually in old configs.
   - OpenVPN - need to click save at openvpn-ui server config page: http://openvpn-ui.antizapret:8080/ov/config/ and then restart openvpn server.

## Reset:
Remove all settings, vpn configs and return initial state of service:
```shell
docker stack rm antizapret || docker compose down --remove-orphans
rm -rf config/*
```

# Documentation

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

Why so complicated? 
- Windows and some other clients do not retry to Fallback DNS, even if  SERVFAIL received. So we added CoreDNS for that. 
- Adguard don't allow to redefine upstream in blacklist/whitelist rules. 
  But this rules have regex support and updated automatically, so we want to use them.
  So multiple requests from different clients are made internally.
- Adguard allows different upstreams for different clients. So we can use different DNS for blocked and non blocked domains.


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

## Adding IPs/Subnets
Add ips and subnets to `./config/antizapret/custom/include-ips-custom.txt`. 
Containers periodically check changes in config folder (every 5-10 seconds) and restart/update after any change.

Trigger update manually: `docker exec $(docker ps -q --filter=name=az | head -n1) doall`

## SOCKS5 Proxy (per-application routing)

AntiZapret uses DNS-based split tunneling, which works only for domain-based connections.
If an application connects directly by IP address, DNS interception does not work and traffic is not routed through the VPN tunnel.

Adding large number of IPs to `include-ips-custom.txt` can cause issues with OpenVPN (push routes limit), so Dante SOCKS5 proxy was added as an alternative solution.

### How it works

1. Connect to VPN (OpenVPN, WireGuard or Amnezia WireGuard)
2. Configure your application to use SOCKS5 proxy via tools like [AntizapretSOCKS5](https://github.com/danayer/AntizapretSOCKS5) (Windows), ProxyBridge, Proxifier, or browser proxy settings
3. All traffic from that application (including direct IP connections) will exit through the selected server node

Two socks5 proxy containers are available:
- **`socks-local.antizapret:8118`** — traffic exits through the **local** server
- **`socks-world.antizapret:8118`** — traffic exits through the **world** server

Authentication: SOCKS5 with username/password (configured via environment variables).
To disable authentication, omit `SOCKS_USERNAME` and `SOCKS_PASSWORD` (or leave them empty).

### When to use Dante instead of DNS-based routing

| Scenario | DNS routing | Dante SOCKS5 |
|---|---|---|
| Application connects by domain | ✅ Works | ✅ Works |
| Application connects by IP | ❌ Not routed | ✅ Works |
| Large number of IPs to route | ❌ OpenVPN push routes limit | ✅ No limit |
| Per-application exit node selection | ❌ | ✅ Choose local or world per app |

### Configuration

Add socks5 services to `docker-compose.override.yml`:
```yml
  socks-local:
    hostname: socks-local.antizapret
    extends:
      file: services/socks/compose.yml
      service: socks
    environment:
      - SOCKS_USERNAME=admin
      - SOCKS_PASSWORD=password
    deploy:
      mode: replicated
      replicas: 1
      endpoint_mode: dnsrr
      placement:
        constraints: [ node.labels.location == local ]

  socks-world:
    hostname: socks-world.antizapret
    extends:
      file: services/socks/compose.yml
      service: socks
    environment:
      - SOCKS_USERNAME=admin
      - SOCKS_PASSWORD=password
    deploy:
      mode: replicated
      replicas: 1
      endpoint_mode: dnsrr
      placement:
        constraints: [ node.labels.location == world ]
```

> **Note:** `socks-world` requires [Docker Swarm mode](#docker-swarm-multiple-exit-nodes-advanced) with two nodes.
> On a single server only `socks-local` will work.

### Client setup

1. Connect to VPN
2. Configure SOCKS5 proxy in your application or proxy manager:
    - **Host:** `socks-local.antizapret` or `socks-world.antizapret`
    - **Port:** `8118`
    - **Type:** SOCKS5
    - **Username:** value of `SOCKS_USERNAME`
    - **Password:** value of `SOCKS_PASSWORD`

#### Windows

For Windows clients, use [AntizapretSOCKS5](https://github.com/danayer/AntizapretSOCKS5) — a GUI application for configuring per-application SOCKS5 routing using [ProxiFyre](https://github.com/wiresock/proxifyre).

1. Download and extract [AntizapretSOCKS5](https://github.com/danayer/AntizapretSOCKS5)
2. Run `ConfigEditor.exe` and install the Windows Packet Filter driver when prompted
3. Add proxy configurations — select applications, choose proxy server (`socks-local.antizapret:8118` or `socks-world.antizapret:8118`), and set credentials
4. Save configuration and start ProxiFyre

### Example use cases

- **Game client** that connects to servers by IP — route through `socks-world` to bypass geo-restrictions
- **Torrent client** — route through `socks-world` for foreign IP
- **Browser** — use proxy extension to route specific sites through `socks-local` or `socks-world`
- **Application with many hardcoded IPs** — instead of adding hundreds of IPs to `include-ips-custom.txt`, just proxy the whole app through socks5

## Environment Variables

You can define these variables in docker-compose.override.yml file for your needs:

### Antizapret:
Consists of two containers: az-local and az-world. This is VPN exit nodes.
- `DNS=adguard` - Upstream DNS for resolving blocked sites (adguard by default)
- `AZ_SUBNET=14.16.0.0/14` Subnet for virtual addresses for blocked hosts.
- `ROUTES` - list of VPN containers and their virtual addresses. Used for iperf3 server.
- `DOALL_DISABLED=` - skip run on az-world node.

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

### Proxy:
- `PROXY_DOMAIN=` - create letsencrypt https certificate for domain. If not set host ip is used for self-signed certificate.
- `PROXY_EMAIL=` - email for letsecnrypt certificate.
- `SOCKS_EXTERNAL_IFACES` - comma-separated list of external network interfaces for the SOCKS proxy (e.g. `eth0,eth1`). If omitted, interfaces are auto-detected; falls back to `eth0` when none are found

### Openvpn
- `ROUTES`
- `OBFUSCATE_TYPE=2` - custom obfuscation level of openvpn protocol.
   0 - disable.Act as regular openvpn client, support by all clients.
   1 - light obfuscation, works with microtic routers
   2 - strong obfuscation, works with most of the clients: openvpn official gui client, asus routers, keenetic routers, openwrt routers.
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

### SOCKS5 Proxy
- `SOCKS_USERNAME` - username for SOCKS5 authentication (omit to disable authentication)
- `SOCKS_PASSWORD` - password for SOCKS5 authentication (omit to disable authentication)

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

#### Ubuntu 24.04
```bash
sudo rm -f /etc/apt/sources.list.d/openvpn.list
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://swupdate.openvpn.net/repos/repo-public.gpg | sudo gpg --dearmor --yes -o /etc/apt/keyrings/openvpn-repo-public.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/openvpn-repo-public.gpg] http://build.openvpn.net/debian/openvpn/release/2.7 noble main" | sudo tee /etc/apt/sources.list.d/openvpn-aptrepo.list > /dev/null
sudo apt update
sudo apt install -y ovpn-dkms
```
#### Ubuntu 22.04
```bash
sudo rm -f /etc/apt/sources.list.d/openvpn.list
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://swupdate.openvpn.net/repos/repo-public.gpg | sudo gpg --dearmor --yes -o /etc/apt/keyrings/openvpn-repo-public.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/openvpn-repo-public.gpg] http://build.openvpn.net/debian/openvpn/release/2.7 jammy main" | sudo tee /etc/apt/sources.list.d/openvpn-aptrepo.list > /dev/null
sudo apt update
sudo apt install -y ovpn-dkms
```
#### Ubuntu 20.04
```bash
sudo rm -f /etc/apt/sources.list.d/openvpn.list
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://swupdate.openvpn.net/repos/repo-public.gpg | sudo gpg --dearmor --yes -o /etc/apt/keyrings/openvpn-repo-public.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/openvpn-repo-public.gpg] http://build.openvpn.net/debian/openvpn/release/2.7 focal main" | sudo tee /etc/apt/sources.list.d/openvpn-aptrepo.list > /dev/null
sudo apt update
sudo apt install -y ovpn-dkms
```
### Legacy clients support
If your clients do not have GCM ciphers support you can use legacy CBC ciphers.
DCO is incompatible with legacy ciphers and will be disabled. This is also increase CPU load.


## Amnezia Wireguard

### Enable Amnezia Wireguard Kernel Extension

https://github.com/amnezia-vpn/amneziawg-linux-kernel-module?tab=readme-ov-file#ubuntu

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

### VPN / Hosting block
Most providers now block vpn to foreign IPs. Obfuscation in amnezia or openvpn not always fix the issue.
For stable vpn operation you can try to connect to  VPS inside of your country and then proxy  traffic to foreign server.

There are two ways: 
1. [Recommended] Install in [docker swarm mode](#docker-swarm-multiple-exit-nodes-advanced)
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
