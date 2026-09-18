import asyncio
import random
import time

import aiohttp

from station import logger

_circuits = {}
_retry_attempts = 3
_retry_base_seconds = 0.5
_circuit_failures = 5
_circuit_cooldown_seconds = 1.0

class CircuitOpen(Exception):
    pass

class Circuit:
    def __init__(self):
        self.failures = 0
        self.open_until = 0.0

    def allow(self):
        if time.monotonic() < self.open_until:
            raise CircuitOpen()

    def success(self):
        self.failures = 0

    def failure(self):
        self.failures += 1
        if self.failures >= _circuit_failures:
            self.open_until = time.monotonic() + _circuit_cooldown_seconds
            self.failures = 0
            return True
        return False

def apply_retry_config(server):
    global _retry_attempts, _retry_base_seconds, _circuit_failures, _circuit_cooldown_seconds
    _retry_attempts = server.outbound_retry_attempts
    _retry_base_seconds = server.outbound_retry_base_seconds
    _circuit_failures = server.outbound_circuit_failures
    _circuit_cooldown_seconds = server.outbound_circuit_cooldown_seconds

def circuit_for(name):
    circuit = _circuits.get(name)
    if circuit is None:
        circuit = Circuit()
        _circuits[name] = circuit
    return circuit

class RetryableError(Exception):
    pass

def is_transient_status(status):
    return status == 429 or status >= 500

async def retry_transient(func, *args, circuit_name="outbound", **kwargs):
    circuit = circuit_for(circuit_name)
    try:
        circuit.allow()
    except CircuitOpen:
        logger.warning("circuit open name=%s", circuit_name)
        raise
    for attempt in range(_retry_attempts + 1):
        try:
            result = await func(*args, **kwargs)
            circuit.success()
            return result
        except CircuitOpen:
            raise
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError, RetryableError) as e:
            opened = circuit.failure()
            if opened:
                logger.warning("circuit open name=%s error=%s", circuit_name, e)
            if attempt == _retry_attempts:
                raise
            sleep_time = (_retry_base_seconds * (2**attempt)) + (random.random() * 0.5)
            logger.warning(
                "retry attempt=%s/%s sleep_s=%.2f error=%s",
                attempt + 1,
                _retry_attempts,
                sleep_time,
                e,
            )
            await asyncio.sleep(sleep_time)
