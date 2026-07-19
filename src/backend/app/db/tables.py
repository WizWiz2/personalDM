import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.engine import Base


def generate_uuid() -> str:
    return str(uuid.uuid4())


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    narrative_style: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_scene_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("scenes.id", ondelete="SET NULL"),
        nullable=True,
    )
    player_character_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("entities.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    provider_config = relationship(
        "ProviderConfig",
        back_populates="campaign",
        cascade="all, delete-orphan",
        uselist=False,
    )
    turns = relationship("Turn", back_populates="campaign", cascade="all, delete-orphan")
    scenes = relationship(
        "Scene",
        back_populates="campaign",
        cascade="all, delete-orphan",
        foreign_keys="Scene.campaign_id",
    )
    entities = relationship(
        "Entity",
        back_populates="campaign",
        cascade="all, delete-orphan",
        foreign_keys="Entity.campaign_id",
    )
    facts = relationship("Fact", back_populates="campaign", cascade="all, delete-orphan")
    relationship_assertions = relationship(
        "RelationshipAssertion",
        back_populates="campaign",
        cascade="all, delete-orphan",
    )
    events = relationship("Event", back_populates="campaign", cascade="all, delete-orphan")
    media_assets = relationship(
        "MediaAsset",
        back_populates="campaign",
        cascade="all, delete-orphan",
    )


class ProviderConfig(Base):
    __tablename__ = "provider_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_window: Mapped[int] = mapped_column(Integer, default=8192)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    campaign = relationship("Campaign", back_populates="provider_config")


class Turn(Base):
    __tablename__ = "turns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    scene_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("scenes.id", ondelete="SET NULL"), nullable=True
    )
    acting_character_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="SET NULL"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    parent_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("turns.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    context_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    campaign = relationship("Campaign", back_populates="turns")
    scene = relationship("Scene", back_populates="turns")
    proposed_changes = relationship(
        "ProposedChange",
        back_populates="turn",
        cascade="all, delete-orphan",
    )


class GenerationRun(Base):
    __tablename__ = "generation_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    user_turn_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("turns.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    assistant_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("turns.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(50), default="running", nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class PostTurnJob(Base):
    __tablename__ = "post_turn_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    assistant_turn_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("turns.id", ondelete="CASCADE"), nullable=False
    )
    job_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("assistant_turn_id", "job_type", name="uq_post_turn_job"),
    )


class WorldStateSnapshot(Base):
    __tablename__ = "world_state_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class Scene(Base):
    __tablename__ = "scenes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    location_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    mood: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tension: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    campaign = relationship(
        "Campaign",
        back_populates="scenes",
        foreign_keys=[campaign_id],
    )
    turns = relationship("Turn", back_populates="scene")
    theses = relationship(
        "SceneThesis", back_populates="scene", cascade="all, delete-orphan"
    )
    media_assets = relationship("MediaAsset", back_populates="scene")


class SceneParticipant(Base):
    __tablename__ = "scene_participants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    scene_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("scene_id", "entity_id", name="uq_scene_participant"),
    )


class SceneThesis(Base):
    __tablename__ = "scene_theses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    scene_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False
    )
    thesis_type: Mapped[str] = mapped_column(String(100), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False)
    visibility: Mapped[str] = mapped_column(String(50), default="dm", nullable=False)
    source_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("turns.id", ondelete="SET NULL"), nullable=True
    )
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    related_entity_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    scene = relationship("Scene", back_populates="theses")


class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    aliases: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False)
    provenance: Mapped[str] = mapped_column(String(100), default="manual", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    custom_fields: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    campaign = relationship(
        "Campaign", back_populates="entities", foreign_keys=[campaign_id]
    )
    character_data = relationship(
        "Character",
        back_populates="base_entity",
        cascade="all, delete-orphan",
        uselist=False,
        foreign_keys="Character.entity_id",
    )
    location_data = relationship(
        "Location",
        back_populates="base_entity",
        cascade="all, delete-orphan",
        uselist=False,
        foreign_keys="Location.entity_id",
    )
    item_data = relationship(
        "Item",
        back_populates="base_entity",
        cascade="all, delete-orphan",
        uselist=False,
        foreign_keys="Item.entity_id",
    )
    faction_data = relationship(
        "Faction",
        back_populates="base_entity",
        cascade="all, delete-orphan",
        uselist=False,
        foreign_keys="Faction.entity_id",
    )
    creature_data = relationship(
        "Creature",
        back_populates="base_entity",
        cascade="all, delete-orphan",
        uselist=False,
        foreign_keys="Creature.entity_id",
    )
    goals = relationship(
        "CharacterGoal", back_populates="character", cascade="all, delete-orphan"
    )
    beliefs = relationship(
        "Belief",
        back_populates="character",
        cascade="all, delete-orphan",
        foreign_keys="Belief.character_id",
    )

    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "entity_type",
            "canonical_name",
            name="uq_campaign_entity_name",
        ),
    )


class Character(Base):
    __tablename__ = "characters"

    entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    appearance: Mapped[str | None] = mapped_column(Text, nullable=True)
    face_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    immutable_features: Mapped[str | None] = mapped_column(Text, nullable=True)
    personality: Mapped[str | None] = mapped_column(Text, nullable=True)
    values: Mapped[str | None] = mapped_column(Text, nullable=True)
    fears: Mapped[str | None] = mapped_column(Text, nullable=True)
    desires: Mapped[str | None] = mapped_column(Text, nullable=True)
    voice: Mapped[str | None] = mapped_column(Text, nullable=True)
    speech_patterns: Mapped[str | None] = mapped_column(Text, nullable=True)
    biography: Mapped[str | None] = mapped_column(Text, nullable=True)
    backstory_public: Mapped[str | None] = mapped_column(Text, nullable=True)
    backstory_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    emotional_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_location_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="SET NULL"), nullable=True
    )
    current_intentions: Mapped[str | None] = mapped_column(Text, nullable=True)
    visual_profile: Mapped[str | None] = mapped_column(Text, nullable=True)

    base_entity = relationship(
        "Entity", back_populates="character_data", foreign_keys=[entity_id]
    )


class Location(Base):
    __tablename__ = "locations"

    entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    geography: Mapped[str | None] = mapped_column(Text, nullable=True)
    atmosphere: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_rules: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_location_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="SET NULL"), nullable=True
    )
    climate: Mapped[str | None] = mapped_column(Text, nullable=True)
    notable_features: Mapped[str | None] = mapped_column(Text, nullable=True)
    danger_level: Mapped[str | None] = mapped_column(String(100), nullable=True)

    base_entity = relationship(
        "Entity", back_populates="location_data", foreign_keys=[entity_id]
    )


class Item(Base):
    __tablename__ = "items"

    entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    item_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    physical_properties: Mapped[str | None] = mapped_column(Text, nullable=True)
    magical_properties: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_estimate: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_owner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="SET NULL"), nullable=True
    )
    current_location_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="SET NULL"), nullable=True
    )
    is_unique: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    lore: Mapped[str | None] = mapped_column(Text, nullable=True)

    base_entity = relationship(
        "Entity", back_populates="item_data", foreign_keys=[entity_id]
    )

    __table_args__ = (
        CheckConstraint(
            "NOT (current_owner_id IS NOT NULL AND current_location_id IS NOT NULL)",
            name="ck_item_single_position",
        ),
    )


class Faction(Base):
    __tablename__ = "factions"

    entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    goals: Mapped[str | None] = mapped_column(Text, nullable=True)
    resources: Mapped[str | None] = mapped_column(Text, nullable=True)
    territory: Mapped[str | None] = mapped_column(Text, nullable=True)
    hierarchy: Mapped[str | None] = mapped_column(Text, nullable=True)
    membership_rules: Mapped[str | None] = mapped_column(Text, nullable=True)
    reputation: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_agenda: Mapped[str | None] = mapped_column(Text, nullable=True)
    leader_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="SET NULL"), nullable=True
    )

    base_entity = relationship(
        "Entity", back_populates="faction_data", foreign_keys=[entity_id]
    )


class Creature(Base):
    __tablename__ = "creatures"

    entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    species: Mapped[str | None] = mapped_column(String(255), nullable=True)
    abilities: Mapped[str | None] = mapped_column(Text, nullable=True)
    behavior: Mapped[str | None] = mapped_column(Text, nullable=True)
    habitat: Mapped[str | None] = mapped_column(Text, nullable=True)
    threat_level: Mapped[str | None] = mapped_column(String(100), nullable=True)
    weaknesses: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_unique: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    base_entity = relationship(
        "Entity", back_populates="creature_data", foreign_keys=[entity_id]
    )


class CharacterGoal(Base):
    __tablename__ = "character_goals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    character_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("turns.id", ondelete="SET NULL"), nullable=True
    )
    valid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    character = relationship("Entity", back_populates="goals")


class Fact(Base):
    __tablename__ = "facts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    predicate: Mapped[str] = mapped_column(String(255), nullable=False)
    object_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    truth_status: Mapped[str] = mapped_column(String(50), default="true", nullable=False)
    source_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("turns.id", ondelete="SET NULL"), nullable=True
    )
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    visibility: Mapped[str] = mapped_column(String(50), default="dm", nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    superseded_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("facts.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    campaign = relationship("Campaign", back_populates="facts")
    beliefs = relationship("Belief", back_populates="fact", cascade="all, delete-orphan")


class Belief(Base):
    __tablename__ = "beliefs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    character_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    fact_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("facts.id", ondelete="SET NULL"), nullable=True
    )
    proposition: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="believed", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    source_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("turns.id", ondelete="SET NULL"), nullable=True
    )
    source_character_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="SET NULL"), nullable=True
    )
    visibility: Mapped[str] = mapped_column(String(50), default="dm", nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    superseded_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("beliefs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    character = relationship("Entity", back_populates="beliefs", foreign_keys=[character_id])
    fact = relationship("Fact", back_populates="beliefs")


class RelationshipAssertion(Base):
    __tablename__ = "relationship_assertions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    subject_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    object_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    intensity: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_event_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )
    source_turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("turns.id", ondelete="SET NULL"), nullable=True
    )
    provenance: Mapped[str] = mapped_column(String(100), default="manual", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    valid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    visibility: Mapped[str] = mapped_column(String(50), default="dm", nullable=False)
    superseded_by: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("relationship_assertions.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    campaign = relationship("Campaign", back_populates="relationship_assertions")

    __table_args__ = (
        CheckConstraint("subject_id != object_id", name="ck_self_relationship"),
        CheckConstraint(
            "intensity IS NULL OR (intensity >= -1.0 AND intensity <= 1.0)",
            name="ck_relationship_intensity",
        ),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    world_time: Mapped[str | None] = mapped_column(String(100), nullable=True)
    location_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="SET NULL"), nullable=True
    )
    importance: Mapped[str] = mapped_column(String(50), default="normal", nullable=False)
    source_turns: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    campaign = relationship("Campaign", back_populates="events")


class EventParticipant(Base):
    __tablename__ = "event_participants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(100), default="participant", nullable=False)


class ProposedChange(Base):
    __tablename__ = "proposed_changes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    turn_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("turns.id", ondelete="CASCADE"), nullable=False
    )
    change_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="proposed", nullable=False)
    user_edit: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    turn = relationship("Turn", back_populates="proposed_changes")


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    asset_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    scene_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("scenes.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    campaign = relationship("Campaign", back_populates="media_assets")
    scene = relationship("Scene", back_populates="media_assets")


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    file_path: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    artist: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mood_tags: Mapped[str | None] = mapped_column(Text, nullable=True)
    energy: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PlaybackHistory(Base):
    __tablename__ = "playback_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    track_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False
    )
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    scene_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("scenes.id", ondelete="SET NULL"), nullable=True
    )
    played_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
