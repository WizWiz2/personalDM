from pathlib import Path

path = Path("src/backend/tests/simulation_dynamic_campaign.py")
text = path.read_text(encoding="utf-8")
old = "from pydantic import BaseModel, Field, field_validator, model_validator\n"
new = (
    "from pydantic import (\n"
    "    BaseModel,\n"
    "    Field,\n"
    "    ValidationError,\n"
    "    field_validator,\n"
    "    model_validator,\n"
    ")\n"
)
if text.count(old) != 1:
    raise SystemExit(f"pydantic import anchor count={text.count(old)}")
path.write_text(text.replace(old, new), encoding="utf-8")
