"""User-facing, read-only GIS lake facade."""

from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from ._logging import operation
from .errors import PublicationPendingError
from .lake.database import Lake


class Database:
    """Connect lazily to a GIS lake. Connection details never appear in repr."""

    def __init__(self, database_url=None):
        self._lake = Lake(database_url)

    def __repr__(self):
        return "Database(GIS lake; connection configured)"

    @staticmethod
    def _publication(connection):
        try:
            row = connection.execute(
                "SELECT current_database() AS dataset_id,* FROM gis.publication_status"
            ).fetchone()
        except psycopg.errors.UndefinedColumn as error:
            raise PublicationPendingError(
                "Ask the administrator to run epsm-db migrate and publish."
            ) from error
        if "model_revision" not in row:
            raise PublicationPendingError(
                "Ask the administrator to run epsm-db migrate and publish."
            )
        revision = row["source_revision"]
        if any(
            row[key] != revision
            for key in ("linked_revision", "published_revision", "model_revision")
        ):
            raise PublicationPendingError(
                "Model records are pending publication. The administrator must finish ingestion, link and publish; "
                "published GIS layers remain available through features() or QGIS."
            )
        from . import __version__

        return dict(row, client_version=__version__)

    @contextmanager
    def _read(self):
        with self._lake.connect(
            row_factory=dict_row, application_name="epsm-db stock reader"
        ) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            publication = self._publication(connection)
            yield connection, publication

    def stock(
        self, *, municipality=None, bbox=None, area_type=None, area_code=None, boundary_version=None
    ):
        from .stock import Stock

        return Stock(
            self,
            municipality=municipality,
            bbox=bbox,
            area_type=area_type,
            area_code=area_code,
            boundary_version=boundary_version,
        )

    @operation("database.record.read", level=10)
    def record(self, building_id):
        return self.stock().record(building_id)

    def building(self, building_id):
        return Building(self, str(building_id))

    def features(self, layer="buildings", **selection):
        """Read the existing bounded GIS API, including during a new import."""
        return self._lake.features(layer, **selection)

    def extract(self, output, **selection):
        """Geographic GIS extract; use stock.extract for classification selections."""
        return self._lake.extract(output, **selection)

    def status(self):
        """Reader-safe publication status; administrative import status is Lake.status()."""
        with self._lake.connect(row_factory=dict_row) as connection:
            return dict(connection.execute("SELECT * FROM gis.publication_status").fetchone())

    def sources(self):
        import pandas as pd

        with self._lake.connect(row_factory=dict_row) as connection:
            return pd.DataFrame(
                connection.execute("SELECT * FROM gis.sources ORDER BY fid").fetchall()
            )

    def boundaries(self, **filters):
        return self.features("boundaries", **filters)


class Building:
    """A lazy evidence handle; the physical model is epsm.Building."""

    def __init__(self, db, building_id):
        self.db, self.building_id = db, building_id

    def __repr__(self):
        from .records import _display_label

        return f"BuildingEvidence(id={_display_label(self.building_id)})"

    def record(self):
        return self.db.record(self.building_id)

    def to_epsm(self, **options):
        return self.record().to_epsm(**options)

    def facts(self):
        return self.record().facts

    def completeness(self):
        return self.record().completeness()

    def _table(self, name):
        import pandas as pd

        return pd.DataFrame(getattr(self.record(), name))

    def components(self):
        return self._table("components")

    def heights(self):
        return self._table("heights")

    def properties(self):
        return self._table("properties")

    def property_parts(self):
        return self._table("property_parts")

    def addresses(self):
        return self._table("addresses")

    def purposes(self):
        return self._table("purposes")

    def areas(self):
        return self._table("areas")

    def epc_candidates(self):
        return self._table("epc_results")

    def epc(self):
        return self._table("certificates")

    def epc_certificates(self, *, latest=True):
        records = self.record().certificates
        if latest and records:
            dates = [
                r["certificate_date"]
                for r in records
                if r.get("certificate_date") is not None and r.get("date_status") == "verified"
            ]
            if dates:
                records = tuple(
                    r
                    for r in records
                    if r.get("date_status") != "verified"
                    or r.get("certificate_date") is None
                    or r["certificate_date"] == max(dates)
                )
        import pandas as pd

        return pd.DataFrame(records)

    def context(self, *, indicator, period, dimensions=None):
        """Area-level observations remain context, never individual building measurements."""
        from psycopg.types.json import Jsonb
        import pandas as pd

        if not indicator or not period:
            raise ValueError("Choose an indicator and observation period")
        with self.db._read() as (connection, _):
            query = """SELECT s.* FROM gis.building_statistics s
                JOIN gis.model_facts f ON f.fid=s.building_id
                WHERE f.building_id=%s AND s.indicator=%s AND s.period=%s"""
            values = [self.building_id, indicator, str(period)]
            if dimensions is not None:
                query += " AND s.dimensions=%s"
                values.append(Jsonb(dimensions))
            return pd.DataFrame(connection.execute(query, values).fetchall())
