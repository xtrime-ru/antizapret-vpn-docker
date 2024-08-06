# Antizapret VPN server
Easy-to-start docker container with antizapret vpn server for selfhosting.

## About
Docker image converted from original Atnizapret LXD image. 
Additional fixes were made:
 - [Apple DNS fix](https://github.com/xtrime-ru/antizapret-vpn-docker/blob/master/patches/kresd.conf#L3);
 - [RU domains excluded from antizapret](https://github.com/xtrime-ru/antizapret-vpn-docker/blob/master/patches/kresd.conf#L13);
 - [IDN domains fix](https://github.com/xtrime-ru/antizapret-vpn-docker/blob/master/patches/fix.sh#L5);
 - [Additional domains list](https://github.com/xtrime-ru/antizapret-vpn-docker/blob/master/config/include-hosts-custom.txt);
 - Switch to Ubuntu 24.04 from Debian 10;
 - Upgrade to OpenVPN 2.6+ and install [openvpn-dco](https://openvpn.net/as-docs/tutorials/tutorial--turn-on-openvpn-dco.html) kernel extension for maximum performance;


## Installation
0. Install docker
    ```shell
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    ```

1. Copy this repository, build container, and run it.
    ```shell
    git clone https://github.com/xtrime-ru/antizapret-vpn-docker.git antizapret
    cd antizapret
    docker compose up -d --build
    ```
2. Download configuration file for your openvpn client from `client_keys` folder. 
There will be udp and tcp versions of the config. For better performance use upd.
Tcp version will be better for unstable conditions.

## Update:
 
```shell
git pull 
docker compose build
docker compose up -d
```

## Keys menagment
Server keys are stored in easyrsa3/pki/ folder and client keys are copied to client_keys/. 
Keys are persistent between container and host restarts.

To generate new keys remove files and start container again:
```shell
docker compose down
rm -rf easyrsa3/pki/
rm -rf client_keys/
docker compose up -d
```

## Additional domains
Any domain and/or IP can be added or excluded from list with [config files](https://github.com/xtrime-ru/antizapret-vpn-docker/tree/master/config)
This lists are added/excluded to/from automatically generated lists of domains and IP's. 
To apply changes you need to reboot container after changing this files.


## Links
- Link to original project website: https://antizapret.prostovpn.org
- Repositories:
    - https://bitbucket.org/anticensority/antizapret-vpn-container/src/master/
    - https://bitbucket.org/anticensority/antizapret-pac-generator-light/src/master/
