# Mutation testing

Measures whether the tests actually catch broken behaviour, not just execute lines.

```
cd <repo root>
PDM_TEXT_PROVIDER=local python scripts/mutation/mutate.py app/services/fact_repo.py --workers 8
```

Needs `pytest-cov`. Pass `--tests tests/test_a.py ...` to limit the coverage pass, `--reuse-coverage` to
skip it on a rerun. Output goes to `.mutation/` (git-ignored): `results.jsonl` lists every survivor with
its line and operator. Score = (killed + timeout) / total.
