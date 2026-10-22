"""Pipeline B, step 1: single Vision-Language Model call -> strict structured JSON.

No separate classifier, anomaly detector or CNN: the VLM performs plant
identification, health assessment and visual evidence extraction in one call.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO
from typing import Literal

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field

from ..providers.llm import LLM, ChatTurn, ImagePart

HealthStatus = Literal["healthy", "unhealthy", "uncertain"]
IssueCategory = Literal[
    "disease",
    "nutrient_deficiency",
    "pest_damage",
    "fungal_infection",
    "bacterial_infection",
    "viral_infection",
    "environmental_stress",
    "physical_damage",
    "abnormal_coloration",
    "abnormal_morphology",
]

CONFIDENCE_NOTE = (
    "Model-reported confidence is a rough indicator of the model's own certainty, "
    "not a statistically calibrated probability."
)
DISCLAIMER = (
    "AI-generated visual assessment, not a confirmed diagnosis. Confirm with a local agricultural "
    "expert (e.g. KVK / extension officer) before applying treatments."
)
RECAPTURE_TIPS = [
    "Move closer so one affected leaf fills most of the frame.",
    "Use even daylight; avoid harsh shadows, backlight and flash glare.",
    "Hold the camera steady (or park the rover) to avoid motion blur.",
    "Photograph both the upper and the lower side of an affected leaf.",
    "Include a healthy leaf of the same plant for comparison in a second photo.",
]


class VLMDiagnosis(BaseModel):
    is_plant_image: bool = Field(description="False if the image does not show a plant or crop")
    plant: str = Field(description="Common name of the plant or crop, or 'Unknown'")
    plant_scientific_name: str | None = Field(default=None, description="Scientific name if reasonably confident")
    plant_part: str | None = Field(default=None, description="Visible part: leaf, fruit, stem, flower, whole plant ...")
    health_status: HealthStatus = Field(description="healthy, unhealthy or uncertain")
    diagnosis: str = Field(
        description="Short possible diagnosis phrased as a possibility, e.g. 'Possible early blight'; "
        "for healthy plants 'No visible signs of disease or stress'"
    )
    issue_categories: list[IssueCategory] = Field(default_factory=list, description="All issue categories that apply")
    evidence: list[str] = Field(description="Directly observable visual features that support the conclusion")
    assumptions: list[str] = Field(default_factory=list, description="Inferences that are not directly visible")
    differential: list[str] = Field(default_factory=list, description="Other conditions that could look similar")
    severity: Literal["none", "mild", "moderate", "severe", "unknown"] = Field(
        default="unknown", description="Visible extent of the problem"
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Rough self-assessed certainty between 0 and 1")
    image_quality: Literal["good", "fair", "poor"] = Field(description="Suitability of the photo for diagnosis")
    image_quality_notes: str | None = Field(default=None, description="Blur, lighting, distance or occlusion issues")


VLM_SYSTEM_PROMPT = """You are the visual-analysis step of GAIA's crop-diagnosis pipeline. You receive \
one photograph taken in a farmer's field by a rover camera or a phone. Analyse ONLY what is visible and \
return strict JSON.

1. is_plant_image: false if the photo does not show a plant or crop. Then use plant "Unknown", \
health_status "uncertain", and describe what is visible in evidence.
2. Identify the plant or crop (common name; scientific name only if reasonably confident). If you cannot \
identify it, use "Unknown" and explain why in assumptions.
3. health_status:
   - "healthy": no abnormality is visible;
   - "unhealthy": a problem is clearly visible;
   - "uncertain": signs are ambiguous, image quality is insufficient, or you cannot tell explanations apart.
4. If an issue is present, consider: disease, nutrient deficiency, pest damage, fungal infection, \
bacterial infection, viral infection, environmental stress (heat, drought, waterlogging, sunscald, \
chemical injury), physical damage, abnormal coloration, abnormal morphology. List every category that \
applies in issue_categories (empty for healthy plants).
5. diagnosis: always phrase as a possibility ("Possible early blight", "Suspected nitrogen deficiency"), \
never as a certainty.
6. evidence: concrete observable features only - colour, shape, size, pattern, position and texture of \
lesions, spots, holes, wilting, curling, visible insects, eggs, webbing or frass. No inferences here.
7. assumptions: anything you infer that is not directly visible.
8. differential: other conditions that could produce similar symptoms.
9. confidence: your rough certainty from 0 to 1. It is not calibrated - be conservative and use 0.5 \
or less when uncertain.
10. image_quality: good / fair / poor, with notes about blur, lighting, distance or occlusion.
Do not give treatment advice; another step handles that."""


class InvalidImage(ValueError):
    pass


@dataclass
class PreparedImage:
    jpeg: bytes
    sha256: str
    width: int
    height: int
    original_format: str | None
    original_bytes: int


def prepare_image(data: bytes, max_px: int) -> PreparedImage:
    """Validate, orient and downscale an upload to a JPEG suitable for the VLM."""
    if not data:
        raise InvalidImage("Empty image upload")
    try:
        with Image.open(BytesIO(data)) as probe:
            probe.verify()
        img = Image.open(BytesIO(data))
        fmt = img.format
        img = ImageOps.exif_transpose(img)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError) as exc:
        raise InvalidImage(f"Not a readable image: {exc}") from exc
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    buf = BytesIO()
    img.save(buf, "JPEG", quality=90, optimize=True)
    return PreparedImage(
        jpeg=buf.getvalue(),
        sha256=hashlib.sha256(data).hexdigest(),
        width=img.width,
        height=img.height,
        original_format=fmt,
        original_bytes=len(data),
    )


class VisionAnalyzer:
    def __init__(self, vlm: LLM):
        self.vlm = vlm

    def analyze(self, jpeg: bytes) -> VLMDiagnosis:
        result = self.vlm.structured(
            system=VLM_SYSTEM_PROMPT,
            turns=[ChatTurn("user", "Analyse this crop photograph and return the JSON assessment.")],
            images=[ImagePart(jpeg, "image/jpeg")],
            schema=VLMDiagnosis,
            temperature=0.1,
        )
        return normalize(result)


def normalize(r: VLMDiagnosis) -> VLMDiagnosis:
    """Enforce invariants the prompt asks for, in case the model drifts."""
    updates: dict = {"confidence": min(max(r.confidence, 0.0), 1.0)}
    if not r.is_plant_image:
        updates["health_status"] = "uncertain"
    if r.health_status == "healthy" and r.issue_categories:
        updates["issue_categories"] = []
    if r.health_status != "healthy" and r.severity == "none":
        updates["severity"] = "unknown"
    updates["evidence"] = [e.strip() for e in r.evidence if e.strip()][:10]
    return r.model_copy(update=updates)


def recapture_tips(r: VLMDiagnosis) -> list[str]:
    if r.health_status == "uncertain" or r.image_quality == "poor" or not r.is_plant_image:
        return RECAPTURE_TIPS
    return []
