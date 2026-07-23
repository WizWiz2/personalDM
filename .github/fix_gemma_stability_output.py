from pathlib import Path


path = Path("src/backend/app/services/context_compiler.py")
text = path.read_text(encoding="utf-8")
start_marker = "@staticmethod\ndef _short"
end_marker = "    async def _history_records("
start = text.find(start_marker)
end = text.find(end_marker, start)
if start < 0 or end < 0:
    raise SystemExit("generated scene receipt helper was not found")
block = text[start:end]
indented = "\n".join(("    " + line) if line else line for line in block.splitlines())
path.write_text(text[:start] + indented + "\n" + text[end:], encoding="utf-8")
