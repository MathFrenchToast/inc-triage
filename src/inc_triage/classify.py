import argparse
import json
import resource
import subprocess
import sys
import time
from pathlib import Path

import torch
from laya import Router

try:
    from inc_triage.questions import QUESTIONS
except ImportError:
    from questions import QUESTIONS


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()

    if suffix in [".txt", ".md"]:
        return path.read_text(encoding="utf-8")



    raise ValueError(f"Format non supporté : {suffix}")


def decide(scores: dict[str, float]) -> str:
    # Pas assez d'information => humain
    if scores["sufficient_information"] < 0.60:
        return "REVIEW"

    # Sécurité / données => escalade immédiate
    if scores["security"] >= 0.80:
        return "CRITICAL"

    if scores["data_loss"] >= 0.80:
        return "CRITICAL"

    # Production fortement indisponible sans workaround
    if (
        scores["production"] >= 0.80
        and scores["unavailable"] >= 0.75
        and scores["no_workaround"] >= 0.60
    ):
        return "CRITICAL"

    # Production + impact large + SLA
    if (
        scores["production"] >= 0.80
        and scores["wide_impact"] >= 0.70
        and scores["sla_risk"] >= 0.70
    ):
        return "CRITICAL"

    # Incident prod significatif
    if (
        scores["production"] >= 0.70
        and (
            scores["unavailable"] >= 0.50
            or scores["wide_impact"] >= 0.50
        )
    ):
        return "MAJOR"

    return "MINOR"
def print_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False), flush=True)


def classify(path: Path) -> dict:
    t0 = time.perf_counter()
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()

    text = extract_text(path)
    router = Router()

    print("Chargement du modèle…", file=sys.stderr, flush=True)
    t_load_start = time.perf_counter()
    router.load("multilingual")
    load_duration_ms = (time.perf_counter() - t_load_start) * 1000

    print("Analyse de l'incident (inférence)…", file=sys.stderr, flush=True)
    t_inf_start = time.perf_counter()
    result = router.predict(
        text,
        QUESTIONS,
        model="multilingual",
        max_len=8192,
    )
    inference_duration_ms = (time.perf_counter() - t_inf_start) * 1000
    total_duration_ms = (time.perf_counter() - t0) * 1000

    scores = {
        name: result["answers"][name]["noul"]
        for name in QUESTIONS
    }

    ram_peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    vram_alloc_mb = torch.cuda.max_memory_allocated() / (1024 ** 2) if torch.cuda.is_available() else 0.0
    vram_res_mb = torch.cuda.max_memory_reserved() / (1024 ** 2) if torch.cuda.is_available() else 0.0

    return {
        "file": str(path),
        "classification": decide(scores),
        "scores": scores,
        "metrics": {
            "load_duration_ms": round(load_duration_ms, 2),
            "inference_duration_ms": round(inference_duration_ms, 2),
            "total_duration_ms": round(total_duration_ms, 2),
            "ram_peak_mb": round(ram_peak_mb, 2),
            "vram_allocated_peak_mb": round(vram_alloc_mb, 2),
            "vram_reserved_peak_mb": round(vram_res_mb, 2),
        },
    }


def worker(path: Path) -> int:
    try:
        print_json(classify(path))
    except (OSError, ValueError, KeyError) as error:
        print_json({
            "file": str(path),
            "classification": None,
            "scores": None,
            "metrics": None,
            "status": "error",
            "error": str(error),
        })
        return 1
    return 0


def run_worker(path: Path) -> int:
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", str(path)]
    completed = subprocess.run(command, stdout=subprocess.PIPE, text=True)

    if completed.returncode == 0:
        sys.stdout.write(completed.stdout)
        return 0

    if completed.stdout:
        sys.stdout.write(completed.stdout)
        return completed.returncode

    if completed.returncode < 0:
        error = (
            "Le processus du modèle a été interrompu par le système, probablement faute de "
            "mémoire. Le checkpoint multilingual requiert davantage de RAM disponible."
        )
    else:
        error = "Le processus du modèle s'est terminé avant de produire un résultat."

    print_json({
        "file": str(path),
        "classification": None,
        "scores": None,
        "metrics": None,
        "status": "error",
        "error": error,
    })
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Classe un rapport d'incident au format JSON.")
    parser.add_argument("path", type=Path, help="Rapport .md ou .txt à classifier")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    return worker(args.path) if args.worker else run_worker(args.path)


if __name__ == "__main__":
    raise SystemExit(main())
