import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.campaigns import router as campaigns_router
from app.api.entities import router as entities_router
from app.api.memory import router as memory_router
from app.api.scenes import router as scenes_router
from app.api.turns import router as turns_router
from app.api.world_state import router as world_state_router
from app.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(settings.DATA_DIR, exist_ok=True)
    print("[personalDM] Starting backend server")
    print(f"[personalDM] Data dir: {settings.DATA_DIR}")
    print(f"[personalDM] Default local LLM: {settings.LLM_MODEL}")
    yield
    print("[personalDM] Shutting down backend server")


app = FastAPI(
    title="Personal DM API",
    description="Local AI Game Master Backend Core Engine",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "tauri://localhost",
        "http://tauri.localhost",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(campaigns_router)
app.include_router(turns_router)
app.include_router(scenes_router)
app.include_router(entities_router)
app.include_router(memory_router)
app.include_router(world_state_router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "0.1.0",
        "model": settings.LLM_MODEL,
    }
