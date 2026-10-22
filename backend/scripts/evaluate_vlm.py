"""Evaluate Pipeline B's vision step on labelled leaf photos (a PlantVillage sample).

    python -m scripts.evaluate_vlm                        # configured VLM (Gemini if GEMINI_API_KEY is set)
    python -m scripts.evaluate_vlm --per-class 6 --out ../docs/evaluation-vlm-local.json
    python -m scripts.evaluate_vlm --download-only        # only fetch and cache the image sample
    python -m scripts.evaluate_vlm --sleep 6              # stay under a free-tier requests-per-minute limit

The images come from the open-access PlantVillage dataset (Mohanty, Hughes & Salathe 2016,
https://github.com/spMohanty/PlantVillage-Dataset). They are downloaded on first use into
data/vlm-eval/ and are not redistributed with GAIA. The sample is seeded, so every run and every
model sees the same images.

What it measures, per image and overall:
  * plant identification - does the reported plant (or scientific name) match the crop?
  * health status        - healthy vs unhealthy; "uncertain" is reported separately and counts as
                           not correct in the strict accuracy
  * missed disease rate  - diseased leaves reported as healthy (the costly error)
  * false alarm rate     - healthy leaves reported as unhealthy
  * disease named        - is the labelled disease named in the diagnosis (top-1) or anywhere in the
                           diagnosis + differential (top-k)?
  * calibration          - expected calibration error and Brier score of the self-reported confidence
                           against health-status correctness, plus a reliability table
  * latency              - per image

PlantVillage photos show single leaves on a plain background, so these numbers are an optimistic
upper bound for real field photos taken by the rover.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gaia.config import get_settings  # noqa: E402
from gaia.pipeline_b.vlm import VisionAnalyzer, VLMDiagnosis, prepare_image  # noqa: E402
from gaia.providers.llm import build_llm  # noqa: E402

REPO = "spMohanty/PlantVillage-Dataset"
LIST_URL = "https://api.github.com/repos/" + REPO + "/contents/raw/color/{folder}"
RAW_URL = "https://raw.githubusercontent.com/" + REPO + "/master/raw/color/{folder}/{name}"
USER_AGENT = "gaia-evaluate-vlm"


@dataclass(frozen=True)
class LeafClass:
    folder: str  # PlantVillage folder name
    crop: str  # key into CROP_PATTERNS
    disease: str | None  # None for healthy leaves
    disease_patterns: tuple[str, ...] = ()  # any match in the text counts as naming the disease

    @property
    def healthy(self) -> bool:
        return self.disease is None


# Crops a smallholder in India is likely to grow; 4 healthy and 6 diseased classes.
CLASSES = [
    LeafClass("Tomato___healthy", "tomato", None),
    LeafClass("Tomato___Early_blight", "tomato", "early blight", (r"early blight", r"alternaria")),
    LeafClass("Tomato___Late_blight", "tomato", "late blight", (r"late blight", r"phytophthora")),
    LeafClass("Tomato___Tomato_Yellow_Leaf_Curl_Virus", "tomato", "yellow leaf curl virus",
              (r"yellow leaf curl", r"tylcv", r"leaf curl virus")),
    LeafClass("Potato___healthy", "potato", None),
    LeafClass("Potato___Late_blight", "potato", "late blight", (r"late blight", r"phytophthora")),
    LeafClass("Corn_(maize)___healthy", "maize", None),
    LeafClass("Corn_(maize)___Common_rust_", "maize", "common rust", (r"rust", r"puccinia")),
    LeafClass("Pepper,_bell___healthy", "bell pepper", None),
    LeafClass("Pepper,_bell___Bacterial_spot", "bell pepper", "bacterial spot",
              (r"bacterial (leaf )?spot", r"xanthomonas")),
]

CROP_PATTERNS = {
    "tomato": (r"tomato", r"solanum lycopersicum", r"lycopersicon"),
    "potato": (r"(?<!sweet )potato", r"solanum tuberosum"),
    "maize": (r"maize", r"corn", r"zea mays"),
    "bell pepper": (r"pepper", r"capsicum"),
}

CALIBRATION_BINS = 5


def mentions(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{p}\b", text, re.IGNORECASE) for p in patterns)


def score_case(cls: LeafClass, r: VLMDiagnosis) -> dict:
    """Per-image correctness flags; pure, so it is unit-tested without a model."""
    truth = "healthy" if cls.healthy else "unhealthy"
    plant_text = " ".join(filter(None, [r.plant, r.plant_scientific_name]))
    named_top1 = named_topk = None
    if not cls.healthy:
        named_top1 = mentions(r.diagnosis, cls.disease_patterns)
        named_topk = named_top1 or mentions(" ".join(r.differential), cls.disease_patterns)
    return {
        "plant_ok": r.is_plant_image and mentions(plant_text, CROP_PATTERNS[cls.crop]),
        "status_ok": r.health_status == truth,
        "disease_named_top1": named_top1,
        "disease_named_topk": named_topk,
    }


def _share(rows: list[dict], pred) -> float | None:
    return sum(1 for r in rows if pred(r)) / len(rows) if rows else None


def calibration(rows: list[dict], bins: int = CALIBRATION_BINS) -> tuple[float | None, float | None, list[dict]]:
    """Expected calibration error, Brier score and a reliability table for confidence vs status_ok."""
    if not rows:
        return None, None, []
    table = []
    ece = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        in_bin = [r for r in rows if lo <= r["confidence"] < hi or (b == bins - 1 and r["confidence"] == 1.0)]
        if not in_bin:
            continue
        conf = statistics.fmean(r["confidence"] for r in in_bin)
        acc = statistics.fmean(1.0 if r["status_ok"] else 0.0 for r in in_bin)
        ece += len(in_bin) / len(rows) * abs(acc - conf)
        table.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(in_bin), "mean_confidence": round(conf, 3),
                      "accuracy": round(acc, 3)})
    brier = statistics.fmean((r["confidence"] - (1.0 if r["status_ok"] else 0.0)) ** 2 for r in rows)
    return ece, brier, table


def summarize(rows: list[dict]) -> dict:
    """Aggregate metrics over scored rows; rows with an "error" are counted but not scored."""
    ok = [r for r in rows if not r.get("error")]
    healthy = [r for r in ok if r["healthy"]]
    diseased = [r for r in ok if not r["healthy"]]
    decided = [r for r in ok if r["health_status"] != "uncertain"]
    ece, brier, _ = calibration(ok)
    latencies = sorted(r["latency_ms"] for r in ok)
    correct_conf = [r["confidence"] for r in ok if r["status_ok"]]
    wrong_conf = [r["confidence"] for r in ok if not r["status_ok"]]
    return {
        "images": len(rows),
        "scored": len(ok),
        "errors": len(rows) - len(ok),
        "healthy_images": len(healthy),
        "diseased_images": len(diseased),
        "plant_id_accuracy": _share(ok, lambda r: r["plant_ok"]),
        "status_accuracy": _share(ok, lambda r: r["status_ok"]),
        "status_accuracy_when_decided": _share(decided, lambda r: r["status_ok"]),
        "uncertain_rate": _share(ok, lambda r: r["health_status"] == "uncertain"),
        "sensitivity": _share(diseased, lambda r: r["health_status"] == "unhealthy"),
        "missed_disease_rate": _share(diseased, lambda r: r["health_status"] == "healthy"),
        "specificity": _share(healthy, lambda r: r["health_status"] == "healthy"),
        "false_alarm_rate": _share(healthy, lambda r: r["health_status"] == "unhealthy"),
        "disease_named_top1": _share(diseased, lambda r: r["disease_named_top1"]),
        "disease_named_topk": _share(diseased, lambda r: r["disease_named_topk"]),
        "not_plant_rate": _share(ok, lambda r: not r["is_plant_image"]),
        "expected_calibration_error": ece,
        "brier_score": brier,
        "mean_confidence_when_correct": statistics.fmean(correct_conf) if correct_conf else None,
        "mean_confidence_when_wrong": statistics.fmean(wrong_conf) if wrong_conf else None,
        "median_latency_ms": statistics.median(latencies) if latencies else None,
        "p90_latency_ms": latencies[math.ceil(0.9 * len(latencies)) - 1] if latencies else None,
    }


def per_class(rows: list[dict]) -> list[dict]:
    out = []
    for cls in CLASSES:
        rs = [r for r in rows if r["class"] == cls.folder and not r.get("error")]
        if not rs:
            continue
        out.append({
            "class": cls.folder,
            "n": len(rs),
            "plant_id_accuracy": _share(rs, lambda r: r["plant_ok"]),
            "status_accuracy": _share(rs, lambda r: r["status_ok"]),
            "uncertain": sum(r["health_status"] == "uncertain" for r in rs),
            "disease_named_top1": None if cls.healthy else _share(rs, lambda r: r["disease_named_top1"]),
            "disease_named_topk": None if cls.healthy else _share(rs, lambda r: r["disease_named_topk"]),
        })
    return out


# ---------------------------------------------------------------- dataset sample

def _get(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except OSError:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def load_sample(cache: Path, per_class_n: int, seed: int) -> list[tuple[LeafClass, Path]]:
    """Pick a seeded sample per class (listing cached in manifest.json) and download missing images."""
    manifest_path = cache / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    sample = []
    for cls in CLASSES:
        names = manifest.get(cls.folder)
        if not names:
            listing = json.loads(_get(LIST_URL.format(folder=urllib.parse.quote(cls.folder))))
            names = sorted(e["name"] for e in listing if e.get("type") == "file")
            manifest[cls.folder] = names
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        picked = random.Random(f"{seed}:{cls.folder}").sample(names, min(per_class_n, len(names)))
        for name in picked:
            path = cache / cls.folder / name
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                url = RAW_URL.format(folder=urllib.parse.quote(cls.folder), name=urllib.parse.quote(name))
                path.write_bytes(_get(url))
            sample.append((cls, path))
    return sample


# ---------------------------------------------------------------- run

def _fmt(key: str, value) -> str:
    if value is None:
        return "n/a"
    if key.endswith("_ms"):
        return f"{value / 1000:.1f} s"
    if isinstance(value, float) and ("confidence" in key or key in ("expected_calibration_error", "brier_score")):
        return f"{value:.3f}"
    if isinstance(value, float):
        return f"{value:.1%}"
    return str(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--per-class", type=int, default=6, help="images per class (10 classes)")
    parser.add_argument("--seed", type=int, default=7, help="sampling seed; keep it fixed to compare models")
    parser.add_argument("--out", type=Path, help="write a JSON report here")
    parser.add_argument("--sleep", type=float, default=0.0, help="seconds to wait between VLM calls")
    parser.add_argument("--resume", action="store_true",
                        help="reuse results for this model from the last (interrupted) run")
    parser.add_argument("--download-only", action="store_true", help="fetch the images and stop")
    args = parser.parse_args()

    settings = get_settings()
    cache = settings.data_dir / "vlm-eval"
    print(f"Loading the PlantVillage sample ({args.per_class} per class, seed {args.seed}) into {cache} ...")
    sample = load_sample(cache, args.per_class, args.seed)
    print(f"{len(sample)} images across {len(CLASSES)} classes.")
    if args.download_only:
        return

    vlm = build_llm(settings, "vision")
    if not vlm.available:
        sys.exit(f"No vision model is configured ({getattr(vlm, 'reason', 'unavailable')}). "
                 "Set GEMINI_API_KEY, or VLM_PROVIDER=openai_compat with a vision model, in backend/.env.")
    analyzer = VisionAnalyzer(vlm)
    print(f"VLM: {vlm.model_id}\n")

    checkpoint = cache / "last-run.jsonl"
    done: dict[str, dict] = {}
    if args.resume and checkpoint.exists():
        for line in checkpoint.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("model") == vlm.model_id and not row.get("error"):
                done[row["file"]] = row
    elif checkpoint.exists():
        checkpoint.unlink()

    rows = []
    for i, (cls, path) in enumerate(sample, 1):
        key = f"{cls.folder}/{path.name}"
        if key in done:
            rows.append(done[key])
            continue
        row: dict = {"file": key, "class": cls.folder, "crop": cls.crop, "healthy": cls.healthy,
                     "disease": cls.disease, "model": vlm.model_id,
                     "source_url": RAW_URL.format(folder=urllib.parse.quote(cls.folder),
                                                  name=urllib.parse.quote(path.name))}
        t0 = time.perf_counter()
        try:
            r = analyzer.analyze(prepare_image(path.read_bytes(), settings.vlm_max_image_px).jpeg)
        except Exception as exc:  # a failed call is reported, not fatal for the whole run
            row.update(error=f"{type(exc).__name__}: {exc}"[:300], latency_ms=int((time.perf_counter() - t0) * 1000))
            print(f"[{i:3d}/{len(sample)}] ERROR {key}: {row['error']}")
        else:
            row.update(
                latency_ms=int((time.perf_counter() - t0) * 1000),
                is_plant_image=r.is_plant_image, plant=r.plant, plant_scientific_name=r.plant_scientific_name,
                health_status=r.health_status, diagnosis=r.diagnosis, differential=r.differential,
                confidence=r.confidence, image_quality=r.image_quality, **score_case(cls, r),
            )
            mark = "OK  " if row["status_ok"] else "MISS"
            print(f"[{i:3d}/{len(sample)}] {mark} {cls.folder:40s} -> {r.plant} | {r.health_status} | "
                  f"{r.diagnosis[:60]} | conf {r.confidence:.2f} | {row['latency_ms'] / 1000:.1f} s")
        rows.append(row)
        with checkpoint.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        if args.sleep:
            time.sleep(args.sleep)

    summary = {"vlm": vlm.model_id, "images_per_class": args.per_class, "seed": args.seed, **summarize(rows)}
    _, _, reliability = calibration([r for r in rows if not r.get("error")])
    print("\nSummary:")
    for k, v in summary.items():
        print(f"  {k:30s} {_fmt(k, v)}")
    print("\nReliability (confidence vs health-status correctness):")
    for b in reliability:
        print(f"  {b['bin']}: n={b['n']:3d}  mean confidence {b['mean_confidence']:.2f}  accuracy {b['accuracy']:.2f}")
    if args.out:
        report = {
            "summary": summary,
            "per_class": per_class(rows),
            "reliability": reliability,
            "dataset": {
                "name": "PlantVillage (color)",
                "source": f"https://github.com/{REPO}",
                "citation": "Mohanty, Hughes & Salathe (2016), Using deep learning for image-based plant "
                            "disease detection, Frontiers in Plant Science 7:1419, doi:10.3389/fpls.2016.01419",
                "note": "Images are downloaded at run time and not redistributed with GAIA.",
            },
            "cases": rows,
        }
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nReport written to {args.out}")


if __name__ == "__main__":
    main()
