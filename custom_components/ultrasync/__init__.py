"""The Interlogix/Hills ComNav UltraSync Hub component."""

import asyncio
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from ultrasync import AlarmScene
import voluptuous as vol

from .const import (
    DATA_COORDINATOR,
    DATA_UNDO_UPDATE_LISTENER,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SENSORS,
    SERVICE_AWAY,
    SERVICE_DISARM,
    SERVICE_STAY,
    SERVICE_FIRE,
    SERVICE_MEDICAL,
    SERVICE_PANIC,
    SERVICE_BYPASS,
    SERVICE_UNBYPASS,
    SERVICE_SWITCH
)
from .coordinator import UltraSyncDataUpdateCoordinator

PLATFORMS = ["sensor"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the UltraSync integration."""
    hass.data.setdefault(DOMAIN, {})

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up UltraSync from a config entry."""
    if not entry.options:
        options = {
            CONF_SCAN_INTERVAL: entry.data.get(
                CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
            ),
        }
        hass.config_entries.async_update_entry(entry, options=options)

    coordinator = UltraSyncDataUpdateCoordinator(
        hass,
        config=entry.data,
        options=entry.options,
    )

    await coordinator.async_refresh()

    if not coordinator.last_update_success:
        raise ConfigEntryNotReady

    undo_listener = entry.add_update_listener(_async_update_listener)

    hass.data[DOMAIN][entry.entry_id] = {
        DATA_COORDINATOR: coordinator,
        DATA_UNDO_UPDATE_LISTENER: [undo_listener],
        SENSORS: {},
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _async_register_services(hass, coordinator)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, component)
                for component in PLATFORMS
            ]
        )
    )

    if unload_ok:
        for unsub in hass.data[DOMAIN][entry.entry_id][DATA_UNDO_UPDATE_LISTENER]:
            unsub()
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


def _async_register_services(
    hass: HomeAssistant,
    coordinator: UltraSyncDataUpdateCoordinator,
) -> None:
    """Register integration-level services."""

    area_command_lock = asyncio.Lock()

    async def command_area(area, scene):
        async with area_command_lock:
            sent = await hass.async_add_executor_job(
                lambda: coordinator.hub.set_alarm(areas=[area], state=scene)
            )
            if not sent:
                raise HomeAssistantError("Comando non inviato alla centrale; verificare connessione e credenziali")
            for attempt in range(3):
                if attempt:
                    await asyncio.sleep(1)
                await coordinator.async_refresh()
                if not coordinator.last_update_success:
                    continue
                current = coordinator.hub.areas.get(area - 1, {})
                states = current.get('states', {})
                required = ('armed', 'partial', 'exit1', 'exit2')
                if not all(isinstance(states.get(k), bool) for k in required):
                    continue
                exiting = states['exit1'] or states['exit2']
                if scene == AlarmScene.DISARMED:
                    confirmed = not (states['armed'] or states['partial'] or exiting)
                elif scene == AlarmScene.STAY:
                    confirmed = states['partial'] or exiting
                else:
                    confirmed = (states['armed'] and not states['partial']) or exiting
                if confirmed:
                    return
            raise HomeAssistantError(
                "La centrale non conferma il comando per l'Area " + str(area)
                + "; stato: " + str(coordinator.hub.areas.get(area - 1, {}).get('status', 'sconosciuto'))
                + ". Verificare codice utente, zone aperte e guasti."
            )

    async def away(call) -> None:
        """Service call to set alarm system to 'away' mode in UltraSync Hub."""
        await command_area(call.data['area'], AlarmScene.AWAY)

    async def stay(call) -> None:
        """Service call to set alarm system to 'stay' mode in UltraSync Hub."""
        await command_area(call.data['area'], AlarmScene.STAY)
        
    def fire(call) -> None:
        """Service call to set alarm system to 'fire' mode in UltraSync Hub."""
        coordinator.hub.set_alarm(state=AlarmScene.FIRE)

    def medical(call) -> None:
        """Service call to set alarm system to 'medical' mode in UltraSync Hub."""
        coordinator.hub.set_alarm(state=AlarmScene.MEDICAL)

    def panic(call) -> None:
        """Service call to set alarm system to 'panic' mode in UltraSync Hub."""
        coordinator.hub.set_alarm(state=AlarmScene.PANIC)

    async def disarm(call) -> None:
        """Service call to disable alarm in UltraSync Hub."""
        await command_area(call.data['area'], AlarmScene.DISARMED)

    async def set_bypass_and_refresh(zone: int, state: bool) -> None:
        """Apply the ComNav toggle, then publish fresh area/zone state."""
        sent = await hass.async_add_executor_job(
            lambda: coordinator.hub.set_zone_bypass(state=state, zone=zone)
        )
        if not sent:
            raise HomeAssistantError(
                "Esclusione zona " + str(zone) + " non confermata dalla centrale; inserimento interrotto"
            )
        await coordinator.async_refresh()
        if not coordinator.last_update_success:
            raise HomeAssistantError(
                "Esclusione zona " + str(zone) + " applicata, ma stato aggiornato non disponibile; controllare la centrale"
            )
        current = coordinator.hub.zones.get(zone - 1, {})
        bits = current.get('bank_state', '')
        if not isinstance(bits, str) or len(bits) <= 3 or (bits[3] == '1') != state:
            raise HomeAssistantError(
                "Stato finale dell'esclusione zona " + str(zone) + " non confermato; controllare la centrale"
            )

    async def bypass(call) -> None:
        """Bypass a zone and publish the resulting Ready state."""
        await set_bypass_and_refresh(call.data['zone'], True)

    async def unbypass(call) -> None:
        """Unbypass a zone and publish the resulting Ready state."""
        await set_bypass_and_refresh(call.data['zone'], False)

    def switch(call) -> None:
        """Service call to switch on/off an output control in UltraSync Hub."""
        coordinator.hub.set_output_control(output=call.data['output'], state=call.data['state'])

    hass.services.async_register(DOMAIN, SERVICE_AWAY, away, schema=vol.Schema({vol.Required('area'): vol.All(vol.Coerce(int), vol.Range(min=1, max=8))}))
    hass.services.async_register(DOMAIN, SERVICE_STAY, stay, schema=vol.Schema({vol.Required('area'): vol.All(vol.Coerce(int), vol.Range(min=1, max=8))}))
    hass.services.async_register(DOMAIN, SERVICE_FIRE, fire, schema=vol.Schema({}))
    hass.services.async_register(DOMAIN, SERVICE_MEDICAL, medical, schema=vol.Schema({}))
    hass.services.async_register(DOMAIN, SERVICE_PANIC, panic, schema=vol.Schema({}))
    hass.services.async_register(DOMAIN, SERVICE_DISARM, disarm, schema=vol.Schema({vol.Required('area'): vol.All(vol.Coerce(int), vol.Range(min=1, max=8))}))
    hass.services.async_register(DOMAIN, SERVICE_BYPASS, bypass, schema=vol.Schema({vol.Required('zone'): vol.All(vol.Coerce(int), vol.Range(min=1, max=192))}))
    hass.services.async_register(DOMAIN, SERVICE_UNBYPASS, unbypass, schema=vol.Schema({vol.Required('zone'): vol.All(vol.Coerce(int), vol.Range(min=1, max=192))}))
    hass.services.async_register(DOMAIN, SERVICE_SWITCH, switch, schema=vol.Schema({
        vol.Required('output'): vol.Coerce(int),
        vol.Required('state'): vol.Coerce(int),
    }))

async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


class UltraSyncEntity(CoordinatorEntity):
    """Defines a base UltraSync entity."""

    def __init__(
        self, *, entry_id: str, name: str, coordinator: UltraSyncDataUpdateCoordinator
    ) -> None:
        """Initialize the UltraSync entity."""
        super().__init__(coordinator)
        self._name = name
        self._entry_id = entry_id

    @property
    def name(self) -> str:
        """Return the name of the entity."""
        return self._name

# ComNav exposes bypass as a toggle. Check the actual bypass bit before and
# after the command; can_bypass is an inverse status bit, not an authority flag.
import ultrasync.main
from ultrasync.main import HubResponseType, NX595EVendor

_original_set_zone_bypass = ultrasync.main.UltraSync.set_zone_bypass

def set_zone_bypass_verified(self, zone, state=False):
    if self.vendor != NX595EVendor.COMNAV:
        return _original_set_zone_bypass(self, zone=zone, state=state)
    if not isinstance(zone, int) or isinstance(zone, bool):
        return False
    if not self.update(max_age_sec=0):
        return False
    if zone - 1 not in self.zones:
        return False
    def bypassed():
        bits = self.zones[zone - 1].get('bank_state', '')
        if not isinstance(bits, str) or len(bits) <= 3 or bits[3] not in '01':
            return None
        return bits[3] == '1'
    before = bypassed()
    if before is None:
        return False
    if before == bool(state):
        return True
    response = self._UltraSync__get(
        '/user/zonefunction.cgi', rtype=HubResponseType.XML,
        payload={'sess': self.session_id, 'comm': 82, 'data0': zone - 1},
    )
    if not response:
        return False
    # The bus applies a bypass asynchronously. Read again, never toggle again.
    for attempt in range(5):
        if attempt:
            time.sleep(0.5)
        if self.update(max_age_sec=0) and bypassed() == bool(state):
            return True
    return False

ultrasync.main.UltraSync.set_zone_bypass = set_zone_bypass_verified
