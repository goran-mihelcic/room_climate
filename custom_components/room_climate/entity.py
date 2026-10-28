"""Shared base for Room Climate entities."""

from __future__ import annotations

from .controller import RoomController


class RoomClimateMixin:
    """Ties an entity to its room (or bathroom) device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, controller: RoomController, key: str, name: str, bathroom: bool = False
    ) -> None:
        self._ctl = controller
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{controller.entry.entry_id}_{key}"
        self._attr_device_info = (
            controller.bath_device_info if bathroom else controller.room_device_info
        )
