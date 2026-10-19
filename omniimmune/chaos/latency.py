from __future__ import annotations

import asyncio
import random
import time

import httpx

from omniimmune.models import AttackDNA, AttackType

from .common import response_body


class LatencyProxy:
    def __init__(
        self,
        upstream_host: str,
        upstream_port: int,
        listen_port: int = 0,
        delay_seconds: float = 0.0,
        drop_rate: float = 0.0,
        seed: int = 1337,
        listen_host: str = "127.0.0.1",
    ) -> None:
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.listen_port = listen_port
        self.listen_host = listen_host
        self.delay_seconds = delay_seconds
        self.drop_rate = drop_rate
        self._rng = random.Random(seed)
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._connection_tasks: set[asyncio.Task] = set()
        self._pipe_tasks: set[asyncio.Task] = set()
        self.port = listen_port

    def _should_drop(self) -> bool:
        return self._rng.random() < self.drop_rate

    def set_chaos(self, delay_seconds: float, drop_rate: float) -> None:
        self.delay_seconds = delay_seconds
        self.drop_rate = drop_rate

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._accept,
            host=self.listen_host,
            port=self.listen_port,
        )
        socket = self._server.sockets[0]
        self.port = int(socket.getsockname()[1])

    def _track_task(self, task: asyncio.Task, collection: set[asyncio.Task]) -> None:
        collection.add(task)
        task.add_done_callback(collection.discard)

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        task = asyncio.create_task(self._handle_client(reader, writer))
        self._track_task(task, self._connection_tasks)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        self._writers.add(writer)
        try:
            if self._should_drop():
                return
            upstream_reader, upstream_writer = await asyncio.open_connection(
                self.upstream_host, self.upstream_port
            )
            self._writers.add(upstream_writer)
            client_to_upstream = asyncio.create_task(
                self._copy(reader, upstream_writer, delay_client_chunks=True)
            )
            upstream_to_client = asyncio.create_task(
                self._copy(upstream_reader, writer, delay_client_chunks=False)
            )
            self._track_task(client_to_upstream, self._pipe_tasks)
            self._track_task(upstream_to_client, self._pipe_tasks)
            done, pending = await asyncio.wait(
                (client_to_upstream, upstream_to_client),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.gather(*done, return_exceptions=True)
        except (ConnectionError, OSError, asyncio.CancelledError):
            if asyncio.current_task().cancelling():
                raise
        finally:
            self._writers.discard(writer)
            if upstream_writer is not None:
                self._writers.discard(upstream_writer)
                upstream_writer.close()
                try:
                    await upstream_writer.wait_closed()
                except OSError:
                    pass
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _copy(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        delay_client_chunks: bool,
    ) -> None:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                return
            if delay_client_chunks and self.delay_seconds > 0:
                await asyncio.sleep(self.delay_seconds)
            writer.write(chunk)
            await writer.drain()

    async def stop(self) -> None:
        server = self._server
        if server is None:
            return
        self._server = None
        server.close()
        for writer in tuple(self._writers):
            writer.close()
            transport = getattr(writer, "transport", None)
            if transport is not None:
                transport.abort()
        tasks = tuple(self._connection_tasks | self._pipe_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await server.wait_closed()
        self._writers.clear()
        self._connection_tasks.clear()
        self._pipe_tasks.clear()


class LatencyDriftAgent:
    def __init__(self, seed: int = 1337) -> None:
        self.seed = seed

    def build_dna(self, delay_seconds: float = 4.0) -> AttackDNA:
        return AttackDNA(
            attack_type=AttackType.LATENCY,
            target_endpoint="/api/v1/payment/verify",
            http_method="GET",
            delay_seconds=delay_seconds,
            query_params={"delay": str(delay_seconds)},
            seed=self.seed,
        )

    async def execute(
        self,
        target_url: str,
        dna: AttackDNA,
        client: httpx.AsyncClient | None = None,
        client_timeout: float = 2.0,
        sla_ms: float = 1500.0,
    ) -> dict:
        owned_client = client is None
        active_client = client if client is not None else httpx.AsyncClient()
        started = time.perf_counter()
        try:
            url = f"{target_url.rstrip('/')}{dna.target_endpoint}"
            response = await asyncio.wait_for(
                active_client.get(url, params=dna.query_params, headers=dna.headers),
                timeout=client_timeout,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
            status_code = int(response.status_code)
            server_failure = status_code >= 500
            sla_breach = elapsed_ms > sla_ms
            return {
                "status_code": status_code,
                "response_body": response_body(response),
                "elapsed_ms": elapsed_ms,
                "is_failure": server_failure or sla_breach,
                "failure_kind": "exception" if server_failure else "hang" if sla_breach else None,
                "error": (
                    f"latency {elapsed_ms:.1f}ms exceeded SLA {sla_ms:.1f}ms"
                    if sla_breach and not server_failure
                    else "upstream returned an error" if server_failure else None
                ),
            }
        except asyncio.TimeoutError:
            return {
                "status_code": 0,
                "response_body": "",
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "is_failure": True,
                "failure_kind": "hang",
                "error": "request timed out",
            }
        except httpx.TimeoutException as exc:
            return {
                "status_code": 0,
                "response_body": "",
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "is_failure": True,
                "failure_kind": "hang",
                "error": str(exc) or exc.__class__.__name__,
            }
        except httpx.TransportError as exc:
            return {
                "status_code": 0,
                "response_body": "",
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "is_failure": True,
                "failure_kind": "crash",
                "error": str(exc) or exc.__class__.__name__,
            }
        finally:
            if owned_client:
                await active_client.aclose()

    async def run(
        self,
        target_url: str,
        client: httpx.AsyncClient | None = None,
        *,
        delay_seconds: float = 4.0,
        client_timeout: float = 2.0,
        sla_ms: float = 1500.0,
    ) -> tuple[AttackDNA, dict]:
        dna = self.build_dna(delay_seconds)
        return dna, await self.execute(
            target_url,
            dna,
            client=client,
            client_timeout=client_timeout,
            sla_ms=sla_ms,
        )
