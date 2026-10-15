"""Inspect classifications for synthetic evidence, without a database or engine."""

from epsm_data import Catalogue
from shapely.geometry import box

from epsm_db import BuildingRecord, BuildingType


def main():
    record = BuildingRecord(
        "synthetic", "example-building", BuildingType("multi_family"),
        box(0, 0, 10, 8), {"construction_year": 2022},
    )
    catalogue = Catalogue.load()
    era = catalogue.era_for_year(record.facts["construction_year"])
    slots = catalogue.search(category=record.type.archetype_category, era=era)
    assert era == "SE.07"
    assert slots and all(slot.key.renovation_state is None for slot in slots)
    assert record.archetype_assignments == ()
    print(record)
    print("Compatible classification slots:", len(slots))
    print("Source catalogue:", catalogue.dataset_id)


if __name__ == "__main__":
    main()
