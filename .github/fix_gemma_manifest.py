from pathlib import Path


path = Path("src/backend/app/services/context_compiler.py")
text = path.read_text(encoding="utf-8")
old = '''            if not actor_mode:
                receipt, stagnation_detected, recent_scene_turns = (
                    await self._scene_progress_receipt(campaign_id, scene_id)
                )
                scene_receipt_items = len(receipt)
                if receipt:
                    scene_info += (
                        "Recent authoritative scene progress:\n"
                        + "".join(f"- {item}\n" for item in receipt)
                        + "Continue from these consequences; do not retell them.\n"
                    )
                if stagnation_detected:
                    scene_info += (
                        "[Progress Watchdog] Последние ходы не дали нового "
                        "устойчивого последствия. В этом ответе сдвинь сцену: "
                        "раскрой конкретный факт, измени опасность, позицию или "
                        "отношение, либо дай явное последствие текущей попытки. "
                        "Не повторяй прежний вопрос или обсуждение.\n"
                    )
            scene_thesis_ids = [str(thesis.id) for thesis in visible_theses]
            scene_tokens = count_tokens(scene_info)
            if current_budget_used + scene_tokens < content_budget:
                packages.append(scene_info)
                current_budget_used += scene_tokens
                included_thesis_ids.extend(scene_thesis_ids)
                included_layers.append("layer_1_scene")
'''
new = '''            receipt_items_candidate = 0
            stagnation_candidate = False
            recent_turns_candidate = 0
            if not actor_mode:
                receipt, stagnation_candidate, recent_turns_candidate = (
                    await self._scene_progress_receipt(campaign_id, scene_id)
                )
                receipt_items_candidate = len(receipt)
                if receipt:
                    scene_info += (
                        "Recent authoritative scene progress:\n"
                        + "".join(f"- {item}\n" for item in receipt)
                        + "Continue from these consequences; do not retell them.\n"
                    )
                if stagnation_candidate:
                    scene_info += (
                        "[Progress Watchdog] Последние ходы не дали нового "
                        "устойчивого последствия. В этом ответе сдвинь сцену: "
                        "раскрой конкретный факт, измени опасность, позицию или "
                        "отношение, либо дай явное последствие текущей попытки. "
                        "Не повторяй прежний вопрос или обсуждение.\n"
                    )
            scene_thesis_ids = [str(thesis.id) for thesis in visible_theses]
            scene_tokens = count_tokens(scene_info)
            if current_budget_used + scene_tokens < content_budget:
                packages.append(scene_info)
                current_budget_used += scene_tokens
                included_thesis_ids.extend(scene_thesis_ids)
                included_layers.append("layer_1_scene")
                scene_receipt_items = receipt_items_candidate
                stagnation_detected = stagnation_candidate
                recent_scene_turns = recent_turns_candidate
'''
if text.count(old) != 1:
    raise SystemExit(f"scene receipt block matched {text.count(old)} times")
path.write_text(text.replace(old, new), encoding="utf-8")


test_path = Path("src/backend/tests/test_narrator_stability.py")
test_text = test_path.read_text(encoding="utf-8")
addition = r'''

@pytest.mark.asyncio
async def test_receipt_manifest_only_reports_content_that_was_sent(
    db_session: AsyncSession,
    monkeypatch,
):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Tight Budget"),
    )
    scene = await SceneRepository(db_session).create(
        campaign_id,
        SceneCreate(title="Очень длинная сцена " + "x" * 3000),
    )
    await add_pair(
        db_session,
        campaign_id,
        scene.id,
        1,
        progress="Этот прогресс не помещается в prompt",
    )
    await db_session.commit()
    monkeypatch.setattr(settings, "RESPONSE_RESERVE_TOKENS", 4000)

    messages, metadata = await ContextCompiler(db_session).compile_context(
        campaign_id=campaign_id,
        scene_id=scene.id,
    )
    context = "\n".join(message.content for message in messages)

    assert "Этот прогресс не помещается в prompt" not in context
    assert metadata["scene_receipt_items"] == 0
    assert metadata["recent_scene_turns_checked"] == 0
    assert metadata["stagnation_detected"] is False
'''
if "test_receipt_manifest_only_reports_content_that_was_sent" not in test_text:
    test_path.write_text(test_text.rstrip() + addition + "\n", encoding="utf-8")
