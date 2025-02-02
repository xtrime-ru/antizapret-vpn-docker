# AntiZapret VPN in Docker

Antizapret created to redirect only blocked domains to VPN tunnel. Its called split tunneling.
This repo is based on idea from original [AntiZapret LXD image](https://bitbucket.org/anticensority/antizapret-vpn-container/src/master/)

# How it works?

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
2. Create docker-compose.override.yml with services you need. For example:
```yml
services:
  antizapret:
    environment:
      - ADGUARDHOME_PASSWORD=somestrongpassword
  openvpn:
    extends:
      file: docker-compose.openvpn.yml
      service: openvpn
    environment:
      - OBFUSCATE_TYPE=2
  openvpn-ui:
    environment:
      - OPENVPN_ADMIN_PASSWORD=somestrongpassword
    extends:
      file: docker-compose.openvpn.yml
      service: openvpn-ui
  wireguard-amnezia:
    environment:
      - WIREGUARD_PASSWORD=somestrongpassword
    extends:
      file: docker-compose.wireguard-amnezia.yml
      service: wireguard-amnezia
```
3. Start services:
```shell
   docker compose pull
   docker compose up -d
```
4. Admin panels started at following ports at your host: 
- adguard: 3000
- wireguard/amnezia: 51821
- openvpn: 8080

## Update:

```shell
git pull
docker compose pull
docker compose down && docker compose up -d
```

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

Openvpn
- `OBFUSCATE_TYPE=0` - custom obfuscation level of openvpn protocol.   
   0 - disable.Act as regular openvpn client, support by all clients.
   1 - light obfuscation, works with microtics
   2 - strong obfuscation, works with some clients: openvpn gui client, asuswrt client...

Openvpn-ui
- `OPENVPN_ADMIN_PASSWORD=` — will be used as a server address in .ovpn profiles upon keys generation (default: your server's IP)
- `OPENVPN_PORT=1194` — will be used as a server port in .ovpn profiles upon keys generation. (default: 1194)

Wireguard/Wireguard Amnesia
- `WIREGUARD_PASSWORD=`

Wireguard, Wireguard Amnesia, Openvpn:
- `FORCE_FORWARD_DNS=true` - Redirects UDP traffic on port 53 to AntiZapret DNS (default: false)
- `FORCE_FORWARD_DNS_PORTS="53 5353"` - Parameter can be used to change port 53 for FORCE_FORWARD_DNS to one or more, separated by a space (default: 53)
- For other environment variables, see the original manual [Wireguard Amnesia](https://github.com/w0rng/amnezia-wg-easy) or [Wireguard](https://github.com/wg-easy/wg-easy).

## Creating OpenVpn client certificates: 
https://github.com/d3vilh/openvpn-ui?tab=readme-ov-file#generating-ovpn-client-profiles
1) go to `http://%your_ip%:8080/certificates`
2) click "create certificate"
3) enter unique name. Leave all other fields empty
4) click create
5) click on certificate name in list to download ovpn file. 

## Extra information
- [OpenWrt setup guide](./docs/guide_OpenWrt.md) - how to setup OpenWrt router with this solution to keep LAN clients happy.
- [Keenetic setup guide](./docs/guide_Keenetic.md) - instructions for configuring the server and connecting Keenetic routers to it [(на русском языке)](./docs/guide_Keenetic_RU.md)

## Enable OpenVPN Data Channel Offload (DCO)
[OpenVPN Data Channel Offload (DCO)](https://openvpn.net/as-docs/openvpn-dco.html) provides performance improvements by moving the data channel handling to the kernel space, where it can be handled more efficiently and with multi-threading.
**tl;dr** it increases speed and reduces CPU usage on a server.

Kernel extensions can be installed only on <u>a host machine</u>, not in a container.

### Ubuntu 24.04
```bash
sudo apt update
sudo apt upgrade
echo "#### Please reboot your system after upgrade ###" && sleep 100
sudo apt install -y efivar
sudo apt install -y openvpn-dco-dkms
```

### Ubuntu 20.04, 22.04
```bash
sudo apt update
sudo apt upgrade 
echo "#### Please reboot your system after upgrade ###" && sleep 100
deb=openvpn-dco-dkms_0.0+git20231103-1_all.deb
sudo apt install -y efivar dkms linux-headers-$(uname -r)
wget http://archive.ubuntu.com/ubuntu/pool/universe/o/openvpn-dco-dkms/$deb
sudo dpkg -i $deb
```

## Legacy clients support

If your clients do not have GCM ciphers support you can use legacy CBC ciphers.
DCO is incompatible with legacy ciphers and will be disabled. This is also increase CPU load.

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
- [No Thought Is a Crime](https://ntc.party) — a forum about technical, political and economical aspects of internet censorship in different countries
