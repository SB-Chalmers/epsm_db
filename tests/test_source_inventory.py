from pathlib import Path

from sweden_ubem.source_inventory import load_source_inventory
from scripts.sync_manual_inventory import synchronized_text


def test_inventory_profiles_and_tower_height_paths_are_portable():
    inventory = load_source_inventory()
    assert set(inventory.profiles) == {"mac-local", "tower", "linux-server"}
    from sweden_ubem.source_inventory import DEFAULT_SOURCE_ROOT
    assert inventory.path_for("height_staging_catalogue", profile="tower") == (
        DEFAULT_SOURCE_ROOT / "prepared/heights/component-stage/handoff/component-height-catalogue.parquet")
    height = next(
        source for source in inventory.sources if source.key == "height_staging_catalogue"
    )
    assert height.expected_sha256 == (
        "42a087f8a0fd401f90d5722656dc4612c918d7e7567c0245f77214c87a96df55"
    )
    assert height.status == "staging_only"


def test_inventory_covers_every_epc_export_and_schema_document():
    inventory = load_source_inventory()
    logical_names = {source.logical_name for source in inventory.sources}
    assert "fore_v260/Datamodell.docx" in logical_names
    assert "fore_v260/Mappningsfil_fore_v260.xlsx" in logical_names
    assert "fore_v260/data/std_uttag_fore_260_del_1.txt" in logical_names
    assert "fore_v260/data/std_uttag_fore_260_del_2.txt" in logical_names
    assert "efter_v260/Datamodell.docx" in logical_names
    assert "efter_v260/Mappningsfil_efter_v260.xlsx" in logical_names
    for year in range(2015, 2026):
        assert f"efter_v260/data/std_uttag_efter_260_{year}.txt" in logical_names


def test_required_validation_reports_a_portable_empty_root(tmp_path: Path):
    report = load_source_inventory().validate(profile="linux-server", source_root=tmp_path)
    assert report["valid"] is False
    assert "lm_building_geometry_primary" in report["missing_required"]
    assert report["checksum_mismatches"] == []


def test_documented_path_appendix_is_generated_from_inventory():
    manual = Path(__file__).parents[1] / "docs/source_paths.md"
    assert synchronized_text(manual.read_text()) == manual.read_text()


def test_documented_paths_do_not_reintroduce_simulation_inputs():
    from scripts.sync_manual_inventory import scoped_path_appendix

    appendix = scoped_path_appendix()
    assert "epsm_parametric_catalogue" not in appendix
    assert "shacl_shapes" not in appendix
    assert "toy_staging_output" not in appendix
    assert "height_staging_catalogue" in appendix
    assert "Göteborg only; not Uddevalla" in appendix
