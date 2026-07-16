import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.api.campaigns import router as campaigns_router
from app.api.turns import router as turns_router
from app.api.scenes import router as scenes_router
from app.api.entities import router as entities_router
from app.api.memory import router as memory_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup actions
    os.makedirs(settings.DATA_DIR, exist_ok=True)
    print(f"[personalDM] Starting up backend server.")
    print(f"[personalDM] Database: {settings.DATABASE_URL}")
    print(f"[personalDM] Data dir: {settings.DATA_DIR}")
    print(f"[personalDM] Default local LLM: {settings.LLM_MODEL} at {settings.LLM_BASE_URL}")
    yield
    # Shutdown actions
    print(f"[personalDM] Shutting down backend server.")

app = FastAPI(
    title="Personal DM API",
    description="Local AI Game Master Backend Core Engine",
    version="0.1.0",
    lifespan=lifespan
)

# CORS middleware for local frontend/Tauri communication
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API Routers
app.include_router(campaigns_router)
app.include_router(turns_router)
app.include_router(scenes_router)
app.include_router(entities_router)
app.include_router(memory_router)

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "0.1.0",
        "database": settings.DATABASE_URL,
        "model": settings.LLM_MODEL
    }
