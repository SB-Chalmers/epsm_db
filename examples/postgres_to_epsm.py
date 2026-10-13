"""Retrieve and compile one building; --simulate additionally runs EnergyPlus.

Set UBEM_DATABASE_URL and install optional EPSM dependencies as shown in README.
"""

import argparse
from pathlib import Path

from sweden_ubem.epsm import EPSMBuilding, ModelAssemblyError
from sweden_ubem.io import AmbiguousRecordError, fetch_record
from sweden_ubem.weather import WeatherReference


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--uuid")
    selector.add_argument("--address")
    parser.add_argument("--municipality")
    parser.add_argument("--dataset", default="uddevalla")
    parser.add_argument("--epw", required=True)
    parser.add_argument("--year", type=int, default=2023)
    parser.add_argument("--energyplus", default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--simulate", action="store_true")
    args = parser.parse_args()
    try:
        record = (
            fetch_record.from_uuid(args.uuid, dataset=args.dataset)
            if args.uuid
            else fetch_record.from_address(
                args.address, municipality=args.municipality, dataset=args.dataset
            )
        )
        weather = WeatherReference.from_epw(args.epw, calendar_year=args.year)
        model, receipt = EPSMBuilding.from_record(
            record, weather=weather, energyplus=args.energyplus
        )
        print(receipt)
        print("Saved:", *model.write(args.output / "building.json"))
        if args.simulate:
            result = model.simulate(args.output / "simulation", energyplus=args.energyplus)
            print("Simulation accepted:", type(result).__name__)
    except AmbiguousRecordError as exc:
        parser.exit(2, f"{exc}\nCandidates: {[r.building_id for r in exc.candidates]}\n")
    except ModelAssemblyError as exc:
        parser.exit(2, str(exc.receipt) + "\n")


if __name__ == "__main__":
    main()
