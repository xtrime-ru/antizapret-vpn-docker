# AntiZapret VPN in Docker

Easy-to-use Docker image based upon original [AntiZapret LXD image](https://bitbucket.org/anticensority/antizapret-vpn-container/src/master/) for self-hosting.


# Improvements

- Patches: [Apple](./rootfs/etc/knot-resolver/kresd.conf#L53-L61), [IDN](./rootfs/root/patches/parse.patch#L16), [RU](./rootfs/etc/knot-resolver/kresd.conf#L117)
- [Community-driven list](https://github.com/xtrime-ru/antizapret-vpn-docker/blob/master/rootfs/root/antizapret/config/include-hosts-dist.txt) with geoblocked and unlisted domains: YouTube, Microsoft, OpenAI and more
- [openvpn-dco](https://openvpn.net/as-docs/tutorials/tutorial--turn-on-openvpn-dco.html) - a kernel extension for improving performance
- Option to [forwarding queries](./rootfs/init.sh#L21-L35) to an external resolver
- [Support regex in custom rules](#adding-domainsips)
- [XOR Tunneblick patch](https://tunnelblick.net/cOpenvpn_xorpatch.html)


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

- `DOMAIN=example.com` — will be used as a server address in .ovpn profiles upon keys generation (default: your server's IP)
- `PORT=1194` — will be used as a server port in .ovpn profiles upon keys generation (default: 1194)
- `DNS=1.1.1.1` — DNS server to resolve domains (default: host DNS server)
- `DNS_RU=77.88.8.8` — Russian DNS server; used to fix issues with geo zones mismatch for domains like `apple.com`
- `ADGUARD=1` - Resolve .ru, .рф and .su via DNS. By default, this zones resolved through DNS_RU.
- `CBC_CIPHERS=1` - Enable support of [legacy clients](#legacy-clients). WIll disable [DCO](#enable-openvpn-data-channel-offload-dco) 
- `SCRAMBLE=1` - Enable additional obfuscation [XOR Tunneblick patch](https://tunnelblick.net/cOpenvpn_xorpatch.html) 
- `TLS_CRYPT=1` - Enable additional TLS encryption in OpenVPN. May help with connection obfuscation.

## Extra information
[OpenWrt setup guide](./docs/guide_OpenWrt.md) - how to setup OpenWrt router with this solution to keep LAN clients happy.

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

### Ubuntu 20.04
```bash
deb=openvpn-dco-dkms_0.0+git20231103-1_all.deb
sudo apt update
sudo apt upgrade 
echo "#### Please reboot your system after upgrade ###" && sleep 100
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

# Credits
- [ProstoVPN](https://antizapret.prostovpn.org) — the original project
- [AntiZapret VPN Container](https://bitbucket.org/anticensority/antizapret-vpn-container/src/master/) — source code of the LXD-based container
- [AntiZapret PAC Generator](https://bitbucket.org/anticensority/antizapret-pac-generator-light/src/master/) — proxy auto-configuration generator to bypass censorship of Russian Federation
- [No Thought Is a Crime](https://ntc.party) — a forum about technical, political and economical aspects of internet censorship in different countries
