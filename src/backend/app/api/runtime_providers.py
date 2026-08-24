from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.models.provider_config import ProviderConfigCreate
from app.services.campaign_service import CampaignService
from app.services.runtime_provider_service import RuntimeProviderError, RuntimeProviderService

router = APIRouter(prefix="/api/runtime/providers", tags=["runtime-providers"])


class TextProviderUpdate(BaseModel):
    mode: str = Field(pattern="^(local|cloud)$")
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    context_window: int | None = Field(default=None, ge=1024)
    campaign_id: UUID | None = None


class ImageProviderUpdate(BaseModel):
    mode: str = Field(pattern="^(local|cloud|off)$")
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None


@dataclass
class InstallJob:
    id: str
    kind: str
    status: str = "running"
    error: str | None = None
    result: dict | None = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


_jobs: dict[str, InstallJob] = {}


def _job_payload(job: InstallJob) -> dict:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "error": job.error,
        "result": job.result,
        "created_at": job.created_at,
    }


async def _run_install(job: InstallJob) -> None:
    service = RuntimeProviderService()
    try:
        if job.kind == "text":
            result = await asyncio.to_thread(service.ensure_local_text)
        else:
            result = await asyncio.to_thread(service.ensure_local_image)
        job.result = result
        job.status = "completed"
    except Exception as exc:  # install boundary: return actionable failure to UI
        job.error = str(exc)
        job.status = "failed"


@router.get("")
async def get_runtime_providers():
    return await asyncio.to_thread(RuntimeProviderService().profile)


@router.post("/check")
async def check_runtime_providers():
    service = RuntimeProviderService()
    return {
        "text": await asyncio.to_thread(service.check_text),
        "image": await asyncio.to_thread(service.check_image),
    }


@router.put("/text")
async def update_text_provider(
    data: TextProviderUpdate,
    session: AsyncSession = Depends(get_session),
):
    service = RuntimeProviderService()
    try:
        profile = service.configure_text(
            data.mode,
            base_url=data.base_url,
            model=data.model,
            api_key=data.api_key,
            context_window=data.context_window,
        )
        if data.campaign_id:
            await CampaignService(session).configure_provider(
                data.campaign_id,
                ProviderConfigCreate(
                    base_url=profile["base_url"],
                    model_name=profile["model"],
                    api_key=(
                        data.api_key
                        if data.mode == "cloud"
                        else None
                    ),
                    context_window=profile["context_window"],
                ),
            )
        return service.profile()["text"]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/image")
async def update_image_provider(data: ImageProviderUpdate):
    service = RuntimeProviderService()
    try:
        service.configure_image(
            data.mode,
            base_url=data.base_url,
            model=data.model,
            api_key=data.api_key,
        )
        return service.profile()["image"]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{kind}/install")
async def install_local_provider(kind: str):
    if kind not in {"text", "image"}:
        raise HTTPException(status_code=404, detail="Unknown provider kind")
    service = RuntimeProviderService()
    mode = service.text_mode() if kind == "text" else service.image_mode()
    if mode != "local":
        raise HTTPException(status_code=409, detail=f"{kind} provider is not configured as local")
    job = InstallJob(id=str(uuid.uuid4()), kind=kind)
    _jobs[job.id] = job
    asyncio.create_task(_run_install(job), name=f"provider-install-{kind}-{job.id}")
    return _job_payload(job)


@router.get("/install/{job_id}")
async def get_install_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Install job not found")
    return _job_payload(job)
