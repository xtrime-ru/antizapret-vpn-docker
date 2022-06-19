# antizapret-vpn-docker
Easy to start docker container with antizapret-vpn for selfhosting.

## About
This is original LXD image converted to docker. 

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
    ./build.sh
    docker-compose up -d
    ```
2. Download configuration file for your openvpn client from `client_keys` folder.
    There will be two files: antizapret-client-tcp.ovpn, antizapret-client-udp.ovpn
    You can the one you preffer most.

## Keys menagment
Server keys are stored in easyrsa3/pki/ folder and client keys are copied to client_keys/. 
Keys are persistent betwen container and host restarts.

To generate new keys just remove files and start container again:
```shell
docker-compose down
rm -rf easyrsa3/pki/
rm -rf client_keys/
docker-compose up -d
```

## Links
- Link to original project website: https://antizapret.prostovpn.org
- Repository: https://bitbucket.org/anticensority/antizapret-vpn-container/src/master/
