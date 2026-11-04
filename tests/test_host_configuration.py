"""Functions host configuration required by the deployed Python trigger apps."""

import json
from pathlib import Path

import pytest


@pytest.mark.parametrize(("service", "max_concurrent_calls"), [("control", 1), ("inference", 8)])
def test_host_enables_service_bus_and_cosmos_extension_bundle(service, max_concurrent_calls):
    host_path = Path(__file__).resolve().parents[1] / "functions" / service / "host.json"
    host = json.loads(host_path.read_text(encoding="utf-8"))

    assert host["version"] == "2.0"
    assert host["extensionBundle"] == {
        "id": "Microsoft.Azure.Functions.ExtensionBundle",
        "version": "[4.0.0, 5.0.0)",
    }
    assert host["functionTimeout"] == "00:10:00"
    assert host["concurrency"]["dynamicConcurrencyEnabled"] is False
    assert host["extensions"]["serviceBus"] == {
        "maxConcurrentCalls": max_concurrent_calls,
        "prefetchCount": 0,
        "autoCompleteMessages": True,
        "maxAutoLockRenewalDuration": "00:15:00",
    }
