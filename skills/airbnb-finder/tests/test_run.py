import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("runmod", ROOT / "run.py")
runmod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runmod)


def test_load_config_dates_are_json_serializable(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("dates: {checkin: 2026-11-10, checkout: 2026-11-15}\nguests: {adults: 2}\n"
                 "anchors: [{name: A, lat: 1, lng: 2, max_distance_m: 500}]\nprice: {currency: USD}\n")
    cfg = runmod.load_config(p)
    assert cfg["dates"] == {"checkin": "2026-11-10", "checkout": "2026-11-15"}
    json.dumps(cfg)


def test_placeholder_config_is_rejected():
    import pytest
    with pytest.raises(SystemExit) as e:
        runmod.load_config(ROOT / "examples" / "config.example.yaml")
    assert "dates.checkin" in str(e.value)


def test_trip_change_archives_previous_results(tmp_path):
    cfg = {"dates": {"checkin": "2026-10-27", "checkout": "2026-10-30"}, "guests": {"adults": 2}, "price": {"currency": "EUR"}}
    runmod.archive_if_trip_changed(cfg, tmp_path)
    (tmp_path / "01_search.json").write_text("[]")
    (tmp_path / "03_details").mkdir()
    runmod.archive_if_trip_changed(cfg, tmp_path)          # same trip: nothing moves
    assert (tmp_path / "01_search.json").exists()
    cfg2 = {**cfg, "dates": {"checkin": "2026-10-19", "checkout": "2026-10-22"}}
    runmod.archive_if_trip_changed(cfg2, tmp_path)
    assert not (tmp_path / "01_search.json").exists() and not (tmp_path / "03_details").exists()
    arch = [p for p in tmp_path.iterdir() if p.name.startswith("archive-")]
    assert len(arch) == 1 and (arch[0] / "01_search.json").exists()
