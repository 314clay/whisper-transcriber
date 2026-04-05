"""Whisper Transcriber — real-time audio transcription microservice.

Connects to a questionnaire live-stream WebSocket, accumulates audio chunks,
and periodically sends them to OpenAI's Whisper API for transcription.
"""

import asyncio
import io
import json
import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# --- Config ---

PORT = int(os.environ.get("PORT", "3100"))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
WHISPER_API_URL = os.environ.get(
    "WHISPER_API_URL", "http://100.127.103.23:30800/v1/audio/transcriptions"
)
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "Systran/faster-whisper-large-v3")
CHUNK_INTERVAL = float(os.environ.get("CHUNK_INTERVAL", "5"))  # seconds between transcriptions
TRANSCRIPT_DIR = Path(os.environ.get("TRANSCRIPT_DIR", "transcripts"))
START_TIME = time.time()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("transcriber")

# --- State ---

# Active transcription sessions: ws_url -> session info
sessions: dict[str, dict] = {}

# Transcription results: session_id -> list of segments
transcripts: dict[str, list[dict]] = defaultdict(list)

# SSE listeners for live transcript updates
sse_listeners: dict[str, set[asyncio.Queue]] = defaultdict(set)


async def broadcast_transcript(session_id: str, segment: dict):
    message = f"event: transcript\ndata: {json.dumps(segment, default=str)}\n\n"
    dead = set()
    for queue in sse_listeners.get(session_id, set()):
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            dead.add(queue)
    if dead:
        sse_listeners[session_id] -= dead


# --- Whisper ---

async def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/webm") -> str | None:
    """Send audio to Whisper API (local GPU or OpenAI) and return transcription text."""
    ext = "webm" if "webm" in mime_type else "mp4" if "mp4" in mime_type else "ogg"

    headers = {}
    if OPENAI_API_KEY and "openai.com" in WHISPER_API_URL:
        headers["Authorization"] = f"Bearer {OPENAI_API_KEY}"

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            WHISPER_API_URL,
            headers=headers,
            files={"file": (f"audio.{ext}", audio_bytes, mime_type)},
            data={"model": WHISPER_MODEL, "response_format": "json"},
        )

    if resp.status_code != 200:
        log.error(f"Whisper API error {resp.status_code}: {resp.text}")
        return None

    return resp.json().get("text", "")


# --- WebSocket Consumer ---

async def stream_consumer(ws_url: str, session_id: str):
    """Connect to a questionnaire live-stream WS and transcribe audio chunks."""
    log.info(f"[{session_id}] Connecting to {ws_url}")
    audio_buffer = bytearray()
    mime_type = "audio/webm"
    last_flush = time.time()
    chunk_index = 0

    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        async with websockets.connect(ws_url) as ws:
            # Send session_start
            await ws.send(json.dumps({
                "type": "session_start",
                "port": session_id,
                "mime_type": mime_type,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }))

            sessions[session_id]["status"] = "connected"
            log.info(f"[{session_id}] Connected, accumulating audio...")

            async for message in ws:
                if isinstance(message, bytes):
                    audio_buffer.extend(message)

                    now = time.time()
                    if now - last_flush >= CHUNK_INTERVAL and len(audio_buffer) > 1000:
                        chunk_bytes = bytes(audio_buffer)
                        audio_buffer.clear()
                        last_flush = now
                        chunk_index += 1

                        # Transcribe in background to not block receiving
                        asyncio.create_task(
                            _process_chunk(session_id, chunk_bytes, mime_type, chunk_index)
                        )

                elif isinstance(message, str):
                    try:
                        msg = json.loads(message)
                        if msg.get("type") == "ack":
                            log.info(f"[{session_id}] Server ack: {msg.get('session_id')}")
                        elif msg.get("type") == "pong":
                            pass
                    except json.JSONDecodeError:
                        pass

    except websockets.ConnectionClosed:
        log.info(f"[{session_id}] WebSocket closed")
    except Exception as e:
        log.error(f"[{session_id}] Error: {e}")
    finally:
        # Flush remaining audio
        if len(audio_buffer) > 1000:
            chunk_index += 1
            await _process_chunk(session_id, bytes(audio_buffer), mime_type, chunk_index)

        sessions[session_id]["status"] = "disconnected"
        sessions[session_id]["ended_at"] = datetime.now(timezone.utc).isoformat()
        log.info(f"[{session_id}] Session ended, {chunk_index} chunks transcribed")


async def _process_chunk(session_id: str, audio_bytes: bytes, mime_type: str, chunk_index: int):
    """Transcribe a single audio chunk and store the result."""
    log.info(f"[{session_id}] Transcribing chunk {chunk_index} ({len(audio_bytes)} bytes)...")
    text = await transcribe_audio(audio_bytes, mime_type)

    if text:
        segment = {
            "chunk": chunk_index,
            "text": text,
            "bytes": len(audio_bytes),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        transcripts[session_id].append(segment)
        log.info(f"[{session_id}] Chunk {chunk_index}: {text}")

        # Broadcast to SSE listeners
        await broadcast_transcript(session_id, segment)

        # Append to file
        out_file = TRANSCRIPT_DIR / f"{session_id}.jsonl"
        with open(out_file, "a") as f:
            f.write(json.dumps(segment) + "\n")
    else:
        log.warning(f"[{session_id}] Chunk {chunk_index}: no transcription returned")


# --- App ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"Whisper Transcriber starting on port {PORT}")
    yield
    # Cancel any active sessions
    for sid, info in sessions.items():
        if "task" in info and not info["task"].done():
            info["task"].cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/api/state")
async def health():
    return {
        "status": "ok",
        "service": "whisper-transcriber",
        "active_sessions": len([s for s in sessions.values() if s.get("status") == "connected"]),
        "uptime_seconds": int(time.time() - START_TIME),
    }


@app.post("/api/connect")
async def connect_to_stream(request: Request):
    """Start transcribing a questionnaire live-stream.

    Body: { "ws_url": "ws://localhost:3050/ws/KQ_rtFFj" }
    """
    body = await request.json()
    ws_url = body.get("ws_url")
    if not ws_url:
        return JSONResponse({"error": "ws_url required"}, 400)

    session_id = body.get("session_id") or f"t_{int(time.time())}"

    if session_id in sessions and sessions[session_id].get("status") == "connected":
        return JSONResponse({"error": "session already active"}, 409)

    task = asyncio.create_task(stream_consumer(ws_url, session_id))

    sessions[session_id] = {
        "ws_url": ws_url,
        "session_id": session_id,
        "status": "connecting",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "task": task,
    }

    return JSONResponse({
        "session_id": session_id,
        "ws_url": ws_url,
        "status": "connecting",
        "transcript_url": f"/api/transcript/{session_id}",
        "live_url": f"/api/live/{session_id}",
    }, 201)


@app.delete("/api/session/{session_id}")
async def stop_session(session_id: str):
    """Stop an active transcription session."""
    if session_id not in sessions:
        return JSONResponse({"error": "not found"}, 404)

    info = sessions[session_id]
    if "task" in info and not info["task"].done():
        info["task"].cancel()

    info["status"] = "stopped"
    return {"session_id": session_id, "status": "stopped"}


@app.get("/api/sessions")
async def list_sessions():
    """List all transcription sessions."""
    return {
        "sessions": [
            {k: v for k, v in info.items() if k != "task"}
            for info in sessions.values()
        ]
    }


@app.get("/api/topology")
async def topology():
    """Report this service's upstream connections and downstream listeners."""
    upstreams = []
    for sid, info in sessions.items():
        upstreams.append({
            "session_id": sid,
            "upstream_url": info.get("ws_url"),
            "upstream_type": "ws",
            "status": info.get("status"),
        })

    downstreams = {}
    for sid, listeners in sse_listeners.items():
        downstreams[sid] = len(listeners)

    return {
        "service": "whisper-transcriber",
        "url": f"http://localhost:{PORT}",
        "upstreams": upstreams,
        "downstreams": downstreams,
        "active_sessions": len([u for u in upstreams if u["status"] == "connected"]),
    }


@app.get("/api/transcript/{session_id}")
async def get_transcript(session_id: str):
    """Get all transcription segments for a session."""
    segments = transcripts.get(session_id, [])
    return {
        "session_id": session_id,
        "segments": segments,
        "full_text": " ".join(s["text"] for s in segments),
    }


@app.get("/api/live/{session_id}")
async def live_transcript(session_id: str, request: Request):
    """SSE stream of live transcription segments."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    sse_listeners[session_id].add(queue)

    async def event_generator():
        try:
            # Replay existing segments
            for seg in transcripts.get(session_id, []):
                yield f"event: transcript\ndata: {json.dumps(seg, default=str)}\n\n"

            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield message
                except asyncio.TimeoutError:
                    yield f"event: heartbeat\ndata: {json.dumps({'time': datetime.now(timezone.utc).isoformat()})}\n\n"

                if await request.is_disconnected():
                    break
        finally:
            sse_listeners[session_id].discard(queue)
            if not sse_listeners[session_id]:
                del sse_listeners[session_id]

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.post("/api/transcribe")
async def transcribe_direct(request: Request):
    """Direct transcription: POST audio bytes, get text back."""
    content_type = request.headers.get("content-type", "audio/webm")
    body = await request.body()

    if len(body) < 100:
        return JSONResponse({"error": "audio too short"}, 400)

    text = await transcribe_audio(body, content_type)
    if text is None:
        return JSONResponse({"error": "transcription failed"}, 500)

    return {"text": text}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
