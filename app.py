import uuid
import logging
import asyncio
from typing import Dict, List, Optional
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends, Security, Query, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from codereview_env.models import (
    TaskId, Action, ResetResult, StepResult, EpisodeResult
)
from codereview_env.env import CodeReviewEnv
from codereview_env.config import get_settings

# ── Logging ───────────────────────────────────────────────────────────────────
settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("codereview_env")

# ── App Initialization ────────────────────────────────────────────────────────
app = FastAPI(
    title="AgentOrg CodeReview OpenEnv API",
    description=(
        "AI Senior Code Reviewer evaluation environment. "
        "Trains agents to detect bugs, security vulnerabilities, and architectural issues "
        "in realistic Python PRs."
    ),
    version="1.0.0",
)

# ── Rate Limiting ─────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address, default_limits=[f"{settings.rate_limit_per_minute}/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── API Key Authentication ────────────────────────────────────────────────────
API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(api_key: str = Security(API_KEY_HEADER)):
    if not settings.api_key_enabled:
        return  # Auth disabled in development
    if api_key != settings.api_key:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")

# ── Storage & TTL ─────────────────────────────────────────────────────────────
episodes: Dict[str, CodeReviewEnv] = {}
episode_timestamps: Dict[str, datetime] = {}

async def cleanup_expired_episodes():
    """Remove episodes older than TTL."""
    while True:
        await asyncio.sleep(300)  # run every 5 minutes
        cutoff = datetime.now(timezone.utc).timestamp() - settings.episode_ttl_seconds
        expired = [
            eid for eid, ts in episode_timestamps.items()
            if ts.timestamp() < cutoff
        ]
        for eid in expired:
            episodes.pop(eid, None)
            episode_timestamps.pop(eid, None)
        if expired:
            logger.info(f"Cleaned up {len(expired)} expired episodes")

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(cleanup_expired_episodes())
    logger.info(f"CodeReview API started on port {settings.app_port}")

# ── Models ────────────────────────────────────────────────────────────────────
class ResetRequest(BaseModel):
    task_id: TaskId
    seed:    int = 42

class ResetResponse(BaseModel):
    episode_id: str
    result:     ResetResult

leaderboard: Dict[TaskId, List[dict]] = {
    TaskId.BUG_DETECTION:        [],
    TaskId.SECURITY_AUDIT:       [],
    TaskId.ARCHITECTURAL_REVIEW: []
}

class SubmitScore(BaseModel):
    agent_name: str
    task_id:    TaskId
    score:      float
    seed:       int

# ── WebSocket clients ─────────────────────────────────────────────────────────
clients = set()

async def broadcast_event(data: dict):
    from fastapi.encoders import jsonable_encoder
    import json
    message = json.dumps(jsonable_encoder(data))
    dead = set()
    for client in clients:
        try:
            await client.send_text(message)
        except Exception:
            dead.add(client)
    clients.difference_update(dead)

# ── Error Handlers ────────────────────────────────────────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_error",
            "detail": str(exc),
            "status_code": 422
        }
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    logger.warning(f"HTTP {exc.status_code}: {exc.detail} \u2014 {request.url}")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "status_code": exc.status_code
        }
    )

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "version": "1.0.0",
        "env_ready": True,
        "env": settings.app_env,
        "active_episodes": len(episodes),
        "auth_enabled": settings.api_key_enabled
    }

@app.post("/reset", response_model=ResetResponse)
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
def reset_env(request: Request, req: ResetRequest, _: None = Depends(verify_api_key)):
    episode_id = str(uuid.uuid4())
    env        = CodeReviewEnv()
    result     = env.reset(req.task_id, req.seed)
    episodes[episode_id] = env
    episode_timestamps[episode_id] = datetime.now(timezone.utc)
    return ResetResponse(episode_id=episode_id, result=result)

@app.post("/step/{episode_id}", response_model=StepResult)
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def step_env(request: Request, episode_id: str, action: Action, _: None = Depends(verify_api_key)):
    if episode_id not in episodes:
        raise HTTPException(status_code=404, detail="Episode not found")

    env = episodes[episode_id]
    try:
        result = env.step(action)
        await broadcast_event({"episode_id": episode_id, "type": "step", "reward": result.reward})
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/result/{episode_id}", response_model=EpisodeResult)
def get_result(episode_id: str, _: None = Depends(verify_api_key)):
    if episode_id not in episodes:
        raise HTTPException(status_code=404, detail="Episode not found")
    return episodes[episode_id].get_final_result()

@app.get("/leaderboard")
def get_leaderboard(
    task_id: Optional[TaskId] = None,
    limit: int = Query(default=10, ge=1, le=50),
    offset: int = Query(default=0, ge=0)
):
    if task_id:
        entries = leaderboard.get(task_id, [])
        return {
            "task_id": task_id,
            "entries": entries[offset:offset+limit],
            "total": len(entries)
        }
    return {
        task: {
            "entries": entries[offset:offset+limit],
            "total": len(entries)
        }
        for task, entries in leaderboard.items()
    }

@app.post("/submit")
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
def submit_to_leaderboard(request: Request, submission: SubmitScore, _: None = Depends(verify_api_key)):
    entries   = leaderboard.get(submission.task_id, [])
    new_entry = submission.model_dump()
    entries.append(new_entry)
    entries.sort(key=lambda x: x["score"], reverse=True)
    rank = entries.index(new_entry) + 1   # capture rank before slicing
    leaderboard[submission.task_id] = entries[:settings.leaderboard_max_entries]
    in_top_n = rank <= settings.leaderboard_max_entries
    return {"status": "submitted", "rank": rank if in_top_n else None}

@app.get("/episodes")
def list_episodes(
    _: None = Depends(verify_api_key),
    limit: int = Query(default=20, ge=1, le=100)
):
    episode_list = [
        {
            "episode_id": eid,
            "task_id": env.task_id,
            "step_count": env.observation.step_count,
            "done": env.done,
            "created_at": episode_timestamps.get(eid, "").isoformat() if episode_timestamps.get(eid) else ""
        }
        for eid, env in list(episodes.items())[:limit]
    ]
    return {"episodes": episode_list, "total": len(episodes)}

@app.websocket("/ws/events")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.app_host, port=settings.app_port)
