#!/usr/bin/env python3
"""Profilage automatisé et benchmark reproductible (CPU, GPU, RAM, VRAM).

Calcule la moyenne, la médiane (P50) et le P95 sur l'exécution de classify.py.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np


def get_process_tree_pids(parent_pid: int) -> list[int]:
    """Récupère récursivement le PID parent et tous ses processus enfants."""
    pids = [parent_pid]
    try:
        out = subprocess.check_output(["pgrep", "-P", str(parent_pid)], text=True)
        children = [int(p) for p in out.strip().split() if p.isdigit()]
        for c in children:
            pids.extend(get_process_tree_pids(c))
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return pids


def get_tree_ram_mb(pids: list[int]) -> float:
    """Somme la mémoire physique RSS (en Mo) consommée par l'ensemble de l'arbre de processus."""
    total_kb = 0
    for pid in pids:
        status_file = Path(f"/proc/{pid}/status")
        if status_file.exists():
            try:
                for line in status_file.read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            total_kb += int(parts[1])
            except (OSError, PermissionError):
                pass
    return total_kb / 1024.0


def get_tree_cpu_ticks(pids: list[int]) -> int:
    """Somme le temps CPU (utime + stime en ticks) consommé par l'arbre de processus."""
    total_ticks = 0
    for pid in pids:
        stat_file = Path(f"/proc/{pid}/stat")
        if stat_file.exists():
            try:
                parts = stat_file.read_text().split()
                if len(parts) >= 15:
                    total_ticks += int(parts[13]) + int(parts[14])
            except (OSError, ValueError, PermissionError):
                pass
    return total_ticks


def compute_stats(data: list[float] | np.ndarray) -> dict[str, float]:
    arr = np.array(data, dtype=float)
    if len(arr) == 0:
        return {"min": 0.0, "mean": 0.0, "median": 0.0, "p95": 0.0, "max": 0.0}
    return {
        "min": round(float(np.min(arr)), 2),
        "mean": round(float(np.mean(arr)), 2),
        "median": round(float(np.median(arr)), 2),
        "p95": round(float(np.percentile(arr, 95)), 2),
        "max": round(float(np.max(arr)), 2),
    }


def profile_execution(report_path: Path, interval_ms: int = 50) -> dict:
    """Échantillonne en continu CPU, RAM, GPU Util et VRAM pendant l'exécution."""
    python_bin = sys.executable
    script_path = Path(__file__).parent / "classify.py"
    cmd = [python_bin, str(script_path), str(report_path)]

    # Démarrage du sous-processus nvidia-smi pour la télémétrie GPU à haute fréquence
    smi_cmd = [
        "nvidia-smi",
        "--query-gpu=utilization.gpu,memory.used",
        "--format=csv,noheader,nounits",
        f"-lms={interval_ms}",
    ]
    smi_proc = subprocess.Popen(smi_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    t_start = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    clk_tck = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", 100))
    cpu_samples = []
    ram_samples = []

    last_time = t_start
    pids = get_process_tree_pids(proc.pid)
    last_ticks = get_tree_cpu_ticks(pids)

    sleep_sec = interval_ms / 1000.0

    while proc.poll() is None:
        time.sleep(sleep_sec)
        now = time.perf_counter()
        dt = now - last_time
        current_pids = get_process_tree_pids(proc.pid)

        # Mesure RAM
        ram_mb = get_tree_ram_mb(current_pids)
        if ram_mb > 0:
            ram_samples.append(ram_mb)

        # Mesure CPU
        current_ticks = get_tree_cpu_ticks(current_pids)
        if dt > 0 and current_ticks >= last_ticks:
            cpu_percent = ((current_ticks - last_ticks) / clk_tck) / dt * 100.0
            cpu_samples.append(cpu_percent)

        last_ticks = current_ticks
        last_time = now

    stdout, stderr = proc.communicate()
    duration_total = time.perf_counter() - t_start

    # Arrêt du monitoring GPU
    smi_proc.terminate()
    try:
        smi_stdout, _ = smi_proc.communicate(timeout=1.0)
    except subprocess.TimeoutExpired:
        smi_proc.kill()
        smi_stdout, _ = smi_proc.communicate()

    gpu_util_samples = []
    vram_samples = []
    for line in smi_stdout.splitlines():
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if len(parts) >= 2:
            try:
                gpu_util_samples.append(float(parts[0]))
                vram_samples.append(float(parts[1]))
            except ValueError:
                pass

    return {
        "duration_seconds": round(duration_total, 3),
        "exit_code": proc.returncode,
        "sample_count": len(ram_samples),
        "cpu_percent": compute_stats(cpu_samples),
        "ram_mb": compute_stats(ram_samples),
        "gpu_util_percent": compute_stats(gpu_util_samples),
        "vram_mb": compute_stats(vram_samples),
        "raw_json_output": json.loads(stdout) if proc.returncode == 0 and stdout else None,
    }


def run_warm_benchmark(report_path: Path, iterations: int = 10) -> dict:
    """Simule un service persistant : charge le modèle une fois, puis mesure l'inférence pure."""
    import resource
    import torch
    from laya import Router
    from classify import extract_text
    from questions import QUESTIONS

    text = extract_text(report_path)
    router = Router()

    print("Initialisation du modèle en mémoire (boot du service)...", file=sys.stderr)
    t_boot = time.perf_counter()
    router.load("multilingual")
    boot_ms = (time.perf_counter() - t_boot) * 1000
    print(f"Service prêt en {boot_ms:.1f} ms. Lancement de la passe de chauffe (warmup)...", file=sys.stderr)

    # 1 run de chauffe (initialisation des graphes et kernels CUDA)
    router.predict(text, QUESTIONS, model="multilingual", max_len=8192)

    durations = []
    print(f"Benchmark de l'encodeur en régime établi ({iterations} requêtes)...", file=sys.stderr)
    for i in range(1, iterations + 1):
        if torch.cuda.is_available():
            torch.cuda.synchronize()
        t0 = time.perf_counter()
        router.predict(text, QUESTIONS, model="multilingual", max_len=8192)
        if torch.cuda.is_available():
            torch.cuda.synchronize()
        inf_ms = (time.perf_counter() - t0) * 1000
        durations.append(inf_ms)
        print(f"  [{i}/{iterations}] Inférence encodeur: {inf_ms:.2f} ms", file=sys.stderr)

    ram_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    vram_alloc = torch.cuda.max_memory_allocated() / (1024 ** 2) if torch.cuda.is_available() else 0.0

    return {
        "mode": "service_warm",
        "iterations": iterations,
        "boot_load_ms": round(boot_ms, 2),
        "inference_ms": compute_stats(durations),
        "ram_peak_mb": round(ram_peak, 2),
        "vram_allocated_peak_mb": round(vram_alloc, 2),
    }


def run_benchmark(report_path: Path, iterations: int = 10) -> dict:
    """Exécute N passes en sous-processus pour mesurer le comportement cold start CLI."""
    python_bin = sys.executable
    script_path = Path(__file__).parent / "classify.py"
    cmd = [python_bin, str(script_path), str(report_path)]

    inference_durations = []
    load_durations = []
    total_durations = []
    ram_peaks = []
    vram_peaks = []

    print(f"Lancement du benchmark cold-start ({iterations} itérations CLI)...", file=sys.stderr)

    for i in range(1, iterations + 1):
        t0 = time.perf_counter()
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        wall_ms = (time.perf_counter() - t0) * 1000

        if res.returncode == 0 and res.stdout:
            try:
                payload = json.loads(res.stdout)
                m = payload.get("metrics") or {}
                inf_ms = m.get("inference_duration_ms", m.get("duration_ms", wall_ms))
                load_ms = m.get("load_duration_ms", 0.0)
                tot_ms = m.get("total_duration_ms", wall_ms)
                inference_durations.append(inf_ms)
                load_durations.append(load_ms)
                total_durations.append(tot_ms)
                ram_peaks.append(m.get("ram_peak_mb", 0.0))
                vram_peaks.append(m.get("vram_allocated_peak_mb", 0.0))
                print(f"  [{i}/{iterations}] Inférence: {inf_ms:.1f} ms | Init: {load_ms:.1f} ms | Total: {tot_ms:.1f} ms", file=sys.stderr)
            except json.JSONDecodeError:
                total_durations.append(wall_ms)
        else:
            print(f"  [{i}/{iterations}] ÉCHEC (code {res.returncode})", file=sys.stderr)

    return {
        "mode": "cli_cold_start",
        "iterations": iterations,
        "inference_ms": compute_stats(inference_durations),
        "load_ms": compute_stats(load_durations),
        "total_ms": compute_stats(total_durations),
        "ram_peak_mb": compute_stats(ram_peaks),
        "vram_peak_mb": compute_stats(vram_peaks),
    }


def print_table(title: str, stats: dict[str, dict[str, float]], unit: str = "") -> None:
    print(f"\n### {title}")
    print(f"| Métrique | Min | Moyenne (Mean) | Médiane (P50) | P95 | Max |")
    print(f"| :--- | :--- | :--- | :--- | :--- | :--- |")
    for name, s in stats.items():
        u = f" {unit}" if unit else ""
        print(f"| **{name}** | {s['min']}{u} | {s['mean']}{u} | {s['median']}{u} | {s['p95']}{u} | {s['max']}{u} |")


def main() -> int:
    parser = argparse.ArgumentParser(description="Mesure statistique reproductible de classify.py (Moyenne, Médiane, P95).")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    # Mode 1: profilage continu d'une exécution
    prof_p = subparsers.add_parser("profile", help="Échantillonne en continu CPU, RAM, GPU, VRAM pendant un run.")
    prof_p.add_argument("report", type=Path, help="Fichier d'incident à classifier")
    prof_p.add_argument("--interval-ms", type=int, default=50, help="Intervalle d'échantillonnage en ms (défaut: 50ms)")
    prof_p.add_argument("--json", action="store_true", help="Sortie brute JSON")

    # Mode 2: benchmark sur N exécutions
    bench_p = subparsers.add_parser("benchmark", help="Exécute N passes pour mesurer la distribution de latence/mémoire.")
    bench_p.add_argument("report", type=Path, help="Fichier d'incident à classifier")
    bench_p.add_argument("-n", "--iterations", type=int, default=5, help="Nombre d'itérations (défaut: 5)")
    bench_p.add_argument("--warm", action="store_true", help="Simule un service en production (modèle gardé en VRAM, mesure l'inférence pure de l'encodeur)")
    bench_p.add_argument("--json", action="store_true", help="Sortie brute JSON")

    args = parser.parse_args()

    if args.mode == "profile":
        results = profile_execution(args.report, interval_ms=args.interval_ms)
        if args.json:
            print(json.dumps(results, indent=2))
        else:
            print(f"\n## Rapport de profilage continu (durée totale : {results['duration_seconds']}s, {results['sample_count']} échantillons)")
            stats_map = {
                "CPU (%)": results["cpu_percent"],
                "RAM (Mo)": results["ram_mb"],
                "GPU Util (%)": results["gpu_util_percent"],
                "VRAM (Mo)": results["vram_mb"],
            }
            print_table("Métriques d'échantillonnage pendant l'exécution", stats_map)
        return 0

    if args.mode == "benchmark":
        if args.warm:
            results = run_warm_benchmark(args.report, iterations=args.iterations)
            if args.json:
                print(json.dumps(results, indent=2))
            else:
                print(f"\n## Benchmark Mode Service / Warm ({results['iterations']} requêtes, boot modèle : {results['boot_load_ms']} ms)")
                stats_map = {
                    "Inférence pure Encodeur (ms)": results["inference_ms"],
                }
                print_table("Distribution de l'inférence en service persistant", stats_map)
                print(f"\n- **Pic RAM service :** {results['ram_peak_mb']} Mo")
                print(f"- **Pic VRAM allouée :** {results['vram_allocated_peak_mb']} Mo")
            return 0

        results = run_benchmark(args.report, iterations=args.iterations)
        if args.json:
            print(json.dumps(results, indent=2))
        else:
            print(f"\n## Résultats du benchmark sur {results['iterations']} itérations (Mode CLI)")
            stats_map = {
                "Inférence pure (ms)": results["inference_ms"],
                "Chargement modèle (ms)": results["load_ms"],
                "Durée totale (ms)": results["total_ms"],
                "Pic RAM (Mo)": results["ram_peak_mb"],
                "Pic VRAM (Mo)": results["vram_peak_mb"],
            }
            print_table("Distribution statistique des runs", stats_map)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
