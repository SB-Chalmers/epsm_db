# EPSM Database

<!-- EPSM introduction: Pocket CAD -->
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/welcome/epsm_db-dark.png">
  <img src="docs/assets/welcome/epsm_db.png" width="160" height="160" alt="EPSM Database — Three record drawers with one active record label; persisted catalogue storage.">
</picture>

Explore the evidence behind Swedish buildings. EPSM Database brings geometry,
energy certificates and statistical context into PostgreSQL/PostGIS, with Python
tools for querying and inspecting records. Sources, coverage and missing inputs
stay visible when you pass selected evidence to an EPSM model.

**[Explore the EPSM platform →](https://github.com/SB-Chalmers/epsm/blob/main/docs/platform.md)**
<!-- /EPSM introduction -->

[![CI](https://img.shields.io/badge/CI-GitHub_Actions-2088FF?logo=githubactions&logoColor=white)](https://github.com/SB-Chalmers/epsm_db/actions/workflows/ci.yml) [![Releases](https://img.shields.io/badge/releases-GitHub-181717?logo=github&logoColor=white)](https://github.com/SB-Chalmers/epsm_db/releases) [![Issues](https://img.shields.io/badge/issues-GitHub-181717?logo=github&logoColor=white)](https://github.com/SB-Chalmers/epsm_db/issues)

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

EPSM-owned software and documentation use the [MIT license](LICENSE).
Source datasets, catalogue evidence and third-party material retain their
providers' terms and attribution; MIT does not grant new redistribution rights
to those sources.

## People and support

Developed at Chalmers University of Technology as part of the EPSM research platform.

**Funding**: Swedish Energy Agency (Project ID: P2024-04053)

**Partners**:

- Lindholmen Science Park AB
- Sinom AB
- Stiftelsen Chalmers Industriteknik

**Team**:

- **Lead Developer**: Sanjay Somanath
- **Principal Investigator**: Alexander Hollberg
- **Contributors**: Yinan Yu, Dag Wästberg

<p align="center">
  <img src="docs/assets/welcome/logos.png" width="100%" alt="Chalmers University of Technology, Chalmers Industriteknik, Sinom, Lindholmen Science Park and Swedish Energy Agency logos">
</p>
