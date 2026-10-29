"""Explicit weather references and optional, cached smhi2epw generation."""

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .records import implementation_hash


def sha256_file(path, chunk_size=8 * 1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class WeatherReference:
    epw_path: str
    calendar_year: int
    sha256: str

    @classmethod
    def from_epw(cls, path, *, calendar_year):
        from epsm_core.weather import validate_epw_calendar

        path = Path(path).resolve()
        validate_epw_calendar(path, calendar_year)
        return cls(str(path), calendar_year, sha256_file(path))

    @classmethod
    def from_location(cls, *, latitude, longitude, year, directory, **options):
        """Explicit network operation; ordinary record retrieval never calls this.

        Cache identity includes location, year, compiler source and all supplied
        options. Cache reuse checks the EPW checksum and calendar.
        """
        import smhi2epw.compiler
        from smhi2epw import EPWConfig, compile_epw

        reserved = {"output_path", "year", "latitude", "longitude", "cache_dir"}
        if reserved.intersection(options):
            raise ValueError("Weather options cannot override location/year/cache/output arguments")
        config = {"latitude": latitude, "longitude": longitude, "year": year, **options}
        identity = {
            "config": config,
            "compiler_sha256": sha256_file(smhi2epw.compiler.__file__),
            "package_sha256": implementation_hash("smhi2epw"),
        }
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        root = Path(directory).resolve() / key
        manifest = root / "weather-reference.json"
        if manifest.exists():
            data = json.loads(manifest.read_text())
            reference = cls.from_epw(data["epw_path"], calendar_year=year)
            if reference.sha256 != data["sha256"]:
                raise ValueError("Cached EPW checksum changed")
            return reference
        if root.exists():
            raise FileExistsError("Incomplete weather artifact; inspect it before retrying")
        root.mkdir(parents=True)
        result = compile_epw(
            EPWConfig(
                output_path=str(root / "weather.epw"), cache_dir=str(root / "raw-cache"), **config
            )
        )
        reference = cls.from_epw(result.output_path, calendar_year=year)
        manifest.write_text(json.dumps(dict(**asdict(reference), identity=identity), indent=2))
        return reference

    def climate(self):
        from epsm_core.weather import climate_from_epw, validate_epw_calendar

        if sha256_file(self.epw_path) != self.sha256:
            raise ValueError("Referenced EPW checksum changed")
        validate_epw_calendar(self.epw_path, self.calendar_year)
        return climate_from_epw(self.epw_path, calendar_year=self.calendar_year)
