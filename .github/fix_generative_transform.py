from pathlib import Path

path = Path(".github/generative_simulation_transform.py")
text = path.read_text(encoding="utf-8")
old = '''replace_once(
    "src/backend/app/db/tables.py",
    \'\'\'    visibility: Mapped[str] = mapped_column(String(50), default="dm", nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
 \'\'\',
    \'\'\'    visibility: Mapped[str] = mapped_column(String(50), default="dm", nullable=False)
    scope: Mapped[str] = mapped_column(String(50), default="campaign", nullable=False)
    scene_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("scenes.id", ondelete="CASCADE"), nullable=True
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
 \'\'\',
)
'''
new = '''replace_once(
    "src/backend/app/db/tables.py",
    \'\'\'    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    visibility: Mapped[str] = mapped_column(String(50), default="dm", nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
 \'\'\',
    \'\'\'    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    visibility: Mapped[str] = mapped_column(String(50), default="dm", nullable=False)
    scope: Mapped[str] = mapped_column(String(50), default="campaign", nullable=False)
    scene_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("scenes.id", ondelete="CASCADE"), nullable=True
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
 \'\'\',
)
'''
if text.count(old) != 1:
    raise SystemExit(f"fact-scope transform block count={text.count(old)}")
path.write_text(text.replace(old, new), encoding="utf-8")
