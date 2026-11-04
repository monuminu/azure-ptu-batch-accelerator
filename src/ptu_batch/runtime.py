import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .ports import Authenticator, BlobStore, Broker, DocumentStore, InferenceClient, Preflight
from .settings import Settings


@dataclass
class Runtime:
    settings: Settings
    store: DocumentStore
    blobs: BlobStore
    broker: Broker
    inference: InferenceClient
    auth: Authenticator
    preflight: Preflight | None = None
    clock: Callable[[], int] = field(default=lambda: int(time.time()))
    cleanup: Callable | None = None
    credential: object | None = None
    _closed: bool = False

    def now(self):
        return int(self.clock())

    async def close(self):
        if self._closed:
            return
        self._closed = True
        await asyncio.gather(
            *(
                x.close()
                for x in [
                    self.store,
                    self.blobs,
                    self.broker,
                    self.inference,
                    self.auth,
                    self.preflight,
                ]
                if x
            ),
            return_exceptions=True,
        )
        if self.credential:
            await self.credential.close()
        if self.cleanup:
            self.cleanup()
