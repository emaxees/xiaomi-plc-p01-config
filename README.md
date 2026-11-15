# Xiaomi PLC P01 - configuración local por miIO

Herramienta de línea de comandos para configurar el extensor Wi‑Fi Powerline de Xiaomi **xiaomi.plc.v1** (hardware **P01**, firmware **1.0.14**) por la red local, **sin la app Mi Home**.

Este modelo se vendió principalmente en China. Si Mi Home rechaza el emparejamiento por la región de la cuenta, igual podés aprovisionarlo y administrarlo con el protocolo miIO en UDP 54321.

Requisitos: Python 3.9+ y el paquete `cryptography`.

```bash
pip install -r requirements.txt
export PLC_IP=192.168.1.X
export PLC_TOKEN=<TOKEN>
python plc.py wifi-get
```

**Nunca subas el token, la contraseña Wi‑Fi ni la NMK del enlace Powerline a un issue, un gist o un commit.**

---

## Español

### Inicio rápido

```bash
git clone https://github.com/emaxees/xiaomi-plc-p01-config.git
cd xiaomi-plc-p01-config
pip install -r requirements.txt

# Descubrir el equipo (en modo fábrica, conectate al AP abierto y usá 192.168.100.1)
python plc.py discover --ip 192.168.100.1

# Guardar credenciales en el entorno (no las dejes en un archivo versionado)
export PLC_IP=192.168.1.X
export PLC_TOKEN=<TOKEN>

python plc.py info
python plc.py devinfo
python plc.py wifi-get
python plc.py wifi-set --channel 6 --bw 20 --mgmt WPA2 --yes
python plc.py plc-link
python plc.py clients
python plc.py reboot
```

En Windows PowerShell:

```powershell
$env:PLC_IP="192.168.1.X"
$env:PLC_TOKEN="<TOKEN>"
python plc.py wifi-get
```

PowerShell suele romper el JSON si lo pasás entre comillas. Para un método crudo, escribí los parámetros en un archivo:

```powershell
python plc.py call set_wifi @params.json
```

### Cómo obtener el token

1. Restablecé el PLC a fábrica (el botón de reset; el firmware vuelve a modo Router y deja el Wi‑Fi abierto).
2. Se publica un AP abierto con el nombre `xiaomi-plc-v1_miapXXXX` (XXXX son los últimos cuatro caracteres hex del MAC de la radio).
3. Conectate a ese AP. El LAN del equipo queda en **192.168.100.1** (así lo define `/etc/init.d/network` cuando no está en modo Relay). Conviene verificarlo con `discover`.
4. En ese estado, el paquete *hello* de miIO **devuelve el token** en el campo de checksum:

   ```bash
   python plc.py discover --ip 192.168.100.1
   ```

5. Guardá el token. Después del aprovisionamiento el equipo pide una IP por DHCP en tu LAN y el *hello* ya no expone el token (el campo viene en ceros).

### Aprovisionar sin la app

Esto funciona aunque Mi Home falle por un desajuste de región. Desde el AP de fábrica:

```bash
python plc.py --ip 192.168.100.1 --token <TOKEN> provision \
  --ssid "HomeWiFi" \
  --password "<PASSWORD>" \
  --tz America/Argentina/Buenos_Aires \
  --gmt-offset -10800 \
  --cc AR
```

`provision` envía `miIO.config_router` con:

```json
{
  "ssid": "HomeWiFi",
  "passwd": "<PASSWORD>",
  "uid": 0,
  "gmt_offset": -10800,
  "tz": "America/Argentina/Buenos_Aires",
  "config_type": "app",
  "country_domain": "cn",
  "wifi_config": {"cc": "AR"}
}
```

`country_domain` tiene que quedar en `cn`: el hardware es de región China. `tz`, `gmt_offset` y `wifi_config.cc` sí se adaptan a tu país.

Qué hace el firmware después:

1. `miio_client` escribe `/etc/miio/wifi.conf` (`ssid`, `psk`, `key_mgmt=WPA` o `NONE`) y dispara `local.status` / `wifi_connecting`.
2. El extensor se une a tu red, pasa a modo **Relay** y clona el SSID como **`<SSID>_plus`** (si el SSID de casa tiene más de 26 caracteres, primero lo recorta y recién después agrega `_plus`).
3. La contraseña es la misma. El cliente de fábrica deja `key_mgmt=WPA` (cifrado `psk`). Si querés WPA2, después corré `wifi-set --mgmt WPA2 --yes`.

### Comandos principales

| Comando | Qué hace |
| --- | --- |
| `discover` | Envía un *hello*. Imprime device id y, si el equipo no está aprovisionado, el token. Sin `--ip` hace broadcast a `255.255.255.255`. |
| `info` | `miIO.info` |
| `devinfo` | `get_devinfo`: MAC, IP, versión de firmware, modo (`Relay` / `Router`) |
| `wifi-get` | `get_wifi`. Enmascara `pswd` salvo que pases `--show-password` |
| `wifi-set` | Lee la config actual, fusiona tus flags, muestra el diff y **solo aplica con `--yes`** |
| `plc-link` | `avln_info` + `avln_stas`: tasas PHY del enlace eléctrico y una pista de calidad. No imprime la NMK |
| `clients` | `extender_stas`: clientes Wi‑Fi asociados |
| `provision` | `miIO.config_router` (ver arriba) |
| `reboot` | `miIO.reboot` (el firmware espera ~5 s y reinicia) |
| `call <método> [json]` | Llamada cruda. Los parámetros pueden ser JSON o `@archivo.json` |

`set_wifi` **pisa todos los campos**. Por eso `wifi-set` siempre lee `get_wifi` antes y reenvía ssid, contraseña, mgmt, hidden, channel y bw. El radio se reinicia unos segundos.

Cambio de canal verificado en 1.0.14: canal 4 → 6, y mgmt WPA → WPA2.

### Elegir un canal 2.4 GHz

El PLC solo emite en 2.4 GHz. Usá 1, 6 u 11, el que esté menos congestionado:

```bash
# Windows
netsh wlan show networks mode=bssid

# Linux
nmcli -f SSID,CHAN,SIGNAL,BARS dev wifi list
# o: iw dev wlan0 scan | grep -E 'SSID|freq|signal|primary channel'
```

Un vecino fuerte en el mismo canal (por ejemplo el 4) degrada bastante el enlace.

### Protocolo miIO (resumen)

- UDP puerto **54321**.
- *Hello* de 32 bytes: `0x2131 0x0020` seguido de 28 bytes `0xFF`. La respuesta trae device id y *stamp*. Si el equipo no está aprovisionado, el campo checksum de 16 bytes es el token; si ya está en la LAN, viene en ceros.
- Cifrado: **AES-128-CBC** con padding PKCS7. `key = md5(token)`, `iv = md5(key + token)`. El token son 16 bytes (32 caracteres hex).
- Encabezado: magic `0x2131`, longitud, `0x00000000`, device id, stamp (el del *hello* + 1, y después se incrementa), checksum = `md5(encabezado_con_el_token_en_el_campo_checksum + payload_cifrado)`.
- Payload JSON: `{"id":n,"method":"...","params":...}`.

Esta herramienta espera 1.5 s entre llamadas de una misma corrida y reintenta si no hay respuesta.

### Métodos del firmware

El despachador está en `/etc/miio/lib_miio_method.sh`. Los nombres con punto se convierten en un solo `_` (`miIO.reboot` → `miIO_reboot`, `local.status` → `local_status`).

**Sin prefijo `miIO.`:** `get_wifi`, `set_wifi`, `get_roam`, `set_roam`, `get_devinfo`, `extender_stas`, `avln_info`, `avln_stas`, `set_bind_key`, `bind_key`, `local.status`, `log_upload`.

**Con prefijo, implementados en el script:** `miIO.ota`, `miIO.get_ota_state`, `miIO.get_ota_progress`, `miIO.reboot`, `miIO.restore`.

**Built-in en `miio_client`:** `miIO.info`, `miIO.config`, `miIO.config_router`, `miIO.wifi_assoc_state`, `miIO.stop_diag_mode`.

Detalle de parámetros y respuestas: [docs/methods.md](docs/methods.md).

### Advertencias

- **Nunca** llames `miIO.restore` salvo que quieras un reset de fábrica. El firmware deja el Wi‑Fi **abierto**, borra `wifi.conf` y vuelve a modo Router. `plc.py call miIO.restore` se niega, a menos que pases `--i-know`.
- Evitá `set_bind_key`, `bind_key`, `local.status` y `miIO.ota` si no sabés exactamente qué hacen. `local.status` es una señal interna de `miio_client` y puede cambiar el modo de trabajo. `miIO.ota` descarga e instala un firmware. `log_upload` manda logs a la nube de Xiaomi.
- `set_roam` solo tiene sentido con un router MiWiFi. Con `roaming=1` copia SSID y clave desde `xiaoqiang.cfgsync`; si eso está vacío, podés dejar el AP sin red utilizable.
- El enlace Powerline **no tiene knobs de configuración**. `avln_info` / `avln_stas` solo leen tasas PHY (`rxdr` / `txdr`, en Mbps). Un enlace de 53/59 Mbps rinde más o menos la mitad en la práctica (unos 25-30 Mbps). Con buen cableado estos equipos negocian 150-300 Mbps PHY. Influyen la fase eléctrica, las zapatillas con protector de picos y los electrodomésticos ruidosos.
- Métodos desconocidos: si llegan al script y no están en la lista, la respuesta es `{"result":[0],"code":"error","message":""}`. Si un handler falla, el código es `-(5000 + valor_de_retorno)`, por ejemplo `-5001`. En otros dispositivos miIO a veces aparece `-9999`; en este firmware ese string no está en `miio_client`.

### Firmware

Imagen analizada:

- <https://bigota.miwifi.com/xiaoqiang/rom/p01/miwifi_p01_firmware_1.0.14_8ce02.bin>
- MD5: `57514b9549073e4a7cca5363eae8ce02`

Se extrajo con `binwalk -e` (squashfs). Los métodos salen de `squashfs-root/etc/miio/lib_miio_method.sh`, `miio_al.sh`, `lib_ota.sh` y de strings de `usr/bin/miio_client`. Este repo **no** incluye archivos del firmware (son de Xiaomi).

### Limitaciones

Por configuración local ya se puede: aprovisionar sin la app, fijar canal/ancho/cifrado del Wi‑Fi, leer el enlace eléctrico y listar clientes. Lo que no se puede es mejorar el cableado de la casa por software. Si el PHY del PLC está en ~55 Mbps, el techo real ronda los 25-30 Mbps.

---

## English

A command-line tool to configure the Xiaomi Wi‑Fi Powerline extender **xiaomi.plc.v1** (hardware **P01**, firmware **1.0.14**) on the local network, **without the Mi Home app**.

The model is China-region hardware. If Mi Home rejects pairing because of an account-region mismatch, you can still provision and manage it with the miIO protocol on UDP 54321.

Requires Python 3.9+ and the `cryptography` package.

### Quick start

```bash
git clone https://github.com/emaxees/xiaomi-plc-p01-config.git
cd xiaomi-plc-p01-config
pip install -r requirements.txt

# Factory-reset AP mode: join xiaomi-plc-v1_miapXXXX, then:
python plc.py discover --ip 192.168.100.1

export PLC_IP=192.168.1.X
export PLC_TOKEN=<TOKEN>

python plc.py info
python plc.py devinfo
python plc.py wifi-get
python plc.py wifi-set --channel 6 --bw 20 --mgmt WPA2 --yes
python plc.py plc-link
python plc.py clients
python plc.py reboot
```

On Windows PowerShell, set the same environment variables (`$env:PLC_IP`, `$env:PLC_TOKEN`). PowerShell quoting will mangle JSON; pass raw method params through a file:

```powershell
python plc.py call set_wifi @params.json
```

### Getting the token

1. Factory-reset the PLC. Firmware returns to Router mode and opens the Wi‑Fi.
2. It broadcasts an open AP named `xiaomi-plc-v1_miapXXXX` (last four hex characters of the radio MAC).
3. Join that AP. The device LAN address is **192.168.100.1** (set by `/etc/init.d/network` when not in Relay mode). Confirm with `discover`.
4. While unprovisioned, a miIO *hello* **returns the token** in the checksum field:

   ```bash
   python plc.py discover --ip 192.168.100.1
   ```

5. Save the token. After provisioning, the device takes a DHCP address on your LAN and *hello* no longer exposes the token (the field is zeroes).

### Provisioning without the app

This works even when Mi Home fails due to a region mismatch. From the factory-reset AP:

```bash
python plc.py --ip 192.168.100.1 --token <TOKEN> provision \
  --ssid "HomeWiFi" \
  --password "<PASSWORD>" \
  --tz America/Argentina/Buenos_Aires \
  --gmt-offset -10800 \
  --cc AR
```

That sends `miIO.config_router` with:

```json
{
  "ssid": "HomeWiFi",
  "passwd": "<PASSWORD>",
  "uid": 0,
  "gmt_offset": -10800,
  "tz": "America/Argentina/Buenos_Aires",
  "config_type": "app",
  "country_domain": "cn",
  "wifi_config": {"cc": "AR"}
}
```

Leave `country_domain` set to `cn` (China-only hardware). Adapt `tz`, `gmt_offset`, and `wifi_config.cc` to your locale.

What the firmware does next:

1. `miio_client` writes `/etc/miio/wifi.conf` (`ssid`, `psk`, `key_mgmt=WPA` or `NONE`) and fires `local.status` / `wifi_connecting`.
2. The extender joins your network, switches to **Relay** mode, and clones the SSID as **`<SSID>_plus`**. Home SSIDs longer than 26 characters are truncated before `_plus` is appended.
3. The password is reused. The stock client writes `key_mgmt=WPA` (cipher `psk`). Raise it to WPA2 afterwards with `wifi-set --mgmt WPA2 --yes`.

### Main commands

| Command | What it does |
| --- | --- |
| `discover` | Sends a *hello*. Prints the device id and, if unprovisioned, the token. With no `--ip`, broadcasts to `255.255.255.255`. |
| `info` | `miIO.info` |
| `devinfo` | `get_devinfo`: MAC, IP, firmware version, mode (`Relay` / `Router`) |
| `wifi-get` | `get_wifi`. Masks `pswd` unless you pass `--show-password` |
| `wifi-set` | Reads the current config, merges your flags, prints a diff, and **applies only with `--yes`** |
| `plc-link` | `avln_info` + `avln_stas`: powerline PHY rates plus a quality hint. NMK is omitted |
| `clients` | `extender_stas`: associated Wi‑Fi stations |
| `provision` | `miIO.config_router` (see above) |
| `reboot` | `miIO.reboot` (firmware waits ~5 s, then reboots) |
| `call <method> [json]` | Raw call. Params may be a JSON string or `@file.json` |

`set_wifi` **overwrites every field**. That is why `wifi-set` always reads `get_wifi` first and writes ssid, password, mgmt, hidden, channel, and bw back. The radio restarts for a few seconds.

Verified on 1.0.14: channel 4 → 6, and mgmt WPA → WPA2.

### Picking a 2.4 GHz channel

The PLC radios are 2.4 GHz only. Stick to 1, 6, or 11, whichever is least crowded:

```bash
# Windows
netsh wlan show networks mode=bssid

# Linux
nmcli -f SSID,CHAN,SIGNAL,BARS dev wifi list
```

A strong neighbour on the same channel (channel 4 is a common collision) will hurt the link.

### miIO protocol

- UDP port **54321**.
- 32-byte *hello*: `0x2131 0x0020` followed by 28 bytes of `0xFF`. The reply carries the device id and stamp. If the device is unprovisioned, the 16-byte checksum field is the token; once it is on the LAN, that field is zeroes.
- Encryption: **AES-128-CBC** with PKCS7 padding. `key = md5(token)`, `iv = md5(key + token)`. The token is 16 bytes (32 hex characters).
- Header: magic `0x2131`, length, `0x00000000`, device id, stamp (hello stamp + 1, then increment), checksum = `md5(header_with_token_in_the_checksum_field + encrypted_payload)`.
- JSON payload: `{"id":n,"method":"...","params":...}`.

The CLI waits 1.5 s between calls in a single run and retries on timeout.

### Firmware methods

The dispatcher lives in `/etc/miio/lib_miio_method.sh`. A single `.` in the method name is rewritten to `_` (`miIO.reboot` → `miIO_reboot`, `local.status` → `local_status`).

**Unprefixed script methods:** `get_wifi`, `set_wifi`, `get_roam`, `set_roam`, `get_devinfo`, `extender_stas`, `avln_info`, `avln_stas`, `set_bind_key`, `bind_key`, `local.status`, `log_upload`.

**Prefixed script methods:** `miIO.ota`, `miIO.get_ota_state`, `miIO.get_ota_progress`, `miIO.reboot`, `miIO.restore`.

**Built into `miio_client`:** `miIO.info`, `miIO.config`, `miIO.config_router`, `miIO.wifi_assoc_state`, `miIO.stop_diag_mode`.

Parameter and return formats: [docs/methods.md](docs/methods.md).

### Warnings

- **Never** call `miIO.restore` unless you want a factory reset. Firmware leaves Wi‑Fi **open**, deletes `wifi.conf`, and switches back to Router mode. `plc.py call miIO.restore` refuses unless you also pass `--i-know`.
- Avoid `set_bind_key`, `bind_key`, `local.status`, and `miIO.ota` unless you know exactly what they do. `local.status` is an internal `miio_client` signal and can change the work mode. `miIO.ota` downloads and flashes firmware. `log_upload` sends logs to Xiaomi's cloud.
- `set_roam` is only meaningful with a MiWiFi router. `roaming=1` copies SSID and password from `xiaoqiang.cfgsync`; if that is empty you can wipe a usable AP.
- The powerline link **has no configuration knobs**. `avln_info` / `avln_stas` only report PHY rates (`rxdr` / `txdr`, in Mbps). A 53/59 Mbps link typically delivers about half of that in real traffic (around 25-30 Mbps). Good household wiring negotiates 150-300 Mbps PHY. Electrical phase, surge-protected power strips, and noisy appliances all matter.
- Unknown methods: if they reach the shell dispatcher and are not in `method_list`, the reply is `{"result":[0],"code":"error","message":""}`. Failed handlers return `-(5000 + return_value)`, e.g. `-5001`. Other miIO devices sometimes answer with `-9999`; that string is not present in this firmware's `miio_client`.

### Firmware image

- <https://bigota.miwifi.com/xiaoqiang/rom/p01/miwifi_p01_firmware_1.0.14_8ce02.bin>
- MD5: `57514b9549073e4a7cca5363eae8ce02`

Extracted with `binwalk -e` (squashfs). Methods were taken from `squashfs-root/etc/miio/lib_miio_method.sh`, `miio_al.sh`, `lib_ota.sh`, and from strings in `usr/bin/miio_client`. This repository does **not** vendor firmware files (they belong to Xiaomi).

### Limits

Local configuration can provision the extender without the app, set the Wi‑Fi channel, width and cipher, read the powerline link, and list clients. It cannot fix house wiring. If the PLC PHY sits around 55 Mbps, real throughput tops out near 25-30 Mbps.

### License

MIT. See [LICENSE](LICENSE).
