# Native FilaMan integration for Moonraker
#
# Inspired by Moonraker's Spoolman component.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from ..common import HistoryFieldData, RequestType
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union, cast

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import WebRequest
    from .announcements import Announcements
    from .database import MoonrakerDatabase
    from .history import History
    from .http_client import HttpClient, HttpResponse
    from .klippy_apis import KlippyAPI as APIComp

DB_NAMESPACE = "moonraker"
ACTIVE_SPOOL_KEY = "filaman.spool_id"
LEGACY_ACTIVE_SPOOL_KEY = "spoolman.spool_id"
EXTRUDER_SPOOLS_KEY = "filaman.extruder_spools"

DEFAULT_PLA_DENSITY_G_CM3 = 1.24
DEFAULT_FILAMENT_DIAMETER_MM = 1.75

# filament_detect.state[channel]: the printer is asking for filament info
FILAMENT_DT_STATE_DETECTING = 1


class FilaManManager:
    def __init__(self, config: ConfigHelper):
        self.server = config.get_server()
        self.eventloop = self.server.get_event_loop()

        self._get_filaman_urls(config)
        self.api_key: Optional[str] = config.get("api_key", default=None)
        self.sync_rate_seconds = config.getint("sync_rate", default=5, minval=1)
        self.reconnect_delay: float = 2.0
        self.connected_check_delay: float = 30.0

        self.track_filament_sensors: bool = config.getboolean(
            "track_filament_sensors", default=True
        )
        self.clear_spool_on_runout: bool = config.getboolean(
            "clear_spool_on_runout", default=True
        )
        self.runout_debounce: float = config.getfloat(
            "runout_debounce", default=1.0, minval=0.0
        )
        self.sensor_map_override: Dict[str, str] = config.getdict(
            "filament_sensors", default={}
        )
        self.respond_to_filament_requests: bool = config.getboolean(
            "respond_to_filament_requests", default=True
        )
        self.repush_on_startup: bool = config.getboolean(
            "repush_on_startup", default=True
        )
        self.repush_delay: float = config.getfloat(
            "repush_delay", default=3.0, minval=0.0
        )

        self.default_density_g_cm3 = self._get_float_option(
            config,
            "default_density_g_cm3",
            default=DEFAULT_PLA_DENSITY_G_CM3,
            minimum=0.0,
        )
        self.default_diameter_mm = self._get_float_option(
            config,
            "default_diameter_mm",
            default=DEFAULT_FILAMENT_DIAMETER_MM,
            minimum=0.0,
        )

        self.report_timer = self.eventloop.register_timer(self.report_extrusion)
        self.pending_reports: Dict[int, float] = {}

        self.connection_task: Optional[asyncio.Task] = None
        self.spool_check_task: Optional[asyncio.Task] = None
        self.is_closing: bool = False

        self.api_connected: bool = False
        self.spool_id: Optional[int] = None
        self.extruder_spools: Dict[str, Optional[int]] = {}
        self._highest_epos: float = 0.0
        self._last_epos: float = 0.0
        self._current_extruder: str = "extruder"

        # Klipper object name ("filament_switch_sensor e0_filament") -> extruder name
        self.filament_sensors: Dict[str, str] = {}
        self.filament_present: Dict[str, bool] = {}
        self._runout_timers: Dict[str, asyncio.TimerHandle] = {}

        self._filament_detect_state: Dict[int, int] = {}
        self._has_filament_detect: bool = False
        self._repush_timer: Optional[asyncio.TimerHandle] = None

        self._error_logged: bool = False
        self._last_error: Optional[str] = None
        self._last_success_at: Optional[str] = None

        self.spool_history = HistoryFieldData(
            "spool_ids",
            "filaman",
            "Spool IDs used",
            "collect",
            reset_callback=self._on_history_reset,
        )
        history: History = self.server.lookup_component("history")
        history.register_auxiliary_field(self.spool_history)

        self.klippy_apis: APIComp = self.server.lookup_component("klippy_apis")
        self.http_client: HttpClient = self.server.lookup_component("http_client")
        self.database: MoonrakerDatabase = self.server.lookup_component("database")

        announcements: Announcements = self.server.lookup_component("announcements")
        with contextlib.suppress(Exception):
            announcements.register_feed("filaman")
        with contextlib.suppress(Exception):
            announcements.register_feed("spoolman")

        if not self.api_key:
            logging.warning(
                "FilaMan component configured without api_key. "
                "If your API requires authentication, requests will fail."
            )

        self._register_notifications()
        self._register_listeners()
        self._register_endpoints()
        self._register_remote_methods()

    def _get_float_option(
        self,
        config: ConfigHelper,
        option: str,
        default: float,
        minimum: float,
    ) -> float:
        raw_val = config.get(option, default=None)
        if raw_val is None:
            return default
        try:
            value = float(raw_val)
        except Exception:
            raise config.error(
                f"Section [filaman], Option {option}: '{raw_val}' is not a valid number"
            )
        if value <= minimum:
            raise config.error(
                f"Section [filaman], Option {option}: value must be > {minimum}"
            )
        return value

    def _get_filaman_urls(self, config: ConfigHelper) -> None:
        orig_url = config.get("server")
        if not re.match(r"(?i)^https?://", orig_url):
            orig_url = f"http://{orig_url}"
        parsed = urlparse(orig_url)
        if not parsed.scheme or not parsed.netloc:
            raise config.error(
                f"Section [filaman], Option server: {orig_url}: Invalid URL format"
            )

        base = f"{parsed.scheme}://{parsed.netloc}"
        server_path = parsed.path.rstrip("/")

        if server_path.endswith("/api/v1"):
            api_path = server_path
        elif server_path.endswith("/api"):
            api_path = f"{server_path}/v1"
        elif server_path:
            api_path = f"{server_path}/api/v1"
        else:
            api_path = "/api/v1"

        self.server_url = f"{base}{server_path}"
        self.api_url = f"{base}{api_path}"

    def _register_notifications(self) -> None:
        self._register_notification_safe("filaman:active_spool_set")
        self._register_notification_safe("filaman:extruder_spools_changed")
        self._register_notification_safe("filaman:filaman_status_changed")
        self._register_notification_safe("filaman:filament_presence_changed")
        self._register_notification_safe("spoolman:active_spool_set")
        self._register_notification_safe("spoolman:spoolman_status_changed")

    def _register_notification_safe(self, event_name: str) -> None:
        with contextlib.suppress(Exception):
            self.server.register_notification(event_name)

    def _register_listeners(self) -> None:
        self.server.register_event_handler("server:klippy_ready", self._handle_klippy_ready)

    def _register_endpoints(self) -> None:
        endpoint_prefixes = ["/server/filaman", "/server/spoolman"]

        for prefix in endpoint_prefixes:
            self.server.register_endpoint(
                f"{prefix}/spool_id",
                RequestType.GET | RequestType.POST,
                self._handle_spool_id_request,
            )
            self.server.register_endpoint(
                f"{prefix}/spool_ids",
                RequestType.GET,
                self._handle_spool_ids_request,
            )
            self.server.register_endpoint(
                f"{prefix}/proxy",
                RequestType.POST,
                self._proxy_filaman_request,
            )
            self.server.register_endpoint(
                f"{prefix}/status",
                RequestType.GET,
                self._handle_status_request,
            )

    def _register_remote_methods(self) -> None:
        self.server.register_remote_method(
            "filaman_set_active_spool", self.set_active_spool
        )
        with contextlib.suppress(Exception):
            self.server.register_remote_method(
                "spoolman_set_active_spool", self.set_active_spool
            )

    def _on_history_reset(self) -> List[int]:
        if self.spool_id is None:
            return []
        return [self.spool_id]

    async def component_init(self) -> None:
        stored_extruder_spools = await self.database.get_item(
            DB_NAMESPACE, EXTRUDER_SPOOLS_KEY, None
        )
        if isinstance(stored_extruder_spools, dict):
            self.extruder_spools = stored_extruder_spools

        self.spool_id = await self.database.get_item(DB_NAMESPACE, ACTIVE_SPOOL_KEY, None)
        if self.spool_id is None:
            self.spool_id = await self.database.get_item(
                DB_NAMESPACE,
                LEGACY_ACTIVE_SPOOL_KEY,
                None,
            )
            if self.spool_id is not None:
                self.database.insert_item(DB_NAMESPACE, ACTIVE_SPOOL_KEY, self.spool_id)

        self.report_timer.start()
        self.connection_task = self.eventloop.create_task(self._availability_loop())

        if self.spool_id is not None:
            self._cancel_spool_check_task()
            self.spool_check_task = self.eventloop.create_task(self._check_spool_deleted())

    async def _availability_loop(self) -> None:
        while not self.is_closing:
            await self._check_api_available()
            if not self.is_closing:
                delay = self.connected_check_delay if self.api_connected else self.reconnect_delay
                await asyncio.sleep(delay)

    async def _check_api_available(self) -> None:
        response = await self._request(
            method="GET",
            url=f"{self.api_url}/spools?page=1&page_size=1",
            connect_timeout=2.0,
            request_timeout=4.0,
        )

        if response.has_error():
            msg = self._get_response_error(response)
            self._set_last_error(f"FilaMan availability check failed: {msg}")
            self._set_connected(False)
            return

        self._mark_success()

    def _set_connected(self, value: bool) -> None:
        if self.api_connected == value:
            return
        self.api_connected = value
        self._send_status_notification()

    def connected(self) -> bool:
        return self.api_connected

    async def _handle_klippy_ready(self) -> None:
        # Klippy may reconnect at any time, so rebuild the printer state from scratch.
        self._cancel_runout_timers()
        self._cancel_repush_timer()
        self.filament_sensors = {}
        self.filament_present = {}
        self._filament_detect_state = {}

        objects_list: List[str] = await self.klippy_apis.get_object_list(default=[])
        self._has_filament_detect = "filament_detect" in objects_list
        if self.track_filament_sensors:
            self._discover_filament_sensors(objects_list)

        objects: Dict[str, Optional[List[str]]] = {
            "toolhead": ["position", "extruder"]
        }
        for obj_name in self.filament_sensors:
            objects[obj_name] = ["filament_detected"]
        if self._has_filament_detect and self.respond_to_filament_requests:
            objects["filament_detect"] = ["state"]

        result: Dict[str, Dict[str, Any]]
        result = await self.klippy_apis.subscribe_objects(
            objects, self._handle_status_update, {}
        )
        toolhead = result.get("toolhead", {})
        self._current_extruder = toolhead.get("extruder", "extruder")
        initial_e_pos = toolhead.get("position", [None] * 4)[3]
        logging.debug(f"Initial epos: {initial_e_pos}")
        if initial_e_pos is not None:
            self._highest_epos = initial_e_pos
            self._last_epos = initial_e_pos
        else:
            logging.error("FilaMan integration unable to subscribe to epos")
            raise self.server.error("Unable to subscribe to e position")

        if self.extruder_spools:
            # Use per-extruder assignment for current extruder
            self.spool_id = self.extruder_spools.get(self._current_extruder)
            logging.info(
                f"Restored spool {self.spool_id} for active extruder {self._current_extruder}"
            )
        elif self.spool_id is not None:
            # Migrate legacy single spool_id into extruder_spools
            self.extruder_spools[self._current_extruder] = self.spool_id
            self.database.insert_item(DB_NAMESPACE, EXTRUDER_SPOOLS_KEY, self.extruder_spools)
            logging.info(
                f"Migrated legacy spool_id {self.spool_id} to extruder {self._current_extruder}"
            )

        # Evaluated last: releasing a spool needs both the restored assignments
        # and _last_epos, and the re-push must see the cleaned up state.
        self._apply_sensor_states(result, initial=True)
        self._apply_filament_detect_state(result)
        if self.repush_on_startup:
            self._repush_timer = self.eventloop.delay_callback(
                self.repush_delay, self._repush_assigned_spools
            )

    def _handle_status_update(self, status: Dict[str, Any], _: float) -> None:
        toolhead: Optional[Dict[str, Any]] = status.get("toolhead")
        if toolhead is not None:
            epos: float = toolhead.get("position", [0, 0, 0, self._highest_epos])[3]
            self._last_epos = epos
            extr = toolhead.get("extruder", self._current_extruder)

            if extr != self._current_extruder:
                self._highest_epos = epos
                self._current_extruder = extr
                # Switch active spool to the one assigned to the new extruder
                new_spool_id = self.extruder_spools.get(extr)
                if new_spool_id != self.spool_id:
                    self.spool_id = new_spool_id
                    self.database.insert_item(DB_NAMESPACE, ACTIVE_SPOOL_KEY, new_spool_id)
                    self.database.insert_item(
                        DB_NAMESPACE, LEGACY_ACTIVE_SPOOL_KEY, new_spool_id
                    )
                    payload = {"spool_id": new_spool_id}
                    self.server.send_event("filaman:active_spool_set", payload)
                    self.server.send_event("spoolman:active_spool_set", payload)
                    logging.info(f"Toolchange to {extr}, switched to spool {new_spool_id}")
            elif epos > self._highest_epos:
                if self.spool_id is not None:
                    self._add_extrusion(self.spool_id, epos - self._highest_epos)
                self._highest_epos = epos

        self._apply_sensor_states(status)
        self._apply_filament_detect_state(status)

    # ------------------------------------------------------------------ #
    # Filament runout sensors: detect a toolhead running empty            #
    # ------------------------------------------------------------------ #

    def _discover_filament_sensors(self, objects: List[str]) -> None:
        known_extruders = {
            name for name in objects
            if name == "extruder" or re.match(r"^extruder\d+$", name)
        }

        for obj_name in objects:
            if not obj_name.startswith(
                ("filament_switch_sensor ", "filament_motion_sensor ")
            ):
                continue
            sensor_name = obj_name.split(" ", 1)[1]
            extruder = self._sensor_to_extruder(sensor_name, known_extruders)
            if extruder is None:
                logging.info(
                    f"FilaMan: no extruder mapping for sensor '{sensor_name}', ignoring"
                )
                continue
            if extruder in self.filament_sensors.values():
                logging.warning(
                    f"FilaMan: {extruder} already has a filament sensor, "
                    f"ignoring '{sensor_name}'"
                )
                continue
            self.filament_sensors[obj_name] = extruder

        if self.filament_sensors:
            mapping = ", ".join(
                f"{obj.split(' ', 1)[1]} -> {extr}"
                for obj, extr in self.filament_sensors.items()
            )
            logging.info(f"FilaMan tracking filament sensors: {mapping}")
        else:
            logging.info("FilaMan: no filament sensors found")

    def _sensor_to_extruder(
        self,
        sensor_name: str,
        known_extruders: set,
    ) -> Optional[str]:
        for extruder, mapped in self.sensor_map_override.items():
            # Accept both the bare sensor name and the full Klipper object name.
            if mapped.split(" ", 1)[-1] == sensor_name:
                return extruder if extruder in known_extruders else None

        name = sensor_name.lower()
        index: Optional[int] = None
        for pattern in (r"^e(\d+)", r"extruder(\d+)", r"^t(\d+)", r"(\d+)\D*$"):
            match = re.search(pattern, name)
            if match is not None:
                index = int(match.group(1))
                break
        if index is None:
            # An unnumbered sensor can only be attributed on a single-extruder printer.
            index = 0 if len(known_extruders) == 1 else None
        if index is None:
            return None

        extruder = "extruder" if index == 0 else f"extruder{index}"
        return extruder if extruder in known_extruders else None

    def _apply_sensor_states(
        self,
        status: Dict[str, Any],
        initial: bool = False,
    ) -> None:
        for obj_name, extruder in self.filament_sensors.items():
            sensor = status.get(obj_name)
            if not isinstance(sensor, dict) or "filament_detected" not in sensor:
                continue
            self._note_filament_presence(
                extruder, bool(sensor["filament_detected"]), initial
            )

    def _note_filament_presence(
        self,
        extruder: str,
        present: bool,
        initial: bool = False,
    ) -> None:
        if self.filament_present.get(extruder) == present:
            return

        self.filament_present[extruder] = present
        self.server.send_event(
            "filaman:filament_presence_changed",
            {
                "extruder": extruder,
                "filament_present": present,
                "filament_present_map": self.filament_present,
            },
        )

        timer = self._runout_timers.pop(extruder, None)
        if timer is not None:
            timer.cancel()

        if present or not self.clear_spool_on_runout:
            return
        if self.extruder_spools.get(extruder) is None:
            return

        if initial:
            # A startup snapshot cannot flicker, so act on it right away.
            self._handle_runout_confirmed(extruder)
            return

        logging.info(
            f"Filament removed from {extruder}, releasing spool in "
            f"{self.runout_debounce}s unless it returns"
        )
        self._runout_timers[extruder] = self.eventloop.delay_callback(
            self.runout_debounce, self._handle_runout_confirmed, extruder
        )

    def _handle_runout_confirmed(self, extruder: str) -> None:
        self._runout_timers.pop(extruder, None)
        if self.filament_present.get(extruder) is not False:
            return
        spool_id = self.extruder_spools.get(extruder)
        if spool_id is None:
            return
        logging.info(f"Releasing spool {spool_id} from empty {extruder}")
        self.set_extruder_spool(extruder, None)

    def _cancel_runout_timers(self) -> None:
        for timer in self._runout_timers.values():
            timer.cancel()
        self._runout_timers = {}

    # ------------------------------------------------------------------ #
    # Printer requests: filament_detect.state -> re-push assigned spools  #
    # ------------------------------------------------------------------ #

    def _apply_filament_detect_state(self, status: Dict[str, Any]) -> None:
        detect = status.get("filament_detect")
        if not isinstance(detect, dict):
            return
        state = detect.get("state")
        if not isinstance(state, list):
            return

        for channel, value in enumerate(state):
            previous = self._filament_detect_state.get(channel)
            self._filament_detect_state[channel] = value
            if value != FILAMENT_DT_STATE_DETECTING or previous == value:
                continue
            self._handle_filament_request(channel)

    def _handle_filament_request(self, channel: int) -> None:
        extruder = "extruder" if channel == 0 else f"extruder{channel}"
        spool_id = self.extruder_spools.get(extruder)
        if spool_id is None:
            # Leave the channel to the RFID reader rather than acknowledging it
            # with an empty payload, which would wipe whatever it just read.
            return
        if self.filament_present.get(extruder) is False:
            return

        logging.info(
            f"Printer requested filament info for channel {channel}, "
            f"answering with spool {spool_id} of {extruder}"
        )
        self.eventloop.create_task(self._push_spool_to_printer(extruder, spool_id))

    def _repush_assigned_spools(self) -> None:
        self._repush_timer = None
        for extruder, spool_id in self.extruder_spools.items():
            if spool_id is None or self.filament_present.get(extruder) is False:
                continue
            logging.info(f"Re-pushing spool {spool_id} of {extruder} to the printer")
            self.eventloop.create_task(self._push_spool_to_printer(extruder, spool_id))

    def _cancel_repush_timer(self) -> None:
        if self._repush_timer is not None:
            self._repush_timer.cancel()
            self._repush_timer = None

    def _add_extrusion(self, spool_id: int, used_length_mm: float) -> None:
        if spool_id in self.pending_reports:
            self.pending_reports[spool_id] += used_length_mm
        else:
            self.pending_reports[spool_id] = used_length_mm

    def _set_last_error(self, message: str) -> None:
        self._last_error = message
        if not self._error_logged:
            self._error_logged = True
            logging.info(message)

    def _mark_success(self) -> None:
        self._error_logged = False
        self._last_error = None
        self._last_success_at = datetime.now(timezone.utc).isoformat()
        self._set_connected(True)

    def _get_response_error(self, response: HttpResponse) -> str:
        err_msg = f"HTTP error: {response.status_code} {response.error}"
        with contextlib.suppress(Exception):
            payload = cast(Dict[str, Any], response.json())
            detail = payload.get("detail")
            if isinstance(detail, dict):
                detail_msg = detail.get("message") or detail.get("code")
                if detail_msg:
                    err_msg += f", FilaMan message: {detail_msg}"
                    return err_msg
            if "message" in payload and isinstance(payload["message"], str):
                err_msg += f", FilaMan message: {payload['message']}"
        return err_msg

    async def _request(
        self,
        method: str,
        url: str,
        body: Optional[Union[bytes, str, List[Any], Dict[str, Any]]] = None,
        connect_timeout: float = 5.0,
        request_timeout: float = 10.0,
    ) -> HttpResponse:
        headers: Dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"ApiKey {self.api_key}"
        return await self.http_client.request(
            method=method,
            url=url,
            body=body,
            headers=headers,
            connect_timeout=connect_timeout,
            request_timeout=request_timeout,
        )

    def set_extruder_spool(self, extruder: str, spool_id: Optional[int]) -> None:
        """Assign a spool to a specific extruder."""
        if not isinstance(spool_id, int) and spool_id is not None:
            raise self.server.error("spool_id must be an integer or None")

        old_id = self.extruder_spools.get(extruder)
        if old_id == spool_id and (extruder != self._current_extruder or self.spool_id == spool_id):
            logging.info(f"Spool for {extruder} already set to {spool_id}")
            return

        self.extruder_spools[extruder] = spool_id
        self.database.insert_item(DB_NAMESPACE, EXTRUDER_SPOOLS_KEY, self.extruder_spools)
        self.server.send_event(
            "filaman:extruder_spools_changed",
            {"extruder_spools": self.extruder_spools}
        )

        if extruder == self._current_extruder:
            self.spool_history.tracker.update(spool_id)
            self.spool_id = spool_id
            self._highest_epos = self._last_epos
            self.database.insert_item(DB_NAMESPACE, ACTIVE_SPOOL_KEY, spool_id)
            self.database.insert_item(DB_NAMESPACE, LEGACY_ACTIVE_SPOOL_KEY, spool_id)
            payload = {"spool_id": spool_id}
            self.server.send_event("filaman:active_spool_set", payload)
            self.server.send_event("spoolman:active_spool_set", payload)

            if spool_id is not None:
                self._cancel_spool_check_task()
                self.spool_check_task = self.eventloop.create_task(self._check_spool_deleted())

        logging.info(f"Set spool for {extruder} to: {spool_id}")
        self.eventloop.create_task(self._push_spool_to_printer(extruder, spool_id))

    # ------------------------------------------------------------------ #
    # Printer push: spool color/material → filament_detect/set            #
    # ------------------------------------------------------------------ #

    def _extruder_to_channel(self, extruder: str) -> int:
        if extruder == "extruder":
            return 0
        m = re.match(r"^extruder(\d+)$", extruder)
        return int(m.group(1)) if m else 0

    def _hex_to_rgb_int(self, hex_color: Optional[str]) -> Optional[int]:
        if not hex_color:
            return None
        h = hex_color.lstrip("#")
        if len(h) == 6:
            with contextlib.suppress(ValueError):
                return int(h, 16)
        return None

    async def _push_spool_to_printer(self, extruder: str, spool_id: Optional[int]) -> None:
        """Forward spool color/material to the printer via filament_detect/set."""
        channel = self._extruder_to_channel(extruder)
        port = self.server.get_host_info()["port"]
        url = f"http://localhost:{port}/printer/filament_detect/set"

        if spool_id is None:
            body: Dict[str, Any] = {"channel": channel, "info": {}}
            response = await self.http_client.request(
                method="POST", url=url, body=body,
                connect_timeout=2.0, request_timeout=5.0,
            )
            if response.has_error():
                logging.debug(
                    f"filament_detect clear ch{channel} failed: {response.error}"
                )
            return

        spool_data, response = await self._fetch_spool(spool_id)
        if spool_data is None:
            return

        filament: Dict[str, Any] = spool_data.get("filament") or {}
        info: Dict[str, Any] = {}

        # Vendor
        manufacturer: Dict[str, Any] = filament.get("manufacturer") or {}
        vendor = manufacturer.get("name")
        if vendor:
            info["VENDOR"] = str(vendor)

        # Material type
        material_type = filament.get("material_type")
        if material_type:
            info["MAIN_TYPE"] = str(material_type)

        # Colors (primary + up to 4 additional)
        colors: List[Any] = filament.get("colors") or []
        for i, color_entry in enumerate(colors[:5]):
            hex_code = (color_entry.get("color") or {}).get("hex_code")
            rgb_int = self._hex_to_rgb_int(hex_code)
            if rgb_int is not None:
                key = "RGB_1" if i == 0 else f"RGB_{i + 1}"
                info[key] = rgb_int
        if "RGB_1" in info:
            info["ALPHA"] = 255

        if not info:
            logging.debug(f"No filament metadata for spool {spool_id}, skipping printer push")
            return

        body = {"channel": channel, "info": info}
        response = await self.http_client.request(
            method="POST", url=url, body=body,
            connect_timeout=2.0, request_timeout=5.0,
        )
        if response.has_error():
            logging.debug(
                f"filament_detect set ch{channel} spool {spool_id} failed: {response.error}"
            )
        else:
            logging.info(
                f"Pushed spool {spool_id} → printer channel {channel}: "
                f"type={info.get('MAIN_TYPE')}, vendor={info.get('VENDOR')}, "
                f"color=#{info.get('RGB_1', 0):06X}"
            )

    def set_active_spool(self, spool_id: Union[int, None]) -> None:
        """Set spool for the currently active extruder (backward-compatible)."""
        self.set_extruder_spool(self._current_extruder, spool_id)

    async def _check_spool_deleted(self) -> None:
        try:
            if self.spool_id is not None:
                response = await self._request(
                    method="GET",
                    url=f"{self.api_url}/spools/{self.spool_id}",
                    connect_timeout=2.0,
                    request_timeout=4.0,
                )
                if response.status_code == 404:
                    logging.info(f"Spool ID {self.spool_id} not found, setting to None")
                    self.pending_reports.pop(self.spool_id, None)
                    self.set_active_spool(None)
                elif response.has_error():
                    err_msg = self._get_response_error(response)
                    self._set_last_error(f"Attempt to check spool status failed: {err_msg}")
                else:
                    self._mark_success()
        finally:
            current_task = asyncio.current_task()
            if self.spool_check_task is current_task:
                self.spool_check_task = None

    def _cancel_spool_check_task(self) -> None:
        if self.spool_check_task is None or self.spool_check_task.done():
            return
        self.spool_check_task.cancel()

    async def _fetch_spool(self, spool_id: int) -> Tuple[Optional[Dict[str, Any]], HttpResponse]:
        response = await self._request(
            method="GET",
            url=f"{self.api_url}/spools/{spool_id}",
            connect_timeout=2.0,
            request_timeout=5.0,
        )
        if response.has_error():
            return None, response
        with contextlib.suppress(Exception):
            payload = cast(Dict[str, Any], response.json())
            return payload, response
        return None, response

    def _resolve_material_values(self, spool_data: Dict[str, Any]) -> Tuple[float, float]:
        filament = spool_data.get("filament")
        if not isinstance(filament, dict):
            filament = {}

        density_raw = filament.get("density_g_cm3")
        diameter_raw = filament.get("diameter_mm")

        density = self.default_density_g_cm3
        diameter = self.default_diameter_mm

        with contextlib.suppress(Exception):
            parsed_density = float(cast(Union[str, int, float], density_raw))
            if parsed_density > 0:
                density = parsed_density

        with contextlib.suppress(Exception):
            parsed_diameter = float(cast(Union[str, int, float], diameter_raw))
            if parsed_diameter > 0:
                diameter = parsed_diameter

        return density, diameter

    def _length_to_weight_g(
        self,
        used_length_mm: float,
        density_g_cm3: float,
        diameter_mm: float,
    ) -> float:
        radius_mm = diameter_mm / 2.0
        cross_section_mm2 = math.pi * radius_mm * radius_mm
        volume_mm3 = cross_section_mm2 * used_length_mm
        volume_cm3 = volume_mm3 / 1000.0
        return volume_cm3 * density_g_cm3

    async def _build_delta_from_length(
        self,
        spool_id: int,
        used_length_mm: float,
    ) -> Tuple[Optional[float], bool, bool]:
        spool_data, response = await self._fetch_spool(spool_id)
        if spool_data is None:
            if response.status_code == 404:
                if spool_id == self.spool_id:
                    logging.info(f"Spool ID {spool_id} not found, setting to None")
                    self.set_active_spool(None)
                return None, False, True

            err_msg = self._get_response_error(response)
            self._set_last_error(
                f"Failed to load spool metadata for spool id {spool_id}: {err_msg}"
            )
            return None, True, False

        density, diameter = self._resolve_material_values(spool_data)
        used_weight_g = self._length_to_weight_g(used_length_mm, density, diameter)
        return -used_weight_g, False, False

    async def _report_spool_usage(self, spool_id: int, used_length_mm: float) -> Tuple[bool, bool]:
        delta_weight_g, should_retry, not_found = await self._build_delta_from_length(
            spool_id,
            used_length_mm,
        )
        if delta_weight_g is None:
            return False, should_retry and not not_found

        response = await self._request(
            method="POST",
            url=f"{self.api_url}/spools/{spool_id}/consumptions",
            body={"delta_weight_g": delta_weight_g},
            connect_timeout=2.0,
            request_timeout=5.0,
        )
        if response.has_error():
            if response.status_code == 404:
                if spool_id == self.spool_id:
                    logging.info(f"Spool ID {spool_id} not found, setting to None")
                    self.set_active_spool(None)
                return False, False

            err_msg = self._get_response_error(response)
            self._set_last_error(
                "Failed to update extrusion for spool id "
                f"{spool_id}, received {err_msg}"
            )
            return False, True

        self._mark_success()
        return True, False

    async def report_extrusion(self, eventtime: float) -> float:
        pending_reports = self.pending_reports
        self.pending_reports = {}

        for spool_id, used_length_mm in pending_reports.items():
            if used_length_mm <= 0:
                continue

            logging.debug(
                f"Sending spool usage: ID: {spool_id}, Length: {used_length_mm:.3f}mm"
            )
            success, should_retry = await self._report_spool_usage(spool_id, used_length_mm)
            if not success and should_retry:
                self._add_extrusion(spool_id, used_length_mm)

        return self.eventloop.get_loop_time() + self.sync_rate_seconds

    async def _handle_spool_id_request(self, web_request: WebRequest) -> Dict[str, Any]:
        extruder = web_request.get_str("extruder", None)

        if web_request.get_request_type() == RequestType.POST:
            spool_id = web_request.get_int("spool_id", None)
            target = extruder if extruder is not None else self._current_extruder
            self.set_extruder_spool(target, spool_id)

        if extruder is not None:
            return {
                "spool_id": self.extruder_spools.get(extruder),
                "extruder": extruder,
                "extruder_spools": self.extruder_spools,
            }
        return {"spool_id": self.spool_id, "extruder_spools": self.extruder_spools}

    async def _handle_spool_ids_request(self, web_request: WebRequest) -> Dict[str, Any]:
        return {"extruder_spools": self.extruder_spools}

    def _normalize_proxy_path(self, path: str) -> str:
        if path.startswith("/api/v1"):
            suffix = path[len("/api/v1"):]
        elif path.startswith("/v1"):
            suffix = path[len("/v1"):]
        elif path.startswith("/"):
            suffix = path
        else:
            raise self.server.error("Invalid path format. Path must start with '/'")

        if suffix == "":
            return ""

        if suffix == "/spool":
            return "/spools"
        if suffix.startswith("/spool/"):
            return "/spools/" + suffix[len("/spool/"):]
        if suffix == "/filament":
            return "/filaments"
        if suffix.startswith("/filament/"):
            return "/filaments/" + suffix[len("/filament/"):]

        return suffix

    async def _map_legacy_use_request(
        self,
        method: str,
        path_suffix: str,
        body: Any,
    ) -> Tuple[str, str, Any]:
        match = re.match(r"^/spools/(?P<spool_id>\d+)/use$", path_suffix)
        if match is None:
            return method, path_suffix, body

        if method not in {"PUT", "POST"}:
            raise self.server.error("Invalid HTTP method for '/use', expected PUT or POST")
        if not isinstance(body, dict):
            raise self.server.error("Legacy '/use' requests require a JSON body")
        if "use_length" not in body:
            raise self.server.error("Legacy '/use' body must include 'use_length'")

        try:
            use_length_mm = float(body["use_length"])
        except Exception:
            raise self.server.error("Legacy '/use' field 'use_length' must be numeric")

        if use_length_mm < 0:
            use_length_mm = abs(use_length_mm)

        spool_id = int(match.group("spool_id"))
        delta_weight_g, should_retry, _ = await self._build_delta_from_length(
            spool_id,
            use_length_mm,
        )
        if delta_weight_g is None:
            if should_retry:
                raise self.server.error("Unable to fetch spool metadata for use_length mapping")
            raise self.server.error(f"Spool id {spool_id} was not found", 404)

        return "POST", f"/spools/{spool_id}/consumptions", {"delta_weight_g": delta_weight_g}

    async def _proxy_filaman_request(self, web_request: WebRequest) -> Dict[str, Any]:
        method = web_request.get_str("request_method").upper()
        path = web_request.get_str("path")
        query = web_request.get_str("query", None)
        body = web_request.get("body", None)
        use_v2_response = web_request.get_boolean("use_v2_response", False)

        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise self.server.error(f"Invalid HTTP method: {method}")
        if body is not None and method == "GET":
            raise self.server.error("GET requests cannot have a body")

        path_suffix = self._normalize_proxy_path(path)
        method, path_suffix, body = await self._map_legacy_use_request(
            method,
            path_suffix,
            body,
        )

        normalized_query: Optional[str] = None
        if query is not None:
            normalized_query = query.lstrip("?").strip()
            if normalized_query == "":
                normalized_query = None

        query_suffix = f"?{normalized_query}" if normalized_query is not None else ""
        full_url = f"{self.api_url}{path_suffix}{query_suffix}"

        logging.debug(f"Proxying {method} request to {full_url}")
        response = await self._request(method=method, url=full_url, body=body)

        if not use_v2_response:
            response.raise_for_status()
            if not response.content:
                return {}
            return cast(Dict[str, Any], response.json())

        if response.has_error():
            msg: str = str(response.error or "")
            with contextlib.suppress(Exception):
                payload = cast(Dict[str, Any], response.json())
                detail = payload.get("detail")
                if isinstance(detail, dict) and isinstance(detail.get("message"), str):
                    msg = detail["message"]
                elif isinstance(payload.get("message"), str):
                    msg = payload["message"]
            return {
                "response": None,
                "error": {
                    "status_code": response.status_code,
                    "message": msg,
                },
            }

        data: Any = None
        with contextlib.suppress(Exception):
            data = response.json()
        return {
            "response": data,
            "response_headers": dict(response.headers.items()),
            "error": None,
        }

    async def _handle_status_request(self, web_request: WebRequest) -> Dict[str, Any]:
        pending: List[Dict[str, Any]] = [
            {
                "spool_id": sid,
                "filament_used": used_mm,
                "filament_used_mm": used_mm,
            }
            for sid, used_mm in self.pending_reports.items()
        ]
        return {
            "filaman_connected": self.api_connected,
            "spoolman_connected": self.api_connected,
            "pending_reports": pending,
            "pending_reports_count": len(pending),
            "spool_id": self.spool_id,
            "extruder_spools": self.extruder_spools,
            "filament_present": self.filament_present,
            "filament_sensors": self.filament_sensors,
            "filament_detect_state": self._filament_detect_state,
            "last_error": self._last_error,
            "last_success_at": self._last_success_at,
        }

    def _send_status_notification(self) -> None:
        payload = {
            "filaman_connected": self.api_connected,
            "spoolman_connected": self.api_connected,
        }
        self.server.send_event("filaman:filaman_status_changed", payload)
        self.server.send_event("spoolman:spoolman_status_changed", payload)

    async def close(self) -> None:
        self.is_closing = True
        self.report_timer.stop()
        self._cancel_spool_check_task()
        self._cancel_runout_timers()
        self._cancel_repush_timer()

        if self.connection_task is None or self.connection_task.done():
            return

        try:
            await asyncio.wait_for(self.connection_task, 2.0)
        except asyncio.TimeoutError:
            self.connection_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.connection_task


def load_component(config: ConfigHelper) -> FilaManManager:
    return FilaManManager(config)
