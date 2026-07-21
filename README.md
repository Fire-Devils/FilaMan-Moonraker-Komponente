# FilaMan Moonraker Component

Native Moonraker component for FilaMan.

## Features

- Tracks active spool via Moonraker endpoint and remote method
- Collects extrusion length from Klipper toolhead updates
- Converts length (mm) to weight (g) using spool filament metadata
- Reports usage to native FilaMan endpoint `/api/v1/spools/{id}/consumptions`
- Uses PLA fallback density (`1.24 g/cm3`) when filament density is missing
- Watches Klipper filament sensors and releases the spool when a toolhead runs empty
- Spoolman-compatible aliases for endpoints/events/remote method are always enabled

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
> obvious error. This is the most common setup mistake, see
> [issue #1](https://github.com/ManuelW77/FilaMan-Moonraker-Komponente/issues/1).

If the key is wrong or missing, `GET /server/filaman/status` reports the problem in
`last_error` and `filaman_connected` stays `false`.

## Filament removal detection

The component subscribes to every `filament_switch_sensor` / `filament_motion_sensor`
object Klipper exposes — the same sensors fluidd lists under *Runout sensors*. Sensors
are mapped to extruders by name (`e0_filament` → `extruder`, `e1_filament` →
`extruder1`, …). When a sensor reports `filament_detected: false`, the spool assigned to
that extruder is released after `runout_debounce` seconds: usage tracking stops, the
FilaMan card shows no spool, and the printer channel is cleared via
`filament_detect/set`.

If the naming heuristic does not fit your printer, map the sensors explicitly:

```ini
[filaman]
filament_sensors:
    extruder = e0_filament
    extruder1 = e1_filament
    extruder2 = e2_filament
    extruder3 = e3_filament
```

The detected mapping is logged to `moonraker.log` on startup and returned by
`GET /server/filaman/status` as `filament_sensors`, alongside the current per-extruder
state in `filament_present`. Changes are pushed as the
`filaman:filament_presence_changed` notification.

A toolhead reported as empty while Klipper starts up releases its spool immediately
rather than after `runout_debounce` — a startup snapshot cannot flicker.

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
