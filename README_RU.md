# AntiZapret VPN в Docker

AntiZapret создан для того, чтобы направлять **в VPN‑туннель только заблокированные домены**. Это называется **split tunneling** (раздельная маршрутизация).
Этот репозиторий основан на идее оригинального образа [AntiZapret LXD image](https://bitbucket.org/anticensority/antizapret-vpn-container/src/master/)

# Группа поддержки и обсуждений:
https://t.me/antizapret_support

# Возможности

- Несколько VPN‑транспортов: WireGuard, Amnezia WireGuard, OpenVPN
- AdGuardHome в качестве основного DNS‑резолвера и менеджера доменов для обхода блокировок
- Мультисерверная архитектура для обхода гео‑ограничений сервисов: разные домены могут использовать разные серверы как выходные узлы (exit nodes).
- Файрвол для защиты от сканирования портов
- Поддержка модулей ядра для OpenVPN и Amnezia WireGuard для снижения нагрузки на CPU.

# Как это работает?

1) Список заблокированных доменов загружается из открытого реестра.
2) Список парсится и создаются правила для DNS‑резолвера (AdGuardHome).
3) AdGuardHome пересылает запросы для заблокированных доменов в Python‑скрипт `dnsmap.py`.
4) Python‑скрипт:
   a) резолвит реальный адрес домена  
   b) создаёт «фейковый» адрес из подсети `10.244.0.0/15`  
   c) создаёт правило iptables, чтобы пересылать все пакеты с фейкового IP на реальный IP.
5) Фейковый IP отправляется клиенту в ответе DNS.
6) Все VPN‑туннели настроены со split tunneling: через VPN маршрутизируется только трафик в подсеть `10.244.0.0/15`.


# Установка

## Один сервер (простой вариант)

Рекомендуется использовать сервер, расположенный в западных странах. Некоторые сайты блокируют пользователей из других стран. 

0. Установите [Docker Engine](https://docs.docker.com/engine/install/):
   ```bash
   curl -fsSL https://get.docker.com -o get-docker.sh
   sudo sh get-docker.sh
   ```
1. Клонируйте репозиторий и подготовьте контейнер:
   ```bash
   git clone https://github.com/xtrime-ru/antizapret-vpn-docker.git antizapret
   cd antizapret
   git checkout v5
   ```
2. Создайте `docker-compose.override.yml` с нужными сервисами. Минимальный пример (только wireguard):
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
Полный пример смотрите в [docker-compose.override.sample.yml](./docker-compose.override.sample.yml)

3. Запустите сервисы:
```shell
   docker compose up -d
   docker system prune -f
```

## Docker Swarm, несколько выходных узлов (продвинутый вариант)

Версия 5 умеет пересылать трафик на разные выходные узлы для разных доменов. 
Например, YouTube лучше работает, если выходной узел близко к клиенту, а другим сервисам для работы нужен иностранный IP. 
Для построения единой сети между контейнерами используется Docker Swarm.

Рекомендуется использовать локальный сервер как manager/primary‑узел для VPN, DNS и контейнеров `az-local`.
Иностранный сервер — как secondary/worker‑узел для контейнера `az-world`.

Большинство доменов будет проксироваться через **локальный** сервер для максимальной скорости и производительности. 
Некоторые сайты, которые используют GeoIP для блокировки пользователей, будут проксироваться через **иностранный** сервер.

0. Повторите шаги 0 и 1 из установки «Один сервер» на **обоих серверах**:
   - установите Docker
   - разверните проект в одном и том же пути на обоих серверах.
1. [Primary] Создайте `docker-compose.override.yml` на primary‑узле и определите, какие сервисы вам нужны (см. шаг 2 из установки «Один сервер»).
1. [Primary] Для удобства смените hostname на `az-local`: `hostnamectl set-hostname az-local`
1. [Secondary] Для удобства смените hostname на `az-world`: `hostnamectl set-hostname az-world`
1. [Опционально] hub.docker.com может быть недоступен у некоторых локальных хостингов. Можно использовать прокси. Инструкция: https://dockerhub.timeweb.cloud  
    Альтернативно образы можно собрать локально на **обоих серверах**: `docker compose build`
1. [Primary]: `docker swarm init --advertise-addr <PRIMARY_SERVER_PUBLIC_IP_ADDRESS>`
1. [Secondary]: Скопируйте команду из вывода и выполните её на secondary‑узле: `docker swarm join --token <TOKEN> <MANAGER_IP_ADDRESS>:<PORT>`
1. [Primary]: Проверьте swarm: `docker node ls`
    ```text
    ID                            HOSTNAME   STATUS    AVAILABILITY   MANAGER STATUS   ENGINE VERSION
    6dzagr08r8d2iidkcumjjz3q7 *   az-local   Ready     Active         Leader           29.0.1
    vspy2m6w4tf7uv4ywgdnzttvr     az-world   Ready     Active                          29.0.1
    ```
1. [Primary] Добавьте метки узлам:
   `docker node update --label-add location=local az-local && docker node update --label-add location=world az-world`
1. [Primary, Secondary] Создайте папки конфигурации на **обоих узлах**:
   ```docker compose pull; docker compose up -d; sleep 60; docker compose down;```
1. [Primary] Запустите стек:
   `docker compose config | docker run --rm -i xtrime/antizapret-vpn:5 compose2swarm | docker stack deploy --prune -c - antizapret `


## Доступ к админ‑панелям

### HTTPS

По умолчанию все контейнеры доступны по HTTPS. Для управления сертификатами используется отдельный контейнер `https`.
Если вы не указали домен и email в переменных окружения, будут сгенерированы самоподписанные сертификаты.

- dashboard: https://<your-server-ip>:443
- adguard: https://<your-server-ip>:1443
- filebrowser: https://<your-server-ip>:2443
- openvpn: https://<your-server-ip>:3443
- wireguard: https://<your-server-ip>:4443
- wireguard-amnezia: https://<your-server-ip>:5443


### Локальная сеть

Когда вы подключены к VPN, вы можете обращаться к контейнерам, не открывая порты в интернет:

- http://adguard.antizapret:3000
- http://dashboard.antizapret:80
- http://wireguard-amnezia.antizapret:51821
- http://wireguard.antizapret:51821
- http://openvpn-ui.antizapret:8080
- http://filebrowser.antizapret:80

### HTTP

По умолчанию контейнеры не публикуют веб‑панели в интернет. Все веб‑панели проксируются через контейнер `https`.
Если вы хотите открыть HTTP в интернет, добавьте проброс портов в `docker-compose.override.yml`.  
Пример:
```yml
services:
   adguard:
      #...
      ports:
        - "3000:3000/tcp"
```

Список портов по умолчанию:

- adguard: http://<your-server-ip>:3000
- dashboard: http://<your-server-ip>:80
- wireguard-amnezia: http://<your-server-ip>:51821
- wireguard: http://<your-server-ip>:51821
- openvpn-ui: http://<your-server-ip>:8080
- filebrowser: http://<your-server-ip>:80

У некоторых контейнеров одинаковые порты, поэтому нужно выбрать уникальный внешний порт в `docker-compose.override.yml`.

## Обновление

- Одиночный инстанс:
   ```shell
   git pull
   docker compose down --remove-orphans
   docker compose up -d --remove-orphans
   ```
- Режим Swarm:
   ```shell
   git pull
   docker pull xtrime/antizapret-vpn:5
   docker compose config | docker run --rm -i xtrime/antizapret-vpn:5 compose2swarm | docker stack deploy --prune -c - antizapret
   ```

### Обновление с v4

Есть два варианта: простой (Easy) или ручной (Manual).

 - Простое обновление [**удалит все конфиги, включая VPN‑конфиги!**]
    ```shell
    docker compose down --remove-orphans
    git fetch && git checkout v5
    rm -rf ./config/*
    sed -i  's/proxy\:/https\:/' docker-compose.override.yml
    sed -i  's/antizapret\:/adguard\:/' docker-compose.override.yml
    docker compose pull && docker compose up -d
    docker system prune -af
    ```

- Ручное обновление:
    - Wireguard/Amnezia — добавлена новая подсеть для выходного узла `az-world`. Нужно скачать новые конфиги.
    - OpenVPN — исправлен баг с дублирующимися маршрутами.  
      Нужно закомментировать `route 10.200.0.0 255.255.255.0` (добавьте `#` в начале) в поле Route (Guest VPN subnet) на странице http://openvpn-ui.antizapret:8080/ov/config и сохранить изменения. Если админ‑панель openvpn-ui не открывается: `rm -rf ./config/openvpn/*`
    - Adguard — нужно удалить старый конфиг: `rm -rf ./config/adguard`
    - Antizapret — AdGuard вынесен в отдельный контейнер, все соответствующие env‑переменные нужно перенести в контейнер `adguard`.  
       Требуется обновить `docker-compose.override.yml`.
    - https/proxy — контейнер `proxy` переименован в `https`. Требуется обновить `docker-compose.override.yml`. И переименовать старую папку конфига: `mv ./config/caddy ./config/https`

    Удалите старую версию:
    ```shell
    docker compose down --remove-orphans
    docker system prune -af
    rm -rf ./config/adguard
    ```

    Затем следуйте шагам установки.

## Сброс

Удалить все настройки, VPN‑конфиги и вернуть сервис в начальное состояние:
```shell
docker stack rm antizapret || docker compose down --remove-orphans
rm -rf config/*
```

# Документация

## Алгоритм DNS‑разрешения

![Preview](./img/chart.png)

1. DNS‑запрос приходит в AdGuardHome
1. AdGuard проверяет его по правилам «чёрного списка». Если домен в чёрном списке — возвращает 0.0.0.0, и клиент не может открыть домен.
1. AdGuard отправляет DNS‑запрос в сервис CoreDNS.
1. CoreDNS отправляет DNS‑запрос на внутренний сервер `dnsmap.py` (контейнер antizapret), а `dnsmap.py` отправляет запрос обратно в AdGuard.
1. AdGuard получает запрос ещё раз, но теперь применяет правила с `$client=az-local` и реальный upstream‑сервер клиента (по умолчанию 8.8.8.8)
1. Если домен в whitelist — AdGuard резолвит его адрес и возвращает в `dnsmap.py`
1. Если домена нет в whitelist — AdGuard возвращает SERVFAIL
1. `dnsmap.py` отправляет ответ в AdGuard:
   1. Если это валидный IP — заменяет его на «внутренний» IP из подсети `10.224.0.0/15`, добавляет masquerade в iptables и возвращает внутренний IP в AdGuard
   1. Если это SERVFAIL — отправляет этот ответ клиенту.
1. Если CoreDNS получает SERVFAIL, он повторяет запрос и отправляет его напрямую в AdGuard. В этом случае правила с `$client=az-local` не применяются и запрос обрабатывается обычным образом.

Почему так сложно?

- Windows и некоторые другие клиенты не повторяют запрос на fallback DNS даже при SERVFAIL. Поэтому добавлен CoreDNS.
- AdGuard не позволяет переопределять upstream в правилах blacklist/whitelist.  
  Но эти правила поддерживают regex и автоматически обновляются, поэтому мы хотим их использовать.
  Поэтому внутри делается несколько запросов от разных «клиентов».
- AdGuard позволяет задавать разные upstream’ы для разных клиентов — значит, можно использовать разные DNS для заблокированных и незаблокированных доменов.


## Добавление доменов

Есть два способа: через пользовательские правила и через списки.

### Добавление доменов через правила

Откройте панель AdGuard: http://adguard.antizapret:3000/#custom_rules  
Правила/синтаксисы: https://adguard-dns.io/kb/general/dns-filtering-syntax/#basic-examples

По умолчанию AdGuard переписывает все запросы в SERVFAIL. Это трюк, чтобы заставить клиента повторить DNS‑запрос на второй, локальный DNS‑сервер.
Правила с модификатором ответа `dnsrewrite` имеют более высокий приоритет, чем другие правила в AdGuard Home и AdGuard DNS.
Чтобы переопределить правило по умолчанию, пользовательские правила должны содержать модификатор `$dnsrewrite`.

Чтобы поддержать стандартные фильтры AdGuard, правило SERVFAIL по умолчанию применяется только к внутренним запросам от `client=az-local` и `client=az-world`.

Примеры:
```
@@||subdomain.host.com^$dnsrewrite,client=az-local
@@||*.host.com^$dnsrewrite,client=az-local
@@||host.com^$dnsrewrite,client=az-world
@@||de^$dnsrewrite,client=az-world

@@/some_.*_regex/$dnsrewrite,client=az-local
```

### Добавление доменов через списки

Также вы можете добавить любые URL’ы в blocklist: http://adguard.antizapret:3000/#dns_blocklist  
Нужно использовать адаптер, чтобы распарсить и привести список к нужному формату.

 - Добавить домены для локального выходного узла: `http://az-local.antizapret/list/?url=<ANY_URL>`
 - Добавить домены для мирового выходного узла: `http://az-world.antizapret/list/?url=<ANY_URL>`

Поддерживаемые форматы: простой список доменов, формат AdGuard, формат hosts, JSON‑массив доменов, список regex.

Опции адаптера:
 - `url` — скачать список по URL
 - `file` — прочитать локальный файл. Используется для include-host-{custom,dist}.txt
 - `filter_custom=1` — фильтровать списки правилами из `exclude-hosts-custom.txt`.
 - `filter_dist=0` — фильтровать списки правилами из `exclude-hosts-dist.txt`
 - `format=list` — `list` или `json`. Определяется автоматически.
 - `client=az-local` — имя клиента, которое добавляется в правила. Определяется автоматически.
 - `allow=1` — отключите эту опцию, чтобы блокировать домены из списка для данного выходного узла.
 - `raw=0` — не модифицировать правила
 - `suffix=1` — добавить `"$dnsrewrite,client=xxx"` к правилам

## Добавление IP/подсетей

Добавляйте IP и подсети в `./config/antizapret/custom/include-ips-custom.txt`.  
Контейнеры периодически проверяют изменения в папке `config` (каждые 5–10 секунд) и перезапускаются/обновляются после любых изменений.

Запустить обновление вручную: `docker exec $(docker ps -q --filter=name=az | head -n1) doall`


## Переменные окружения

Вы можете задать эти переменные в `docker-compose.override.yml` под свои нужды:

Antizapret:  
Состоит из двух контейнеров: `az-local` и `az-world` — это выходные узлы VPN.
- `DNS=adguard` — upstream DNS для резолва заблокированных сайтов (по умолчанию adguard)
- `AZ_SUBNET=10.224.0.0/15` — подсеть для виртуальных адресов заблокированных хостов.
- `ROUTES` — список VPN‑контейнеров и их виртуальных адресов. Используется для сервера iperf3.
- `DOALL_DISABLED=` — пропустить запуск на узле `az-world`.

Adguard:
- `ROUTES` — список VPN‑контейнеров и их виртуальных адресов. Используется для уникальных адресов клиентов в логах AdGuard
- `ADGUARDHOME_PORT=3000`
- `ADGUARDHOME_USERNAME=admin`
- `ADGUARDHOME_PASSWORD=`
- `ADGUARDHOME_PASSWORD_HASH=` — хеш пароля, берётся из файла `AdGuardHome.yaml` после первого запуска с `ADGUARDHOME_PASSWORD`. Знак доллара `$` в хеше нужно экранировать ещё одним долларом: `$$`

CoreDNS:
- Нет

Filebrowser:
- `FILEBROWSER_PORT=admin`
- `FILEBROWSER_PASSWORD=password`

Proxy (https):
- `PROXY_DOMAIN=` — создать HTTPS‑сертификат Let’s Encrypt для домена. Если не задано, будет использован IP хоста для самоподписанного сертификата.
- `PROXY_EMAIL=` — email для сертификата Let’s Encrypt.

Openvpn:
- `ROUTES`
- `OBFUSCATE_TYPE=0` — уровень обфускации протокола OpenVPN.
   0 — отключено. Работает как обычный OpenVPN‑клиент, поддерживается всеми клиентами.  
   1 — лёгкая обфускация, работает с MikroTik  
   2 — сильная обфускация, работает с некоторыми клиентами: OpenVPN GUI client, AsusWRT client...
- `AZ_LOCAL_SUBNET=10.224.0.0/15` — подсеть виртуальных «заблокированных» IP. Локальный выходной узел
- `AZ_WORLD_SUBNET=10.226.0.0/15` — подсеть виртуальных «заблокированных» IP. Удалённый выходной узел

Openvpn-ui:
- `OPENVPN_ADMIN_PASSWORD=` — будет использован как адрес сервера в профилях `.ovpn` при генерации ключей (по умолчанию: IP вашего сервера)
- `OPENVPN_DNS=10.224.0.1` — DNS‑адрес для клиентов. Должен быть в `ANTIZAPRET_SUBNET`
- `OPENVPN_LOCAL_IP_RANGE=10.1.165.0` — подсеть для ovpn‑клиентов. Подсеть можно посмотреть в журнале AdGuard или в панели ovpn‑ui

Wireguard / Wireguard Amnezia:
- `ROUTES`
- `WIREGUARD_PASSWORD=` — пароль для админ‑панели
- `WIREGUARD_PASSWORD_HASH=` — [хеш пароля](https://github.com/wg-easy/wg-easy/blob/v14.0.0/How_to_generate_an_bcrypt_hash.md) для админ‑панели
- `AZ_LOCAL_SUBNET=10.224.0.0/15` — подсеть виртуальных «заблокированных» IP. Локальный выходной узел
- `AZ_WORLD_SUBNET=10.226.0.0/15` — подсеть виртуальных «заблокированных» IP. Удалённый выходной узел
- `WG_DEFAULT_DNS=10.224.0.1` — DNS‑адрес для клиентов. Должен быть в `ANTIZAPRET_SUBNET`
- `WG_PERSISTENT_KEEPALIVE=25`
- `PORT=51821` — порт админ‑панели
- `WG_PORT=51820` — порт WireGuard‑сервера
- `WG_DEVICE=eth0`

## DNS

### Upstream DNS в AdGuard

AdGuard использует Google DNS и Quad9 DNS для резолва незаблокированных доменов. Эти upstream’ы поддерживают ECS‑запросы (подробнее ниже).
Cloudflare DNS не поддерживает ECS и не рекомендуется к использованию.  

Исходный код: [Adguard upstream DNS](./antizapret/root/adguardhome/upstream_dns_file_basis)

После запуска контейнера рабочая копия находится здесь: `./config/adguard/conf/upstream_dns_file_basis`

### CDN + ECS

Некоторые домены могут резолвиться по‑разному в зависимости от подсети (GeoIP) клиента. В таком случае использование DNS, расположенного на удалённом сервере, может «сломать» часть сервисов.
ECS позволяет передавать IP клиента в DNS‑запросах к upstream‑серверу и получать корректные результаты.
ECS включён по умолчанию в AdGuard, IP клиента указывается на Москву (подсеть Яндекса).

Если вы находитесь в другом регионе, замените `77.88.8.8` на ваш реальный IP на странице `http://your-server-ip:3000/#dns`



## OpenVPN

### Создание клиентских сертификатов

https://github.com/d3vilh/openvpn-ui?tab=readme-ov-file#generating-ovpn-client-profiles

1) откройте `http://%your_ip%:8080/certificates`
2) нажмите "create certificate"
3) введите уникальное имя. Остальные поля оставьте пустыми
4) нажмите create
5) нажмите на имя сертификата в списке, чтобы скачать `.ovpn` файл.

### Включение OpenVPN Data Channel Offload (DCO)

[OpenVPN Data Channel Offload (DCO)](https://openvpn.net/as-docs/openvpn-dco.html) даёт прирост производительности, перенося обработку data‑канала в пространство ядра, где это делается эффективнее и с поддержкой многопоточности.  
**tl;dr** — увеличивает скорость и снижает нагрузку на CPU на сервере.

Расширения ядра можно установить только на <u>хост‑машину</u>, не в контейнер.

#### Ubuntu 24.04
```bash
sudo apt install -y openvpn-dco-dkms
```

#### Ubuntu 20.04, 22.04
```bash
sudo apt update
sudo apt upgrade
echo "#### Please reboot your system after upgrade ###" && sleep 100
deb=openvpn-dco-dkms_0.0+git20231103-1_all.deb
sudo apt install -y efivar dkms linux-headers-$(uname -r)
wget http://archive.ubuntu.com/ubuntu/pool/universe/o/openvpn-dco-dkms/$deb
sudo dpkg -i $deb
```

### Поддержка legacy‑клиентов

Если ваши клиенты не поддерживают GCM‑шифры, вы можете использовать legacy CBC‑шифры.
DCO несовместим с legacy‑шифрами и будет отключён. Это также увеличит нагрузку на CPU.


## Amnezia Wireguard

##
# Включение kernel‑модуля Amnezia Wireguard

https://github.com/amnezia-vpn/amneziawg-linux-kernel-module?tab=readme-ov-file#ubuntu

#### Ubuntu 24.04
1. `sudo add-apt-repository ppa:amnezia/ppa`
2. `sudo apt install -y amneziawg`
3. перезапустите сервер или выполните `docker compose restart wireguard-amnezia`
4. проверьте список модулей ядра `dkms status`,  
   и убедитесь, что теперь запущено множество процессов вида `[kworker/X:X-wg-crypt-wg0]`.

#### Ubuntu 20.04, 22.04
1. Отредактируйте `etc/apt/sources.list` и раскомментируйте `deb-src http://archive.ubuntu.com/ubuntu ... main restricted`
2. `sudo apt update`
3. `sudo apt install -y software-properties-common python3-launchpadlib gnupg2 linux-headers-$(uname -r)`
4. установите исходники ядра: `sudo apt-get source linux-image-$(uname -r)`
5. `sudo add-apt-repository ppa:amnezia/ppa`
6. `sudo apt install -y amneziawg`
7. `sudo dkms install -m amneziawg -v 1.0.0`
8. перезапустите сервер или выполните `docker compose restart wireguard-amnezia`
9. проверьте список модулей ядра `dkms status`,  
   и убедитесь, что теперь запущено множество процессов вида `[kworker/X:X-wg-crypt-wg0]`.

### Размер «мусорных» пакетов в Amnezia Wireguard

Amnezia добавляет случайные пакеты, чтобы менять «сигнатуру» WireGuard‑протокола и обходить DPI.  
По умолчанию используется `JMIN=20; JMAX=100` — размер junk‑пакетов в байтах.

Большие junk‑пакеты иногда помогают обойти DPI, но некоторые файрволы могут блокировать их как DDoS‑атаку.
Если у вас проблемы с подключением через amnezia — попробуйте изменить их размер через переменные окружения:

```
Jc=3
Jmin=20
Jmax=100
```
или
```
Jc=2
Jmin=10
Jmax=20
```

Пример фрагмента `docker-compose.override.yml` с JMIN и JMAX:
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

Настройки/env‑переменные сохраняются в папке `./config/wireguard_amnezia/`. Чтобы обновить их, удалите папку и запустите контейнер заново.
Это также удалит всех существующих клиентов/сертификаты.
```shell
docker compose down && rm -rf ./config/wireguard_amnezia/ && docker compose up -d
```

### Блокировки VPN / ограничения хостингов

Многие провайдеры сейчас блокируют VPN на иностранные IP. Обфускация в amnezia или openvpn не всегда решает проблему.
Для более стабильной работы VPN можно подключиться к VPS внутри вашей страны, а затем проксировать трафик на иностранный сервер.

Есть два способа:
1. [Рекомендуется] Установка в [режиме Docker Swarm](#docker-swarm-несколько-выходных-узлов-продвинутый-вариант)
1. Проксировать весь трафик через локальный прокси. См. ниже.

Пример стартового скрипта.  
Замените `<SERVER_IP>` на IP вашего сервера и запустите на свежем VPS (рекомендуется Ubuntu 24.04):

```shell
#!/bin/sh

# Fill with your foreign server ip
export VPN_IP=<SERVER_IP>

echo "net.ipv4.ip_forward=1" >> /etc/sysctl.d/99-sysctl.conf
sysctl -w net.ipv4.ip_forward=1

# DNAT rules
iptables -t nat -A PREROUTING -p tcp ! --dport 22 -j DNAT --to-destination "$VPN_IP"
iptables -t nat -A PREROUTING -p udp ! --dport 22 -j DNAT --to-destination "$VPN_IP"
# MASQUERADE rules
iptables -t nat -A POSTROUTING -p tcp -d "$VPN_IP" -j MASQUERADE
iptables -t nat -A POSTROUTING -p udp -d "$VPN_IP"  -j MASQUERADE

echo iptables-persistent iptables-persistent/autosave_v4 boolean true | sudo debconf-set-selections
echo iptables-persistent iptables-persistent/autosave_v6 boolean false | sudo debconf-set-selections
apt install -y iptables-persistent

```

## Дополнительная информация

- [Гайд по настройке OpenWrt](./docs/guide_OpenWrt.md) — как настроить роутер OpenWrt с этим решением, чтобы LAN‑клиенты работали корректно.
- [Гайд по настройке Keenetic](./docs/guide_Keenetic.md) — инструкции по настройке сервера и подключению роутеров Keenetic к нему [(на русском языке)](./docs/guide_Keenetic_RU.md)

## Проверка скорости с iperf3

Сервер iperf3 включён в контейнер antizapret-vpn.

1. Подключитесь к VPN
2. Используйте iperf3‑клиент на телефоне или компьютере, чтобы проверить скорость upload/download.  
   Пример: 10 потоков на 10 секунд, отчёт каждую секунду:
    ```shell
    # local node
    iperf3 -c az-local.antizapret -i1 -t10 -P10
    iperf3 -c az-local.antizapret -i1 -t10 -P10 -R

   # world node
    iperf3 -c az-world.antizapret -i1 -t10 -P10
    iperf3 -c az-world.antizapret -i1 -t10 -P10 -R
    ```

# Благодарности

- [ProstoVPN](https://antizapret.prostovpn.org) — оригинальный проект
- [AntiZapret VPN Container](https://bitbucket.org/anticensority/antizapret-vpn-container/src/master/) — исходный код контейнера на базе LXD
- [AntiZapret PAC Generator](https://bitbucket.org/anticensority/antizapret-pac-generator-light/src/master/) — генератор PAC (proxy auto‑configuration) для обхода цензуры РФ
- [Amnezia WireGuard VPN](https://github.com/w0rng/amnezia-wg-easy) — используется для интеграции Amnezia Wireguard
- [WireGuard VPN](https://github.com/wg-easy/wg-easy) — используется для интеграции Wireguard
- [OpenVPN](https://github.com/d3vilh/openvpn-ui) — используется для интеграции OpenVPN
- [IPsec VPN](https://github.com/hwdsl2/docker-ipsec-vpn-server) — используется для интеграции IPsec
