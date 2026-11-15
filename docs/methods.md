# Method reference (xiaomi.plc.v1 / P01, firmware 1.0.14)

Formats below were checked against the firmware scripts in `/etc/miio/` and against strings in `usr/bin/miio_client`. This is not an official Xiaomi API document.

The adapt layer (`miio_al.sh`) rewrites the first `.` in a method name to `_` before dispatch, so `miIO.reboot` is looked up as `miIO_reboot` and `local.status` as `local_status`. Built-in commands are handled inside `miio_client` and never reach that list.

Unless noted, the shell wraps successful results with `json_method_ret`, which produces:

```json
{"id": <n>, "result": [<value>], "code": 0, "message": ""}
```

Objects therefore arrive as a one-element array (`[{"mac": "..."}]`), and strings as `["ok"]`. `get_wifi` builds its own reply but uses the same array shape.

Handler failures use `code = -(5000 + return_value)` and still include whatever `result` the handler left (often `"error"` or `{}`).

Unknown methods that reach the adapt layer and are not in `method_list` get:

```json
{"id": <n>, "result": [0], "code": "error", "message": ""}
```

(`json_method_ret "$mid" 0 "\"error\""` - the code field is the string `"error"`, not an integer.) Other miIO devices sometimes return `{"error":{"code":-9999,"message":"..."}}`. That `-9999` string is **not** present in this firmware's `miio_client` binary.

`plc.py call` prints the raw JSON object the device sent.

---

## Safe / everyday methods

### `get_wifi`

Script handler: `method_get_wifi_result`. Params: `[]` or unused.

Reads UCI `wireless.@wifi-iface[0]` and `wireless.wifi0`, plus the live radio via `iwinfo` / `iwpriv`.

```json
{
  "id": 1,
  "result": [
    {
      "pswd": "<wifi password>",
      "ssid": "HomeWiFi_plus",
      "hidden": 0,
      "channel": 6,
      "bw": 20,
      "channelInfo": {"channel": 6, "bw": 20},
      "mgmt": "WPA2"
    }
  ],
  "code": 0,
  "message": ""
}
```

| Field | Meaning |
| --- | --- |
| `ssid`, `pswd` | Current AP credentials. `pswd` is omitted / empty when `mgmt` is `NONE`. |
| `hidden` | `0` visible, `1` hidden. Defaults to `0` if unset. |
| `channel` | Configured channel. `0` means auto (`uci` value `auto` / `AUTO`). |
| `bw` | Configured width. Forced to `0` when the channel is auto. |
| `channelInfo.channel` / `channelInfo.bw` | **Live** channel and HT width reported by the radio. |
| `mgmt` | `NONE` (UCI `none` or empty), `WPA/WPA2` (`mixed-psk`), `WPA2` (`psk2`), `WPA` (`psk`). Anything else is reported as `WPA/WPA2`. |

This handler sends the reply itself (and clears `mid` so the main loop does not answer twice). It also pushes `prop_stacount`, `prop_wifi`, and `prop_roaming` in the background.

### `set_wifi`

Script handler: `method_set_wifi_result`. **Overwrites every field** it reads from the JSON. If you omit `ssid` or `pswd`, the firmware writes empty values and can take the AP down. Always `get_wifi` first and echo the current values back, which is what `plc.py wifi-set` does.

```json
{
  "ssid": "HomeWiFi_plus",
  "pswd": "<wifi password>",
  "mgmt": "WPA2",
  "hidden": 0,
  "channel": 6,
  "bw": 20
}
```

Behaviour, from the script:

- If `channel` is missing, `"0"`, or `"auto"`, firmware stores channel `auto` and forces `bw=0`.
- Otherwise it writes `wireless.wifi0.channel` and `wireless.wifi0.bw`.
- `ssid` and `hidden` go to `wireless.@wifi-iface[0]`.
- `mgmt` mapping:
  - `NONE` → UCI `encryption=none`, key deleted
  - `WPA/WPA2` → `mixed-psk`
  - `WPA2` → `psk2`
  - `WPA` → `psk`
  - anything else → `mixed-psk`
- Then `uci commit wireless` and `wifi &` (radio restart, a few seconds).
- Result: `"ok"` (on the wire: `["ok"]`).

Verified on 1.0.14: changing channel `4` → `6` and `mgmt` `WPA` → `WPA2`.

### `get_devinfo`

Script handler: `method_get_devinfo_result`. Params unused.

```json
{
  "mac_lan": "00:11:22:33:44:55",
  "mac_wifi": "00:11:22:33:44:56",
  "ip": "192.168.1.X",
  "ver": "1.0.14",
  "channel": "<firmware channel, e.g. release>",
  "mode": "Relay"
}
```

`ver` and `channel` come from `/usr/share/xiaoqiang/xiaoqiang_version`. `mode` is `work_mode get` (`Relay` once provisioned, `Router` in factory-reset / AP mode).

### `avln_info`

Script handler: `method_avln_info_result`. Local powerline station plus the link to its peer.

On success:

```json
{
  "mac": "00:11:22:33:44:55",
  "hfid": "P01_PLW",
  "nmk": "<32 hex chars>",
  "stat": "ok",
  "rxdr": "53",
  "txdr": "59"
}
```

| Field | Meaning |
| --- | --- |
| `mac` | Local station MAC |
| `hfid` | `P01_PLC` if `is_sta_plc`, otherwise `P01_PLW` (the Wi‑Fi unit) |
| `nmk` | Powerline **network membership key**. Treat it as a secret; `plc.py plc-link` hides it |
| `stat` | `ok` if `is_in_network`, else `error` |
| `rxdr`, `txdr` | PHY rates in Mbps, **as strings**. The script copies the other direction if one side reports 0 (a firmware quirk) |

If the local STA is not valid, the handler returns `{}` and code `-5001`.

PHY rates of 53/59 Mbps typically yield about 25-30 Mbps of real traffic. Healthy household wiring is usually 150-300 Mbps PHY. There is no method to change powerline power, band, or mode.

### `avln_stas`

Script handler: `method_avln_stas_result`. Remote stations on the same AVLN.

```json
{
  "stat": {
    "plcsta": "00:11:22:33:44:b1",
    "nmk": "<32 hex chars>",
    "count": 1
  },
  "list": [
    {"mac": "aa:bb:cc:dd:ee:f0", "rxdr": 100, "txdr": 70}
  ]
}
```

`rxdr` / `txdr` here are **numbers**, not strings. The local PLC STA is dropped from `list`; the local PLW MAC is included. Errors: `-5001` local STA invalid, `-5002` no valid remote STA. Both cases return `{}`.

### `extender_stas`

Script handler: `method_extender_stas_result`. Wi‑Fi stations associated to the extender radio (`wlanconfig <ifname> list`, default `wl1`).

```json
{
  "sta": {"count": 2, "access_policy": 0},
  "mat": [
    {"mac": "aa:bb:cc:dd:ee:f0", "last_time": "123", "ip": "192.168.1.X"}
  ],
  "access_list": ""
}
```

`access_policy`: `0` none, `1` allow (`macfilter=allow`), `2` deny. `access_list` is the UCI `maclist` joined with `;`. IPs come from the ARP table (and optionally trafficd). `last_time` is the association-time column from `wlanconfig`.

### `get_roam`

Script handler: `method_get_roam_result`.

```json
{"roam_valid": 0, "roaming": 0}
```

`roaming` is `mijia.common.roaming` (default 0). `roam_valid` is `1` only when `xiaoqiang.common.ap_hostname` starts with `MiWiFi` - i.e. the uplink is a Xiaomi router. On a generic home AP it stays 0.

### `miIO.info`

Built into `miio_client`. Standard Xiaomi device info (model `xiaomi.plc.v1`, firmware, hardware, uptime, netif). Used by `plc.py info`.

### `miIO.wifi_assoc_state`

Built into `miio_client`. Reply template found in the binary:

```json
{
  "id": 1,
  "code": 0,
  "message": "ok",
  "result": {
    "state": "<state>",
    "auth_fail_count": 0,
    "conn_success_count": 1,
    "conn_fail_count": 0,
    "dhcp_fail_count": 0
  }
}
```

### `miIO.reboot`

Script handler: `method_miIO_reboot_result`. Params unused. Schedules `(sleep 5; reboot) &` and returns `"ok"`.

### `miIO.config_router`

Built into `miio_client` (string `Got miIO.config_router.`). Used by `plc.py provision`. The C client itself validates and writes `ssid`, `uid`, and `passwd` into `/etc/miio/wifi.conf`:

```
# Wifi config file, user & passwd
ssid="<ssid>"
psk="<passwd>"
key_mgmt=WPA
```

If there is no password it writes `key_mgmt=NONE` instead of `psk`. It then emits `{"method": "local.status", "params": "wifi_connecting"}` and replies `{"id":n,"result":["OK"]}`.

The official app / python-miio also send these fields (accepted in practice; `country_domain` must stay `cn` on this hardware):

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

`local.status` / `wifi_connecting` then clones the SSID as `<ssid>_plus` (after `ssid_truncate` to 26 characters) and maps `key_mgmt`:

- `NONE` → open
- `WPA2` → `psk2`
- `WPA` → `psk`
- else → `mixed-psk`

Because the stock client writes `key_mgmt=WPA`, a freshly provisioned `_plus` network is WPA (`psk`). Raise it with `set_wifi` / `wifi-set --mgmt WPA2`.

Validation strings in the binary: `ssid is not a string`, `ssid too long.`, `uid not found, or uid not int.`, `passwd is not a string`, `passwd too long.`

---

## Methods to use with care

### `set_roam`

```json
{"roaming": 0}
```

- `roaming=1`: copies `ssid` / `pswd` / `mgmt` / `hidden` from `xiaoqiang.cfgsync` (only populated when the uplink is a MiWiFi router) and runs `wifi &`. If that UCI is empty, you can wipe a working AP.
- `roaming=0`: sets the SSID to `<truncated ssid>_plus` and runs `wifi &`.
- Result: `"ok"`. Also pushes `prop_roaming`.

### `miIO.config`

Built into `miio_client`. Takes `{"enauth": <int>}`. The binary logs `miIO.config, enauth: %d`. Do not toggle this unless you know you need it.

### `miIO.get_ota_state` / `miIO.get_ota_progress`

Script wrappers around `/etc/miio/lib_ota.sh`. State is derived from files under `/tmp/run/ota/` and may be `"downloading"`, `"downloaded"`, `"installing"`, `"installed"`. Progress is a percentage (download mapped 0-100, or 0-40 plus an install estimate when install ratios are enabled). Safe to read; they do not start an upgrade.

### `miIO.stop_diag_mode`

Built into `miio_client`. No parameters observed in the binary. Diagnostic-mode teardown; usually unnecessary.

---

## Dangerous methods

`plc.py call` prints a warning for these. `miIO.restore` is refused unless you pass `--i-know`.

### `miIO.restore`

Script handler: `method_miIO_restore_result`. **Factory-reset of the miIO / Wi‑Fi state.**

- Rebuilds an **open** AP: SSID = `nvram wl_ssid` plus the last four hex characters of the radio MAC (the same `xiaomi-plc-v1_miapXXXX` pattern used in other restore paths), `hidden=0`, `encryption=none`, key / MAC filter deleted.
- Clears roam flags, deletes `/etc/miio/wifi.conf`.
- After the `"ok"` reply, the adapt layer runs `work_mode Router` and restarts `miio_client`.

The Wi‑Fi is left open on purpose so Mi Home (or `discover`) can find it again.

### `miIO.ota`

```json
{
  "app_url": "https://...",
  "file_md5": "<md5>",
  "proc": "dnld install",
  "mode": "..."
}
```

If `params` is `null` or `{}`, the handler returns `"error"` and code `-5001`. Otherwise it starts `run_ota` in the background (download with `curl`, MD5 check, `flash.sh`, then `reboot`) and immediately returns `"ok"`. A failed download or flash can brick or temporarily disable Wi‑Fi (`ota_prepare` runs `wifi down`). Do not call this unless you are flashing a known image.

OTA error tokens from `lib_ota.sh` include `-33001|down_error`, `-33005|md5_error`, `-33006|xqcheck_error` / `memory_not_enough`, `-33007|fw_not_valid`.

### `set_bind_key`

Used by Mi Home to bind the device to a cloud account. Params include `bind_key`, optional `ssid`, optional `pswd`.

The handler refuses if the device is already bound (`mijia.common.init_done=1`, `-5001`), if WAN is down (`-5002` … `-5005`), or if `bind_key` is missing (`-5006`). On success it may change the SSID (default `Xiaomi_plc_<suf>`), writes a dummy `wifi.conf`, and restarts `miio_client`. A failed cloud bind path restores the open `xiaomi-plc-v1_miapXXXX` AP. Do not use this for local-only setup; `miIO.config_router` is enough.

### `bind_key`

Confirm step for the bind flow. Returns `"ok"`. Avoid unless you are completing a bind you started on purpose.

### `local.status`

Internal callback from `miio_client`, **not** a user command. `params` is a bare string, not an object, for example `"wifi_connecting"`.

| `params` | Effect |
| --- | --- |
| `wifi_connecting` | Reads `wifi.conf`, sets the cloned `<ssid>_plus` AP, pushes props, switches `work_mode Relay`. Ignored if already initialized (`init_done=1`). |
| `wifi_connected` | Pushes props (optionally `bind_key`) and switches to Relay. On bind failure, deletes `wifi.conf` and restarts the client. |
| `wifi_ap_mode` | Logged only. |
| `cloud_connected` | Resets the cloud retry counter, pushes props, sets the WAN-valid LED. |
| `cloud_retry` | After six retries while WAN is up, restarts `miio_client`. |
| anything else | Logged as unsupported. |

Calling this by hand can flip Router/Relay mode or restart the client.

### `log_upload`

Runs `log_collection.sh` and `log_upload` in the background, then returns `"ok"`. That sends device logs to Xiaomi's cloud. Skip it unless you intend that.

---

## Error code summary

| Code | Source |
| --- | --- |
| `0` | Success |
| `"error"` (string) plus `result: [0]` | Method reached `miio_al.sh` but is not in `method_list` |
| `-5001`, `-5002`, … | Script handler `return N` became `-(5000+N)` |
| `-5001` on `avln_info` | Local PLC STA not valid |
| `-5001` / `-5002` on `avln_stas` | Local STA invalid / no remote STA |
| `-5001` on `miIO.ota` | Missing or empty params |
| `-5001`…`-5006` on `set_bind_key` | Already bound, WAN problems, or missing `bind_key` |
| `-30011` | `miio_client` cloud path: `OT server disconnected` / `OT server noack` (cloud, not local UDP) |
| `-33001`, `-33005`, `-33006`, `-33007` | OTA download / MD5 / flash failures (reported via `ota_state` props) |

---

## Built-in vs script (complete list)

| Method | Where | Notes |
| --- | --- | --- |
| `get_wifi` | script | Safe to read |
| `set_wifi` | script | Overwrites all fields; radio restarts |
| `get_roam` | script | `roam_valid` only with a MiWiFi uplink |
| `set_roam` | script | Dangerous without a MiWiFi router |
| `get_devinfo` | script | MAC, IP, firmware, mode |
| `extender_stas` | script | Associated Wi‑Fi clients |
| `avln_info` | script | Local powerline PHY rates |
| `avln_stas` | script | Peer powerline PHY rates |
| `set_bind_key` | script | Cloud bind; avoid |
| `bind_key` | script | Bind confirm; avoid |
| `local.status` | script | Internal; avoid |
| `log_upload` | script | Uploads logs to Xiaomi |
| `miIO.ota` | script | Flashes firmware; dangerous |
| `miIO.get_ota_state` | script | Read-only |
| `miIO.get_ota_progress` | script | Read-only |
| `miIO.reboot` | script | Reboots after ~5 s |
| `miIO.restore` | script | Factory reset, open Wi‑Fi |
| `miIO.info` | `miio_client` | Device info |
| `miIO.config` | `miio_client` | `enauth` |
| `miIO.config_router` | `miio_client` | Provision onto a home SSID |
| `miIO.wifi_assoc_state` | `miio_client` | Association counters |
| `miIO.stop_diag_mode` | `miio_client` | Ends diagnostic mode |
