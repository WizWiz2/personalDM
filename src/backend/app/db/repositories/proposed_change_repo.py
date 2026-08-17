import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from app.db.repositories.base import BaseRepository
from app.db.tables import ProposedChange, Turn
from app.models.proposed_change import (
    ChangeType,
    ProposalAction,
    ProposedChangeCreate,
    ProposedChangeRead,
)


class ProposedChangeRepository(BaseRepository):
    @staticmethod
    def _validator_status(turn: Turn | None) -> str | None:
        if turn is None or not turn.context_snapshot:
            return None
        raw = turn.context_snapshot
        try:
            snapshot = raw if isinstance(raw, dict) else json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(snapshot, dict):
            return None
        protocol = snapshot.get("interagent_protocol")
        if not isinstance(protocol, dict):
            return None
        value = protocol.get("validator_status")
        return str(value) if value else None

    async def create_batch(
        self,
        turn_id: UUID,
        changes: list[ProposedChangeCreate],
    ) -> list[ProposedChangeRead]:
        """Persist proposals, but never let rejected narration become objective memory."""
        source_turn = await self._session.get(Turn, str(turn_id))
        validator_status = self._validator_status(source_turn)
        contained_publication = validator_status in {"safe_fallback", "failed_open"}

        results = []
        for change in changes:
            payload = dict(change.payload)
            if contained_publication:
                actor_claim = bool(source_turn and source_turn.acting_character_id) and (
                    change.change_type == ChangeType.KNOWLEDGE
                )
                texture_only = change.change_type == ChangeType.NARRATIVE_DETAIL
                if not actor_claim and not texture_only:
                    payload.setdefault(
                        "_validation_error",
                        "Objective memory is blocked for narration published through containment",
                    )

            payload_str = json.dumps(payload)
            validation_error = payload.get("_validation_error")
            db_change = ProposedChange(
                turn_id=str(turn_id),
                change_type=change.change_type.value,
                payload=payload_str,
                status="invalid" if validation_error else "proposed",
            )
            self._session.add(db_change)
            results.append(db_change)

        await self._session.flush()
        return [self._to_change_read(change) for change in results]

    async def get_for_turn(self, turn_id: UUID) -> list[ProposedChangeRead]:
        result = await self._session.execute(
            select(ProposedChange)
            .where(ProposedChange.turn_id == str(turn_id))
            .order_by(ProposedChange.created_at.asc())
        )
        changes = result.scalars().all()
        return [self._to_change_read(change) for change in changes]

    async def resolve(
        self,
        change_id: UUID,
        action: ProposalAction,
    ) -> ProposedChangeRead | None:
        result = await self._session.execute(
            select(ProposedChange).where(ProposedChange.id == str(change_id))
        )
        db_change = result.scalar_one_or_none()
        if not db_change:
            return None

        if db_change.status == "invalid" and action.status in {"accepted", "edited"}:
            payload = json.loads(db_change.payload or "{}")
            detail = payload.get("_validation_error", "deterministic validation failed")
            raise ValueError(f"Invalid proposal cannot be accepted: {detail}")

        if (
            db_change.status == "accepted"
            and action.status == "accepted"
            and action.user_edit is None
        ):
            return self._to_change_read(db_change)

        if db_change.status not in {"proposed", "invalid"}:
            raise ValueError(
                f"Proposal is already resolved with status '{db_change.status}'"
            )

        if db_change.status == "invalid" and action.status != "rejected":
            raise ValueError("Invalid proposal may only be rejected")

        db_change.status = action.status
        db_change.resolved_at = datetime.utcnow()
        if action.user_edit is not None:
            db_change.user_edit = json.dumps(action.user_edit)

        await self._session.flush()
        return self._to_change_read(db_change)

    def _to_change_read(self, db_change: ProposedChange) -> ProposedChangeRead:
        payload = {}
        if db_change.payload:
            try:
                payload = json.loads(db_change.payload)
            except Exception:
                pass

        user_edit = None
        if db_change.user_edit:
            try:
                user_edit = json.loads(db_change.user_edit)
            except Exception:
                pass

        return ProposedChangeRead(
            id=UUID(db_change.id),
            turn_id=UUID(db_change.turn_id),
            change_type=db_change.change_type,
            payload=payload,
            status=db_change.status,
            user_edit=user_edit,
            created_at=db_change.created_at,
            resolved_at=db_change.resolved_at,
        )
