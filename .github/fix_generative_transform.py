from pathlib import Path

path = Path(".github/generative_simulation_transform.py")
text = path.read_text(encoding="utf-8")
old = """    '''    visibility: Mapped[str] = mapped_column(String(50), default=\"dm\", nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    '''"""
new = """    '''    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    visibility: Mapped[str] = mapped_column(String(50), default=\"dm\", nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    '''"""
if text.count(old) != 1:
    raise SystemExit(f"fact-scope transform anchor count={text.count(old)}")
path.write_text(text.replace(old, new), encoding="utf-8")
