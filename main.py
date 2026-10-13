import os
import json
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

# CORS: in production, replace "*" with your actual frontend origin.
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "..", "frontend")

app = FastAPI(title="studysheet API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/generate")
async def generate(request: Request):
    """
    Streams a chat completion from Groq back to the client as
    Server-Sent Events. The API key never leaves the server.
    """
    if not API_KEY:
        return JSONResponse(
            {"error": "GROQ_API_KEY is not set on the server. Add it as an "
                      "environment variable (see .env.example)."},
            status_code=500,
        )

    body = await request.json()
    system_prompt = body.get("system", "")
    user_messages = body.get("messages", [])

    chat_messages = []
    if system_prompt:
        chat_messages.append({"role": "system", "content": system_prompt})
    for m in user_messages:
        chat_messages.append({"role": m.get("role", "user"), "content": m.get("content", "")})

    payload = {
        "model": GROQ_MODEL,
        "messages": chat_messages,
        "temperature": 0.7,
        "max_tokens": body.get("max_tokens", 4000),
        "response_format": {"type": "json_object"},
        "stream": True,
    }

    async def event_stream():
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                async with client.stream(
                    "POST",
                    GROQ_URL,
                    headers={
                        "Authorization": f"Bearer {API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                ) as resp:
                    if resp.status_code != 200:
                        err_bytes = await resp.aread()
                        try:
                            err_json = json.loads(err_bytes)
                            msg = err_json.get("error", {}).get("message", "Upstream error.")
                        except Exception:
                            msg = "Upstream error from Groq."
                        yield f"data: {json.dumps({'error': msg})}\n\n"
                        return

                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[len("data:"):].strip()
                        if data_str == "[DONE]":
                            yield "data: [DONE]\n\n"
                            break
                        try:
                            chunk = json.loads(data_str)
                            delta = chunk["choices"][0]["delta"].get("content", "")
                        except Exception:
                            continue
                        if delta:
                            yield f"data: {json.dumps({'token': delta})}\n\n"
        except httpx.RequestError as e:
            yield f"data: {json.dumps({'error': f'Could not reach Groq API: {e}'})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering (nginx etc.)
        },
    )


# Serve the frontend as static files. This must be mounted last so it
# doesn't shadow the /api routes above.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
