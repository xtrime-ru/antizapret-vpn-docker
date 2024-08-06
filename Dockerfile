FROM alpine:latest AS downloader
# Docker cant unpack remote archives via ADD command :(
# Lets use multistage build to download and unpack remote archive.
RUN wget https://antizapret.prostovpn.org/container-images/az-vpn/rootfs.tar.xz  \
    && mkdir /rootfs-dir  \
    && tar -xf /rootfs.tar.xz -C /rootfs-dir/

FROM scratch
COPY --from=downloader /rootfs-dir /
RUN wget https://secure.nic.cz/files/knot-resolver/knot-resolver-release.deb --no-check-certificate \
    && dpkg --force-confnew -i knot-resolver-release.deb \
    && rm knot-resolver-release.deb \
    && chmod 1777 /tmp \
    && apt update -y \
    && apt upgrade -y -o Dpkg::Options::="--force-confold" \
    && apt autoremove -y && apt clean

ENV OPENVPN_VERSION=2.6.12
ADD https://swupdate.openvpn.org/community/releases/openvpn-${OPENVPN_VERSION}.tar.gz /root/
RUN cd /root/ \
    && apt install -y build-essential  pkg-config libnl-genl-3-dev liblzo2-dev libcap-ng-dev libssl-dev liblz4-dev libpam0g-dev \
    && tar xfz openvpn-${OPENVPN_VERSION}.tar.gz \
    && cd openvpn-${OPENVPN_VERSION} \
    && ./configure \
    && make -j$(nproc) \
    && make install \
    && rm -rf /root/openvpn-${OPENVPN_VERSION}* \
    && apt purge -y build-essential  pkg-config \
    && apt autoremove -y && apt clean

ADD patches/ /root/antizapret/patches
RUN cd /root/antizapret/ \
    && chmod +x patches/*.sh \
	&& git pull && ./patches/fix.sh

COPY ./init.sh /
ENTRYPOINT ["/init.sh"]
