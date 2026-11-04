"""Control, outbox, and recovery triggers for the PTU batch engine."""

import azure.functions as func

from ptu_batch.functions_support import (
    change_feed_documents,
    get_engine,
    parse_service_bus_json,
)

app = func.FunctionApp()


@app.function_name(name="control_queue")
@app.service_bus_queue_trigger(
    arg_name="message",
    queue_name="%PTU_BATCH_CONTROL_QUEUE%",
    connection="ServiceBusConnection",
)
async def control_queue(message: func.ServiceBusMessage) -> None:
    payload = parse_service_bus_json(message)
    engine = await get_engine()
    await engine.handle_control(payload)


@app.function_name(name="outbox_change_feed")
@app.cosmos_db_trigger(
    arg_name="documents",
    database_name="%PTU_BATCH_COSMOS_DATABASE%",
    container_name="%PTU_BATCH_COSMOS_CONTAINER%",
    connection="CosmosConnection",
    lease_container_name="%PTU_BATCH_COSMOS_LEASE_CONTAINER%",
    create_lease_container_if_not_exists=False,
)
async def outbox_change_feed(documents: func.DocumentList) -> None:
    records = change_feed_documents(documents)
    engine = await get_engine()
    await engine.publish_outbox(records)


@app.function_name(name="maintenance_timer")
@app.timer_trigger(
    arg_name="timer",
    schedule="0 */5 * * * *",
    use_monitor=True,
    run_on_startup=False,
)
async def maintenance_timer(timer: func.TimerRequest) -> None:
    engine = await get_engine()
    await engine.recover()
