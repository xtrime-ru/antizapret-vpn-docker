# AntiZapret VPN in Docker

Antizapret created to redirect only blocked domains to VPN tunnel. Its called split tunneling.
This repo is based on idea from original [AntiZapret LXD image](https://bitbucket.org/anticensority/antizapret-vpn-container/src/master/)

# Support and discussions group:
https://t.me/antizapret_support

# How  works?

1) List of blocked domains downloaded from open registry.
2) List parsed and rules for dns resolver (adguardhome) created.
3) Adguardhome resend requests for blocked domains to python script dnsmap.py.
4) Python script:
   a) resolve real address for domain
   b) create fake address from 10.244.0.0/15 subnet
   c) create iptables rule to forward all packets from fake ip to real ip.
5) Fake IP is sent in DNS response to client
6) All vpn tunnels configured with split tunneling. Only traffic to 10.244.0.0/15 subnet is routed through VPN.

# Features

- [openvpn-dco](https://openvpn.net/as-docs/tutorials/tutorial--turn-on-openvpn-dco.html) - a kernel extension for improving performance of OpenVPN
- Multiple VPN transports: Wireguard, OpenVPN, IPsec/XAuth ("Cisco IPsec")
- Adguard as main DNS resolver
- filebrowser as web viewer & editor for `*-custom.txt` files
- Unified dashboard
- Optional built-in reverse proxy based on caddy


# Installation

0. Install [Docker Engine](https://docs.docker.com/engine/install/):
   ```bash
   curl -fsSL https://get.docker.com -o get-docker.sh
   sudo sh get-docker.sh
   ```
1. Clone repository and start container:
   ```bash
   git clone https://github.com/xtrime-ru/antizapret-vpn-docker.git antizapret
   cd antizapret
   ```
2. Create docker-compose.override.yml with services you need. Minimal example with only wireguard:
```yml
services:
  antizapret:
    environment:
      - ADGUARDHOME_PASSWORD=somestrongpassword
  wireguard:
     environment:
        - WIREGUARD_PASSWORD=somestrongpassword
     extends:
        file: services/wireguard/docker-compose.yml
        service: wireguard
     depends_on:
        - antizapret
```
Find full example in [docker-compose.override.sample.yml](./docker-compose.override.sample.yml)

3. Start services:
```shell
   docker compose pull
   docker compose build
   docker compose up -d
   docker system prune -f
```
4. Admin panels started as HTTPS at following ports at your host (with proxy container):
- dashboard: 433
- adguard: 1443
- filebrowser: 2443
- openvpn: 3443
- wireguard: 4443
- wireguard-amnezia: 5443


## Update

```shell
git pull
docker compose pull
docker compose build
docker compose down --remove-orphans && docker compose up -d --remove-orphans
```

### Upgrade from v3
**Only WireGuard/Amnezia configs can be moved**, please make backup WireGuard files (from `./.etc_wireguard` or `./.etc_wireguard_amnezia`) and put them in `./config/wireguard` or `./config/wireguard_amnezia` accordingly after steps below.

Recommended to perform full remove of old version:
```shell
docker compose down --remove-orphans
docker system prune -af
cd ../
rm -rf antizapret/
```

Then follow installation steps from this README.

## Reset:
Remove all settings, vpn configs and return initial state of service:
```shell
docker compose down
rm -rf config/*
docker compose up -d
```

# Documentation

## Adding Domains/IPs
Any domains or IPs can be added or excluded from routing with config files from `./config/antizapret/custom` directory.
These lists are added/excluded to/from automatically generated lists of domains and IP's.
Reboot container and wait few minutes for applying changes.
Here is rules for lists: https://github.com/AdguardTeam/AdGuardHome/wiki/Configuration#upstreams

Examples:
```
subdomain.host.com
*.host.com
host.com
de
```

## Environment Variables

You can define these variables in docker-compose.override.yml file for your needs:

Antizapret:
- `SKIP_UPDATE_FROM_ZAPRET=true` - do not download and use list of all blocked domains from internet.
    Will reduce RAM consumption. Need to manually fill domains in `*-custom.txt` files.
- `UPDATE_TIMER=1d` - blocked domains update interval
- `ADGUARDHOME_PORT=3000`
- `ADGUARDHOME_USERNAME=admin`
- `ADGUARDHOME_PASSWORD=`
- `ADGUARDHOME_PASSWORD_HASH=` - hashed password, taken from the AdGuardHome.yaml file after the first run using `ADGUARDHOME_PASSWORD`
- `DNS=8.8.8.8` - Upstream DNS for resolving blocked sites
- `ROUTES` - list of VPN containers and their virtual addresses. Needed for uniq client addresses in adguard logs 
- `LISTS` - list of urls to get blocked domains lists
- `IP_LIST` - main url to get list of blocked ips and domains. Override with blank value to disable download of this list.

Filebrowser:
- `FILEBROWSER_PORT=admin`
- `FILEBROWSER_PASSWORD=password`

Openvpn
- `OBFUSCATE_TYPE=0` - custom obfuscation level of openvpn protocol.
   0 - disable.Act as regular openvpn client, support by all clients.
   1 - light obfuscation, works with microtics
   2 - strong obfuscation, works with some clients: openvpn gui client, asuswrt client...
- `ANTIZAPRET_SUBNET=10.224.0.0/15` - subnet for virtual blocked ips
- `OPENVPN_DNS=10.1.165.1` - DNS address for clients. Must be in `ANTIZAPRET_SUBNET`

Openvpn-ui
- `OPENVPN_ADMIN_PASSWORD=` — will be used as a server address in .ovpn profiles upon keys generation (default: your server's IP)
- `OPENVPN_PORT=1194` — will be used as a server port in .ovpn profiles upon keys generation. (default: 1194)

Wireguard/Wireguard Amnezia
- `WIREGUARD_PASSWORD=` - password for admin panel
- `WIREGUARD_PASSWORD_HASH=` - [hashed password](https://github.com/wg-easy/wg-easy/blob/master/How_to_generate_an_bcrypt_hash.md) for admin panel
- `ANTIZAPRET_SUBNET=10.224.0.0/15` - subnet for virtual blocked ips
- `WG_DEFAULT_DNS=10.224.0.1` - DNS address for clients. Must be in `ANTIZAPRET_SUBNET`
- `WG_PERSISTENT_KEEPALIVE=25`
- `PORT=51821` - admin panel port
- `WG_PORT=51820` - wireguard server port
- `WG_DEVICE=eth0`

Wireguard, Wireguard Amnezia, Openvpn:
- `FORCE_FORWARD_DNS=true` - Redirects UDP traffic on port 53 to AntiZapret DNS (default: false)
- `FORCE_FORWARD_DNS_PORTS="53 5353"` - Parameter can be used to change port 53 for FORCE_FORWARD_DNS to one or more, separated by a space (default: 53)
- For other environment variables, see the original manual [Wireguard Amnezia](https://github.com/w0rng/amnezia-wg-easy) or [Wireguard](https://github.com/wg-easy/wg-easy).

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

## OpenVpn
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
sudo apt update
sudo apt upgrade
echo "#### Please reboot your system after upgrade ###" && sleep 100
sudo apt install -y efivar
sudo apt install -y openvpn-dco-dkms
```

#### Ubuntu 20.04, 22.04
```bash
sudo apt update
sudo apt upgrade
echo "#### Please reboot your system after upgrade ###" && sleep 100
deb=openvpn-dco-dkms_0.0+git20231103-1_all.deb
sudo apt install -y efivar dkms linux-headers-$(uname -r)
wget http://archive.ubuntu.com/ubuntu/pool/universe/o/openvpn-dco-dkms/$deb
sudo dpkg -i $deb
```

### Enable Amnezia Wireguard Kernel Extension

https://github.com/amnezia-vpn/amneziawg-linux-kernel-module?tab=readme-ov-file#ubuntu

1. Edit  `vi /etc/apt/sources.list` and uncomment `deb-src http://archive.ubuntu.com/ubuntu ... main restricted`
2. `sudo apt update`
3. `sudo apt install -y software-properties-common python3-launchpadlib gnupg2 linux-headers-$(uname -r)`
4. install source for kernel `sudo apt-get source linux-image-$(uname -r)`
5. `sudo add-apt-repository ppa:amnezia/ppa`
6. `sudo apt-get install -y amneziawg`
7. restart server or `docker compose restart wireguard-amnezia`

### Legacy clients support
If your clients do not have GCM ciphers support you can use legacy CBC ciphers.
DCO is incompatible with legacy ciphers and will be disabled. This is also increase CPU load.

### OpenVPN block
Most providers now block openvpn to foreign IPs. Obfuscation not always fix the issue.
For stable openvpn operation you can buy VPS inside of your country and then proxy all traffic to foreign server.
Here is example of startup script.
Replace X.X.X.X with IP address of your server and run it on fresh VPS (ubuntu 24.04 is recommended):

```shell
#!/bin/sh

# Fill with your foreign server ip
export VPN_IP=X.X.X.X

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
    iperf3 -c 10.224.0.1 -i1 -t10 -P10
    iperf3 -c 10.224.0.1 -i1 -t10 -P10 -R
    ```

## IPsec/XAuth (Cisco IPsec) server
**Important notice**: not all clients support tunnel-split (send only part of traffic via VPN).
For example **Apple** devices **will not** be able **to connect** to this server.

**Recommended to use OpenVPN or Wireguard/Amnezia instead.**

1. Create settings file:
   ```shell
   cp ipsec/ipsec.env.example ipsec/ipsec.env
   ```
2. Fill your creditentials in `ipsec/ipsec.env`
3. Start
   ```shell
   docker compose down
   docker compose -f docker-compose.ipsec.yml up -d
   ```
4. Setup your clients: https://github.com/hwdsl2/setup-ipsec-vpn/blob/master/docs/clients-xauth.md

# Credits
- [ProstoVPN](https://antizapret.prostovpn.org) — the original project
- [AntiZapret VPN Container](https://bitbucket.org/anticensority/antizapret-vpn-container/src/master/) — source code of the LXD-based container
- [AntiZapret PAC Generator](https://bitbucket.org/anticensority/antizapret-pac-generator-light/src/master/) — proxy auto-configuration generator to bypass censorship of Russian Federation
- [Amnezia WireGuard VPN](https://github.com/w0rng/amnezia-wg-easy) — used for Amnezia Wireguard integration
- [WireGuard VPN](https://github.com/wg-easy/wg-easy) — used for Wireguard integration
- [OpenVPN](https://github.com/d3vilh/openvpn-ui) - used for OpenVPN integration
- [IPsec VPN](https://github.com/hwdsl2/docker-ipsec-vpn-server) — used for IPsec integration
- [AdGuardHome](https://github.com/AdguardTeam/AdGuardHome) - DNS resolver
- [filebrowser](https://github.com/filebrowser/filebrowser) - web file browser & editor
- [lighttpd](https://github.com/lighttpd/lighttpd1.4) - web server for unified dashboard
- [caddy](https://github.com/caddyserver/caddy) - reverse proxy
- [No Thought Is a Crime](https://ntc.party) — a forum about technical, political and economical aspects of internet censorship in different countries
