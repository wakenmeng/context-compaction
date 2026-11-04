import hashlib
import json
import math
from fractions import Fraction

import pytest

import sample_instances as si
from context_compaction import pins
from sample_instances import allocate, sample

SIZES = {"a/x": 230, "b/y": 120, "c/z": 100, "d/w": 49, "e/v": 1}


def records(sizes=SIZES):
    return [
        {"instance_id": f"{repo.replace('/', '__')}-{i}", "repo": repo, "patch": f"p{i}"}
        for repo, size in sizes.items()
        for i in range(size)
    ]


# frozen contract: changing seed handling, hashing, salts or rounding breaks this
GOLDEN = "677db8258607be5a6acc456bac6fc767c7b4a56a65a5e7953c47578f1ae27b28"


def digest(result):
    return hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()


def test_golden_output_is_frozen():
    assert digest(sample(records())) == GOLDEN


def test_input_order_does_not_matter():
    assert sample(records()) == sample(list(reversed(records())))


def test_different_seed_changes_selection():
    assert sample(records(), seed=1)["formal"]["ids"] != sample(records(), seed=2)["formal"]["ids"]


def test_sizes_and_no_overlap():
    result = sample(records())
    smoke, formal = result["smoke"]["ids"], result["formal"]["ids"]
    assert len(smoke) == 20
    assert len(formal) == 100
    assert not set(smoke) & set(formal)
    assert result["formal"]["inclusion_probability"] == "1/5"


def test_allocation_is_floor_or_ceil_with_exact_total():
    for k in range(50):
        alloc = allocate(SIZES, 100, Fraction(k, 50))
        assert sum(alloc.values()) == 100
        for repo, n in alloc.items():
            quota = Fraction(SIZES[repo] * 100, 500)
            assert n in (math.floor(quota), math.ceil(quota))


def test_allocation_is_unbiased_over_offset():
    # quotas are multiples of 1/5, so a grid of 500 offsets averages exactly
    total = {repo: 0 for repo in SIZES}
    for k in range(500):
        for repo, n in allocate(SIZES, 100, Fraction(k, 500)).items():
            total[repo] += n
    for repo, size in SIZES.items():
        assert Fraction(total[repo], 500) == Fraction(size * 100, 500)


def test_single_instance_repo_can_enter_formal():
    hits = sum("e__v-0" in sample(records(), seed=s)["formal"]["ids"] for s in range(300))
    assert 30 < hits < 90  # expected 60 at probability 1/5


def test_smoke_covers_every_repo_with_leftovers():
    for seed in range(30):
        result = sample(records(), seed=seed)
        repos = {i.split("-")[0].replace("__", "/") for i in result["smoke"]["ids"]}
        in_formal = "e__v-0" in result["formal"]["ids"]
        expected = set(SIZES) - ({"e/v"} if in_formal else set())
        assert repos == expected
        assert result["smoke"]["missing_formal_repos"] == (["e/v"] if in_formal else [])


def test_fingerprint_sees_content_changes():
    changed = records()
    changed[0] = {**changed[0], "patch": "other"}
    assert sample(changed)["records_sha256"] != sample(records())["records_sha256"]


def test_fingerprint_sees_repo_changes():
    changed = records()
    changed[0] = {**changed[0], "repo": "b/y"}
    assert sample(changed)["records_sha256"] != sample(records())["records_sha256"]


def test_duplicate_ids_are_rejected():
    with pytest.raises(ValueError):
        sample(records() + records()[:1])


def test_oversized_sample_is_rejected():
    with pytest.raises(ValueError):
        allocate({"a/x": 5}, 6, Fraction(0))
    with pytest.raises(ValueError):
        sample(records({"a/x": 50}))


@pytest.mark.parametrize("suffix", [".json", ".jsonl", ".parquet"])
def test_cli_loads_formats_and_creates_dirs(tmp_path, suffix):
    data = tmp_path / f"data{suffix}"
    rows = records()
    if suffix == ".json":
        data.write_text(json.dumps(rows))
    elif suffix == ".jsonl":
        data.write_text("\n".join(json.dumps(r) for r in rows))
    else:
        import pyarrow as pa
        import pyarrow.parquet as pq

        pq.write_table(pa.Table.from_pylist(rows), data)
    out = tmp_path / "new" / "dir" / "instances.json"
    si.main(["--input", str(data), "--output", str(out)])
    written = json.loads(out.read_text())
    assert written["formal"]["ids"] == sample(rows)["formal"]["ids"]
    assert written["source"]["dataset"] == "local"


def test_cli_is_idempotent_and_refuses_overwrite(tmp_path, capsys):
    data = tmp_path / "data.json"
    data.write_text(json.dumps(records()))
    out = tmp_path / "instances.json"
    si.main(["--input", str(data), "--output", str(out)])
    si.main(["--input", str(data), "--output", str(out)])
    assert "unchanged" in capsys.readouterr().out
    out.write_text("{}\n")
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        si.main(["--input", str(data), "--output", str(out)])
    assert out.read_text() == "{}\n"


@pytest.mark.skipif(
    not (pins.DATASET_PATH.exists() and pins.INSTANCES_PATH.exists()),
    reason="needs the pinned snapshot and configs/instances.json",
)
def test_frozen_config_matches_regeneration(tmp_path):
    out = tmp_path / "instances.json"
    si.main(["--output", str(out)])
    assert out.read_text() == pins.INSTANCES_PATH.read_text()
