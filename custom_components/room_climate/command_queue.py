"""One command at a time per cloud integration, with a pause in between.

LG ThinQ (a cloud integration) rejects or drops a request while it is still handling
another one. Room Climate sends commands from every room and from the House, so commands
to devices of the same cloud integration go through one queue. Local integrations (Matter,
Zigbee) are not queued: their commands go out at once.
"""

from __future__ import annotations

import asyncio
import logging
import time
from asyncio import sleep
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_DATA = f"{DOMAIN}_command_queues"


@dataclass
class _Queue:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last: float = float("-inf")  # monotonic time the last command returned


@dataclass
class _Queues:
    queues: dict[str, _Queue] = field(default_factory=dict)
    cloud: dict[str, bool] = field(default_factory=dict)  # integration -> is cloud


async def _cloud_integration(hass: HomeAssistant, entity_id: str) -> str | None:
    """The integration behind `entity_id` if it is a cloud integration, else None."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return None
    data: _Queues = hass.data.setdefault(_DATA, _Queues())
    if entry.platform not in data.cloud:
        try:
            integration = await async_get_integration(hass, entry.platform)
            iot_class = integration.iot_class or ""
        except Exception:  # unknown integration: treat as local
            iot_class = ""
        data.cloud[entry.platform] = iot_class.startswith("cloud")
    return entry.platform if data.cloud[entry.platform] else None


async def async_queued_call(
    hass: HomeAssistant,
    entity_id: str,
    domain: str,
    service: str,
    data: dict[str, Any],
    pause: float,
    context: Context | None = None,
) -> None:
    """Call a service; for a cloud integration wait for the queue and the pause first."""
    integration = await _cloud_integration(hass, entity_id)
    if integration is None:
        await hass.services.async_call(domain, service, data, blocking=True, context=context)
        return
    queues: _Queues = hass.data.setdefault(_DATA, _Queues())
    queue = queues.queues.setdefault(integration, _Queue())
    async with queue.lock:
        wait = queue.last + pause - time.monotonic()
        if wait > 0:
            _LOGGER.debug("%s: waiting %.1f s before %s.%s", integration, wait, domain, service)
            await sleep(wait)
        try:
            await hass.services.async_call(domain, service, data, blocking=True, context=context)
        finally:
            queue.last = time.monotonic()
