# Keenetic router manual:
- [Keenetic router manual:](#keenetic-router-manual)
  - [OpenVPN](#openvpn)
    - [OpenVPN server part](#openvpn-server-part)
    - [OpenVPN client part](#openvpn-client-part)
  - [WireGuard](#wireguard)
    - [WireGuard server part](#wireguard-server-part)
    - [WireGuard client part](#wireguard-client-part)
  - [IPsec](#ipsec)
    - [IPsec server side](#ipsec-server-side)
    - [IPsec client side](#ipsec-client-side)

## OpenVPN
### OpenVPN server part
No special steps are required, follow [instructions](https://github.com/xtrime-ru/antizapret-vpn-docker?tab=readme-ov-file#installation).
### OpenVPN client part
1. Install [OpenVPN client](https://help.keenetic.com/hc/en-us/articles/360000632239-OpenVPN-client)
2. In the `antizapret-client-client-tcp.ovpn` or `antizapret-client-udp.ovpn` OpenVPN configuration file, add the lines:
    ```
    pull-filter ignore block-outside-dns
    route 77.88.8.8
    ```
3. Add an OpenVPN connection under `Internet` > `Other Connections` > `VPN Connections` > `Add Connection`.
   1. Use to connect to the Internet: ``NONE''.
   2. connection name: `AntiZapret`.
   3 Type (protocol): `OpenVPN`.
   4. Receive routes from the remote party: `YES`.
   5. OpenVPN configuration: `Content file from item 2`.
   6. `Save`.
4. `Network Rules` > `Internet Filters`.
    1. DNS Settings > Add Profile
       1. profile name: `AntiZapret`.
       2. query transit: `NONE`.
       3. `Save`.
       4. `Add Server`.
          1. DNS server type: `Default`.
          2. IP address: `77.88.8.8`.
          3. `Save`.
    2. `Content Filter`.
         1. Filtering mode: `Public DNS resolvers`.
         2. Default content filtering profiles (`guest` and `home`):`AntiZapret`.
5. Under `Internet` > `Other Connections` enable `AntiZapret` connection.

**Done!**
## WireGuard
> [!WARNING]  
> Amnezia Wireguard requires firmware version **4.2+** to work.
> Until 4.2 is introduced in the stable branch, you can use regular WireGuard on port 443.
> But it may not work for everyone, I recommend using Amnezia.
### WireGuard server part
1. Install [Docker Engine](https://docs.docker.com/engine/install/):
    ```shell
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    ```
    
2. Clone the repository:
    ```shell
    git clone https://github.com/xtrime-ru/antizapret-vpn-docker.git antizapret
    cd antizapret
    ```
3. Generate password for wireguard admin panel:
    ```shell
    docker run --rm ghcr.io/wg-easy/wg-easy wgpw 'YOUR_PASSWORD' | sed "s/'//g" | sed -r 's/\$/\$\$/g' | tee ./wireguard/wireguard.env
    ```
4. Create a file `docker-compose.override.yml` with the following content:
    > Pay attention to WireGuard's assigned port! 443 bypasses blocking better, but may be considered by your hoster as an attempted DDoS attack on your server. If you experience any lags or connection failures, change 443 to another port first!
    ```yaml
    services:
      antizapret-vpn:
        environment:
          - DNS=adguardhome
          - ADGUARD=1
          - OPENVPN_OPTIMIZATIONS=1
          - OPENVPN_TLS_CRYPT=1
          # Port for OpenVPN
          - OPENVPN_PORT=6841
        ports:
          # Port for OpenVPN (you can delete ports if you don't need them)
          - "6841:1194/tcp"
          - "6841:1194/udp"
        depends_on:
          - adguardhome
      adguardhome:
        extends:
          file: docker-compose.adguard.yml
          service: adguardhome
          container_name: adguardhome
        ports:
          # Port for installation, can be deleted afterwards
          - "6844:3000/tcp"
          # Port for AdGuard control panel
          - "6845:80/tcp"
      
      # For Amnezia, replace with
      # amnezia-wg-easy
      wg-easy:

        environment:
          # Allow routing of all IP addresses, routes are manually added to the router anyway
          # This way you can use the connection for a full VPN
          - WG_ALLOWED_IPS=0.0.0.0.0/0
          # Forced redirection of all DNS (udp 53) to antizapret
          # In keenetic, route 77.88.8.8.8 (or any other DNS) to WG gateway (added automatically)
          # When WG goes down, DNS works directly.
          - FORCE_FORWARD_DNS=true
          # Language
          - LANGUAGE=en
          # Port for WireGuard control panel
          - PORT=6843
          # WireGuard port
          - WG_PORT=443
        ports:
          # Port for WireGuard control panel
          - "6843:6843/tcp"
          # WireGuard port
          - "443:443/udp"
        extends:
          
          # For Amnezia, replace with
          # file: docker-compose.wireguard-amnezia.yml
          file: docker-compose.wireguard.yml
          
          # For Amnezia, replace with
          # service: amnezia-wg-easy
          service: wg-easy
    ```
5. Assemble the container:
   ```shell
   docker compose pull
   docker compose up -d
   ```
6. Installing AdGuard Home
   1. Installation script: `http://<SERVER_IP>:6844`.
   2. Control Panel: `http://<SERVER_IP>:6845`.
7. Create a profile in Wireguard: `http://<SERVER_IP>:6843`.
8. Download the profile and go to the client part
### WireGuard client part
1. [Install the "WireGuard VPN" component](https://help.keenetic.com/hc/en-us/articles/360010592379-WireGuard-VPN)
2. Load the profile downloaded from the panel `Internet` > `Other Connections` > `Wireguard` > `Download from file`.
3. open the downloaded connection and check `Use for Internet access`.
   (Optional, change the name to `Antizapret`).
4. `Network Rules` > `Routing`.
   1. `Add route`.
      1. route type: `Route to node`.
      2. Description: `AntiZapretDNS`.
      3. destination node address: `77.88.8.8`
      4. gateway address: `empty`.
      5. Interface: `Antizapret` (if you did not change the name, by file name)
   2. `Add Route`.
      1. route type: `Route to network`.
      2 Description: `AntiZapret`.
      2. destination network address: `10.224.0.0`
      3. Subnet mask: `255.254.0.0.0/15`.
      4. Gateway address: `blank`.
      5. Interface: `Antizapret` (if you did not change the name, then by file name)
5. `Network Rules` > `Internet Filters`.
    1. DNS Setup > Add Profile.
       1. profile name: `AntiZapret`.
       2. query transit: `NONE`.
       3. `Save`.
       4. `Add Server`.
          1. DNS server type: `Default`.
          2. IP address: `77.88.8.8`.
          3. `Save`.
    2. `Content Filter`.
         1. Filtering mode: `Public DNS resolvers`.
         2. Default content filtering profiles (`guest` and `home`): `AntiZapret`.
> [!NOTE]  
> If using Amnezia Wireguard, there are a few more steps to follow
[instructions](https://docs.amnezia.org/documentation/instructions/keenetic-os-awg)
starting at step 20. I'll briefly duplicate it here.
1. Go to settings, click on the gear image in the upper right corner of the web page, and click on `Command Line` link.
2. Send a request: `show interface`.
3. Now we need to find out the name of the desired interface, by the name of the previously created connection. To do this, open a search on the page (you can do this by pressing two keys simultaneously, Ctrl+F). Enter for the search, the name of the previously created connection. In this example, it is `AntiZapret` . One, unique name should be found in the `description` field. And next to it, there will be another field, `interface-name`, which displays the name of the desired interface. In this example, it is `Wireguard1`.
4. Now, knowing the interface name and the values of the asc parameters from the .conf file we saved earlier. We need to replace all the template values in brackets with your values, and delete the brackets themselves.

    `interface {name} wireguard asc {jc} {jmin} {jmax} {s1} {s2} {h1} {h2} {h3} {h4}`

    To give an example, you get a string like this:

    `interface Wireguard1 wireguard asc 8 50 1000 30 32 1811016522 1196729875 457766807 1765857463`.

    The resulting string should be pasted into the web version of the router's command line, and the "Send Request" button should be clicked.
5. Send the request: `system configuration save`.

In the `Internet` > `Other connections` section, enable the `AntiZapret` connection.

**Done!**
## IPsec
### IPsec server side
In the process of writing
### IPsec client side
In progress