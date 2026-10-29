# Compact evidence consumer audit, 0.0.4

The physical schema stores typed LM facts and locators; the public projections keep
UUIDs, component identifiers, geometry hashes, decisions and evidence fields.
Original LM and detailed measurement records are retrieved explicitly from verified
files. EPC rows remain in PostgreSQL because ordinary consumers need their originals.

| Consumer | Evidence retained in PostgreSQL | Verification |
|---|---|---|
| Selection processing | Complete building components and municipality assertions; purposes, entrances, address matching values; properties; complete EPC certificates and subjects | PostGIS selection, shared competition and certificate closure tests |
| Property diagnostics | Original property part/unit identifiers; exceptional geometry; valid geometry reused from typed parts | Missing identifiers, invalid/ambiguous geometry and ordinary linkage tests |
| EPC assessment | Each source row once, representative subject reference, certificate-owned values, conflicts, observations and source locators | Exact original rows, subject payloads and observation multiplicity in paired real-source samples |
| Physical assessment | Catalogue scalars and status, source null distinctions, roof facts, measurement hash/record, pinned EPC observations | Geometry identity, missing height, preflight and report tests |
| Exploration | Public `mart` views, typed values, geometry, coverage and decision explanations | Read-only role, executed notebook, protected completed Uddevalla exploration |
| Exports | Same public fields and cohort boundaries as exploration; full database geometry | CSV, GeoParquet and GeoPackage tests |
| Reports | Certificate source rows, outcomes, rejected candidates, site-event assertions and document hashes | EPC report and candidate diagnostics tests |
| Public views/comparison | Compatibility views reconstruct identifiers and source headers from compact keys; original payloads consumed by public views remain available | Exact multiset hashes, complete geometry and keyed Uddevalla comparison |
| Full original inspection | Asset hash, portable path, format and registered record locator | Changed/missing source errors; root relocation; XML, CSV, GeoPackage and Parquet tests |

Property display representatives follow the historical 1,024-record source groups,
independently of configurable transport batches. This preserves existing display
values without changing property identity or matching policy. Height catalogue
source nulls are preserved separately from normalized missing-status facts.

The final real Uddevalla acceptance physically disconnects the source bundle while
processing, exploration, reports and exports run. Source completion, Uddevalla
acceptance and national publication remain distinct gates.
