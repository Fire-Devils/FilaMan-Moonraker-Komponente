# FilaMan Moonraker Component

Native Moonraker component for FilaMan.

## Features

- Tracks active spool via Moonraker endpoint and remote method
- Collects extrusion length from Klipper toolhead updates
- Converts length (mm) to weight (g) using spool filament metadata
- Reports usage to native FilaMan endpoint `/api/v1/spools/{id}/consumptions`
- Uses PLA fallback density (`1.24 g/cm3`) when filament density is missing
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
- `api_key` (recommended): FilaMan API key token (`uak.<id>.<secret>`)
- `sync_rate` (optional): Report interval in seconds, default `5`
- `default_density_g_cm3` (optional): fallback density, default `1.24`
- `default_diameter_mm` (optional): fallback filament diameter, default `1.75`

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
