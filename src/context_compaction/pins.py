"""Pinned versions and dataset snapshot, the single source for scripts and checks"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PACKAGES = {"mini-swe-agent": "2.4.0", "swebench": "5.0.2"}
PYTHON = (3, 12)

# swebench 5.x evaluates with the image, eval_script and log_parser fields,
# which only this dataset has; ids and problem statements match princeton-nlp
DATASET = "SWE-bench/SWE-bench_Verified"
DATASET_REVISION = "78f471bf655a3137b2e8a75af1501690ec009ec3"
DATASET_REMOTE_FILE = "data/test-00000-of-00001.parquet"
DATASET_SHA256 = "030cfd7f2a704c4c0226e7f104c725a3b41230b1d3517f9c915ad7ea5be3fa25"
DATASET_SIZE = 500

# harness and mini-swe-agent both accept this directory as the dataset name
DATASET_DIR = ROOT / "data" / "SWE-bench_Verified"
DATASET_PATH = DATASET_DIR / "test.parquet"

INSTANCES_PATH = ROOT / "configs" / "instances.json"
