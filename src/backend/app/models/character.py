from uuid import UUID
from pydantic import Field
from app.models.entity import EntityCreate, EntityRead, EntityUpdate, EntityType

class CharacterCreate(EntityCreate):
    entity_type: EntityType = Field(default=EntityType.CHARACTER, frozen=True)
    
    appearance: str | None = None
    face_description: str | None = None
    body_description: str | None = None
    immutable_features: str | None = None
    personality: str | None = None
    values: list[str] = []
    fears: list[str] = []
    desires: list[str] = []
    voice: str | None = None
    speech_patterns: str | None = None
    biography: str | None = None
    backstory_public: str | None = None
    backstory_secret: str | None = None
    emotional_state: str | None = None
    current_location_id: UUID | None = None
    current_intentions: list[str] = []
    visual_profile: dict | None = None

class CharacterRead(EntityRead):
    appearance: str | None = None
    face_description: str | None = None
    body_description: str | None = None
    immutable_features: str | None = None
    personality: str | None = None
    values: list[str] = []
    fears: list[str] = []
    desires: list[str] = []
    voice: str | None = None
    speech_patterns: str | None = None
    biography: str | None = None
    backstory_public: str | None = None
    backstory_secret: str | None = None
    emotional_state: str | None = None
    current_location_id: UUID | None = None
    current_intentions: list[str] = []
    visual_profile: dict | None = None

class CharacterUpdate(EntityUpdate):
    appearance: str | None = None
    face_description: str | None = None
    body_description: str | None = None
    immutable_features: str | None = None
    personality: str | None = None
    values: list[str] | None = None
    fears: list[str] | None = None
    desires: list[str] | None = None
    voice: str | None = None
    speech_patterns: str | None = None
    biography: str | None = None
    backstory_public: str | None = None
    backstory_secret: str | None = None
    emotional_state: str | None = None
    current_location_id: UUID | None = None
    current_intentions: list[str] | None = None
    visual_profile: dict | None = None
