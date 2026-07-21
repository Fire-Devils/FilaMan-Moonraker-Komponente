# FilaMan Moonraker-Komponente

*[English version](README.md)*

Native Moonraker-Komponente für FilaMan.

## Funktionen

- Verwaltet die aktive Spule über einen Moonraker-Endpoint und eine Remote-Methode
- Erfasst die extrudierte Länge aus den Toolhead-Updates von Klipper
- Rechnet die Länge (mm) anhand der Filament-Metadaten der Spule in Gewicht (g) um
- Meldet den Verbrauch an den nativen FilaMan-Endpoint `/api/v1/spools/{id}/consumptions`
- Nutzt die PLA-Ersatzdichte (`1,24 g/cm³`), wenn die Dichte des Filaments fehlt
- Überwacht die Filamentsensoren von Klipper und gibt die Spule frei, sobald ein Toolhead
  leer läuft
- Spoolman-kompatible Aliasse für Endpoints, Events und Remote-Methode sind immer aktiv

## Installation auf dem Snapmaker U1

Der U1 lässt in seinem Auslieferungszustand weder SSH zu, noch überleben eigene Dateien
einen Neustart. Für diese Komponente sind deshalb zwei Vorarbeiten nötig: die
Custom-Firmware und der Debug-Modus.

### 1. Custom-Firmware von paxx12 aufspielen

Die Komponente setzt die
[SnapmakerU1 Extended Firmware](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware)
voraus — sie schaltet den SSH-Zugang frei. Die `.bin` auf einen FAT32-formatierten
USB-Stick kopieren und am Drucker über **About → Firmware version → Local Update**
einspielen; die
[Installationsanleitung](https://snapmakeru1-extended-firmware.pages.dev/install.html)
beschreibt das im Detail.

Danach ist der Drucker per SSH erreichbar (Port 22, Benutzer `root` oder `lava`, Passwort
`snapmaker`):

```bash
ssh lava@<drucker-ip>
```

> Das ist ein Root-Zugang mit Standardpasswort. Nur in einem vertrauenswürdigen Netz
> verwenden und das Passwort nach dem ersten Login ändern.

### 2. Debug-Modus aktivieren

Ohne Debug-Modus wird das Dateisystem bei jedem Neustart zurückgesetzt — die kopierte
Komponente wäre nach dem nächsten Einschalten wieder verschwunden. Der Debug-Modus wird
über eine leere Datei `.debug` eingeschaltet:

```bash
touch /oem/.debug
reboot
```

> Der Wechsel in den Debug-Modus setzt die WLAN-Einstellungen zurück. Der Drucker muss
> danach einmalig neu mit dem WLAN verbunden werden.

### 3. Komponente hochladen

`filaman.py` per SSH in das Komponenten-Verzeichnis von Moonraker kopieren:

```bash
scp filaman.py lava@<drucker-ip>:/home/lava/moonraker/moonraker/components/
```

Anschließend den `[filaman]`-Abschnitt in die `moonraker.conf` eintragen (siehe
[Konfiguration](#konfiguration)) und Moonraker neu starten. Ob die Komponente geladen
wurde, steht in der `moonraker.log` und in `GET /server/filaman/status`.

### 4. Optional: FilaMan-Karte im Drucker-Frontend

Die Komponente arbeitet vollständig ohne Weboberfläche. Wer die FilaMan-Karte auch im
fluidd des Druckers sehen und Spulen dort zuweisen möchte, braucht zusätzlich das
modifizierte fluidd mit dem FilaMan-Modul:
[ManuelW77/fluidd](https://github.com/ManuelW77/fluidd). Das Standard-fluidd des U1 bleibt
ohne diesen Austausch funktionsfähig, zeigt die Karte aber nicht an.

## Konfiguration

Diesen Abschnitt in die `moonraker.conf` eintragen:

```ini
[filaman]
server: http://192.168.1.50:8000
api_key: uak.123.xxxxxxxxxxxxxxxxxxxxx
sync_rate: 5
default_density_g_cm3: 1.24
default_diameter_mm: 1.75
```

### Optionen

- `server` (erforderlich): Basis-URL deiner FilaMan-Instanz
- `api_key` (empfohlen): FilaMan-API-Schlüssel (`uak.<id>.<secret>`), siehe
  [Woher der API-Schlüssel kommt](#woher-der-api-schlüssel-kommt)
- `sync_rate` (optional): Melde-Intervall in Sekunden, Standard `5`
- `default_density_g_cm3` (optional): Ersatzdichte, Standard `1.24`
- `default_diameter_mm` (optional): Ersatz-Filamentdurchmesser, Standard `1.75`
- `track_filament_sensors` (optional): Filamentsensoren von Klipper überwachen, Standard
  `True`
- `clear_spool_on_runout` (optional): Spule freigeben, wenn der Sensor kein Filament
  meldet, Standard `True`
- `runout_debounce` (optional): Sekunden, die der Sensor leer bleiben muss, bevor die
  Spule freigegeben wird, Standard `1.0`
- `filament_sensors` (optional): explizite Zuordnung Extruder → Sensor, siehe unten
- `respond_to_filament_requests` (optional): Anfragen über `filament_detect.state`
  beantworten, Standard `True`
- `repush_on_startup` (optional): alle zugewiesenen Spulen einmalig erneut an den Drucker
  senden, sobald Klipper bereit ist, Standard `True`
- `repush_delay` (optional): Wartezeit in Sekunden vor diesem erneuten Senden, Standard
  `3.0`

### Woher der API-Schlüssel kommt

Der `api_key` wird von **FilaMan** ausgestellt, nicht von Moonraker oder Klipper. Du
erzeugst ihn in deiner FilaMan-Instanz unter **Admin → Benutzereinstellungen → API Keys**.
Ein gültiger Schlüssel sieht aus wie `uak.<id>.<secret>`.

> **Nicht die Device Tokens.** Die *Device Tokens* im Admin-Bereich sind ein anderes
> Zugangsmerkmal und authentifizieren diese Komponente nicht — die Anfragen scheitern
> dann ohne erkennbare Fehlermeldung. Das ist der häufigste Einrichtungsfehler.

Ist der Schlüssel falsch oder fehlt er, nennt `GET /server/filaman/status` das Problem im
Feld `last_error`, und `filaman_connected` bleibt `false`. Die aufgelöste API-URL wird
beim Start in die `moonraker.log` geschrieben.

### Zugangsdaten in moonraker.secrets auslagern

`server` und `api_key` akzeptieren die Platzhalter von Moonraker:

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

Nur diese beiden Optionen werden aufgelöst, und auch nur dann, wenn der Wert tatsächlich
auf ein Template verweist — ein Klartext-Schlüssel mit einer geschweiften Klammer darin
bleibt unverändert. Zeigt ein Platzhalter ins Leere, bricht der Start mit einem
Konfigurationsfehler ab, der die Option benennt, statt den Drucker still auf
„disconnected" laufen zu lassen.

## Erkennung entnommenen Filaments

Meldet ein Sensor `filament_detected: false`, wird die dem Extruder zugewiesene Spule nach
`runout_debounce` Sekunden freigegeben: Die Verbrauchsbuchung stoppt, die FilaMan-Karte
zeigt keine Spule mehr, und der Druckerkanal wird über `filament_detect/set` geleert.

Damit das funktioniert, muss jeder Sensor einem Extruder zugeordnet sein. Diese Zuordnung
geschieht entweder automatisch oder manuell.

### Belegung beim Start

Ein `filament_motion_sensor` meldet `filament_detected: false`, solange sein Encoder keine
Bewegung gesehen hat — `RunoutHelper` startet mit `False`, und der initiale Pin-Zustand
wird nie gemeldet. Direkt nach dem Einschalten bedeutet `false` also *noch nicht gemessen*
und nicht *leer*. Die Komponente wertet ein `false` vom Sensor beim Start deshalb als
**unbekannt** und gibt darauf niemals eine Spule frei; es zählt nur ein `true` oder ein
späterer Wechsel im laufenden Betrieb.

Stellt der Drucker `print_task_config.filament_exist` bereit (Snapmaker U1), wird dieses
Array stattdessen als maßgebliche Belegung genutzt. Es ist schon beim Start gültig und ist
die Quelle, die auch das Drucker-Display anzeigt. Ein Toolhead, der im ausgeschalteten
Zustand geleert wurde, wird damit weiterhin erkannt und freigegeben. Ohne dieses Objekt
bleibt die Belegung schlicht unbekannt, bis der Sensor etwas meldet.

„Unbekannt" blockiert nichts: Spulen werden weiterhin an den Drucker nachgeschoben, und
nur ein *bestätigt* leerer Kanal wird übersprungen. `GET /server/filaman/status` liefert
die zusammengeführte Belegung `filament_present` (`true` / `false` / `null`) sowie die
Rohwerte in `sensor_present` und `printer_present`.

### Automatische Zuordnung

Bei jedem Klipper-Neustart listet die Komponente alle Objekte vom Typ
`filament_switch_sensor` und `filament_motion_sensor` auf — dieselben Sensoren, die fluidd
unter *Auslaufsensoren* anzeigt — und leitet aus jedem Sensornamen einen Extruder ab. Die
erste passende Regel gewinnt, Groß- und Kleinschreibung spielt keine Rolle:

| Regel | Muster | Beispiel |
| --- | --- | --- |
| `e` am Anfang + Zahl | `^e(\d+)` | `e0_filament` → `extruder` |
| `extruder` + Zahl | `extruder(\d+)` | `extruder1_runout` → `extruder1` |
| `T` am Anfang + Zahl | `^t(\d+)` | `T2_sensor` → `extruder2` |
| Zahl am Ende | `(\d+)\D*$` | `filament_sensor_1` → `extruder1` |

Index `0` steht für `extruder`, jeder weitere Index `N` für `extruderN`. Ein Sensor ganz
ohne Zahl wird nur auf einem Drucker mit genau einem Extruder zugeordnet.

Zwei Sicherungen verhindern, dass eine falsche Vermutung Schaden anrichtet: Ein Sensor
wird ignoriert, wenn der abgeleitete Extruder auf dem Drucker gar nicht existiert, und
wenn ein Extruder bereits einen Sensor hat, wird der spätere mit einer Warnung ignoriert.
Ein mehrdeutiger Name auf einem Mehrfach-Extruder-Drucker wird also übersprungen, statt am
falschen Toolhead zu landen.

### Manuelle Zuordnung

Passt die Benennung auf deinem Drucker nicht zu diesen Regeln, ordnest du die Sensoren mit
`filament_sensors` selbst zu. Die Option nimmt pro Toolhead eine Zeile `extruder = sensor`
und hat Vorrang vor den automatischen Regeln:

```ini
[filaman]
filament_sensors:
    extruder = e0_filament
    extruder1 = e1_filament
    extruder2 = e2_filament
    extruder3 = e3_filament
```

Der Sensor darf als reiner Name (`e0_filament`) oder als vollständiger Klipper-Objektname
(`filament_motion_sensor e0_filament`) geschrieben werden. Nicht aufgeführte Sensoren
durchlaufen weiterhin die automatischen Regeln, eine unvollständige Zuordnung ist also
zulässig. Mit `track_filament_sensors: False` schaltest du die gesamte Funktion ab.

### Ergebnis prüfen

Unabhängig davon, wie die Zuordnung zustande kam, wird sie beim Start in die
`moonraker.log` geschrieben:

```
FilaMan tracking filament sensors: e0_filament -> extruder, e1_filament -> extruder1
```

Sensoren, die sich nicht zuordnen ließen, werden ebenfalls protokolliert. Dieselbe
Zuordnung liefert `GET /server/filaman/status` im Feld `filament_sensors`, zusammen mit
dem aktuellen Zustand je Extruder in `filament_present`. Änderungen werden als
Notification `filaman:filament_presence_changed` verschickt.

## Drucker synchron halten

Drucker mit einem `filament_detect`-Objekt (Snapmaker U1) verlieren ihre Filament-Infos
beim Aus- und Einschalten. Zwei Mechanismen stellen sie wieder her:

- **Auf Anfrage:** Die Komponente abonniert `filament_detect.state`. Der Wert `1` für
  einen Kanal bedeutet, dass der Drucker um Filament-Infos bittet; die Komponente
  antwortet mit der Spule, die dem Extruder zugewiesen ist (Kanal 0 → `extruder`,
  Kanal 1 → `extruder1`, …). Kanäle ohne FilaMan-Zuweisung werden bewusst in Ruhe
  gelassen, damit ein parallel laufender RFID-Lesevorgang nicht mit einem leeren Payload
  überschrieben wird.
- **Beim Start:** `repush_delay` Sekunden, nachdem Klipper bereit ist, wird jede
  zugewiesene Spule einmalig an den Drucker geschickt. Extruder, deren Sensor kein
  Filament meldet, werden übersprungen. Die Verzögerung gibt dem druckereigenen
  RFID-Scan einen Vorsprung.

Beides lässt sich über `respond_to_filament_requests` und `repush_on_startup` abschalten.
Das zuletzt gesehene State-Array liefert `GET /server/filaman/status` im Feld
`filament_detect_state`.

## Endpoints

Primäre Endpoints:

- `GET|POST /server/filaman/spool_id`
- `GET /server/filaman/status`
- `POST /server/filaman/proxy`

Zusätzlich stehen die Spoolman-Aliasse immer zur Verfügung:

- `GET|POST /server/spoolman/spool_id`
- `GET /server/spoolman/status`
- `POST /server/spoolman/proxy`

## Remote-Methoden

- `filaman_set_active_spool`
- `spoolman_set_active_spool` (immer aktiver Alias)

## Beispiele für GCode-Makros

Die obigen Remote-Methoden sind aus Klipper heraus aufrufbar. Damit lässt sich eine Spule
per GCode statt über die Weboberfläche zuweisen — aus dem Start-GCode eines Slicers, über
eine Schaltfläche am Drucker-Display oder als Konsolenbefehl `SET_ACTIVE_SPOOL ID=42`.
Trag die Makros in deine `printer.cfg` ein:

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

Beide wirken ausschließlich auf den **aktuell aktiven Extruder**. Auf einem Drucker mit
mehreren Extrudern musst du den Toolhead deshalb zuerst auswählen (`T1`, danach
`SET_ACTIVE_SPOOL ID=42`) — oder du sprichst einen Extruder direkt über HTTP an, wofür
kein Werkzeugwechsel nötig ist:

```
POST /server/filaman/spool_id?extruder=extruder1&spool_id=42
```

Die Makros sind vollständig optional — das Zuweisen über die FilaMan-Karte in fluidd nutzt
dieselben Endpoints.
