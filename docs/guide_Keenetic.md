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

This is most usable and reliable way to bypass blockings.

For better OpenVPN performance, new Keenetic routers with fast processors (from 1 GHz) and large amounts of RAM (from 256 MB) are recommended: Peak (KN-2710), Giga (KN-1012), Hopper (KN-3811/3812), Sprinter (KN-3711/3712), Challenger SE (KN-3911) и Ultra (KN-1811). **Give attention the model number.**

For example: on old City KN-1511 bandwidth speed through server is limited at 6-8 Mbps, but on new Hopper KN-3811 speed reaches 55-60 Mbit/s

Detailed information about the different models and OpenVPN speeds you can found at [manufacturer's website](https://help.keenetic.com/hc/en-us/articles/115005342025-VPN-types-in-Keenetic-routers).

### OpenVPN server part
No special steps are required, follow [instructions](https://github.com/xtrime-ru/antizapret-vpn-docker?tab=readme-ov-file#installation). Also, you need [create client certificate](https://github.com/xtrime-ru/antizapret-vpn-docker?tab=readme-ov-file#create-client-certificates).

### OpenVPN client part
1. Install [OpenVPN client](https://help.keenetic.com/hc/en-us/articles/360000632239-OpenVPN-client)
2. In the OpenVPN configuration file, add the lines:
    ```
    pull-filter ignore block-outside-dns
    route 77.88.8.8
    ```
3. Add an OpenVPN connection under `Internet` > `Other Connections` > `VPN Connections` > `Create Connection`.
   1. Use for accessing the Internet: `NO`.
   2. Connection name: `AntiZapret`.
   3. Type (protocol): `OpenVPN`.
   4. Obtain routes from the remote side: `YES`.
   5. OpenVPN configuration: `Content file from item 2`.
   6. `Save`.
4. `Network Rules` > `Internet Safety`.
   1. `DNS Configuration` > `Add Profile`.
      1. Profile name: `AntiZapret`.
      2. Transit requests: `NO`.
      3. `Save`.
      4. `Add Server`.
         1. DNS server type: `Default`.
         2. DNS server address: `77.88.8.8`.
         3. `Save`.
   2. `Content Filter`.
      1. Filtering mode: `Public DNS resolvers`.
      2. Default Content Filtering Profiles (`guest` and `home`): `AntiZapret`.
5. Under `Internet` > `Other Connections` enable `AntiZapret` connection.

**Done!**

## WireGuard
> [!WARNING]
> Amnezia WireGuard requires firmware version **4.2+** to work.
> For firmware lower than 4.2 you can use regular WireGuard on port 443.
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
3. Create a file `docker-compose.override.yml` with the following content:
    > Pay attention to WireGuard's assigned port! 443 bypasses blocking better, but may be considered by your hoster as an attempted DDoS attack on your server. If you experience any lags or connection failures, change 443 to another port first!
    ```yaml
    services:
      antizapret:
        environment:
          # Username for AdGuard Home (set yours!)
          - ADGUARDHOME_USERNAME=user
          # Password for AdGuard Home (set yours!)
          - ADGUARDHOME_PASSWORD=somestrongpassword
      # For Amnezia, replace with
      # wireguard-amnezia
      wireguard:
        environment:
          # Password for WireGuard control panel (set yours!)
          - WIREGUARD_PASSWORD=somestrongpassword
          # Allow routing of all IP addresses, routes are manually added to the router anyway
          # This way you can use the connection for a full VPN
          - WG_ALLOWED_IPS=0.0.0.0.0/0
          # Forced redirection of all DNS (udp 53) to antizapret
          # In keenetic, route 77.88.8.8.8 (or any other DNS) to WG gateway (Add automatically)
          # When WG goes down, DNS works directly.
          - FORCE_FORWARD_DNS=true
          # Language
          - LANG=en
          # Port for WireGuard control panel
          - PORT=51821
          # WireGuard port
          - WG_PORT=443
        ports:
          # Port for WireGuard control panel
          - "51821:51821/tcp"
          # WireGuard port
          - "443:443/udp"
        extends:
          # For Amnezia, replace with
          # file: docker-compose.wireguard-amnezia.yml
          file: docker-compose.wireguard.yml
          # For Amnezia, replace with
          # service: wireguard-amnezia
          service: wireguard
    ```
4. Assemble the container:
   ```shell
   docker compose pull
   docker compose build
   docker compose up -d
   ```
5. Create a profile in WireGuard: `http://<SERVER_IP>:6843`.
6. Download the profile and go to the client part

> [!NOTE]
> You can change additional settings in AdGuard Home Control Panel: `http://<SERVER_IP>:3000`

### WireGuard client part
1. [Install the "WireGuard VPN" component](https://help.keenetic.com/hc/en-us/articles/360010592379-WireGuard-VPN)
2. Load the profile downloaded from the panel `Internet` > `Other Connections` > `WireGuard` > `Import from a file`.
3. Open imported connection and check `Use for accessing the Internet`, change the name to `Antizapret` (optional).
4. `Network Rules` > `Routing`.
   1. `Create route`.
      1. Route type: `Route to host`.
      2. Description: `AntiZapretDNS`.
      3. Destination host address: `77.88.8.8`
      4. Gateway IP: `empty`.
      5. Interface: `Antizapret` (if you did not change the name, by file name)
      6. Enable checkbox `Add automatically`
   2. `Create Route`.
      1. Route type: `Route to network`.
      2. Description: `AntiZapret`.
      3. Destination network address: `10.224.0.0`
      4. Subnet mask: `255.254.0.0.0/15`.
      5. Gateway IP: `blank`.
      6. Interface: `Antizapret` (if you did not change the name, then by file name)
5. `Network Rules` > `Internet Safety` > `DNS Configuration`.
   1. Profile name: `System`.
   2. Transit requests: `NO`.
   3. `Save`.
   4. `Add Server`.
      1. DNS server type: `Default`.
      2. DNS server address: `77.88.8.8`.
      3. `Save`.
6. `Internet` > `Ethernet Cable`
   1. Find your active ISP connect:
      1. Enable checkbox `Ignore DNSv4 from ISP`
      2. Enable checkbox `Ignore DNSv6 from ISP`

> [!NOTE]
> If using Amnezia Wireguard, there are a few more steps to follow
[instructions](https://docs.amnezia.org/documentation/instructions/keenetic-os-awg)
starting at step 20. I'll briefly duplicate it here.

1. Go to settings, click on the gear image in the upper right corner of the web page, and click on `Command Line` link.
2. Send a request: `show interface`.
3. Now we need to find out the name of the desired interface, by the name of the previously created connection. To do this, open a search on the page (you can do this by pressing two keys simultaneously, Ctrl+F). Enter for the search, the name of the previously created connection. In this example, it is `AntiZapret` . One unique name should be found in the `description` field. And next to it there will be another field, `interface-name`, which displays the name of the desired interface. In this example, it is `Wireguard1`.
4. Now, knowing the interface name and the values of the asc parameters from the .conf file we saved earlier. We need to replace all the template values in brackets with your values, and delete the brackets themselves.

    `interface {name} wireguard asc {jc} {jmin} {jmax} {s1} {s2} {h1} {h2} {h3} {h4}`

    To give an example, you get a string like this:

    `interface Wireguard1 wireguard asc 8 50 1000 30 32 1811016522 1196729875 457766807 1765857463`.

    The resulting string should be pasted into the web version of the router's command line, and the "Send Request" button should be clicked.
5. Send the request: `system configuration save`.

In the `Internet` > `Other Connections` section, enable the `AntiZapret` connection.

**Done!**

## IPsec
### IPsec server side
In the process of writing

### IPsec client side
In progress
