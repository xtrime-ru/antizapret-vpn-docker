# Инструкция для маршрутизатора Xiaomi Mi Router AX1800 (RM1800):
- [Инструкция для маршрутизатора Xiaomi Mi Router AX1800 (RM1800):](#инструкция-для-маршрутизатора-xiaomi-mi-router-ax1800-(RM1800))
   - [Подготовка маршрутизатора](#подготовка-маршрутизатора)
      - [Получение SSH доступа](#получение-ssh-доступа)
      - [Краткая справка по VIM](#краткая-справка-по-vim)
      - [Установка OPKG через SSH](#установка-opkg-через-ssh)
   - [OpenVPN](#openvpn)
      - [OpenVPN клиентская часть](#openvpn-клиентская-часть)

## Подготовка маршрутизатора
Работоспособность проверялась только на маршрутизаторе [Xiaomi Mi Router AX1800](https://www.ixbt.com/nw/xiaomi-mi-ax1800-review.html) (чёрный прямоугольник).
Для установки OpenVPN необходим SSH доступ. Чтобы его получить, необходимо понизить прошивку маршрутизатора до [1.0.336](https://cdn.cnbj1.fds.api.mi-img.com/xiaoqiang/rom/rm1800/miwifi_rm1800_firmware_fafda_1.0.336.bin)

### Получение SSH доступа
1. Залогиниться в веб-панель роутера.
2. После логина в адресной строке видим строку, в которой содержится параметр stok. Выглядит примерно так - 62acadc375a9f9d96e421cce50266247
```
http://192.168.31.1/cgi-bin/luci/;stok=<STOK>/web/home#router
```
3. Переходим последовательно по следующим адресам (не забывая менять <STOK> на строку из пункта 2)
```
http://192.168.31.1/cgi-bin/luci/;stok=<STOK>/api/misystem/set_config_iotdev?bssid=Xiaomi&user_id=longdike&ssid=-h%3Bnvram%20set%20ssh%5Fen%3D1%3B%20nvram%20commit%3B
```
```
http://192.168.31.1/cgi-bin/luci/;stok=<STOK>/api/misystem/set_config_iotdev?bssid=Xiaomi&user_id=longdike&ssid=-h%3Bsed%20-i%20's/channel=.*/channel=%5C%22debug%5C%22/g'%20/etc/init.d/dropbear%3B
```
```
http://192.168.31.1/cgi-bin/luci/;stok=<STOK>/api/misystem/set_config_iotdev?bssid=Xiaomi&user_id=longdike&ssid=-h%3B/etc/init.d/dropbear%20start%3B
```
```
http://192.168.31.1/cgi-bin/luci/;stok=<STOK>/api/misystem/set_config_iotdev?bssid=Xiaomi&user_id=longdike&ssid=-h%3B%20echo%20-e%20'admin%5Cnadmin'%20%7C%20passwd%20root%3B
```
4. После этого можно подключиться по SSH. Логин root, пароль admin.
```
ssh root@192.168.31.1
```
5. Чтобы сменить пароль
```
passwd
```

### Краткая справка по VIM
По-умолчанию уже установлен текстовый редактор VIM. Краткая справка по нему:
```
dd - удаление строки
i - вход в режим редактирования
esc - выход из режима редактирования
:w - сохранить
:q - выйти
:q! - форсированный выход без сохранения
:wq - сохранение с выходом
```

### Установка OPKG через SSH
1. Отредактировать файл /etc/opkg/distfeeds.conf
```
src/gz openwrt_base http://downloads.openwrt.org/releases/18.06.9/packages/arm_cortex-a7_neon-vfpv4/base
src/gz openwrt_packages http://downloads.openwrt.org/releases/18.06.9/packages/arm_cortex-a7_neon-vfpv4/packages
src/gz openwrt_routing http://downloads.openwrt.org/releases/18.06.9/packages/arm_cortex-a7_neon-vfpv4/routing
```
2. Отредактировать файл /etc/opkg.conf
```
dest root /
dest ram /tmp
lists_dir ext /var/opkg-lists
option overlay_root /overlay
option check_signature

arch armv7 199
arch arm_cortex-a7 200
arch arm_cortex-a7_neon-vfpv4 201
```
3. Выполнить команду (создает ссылку на отсутствующую библиотеку)
```
ln -s /lib/ld-musl-arm.so.1 /lib/ld-musl-armhf.so.1
```
4. Обновить источники пакетов
```
opkg update
```
5. Для удобства предлагаю установить sftp-server
```
opkg update
opkg install openssh-sftp-server
```

## OpenVPN

Это самый удобный и надёжный способ обхода блокировок.

### OpenVPN клиентская часть

1. Установить через SSH OpenVPN
```
opkg update
opkg install openvpn-openssl
```
2. Исправить файл конфигурации OpenVPN. Шапка конфигурации должна выглядеть так
```
nobind
client
remote <ip> <port>
remote-cert-tls server
dev tun
proto udp
cipher AES-128-CBC
resolv-retry infinite
persist-tun
persist-key

route 77.88.8.8
<ca>
....
```
3. Файл конфигурации .ovpn разместить в /etc/openvpn/
4. Заменить сожержимое файла /etc/config/openvpn
```
package openvpn

config openvpn antizapret

      option enabled 1
      option config /etc/openvpn/antizapret.ovpn
```
5. В файл /etc/config/network добавить
```
config interface 'antizapret'
option ifname 'tun0'
option proto 'none'
option auto '1'
```
6. В файл /etc/config/firewall добавить строку
```list network 'antizapret'```
```
config zone
	option name 'wan'
	list network 'wan'
	list network 'wan6'
	list network 'antizapret'
	option input 'REJECT'
	option output 'ACCEPT'
	option forward 'REJECT'
	option masq '1'
	option mtu_fix '1'

```
7. В веб-панели маршрутизатор установить DNS1 в 77.88.8.8
8. Перезапустить маршрутизатор
```
reboot
```

9. Запретить обновление маршрутизатора через MiWiFi.

**Готово!**