"""Public building classes, independent of simulation-engine dependencies."""

from enum import Enum


class BuildingType(str, Enum):
    RESIDENTIAL_SINGLE_FAMILY = "single_family"
    RESIDENTIAL_MULTIFAMILY = "multi_family"
    SCHOOL = "school"
    OFFICE = "office"
    INDUSTRIAL = "industrial"
    COMPLEMENTARY = "complementary"
    OTHER = "other"
    UNKNOWN = "unknown"

    @property
    def archetype_category(self):
        return (
            self.value
            if self.value in {"single_family", "multi_family", "school", "office"}
            else "generic"
        )


# Optional notebook-friendly spelling; members remain ordinary Enum members.
building_type = BuildingType
