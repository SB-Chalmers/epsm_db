# Database diagrams — v0.0.4

The existing rendered diagrams describe the v0.0.2 evidence relationships. In
v0.0.4, source entities belong to a national backing snapshot; `scope.dataset_building`
and `scope.dataset_certificate` connect selections to shared evidence. Geometry,
property and EPC derivations also carry an immutable `processing_release_id`.
Context assignments retain the selection ID and reference shared source areas.
Municipal processing follows national import and reads only PostgreSQL.
The manual lists the current physical
tables and views. `1` means exactly one; `0..*` means zero or more. A raw
reference remains recorded even when its target cannot be resolved.

1. [System](diagrams/01-system.mmd): external source files, one PostGIS database and readers.
2. [Building components](diagrams/02-building-components.mmd): UUID identity, source lineage and component-level height/roof evidence.
3. [Register/addresses](diagrams/03-register-addresses.mmd): repeated purposes, entrances and address lookup.
4. [Properties/EPC](diagrams/04-properties-epc.mmd): cadastral parts, certificate subjects and accepted/rejected many-to-many decisions.
5. [SCB context](diagrams/05-scb-context.mmd): areas, hierarchy, observations and building-area assignments.
6. [Sources/quality](diagrams/06-sources-quality.mmd): source records, rule-backed findings and independent loading/coverage states.
7. [Loading sequence](diagrams/07-ingestion.mmd): preserve, normalize, derive and check one layer at a time.

Repeated provenance fields are omitted from class diagrams for readability.
Every record still identifies its source version/raw record, and every derived
relationship its run and rule version. Diagram edges are not ownership claims
or guarantees of physically certain relationships.

Rendered SVG/PNG previews are under `output/diagrams/`. Mermaid sources are
editable. They use light backgrounds and dark text.

```sh
mmdc -i docs/diagrams/02-building-components.mmd \
      -o output/diagrams/02-building-components.svg -b white
```

The [manual](ubem_database_design_and_setup.md) gives table/view row identities;
the [validation list](validation_rules.md) names source documentation and gaps.
