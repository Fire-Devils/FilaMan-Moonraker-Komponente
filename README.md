# FilaMan Moonraker Component

*[Deutsche Version](README.de.md)*

Native Moonraker component for FilaMan.

## Features

- Tracks active spool via Moonraker endpoint and remote method
- Collects extrusion length from Klipper toolhead updates
- Converts length (mm) to weight (g) using spool filament metadata
- Reports usage to native FilaMan endpoint `/api/v1/spools/{id}/consumptions`
- Uses PLA fallback density (`1.24 g/cm3`) when filament density is missing
- Watches Klipper filament sensors and releases the spool when a toolhead runs empty
- Spoolman-compatible aliases for endpoints/events/remote method are always enabled

## Installing on the Snapmaker U1

Out of the box the U1 allows no SSH access, and files you add do not survive a reboot. Two
preparation steps are therefore required before installing this component: the custom
firmware and debug mode.

### 1. Flash the paxx12 custom firmware

The component requires the
[SnapmakerU1 Extended Firmware](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware),
which enables SSH access. Copy the `.bin` onto a FAT32-formatted USB drive and install it
on the printer via **About → Firmware version → Local Update**; the
[installation guide](https://snapmakeru1-extended-firmware.pages.dev/install.html)
covers this in detail.

The printer is then reachable over SSH (port 22, user `root` or `lava`, password
`snapmaker`):

```bash
ssh lava@<printer-ip>
```

> This is root access with a default password. Only use it on a trusted network, and
> change the password after the first login.

### 2. Enable debug mode

Without debug mode the filesystem is reset on every reboot — the component you copied
would be gone the next time the printer starts. Debug mode is enabled by an empty `.debug`
file:

```bash
touch /oem/.debug
reboot
```

> Switching to debug mode resets the Wi-Fi settings. The printer has to be reconnected to
> your Wi-Fi once afterwards.

### 3. Upload the component

Copy `filaman.py` into Moonraker's components directory over SSH:

```bash
scp filaman.py lava@<printer-ip>:/home/lava/moonraker/moonraker/components/
```

Then add the `[filaman]` section to `moonraker.conf` (see
[Configuration](#configuration)) and restart Moonraker. Whether the component loaded is
reported in `moonraker.log` and by `GET /server/filaman/status`.

### 4. Optional: FilaMan card in the printer's frontend

The component works fine without any web UI. To also see the FilaMan card in the printer's
fluidd and assign spools there, you additionally need the modified fluidd carrying the
FilaMan module: [ManuelW77/fluidd](https://github.com/ManuelW77/fluidd). The U1's stock
fluidd keeps working without this swap, it just does not show the card.

## Configuration

Add this section to `moonraker.conf`:

```ini
[filaman]
server: http://192.168.1.50:8000
api_key: uak.123.xxxxxxxxxxxxxxxxxxxxx
sync_rate: 5
default_density_g_cm3: 1.24
default_diameter_mm: 1.75
```

### Options

- `server` (required): Base URL of your FilaMan instance
- `api_key` (recommended): FilaMan API key token (`uak.<id>.<secret>`), see
  [Where to get the API key](#where-to-get-the-api-key)
- `sync_rate` (optional): Report interval in seconds, default `5`
- `default_density_g_cm3` (optional): fallback density, default `1.24`
- `default_diameter_mm` (optional): fallback filament diameter, default `1.75`
- `track_filament_sensors` (optional): watch Klipper filament sensors, default `True`
- `clear_spool_on_runout` (optional): release the spool when the sensor reports no
  filament, default `True`
- `runout_debounce` (optional): seconds the sensor must stay empty before the spool is
  released, default `1.0`
- `filament_sensors` (optional): explicit extruder → sensor mapping, see below
- `respond_to_filament_requests` (optional): answer `filament_detect.state` requests from
  the printer, default `True`
- `repush_on_startup` (optional): re-send all assigned spools to the printer once after
  Klipper becomes ready, default `True`
- `repush_delay` (optional): seconds to wait before that re-push, default `3.0`

### Where to get the API key

The `api_key` is issued by **FilaMan**, not by Moonraker or Klipper. Create it in your
FilaMan instance under **Admin → user settings → API Keys**. A valid key looks like
`uak.<id>.<secret>`.

> **Not the device tokens.** The *Device tokens* in the admin panel are a different
> credential and will not authenticate this component — requests keep failing without an
> obvious error. This is the most common setup mistake.

If the key is wrong or missing, `GET /server/filaman/status` reports the problem in
`last_error` and `filaman_connected` stays `false`. The resolved API URL is logged to
`moonraker.log` on startup.

### Keeping credentials in moonraker.secrets

`server` and `api_key` accept Moonraker's secret placeholders:

```ini
# moonraker.conf
[secrets]

[filaman]
server: {secrets.filaman.server}
api_key: {secrets.filaman.api_key}
```

```ini
# moonraker.secrets
[filaman]
server: http://192.168.1.50:8000
api_key: uak.1.xxxxxxxxxxxxxxxxxxxxx
```

Only these two options are expanded, and only when the value actually references a
template — a plain key containing a literal `{` is passed through untouched. A
placeholder pointing at a missing entry aborts startup with a config error naming the
option, instead of silently leaving the printer disconnected.

## Filament removal detection

When a sensor reports `filament_detected: false`, the spool assigned to that extruder is
released after `runout_debounce` seconds: usage tracking stops, the FilaMan card shows no
spool, and the printer channel is cleared via `filament_detect/set`.

Every sensor has to belong to an extruder for this to work. Each sensor is assigned
either automatically or manually.

### Occupancy at startup

A `filament_motion_sensor` reports `filament_detected: false` until its encoder has seen
movement — `RunoutHelper` starts out `False` and the initial pin state is never reported.
Right after a power cycle, `false` therefore means *not measured yet*, not *empty*. The
component treats a sensor's `false` at startup as **unknown** and never releases a spool
on it; only a `true`, or a later change during operation, counts.

Where the printer exposes `print_task_config.filament_exist` (Snapmaker U1), that array is
used as the authoritative occupancy instead. It is valid immediately at startup and is
what the printer's own display shows, so a toolhead emptied while the printer was off is
still detected and released. Without that object, occupancy simply stays unknown until the
sensor reports something.

Unknown never blocks anything: spools are still re-pushed to the printer, and only a
*confirmed* empty channel is skipped. `GET /server/filaman/status` exposes the merged
`filament_present` (`true` / `false` / `null`) plus the raw `sensor_present` and
`printer_present` maps.

### Automatic assignment

On every Klipper restart the component lists all `filament_switch_sensor` and
`filament_motion_sensor` objects — the same sensors fluidd lists under *Runout sensors* —
and derives an extruder from each sensor name. The first matching rule wins,
case-insensitively:

| Rule | Matches | Example |
| --- | --- | --- |
| Leading `e` + number | `^e(\d+)` | `e0_filament` → `extruder` |
| `extruder` + number | `extruder(\d+)` | `extruder1_runout` → `extruder1` |
| Leading `T` + number | `^t(\d+)` | `T2_sensor` → `extruder2` |
| Trailing number | `(\d+)\D*$` | `filament_sensor_1` → `extruder1` |

Index `0` maps to `extruder`, every other index `N` to `extruderN`. A sensor without any
number is only assigned on a printer that has exactly one extruder.

Two safeguards keep a wrong guess from doing damage: a sensor is ignored when the derived
extruder does not exist on the printer, and when an extruder already has a sensor the
later one is ignored with a warning. So an ambiguous name on a multi-extruder printer is
skipped rather than attached to the wrong toolhead.

### Manual assignment

If the naming on your printer does not fit those rules, map the sensors yourself with
`filament_sensors`. It takes one `extruder = sensor` line per toolhead and overrides the
automatic rules:

```ini
[filaman]
filament_sensors:
    extruder = e0_filament
    extruder1 = e1_filament
    extruder2 = e2_filament
    extruder3 = e3_filament
```

The sensor may be written as the bare name (`e0_filament`) or as the full Klipper object
name (`filament_motion_sensor e0_filament`). Sensors you do not list still go through the
automatic rules, so partial mappings are fine. Set `track_filament_sensors: False` to
switch the whole feature off.

### Checking the result

Whichever way the mapping was produced, it is logged to `moonraker.log` on startup:

```
FilaMan tracking filament sensors: e0_filament -> extruder, e1_filament -> extruder1
```

Sensors that could not be assigned are logged too. The same mapping is returned by
`GET /server/filaman/status` as `filament_sensors`, alongside the current per-extruder
state in `filament_present`. Changes are pushed as the
`filaman:filament_presence_changed` notification.

## Keeping the printer in sync

Printers exposing a `filament_detect` object (Snapmaker U1) lose their filament info on
power cycle. Two mechanisms restore it:

- **On request:** the component subscribes to `filament_detect.state`. A value of `1` for
  a channel means the printer is asking for filament info, and the component answers with
  the spool assigned to that extruder (channel 0 → `extruder`, channel 1 → `extruder1`, …).
  Channels without a FilaMan assignment are deliberately left alone, so a parallel RFID
  read is never overwritten with an empty payload.
- **On startup:** `repush_delay` seconds after Klipper becomes ready, every assigned spool
  is sent to the printer once. Extruders whose sensor reports no filament are skipped. The
  delay gives the printer's own RFID scan a head start.

Both can be turned off via `respond_to_filament_requests` and `repush_on_startup`. The
last seen state array is exposed as `filament_detect_state` by
`GET /server/filaman/status`.

## Endpoints

Primary endpoints:

- `GET|POST /server/filaman/spool_id`
- `GET /server/filaman/status`
- `POST /server/filaman/proxy`

Additional Spoolman aliases are always available:

- `GET|POST /server/spoolman/spool_id`
- `GET /server/spoolman/status`
- `POST /server/spoolman/proxy`

## Remote methods

- `filaman_set_active_spool`
- `spoolman_set_active_spool` (always enabled alias)

## GCode macro examples

The remote methods above are callable from Klipper, which lets you assign a spool from
GCode instead of the web UI — from a slicer's start GCode, a printer touchscreen button,
or a `SET_ACTIVE_SPOOL ID=42` console command. Add these to `printer.cfg`:

```ini
[gcode_macro SET_ACTIVE_SPOOL]
gcode:
  {% if params.ID %}
    {% set id = params.ID|int %}
    {action_call_remote_method("filaman_set_active_spool", spool_id=id)}
  {% else %}
    {action_respond_info("Parameter 'ID' is required")}
  {% endif %}

[gcode_macro CLEAR_ACTIVE_SPOOL]
gcode:
  {action_call_remote_method("filaman_set_active_spool", spool_id=None)}
```

Both act on the **currently active extruder** only. On a multi-extruder printer you
therefore have to select the toolhead first (`T1`, then `SET_ACTIVE_SPOOL ID=42`), or
address an extruder directly over HTTP, which needs no tool change:

```
POST /server/filaman/spool_id?extruder=extruder1&spool_id=42
```

The macros are entirely optional — assigning spools from the FilaMan card in fluidd uses
the same endpoints.
