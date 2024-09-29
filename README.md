# AntiZapret VPN in Docker

Easy-to-use Docker image based upon original [AntiZapret LXD image](https://bitbucket.org/anticensority/antizapret-vpn-container/src/master/) for self-hosting.


# Improvements

- Patches: [Apple](./rootfs/etc/knot-resolver/kresd.conf#L53-L61), [IDN](./rootfs/root/patches/parse.patch#L16), [RU](./rootfs/etc/knot-resolver/kresd.conf#L117)
- [Community-driven list](https://github.com/xtrime-ru/antizapret-vpn-docker/blob/master/rootfs/root/antizapret/config/include-hosts-dist.txt) with geoblocked and unlisted domains: YouTube, Microsoft, OpenAI and more
- [openvpn-dco](https://openvpn.net/as-docs/tutorials/tutorial--turn-on-openvpn-dco.html) - a kernel extension for improving performance
- Option to [forwarding queries](./rootfs/init.sh#L21-L35) to an external resolver, aka Adguard support.
- [Support regex in custom rules](#adding-domainsips)
- [XOR Tunneblick patch](https://tunnelblick.net/cOpenvpn_xorpatch.html)
- Multiple VPN transports: Wireguard, OpenVPN, IPsec/XAuth ("Cisco IPsec")


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
   docker compose pull
   docker compose up -d
   ```
2. Download keys  
After start of the container folders `./keys/client` and `./configs` will be created. 
Download `.ovpn` configs from `./keys/client` directory and use to setup your clients.
There will be UDP and TCP configurations.
Use UDP for better performance.
Use TCP in unstable conditions.

## Wireguard server

1. Generate password for wireguard admin panel
    ```shell
    docker run --rm ghcr.io/wg-easy/wg-easy wgpw 'YOUR_PASSWORD' | sed "s/'//g" | sed -r 's/\$/\$\$/g' | tee ./wireguard/wireguard.env
    ```
2. Start container
    ```shell
    docker compose -f docker-compose.wireguard.yml pull
    docker compose -f docker-compose.wireguard.yml up -d
    ```
3. Open `http://YOUR_SERVER_IP:51821` and create new client

## IPsec/XAuth (Cisco IPsec) server
**Important notice**: not all clients support tunnel-split (send only part of traffic via VPN).
For example **Apple** devices **will not** be able **to connect** to this server. 

**Its recomended to use OpenVPN or Wireguard instead.**

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

## Adguard
Antizapret-VPN can use external DNS resolvers. 
To start your own adguard docker container and use it as backend for antizapret:
```shell
docker compose down
docker compose -f docker-compose.adguard.yml up -d
```

Go to `http://YOUR_SERVER_IP:3000` and setup adguard. 
You can leave all values default. Except port for adguard. Change it from 80 to 3000

## Customize containers
Its recommended not to change docker-compose files, because it can break ability to git pull updates.

The correct way - is to create [docker-compose.override.yml](https://docs.docker.com/compose/multiple-compose-files/merge/).

For example you want all transports and adguard, and modify env variables of antizapret-vpn:
```yml
services:
  antizapret-vpn:
    environment:
      - DNS=adguardhome
      - ADGUARD=1
      - OPENVPN_OPTIMIZATIONS=1
      - OPENVPN_TLS_CRYPT=1
    depends_on:
      - adguardhome
  adguardhome:
    extends:
      file: docker-compose.adguard.yml
      service: adguardhome
  ipsec:
    extends:
      file: docker-compose.ipsec.yml
      service: ipsec
  amnezia-wg-easy:
    extends:
      file: docker-compose.wireguard-amnezia.yml
      service: amnezia-wg-easy
```

`docker compose` will merge `docker-compose.yml` and your custom `docker-compose.override.yml`.

Start all containers from `docker-compose.override.yml`:
```shell
docker compose down && docker compose pull && docker compose up -d
```


## Update:

```shell
git pull
docker compose pull
docker compose down && docker compose up -d
```




# Documentation

## Adding Domains/IPs
Any domains or IPs can be added or excluded from routing with config files from `./config` directory.
These lists are added/excluded to/from automatically generated lists of domains and IP's.
Reboot container and wait few minutes for applying changes.

Its recommended to use `*-regex-custom.txt` files.
You can debug your regular expressions online: https://regex101.com
Here is few regex example:
1. Exact match:
	```regexp
	^2ip\.ru$
	```
1. Subdomains only:
	```regexp
	\.microsoft\.com$
	```
    Will match any subdomain from microsoft.com. Both regular works same way.
1. List of first level domains:
	```regexp
	microsoft\.[^.]*$
	microsoft\.(ru|com|com\.de)
	```

## Keys/Configs Persistence

Client and server keys are stored in `./keys`.
They are persistent between container and host restarts.

To regenerating the keys use the following commands:
```shell
docker compose down
rm -rf keys/{client,server}/keys/*.{crt,key}
docker compose up -d
```

## Environment Variables

You can define these variables in docker-compose.yml file for your needs:

- `SKIP_UPDATE_FROM_ZAPRET=true` - do not download and use list of all blocked domains from internet. 
	Will reduce RAM consumption. Need to manually fill domains in `*-custom.txt` files.
- `UPDATE_TIMER=1d` - blocked domains update interval
- `OPENVPN_HOST=example.com` — will be used as a server address in .ovpn profiles upon keys generation (default: your server's IP)
- `OPENVPN_PORT=1194` — will be used as a server port in .ovpn profiles upon keys generation. (default: 1194)
  Also port need to be changed manually in [docker-compose.yml](./docker-compose.yml#L21-L22).
  Replace `%EXTERNAL_PORT%` with port number,
  and dont change internal port, because this variable do not override openvpn server configs:
  ```yml
  ports:
      - %EXTERNAL_PORT%:1194/tcp
      - %EXTERNAL_PORT%:1194/udp
  ```
- `OPENVPN_MTU=1420` - Set tun-mtu option with fixed value. (default: auto)
- `OPENVPN_OPTIMIZATIONS=1` - Enable tcp-nodelay, fast-io options and invrease sndbuf and rcvbuf. (default: 0)
- `OPENVPN_CBC_CIPHERS=1` - Enable support of [legacy clients](#legacy-clients). WIll disable [DCO](#enable-openvpn-data-channel-offload-dco)
- `OPENVPN_SCRAMBLE=1` - Enable additional obfuscation [XOR Tunneblick patch](https://tunnelblick.net/cOpenvpn_xorpatch.html)
- `OPENVPN_TLS_CRYPT=1` - Enable additional TLS encryption in OpenVPN. May help with connection obfuscation.
- `OPENVPN_TCP_SUBNET=192.168.108.0/22` - Customize TCP subnet for OpenVPN connection, in case of conflict with your network settings. Default: 192.168.104.0/22
- `OPENVPN_UDP_SUBNET=192.168.60.0/22` - Customize UDP subnet for OpenVPN connection, in case of conflict with your network settings. Default: 192.168.100.0/22
- `DNS=1.1.1.1` — DNS server to resolve domains (default: host DNS server)
- `DNS_RU=77.88.8.8` — Russian DNS server; used to fix issues with geo zones mismatch for domains like `apple.com` (default: 77.88.8.8)
- `LOG_DNS=1` - Log all DNS requests and responses (default: 0)
- `ADGUARD=1` - Resolve .ru, .рф and .su via DNS. By default, this zones resolved through DNS_RU. (default: 0)

### Environment Variables for Wireguard and Wireguard Amnesia:
- `FORCE_FORWARD_DNS=true` - Redirects UDP traffic on port 53 to AntiZapret DNS (default: false)
- `FORCE_FORWARD_DNS_PORTS="53 5353"` - Parameter can be used to change port 53 for FORCE_FORWARD_DNS to one or more, separated by a space (default: 53)
- For other environment variables, see the original manual [Wireguard Amnesia](https://github.com/w0rng/amnezia-wg-easy) or [Wireguard](https://github.com/wg-easy/wg-easy).

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
1. Set ENV variable `CBC_CIPHERS=1` in docker-compose.yml.
2. Restart container.
3. Download and apply updated .ovpn files from `keys/client/` folder.

## Test speed with iperf3
iperf3 server is included in antizapret-vpn container. 
1. Connect to VPN
2. Use iperf3 client on your phone or computer to check upload/download speed. 
	Example 10 threads for 10 seconds and report result every second:
	```shell
	iperf3 -c 10.224.0.1 -i1 -t10 -P10
	iperf3 -c 10.224.0.1 -i1 -t10 -P10 -R
	```

# Credits
- [ProstoVPN](https://antizapret.prostovpn.org) — the original project
- [AntiZapret VPN Container](https://bitbucket.org/anticensority/antizapret-vpn-container/src/master/) — source code of the LXD-based container
- [AntiZapret PAC Generator](https://bitbucket.org/anticensority/antizapret-pac-generator-light/src/master/) — proxy auto-configuration generator to bypass censorship of Russian Federation
- [Amnezia WireGuard VPN](https://github.com/w0rng/amnezia-wg-easy) — used for Amnezia Wireguard integration
- [WireGuard VPN](https://github.com/wg-easy/wg-easy) — used for Wireguard integration
- [IPsec VPN](https://github.com/hwdsl2/docker-ipsec-vpn-server) — used for IPsec integration
- [No Thought Is a Crime](https://ntc.party) — a forum about technical, political and economical aspects of internet censorship in different countries
