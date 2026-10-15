# Inspect catalogue classifications alongside evidence

Stock exploration uses the public `epsm_data.Catalogue` object. It reads Data's
packaged definitions when the caller creates a Stock; printing an existing Stock
or inspecting already loaded dimensions does not query a database. Data remains
an optional dependency, and no Core, EPSM, weather or engine is needed for
catalogue classification.

The public inspection operations are `dataset_id`, `dimensions`,
`era_for_year(year)`, `get_slot(key)`, `search(**filters)`, `summary()` and
`effective_configuration()`. `get_slot` accepts the public `ArchetypeKey` or a
slot identifier. The effective configuration is the same source payload used by
the previous effective-property catalogue. Hashing its canonical JSON preserves
the existing stock-assignment content identity; it is not a lossless physical
archetype export. Complete archetypes use Data's separate versioned exchange.

Existing `stock.catalogue.to_dict()` calls should use
`stock.catalogue.effective_configuration()`. Stock no longer imports the older
parametric loader or selects physical constructions while inspecting a slot.
Advanced inspection of effective definitions remains available through Data's
documented interfaces.

Classification stays separate from evidence. Missing and nonpositive register
years remain in the `Missing year` column. Source types other than single-family,
multi-family, office or school remain in the generic catalogue category. SE.06
ends in 2021; SE.07 starts in 2022 and has no renovation state. Selecting a climate
zone, ventilation classification or renovation state records an assumption; it
does not turn it into an observation or apply it to a physical Building.

Database-backed calls still require the explicitly configured dataset and
connection. Fully specified stock cells return independent records with a slot,
source catalogue identity, effective-configuration hash and year interval.
Partially specified cells leave those assignments unresolved. Model assembly
belongs to EPSM and is requested separately.

Run `python examples/catalogue_record.py` in an environment containing the
coordinated development revisions of DB and Data for an offline synthetic
example. It writes no files and opens no database. The stock notebook retains
its explicit database prerequisite.

The wheel smoke check runs outside the checkout and exercises the installed
Catalogue's year classification, dimensions, slot search/lookup, summary and
detached effective configuration. The pinned CI lane must select the exact
coordinated Data/Core/EPSM revisions that provide those APIs. The optional
`catalogue` and `epsm` extras require `epsm_data>=0.5.0`; development features also
need those exact commit pins because the version alone does not identify them.
This change creates no new package release and leaves the separate release
process in place.
