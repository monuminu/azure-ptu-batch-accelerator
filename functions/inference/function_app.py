"""Single-message inference trigger for the PTU batch engine."""

import azure.functions as func

from ptu_batch.functions_support import get_engine, parse_service_bus_json

app = func.FunctionApp()


@app.function_name(name="inference_queue")
@app.service_bus_queue_trigger(
    arg_name="message",
    queue_name="%PTU_BATCH_INFERENCE_QUEUE%",
    connection="ServiceBusConnection",
)
async def inference_queue(message: func.ServiceBusMessage) -> None:
    payload = parse_service_bus_json(message)
    engine = await get_engine()
    await engine.handle_inference(payload)
