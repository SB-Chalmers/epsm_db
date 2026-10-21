# epsm_db

[![CI](https://img.shields.io/badge/CI-GitHub_Actions-2088FF?logo=githubactions&logoColor=white)](https://github.com/SB-Chalmers/epsm_db/actions/workflows/ci.yml) [![Releases](https://img.shields.io/badge/releases-GitHub-181717?logo=github&logoColor=white)](https://github.com/SB-Chalmers/epsm_db/releases) [![Issues](https://img.shields.io/badge/issues-GitHub-181717?logo=github&logoColor=white)](https://github.com/SB-Chalmers/epsm_db/issues)

EPSM Database stores Swedish building evidence in PostgreSQL/PostGIS and provides
Python queries for buildings, geometry, energy certificates and statistical context.
Records retain source provenance and coverage so selected evidence can be inspected
or passed to EPSM models with modelling assumptions kept explicit.

## Getting started

Use Python 3.11 or newer and Docker Compose. Clone the repository to begin:

```sh
git clone https://github.com/SB-Chalmers/epsm_db.git
cd epsm_db
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[notebook]'
UBEM_POSTGRES_PORT=55434 docker compose -p epsm-db-quickstart up -d --wait
export UBEM_DATABASE_URL='postgresql://ubem_owner:ubem-local-only@127.0.0.1:55434/sweden_ubem'
epsm-db db init --schema-only
epsm-db db status
```

This creates an empty local database on port 55434. To work with real building
evidence, acquire the source bundle separately and follow the [detailed setup
guide](docs/guide.md) before importing it. Source datasets are excluded from Git.

For an existing processed database, set `UBEM_DATABASE_URL` to its connection URL
and start with the [exploration notebook](notebooks/explore_stock.ipynb).

## Documentation

- [Setup, record queries and EPSM integration](docs/guide.md)
- [Database design and ingestion](docs/ubem_database_design_and_setup.md)
- [Source bundle and portable paths](docs/source_paths.md)
- [Evidence matching and reporting](docs/epc_matching_and_reporting.md)
- [Validation rules](docs/validation_rules.md)

## License

See the [shared EPSM licensing guidance](https://github.com/SB-Chalmers/epsm_development/blob/main/docs/licensing.md).
This repository has no root software license declaration. Source datasets retain
their providers' access and redistribution terms.
