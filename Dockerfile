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

ADD rootfs /root/rootfs
RUN git clone https://bitbucket.org/anticensority/antizapret-pac-generator-light.git antizapret \
    && cp -rf /root/rootfs/* / \
    \
	&& curl -L https://github.com/OpenVPN/easy-rsa/releases/download/v3.2.0/EasyRSA-3.2.0.tgz | tar -xz \
	&& mv EasyRSA-3.2.0/ /root/easyrsa3/ \
	\
	&& systemctl enable systemd-networkd \
		 kresd@1 \
		 antizapret-update.service antizapret-update.timer \
		 dnsmap \
		 openvpn-server@antizapret openvpn-server@antizapret-tcp

RUN /root/fix.sh \
    && cd /root/antizapret \
    && ./update.sh \
    && ./parse.sh

COPY ./init.sh /
ENTRYPOINT ["/init.sh"]
