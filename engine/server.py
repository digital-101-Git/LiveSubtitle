"""Loopback-only authenticated API. Run: python engine/server.py --root APP_ROOT."""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# The Windows embedded Python distribution omits the script directory from sys.path.
APP_DIRECTORY = Path(__file__).resolve().parent.parent
if str(APP_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(APP_DIRECTORY))

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from engine.models import import_gguf, inventory
from engine.history import CaptionHistory
from engine.http_guard import HTTPGuard
from engine.runtime import Runtime, configure_cuda
from engine.sessions import SessionManager, StreamSession
from engine.settings import EngineError, Settings, normalize_language, normalize_target_language


def origin_allowed(origin: str | None) -> bool:
    return origin is None or re.fullmatch(r"chrome-extension://[a-p]{32}", origin) is not None


class TranslateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=6000)
    source_language: str = Field(default="auto", max_length=32)
    target_language: str | None = Field(default=None, max_length=32)


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)


def create_app(root: Path, runtime=None) -> FastAPI:
    root = root.resolve()
    configure_cuda(root)
    settings = Settings(root)
    runtime = runtime or Runtime(root)
    manager = SessionManager(CaptionHistory(root))
    operation = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        await manager.stop()
        await runtime.release()

    app = FastAPI(title="LiveSubtitle Engine", version="1.0.1", docs_url=None,
                  redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings, app.state.runtime, app.state.sessions = settings, runtime, manager
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    app.add_middleware(HTTPGuard, token=settings.token, allowed_origin=origin_allowed)
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"chrome-extension://[a-p]{32}",
                       allow_methods=["GET", "PUT", "POST"], allow_headers=["Authorization", "Content-Type"])

    @app.exception_handler(EngineError)
    async def engine_error(request: Request, exc: EngineError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    async def authenticated(request: Request):
        authorization = request.headers.get("authorization", "")
        if not origin_allowed(request.headers.get("origin")):
            raise HTTPException(403, "허용되지 않은 브라우저 출처입니다.")
        expected = "Bearer " + settings.token
        if not hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(401, "엔진 인증이 필요합니다.")

    def status() -> dict:
        return {**runtime.status(), "active_session": manager.active.id if manager.active else None}

    def ensure_idle():
        if manager.active:
            raise EngineError("session_busy", "자막을 중지한 다음 설정이나 모델을 변경해 주세요.", 409)

    @app.get("/health")
    async def health():
        return {"ok": True, "service": "LiveSubtitle", "version": "1.0.1"}

    @app.get("/v1/settings", dependencies=[Depends(authenticated)])
    async def get_settings():
        return {**settings.public(), "status": status()}

    @app.put("/v1/settings", dependencies=[Depends(authenticated)])
    async def put_settings(patch: dict):
        async with operation:
            ensure_idle()
            settings.update(patch)
            return {**settings.public(), "status": status()}

    @app.get("/v1/models", dependencies=[Depends(authenticated)])
    async def get_models():
        return await asyncio.to_thread(inventory, root)

    @app.post("/v1/models/import", dependencies=[Depends(authenticated)])
    async def import_model(body: ImportRequest, request: Request):
        # Importing arbitrary filesystem paths belongs to the native authenticated UI.
        if request.headers.get("origin") is not None:
            raise HTTPException(403, "모델 가져오기는 Windows 앱에서 실행해 주세요.")
        async with operation:
            ensure_idle()
            try:
                return await asyncio.to_thread(import_gguf, root, body.path)
            except (OSError, ValueError) as exc:
                raise EngineError("import_failed", "파일을 가져올 수 없습니다. 파일 경로와 디스크 여유 공간을 확인해 주세요.") from exc

    @app.post("/v1/prepare", dependencies=[Depends(authenticated)])
    async def prepare():
        async with operation:
            ensure_idle()
            if settings.data["mode"] == "gemini":
                settings.key()
            await runtime.prepare(settings.data)
            return {"ok": True, "ready": True, "status": status()}

    @app.post("/v1/release", dependencies=[Depends(authenticated)])
    async def release():
        async with operation:
            await manager.stop()
            await runtime.release()
            return {"ok": True, "status": status()}

    @app.post("/v1/translate", dependencies=[Depends(authenticated)])
    async def translate(body: TranslateRequest):
        if not body.text.strip():
            raise EngineError("empty_text", "번역할 문장을 입력해 주세요.")
        source = normalize_language(body.source_language)
        target = normalize_target_language(body.target_language if "target_language" in body.model_fields_set
                                           else settings.data["target_language"])
        translation = await runtime.translate(body.text.strip(), source, target_language=target)
        return {"translation": translation, "target_language": target}

    @app.post("/v1/logs/clear", dependencies=[Depends(authenticated)])
    async def clear_logs(request: Request):
        if request.headers.get("origin") is not None:
            raise HTTPException(403, "번역 기록 삭제는 Windows 앱에서 실행해 주세요.")
        async with operation:
            return await manager.clear_logs(runtime.log_lock)

    @app.websocket("/v1/stream")
    async def stream(websocket: WebSocket):
        if not origin_allowed(websocket.headers.get("origin")):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        session = None
        try:
            # The first and only pre-authentication message must be small JSON auth.
            first = await asyncio.wait_for(websocket.receive(), 10)
            raw = first.get("text")
            if not isinstance(raw, str) or len(raw) > 2048:
                await websocket.close(code=1008)
                return
            try:
                authentication = json.loads(raw)
            except ValueError:
                await websocket.close(code=1008)
                return
            if (not isinstance(authentication, dict) or authentication.get("type") != "auth"
                    or not isinstance(authentication.get("token"), str)
                    or not hmac.compare_digest(authentication["token"].encode("utf-8"), settings.token.encode("utf-8"))):
                await websocket.close(code=1008)
                return
            while True:
                incoming = await websocket.receive()
                if incoming["type"] == "websocket.disconnect":
                    break
                if incoming.get("bytes") is not None:
                    if session is None or not session.active:
                        raise EngineError("not_started", "오디오 전송 전에 자막 세션을 시작해 주세요.", 409)
                    await session.feed(incoming["bytes"])
                    continue
                text = incoming.get("text", "")
                if len(text) > 4096:
                    raise EngineError("invalid_message", "제어 메시지가 너무 큽니다.")
                try:
                    message = json.loads(text)
                except (TypeError, ValueError) as exc:
                    raise EngineError("invalid_message", "올바른 JSON 제어 메시지가 필요합니다.") from exc
                if not isinstance(message, dict):
                    raise EngineError("invalid_message", "제어 메시지는 JSON 객체여야 합니다.")
                if message.get("type") == "start":
                    if session is not None and session.active:
                        raise EngineError("already_started", "이미 시작된 세션입니다.", 409)
                    mode = message.get("mode", settings.data["mode"])
                    language = normalize_language(message.get("language", settings.data["language"]))
                    target_language = normalize_target_language(message.get("target_language", settings.data["target_language"]))
                    if mode not in ("local", "gemini"):
                        raise EngineError("invalid_settings", "음성 인식 모드나 언어가 올바르지 않습니다.")
                    async with operation:
                        session = StreamSession(websocket, runtime, manager, settings, mode, language,
                                                target_language=target_language)
                        await session.start()
                elif message.get("type") == "audio_gap":
                    if session is None or not session.active:
                        raise EngineError("not_started", "오디오 전송 전에 자막 세션을 시작해 주세요.", 409)
                    session.input_gap(message.get("dropped_samples"))
                elif message.get("type") == "stop":
                    if session and session.active:
                        await session.stop()
                    else:
                        await websocket.send_json({"type": "stopped"})
                else:
                    raise EngineError("invalid_message", "지원하지 않는 제어 메시지입니다.")
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        except EngineError as exc:
            try:
                await websocket.send_json({"type": "error", "code": exc.code, "message": exc.message})
                await websocket.close(code=1011 if exc.status >= 500 else 1008)
            except Exception:
                pass
        except Exception:
            # Never serialize traceback/provider exception text: it may include an API key.
            try:
                await websocket.send_json({"type": "error", "code": "internal_error",
                                           "message": "엔진 오류가 발생했습니다. 자막을 다시 시작해 주세요."})
                await websocket.close(code=1011)
            except Exception:
                pass
        finally:
            if session:
                await session.stop(notify=False)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="LiveSubtitle loopback engine")
    parser.add_argument("--root", type=Path, default=APP_DIRECTORY)
    parser.add_argument("--port", type=int, default=17865)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(create_app(args.root), host="127.0.0.1", port=args.port,
                access_log=False, log_level="warning", ws_max_size=65536,
                ws_max_queue=16, timeout_graceful_shutdown=15)


if __name__ == "__main__":
    main()
