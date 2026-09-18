import asyncio

from station import logger

class PrototypeGateway:
    def _init_gateway_lifecycle(self):
        self._gateway = None
        self._settings = None
        self._gateway_lock = asyncio.Lock()
        self._gateway_pid = None

    async def start(self):
        await super().start()
        await self._ensure_gateway()

    async def stop(self):
        await self._teardown_gateway()
        await super().stop()

    async def reload(self, model_settings=None):
        await super().reload(model_settings=model_settings)
        await self._teardown_gateway()
        await self._ensure_gateway()

    async def _restart_gateway(self):
        await self._teardown_gateway()
        await self._ensure_gateway()

    async def _teardown_gateway(self):
        await self._before_teardown_gateway()
        async with self._gateway_lock:
            gateway = self._gateway
            self._gateway = None
            self._settings = None
            self._clear_gateway_state()
        await self._cancel_chat_workers()
        if gateway is not None:
            self._unregister_gateway_process(gateway)
            await gateway.close()
        logger.info("%s gateway stopped model_id=%s", self._gateway_label(), self.model_id)

    async def _ensure_gateway(self):
        async with self._gateway_lock:
            if self._gateway is not None:
                return self._gateway
            gateway = await self._start_gateway_locked()
            self._gateway = gateway
            self._register_gateway_process(gateway)
        await self._after_gateway_started(gateway)
        return gateway

    def _gateway_registry(self):
        return self.app.get("gateway_processes")

    def _register_gateway_process(self, gateway):
        registry = self._gateway_registry()
        proc = gateway.proc
        if registry is None or proc is None:
            return
        registry.register(model_id=self.model_id, kind=self._gateway_label(), proc=proc)
        self._gateway_pid = proc.pid

    def _unregister_gateway_process(self, gateway):
        registry = self._gateway_registry()
        if registry is None:
            return
        proc = gateway.proc
        if proc is not None:
            registry.unregister(proc)
        elif self._gateway_pid is not None:
            registry.unregister_pid(self._gateway_pid)
        self._gateway_pid = None

    async def _before_teardown_gateway(self):
        return

    def _clear_gateway_state(self):
        return

    async def _after_gateway_started(self, gateway):
        return

    def _gateway_label(self):
        raise NotImplementedError

    async def _start_gateway_locked(self):
        raise NotImplementedError
