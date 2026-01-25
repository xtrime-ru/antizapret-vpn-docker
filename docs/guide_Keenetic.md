# Keenetic router manual:
- [Keenetic router manual:](#keenetic-router-manual)
  - [OpenVPN](#openvpn)
    - [OpenVPN client part](#openvpn-client-part)
  - [WireGuard](#wireguard)
    - [WireGuard client part](#wireguard-client-part)

## OpenVPN

This is most usable and reliable way to bypass blockings.

For better OpenVPN performance, new Keenetic routers with fast processors (from 1 GHz) and large amounts of RAM (from 256 MB) are recommended: Peak (KN-2710), Giga (KN-1012), Hopper (KN-3811/3812), Sprinter (KN-3711/3712), Challenger SE (KN-3911) и Ultra (KN-1811). **Give attention the model number.**

For example: on old City KN-1511 bandwidth speed through server is limited at 6-8 Mbps, but on new Hopper KN-3811 speed reaches 55-60 Mbit/s

Detailed information about the different models and OpenVPN speeds you can found at [manufacturer's website](https://help.keenetic.com/hc/en-us/articles/115005342025-VPN-types-in-Keenetic-routers).

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
> But it may not work for everyone, using Amnezia is recommended.

### WireGuard client part
1. [Install the "WireGuard VPN" component](https://help.keenetic.com/hc/en-us/articles/360010592379-WireGuard-VPN)
2. Load the profile downloaded from the panel `Internet` > `Other Connections` > `WireGuard` > `Import from a file`.
3. Open imported connection and check `Use for accessing the Internet`, change the name to `Antizapret` (optional).
4. Add `77.88.8.8/32` to `Allowed v4 IPs`.
5. `Network Rules` > `Routing`.
   1. `Create route`.
      1. Route type: `Route to host`.
      2. Description: `AntiZapretDNS`.
      3. Destination host address: `77.88.8.8`
      4. Gateway IP: `10.1.166.2` (`IPv4 address` from the `Antizapret` connection options)
      5. Interface: `Antizapret` (if you did not change the name, by file name)
      6. Enable checkbox `Add automatically`
   2. `Create route`.
      1. Route type: `Route to network`.
      2. Description: `AntiZapret`.
      3. Destination network address: `14.16.0.0`
      4. Subnet mask: `255.252.0.0/14`.
      5. Gateway IP: `blank`.
      6. Interface: `Antizapret` (if you did not change the name, then by file name)
   3. Similarly, add routes to all subnets specified in `Allowed v4 IPs` in the `Antizapret` connection options.
6. `Network Rules` > `Internet Safety` > `DNS Configuration`.
   1. Profile name: `System`.
   2. Transit requests: `NO`.
   3. `Save`.
   4. `Add Server`.
      1. DNS server type: `Default`.
      2. DNS server address: `77.88.8.8`.
      3. `Save`.
7. `Internet` > `Ethernet Cable`
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

> [!NOTE]
> If any of the client devices does not require blocking bypass, then you should [register it](https://help.keenetic.com/hc/en-us/articles/360000394159-Client-devices-registration), [create a DNS profile](https://help.keenetic.com/hc/en-us/articles/7248035195548-Creating-a-DNS-profile-without-filtering), add some public DNS servers there (different from those you added to the `System` profile), enable the `Public DNS resolvers` filtering mode in the `Content Filter` section and assign the newly created DNS profile to this client below on this page.
