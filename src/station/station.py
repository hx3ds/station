import asyncio
import os
import signal
import aiohttp
from aiohttp import web
from station.client.context import ClientContext, update_client_context_prototype
from station.client.conductor import configure_outbound_concurrency
from station.client.retry import apply_retry_config
from station.config.config import Config
from station.config.reload import apply_reloaded_config, reload_from_disk
from station.database.database import create_database, wipe_station_runtime_data
from station.api.routes import Routes
from station.client.consul import fetch_prototype
from station.webrtc import WebRTCService
from station.discord_voice import DiscordVoiceService
from station.recorder import Recorder, setup_recorder_routes
from station.conductor.handlers import ensure_local_conductor
from station.gateway_processes import GatewayProcessRegistry
from station.tenants import Tenant, TenantRegistry, TokenState
from station.prototypes.registry import register_kind

from station import logger
from station.prototypes.boundary import ext_bool, ext_dict, ext_int, ext_require, ext_str

class Station:
    def __init__(
        self,
        host=None,
        port=None,
        config_file=None,
        secret_file=None,
        env=None,
        consul_url=None,
        db_backend=None,
        db_dsn=None,
        db_path=None,
        fs_root=None,
        prototype_id=None,
        token=None,
        prototype_config_file=None,
        prototype_secret_file=None,
        ava=None,
        prototype=None,
        prototype_kind=None,
        kinds=None,
    ):
        self.config = Config(config_file=config_file, secret_file=secret_file)

        if env is not None:
            self.config.env = env
        if consul_url is not None:
            self.config.consul_url = consul_url
        if host is not None:
            self.config.host = host
        if port is not None:
            self.config.port = ext_int("port", port)
        if db_backend is not None:
            db_backend = ext_str("db_backend", db_backend, default="")
            if not db_backend:
                raise TypeError("db_backend must be non-empty str")
            self.config.database.backend = db_backend.lower()
        if db_dsn is not None:
            self.config.database.dsn = ext_str("db_dsn", db_dsn, default="").strip() or None
        if db_path is not None:
            self.config.database.path = db_path
        if fs_root is not None:
            self.config.fs_root = fs_root
        primary = self.config.prototype
        if prototype_id is not None:
            primary.id = prototype_id
        if token is not None:
            primary.token = token
        if prototype_config_file is not None:
            primary.config_file = ext_str("prototype_config_file", prototype_config_file, default="").strip() or None
        if prototype_secret_file is not None:
            primary.secret_file = ext_str("prototype_secret_file", prototype_secret_file, default="").strip() or None
        if ava is not None:
            primary.ava = ava

        if kinds is not None:
            kinds = ext_dict("kinds", kinds)
            for kind_name, kind_cls in kinds.items():
                register_kind(kind_name, kind_cls)

        self._primary_prototype_class = None
        if prototype is not None:
            ext_require("prototype", prototype, (type,))
            kind = prototype_kind
            if kind is None:
                kind = primary.kind or "station"
            kind = ext_str("prototype_kind", kind, default="")
            if not kind:
                raise TypeError("prototype_kind must be non-empty str")
            register_kind(kind, prototype)
            primary.kind = kind
            self._primary_prototype_class = prototype
        elif prototype_kind is not None:
            kind = ext_str("prototype_kind", prototype_kind, default="")
            if not kind:
                raise TypeError("prototype_kind must be non-empty str")
            primary.kind = kind

        self.prototype_id = primary.id
        if self.prototype_id is None:
            raise ValueError("prototypes[0].id is required")
        if not primary.token:
            raise ValueError("prototypes[0].token is required")

        self.db = None
        self.app = None
        self.recorder = None
        self.logger = None

    async def handle_health(self, request):
        registry = self.app["tenants"]
        return web.json_response(
            {
                "ok": True,
                "ready": True,
                "needs_manual": self.app["needs_manual"],
                "prototype_id": self.prototype_id,
                "prototype_ids": registry.prototype_ids,
            }
        )

    async def _fetch_prototype_with_retry(self, session, consul_url, token):
        for _ in range(8):
            fetched_data = await fetch_prototype(
                session=session,
                consul_url=consul_url,
                token=token,
            )
            if fetched_data:
                return True, ext_dict("fetch_prototype data", fetched_data)
            await asyncio.sleep(0.25)
        return False, {}

    def _make_session(self):
        out_limit = self.config.server.outbound_concurrency
        if out_limit > 0:
            conn_limit = max(out_limit * 4, 256)
            per_host = max(out_limit * 2, 128)
        else:
            conn_limit = 0
            per_host = 0
        return aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(
                limit=conn_limit,
                limit_per_host=per_host,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
            ),
            trust_env=False,
        )

    async def _lifecycle_manager(self, app):
        poller_gen = None
        lc = app.get("local_conductor")
        if lc is not None and app.get("_local_conductor_poller") is None:
            poller_gen = lc.poller_ctx(app)
            await poller_gen.asend(None)
            app["_local_conductor_poller"] = poller_gen

        try:
            self.logger.info("station initialized")
            yield
        finally:
            self.logger.info("station stopping")
            poller_gen = app.get("_local_conductor_poller")
            if poller_gen is not None:
                await poller_gen.aclose()
                app["_local_conductor_poller"] = None

            for instance in list(app["instances"].values()):
                try:
                    await instance.stop()
                except Exception as e:
                    self.logger.error("unexpected where=cleanup instance stop error=%s", e, exc_info=e)

            try:
                await app["gateway_processes"].close_all()
            except Exception as e:
                self.logger.error("unexpected where=cleanup gateway_processes error=%s", e, exc_info=e)

            try:
                await app["discord_voice"].close()
            except Exception as e:
                self.logger.error("unexpected where=cleanup discord_voice error=%s", e, exc_info=e)

            try:
                await app["db"].release_all_model_locks(app)
            except Exception as e:
                self.logger.error("unexpected where=cleanup release locks error=%s", e, exc_info=e)
            try:
                await app["db"].close()
            except Exception as e:
                self.logger.error("unexpected where=cleanup database error=%s", e, exc_info=e)

            try:
                await app["session"].close()
            except Exception as e:
                self.logger.error("unexpected where=cleanup session error=%s", e, exc_info=e)

            self.logger.info("station stopped")

    async def dedupe_cleanup(self, app):
        db = app["db"]
        config = app["config"]
        ttl = config.server.request_dedupe_ttl_seconds
        interval = config.server.dedupe_cleanup_interval_seconds

        stop = asyncio.Event()

        async def run():
            while not stop.is_set():
                try:
                    await db.cleanup_requests(ttl)
                except Exception as e:
                    self.logger.error("unexpected where=dedupe cleanup error=%s", e, exc_info=e)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    continue

        task = asyncio.create_task(run())
        app["dedupe_cleanup_stop"] = stop
        app["dedupe_cleanup_task"] = task
        yield
        stop.set()
        try:
            await task
        except Exception as e:
            self.logger.error("unexpected where=dedupe cleanup shutdown error=%s", e, exc_info=e)

    async def _do_reload(self, app):
        try:
            result = reload_from_disk(app["config"])
            apply_reloaded_config(app, result)
            self.logger.info(
                "config reload applied=%s pending_restart=%s unchanged=%s",
                result["applied"],
                result["pending_restart"],
                result["unchanged"],
            )
        except Exception as e:
            self.logger.error("unexpected where=sighup reload error=%s", e, exc_info=e)

    async def _register_sighup(self, app):
        loop = asyncio.get_running_loop()

        def _hup():
            asyncio.create_task(self._do_reload(app))

        try:
            loop.add_signal_handler(signal.SIGHUP, _hup)
        except (NotImplementedError, RuntimeError) as e:
            self.logger.debug("SIGHUP unsupported, skipping reload handler error=%s", e)

    async def init(self):
        out_limit = self.config.server.outbound_concurrency
        configure_outbound_concurrency(out_limit)
        apply_retry_config(self.config.server)
        session = None
        try:
            fs_root = self.config.fs_root
            if not fs_root:
                raise ValueError("FS_ROOT is required")
            os.makedirs(fs_root, exist_ok=True)

            consul_url = self.config.consul_url
            if not consul_url:
                raise ValueError("CONSUL_URL is required (env or config.toml).")

            primary_cfg = self.config.prototype
            primary_token = primary_cfg.token
            if not primary_token:
                raise ValueError("prototypes[0].token is required")

            session = self._make_session()
            primary_fetched_ok, primary_fetched = await self._fetch_prototype_with_retry(
                session, consul_url, primary_token
            )
            if not primary_fetched_ok:
                raise RuntimeError("fetch_prototype failed (consul_url=%s)" % consul_url)

            if primary_fetched.get("prototype_id") is not None:
                self.prototype_id = ext_int("prototype_id", primary_fetched["prototype_id"])
                primary_cfg.id = self.prototype_id

            prototype_access_point = ""
            if "access_point" in primary_fetched:
                prototype_access_point = ext_str("access_point", primary_fetched["access_point"], default="")

            if not self.config.host or not self.config.port:
                if not prototype_access_point:
                    raise TypeError("Prototype access_point from Consul must be a non-empty string.")

                host, port = self.config._parse_access_point_host_port(prototype_access_point)
                if not host or not port:
                    raise RuntimeError(f"Invalid prototype access_point: {prototype_access_point}")

                self.config.host = host
                self.config.port = port

            if not self.config.host or not self.config.port:
                raise RuntimeError(
                    "Station listen address not resolved. Set SERVER_HOST/SERVER_PORT (env or config.toml), or pass --host/--port."
                )

            if "is_local" not in primary_fetched:
                raise TypeError("fetch_prototype is_local is required")
            primary_is_local = ext_bool("is_local", primary_fetched["is_local"])

            self.recorder = Recorder("station", self.config.recorder, port=self.config.port)
            self.logger = self.recorder.logger

            self.app = web.Application(
                middlewares=[
                    self.recorder.error_middleware,
                    self.recorder.middleware,
                    self.recorder.tracing_middleware,
                ]
            )
            self.app["_station"] = self
            self.app["needs_manual"] = False
            self.app.on_startup.append(self._register_sighup)
            self.app["_prototype_access_point"] = prototype_access_point
            self.app["env"] = self.config.env
            self.app["recorder"] = self.recorder
            self.app["logger"] = self.logger
            self.app["session"] = session
            self.app["fs_root"] = self.config.fs_root
            self.app["prototype_config_file"] = self.config.prototype.config_file
            self.app["prototype_secret_file"] = self.config.prototype.secret_file
            self.app["webrtc"] = WebRTCService(config=self.config, session=session)
            self.app["discord_voice"] = DiscordVoiceService(session=session)
            self.app["config"] = self.config

            wipe_on_restart = self.config.database.wipe_on_restart
            if wipe_on_restart:
                wipe_station_runtime_data(
                    db_path=self.config.database.path,
                    fs_root=fs_root,
                )
            self.db = await create_database(
                backend=self.config.database.backend,
                db_path=self.config.database.path,
                dsn=self.config.database.dsn,
                enable_local_conductor=True,
                wipe_on_restart=wipe_on_restart,
            )
            self.app["db"] = self.db

            registry = TenantRegistry()
            for idx, cfg in enumerate(self.config.hosted_prototypes):
                is_primary = idx == 0
                pid = cfg.id
                token = cfg.token
                kind = cfg.kind
                if is_primary:
                    fetched = primary_fetched
                    is_local = primary_is_local
                else:
                    fetched_ok, fetched = await self._fetch_prototype_with_retry(session, consul_url, token)
                    if not fetched_ok:
                        raise RuntimeError("fetch_prototype failed for prototype_id=%s" % pid)
                    if fetched.get("prototype_id") is not None:
                        pid = ext_int("prototype_id", fetched["prototype_id"])
                        cfg.id = pid
                    if "is_local" not in fetched:
                        raise TypeError("fetch_prototype is_local is required")
                    is_local = ext_bool("is_local", fetched["is_local"])

                token_state = TokenState(token)
                proto_version = 0
                proto_type = ""
                stored = await self.db.get_prototype_info(pid)
                if stored:
                    if stored.get("version") is not None:
                        proto_version = stored["version"]
                    if stored.get("type") is not None:
                        proto_type = stored["type"]
                client_context = ClientContext(
                    session=session,
                    consul_url=consul_url,
                    env=self.config.env,
                    prototype_id=pid,
                    token=token_state.token,
                    prototype_version=proto_version,
                    prototype_type=proto_type,
                    db=self.db,
                    app=self.app,
                )
                update_client_context_prototype(client_context, fetched)
                await self.db.put_prototype(fetched)

                tenant = Tenant(
                    id=pid,
                    kind=kind,
                    token_state=token_state,
                    client_context=client_context,
                    config_file=cfg.config_file,
                    secret_file=cfg.secret_file,
                    ava=cfg.ava,
                    reply_to=cfg.reply_to,
                    is_local=is_local,
                )
                if is_primary and self._primary_prototype_class is not None:
                    tenant._prototype_class = self._primary_prototype_class
                registry.add(tenant, primary=is_primary)

            primary_tenant = registry.primary
            self.prototype_id = primary_tenant.id
            primary_cfg = self.config.prototype
            primary_cfg.id = primary_tenant.id
            primary_cfg.token = primary_tenant.token
            primary_cfg.kind = primary_tenant.kind

            self.app["tenants"] = registry
            self.app["prototype_is_local"] = registry.any_local()
            self.app["local_conductor"] = None
            self.app["_local_conductor_routes"] = False
            ensure_local_conductor(self.app)
            self.app["instances"] = {}
            self.app["instance_lock"] = asyncio.Lock()
            self.app["instance_runtime"] = {}
            self.app["gateway_processes"] = GatewayProcessRegistry()

            Routes(self.app)
            setup_recorder_routes(self.app)
            self.app.router.add_get("/healthz", self.handle_health)

            self.app.cleanup_ctx.append(self._lifecycle_manager)
            self.app.cleanup_ctx.append(self.dedupe_cleanup)
            session = None
            return self.app
        except Exception as e:
            logger.error("startup failed error=%s", e, exc_info=e)
            if session is not None:
                try:
                    await session.close()
                except Exception as close_err:
                    logger.error(
                        "unexpected where=startup session close error=%s",
                        close_err,
                        exc_info=close_err,
                    )
            raise

    def start(self):
        try:
            import uvloop

            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        except ImportError:
            logger.debug("uvloop not installed, using default asyncio event loop")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app = loop.run_until_complete(self.init())
        backlog = self.config.server.listen_backlog
        if backlog < 128:
            backlog = 128
        self.logger.info(
            "station starting host=%s port=%s env=%s",
            self.config.host,
            self.config.port,
            self.config.env,
        )
        web.run_app(
            app,
            host=self.config.host,
            port=self.config.port,
            loop=loop,
            backlog=backlog,
            access_log=None,
        )
