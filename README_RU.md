[English](README.md) | [Русский](README_RU.md)

# AntiZapret VPN в Docker

Antizapret создан для того, чтобы перенаправлять только заблокированные домены через VPN-туннель. Это называется раздельное туннелирование (split tunneling).
Этот репозиторий основан на идее оригинального [AntiZapret LXD image](https://bitbucket.org/anticensority/antizapret-vpn-container/src/master/).

## Содержание

- [Группа поддержки](#группа-поддержки-и-обсуждений)
- [Возможности](#возможности)
- [Как это работает](#как-это-работает)
- [Установка](#установка)
  - [Один сервер (Просто)](#один-сервер-просто)
  - [Docker Swarm, несколько узлов выхода (Продвинутый)](#docker-swarm-несколько-узлов-выхода-продвинутый)
  - [Блокировка VPN / Хостинга](#блокировка-vpn--хостинга)
  - [После установки](#после-установки)
  - [Доступ к админ-панелям](#доступ-к-админ-панелям)
    - [HTTPS](#https)
    - [Локальная сеть](#локальная-сеть)
    - [HTTP](#http)
  - [Обновление](#обновление)
    - [Обновление с v5](#обновление-с-v5)
  - [Сброс](#сброс)
- [Документация](#документация)
  - [FAQ (Часто задаваемые вопросы)](#faq-часто-задаваемые-вопросы)
  - [Алгоритм разрешения DNS](#алгоритм-разрешения-dns)
  - [Добавление доменов](#добавление-доменов)
    - [Добавление доменов через правила](#добавление-доменов-через-правила)
    - [Добавление доменов через списки](#добавление-доменов-через-списки)
  - [Добавление IP/подсетей](#добавление-ipподсетей)
  - [SOCKS5 прокси (маршрутизация для конкретных приложений)](#socks5-прокси-маршрутизация-для-конкретных-приложений)
    - [Как это работает](#как-это-работает-1)
    - [Когда использовать Dante вместо DNS-маршрутизации](#когда-использовать-dante-вместо-dns-маршрутизации)
    - [Конфигурация](#конфигурация)
    - [Настройка клиента](#настройка-клиента)
    - [Примеры использования](#примеры-использования)
  - [Переменные окружения](#переменные-окружения)
  - [DNS](#dns)
    - [Upstream DNS для Adguard](#upstream-dns-для-adguard)
    - [CDN + ECS](#cdn--ecs)
  - [OpenVPN](#openvpn)
    - [Создание клиентских сертификатов](#создание-клиентских-сертификатов)
    - [Включение OpenVPN Data Channel Offload (DCO)](#включение-openvpn-data-channel-offload-dco)
    - [Поддержка устаревших клиентов](#поддержка-устаревших-клиентов)
  - [Amnezia Wireguard](#amnezia-wireguard)
    - [Включение расширения ядра Amnezia Wireguard](#включение-расширения-ядра-amnezia-wireguard)
    - [Параметры AmneziaWG](#параметры-amneziawg)
    - [Размер блока Amnezia Wireguard](#размер-блока-amnezia-wireguard)
  - [Дополнительная информация](#дополнительная-информация)
  - [Тест скорости с iperf3](#тест-скорости-с-iperf3)
- [Благодарности](#благодарности)

# Группа поддержки и обсуждений:
https://t.me/antizapret_support

# Возможности

- Модульный дизайн. В качестве строительных блоков нашей системы используются внешние высококачественные open-source модули/контейнеры.
- Удобные веб-панели для администрирования VPN и DNS.
- Множество VPN-транспортов: Wireguard, Amnezia Wireguard, OpenVPN.
- AdguardHome в качестве основного DNS-резолвера и менеджера заблокированных доменов.
- Многосерверная архитектура для обхода гео-ограничений сервисов. Разные домены используют разные серверы в качестве узлов выхода.
- Файрвол для защиты от сканирования портов.
- Поддержка модулей ядра для OpenVPN и Amnezia Wireguard для снижения нагрузки на процессор.
- SOCKS5 прокси (Dante) для маршрутизации конкретных приложений через локальные или зарубежные узлы выхода.

# Как это работает?

1) Список заблокированных доменов загружается из открытого реестра.
2) Список парсится и создаются правила для DNS-резолвера (adguardhome).
3) Adguardhome перенаправляет запросы для заблокированных доменов в python-скрипт dnsmap.py.
4) Python-скрипт:
   а) разрешает реальный адрес для домена
   б) создает фейковый адрес из подсети 14.16.0.0/14
   в) создает правило iptables для перенаправления всех пакетов с фейкового ip на реальный ip.
5) Фейковый IP отправляется в DNS-ответе клиенту.
6) VPN-туннели настроены с раздельным туннелированием. Только трафик в подсеть 14.16.0.0/14 маршрутизируется через VPN.

# Установка

## Один сервер (Просто)

Рекомендуется использовать сервер, расположенный в западных странах. Некоторые сайты будут блокировать пользователей из других стран.

0. Установите [Docker Engine](https://docs.docker.com/engine/install/):
   ```bash
   curl -fsSL https://get.docker.com -o get-docker.sh
   sudo sh get-docker.sh
   ```
1. Клонируйте репозиторий и запустите контейнер:
   ```bash
   git clone https://github.com/xtrime-ru/antizapret-vpn-docker.git antizapret
   cd antizapret
   git checkout v6
   ```
2. Создайте docker-compose.override.yml с нужными вам сервисами. Минимальный пример только с wireguard:
```yml
services:
  adguard:
    environment:
      - ADGUARDHOME_PASSWORD=somestrongpassword
  wireguard:
     environment:
        - WIREGUARD_PASSWORD=somestrongpassword
     extends:
        file: services/wireguard/docker-compose.yml
        service: wireguard
```
Полный пример можно найти в [docker-compose.override.sample.yml](./docker-compose.override.sample.yml)

3. Запустите сервисы:
```shell
   docker compose up -d
   docker system prune -f
```

## Docker Swarm, несколько узлов выхода (Продвинутый)

Версии 5 и 6 обладают возможностью перенаправлять трафик на разные узлы выхода для разных доменов.
Например, YouTube лучше всего работает, если узел выхода находится близко к клиенту, а другие сервисы требуют зарубежный IP для работы.
Docker swarm используется для построения единой сети между контейнерами.

Рекомендуется использовать локальный сервер в качестве менеджера/первичного узла для VPN, DNS и контейнеров az-local.
Зарубежный сервер — в качестве вторичного/воркер-узла для контейнера az-world.

Большинство доменов будут проксироваться через **локальный** сервер для максимальной скорости и производительности.
Некоторые сайты, использующие geoip для блокировки пользователей, будут проксироваться через **зарубежный** сервер.

0. Повторите шаги 0 и 1 из установки на одном сервере на **обоих серверах**:
   - Установите docker
   - Зачекаутьте проект в том же месте на обоих серверах.
1. [Первичный] Создайте docker-compose.override.yml на первичном узле и определите, какие сервисы вам нужны. См. шаг 2 из установки на одном сервере.
2. [Первичный] Измените имена хостов серверов на az-local и az-world для удобства: `hostnamectl set-hostname az-local`
3. [Вторичный] Измените имена хостов серверов на az-local и az-world для удобства: `hostnamectl set-hostname az-world`
4. [Опционально] hub.docker.com может быть недоступен на локальных хостингах. Можно использовать прокси. См. инструкции: https://dockerhub.timeweb.cloud
   Альтернативно образы можно собрать локально на **обоих серверах**: `docker compose build`
5. [Первичный]: `docker swarm init --advertise-addr <PRIMARY_SERVER_PUBLIC_IP_ADDRESS>`
6. [Вторичный]: Скопируйте команду из результатов и выполните ее на вторичном узле: `docker swarm join --token <TOKEN> <MANAGER_IP_ADDRESS>:<PORT>`
7. [Первичный]: Проверьте swarm `docker node ls`
    ```text
    ID                            HOSTNAME   STATUS    AVAILABILITY   MANAGER STATUS   ENGINE VERSION
    6dzagr08r8d2iidkcumjjz3q7 *   az-local   Ready     Active         Leader           29.0.1
    vspy2m6w4tf7uv4ywgdnzttvr     az-world   Ready     Active                          29.0.1
    ```
8. [Первичный] Добавьте метки для узлов `docker node update --label-add location=local az-local && docker node update --label-add location=world az-world`
9. [Первичный]: запустите swarm `   docker compose config | docker run --pull always --rm -i xtrime/antizapret-vpn:6 compose2swarm | docker stack deploy --prune -c - antizapret`

## Блокировка VPN / Хостинга
Большинство провайдеров сейчас блокируют VPN-соединения с зарубежными IP-адресами. Обфускация в Amnezia или OpenVpn не всегда решает проблему.
Для стабильной работы VPN вы можете попробовать подключиться к VPS внутри вашей страны, а затем проксировать трафик на зарубежный сервер.

Есть два способа:
1. [Рекомендуется] Установка в режиме [docker swarm](#docker-swarm-несколько-узлов-выхода-продвинутый)
2. Проксирование всего трафика через локальный прокси. См. ниже.

Пример скрипта запуска.
Замените <SERVER_IP> на IP-адрес вашего сервера и запустите его на чистой VPS (рекомендуется ubuntu 24.04):

```shell
#!/bin/sh

# Укажите ip вашего зарубежного сервера
export VPN_IP=<SERVER_IP>

echo "net.ipv4.ip_forward=1" >> /etc/sysctl.d/99-sysctl.conf
sysctl -w net.ipv4.ip_forward=1

# DNAT правила
iptables -t nat -A PREROUTING -p tcp ! --dport 22 -j DNAT --to-destination "$VPN_IP"
iptables -t nat -A PREROUTING -p udp ! --dport 22 -j DNAT --to-destination "$VPN_IP"
# MASQUERADE правила
iptables -t nat -A POSTROUTING -p tcp -d "$VPN_IP" -j MASQUERADE
iptables -t nat -A POSTROUTING -p udp -d "$VPN_IP"  -j MASQUERADE

echo iptables-persistent iptables-persistent/autosave_v4 boolean true | sudo debconf-set-selections
echo iptables-persistent iptables-persistent/autosave_v6 boolean false | sudo debconf-set-selections
apt install -y iptables-persistent
```

## После установки
1. Убедитесь, что безопасный DNS отключен в настройках вашего браузера.
   В chrome: Перейдите в Настройки > Конфиденциальность и безопасность > Безопасность, прокрутите до раздела "Дополнительные" и выключите "Использовать безопасный DNS"
2. Установите модули DKMS для openvpn и/или amnezia wireguard (если вы их используете):
    - [Включение OpenVPN Data Channel Offload (DCO)](#включение-openvpn-data-channel-offload-dco)
    - [Включение расширения ядра Amnezia Wireguard](#включение-расширения-ядра-amnezia-wireguard)

## Доступ к админ-панелям

### HTTPS
По умолчанию все контейнеры доступны через https. Для управления сертификатами используется отдельный контейнер `https`.
Если вы не предоставили домен и email в его переменных окружения, он сгенерирует самоподписанные сертификаты.

- dashboard: https://%your-server-ip%:443
- adguard: https://%your-server-ip%:1443
- filebrowser: https://%your-server-ip%:2443
- openvpn: https://%your-server-ip%:3443
- wireguard: https://%your-server-ip%:4443
- wireguard-amnezia: https://%your-server-ip%:5443


### Локальная сеть
Когда вы подключены к VPN, вы можете получить доступ к контейнерам, не открывая порты в интернет:
- http://adguard.antizapret:3000
- http://dashboard.antizapret:80
- http://wireguard-amnezia.antizapret:51821
- http://wireguard.antizapret:51821
- http://openvpn-ui.antizapret:8080
- http://filebrowser.antizapret:80

### HTTP:
По умолчанию контейнеры не открывают веб-панели в интернет. Все веб-панели проксируются через контейнер `https`.
Если вы хотите открыть HTTP в интернет, добавьте перенаправление портов в docker-compose.override.yml.
Пример:
```yml
services:
   adguard:
      #...
      ports:
        - "3000:3000/tcp"
```

Список портов по умолчанию:

- adguard: http://%your-server-ip%:3000
- dashboard: http://%your-server-ip%:80
- wireguard-amnezia: http://%your-server-ip%:51821
- wireguard: http://%your-server-ip%:51821
- openvpn-ui: http://%your-server-ip%:8080
- filebrowser: http://%your-server-ip%:80

Некоторые контейнеры используют одинаковые порты. Поэтому вам нужно выбрать уникальный внешний порт в docker-compose.override.yml.

## Обновление

- Одиночный экземпляр
   ```shell
   git pull --rebase
   docker compose down --remove-orphans
   docker compose up -d --remove-orphans
   docker system prune -af
   ```
- Режим Swarm:
   ```shell
   git pull --rebase
   docker pull xtrime/antizapret-vpn:6
      docker compose config | docker run --pull always --rm -i xtrime/antizapret-vpn:6 compose2swarm | docker stack deploy --prune -c - antizapret
   docker system prune -af
   ```

### Обновление с v5

1. Обновите контейнеры:
- Режим Docker Compose (один сервер):
   ```shell
   docker compose down --remove-orphans
   git fetch && git checkout v6 && git pull --rebase
   docker compose down --remove-orphans
   docker compose up -d --remove-orphans
   docker system prune -af
   ```
- Режим Swarm:
   ```shell
   docker stack rm antizapret && sleep 10
   git fetch && git checkout v6 && git pull --rebase
   docker compose config | docker run --pull always --rm -i xtrime/antizapret-vpn:6 compose2swarm | docker stack deploy --prune -c - antizapret
   docker system prune -af
   ```
2. Обновите клиентов:
   - Wireguard/Amnezia - нужно скачать новые конфигурации клиентов или вручную добавить `14.16.0.0/14` в AllowedIps в старых конфигурациях.
   - OpenVPN - нужно нажать save на странице конфигурации сервера openvpn-ui: http://openvpn-ui.antizapret:8080/ov/config/ а затем перезапустить сервер openvpn.

## Сброс:
Удалить все настройки, VPN-конфигурации и вернуть начальное состояние сервиса:
```shell
docker stack rm antizapret || docker compose down --remove-orphans
rm -rf config/*
git restore config
```

# Документация

## FAQ (Часто задаваемые вопросы)

1. Как получить VPN-конфигурации?
    - OVPN:
        1. https://%your-server-ip%:3443/certificates
        1. Создать сертификат
        1. Введите любое имя и оставьте остальные поля как есть
        1. Нажмите "Create". Новый сертификат появится в списке.
        1. Нажмите на имя сертификата в списке, чтобы скачать его.
    - Wireguard или Amnezia:
        1. Перейдите на https://%your-server-ip%:4443 или https://%your-server-ip%:5443
        2. Нажмите "New"
        3. Введите любое имя.
        4. Создайте клиента
        5. Нажмите кнопку скачивания в списке.
        6. QR-коды не работают для Amnezia Wireguard, так как конфиг слишком большой для QR-кода
2. Какой клиент Amnezia Wireguard использовать?
   Рекомендуемый клиент для Amnezia Wireguard - AmneziaWG:
    - [Android (Google Play)](https://play.google.com/store/apps/details?id=org.amnezia.awg)
    - [iOS (App Store)](https://apps.apple.com/app/amneziawg/id6478942365)
    - [Windows (GitHub)](https://github.com/amnezia-vpn/amneziawg-windows-client/releases)
3. Почему OpenVPN-клиент не подключается к серверу?
   Большинство провайдеров блокируют протокол openvpn, особенно к зарубежным IP.
   Симптомы: клиент подключается, но после передачи нескольких байт сервер перестает отвечать, и соединение разрывается.

   - По умолчанию контейнер openvpn использует легкую обфускацию UDP-пакетов.
     Это работает на большинстве клиентов (включая роутеры), но все еще может блокироваться провайдерами.
     См. переменную окружения [OBFUSCATE_TYPE](#openvpn).
     Попробуйте изменить ее со значения по умолчанию `1` (легкая) на `2` (сильная) или `0` (выкл).
   - Используйте каскадное соединение или swarm-режим [cascade](#блокировка-vpn--хостинга)
4. Почему VPN-соединение может быть медленным и иметь много потерянных пакетов?
   1. Сначала используйте воспроизводимый тест для обнаружения проблем: [Тест скорости с iperf3](#тест-скорости-с-iperf3)
   2. Проверьте, не перегружен ли процессор на вашем хостинге во время iperf-теста.
   3. Убедитесь, что модули ядра для вашего VPN установлены и работают: [OVPN DCO](#включение-openvpn-data-channel-offload-dco), [расширение ядра Amnezia Wireguard](#включение-расширения-ядра-amnezia-wireguard).
   4. Некоторые недорогие хостинги имеют очень медленные процессоры, поэтому даже с установленными модулями ядра скорость соединения не превысит 100 Мбит/с.
   5. Большинство роутеров имеют медленные процессоры и обеспечивают только 30-60 Мбит/с через openvpn. Попробуйте использовать Wireguard или Amnezia Wireguard, если роутер поддерживает это, или обновите роутер до более новой модели.
   6. В редких случаях низкий MTU между клиентом и сервером может вызывать фрагментацию пакетов.
      Сначала проверьте, есть ли у вашего VPN-соединения проблемы со стандартным MTU.
      - MacOs: `ping -D -s 1420 google.com`
      - Linux: `ping -M -s 1420 google.com`
      - Windows: `ping google.com -f -l 1420`

      Если эта команда возвращает ошибки, продолжайте уменьшать значение, пока не заработает. Затем уменьшите MTU в настройках VPN.
      - Wireguard/Amezia:
        MTU должен быть меньше на сервере и клиенте.
          1. Перейдите на http://wireguard.antizapret:51821 или http://wireguard-amnezia.antizapret:51821 и нажмите на иконку конфига клиента.
          1. Уменьшите MTU до 1200 и сохраните. MTU специфичен для клиента.
          1. Скачайте и примените новый конфиг к вашему клиенту.
          1. [Тест скорости с iperf3](#тест-скорости-с-iperf3)
      - OpenVPN:
          1. Перейдите на http://openvpn-ui.antizapret:8080/ov/config и добавьте `link-mtu 1200` в конфиг сервера.
          1. Сохраните конфигурацию и перезапустите сервер.
          1. Добавьте `link-mtu 1200` в ваш client.conf
   7. Если ничего не помогает, попробуйте другой хостинг и/или [каскад](#блокировка-vpn--хостинга)
4. Как отладить проблемы с VPN?
    1. Проверьте, установлено ли VPN-соединение и работает ли DNS-сервер:
       ```shell
       > nslookup youtube.com
       
       Server:         14.16.0.1
       Address:        14.16.0.1#53
       
       Non-authoritative answer:
       Name:   youtube.com
       Address: 14.16.13.209
       ```
    2. Убедитесь, что браузер не использует DoH/Secure DNS.
    3. Проверьте, загружены ли DIST-фильтры и имеют ли они ненулевые счетчики правил: http://adguard.antizapret:3000/#filters
    4. Проверьте этапы разрешения DNS: http://adguard.antizapret:3000/#logs?response_status=all&search=youtube.com  
       Каждый домен разрешается через 2-4 DNS-запроса. Подробнее: [Алгоритм разрешения DNS](#алгоритм-разрешения-dns)

## Алгоритм разрешения DNS

![Preview](./img/chart.png)

1. DNS-запрос поступает в AdGuardHome
2. Adguard проверяет его правилами черного списка. Если домен в черном списке - возвращается 0.0.0.0, и клиент не может получить доступ к домену.
3. Adguard отправляет DNS-запрос в сервис CoreDNS.
4. CoreDNS отправляет DNS-запрос на внутренний сервер dnsmap.py (контейнер antizapret), а dnsmap.py отправляет запрос обратно в adguard.
5. Adguard получает запросы еще раз, но теперь применяет правила с `$client=az-local` и реальным upstream-сервером клиента (по умолчанию 8.8.8.8)
6. Если домен в белом списке - adguard разрешит его адрес и вернет в dnsmap.py.
7. Если домен не в белом списке, adguard возвращает SERVFAIL.
8. dnsmap.py отправляет ответ в adguard:
   1. Если это валидный IP, то заменяет его на "внутренний" IP из подсети `14.16.0.0/15`, добавляет маскарадинг в iptables и возвращает внутренний ip в adguard
   2. Если это SERVFAIL, он отправляет этот ответ клиенту.
9. Если CoreDNS получает SERVFAIL, он повторяет запрос и отправляет его напрямую в Adguard. В этом случае правила с `$client=az-local` не применяются, и запрос обрабатывается нормально.

**Почему так сложно?**
- Windows и некоторые другие клиенты не повторяют запрос к Fallback DNS, даже если получен SERVFAIL. Поэтому мы добавили для этого CoreDNS.
- Adguard не позволяет переопределять upstream в правилах черного/белого списка.
  Но эти правила имеют поддержку regex и обновляются автоматически, поэтому мы хотим их использовать.
  Поэтому внутри совершается несколько запросов от разных клиентов.
- Adguard позволяет разные upstreams для разных клиентов. Поэтому мы можем использовать разный DNS для заблокированных и незаблокированных доменов.

**Пример:**
Мы запросили `youtube.com`, который должен маршрутизироваться через узел az-local.
1. DNS-запрос от клиента в Adguard. Перенаправлен в coredns. Ответ появится после того, как будут обработаны все последующие запросы.
   ```text
   Status: Processed
   DNS server: coredns:53
   Elapsed: 91 ms
   Served from cache: False
   Response code: NOERROR
   Response:
   A: 14.16.13.209 (ttl=300)
   A: 14.16.13.207 (ttl=300)
   A: 14.16.13.206 (ttl=300)
   A: 14.16.13.208 (ttl=300)
   ```
2. DNS-запрос от coredns в az-world. И запрос от az-world в Adguard:
   ```text
   Status: Rewritten
   Elapsed: 0.10 ms
   Response code: SERVFAIL
   Rule(s):
   ||*^$dnsrewrite=SERVFAIL,client=az-world
   Custom filtering rules
   ```
   Ответ SERVFAIL означает, что эти домены не маршрутизируются через az-world.
3. DNS-запрос от coredns в az-local. И запрос от az-local в Adguard:
   ```text
   Status: Processed
   DNS server: 149.112.112.11:53
   Elapsed: 50 ms
   Response code: NOERROR
   Response
   A: 173.194.221.190 (ttl=300)
   A: 173.194.221.91 (ttl=300)
   A: 173.194.221.136 (ttl=300)
   A: 173.194.221.93 (ttl=300)
   ```
   В этом случае домен должен обслуживаться через az-local и быть исключен из черного списка для клиента az-local.
   Adguard не может найти этот домен в черном списке для az-local и возвращает реальные адреса клиенту az-local.
4. Контейнер az-local добавляет маскарадинг в iptables и возвращает внутренний ip в coredns.
5. coredns отправляет ответ в adguard, adguard кеширует его и возвращает клиенту.


## Добавление доменов
Есть два способа добавления доменов. Через пользовательские правила и через черные списки.

### Добавление доменов через правила
Откройте панель adguard: http://adguard.antizapret:3000/#custom_rules
Правила/синтаксис: https://adguard-dns.io/kb/general/dns-filtering-syntax/#basic-examples

По умолчанию adguard перезаписывает все запросы с SERVFAIL. Это трюк, чтобы заставить клиент повторить DNS-запрос ко второму, локальному DNS-серверу.
Правила с модификатором ответа dnsrewrite имеют более высокий приоритет, чем другие правила в AdGuard Home и AdGuard DNS.
Чтобы переопределить стандартное правило, пользовательские правила должны иметь модификатор `$dnsrewrite`.

Для поддержки стандартных фильтров adguard стандартное правило SERVFAIL применяется только к внутренним запросам от client=az-local и client=az-world


Примеры:
```
@@||subdomain.host.com^$dnsrewrite,client=az-local
@@||*.host.com^$dnsrewrite,client=az-local
@@||host.com^$dnsrewrite,client=az-world
@@||de^$dnsrewrite,client=az-world

@@/some_.*_regex/$dnsrewrite,client=az-local
```

### Добавление доменов через списки
Также вы можете добавить любые url в blocklist. http://adguard.antizapret:3000/#dns_blocklist
Необходимо использовать адаптер для парсинга и адаптации списка в различных форматах.
 - Добавить домены для локального узла выхода: `http://az-local.antizapret/list/?url=<ANY_URL>`
 - Добавить домены для мирового узла выхода `http://az-world.antizapret/list/?url=<ANY_URL>`
Поддерживаемые форматы: простой список доменов, формат adguard, формат hosts, json-массив доменов, список regex.


Опции для адаптера:
 - `url` - скачать список по url
 - `file` - прочитать локальный файл. Используется для include-host-{custom,dist}.txt
 - `filter_custom=1` - фильтровать списки правилами из exclude-hosts-custom.txt.
 - `filter_dist=0` - фильтровать списки правилами из exclude-hosts-dist.txt
 - `format=list` - 'list' или 'json'. Определяется автоматически.
 - `client=az-local` - имя клиента для добавления в правила. Определяется автоматически.
 - `allow=1` - отключить эту опцию, чтобы блокировать домены из списка для этого узла выхода.
 - `raw=0` - не изменять правила
 - `suffix=1` - добавить "$dnsrewrite,client=xxx" в правила

## Добавление IP/подсетей
Добавьте ip и подсети в `./config/antizapret/custom/include-ips-custom.txt`.
Контейнеры периодически проверяют изменения в папке config (каждые 5-10 секунд) и перезапускаются/обновляются после любых изменений.

Запустить обновление вручную: `docker exec $(docker ps -q --filter=name=az | head -n1) doall`

## SOCKS5 прокси (маршрутизация для конкретных приложений)

AntiZapret использует DNS-маршрутизацию (split tunneling), которая работает только для соединений по доменным именам.
Если приложение подключается напрямую по IP-адресу, перехват DNS не работает, и трафик не маршрутизируется через VPN-туннель.

Добавление большого количества IP в `include-ips-custom.txt` может вызвать проблемы с OpenVPN (лимит push routes), поэтому Dante SOCKS5 прокси был добавлен как альтернативное решение.

### Как это работает

1. Подключитесь к VPN (OpenVPN, WireGuard или Amnezia WireGuard)
2. Настройте приложение на использование SOCKS5-прокси с помощью инструментов типа [AntizapretSOCKS5](https://github.com/danayer/AntizapretSOCKS5) (Windows), ProxyBridge, Proxifier или настроек прокси в браузере.
3. Весь трафик от этого приложения (включая прямые IP-соединения) будет выходить через выбранный серверный узел.

Доступны два контейнера socks5-прокси:
- **`socks-local.antizapret:8118`** — трафик выходит через **локальный** сервер
- **`socks-world.antizapret:8118`** — трафик выходит через **зарубежный** сервер

Аутентификация: SOCKS5 с именем пользователя/паролем (настраивается через переменные окружения).
Чтобы отключить аутентификацию, пропустите `SOCKS_USERNAME` и `SOCKS_PASSWORD` (или оставьте их пустыми).

### Когда использовать Dante вместо DNS-маршрутизации

| Сценарий | DNS-маршрутизация | Dante SOCKS5 |
|---|---|---|
| Приложение соединяется по домену | ✅ Работает | ✅ Работает |
| Приложение соединяется по IP | ❌ Не маршрутизируется | ✅ Работает |
| Большое количество IP для маршрутизации | ❌ Лимит OpenVPN push routes | ✅ Без лимитов |
| Выбор узла выхода для приложения | ❌ | ✅ Выбор local или world для приложения |

### Конфигурация

Добавьте socks5-сервисы в `docker-compose.override.yml`:
```yml
  socks-local:
    hostname: socks-local.antizapret
    extends:
      file: services/socks/compose.yml
      service: socks
    environment:
      - SOCKS_USERNAME=admin
      - SOCKS_PASSWORD=password
    deploy:
      mode: replicated
      replicas: 1
      endpoint_mode: dnsrr
      placement:
        constraints: [ node.labels.location == local ]

  socks-world:
    hostname: socks-world.antizapret
    extends:
      file: services/socks/compose.yml
      service: socks
    environment:
      - SOCKS_USERNAME=admin
      - SOCKS_PASSWORD=password
    deploy:
      mode: replicated
      replicas: 1
      endpoint_mode: dnsrr
      placement:
        constraints: [ node.labels.location == world ]
```

> **Примечание:** `socks-world` требует режима [Docker Swarm](#docker-swarm-несколько-узлов-выхода-продвинутый) с двумя узлами.
> На одном сервере будет работать только `socks-local`.

### Настройка клиента

1. Подключитесь к VPN
2. Настройте SOCKS5-прокси в приложении или прокси-менеджере:
    - **Хост:** `socks-local.antizapret` или `socks-world.antizapret`
    - **Порт:** `8118`
    - **Тип:** SOCKS5
    - **Имя пользователя:** значение `SOCKS_USERNAME`
    - **Пароль:** значение `SOCKS_PASSWORD`

#### Windows

Для Windows-клиентов используйте [AntizapretSOCKS5](https://github.com/danayer/AntizapretSOCKS5) — графическое приложение для настройки SOCKS5-маршрутизации для конкретных приложений с использованием [ProxiFyre](https://github.com/wiresock/proxifyre).

1. Скачайте и распакуйте [AntizapretSOCKS5](https://github.com/danayer/AntizapretSOCKS5)
2. Запустите `ConfigEditor.exe` и установите драйвер Windows Packet Filter, если потребуется
3. Добавьте конфигурации прокси — выберите приложения, укажите прокси-сервер (`socks-local.antizapret:8118` или `socks-world.antizapret:8118`) и введите учетные данные
4. Сохраните конфигурацию и запустите ProxiFyre

### Примеры использования

- **Игровой клиент**, подключающийся к серверам по IP — маршрутизируйте через `socks-world` для обхода гео-ограничений
- **Торрент-клиент** — маршрутизируйте через `socks-world` для использования зарубежного IP
- **Браузер** — используйте прокси-расширение для маршрутизации конкретных сайтов через `socks-local` или `socks-world`
- **Приложение с множеством жестко закодированных IP** — вместо добавления сотен IP в `include-ips-custom.txt`, просто проксируйте все приложение через socks5

## Переменные окружения

Вы можете определить эти переменные в файле docker-compose.override.yml для своих нужд:

### Antizapret:
Состоит из двух контейнеров: az-local и az-world. Это VPN-узлы выхода.
- `DNS=adguard` - Upstream DNS для разрешения заблокированных сайтов (adguard по умолчанию)
- `AZ_SUBNET=14.16.0.0/14` Подсеть для виртуальных адресов заблокированных хостов.
- `ROUTES` - список VPN-контейнеров и их виртуальных адресов. Используется для iperf3 сервера.
- `DOALL_DISABLED=` - пропустить запуск на узле az-world.

### Adguard:
- `ROUTES` - список VPN-контейнеров и их виртуальных адресов. Используется для уникальных клиентских адресов в логах adguard
- `ADGUARDHOME_PORT=3000`
- `ADGUARDHOME_USERNAME=admin`
- `ADGUARDHOME_PASSWORD=`
- `ADGUARDHOME_PASSWORD_HASH=` - хешированный пароль, берется из файла AdGuardHome.yaml после первого запуска с использованием `ADGUARDHOME_PASSWORD`. Знак доллара `$` в хеше должен быть экранирован вторым знаком доллара: `$$`

### CoreDNS:
- Нет

### Filebrowser:
- `FILEBROWSER_PORT=admin`
- `FILEBROWSER_PASSWORD=password`

### Proxy:
- `PROXY_DOMAIN=` - создать letsencrypt https-сертификат для домена. Если не задано, для самоподписанного сертификата используется host ip.
- `PROXY_EMAIL=` - email для сертификата letsecnrypt.
- `SOCKS_EXTERNAL_IFACES` - список внешних сетевых интерфейсов для SOCKS-прокси через запятую (например, `eth0,eth1`). Если пропущено, интерфейсы определяются автоматически; по умолчанию `eth0`, если другие не найдены

### Openvpn
- `ROUTES`
- `OBFUSCATE_TYPE=1` - уровень кастомной обфускации протокола openvpn.
   - 0 - отключить. Обычный режим openvpn-клиента, поддерживается всеми клиентами.
   - 1 - легкая обфускация. Работает с роутерами microtic и старыми keenetic
   - 2 - сильная обфускация. Работает с большинством клиентов: официальный openvpn gui клиент, роутеры asus, новые роутеры keenetic, роутеры openwrt.
- `AZ_SUBNET=14.16.0.0/14` - подсеть для виртуальных заблокированных ip.

### Openvpn-ui
- `OPENVPN_ADMIN_USERNAME=` - замените имя пользователя по умолчанию на свое
- `OPENVPN_ADMIN_PASSWORD=` - замените пароль по умолчанию на свой
- `OPENVPN_EXTERNAL_IP` - внешний ip вашего сервера, по умолчанию определяется автоматически
- `OPENVPN_DNS=14.16.0.1` - DNS-адрес для клиентов. Должен быть в `ANTIZAPRET_SUBNET`
- `OPENVPN_LOCAL_IP_RANGE=10.1.165.0` - подсеть для ovpn-клиентов. Подсеть можно увидеть в журнале adguard или в панели ovpn-ui

### Wireguard/Wireguard Amnezia
- `ROUTES`
- `WIREGUARD_PASSWORD=` - пароль для админ-панели (используется только во время первичной настройки, пароль можно изменить через веб-интерфейс позже)
- `WIREGUARD_USERNAME=admin` - имя пользователя для админ-панели (используется только во время первичной настройки)
- `AZ_SUBNET=14.16.0.0/14` - подсеть для виртуальных заблокированных ip.
- `WG_DEFAULT_DNS=14.16.0.1` - DNS-адрес для клиентов. Должен быть в `ANTIZAPRET_SUBNET`
- `WG_PERSISTENT_KEEPALIVE=25`
- `PORT=51821` - порт админ-панели
- `INSECURE=true` - разрешить доступ к админ-панели по HTTP
- `DISABLE_IPV6=true` - отключить поддержку IPv6
- `WG_PORT=51820` - порт сервера wireguard
- `EXPERIMENTAL_AWG=true` - включить поддержку AmneziaWG (только wireguard-amnezia)
- `OVERRIDE_AUTO_AWG=awg`- переменная окружения для принудительного типа туннеля: `awg` для всегда AmneziaWG, `wg` для всегда стандартного WireGuard; по умолчанию не задано и используется автоматическое определение, полезно для переопределения автовыбора и фиксации режима.
- `BGP_ENABLE=false` - запустить bird BGP сервер. Сервер будет передавать маршруты клиентам (некоторым роутерам). Клиенты будут получать обновления маршрутов без обновления конфига wg/awg.

### SOCKS5 прокси
- `SOCKS_USERNAME` - имя пользователя для аутентификации SOCKS5 (пропустите, чтобы отключить аутентификацию)
- `SOCKS_PASSWORD` - пароль для аутентификации SOCKS5 (пропустите, чтобы отключить аутентификацию)

## DNS
### Upstream DNS для Adguard
Adguard использует Google DNS и Quad9 DNS для разрешения незаблокированных доменов. Эти апстримы поддерживают ECS-запросы (подробнее ниже).
Cloudflare DNS не поддерживает ECS и не рекомендуется к использованию.

Исходный код: [Adguard upstream DNS](./antizapret/root/adguardhome/upstream_dns_file_basis)
После запуска контейнера рабочая копия находится здесь: `./config/adguard/conf/upstream_dns_file_basis`

### CDN + ECS
Некоторые домены могут разрешаться по-разному в зависимости от подсети (geoip) клиента. В этом случае использование DNS, расположенного на удаленном сервере, сломает некоторые сервисы.
ECS позволяет предоставить IP клиента в DNS-запросах к upstream-серверу и получить корректные результаты.
По умолчанию включено в Adguard, и ip клиента указывается на Москву (подсеть Yandex).

Если вы находитесь в другом регионе, вам нужно заменить `77.88.8.8` на ваш реальный ip-адрес на этой странице `http://your-server-ip:3000/#dns`

## OpenVPN
### Создание клиентских сертификатов:
https://github.com/d3vilh/openvpn-ui?tab=readme-ov-file#generating-ovpn-client-profiles
1) перейдите на `http://%your_ip%:8080/certificates`
2) нажмите "create certificate"
3) введите уникальное имя. Оставьте остальные поля пустыми
4) нажмите create
5) нажмите на имя сертификата в списке, чтобы скачать файл ovpn.

### Включение OpenVPN Data Channel Offload (DCO)
[OpenVPN Data Channel Offload (DCO)](https://openvpn.net/as-docs/openvpn-dco.html) обеспечивает повышение производительности за счет перемещения обработки канала данных в пространство ядра, где она может обрабатываться более эффективно и с многопоточностью.
**tl;dr** увеличивает скорость и снижает нагрузку на процессор сервера.

Расширения ядра можно установить только на <u>хостовую машину</u>, а не в контейнер.

#### Ubuntu 24.04
```bash
sudo rm -f /etc/apt/sources.list.d/openvpn.list
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://swupdate.openvpn.net/repos/repo-public.gpg | sudo gpg --dearmor --yes -o /etc/apt/keyrings/openvpn-repo-public.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/openvpn-repo-public.gpg] http://build.openvpn.net/debian/openvpn/release/2.7 noble main" | sudo tee /etc/apt/sources.list.d/openvpn-aptrepo.list > /dev/null
sudo apt update
sudo apt install -y ovpn-dkms
```
#### Ubuntu 22.04
```bash
sudo rm -f /etc/apt/sources.list.d/openvpn.list
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://swupdate.openvpn.net/repos/repo-public.gpg | sudo gpg --dearmor --yes -o /etc/apt/keyrings/openvpn-repo-public.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/openvpn-repo-public.gpg] http://build.openvpn.net/debian/openvpn/release/2.7 jammy main" | sudo tee /etc/apt/sources.list.d/openvpn-aptrepo.list > /dev/null
sudo apt update
sudo apt install -y ovpn-dkms
```
#### Ubuntu 20.04
```bash
sudo rm -f /etc/apt/sources.list.d/openvpn.list
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://swupdate.openvpn.net/repos/repo-public.gpg | sudo gpg --dearmor --yes -o /etc/apt/keyrings/openvpn-repo-public.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/openvpn-repo-public.gpg] http://build.openvpn.net/debian/openvpn/release/2.7 focal main" | sudo tee /etc/apt/sources.list.d/openvpn-aptrepo.list > /dev/null
sudo apt update
sudo apt install -y ovpn-dkms
```
### Поддержка устаревших клиентов
Если ваши клиенты не поддерживают шифры GCM, вы можете использовать устаревшие шифры CBC.
DCO несовместим с устаревшими шифрами и будет отключен. Это также увеличит нагрузку на процессор.

## Amnezia Wireguard

### Включение расширения ядра Amnezia Wireguard

https://github.com/amnezia-vpn/amneziawg-linux-kernel-module?tab=readme-ov-file#ubuntu

#### Ubuntu 24.04
1. `sudo add-apt-repository ppa:amnezia/ppa`
2. `sudo apt install -y amneziawg`
3. перезапустите сервер или `docker compose restart wireguard-amnezia`
4. проверьте список модулей ядра `dkms status`,
   и убедитесь, что запущена куча процессов `[kworker/X:X-wg-crypt-wg0]`.

#### Ubuntu 20.04, 22.04
1. Отредактируйте `etc/apt/sources.list` и раскомментируйте `deb-src http://archive.ubuntu.com/ubuntu ... main restricted`
2. `sudo apt update`
3. `sudo apt install -y software-properties-common python3-launchpadlib gnupg2 linux-headers-$(uname -r)`
4. установите исходный код ядра `sudo apt-get source linux-image-$(uname -r)`
5. `sudo add-apt-repository ppa:amnezia/ppa`
6. `sudo apt install -y amneziawg`
7. `sudo dkms install -m amneziawg -v 1.0.0`
8. перезапустите сервер или `docker compose restart wireguard-amnezia`
9. проверьте список модулей ядра `dkms status`,
   и убедитесь, что запущена куча процессов `[kworker/X:X-wg-crypt-wg0]`.

### Параметры AmneziaWG

Описание параметров можно найти в [документации AmneziaWG](https://docs.amnezia.org/documentation/amnezia-wg) и на странице модуля ядра.

Все параметры, **кроме I1–I5**, будут установлены автоматически при первом запуске. Для инструкций по настройке I1–I5 обратитесь к документации AmneziaWG.

- Если параметр **не задан**, он не будет включен в конфигурацию.
- Если **все специфичные для AmneziaWG параметры отсутствуют**, AmneziaWG полностью совместим со стандартным WireGuard.

## Таблица совместимости параметров

| Параметр | Может отличаться на сервере и клиенте | Настраивается на сервере | Настраивается на клиенте |
|-----------|-------------------------------------|----------------------|----------------------|
| Jc        | ✅ Да                               | ✅ Да                | ✅ Да                |
| Jmin      | ✅ Да                               | ✅ Да                | ✅ Да                |
| Jmax      | ✅ Да                               | ✅ Да                | ✅ Да                |
| S1–S4     | ❌ Нет, должны совпадать            | ✅ Да                | ❌ Нет (копируется с сервера) |
| H1–H4     | ❌ Нет, должны совпадать            | ✅ Да                | ❌ Нет (копируется с сервера) |
| I1–I5     | ✅ Да                               | ✅ Да                | ✅ Да                |

## Примечания

- Параметры Jc, Jmin, Jmax, I1–I5 могут быть настроены независимо на сервере и клиенте, если это необходимо.
- Параметры S1–S4 и H1–H4 **должны совпадать** между сервером и клиентом; клиент копирует их автоматически с сервера.
- Используйте I1–I5 только если вам нужна расширенная настройка. В остальных случаях достаточно стандартных автоматических значений.

### Размер блока Amnezia Wireguard
Amnezia добавляет случайные пакеты для изменения сигнатуры протокола wireguard и обхода DPI.
По умолчанию мы используем `JMIN=20; JMAX=100` для размера "мусорного" пакета в байтах.

Большие "мусорные" пакеты могут помочь обойти DPI, но некоторые файрволы могут блокировать их как DDOS-атаку.
Используйте переменные окружения, чтобы изменить их размер, если у вас проблемы с подключением amnezia:

```
JC=3
JMIN=20
JMAX=100
```
или
```
JC=2
JMIN=10
JMAX=20
```
Пример части docker-compose.override.yml с JC, JMIN и JMAX:
```yml
  wireguard-amnezia:
    environment:
      - WIREGUARD_PASSWORD=xxxxx
      - JC=2
      - JMIN=10
      - JMAX=20
    extends:
      file: services/wireguard/docker-compose.yml
      service: wireguard-amnezia
```
Настройки/переменные окружения сохраняются в папке ./config/wireguard_amnezia/. Чтобы обновить их, удалите папку и запустите контейнер снова.
Это также удалит всех существующих клиентов/сертификаты.
```shell
docker compose down && rm -rf ./config/wireguard_amnezia/ && docker compose up -d
```


## Дополнительная информация
- [Руководство по настройке OpenWrt](./docs/guide_OpenWrt.md) - как настроить роутер OpenWrt с этим решением, чтобы локальные клиенты работали нормально.
- [Руководство по настройке Keenetic](./docs/guide_Keenetic_RU.md) - инструкции по настройке сервера и подключению к нему роутеров Keenetic [(на русском языке)](./docs/guide_Keenetic_RU.md)

## Тест скорости с iperf3
iperf3 сервер включен в контейнер antizapret-vpn.
1. Подключитесь к VPN
2. Используйте iperf3 клиент на вашем телефоне или компьютере, чтобы проверить скорость загрузки/отгрузки.
    Пример с 10 потоками на 10 секунд и отчетом результата каждую секунду:
    ```shell
    # локальный узел
    iperf3 -c az-local.antizapret -i1 -t10 -P10
    iperf3 -c az-local.antizapret -i1 -t10 -P10 -R

   # мировой узел
    iperf3 -c az-world.antizapret -i1 -t10 -P10
    iperf3 -c az-world.antizapret -i1 -t10 -P10 -R
    ```

# Благодарности
- [ProstoVPN](https://antizapret.prostovpn.org) — оригинальный проект
- [AntiZapret VPN Container](https://bitbucket.org/anticensority/antizapret-vpn-container/src/master/) — исходный код LXD-контейнера
- [AntiZapret PAC Generator](https://bitbucket.org/anticensority/antizapret-pac-generator-light/src/master/) — генератор автоконфигурации прокси для обхода цензуры в Российской Федерации
- [WireGuard VPN](https://github.com/wg-easy/wg-easy) — используется для интеграции Wireguard
- [OpenVPN](https://github.com/d3vilh/openvpn-ui) - используется для интеграции OpenVPN
- [AdGuardHome](https://github.com/AdguardTeam/AdGuardHome) - DNS-резолвер
- [filebrowser](https://github.com/filebrowser/filebrowser) - веб-браузер файлов и редактор
- [lighttpd](https://github.com/lighttpd/lighttpd1.4) - веб-сервер для единой панели управления
- [caddy](https://github.com/caddyserver/caddy) - обратный прокси
- [No Thought Is a Crime](https://ntc.party) — форум о технических, политических и экономических аспектах интернет-цензуры в разных странах
- [Dante](https://www.inet.no/dante/) - SOCKS5 прокси-сервер для маршрутизации конкретных приложений
