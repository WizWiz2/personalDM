"""Bind a speculative prose review to exactly the candidate and frozen public authority."""
import hashlib
import json

from app.config import settings
from app.services.narration_call_budget import narration_budget
from app.services.turn_authority_validator import TurnAuthorityValidator


def fingerprint(authority, candidate: str) -> str:
    payload = authority.validator_payload()
    # New condition identities are assigned after director review. Their text must match;
    # existing identities are immutable and remain part of the signature.
    known = {str(item['state_id']) for item in authority.published_world_state.get('conditions', [])}
    for item in (payload.get('scene_development') or {}).get('state_updates', []):
        if str(item.get('state_id')) not in known:
            item['state_id'] = None
    wire = json.dumps({'authority': payload, 'candidate': candidate}, ensure_ascii=False,
                      sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(wire.encode()).hexdigest()


async def review_prepared(router, selection, authority, candidate: str) -> dict:
    validator = TurnAuthorityValidator(router)
    with narration_budget(settings.NARRATION_CONTROL_MAX_CALLS, settings.NARRATION_RENDER_MAX_CALLS) as budget:
        try:
            result = await validator.validate(selection, authority, candidate)
            return {'fingerprint': fingerprint(authority, candidate),
                    'result': result.model_dump(mode='json'), 'telemetry': validator.telemetry,
                    'model_name': selection.config.model_name, 'base_url': selection.config.base_url,
                    'control_calls': budget['control_used']}
        except Exception as exc:
            return {'error_type': type(exc).__name__, 'control_calls': budget['control_used']}
