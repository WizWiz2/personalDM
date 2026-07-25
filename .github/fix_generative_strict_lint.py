from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one match, found {count}: {old[:120]!r}")
    target.write_text(text.replace(old, new), encoding="utf-8")


replace_once(
    "src/backend/app/services/memory_scribe.py",
    '''        except Exception:
            self.last_audit = CanonAudit(
''',
    '''        except json.JSONDecodeError:
            self.last_audit = CanonAudit(
''',
)

replace_once(
    "src/backend/tests/run_realistic_simulation.py",
    '''            try:
                decision = _BasePlayerDecision(
                    target=str(player.get("target", "narrator")),
                    mode=str(player.get("mode", "action")),
                    intent=str(player.get("intent", "")),
                )
            except Exception:
                continue
''',
    '''            decision = _BasePlayerDecision(
                target=str(player.get("target", "narrator")),
                mode=str(player.get("mode", "action")),
                intent=str(player.get("intent", "")),
            )
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation.py",
    '''    try:
        return _BasePlayerDecision(
            target=str(player.get("target", "narrator")),
            mode=str(player.get("mode", "action")),
            intent=str(player.get("intent", "")),
        )
    except Exception:
        return None
''',
    '''    return _BasePlayerDecision(
        target=str(player.get("target", "narrator")),
        mode=str(player.get("mode", "action")),
        intent=str(player.get("intent", "")),
    )
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation.py",
    '''    except Exception:
        return None


async def resumable_generate_player_decision''',
    '''    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


async def resumable_generate_player_decision''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation.py",
    '''    except Exception:
        return default
''',
    '''    except (json.JSONDecodeError, TypeError):
        return default
''',
)

replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''        except Exception:
            return None

    def save(self, path: Path) -> None:
''',
    '''        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None

    def save(self, path: Path) -> None:
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''                except Exception:
                    continue

    def upsert(self, record: dict) -> None:
''',
    '''                except (json.JSONDecodeError, KeyError, TypeError, ValueError):  # noqa: S112
                    continue

    def upsert(self, record: dict) -> None:
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''    except Exception as first_error:
        repair_prompt = f"""Исправь JSON карточки NPC {seed.name}.
''',
    '''    except (
        LLMProviderError,
        ValidationError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as first_error:
        repair_prompt = f"""Исправь JSON карточки NPC {seed.name}.
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''        except Exception:
            return deterministic_fallback_card(seed, location_id), "fallback"
''',
    '''        except (
            LLMProviderError,
            ValidationError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            return deterministic_fallback_card(seed, location_id), "fallback"
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''    except Exception:
        return ObjectiveEvaluation(
            status="progressing",
''',
    '''    except (
        LLMProviderError,
        ValidationError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        return ObjectiveEvaluation(
            status="progressing",
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''        except Exception as exc:
            await session.rollback()
            rejected.append(f"{proposal.change_type}: {exc}")
''',
    '''        except Exception as exc:  # noqa: BLE001
            # Proposal application is an isolation boundary: one malformed semantic
            # change must be rejected without aborting the whole benchmark turn.
            await session.rollback()
            rejected.append(f"{proposal.change_type}: {exc}")
''',
)
replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    '''        except Exception:
            continue
        marker = snapshot.get("simulation") or {}
''',
    '''        except (json.JSONDecodeError, TypeError):  # noqa: S112
            continue
        marker = snapshot.get("simulation") or {}
''',
)

replace_once(
    "src/backend/tests/simulation_dynamic_campaign.py",
    '''        except Exception:
            return None
''',
    '''        except (OSError, ValidationError):
            return None
''',
)

replace_once(
    "src/backend/tests/simulation_quality_controls.py",
    '''from app.providers.llm_provider import LLMProvider
''',
    '''from app.providers.llm_provider import LLMProvider, LLMProviderError
''',
)
replace_once(
    "src/backend/tests/simulation_quality_controls.py",
    '''        except Exception as exc:
            last_error = exc

    record_control_failure(label, last_error or "unknown JSON control error")
''',
    '''        except (
            LLMProviderError,
            ValidationError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            last_error = exc

    record_control_failure(label, last_error or "unknown JSON control error")
''',
)
replace_once(
    "src/backend/tests/simulation_quality_controls.py",
    '''            except Exception as exc:
                error = str(exc)
        if quality_mode():
''',
    '''            except (ValidationError, TypeError, ValueError) as exc:
                error = str(exc)
        if quality_mode():
''',
)
