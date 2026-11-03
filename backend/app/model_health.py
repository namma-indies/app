"""Local model-presence check shared by private health and maintenance tools."""
import argparse
import json
from pathlib import Path
from urllib.request import urlopen

MODEL_DIR = Path(__file__).resolve().parent / "ml"
MODEL_NAMES = ("miewid_msv3.onnx", "yolo26x.onnx")


def local_reid_health():
    models = {}
    for name in MODEL_NAMES:
        try:
            path = MODEL_DIR / name
            models[name] = path.is_file() and path.stat().st_size > 0
        except OSError:
            models[name] = False
    return {"status": "ok" if all(models.values()) else "unavailable", "models": models}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="also require local API liveness after restart")
    args = parser.parse_args()
    result = local_reid_health()
    if args.live:
        try:
            with urlopen("http://localhost:8000/health", timeout=3) as response:
                if response.status != 200 or json.loads(response.read(4096))["status"] != "ok":
                    result["status"] = "unavailable"
        except Exception:
            result["status"] = "unavailable"
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
