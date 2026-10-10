"""Keep the client-facing HTTP contract stable while the worker runs in Python."""

from contextlib import asynccontextmanager
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .config import ServiceConfig, load_config
from .models import HttpError, equal_secret
from .runtime import LoginRuntime


def create_app(
    runtime: LoginRuntime | None = None, *, config: ServiceConfig | None = None
) -> FastAPI:
    runtime = runtime or LoginRuntime(config or load_config())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime

    @app.middleware("http")
    async def protect_internal(request: Request, call_next):
        if request.url.path.startswith("/internal/") and not equal_secret(
            request.headers.get("x-service-token"), runtime.config.service_token
        ):
            response = JSONResponse({"error": "Unauthorized"}, status_code=401)
        else:
            try:
                response = await call_next(request)
            except Exception:
                # Do not return SDK messages, file paths or credential-bearing errors.
                response = JSONResponse({"error": "Internal service error"}, status_code=500)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(HttpError)
    async def handle_runtime_error(_request: Request, error: HttpError):
        return JSONResponse({"error": str(error)}, status_code=error.status)

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "simulation": True,
            "storage": "oss",
            **runtime.stats,
        }

    @app.post("/internal/tasks")
    async def submit(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                return JSONResponse({"error": "Request body is too large"}, status_code=413)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        task_id = runtime.submit(payload)
        return JSONResponse({"id": task_id, "status": "queued"}, status_code=202)

    @app.get("/internal/tasks/{task_id}")
    async def task_status(task_id: str, request: Request):
        task = runtime.get_task(task_id, request.headers.get("x-session-token"))
        if task is None:
            return JSONResponse({"error": "Task not found"}, status_code=404)
        return task

    @app.api_route("/internal/objects/{task_id}/qr.png", methods=["HEAD", "GET"])
    async def read_object(task_id: str, request: Request):
        if not runtime.object_available(task_id):
            return Response(status_code=404)
        storage = runtime.storage
        obj = await (storage.head(task_id.lower()) if request.method == "HEAD" else storage.get(task_id.lower()))
        if obj is None or not runtime.object_available(task_id):
            return Response(status_code=404)
        headers = {"X-Expires-At": obj.expires_at, "Content-Length": str(obj.content_length)}
        return Response(
            content=b"" if request.method == "HEAD" else obj.body,
            media_type=obj.content_type,
            headers=headers,
        )

    return app
