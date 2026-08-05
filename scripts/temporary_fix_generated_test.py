from pathlib import Path

path = Path("src/backend/tests/test_session_zero_rate_limit_budget.py")
text = path.read_text(encoding="utf-8")
replacements = {
    "assert '\n  \"world\"' not in messages[0].content": (
        "assert '\\n  \"world\"' not in messages[0].content"
    ),
    "assert '[CURRENT DRAFT]\n{\"world\":' in messages[0].content": (
        "assert '[CURRENT DRAFT]\\n{\"world\":' in messages[0].content"
    ),
}
for old, new in replacements.items():
    if old not in text:
        raise SystemExit(f"generated test pattern not found: {old!r}")
    text = text.replace(old, new, 1)
path.write_text(text, encoding="utf-8")
