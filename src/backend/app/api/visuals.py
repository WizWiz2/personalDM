from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.engine import get_session
from app.db.tables import Campaign, MediaAsset
from app.services.visual_generation import ComfyUIError, VisualGenerationService

router = APIRouter(tags=["visuals"])


def _asset_payload(service: VisualGenerationService, path: Path, kind: str) -> dict:
    return {
        "kind": kind,
        "available": path.is_file(),
        "url": service.public_url(path),
    }


def _generated_payload(result) -> dict:
    payload = asdict(result)
    payload["available"] = True
    return payload


async def _archive_generated_result(
    session: AsyncSession,
    service: VisualGenerationService,
    result,
    *,
    campaign_id: UUID | None = None,
) -> dict:
    """Preserve each explicit generation while keeping stable latest/cover/portrait paths.

    VisualGenerationService writes a stable path used by the live UI. Without this copy,
    a second scene generation overwrites the first PNG and MediaAsset history points at
    the same file. API-triggered generations therefore get an immutable gallery copy.
    """
    payload = _generated_payload(result)
    if not result.generated or not result.seed:
        return payload

    source = Path(result.file_path)
    if not source.is_file():
        return payload

    archive_dir = source.parent / "gallery"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = archive_dir / f"{result.kind}-{result.seed}.png"
    temporary = archive.with_suffix(".tmp")
    shutil.copyfile(source, temporary)
    temporary.replace(archive)

    query = select(MediaAsset).where(
        MediaAsset.asset_type == result.kind,
        MediaAsset.seed == result.seed,
    )
    if campaign_id is not None:
        query = query.where(MediaAsset.campaign_id == str(campaign_id))
    asset = (
        await session.execute(query.order_by(MediaAsset.created_at.desc()).limit(1))
    ).scalar_one_or_none()
    if asset is not None:
        relative = archive.resolve().relative_to(Path(settings.DATA_DIR).resolve())
        asset.file_path = relative.as_posix()
        await session.flush()

    payload["file_path"] = str(archive)
    payload["url"] = service.public_url(archive)
    return payload


@router.get("/api/visuals/status")
async def visual_status(session: AsyncSession = Depends(get_session)):
    return await VisualGenerationService(session).status()


@router.get("/api/characters/{character_id}/visuals/portrait")
async def get_character_portrait(
    character_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    service = VisualGenerationService(session)
    return _asset_payload(
        service,
        service.character_portrait_path(character_id),
        service.PORTRAIT_TYPE,
    )


@router.post("/api/characters/{character_id}/visuals/portrait")
async def generate_character_portrait(
    character_id: UUID,
    force: bool = Query(default=True),
    session: AsyncSession = Depends(get_session),
):
    try:
        service = VisualGenerationService(session)
        result = await service.generate_character_portrait(character_id, force=force)
        return await _archive_generated_result(session, service, result)
    except ComfyUIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/campaigns/{campaign_id}/visuals/cover")
async def get_campaign_cover(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    service = VisualGenerationService(session)
    return _asset_payload(
        service,
        service.campaign_cover_path(campaign_id),
        service.CAMPAIGN_COVER_TYPE,
    )


@router.post("/api/campaigns/{campaign_id}/visuals/cover")
async def generate_campaign_cover(
    campaign_id: UUID,
    force: bool = Query(default=True),
    session: AsyncSession = Depends(get_session),
):
    try:
        service = VisualGenerationService(session)
        result = await service.generate_campaign_cover(campaign_id, force=force)
        return await _archive_generated_result(
            session,
            service,
            result,
            campaign_id=campaign_id,
        )
    except ComfyUIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/campaigns/{campaign_id}/visuals/gallery")
async def get_campaign_gallery(
    campaign_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    if await session.get(Campaign, str(campaign_id)) is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    service = VisualGenerationService(session)
    rows = (
        await session.execute(
            select(MediaAsset)
            .where(
                MediaAsset.campaign_id == str(campaign_id),
                MediaAsset.asset_type.in_(
                    (
                        service.CAMPAIGN_COVER_TYPE,
                        service.PORTRAIT_TYPE,
                        service.SCENE_TYPE,
                    )
                ),
            )
            .order_by(MediaAsset.created_at.desc())
        )
    ).scalars().all()

    items: list[dict] = []
    seen_urls: set[str] = set()
    for asset in rows:
        path = Path(settings.DATA_DIR) / asset.file_path
        if not path.is_file():
            continue
        try:
            url = service.public_url(path)
        except ValueError:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            metadata = json.loads(asset.metadata_json) if asset.metadata_json else {}
        except (TypeError, ValueError):
            metadata = {}
        items.append(
            {
                "id": asset.id,
                "kind": asset.asset_type,
                "url": url,
                "prompt": asset.prompt,
                "seed": asset.seed,
                "scene_id": asset.scene_id,
                "created_at": asset.created_at.isoformat(),
                "metadata": metadata,
            }
        )
    return items


@router.get("/api/campaigns/{campaign_id}/scenes/{scene_id}/visuals/latest")
async def get_scene_visual(
    campaign_id: UUID,
    scene_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    service = VisualGenerationService(session)
    # Validate ownership even when there is no generated file yet.
    try:
        from app.services.scene_state_service import SceneStateService

        await SceneStateService(session).get(campaign_id, scene_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _asset_payload(service, service.scene_path(scene_id), service.SCENE_TYPE)


@router.post("/api/campaigns/{campaign_id}/scenes/{scene_id}/visuals")
async def generate_scene_visual(
    campaign_id: UUID,
    scene_id: UUID,
    force: bool = Query(default=True),
    session: AsyncSession = Depends(get_session),
):
    try:
        service = VisualGenerationService(session)
        result = await service.generate_scene(campaign_id, scene_id, force=force)
        return await _archive_generated_result(
            session,
            service,
            result,
            campaign_id=campaign_id,
        )
    except ComfyUIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
