"""OpenAI-compatible HTTP surface. Long-running inference never runs in an API request."""

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager, suppress
from urllib.parse import quote

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException

from .bootstrap import build_runtime
from .engine import Engine, file_view
from .errors import AuthError, BatchError
from .schemas import BatchList, BatchObject, FileDeleted, FileList, FileObject, install_openapi

log = logging.getLogger(__name__)
security = HTTPBearer(auto_error=False)
security_dependency = Depends(security)


async def _demo_pump(runtime, engine):
    while True:
        try:
            await engine.publish_outbox()
            message = runtime.broker.pop_ready(runtime.now())
            if message:
                handler = (
                    engine.handle_inference
                    if message["queue"] == runtime.settings.inference_queue
                    else engine.handle_control
                )
                await handler(message["payload"])
            else:
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Local demo pipeline error", extra={"exception_type": type(exc).__name__})
            await asyncio.sleep(1)
            await engine.recover()


class UploadLimitMiddleware:
    def __init__(self, app, limit):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") not in ("/openai/v1/files", "/openai/v1/batches")
        ):
            await self.app(scope, receive, send)
            return
        limit = self.limit() if scope["path"].endswith("/files") else 65536
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = limit + 1
        if declared > limit:
            await JSONResponse(
                BatchError(413, "Request body exceeds upload limit", "file_too_large").envelope(),
                status_code=413,
            )(scope, receive, send)
            return
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BatchError(413, "Request body exceeds upload limit", "file_too_large")
            return message

        await self.app(scope, bounded_receive, send)


def create_app(runtime=None):
    @asynccontextmanager
    async def lifespan(app):
        rt = runtime or await build_runtime()
        engine = Engine(rt)
        app.state.runtime, app.state.engine = rt, engine
        pump = None
        if rt.settings.mode == "local" and rt.settings.local_autorun:
            pump = asyncio.create_task(_demo_pump(rt, engine))
        try:
            yield
        finally:
            if pump:
                pump.cancel()
                with suppress(asyncio.CancelledError):
                    await pump
            await rt.close()

    app = FastAPI(
        title="Azure PTU Batch Accelerator",
        version="0.1.0",
        lifespan=lifespan,
        description="Files and Batch API compatibility over existing Azure OpenAI PTU inference.",
    )
    app.add_middleware(
        UploadLimitMiddleware,
        limit=lambda: (
            (
                getattr(app.state, "runtime", runtime).settings.max_file_bytes
                if getattr(app.state, "runtime", runtime)
                else 200 * 1024 * 1024
            )
            + 1048576
        ),
    )

    @app.exception_handler(BatchError)
    async def batch_error(request, exc):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(exc.envelope(), status_code=exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def request_error(request, exc):
        first = exc.errors()[0]
        param = ".".join(str(x) for x in first["loc"] if x not in ("body", "query"))
        return JSONResponse(
            BatchError(400, "Invalid request parameters", param=param or None).envelope(),
            status_code=400,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse(
            BatchError(exc.status_code, "HTTP request could not be processed").envelope(),
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        log.error("API dependency failure", extra={"exception_type": type(exc).__name__})
        return JSONResponse(
            BatchError(
                500,
                "The batch service encountered an internal error",
                "internal_error",
                error_type="server_error",
            ).envelope(),
            status_code=500,
        )

    @app.middleware("http")
    async def correlation(request, call_next):
        response = await call_next(request)
        response.headers["x-request-id"] = "req_" + uuid.uuid4().hex
        return response

    async def principal(
        request: Request, credentials: HTTPAuthorizationCredentials | None = security_dependency
    ):
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise AuthError("A bearer token is required")
        return await request.app.state.runtime.auth.authenticate(credentials.credentials)

    caller_dependency = Depends(principal)

    def engine(request):
        return request.app.state.engine

    @app.get("/healthz", include_in_schema=False)
    async def health():
        return {"status": "ok"}

    @app.post("/openai/v1/files", operation_id="create_file", response_model=FileObject)
    async def upload(request: Request, caller=caller_dependency):
        try:
            async with request.form(max_files=1, max_fields=8, max_part_size=4096) as form:
                file = form.get("file")
                if not isinstance(file, UploadFile):
                    raise BatchError(400, "A multipart file is required", param="file")
                unknown = set(form) - {
                    "file",
                    "purpose",
                    "expires_after",
                    "expires_after[anchor]",
                    "expires_after[seconds]",
                }
                if unknown:
                    raise BatchError(400, "Unsupported upload parameter", param=sorted(unknown)[0])
                expiry = None
                if "expires_after" in form:
                    expiry = json.loads(str(form["expires_after"]))
                elif "expires_after[seconds]" in form or "expires_after[anchor]" in form:
                    expiry = {
                        "anchor": str(form.get("expires_after[anchor]", "")),
                        "seconds": int(str(form.get("expires_after[seconds]", "0"))),
                    }

                async def chunks():
                    while data := await file.read(65536):
                        yield data

                document = await engine(request).upload_file(
                    caller,
                    file.filename or "input.jsonl",
                    chunks(),
                    purpose=str(form.get("purpose", "")),
                    expires_after=expiry,
                )
                return file_view(document)
        except (json.JSONDecodeError, ValueError) as exc:
            raise BatchError(400, "Invalid multipart upload parameters") from exc

    @app.get("/openai/v1/files", operation_id="list_files", response_model=FileList)
    async def list_files(
        request: Request,
        limit: int = Query(10000, ge=1, le=10000),
        after: str | None = None,
        order: str = "desc",
        purpose: str | None = None,
        caller=caller_dependency,
    ):
        return await engine(request).list_objects(
            caller, "file", limit=limit, after=after, order=order, purpose=purpose
        )

    @app.get("/openai/v1/files/{file_id}", operation_id="retrieve_file", response_model=FileObject)
    async def get_file(file_id: str, request: Request, caller=caller_dependency):
        return file_view(await engine(request).get_file(caller, file_id))

    @app.delete(
        "/openai/v1/files/{file_id}", operation_id="delete_file", response_model=FileDeleted
    )
    async def delete_file(file_id: str, request: Request, caller=caller_dependency):
        return await engine(request).delete_file(caller, file_id)

    @app.get("/openai/v1/files/{file_id}/content", operation_id="retrieve_file_content")
    async def content(file_id: str, request: Request, caller=caller_dependency):
        file = await engine(request).get_file(caller, file_id)
        return StreamingResponse(
            request.app.state.runtime.blobs.read(file["blob_name"]),
            media_type="application/jsonl",
            headers={
                "Content-Length": str(file["bytes"]),
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(file['filename'], safe='')}",
            },
        )

    @app.post("/openai/v1/batches", operation_id="create_batch", response_model=BatchObject)
    async def create_batch(request: Request, caller=caller_dependency):
        try:

            def nonfinite(value):
                raise ValueError("Invalid JSON number")

            body = json.loads(await request.body(), parse_constant=nonfinite)
        except (ValueError, UnicodeError) as exc:
            raise BatchError(400, "Request body must be a JSON object") from exc
        if not isinstance(body, dict):
            raise BatchError(400, "Request body must be a JSON object")
        return await engine(request).create_batch(caller, body)

    @app.get("/openai/v1/batches", operation_id="list_batches", response_model=BatchList)
    async def list_batches(
        request: Request,
        limit: int = Query(20, ge=1, le=100),
        after: str | None = None,
        caller=caller_dependency,
    ):
        return await engine(request).list_objects(caller, "batch", limit=limit, after=after)

    @app.get(
        "/openai/v1/batches/{batch_id}", operation_id="retrieve_batch", response_model=BatchObject
    )
    async def get_batch(batch_id: str, request: Request, caller=caller_dependency):
        return await engine(request).get_batch(caller, batch_id)

    @app.post(
        "/openai/v1/batches/{batch_id}/cancel",
        operation_id="cancel_batch",
        response_model=BatchObject,
    )
    async def cancel_batch(batch_id: str, request: Request, caller=caller_dependency):
        return await engine(request).cancel_batch(caller, batch_id)

    install_openapi(app)
    return app


app = create_app()
