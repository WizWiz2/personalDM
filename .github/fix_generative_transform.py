from pathlib import Path

path = Path(".github/generative_simulation_transform.py")
text = path.read_text(encoding="utf-8")
needle = "    '''    visibility: Mapped[str] = mapped_column(String(50), default=\"dm\", nullable=False)"
replacement = (
    "    '''    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)\n"
    "    visibility: Mapped[str] = mapped_column(String(50), default=\"dm\", nullable=False)"
)
if text.count(needle) != 2:
    raise SystemExit(f"fact-scope transform anchor count={text.count(needle)}")
path.write_text(text.replace(needle, replacement), encoding="utf-8")
