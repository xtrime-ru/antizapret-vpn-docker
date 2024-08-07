FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /root
RUN apt update -y && apt upgrade -y \
    && apt install -y python3-dnslib \
		ipcalc \
		sipcalc \
		gawk \
		idn \
		iptables \
		ferm \
		openvpn \
		inetutils-ping \
		curl \
		wget \
		ca-certificates \
		openssl \
		host \
		dnsutils \
		bsdmainutils \
		procps \
		unattended-upgrades \
		nano \
		vim-tiny \
		git \
    	gpg \
    # install knot-resolver \
    && echo 'deb http://download.opensuse.org/repositories/home:/CZ-NIC:/knot-resolver-latest/xUbuntu_22.04/ /' | tee /etc/apt/sources.list.d/home:CZ-NIC:knot-resolver-latest.list \
    && curl -fsSL https://download.opensuse.org/repositories/home:CZ-NIC:knot-resolver-latest/xUbuntu_22.04/Release.key | gpg --dearmor | tee /etc/apt/trusted.gpg.d/home_CZ-NIC_knot-resolver-latest.gpg > /dev/null \
    && apt update -y \
    && apt install -y knot-resolver \
    && apt -o Dpkg::Options::="--force-confold" -y full-upgrade \
    && chmod 1777 /tmp \
    && apt autoremove -y && apt clean

RUN mkdir "/root/antizapret" \
    && cd /root \
	&& git clone https://bitbucket.org/anticensority/antizapret-vpn-container.git \
    && cd antizapret-vpn-container/ \
    && mv -f mkosi/mkosi.extra/etc/ferm/* /etc/ferm/ \
    && mv -f mkosi/mkosi.extra/etc/knot-resolver/* /etc/knot-resolver/ \
    && mv -f mkosi/mkosi.extra/etc/openvpn/server/*.conf /etc/openvpn/server/ \
    && mkdir /etc/openvpn/server/keys/ \
    && mkdir /etc/openvpn/server/ccd/ \
    && mkdir /etc/openvpn/server/logs/ \
    && mv -f mkosi/mkosi.extra/etc/openvpn/server/keys/* /etc/openvpn/server/keys/ \
    && mv -f mkosi/mkosi.extra/etc/sysctl.d/* /etc/sysctl.d/ \
    && mv -f mkosi/mkosi.extra/etc/systemd/journald.conf /etc/systemd/journald.conf \
    && mv -f mkosi/mkosi.extra/etc/systemd/network/* /etc/systemd/network/ \
    && mv -f mkosi/mkosi.extra/etc/systemd/system/* /etc/systemd/system/ \
    && mv -f mkosi/mkosi.extra/root/* /root/ \
    && cd /root/ \
    && rm -rf /root/antizapret-vpn-container \
    && git clone https://bitbucket.org/anticensority/antizapret-pac-generator-light.git antizapret \
    && mv -f /root/antizapret-process.sh antizapret/process.sh \
	&& systemctl enable systemd-networkd \
                     kresd@1 \
                     antizapret-update.service antizapret-update.timer \
                     dnsmap openvpn-generate-keys \
                     openvpn-server@antizapret openvpn-server@antizapret-tcp

# Build openvpn with dco support
RUN apt install -y openvpn-dco-dkms \
	&& apt autoremove -y && apt clean

ADD patches/ /root/antizapret/patches
RUN cp -rf /root/antizapret/patches/etc/openvpn/server/*.conf /etc/openvpn/server/ \
    && cp -rf /root/antizapret/patches/root/antizapret/process.sh /root/antizapret/process.sh \
    && cp -rf /root/antizapret/patches/root/dnsmap/* /root/dnsmap/ \
    && cp -rf /root/antizapret/patches/etc/knot-resolver/kresd.conf /etc/knot-resolver/kresd.conf \
    && cp -rf /root/antizapret/patches/root/easy-rsa-ipsec/templates/*.conf /root/easy-rsa-ipsec/templates/ \
	&& cd /root/antizapret/ \
    && chmod +x patches/*.sh \
	&& git pull && ./patches/fix.sh

COPY ./init.sh /
ENTRYPOINT ["/init.sh"]
