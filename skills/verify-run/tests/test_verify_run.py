"""Correctness tests for verify-run (synthetic data, offline)."""

import json
import re
import shutil
import sys

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from PIL import Image
from weather_skills_core.provenance import input_ref, load_history, stamp_figure, stamp_zarr


@pytest.fixture(scope="module")
def clip_region():
    return load_skill("clip-region", "clip").clip_region


@pytest.fixture(scope="module")
def aggregate():
    return load_skill("aggregate-temporal", "aggregate").aggregate


@pytest.fixture(scope="module")
def vr_module():
    return load_skill("verify-run", "verify_run")


@pytest.fixture
def verify_run(vr_module, monkeypatch):
    # Replay normally shells out via `uv run --script`, which resolves the
    # skill's inline deps from the network. Run sibling scripts with the test
    # interpreter (the dev env already carries every skill's imports) instead.
    monkeypatch.setattr(vr_module, "_runner", lambda script: [sys.executable, str(script)])
    return vr_module.verify_run


def _synthetic_input(path):
    ds = make_gridded(n_time=14)
    rng = np.random.default_rng(42)
    ds["precip"].values = rng.gamma(2.0, 3.0, size=ds["precip"].shape)
    return write_zarr(ds, path)


def _chain(tmp_path, clip_region, aggregate):
    """in.zarr (no history) -> clip-region -> clipped.zarr -> aggregate-temporal -> weekly.zarr."""
    src = _synthetic_input(tmp_path / "in.zarr")
    clipped = tmp_path / "clipped.zarr"
    weekly = tmp_path / "weekly.zarr"
    run_skill(clip_region, "-i", str(src), "-o", str(clipped), "--bbox", "3/10/1/12")
    run_skill(aggregate, "-i", str(clipped), "-o", str(weekly), "--period", "weekly")
    return src, clipped, weekly


def _gate(fn, *argv):
    """Run verify-run; return its exit code (0 when it returns normally)."""
    try:
        run_skill(fn, *argv)
    except SystemExit as exc:
        return exc.code
    return 0


def _has_check(out, status, text):
    """True if the card lists ``[status] C<n> text``.

    Check ids are not asserted: INFO checks (e.g. a ``dirty`` note when the
    repo has uncommitted changes) shift the numbering of later checks.
    """
    return re.search(rf"^\s*\[{status}\] C\d+ {re.escape(text)}", out, re.MULTILINE) is not None


def _rewrite_values(path, change):
    """Rewrite a Zarr's data in place (attrs, incl. provenance, untouched)."""
    ds = xr.open_zarr(path, consolidated=True).load()
    ds["precip"].values = change(ds["precip"].values)
    tmp = path.with_name(path.name + ".tmp")
    for name in ds.variables:
        ds[name].encoding = {}
    ds.to_zarr(tmp, mode="w", consolidated=True)
    shutil.rmtree(path)
    tmp.rename(path)


def test_pass_on_untouched_chain(tmp_path, clip_region, aggregate, verify_run, capsys):
    _src, _clipped, weekly = _chain(tmp_path, clip_region, aggregate)

    code = _gate(verify_run, "-i", str(weekly))

    out = capsys.readouterr().out
    assert code == 0
    assert "VERDICT  : PASS" in out
    assert "replay not requested" in out
    assert out.count("[PASS] ") == 3  # provenance + two input hashes
    assert "step 1 clip-region <- in.zarr" in out
    assert "step 2 aggregate-temporal <- clipped.zarr" in out


def test_block_when_input_modified_after_the_fact(
    tmp_path, clip_region, aggregate, verify_run, capsys
):
    src, _clipped, weekly = _chain(tmp_path, clip_region, aggregate)
    _rewrite_values(src, lambda v: v + 1.0)

    code = _gate(verify_run, "-i", str(weekly))

    out = capsys.readouterr().out
    assert code == 1
    assert "VERDICT  : BLOCK" in out
    assert _has_check(out, "BLOCK", "input-hash: step 1 clip-region <- in.zarr")
    assert "changed since the step ran" in out
    assert _has_check(out, "PASS", "input-hash: step 2 aggregate-temporal <- clipped.zarr")


def test_unverifiable_when_provenance_absent(tmp_path, verify_run, capsys):
    bare = _synthetic_input(tmp_path / "bare.zarr")

    code = _gate(verify_run, "-i", str(bare), "--replay")

    out = capsys.readouterr().out
    assert code == 2
    assert "VERDICT  : UNVERIFIABLE" in out
    assert "no weather_skills_history recorded" in out
    assert "[PASS]" not in out


def test_unverifiable_when_artifact_missing(tmp_path, verify_run, capsys):
    code = _gate(verify_run, "-i", str(tmp_path / "nope.zarr"))

    assert code == 2
    assert "does not exist" in capsys.readouterr().out


def test_json_gate_preserves_actual_unverifiable_verdict(tmp_path, verify_run, capsys):
    path = tmp_path / "missing.zarr"
    assert _gate(verify_run, "-i", str(path), "--require-replay", "--format", "json") == 2
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == "verify-run.gate/1"
    assert report["exit_code"] == 2 and report["verdict"] == "UNVERIFIABLE"
    assert report["artifact"] == str(path.resolve())


def test_unverifiable_when_input_gone(tmp_path, clip_region, aggregate, verify_run, capsys):
    _src, clipped, weekly = _chain(tmp_path, clip_region, aggregate)
    shutil.rmtree(clipped)

    code = _gate(verify_run, "-i", str(weekly))

    out = capsys.readouterr().out
    assert code == 2
    assert "VERDICT  : UNVERIFIABLE" in out
    assert _has_check(out, "UNVERIFIABLE", "input-hash: step 2 aggregate-temporal <- clipped.zarr")


def test_search_dir_finds_relocated_input(tmp_path, clip_region, aggregate, verify_run, capsys):
    _src, clipped, weekly = _chain(tmp_path, clip_region, aggregate)
    elsewhere = tmp_path / "archive"
    elsewhere.mkdir()
    shutil.move(str(clipped), str(elsewhere / clipped.name))

    code = _gate(verify_run, "-i", str(weekly), "--search-dir", str(elsewhere))

    assert code == 0, capsys.readouterr().out


def test_fetch_only_chain_is_unverifiable(tmp_path, verify_run, capsys):
    ds = make_gridded()
    stamp_zarr(
        ds,
        [{"skill": "chirps-fetch", "version": "0.0.2", "args": {"bbox": "1/2/3/4"}, "input": None}],
    )
    path = tmp_path / "fetched.zarr"
    ds.to_zarr(path, mode="w", consolidated=True)

    code = _gate(verify_run, "-i", str(path))

    out = capsys.readouterr().out
    assert code == 2
    assert "no input or replay check could be run" in out


def test_replay_pass(tmp_path, clip_region, aggregate, verify_run, capsys):
    _src, _clipped, weekly = _chain(tmp_path, clip_region, aggregate)

    code = _gate(verify_run, "-i", str(weekly), "--replay")

    out = capsys.readouterr().out
    assert code == 0, out
    assert _has_check(out, "PASS", "replay-step: replay step 1 clip-region")
    assert _has_check(out, "PASS", "replay: replay of weekly.zarr")
    assert "bit-identical values" in out


def test_replay_blocks_when_output_edited(tmp_path, clip_region, aggregate, verify_run, capsys):
    """Inputs intact, provenance intact, but the artifact's numbers were changed."""
    _src, _clipped, weekly = _chain(tmp_path, clip_region, aggregate)
    _rewrite_values(weekly, lambda v: v * 2.0)

    assert _gate(verify_run, "-i", str(weekly)) == 0  # hash checks cannot see it
    capsys.readouterr()

    code = _gate(verify_run, "-i", str(weekly), "--replay")

    out = capsys.readouterr().out
    assert code == 1
    assert _has_check(out, "BLOCK", "replay: replay of weekly.zarr")
    assert "precip: values differ" in out


def test_replay_rtol(tmp_path, clip_region, aggregate, verify_run, capsys):
    _src, _clipped, weekly = _chain(tmp_path, clip_region, aggregate)
    _rewrite_values(weekly, lambda v: v * (1.0 + 1e-9))

    assert _gate(verify_run, "-i", str(weekly), "--replay") == 1
    capsys.readouterr()
    code = _gate(verify_run, "-i", str(weekly), "--replay", "--rtol", "1e-6")

    out = capsys.readouterr().out
    assert code == 0, out
    assert "equal within tolerance" in out


def test_required_replay_runs_without_optional_flag(
    tmp_path, clip_region, aggregate, verify_run, capsys
):
    _src, _clipped, weekly = _chain(tmp_path, clip_region, aggregate)
    assert _gate(verify_run, "-i", str(weekly), "--require-replay") == 0
    out = capsys.readouterr().out
    assert _has_check(out, "PASS", "replay: replay of weekly.zarr")
    assert "final data comparison required" in out
    _rewrite_values(weekly, lambda v: v * 2)
    assert _gate(verify_run, "-i", str(weekly), "--require-replay") == 1
    assert "VERDICT  : BLOCK" in capsys.readouterr().out


def test_required_replay_cannot_pass_on_intermediate_comparison_only(
    tmp_path, clip_region, aggregate, verify_run, vr_module, monkeypatch, capsys
):
    _src, _clipped, weekly = _chain(tmp_path, clip_region, aggregate)

    def intermediate_only(gate, *args):
        gate.add("replay-step", "intermediate", "PASS", "identical", "identical")

    monkeypatch.setattr(vr_module, "_replay", intermediate_only)
    assert _gate(verify_run, "-i", str(weekly), "--require-replay") == 2
    assert _has_check(capsys.readouterr().out, "UNVERIFIABLE", "required-replay:")


def test_required_replay_rejects_fetch_only_figure(tmp_path, verify_run, capsys):
    ds = make_gridded()
    history = [{"skill": "chirps-fetch", "version": "0.0.2", "args": {}, "input": None}]
    stamp_zarr(ds, history)
    raw = write_zarr(ds, tmp_path / "raw.zarr")
    png = tmp_path / "raw.png"
    Image.new("RGB", (8, 8)).save(png)
    stamp_figure(
        png, history + [{"skill": "plot", "version": "0.0.2", "args": {}, "input": input_ref(raw)}]
    )
    assert _gate(verify_run, "-i", str(png), "--replay") == 0
    capsys.readouterr()
    assert _gate(verify_run, "-i", str(png), "--require-replay") == 2
    out = capsys.readouterr().out
    assert "input hashes alone do not verify output values" in out


def test_png_gates_the_plotted_data(tmp_path, clip_region, aggregate, verify_run, capsys):
    _src, _clipped, weekly = _chain(tmp_path, clip_region, aggregate)
    png = tmp_path / "weekly.png"
    # conftest.TINY_PNG does not decode (PIL: "broken data stream"); build a real one.
    Image.new("RGB", (8, 8), (255, 255, 255)).save(png)
    history = load_history(weekly) + [
        {"skill": "plot", "version": "0.0.2", "args": {"title": "x"}, "input": input_ref(weekly)}
    ]
    stamp_figure(png, history)

    code = _gate(verify_run, "-i", str(png), "--replay")

    out = capsys.readouterr().out
    assert code == 0, out
    assert "step 3 plot <- weekly.zarr" in out
    assert "replay of weekly.zarr (data plotted by plot)" in out

    _rewrite_values(weekly, lambda v: v + 0.5)
    assert _gate(verify_run, "-i", str(png)) == 1
