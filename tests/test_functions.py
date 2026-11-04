"""Local contract tests for the two Azure Functions apps.

No Azure client is constructed by these tests. The app metadata assertions use
the real azure-functions decorator library when it is installed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class Message:
    def __init__(self, body: bytes):
        self.body = body

    def get_body(self) -> bytes:
        return self.body


class FunctionsSupportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        from ptu_batch.functions_support import close_runtime

        await close_runtime()

    async def test_service_bus_json_rejects_invalid_or_oversized_body_without_logging_content(self):
        from ptu_batch.functions_support import parse_service_bus_json

        secret = "prompt-should-never-be-logged"
        with self.assertLogs(level=logging.ERROR) as captured:
            logging.error("start capture")
            for body in (
                b"{",
                json.dumps({"prompt": secret}).encode() + b" ",
                b"x" * (1_048_576 + 1),
            ):
                if body.startswith(b"{") and body.endswith(b" "):
                    # This body is valid and stays below the documented limit.
                    self.assertEqual(parse_service_bus_json(Message(body)), {"prompt": secret})
                else:
                    with self.assertRaises(ValueError):
                        parse_service_bus_json(Message(body))
        self.assertNotIn(secret, "\n".join(captured.output))

    async def test_service_bus_json_requires_object(self):
        from ptu_batch.functions_support import parse_service_bus_json

        with self.assertRaises(ValueError):
            parse_service_bus_json(Message(b"[]"))

    async def test_service_bus_json_accepts_byte_buffers(self):
        from ptu_batch.functions_support import parse_service_bus_json

        self.assertEqual(
            parse_service_bus_json(Message(memoryview(b'{"kind":"validate"}'))),
            {"kind": "validate"},
        )

    async def test_change_feed_documents_become_dictionaries(self):
        from ptu_batch.functions_support import change_feed_documents

        class Document:
            def to_json(self):
                return '{"id":"outbox-1","kind":"outbox"}'

        self.assertEqual(
            change_feed_documents([Document(), {"id": "outbox-2", "kind": "other"}]),
            [
                {"id": "outbox-1", "kind": "outbox"},
                {"id": "outbox-2", "kind": "other"},
            ],
        )

    async def test_get_engine_builds_one_runtime_for_concurrent_invocations(self):
        from ptu_batch.functions_support import get_engine

        created = []

        class Runtime:
            async def close(self):
                created.append("closed")

        class Engine:
            def __init__(self, runtime):
                self.runtime = runtime

        async def build_runtime():
            created.append("built")
            await asyncio.sleep(0)
            return Runtime()

        with patch.dict(
            sys.modules,
            {
                "ptu_batch.bootstrap": types.SimpleNamespace(build_runtime=build_runtime),
                "ptu_batch.engine": types.SimpleNamespace(Engine=Engine),
            },
        ):
            first, second = await asyncio.gather(get_engine(), get_engine())
        self.assertIs(first, second)
        self.assertEqual(created, ["built"])

    async def test_failed_runtime_startup_is_not_cached(self):
        from ptu_batch.functions_support import get_engine

        calls = 0

        async def build_runtime():
            nonlocal calls
            calls += 1
            raise RuntimeError("configuration invalid")

        with patch.dict(
            sys.modules,
            {
                "ptu_batch.bootstrap": types.SimpleNamespace(build_runtime=build_runtime),
                "ptu_batch.engine": types.SimpleNamespace(Engine=object),
            },
        ):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "configuration invalid"):
                    await get_engine()
        self.assertEqual(calls, 2)

    async def test_cancelled_invocation_does_not_cancel_shared_startup(self):
        from ptu_batch.functions_support import get_engine

        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        class Runtime:
            async def close(self):
                pass

        class Engine:
            def __init__(self, runtime):
                self.runtime = runtime

        async def build_runtime():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return Runtime()

        with patch.dict(
            sys.modules,
            {
                "ptu_batch.bootstrap": types.SimpleNamespace(build_runtime=build_runtime),
                "ptu_batch.engine": types.SimpleNamespace(Engine=Engine),
            },
        ):
            cancelled = asyncio.create_task(get_engine())
            await started.wait()
            survivor = asyncio.create_task(get_engine())
            await asyncio.sleep(0)
            cancelled.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled
            release.set()
            self.assertIsInstance(await survivor, Engine)
        self.assertEqual(calls, 1)


try:
    import azure.functions  # noqa: F401
except ImportError:
    AZURE_FUNCTIONS_AVAILABLE = False
else:
    AZURE_FUNCTIONS_AVAILABLE = True


def load_app(which: str):
    path = ROOT / "functions" / which / "function_app.py"
    spec = importlib.util.spec_from_file_location(f"test_{which}_function_app", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(AZURE_FUNCTIONS_AVAILABLE, "azure-functions package is not installed")
class FunctionsEntrypointTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_control_message_does_not_initialize_runtime_or_log_body(self):
        import azure.functions as func

        module = load_app("control")
        secret = "private-prompt-file-token"
        logged = []

        class Capture(logging.Handler):
            def emit(self, record):
                logged.append(record.getMessage())

        async def get_engine():
            self.fail("invalid messages must be rejected before runtime startup")

        handler = Capture()
        logger = logging.getLogger()
        logger.addHandler(handler)
        try:
            with patch.object(module, "get_engine", get_engine):
                with self.assertRaises(ValueError) as error:
                    await module.control_queue(func.ServiceBusMessage(body=secret.encode()))
        finally:
            logger.removeHandler(handler)
        self.assertNotIn(secret, str(error.exception))
        self.assertNotIn(secret, "\n".join(logged))

    async def test_control_dispatch_awaits_engine_and_propagates_errors(self):
        import azure.functions as func

        module = load_app("control")
        calls = []

        class Engine:
            async def handle_control(self, payload):
                calls.append(payload)
                raise RuntimeError("transient")

        async def get_engine():
            return Engine()

        with patch.object(module, "get_engine", get_engine):
            with self.assertRaisesRegex(RuntimeError, "transient"):
                await module.control_queue(
                    func.ServiceBusMessage(body=b'{"kind":"validate","job_id":"j1"}')
                )
        self.assertEqual(calls, [{"kind": "validate", "job_id": "j1"}])

    async def test_inference_dispatch_awaits_engine(self):
        import azure.functions as func

        module = load_app("inference")
        calls = []

        class Engine:
            async def handle_inference(self, payload):
                await asyncio.sleep(0)
                calls.append(payload)

        async def get_engine():
            return Engine()

        with patch.object(module, "get_engine", get_engine):
            await module.inference_queue(
                func.ServiceBusMessage(body=b'{"kind":"infer","request_id":"r1"}')
            )
        self.assertEqual(calls, [{"kind": "infer", "request_id": "r1"}])

    async def test_change_feed_dispatches_documents_and_timer_awaits_recovery(self):
        import azure.functions as func

        module = load_app("control")
        calls = []

        class Engine:
            async def publish_outbox(self, documents=None):
                calls.append(("outbox", documents))

            async def recover(self):
                calls.append(("recover",))

        async def get_engine():
            return Engine()

        with patch.object(module, "get_engine", get_engine):
            await module.outbox_change_feed([func.Document({"id": "o1", "kind": "outbox"})])
            await module.maintenance_timer(object())
        self.assertEqual(calls, [("outbox", [{"id": "o1", "kind": "outbox"}]), ("recover",)])

    def test_registered_metadata_uses_expected_triggers_and_settings(self):
        control = load_app("control")
        inference = load_app("inference")
        control_functions = {f.get_function_name(): f for f in control.app.get_functions()}
        inference_functions = {f.get_function_name(): f for f in inference.app.get_functions()}

        self.assertEqual(
            set(control_functions), {"control_queue", "outbox_change_feed", "maintenance_timer"}
        )
        self.assertEqual(set(inference_functions), {"inference_queue"})

        def trigger(function):
            bindings = [b.get_dict_repr() for b in function.get_bindings()]
            self.assertEqual(len(bindings), 1)
            return bindings[0]

        control_bus = trigger(control_functions["control_queue"])
        self.assertEqual(control_bus["type"], "serviceBusTrigger")
        self.assertEqual(control_bus["queueName"], "%PTU_BATCH_CONTROL_QUEUE%")
        self.assertEqual(control_bus["connection"], "ServiceBusConnection")
        cosmos = trigger(control_functions["outbox_change_feed"])
        self.assertEqual(cosmos["type"], "cosmosDBTrigger")
        self.assertEqual(cosmos["databaseName"], "%PTU_BATCH_COSMOS_DATABASE%")
        self.assertEqual(cosmos["containerName"], "%PTU_BATCH_COSMOS_CONTAINER%")
        self.assertEqual(cosmos["leaseContainerName"], "%PTU_BATCH_COSMOS_LEASE_CONTAINER%")
        self.assertFalse(cosmos["createLeaseContainerIfNotExists"])
        timer = trigger(control_functions["maintenance_timer"])
        self.assertEqual(timer["type"], "timerTrigger")
        self.assertEqual(timer["schedule"], "0 */5 * * * *")
        self.assertTrue(timer["useMonitor"])
        self.assertFalse(timer["runOnStartup"])
        inference_bus = trigger(inference_functions["inference_queue"])
        self.assertEqual(inference_bus["type"], "serviceBusTrigger")
        self.assertEqual(inference_bus["queueName"], "%PTU_BATCH_INFERENCE_QUEUE%")


if __name__ == "__main__":
    unittest.main()
