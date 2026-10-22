import pytest

from gaia.pipeline_b.vlm import VLMDiagnosis
from scripts.evaluate_vlm import CLASSES, CROP_PATTERNS, calibration, per_class, score_case, summarize

BY_FOLDER = {c.folder: c for c in CLASSES}


def _vlm(**kw) -> VLMDiagnosis:
    base = {"is_plant_image": True, "plant": "Tomato", "health_status": "unhealthy",
            "diagnosis": "Possible early blight", "evidence": ["Brown concentric lesions"], "confidence": 0.8,
            "image_quality": "good"}
    return VLMDiagnosis(**{**base, **kw})


def _row(folder: str, status: str, conf: float, **flags) -> dict:
    cls = BY_FOLDER[folder]
    truth = "healthy" if cls.healthy else "unhealthy"
    return {"class": folder, "healthy": cls.healthy, "health_status": status, "confidence": conf,
            "is_plant_image": True, "plant_ok": True, "status_ok": status == truth, "latency_ms": 1000,
            "disease_named_top1": flags.get("top1"), "disease_named_topk": flags.get("topk")}


def test_class_table_is_consistent():
    assert sum(c.healthy for c in CLASSES) == 4 and sum(not c.healthy for c in CLASSES) == 6
    for c in CLASSES:
        assert c.crop in CROP_PATTERNS
        assert bool(c.disease_patterns) == (not c.healthy)


def test_disease_named_in_diagnosis_or_only_in_differential():
    blight = BY_FOLDER["Tomato___Early_blight"]
    assert score_case(blight, _vlm()) == {
        "plant_ok": True, "status_ok": True, "disease_named_top1": True, "disease_named_topk": True,
    }
    s = score_case(blight, _vlm(diagnosis="Possible Septoria leaf spot", differential=["Early blight (Alternaria)"]))
    assert (s["disease_named_top1"], s["disease_named_topk"]) == (False, True)
    healthy = score_case(BY_FOLDER["Tomato___healthy"], _vlm(health_status="uncertain"))
    assert healthy["status_ok"] is False and healthy["disease_named_top1"] is None


def test_crop_matching_uses_scientific_name_and_rejects_look_alikes():
    potato = BY_FOLDER["Potato___healthy"]
    assert score_case(potato, _vlm(plant="Sweet potato", health_status="healthy"))["plant_ok"] is False
    assert score_case(potato, _vlm(plant="Unknown", plant_scientific_name="Solanum tuberosum"))["plant_ok"] is True
    assert score_case(BY_FOLDER["Corn_(maize)___healthy"], _vlm(plant="Maize (corn)"))["plant_ok"] is True
    assert score_case(BY_FOLDER["Tomato___healthy"], _vlm(is_plant_image=False))["plant_ok"] is False


def test_summary_rates_and_calibration():
    rows = [
        _row("Tomato___healthy", "healthy", 0.9),
        _row("Potato___healthy", "unhealthy", 0.8),  # false alarm
        _row("Tomato___Early_blight", "unhealthy", 0.7, top1=True, topk=True),
        _row("Tomato___Late_blight", "uncertain", 0.3, top1=False, topk=False),
        {"class": "Tomato___Late_blight", "healthy": False, "error": "timeout"},
    ]
    s = summarize(rows)
    assert (s["images"], s["scored"], s["errors"]) == (5, 4, 1)
    assert s["status_accuracy"] == pytest.approx(0.5)
    assert s["status_accuracy_when_decided"] == pytest.approx(2 / 3)
    assert s["uncertain_rate"] == pytest.approx(0.25)
    assert (s["sensitivity"], s["missed_disease_rate"]) == (pytest.approx(0.5), pytest.approx(0.0))
    assert (s["specificity"], s["false_alarm_rate"]) == (pytest.approx(0.5), pytest.approx(0.5))
    assert s["disease_named_top1"] == pytest.approx(0.5)
    # bins 0.8-1.0: conf .85 vs acc .5; 0.6-0.8: .7 vs 1; 0.2-0.4: .3 vs 0
    assert s["expected_calibration_error"] == pytest.approx(0.5 * 0.35 + 0.25 * 0.3 + 0.25 * 0.3)
    assert s["brier_score"] == pytest.approx((0.01 + 0.64 + 0.09 + 0.09) / 4)
    assert s["mean_confidence_when_correct"] == pytest.approx(0.8)

    table = per_class(rows)
    assert [r["class"] for r in table][:2] == ["Tomato___healthy", "Tomato___Early_blight"]
    late = next(r for r in table if r["class"] == "Tomato___Late_blight")
    assert (late["n"], late["uncertain"]) == (1, 1)


def test_full_confidence_lands_in_the_top_bin():
    ece, brier, table = calibration([{"confidence": 1.0, "status_ok": True}])
    assert (ece, brier) == (0.0, 0.0)
    assert table == [{"bin": "0.8-1.0", "n": 1, "mean_confidence": 1.0, "accuracy": 1.0}]
