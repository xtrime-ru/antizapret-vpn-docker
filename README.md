# antizapret-vpn-docker
Easy-to-start docker container with antizapret-vpn for selfhosting.

## About
Docker image converted from original LXD image.

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

## Keys menagment
Server keys are stored in easyrsa3/pki/ folder and client keys are copied to client_keys/. 
Keys are persistent between container and host restarts.

To generate new keys remove files and start container again:
```shell
docker-compose down
rm -rf easyrsa3/pki/
rm -rf client_keys/
docker-compose up -d
```

## Links
- Link to original project website: https://antizapret.prostovpn.org
- Repository: https://bitbucket.org/anticensority/antizapret-vpn-container/src/master/
