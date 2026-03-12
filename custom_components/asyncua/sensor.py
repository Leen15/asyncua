"""Platform for sensor integration."""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Union

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.config_validation import PLATFORM_SCHEMA
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AsyncuaCoordinator
from .const import (
    CONF_NODE_DEVICE_CLASS,
    CONF_NODE_HUB,
    CONF_NODE_ID,
    CONF_NODE_NAME,
    CONF_NODE_STATE_CLASS,
    CONF_NODE_UNIQUE_ID,
    CONF_NODE_UNIT_OF_MEASUREMENT,
    CONF_NODES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# NOTE:
# - state_class is optional. If you set a default here, string sensors would start
#   with a numeric state_class and then lose it at runtime, which triggers Repairs.
NODE_SCHEMA = {
    CONF_NODES: [
        {
            vol.Optional(CONF_NODE_DEVICE_CLASS): cv.string,
            vol.Optional(CONF_NODE_STATE_CLASS): cv.string,
            vol.Optional(CONF_NODE_UNIT_OF_MEASUREMENT): cv.string,
            vol.Optional(CONF_NODE_UNIQUE_ID): cv.string,
            vol.Required(CONF_NODE_ID): cv.string,
            vol.Required(CONF_NODE_NAME): cv.string,
            vol.Required(CONF_NODE_HUB): cv.string,
        }
    ]
}

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    schema=NODE_SCHEMA,
    extra=vol.ALLOW_EXTRA,
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up asyncua sensor platform."""

    # {"hub": [node0, node1]}
    # where node0 equals {"name": "node0", "unique_id": "node0", ...}.

    coordinator_nodes: dict[str, list[dict[str, str]]] = {}
    coordinators: dict[str, AsyncuaCoordinator] = {}
    asyncua_sensors: list[AsyncuaSensor] = []

    # Compile dictionary of {hub: [node0, node1, ...]}
    for val_node in config[CONF_NODES]:
        coordinator_nodes.setdefault(val_node[CONF_NODE_HUB], []).append(val_node)

    for hub_name, hub_nodes in coordinator_nodes.items():
        # Get the respective asyncua coordinator
        if hub_name not in hass.data[DOMAIN]:
            raise ConfigEntryError(
                f"Asyncua hub {hub_name} not found. Specify a valid asyncua hub in the configuration."
            )

        coordinators[hub_name] = hass.data[DOMAIN][hub_name]
        coordinators[hub_name].add_sensors(sensors=hub_nodes)

        # Create sensors with injecting respective asyncua coordinator
        for node_cfg in hub_nodes:
            asyncua_sensors.append(
                AsyncuaSensor(
                    coordinator=coordinators[hub_name],
                    name=node_cfg[CONF_NODE_NAME],
                    unique_id=node_cfg.get(CONF_NODE_UNIQUE_ID),
                    hub=node_cfg[CONF_NODE_HUB],
                    node_id=node_cfg[CONF_NODE_ID],
                    device_class=node_cfg.get(CONF_NODE_DEVICE_CLASS),
                    unit_of_measurement=node_cfg.get(CONF_NODE_UNIT_OF_MEASUREMENT),
                    state_class=node_cfg.get(CONF_NODE_STATE_CLASS),
                )
            )

    async_add_entities(new_entities=asyncua_sensors)


class AsyncuaSensor(CoordinatorEntity[AsyncuaCoordinator], SensorEntity):
    """A sensor implementation for Asyncua OPCUA nodes."""

    def __init__(
        self,
        coordinator: AsyncuaCoordinator,
        name: str,
        hub: str,
        node_id: str,
        device_class: Any,
        unique_id: Union[str, None] = None,
        state_class: Union[str, None] = None,
        precision: int = 2,
        unit_of_measurement: Union[str, None] = None,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator=coordinator)

        self._attr_name = name
        self._attr_unique_id = (
            unique_id if unique_id is not None else f"{DOMAIN}.{hub}.{node_id}"
        )

        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit_of_measurement
        self._attr_native_value = None

        # Keep a copy of the configured metadata so we can restore it after reconnects.
        self._configured_state_class = state_class
        self._configured_precision = precision

        self._attr_state_class = state_class
        self._attr_suggested_display_precision = precision

        self._attr_available = True
        self._hub = hub
        self._node_id = node_id

        # Initialize from any existing coordinator data (if any)
        initial_value = self._parse_coordinator_data(coordinator_data=coordinator.data)
        if initial_value is None:
            self._attr_available = False
        else:
            self._attr_available = True
            self._attr_native_value = initial_value
            self._apply_metadata_for_value(initial_value)

    @property
    def unique_id(self) -> str | None:
        """Return the unique_id of the sensor."""
        return self._attr_unique_id

    @property
    def node_id(self) -> str:
        """Return the node address provided by the OPCUA server."""
        return self._node_id

    def _parse_coordinator_data(self, coordinator_data: dict[str, Any]) -> Any:
        """Parse the value from the mapped coordinator."""
        if self._attr_name is None:
            raise ConfigEntryError(
                f"Unable to find {self._attr_name} in coordinator {self.coordinator.name}"
            )
        return coordinator_data.get(self._attr_name)

    def _apply_metadata_for_value(self, value: Any) -> None:
        """Apply state_class/precision depending on the native value type."""
        # Home Assistant: if state_class != None, sensor is treated as numeric.
        # Keep state_class only for numeric values; drop it for strings/other types.
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            self._attr_state_class = self._configured_state_class
            self._attr_suggested_display_precision = self._configured_precision
        else:
            self._attr_state_class = None
            self._attr_suggested_display_precision = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle update of the data."""
        value = self._parse_coordinator_data(coordinator_data=self.coordinator.data)

        # PLC down / value missing: mark unavailable but DO NOT touch metadata like state_class.
        if value is None:
            self._attr_available = False
            self.async_write_ha_state()
            return

        self._attr_available = True
        self._attr_native_value = value
        self._apply_metadata_for_value(value)

        self.async_write_ha_state()
