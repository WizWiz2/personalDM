from app.config import settings
from app.services.visual_generation import Flux2PixelWorkflowBuilder


def _build(reference_names=None):
    return Flux2PixelWorkflowBuilder.build(
        prompt="pixel art game scene, two adventurers in a tavern",
        seed=123,
        width=768,
        height=512,
        reference_names=reference_names or [],
        lora_strength=0.9,
        filename_prefix="personaldm/test",
    )


def test_flux2_workflow_uses_compact_local_models_and_pixel_lora():
    workflow = _build()

    assert workflow["1"]["class_type"] == "UNETLoader"
    assert workflow["1"]["inputs"]["unet_name"] == settings.IMAGE_DIFFUSION_MODEL
    assert workflow["2"]["inputs"]["clip_name"] == settings.IMAGE_TEXT_ENCODER
    assert workflow["2"]["inputs"]["type"] == "flux2"
    assert workflow["3"]["inputs"]["vae_name"] == settings.IMAGE_VAE_MODEL
    assert workflow["4"]["class_type"] == "LoraLoader"
    assert workflow["4"]["inputs"]["lora_name"] == settings.IMAGE_LORA_MODEL
    assert workflow["7"]["class_type"] == "EmptyFlux2LatentImage"
    assert workflow["11"]["inputs"]["steps"] == settings.IMAGE_STEPS
    assert workflow["14"]["class_type"] == "SaveImage"


def test_reference_images_are_chained_into_positive_conditioning():
    workflow = _build(["refs/hero.png", "refs/npc.png"])

    assert workflow["20"]["class_type"] == "LoadImage"
    assert workflow["20"]["inputs"]["image"] == "refs/hero.png"
    assert workflow["21"]["class_type"] == "VAEEncode"
    assert workflow["22"]["class_type"] == "ReferenceLatent"
    assert workflow["22"]["inputs"]["conditioning"] == ["5", 0]

    assert workflow["23"]["inputs"]["image"] == "refs/npc.png"
    assert workflow["25"]["class_type"] == "ReferenceLatent"
    assert workflow["25"]["inputs"]["conditioning"] == ["22", 0]
    assert workflow["9"]["inputs"]["positive"] == ["25", 0]


def test_reference_count_is_capped_for_consumer_gpu():
    refs = [f"refs/{index}.png" for index in range(settings.IMAGE_MAX_REFERENCES + 3)]
    workflow = _build(refs)

    load_nodes = [
        node for node in workflow.values() if node["class_type"] == "LoadImage"
    ]
    assert len(load_nodes) == settings.IMAGE_MAX_REFERENCES
