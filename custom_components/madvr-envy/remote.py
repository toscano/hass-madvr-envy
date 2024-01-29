"""
Support for madVR Labs Envy video processor.
For more information visit
https://github.com/toscano/hass-madvr-envy
"""

import logging
import asyncio
import shlex
import voluptuous as vol
import homeassistant.util.dt as dt_util

from datetime import timedelta
from wakeonlan import send_magic_packet

from homeassistant.components.remote import PLATFORM_SCHEMA, RemoteEntity
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_TIMEOUT,
    CONF_MAC,
    CONF_SCAN_INTERVAL,
    STATE_ON,
    STATE_OFF,
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType


# Some Constants
CONTROL_PROTOCOL_PORT   = 44077
RETRY_CONNECT_INTERVAL  = timedelta(seconds=20)
HEARTBEAT_INTERVAL      = timedelta(seconds=20)

_LOGGER = logging.getLogger(__name__)

# Validation of the user's configuration
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_MAC): cv.string,
        vol.Optional(CONF_TIMEOUT): cv.string,
    }
)

def is_integer(n):
    try:
        float(n)
    except ValueError:
        return False
    else:
        return float(n).is_integer()

async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType = None,
) -> None:
    """Set up platform."""
    host = config.get(CONF_HOST)
    name = config.get(CONF_NAME)
    mac = config.get(CONF_MAC)

    async_add_entities(
        [
            madVR_remote(hass, name, host, mac),
        ]
    )

class madVR_remote(RemoteEntity):
    """Implements the interface for Madvr Remote in HA."""

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        host: str,
        mac: str,
    ) -> None:
        """MadVR Init."""
        self._hass = hass
        self._name = name
        self._host = host
        self._port = CONTROL_PROTOCOL_PORT

        if len(mac) == 17:
            sep = mac[2]
            mac = mac.replace(sep, "")
        elif len(mac) == 14:
            sep = mac[4]
            mac = mac.replace(sep, "")

        if len(mac) != 12:
            _LOGGER.error("Invalid MAC ADDRESS in configuration %s.", mac)
            mac = ""

        self._mac = mac.lower()
        if (self._mac != ""):
            self._attr_unique_id = 'madvr_envy_{}'.format(self._mac)


        self._attr_is_on = False
        self._wait_on_next_open = False
        self._last_inbound_data_utc = dt_util.utcnow()
        self._sent_heartbeat = 0
        self._cmd_queue = None

        self._ioloop_future = None
        self._is_closing = False
        self._queue_future = None
        self._net_future = None

        self._extra_attributes = {}
        self.tasks = []

        async_track_time_interval(self._hass, self._async_check_heartbeat, HEARTBEAT_INTERVAL)

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        connect_task = self._hass.loop.create_task(self._open_connection())
        self.tasks.append(connect_task)

    async def async_will_remove_from_hass(self) -> None:
        self._close_connection()

        for task in self.tasks:
            if not task.done():
                task.cancel()

    @property
    def should_poll(self):
        """Poll."""
        return False

    @property
    def state(self):
        if (self._attr_is_on):
            return STATE_ON

        return STATE_OFF

    @property
    def name(self):
        """Name."""
        return self._name

    @property
    def icon(self):
        if self.state == STATE_OFF:
            return "mdi:remote-off"

        return "mdi:remote"

    @property
    def host(self):
        """Host."""
        return self._host

    @property
    def extra_state_attributes(self):
        """Return extra state attributes."""
        # Useful for making sensors
        return self._extra_attributes


    async def async_turn_off(self, **kwargs):
        """
        Send the Standby command.
        """
        if (self._attr_is_on):
            _LOGGER.info("%s:Turning off: Standby", self._mac)
            self.send("Standby")

    async def async_turn_on(self, **kwargs):
        """
        Send the WOL packet
        """
        _LOGGER.info("%s:Sending Wake On LAN", self._mac)
        send_magic_packet(self._mac)

    async def async_send_command(self, command: list, **kwargs):
        """Send commands to a device."""
        _LOGGER.debug("%s:adding commands '%s'", self._mac, command)
        for c in command:
            self._cmd_queue.put_nowait(c)

    def send(self, cmd):
        _LOGGER.debug("%s:-->%s", self._mac, cmd)
        self._cmd_queue.put_nowait(cmd)

    async def _async_check_heartbeat(self, now=None):
        """Maybe send a Heartbeat."""
        if (self._attr_is_on == False):
            #_LOGGER.debug("%s:Heartbeat...not connected", self._mac)
            return

        if (self._profile_eol==""):
            self._profile_groups = {}
            self._profiles = {}
            self._profile_eol="."
            self.send("EnumProfileGroups")

        #_LOGGER.debug("%s:Heartbeat...CONNECTED [%i] [%s]", self._mac, self._sent_heartbeat, self._last_inbound_data_utc)

        if (self._last_inbound_data_utc + HEARTBEAT_INTERVAL + HEARTBEAT_INTERVAL < dt_util.utcnow() ):

            if (self._sent_heartbeat > 2):
                # Schedule a re-connect...
                _LOGGER.error("%s:HEARTBEAT...reconnect needed.", self._mac)
                self._sent_heartbeat = 0
                self._attr_is_on = False
                self._reset_attributes()
                self._hass.async_add_job(self._open_connection())
                return

            self._sent_heartbeat = self._sent_heartbeat + 1
            if (self._sent_heartbeat > 1):
                _LOGGER.debug("%s:HEARTBEAT...sending Heartbeat %d", self._mac, self._sent_heartbeat)
            self.send("Heartbeat")
        elif (self._sent_heartbeat > 0):
            if (self._sent_heartbeat > 1):
                _LOGGER.debug("%s:HEARTBEAT...resetting Heartbeat %s",  self._mac, self._last_inbound_data_utc)
            self._sent_heartbeat = 0


    async def _open_connection(self) -> None:
        """Let's try to connect to the server"""

        self._attr_is_on = False
        self._reset_attributes()
        self.schedule_update_ha_state()

        if self._wait_on_next_open:
            _LOGGER.debug("%s:Waiting %d seconds prior to reconnect.", self._mac, RETRY_CONNECT_INTERVAL.total_seconds())
            self._wait_on_next_open = False
            await asyncio.sleep(RETRY_CONNECT_INTERVAL.total_seconds())

        self._last_inbound_data_utc = dt_util.utcnow()
        self._sent_heartbeat = 0
        self._cmd_queue = asyncio.Queue()
        self._profile_eol = ""
        self._profile_groups = {}
        self._profiles = {}
        self._profile_selects = {}

        # Now open the socket
        workToDo = True
        notify_log = True
        while workToDo:
            try:
                _LOGGER.debug("%s:Attempting connection to %s:%s", self._mac, self._host, self._port)

                reader, writer = await asyncio.open_connection(self._host, self._port)
                workToDo = False
            except:
                if (notify_log):
                    _LOGGER.warn("%s:Connection to %s:%s failed... will try again every %d seconds.", self._mac, self._host, self._port, RETRY_CONNECT_INTERVAL.total_seconds())
                    # Don't fill up the log please
                    notify_log = False
                else:
                    _LOGGER.debug("%s:Connection to %s:%s failed... will try again in %d seconds.", self._mac, self._host, self._port, RETRY_CONNECT_INTERVAL.total_seconds())

                await asyncio.sleep(RETRY_CONNECT_INTERVAL.total_seconds())

        _LOGGER.info("%s:Connected to %s:%s", self._mac, self._host, self._port)
        self._attr_is_on = True
        self._reset_attributes()
        self.schedule_update_ha_state()

        self.send("GetIncomingSignalInfo")
        self.send("GetAspectRatio")
        self.send("GetMaskingRatio")
        self.send("GetTemperatures")
        self.send("GetMacAddress")
        self.send("EnumProfileGroups")

        self._ioloop_future = asyncio.ensure_future(self._ioloop(reader, writer))

    async def _close_connection(self):
        """
        Disconnect from the server.
        """
        if self._is_closing == False:
            _LOGGER.info("%s:Closing connection to %s:%s", self._mac, self._host, self._port)
            self._is_closing = True
            self._attr_is_on = False
            self._extra_attributes = {}
            self.schedule_update_ha_state()
            self._queue_future.cancel()

    async def _ioloop(self, reader, writer):
        """This is our Socket I/O Loop"""
        self._queue_future = asyncio.ensure_future(self._cmd_queue.get())

        self._net_future = asyncio.ensure_future(reader.readline())

        try:

            while True:

                done, pending = await asyncio.wait(
                        [self._queue_future, self._net_future],
                        return_when=asyncio.FIRST_COMPLETED)

                if self._is_closing:
                    writer.close()
                    self._queue_future.cancel()
                    self._net_future.cancel()
                    _LOGGER.info("%s:IO loop exited for local close", self._mac)
                    return

                if self._net_future in done:

                    if reader.at_eof():
                        self._queue_future.cancel()
                        self._net_future.cancel()
                        _LOGGER.info("%s:IO loop exited for remote close...", self._mac)
                        return

                    response = self._net_future.result()
                    self._last_inbound_data_utc = dt_util.utcnow()
                    try:
                        self._process_response(response)
                    except:
                        pass

                    self._net_future = asyncio.ensure_future(reader.readline())

                if self._queue_future in done:
                    cmd = self._queue_future.result()
                    cmd += '\r'
                    writer.write(bytearray(cmd, 'utf-8'))
                    await writer.drain()

                    self._queue_future = asyncio.ensure_future(self._cmd_queue.get())


            _LOGGER.debug("%s:IO loop exited", self._mac)

        except GeneratorExit:
            return

        except asyncio.CancelledError:
            _LOGGER.debug("%s:IO loop cancelled", self._mac)
            writer.close()
            self._queue_future.cancel()
            self._net_future.cancel()
            raise
        except:
            _LOGGER.exception("%s:Unhandled exception in IO loop",self._mac)
            raise

    def _process_response(self, res):
        try:
            s = str(res, 'utf-8').strip()
            _LOGGER.debug("%s:<--%s", self._mac, s)

            if (s=="PowerOff" or
                s=="Standby"  or
                s=="Restart"    ):
                self._process_power_message(s)

            elif (s.startswith("MacAddress")):
                self._process_MacAddress_message(s)

            elif (s.startswith("NoSignal")           or
                  s.startswith("IncomingSignalInfo") or
                  s.startswith("OutgoingSignalInfo") or
                  s.startswith("AspectRatio")        or
                  s.startswith("MaskingRatio")       or
                  s.startswith("ActivateProfile")    or
                  s.startswith("Temperatures")       or
                  s.startswith("ActiveProfile")      or
                  s.startswith("ActivateProfile")    or
                  s.startswith("DeleteProfile")      or
                  s.startswith("DeleteProfileGroup") or
                  s.startswith("CreateProfileGroup") or
                  s.startswith("RenameProfileGroup") or
                  s.startswith("CreateProfile")      or
                  s.startswith("RenameProfile" )     or
                  s.startswith("WELCOME")    ):
                self._process_notification_message(s)

            elif (s.startswith("ProfileGroup")):
                self._process_profile_group(s)

            elif (s.startswith("Profile")):
                self._process_profile(s)

        except Exception as e:
            _LOGGER.exception("%s:_process_response ex with res=%s", self._mac, res)

    def _process_profile_group(self, msg: str):

        if (msg == "ProfileGroup."):
            # end of list marker
            for k in self._profile_groups:
                self.send("EnumProfiles {}".format(k))
                self._extra_attributes["profile_group_name_{}".format(k)]=self._profile_groups[k]

            for k in self._profile_groups:
                self.send("GetActiveProfile {}".format(k))

        else:
            title, *args = shlex.split(msg)

            args[0] = args[0].replace("customProfileGroup", "")
            args[0] = args[0].replace("Profiles", "")

            if (is_integer(args[0]) or args[0] in ['source','display']):
                self._profile_groups[args[0]] = args[1]

    def _process_profile(self, msg: str):

        if (msg == "Profile."):
            # end of list marker
            l = ['None']
            for k in self._profiles:
                if k.startswith(self._profile_eol):
                    l.append(self._profiles[k])

            self._extra_attributes["profile_group_options_{}".format(self._profile_eol)]=l

            self.schedule_update_ha_state()
        else:
            title, *args = shlex.split(msg)

            args[0] = args[0].replace("customProfileGroup", "")
            args[0] = args[0].replace("Profiles_", "_")
            args[0] = args[0].replace("_profile", "_")

            self._profiles[args[0]] = args[1]
            self._profile_eol = args[0].split("_")[0]


    def _process_power_message(self, msg: str):

        if (msg=="PowerOff" or msg=="Standby" or msg=="Restart"):
            self._close_connection()
            self._wait_on_next_open = True
            self._hass.async_add_job(self._open_connection())
            self.schedule_update_ha_state()

    def _process_MacAddress_message(self, msg: str):

        macaddress = msg.split(" ")[1].strip().lower()
        if len(macaddress) == 17:
            sep = macaddress[2]
            macaddress = macaddress.replace(sep, "")
        elif len(macaddress) == 14:
            sep = macaddress[4]
            macaddress = macaddress.replace(sep, "")

        if len(macaddress) != 12:
            raise ValueError("Incorrect MAC address format")

        if (macaddress!=self._mac):
            _LOGGER.error("Using reported MAC address %s from Envy NOT %s from configuration.", macaddress, self._mac)
            self._mac = macaddress

        self._extra_attributes["reported_mac"] = macaddress
        self.schedule_update_ha_state()

    def _reset_attributes(self):
        self._extra_attributes["incoming_signal"] = False
        self._extra_attributes["incoming_res"] = ""
        self._extra_attributes["incoming_frame_rate"] = ""
        self._extra_attributes["incoming_3d"] = False
        self._extra_attributes["incoming_color_space"] = ""
        self._extra_attributes["incoming_bit_depth"] = ""
        self._extra_attributes["incoming_hdr_flag"] = False
        self._extra_attributes["incoming_colorimetry"] = ""
        self._extra_attributes["incoming_black_levels"] = ""
        self._extra_attributes["incoming_aspect_ratio"] = ""

        self._extra_attributes["outgoing_res"] = ""
        self._extra_attributes["outgoing_frame_rate"] = ""
        self._extra_attributes["outgoing_color_space"] = ""
        self._extra_attributes["outgoing_bit_depth"] = ""
        self._extra_attributes["outgoing_hdr_flag"] = False
        self._extra_attributes["outgoing_colorimetry"] = ""
        self._extra_attributes["outgoing_black_levels"] = ""

        self._extra_attributes["aspect_res"] = ""
        self._extra_attributes["aspect_dec"] = float(0)
        self._extra_attributes["aspect_int"] = 0
        self._extra_attributes["aspect_name"] = ""

        self._extra_attributes["masking_res"] = ""
        self._extra_attributes["masking_dec"] = float(0)
        self._extra_attributes["masking_int"] = 0

        self._extra_attributes["temp_gpu"] = 0
        self._extra_attributes["temp_hdmi"]= 0
        self._extra_attributes["temp_main"]= 0
        self._extra_attributes["temp_other"]= 0

        for k in self._extra_attributes:
            if (k.startswith("profile_")):
                self._extra_attributes[k]=""

    def _process_notification_message(self, msg: str):

        title, *signal_info = msg.split(" ")

        if "NoSignal" in title:
            self._extra_attributes["incoming_signal"] = False
            self._extra_attributes["incoming_res"] = ""
            self._extra_attributes["incoming_frame_rate"] = ""
            self._extra_attributes["incoming_3d"] = False
            self._extra_attributes["incoming_color_space"] = ""
            self._extra_attributes["incoming_bit_depth"] = ""
            self._extra_attributes["incoming_hdr_flag"] = False
            self._extra_attributes["incoming_colorimetry"] = ""
            self._extra_attributes["incoming_black_levels"] = ""
            self._extra_attributes["incoming_aspect_ratio"] = ""

        elif "IncomingSignalInfo" in title:
            self._extra_attributes["incoming_signal"] = True
            self._extra_attributes["incoming_res"] = signal_info[0]
            self._extra_attributes["incoming_frame_rate"] = signal_info[1]
            self._extra_attributes["incoming_3d"] = "3D" in signal_info[2]
            self._extra_attributes["incoming_color_space"] = signal_info[3]
            self._extra_attributes["incoming_bit_depth"] = signal_info[4]
            self._extra_attributes["incoming_hdr_flag"] = "HDR" in signal_info[5]
            self._extra_attributes["incoming_colorimetry"] = signal_info[6]
            self._extra_attributes["incoming_black_levels"] = signal_info[7]
            self._extra_attributes["incoming_aspect_ratio"] = signal_info[8]

        elif "OutgoingSignalInfo" in title:
            self._extra_attributes["outgoing_res"] = signal_info[0]
            self._extra_attributes["outgoing_frame_rate"] = signal_info[1]
            self._extra_attributes["outgoing_color_space"] = signal_info[3]
            self._extra_attributes["outgoing_bit_depth"] = signal_info[4]
            self._extra_attributes["outgoing_hdr_flag"] = "HDR" in signal_info[5]
            self._extra_attributes["outgoing_colorimetry"] = signal_info[6]
            self._extra_attributes["outgoing_black_levels"] = signal_info[7]

        elif "AspectRatio" in title:
            self._extra_attributes["aspect_res"] = signal_info[0]
            self._extra_attributes["aspect_dec"] = float(signal_info[1])
            self._extra_attributes["aspect_int"] = signal_info[2]
            self._extra_attributes["aspect_name"] = signal_info[3]

        elif "MaskingRatio" in title:
            self._extra_attributes["masking_res"] = signal_info[0]
            self._extra_attributes["masking_dec"] = float(signal_info[1])
            self._extra_attributes["masking_int"] = signal_info[2]

        elif "Temperatures" in title:
            self._extra_attributes["temp_gpu"] = signal_info[0]
            self._extra_attributes["temp_hdmi"]= signal_info[1]
            self._extra_attributes["temp_main"]= signal_info[2]
            self._extra_attributes["temp_other"]= signal_info[3]

        elif "ActiveProfile" in title or "ActivateProfile" in title:
            signal_info[0] = signal_info[0].lower()

            if signal_info[1]=="0":
                self._extra_attributes["profile_group_selecton_{}".format(signal_info[0])] = "None"
            else:
                for k in self._profiles:
                    if k == "{}_{}".format( signal_info[0], signal_info[1]):
                        self._extra_attributes["profile_group_selecton_{}".format(signal_info[0])] = self._profiles[k]

        elif "WELCOME" in title:
            self._extra_attributes["version"]= signal_info[2]

        elif ("DeleteProfile" in title or
              "DeleteProfileGroup" in title or
              "CreateProfileGroup" in title or
              "RenameProfileGroup" in title or
              "CreateProfile" in title or
              "RenameProfile" in title):
            # This will cause a rescan on the next HEARTBEAT
            self._profile_eol = ""


        self.schedule_update_ha_state()


