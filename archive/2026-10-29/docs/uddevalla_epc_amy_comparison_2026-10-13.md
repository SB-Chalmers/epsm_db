# Uddevalla EPC heating comparison with measured-year weather

The EPSM baseline was rerun for the previously screened Uddevalla archetype panel using `smhi2epw` actual meteorological year (AMY) files for each EPC reporting year. The comparison is **modelled delivered space-heating carrier input divided by EPC source-form reported space heating for the same twelve months**. It is not ideal heat demand divided by total EPC energy.

## Result

The 107 panel buildings had already passed the source-function and minimum 100 m² footprint screen, excluding complementary and small ancillary buildings. Of these, 82 have a single comparable heating carrier, a measurement-indicated EPC, and a resolved twelve-month reporting period. There were 108 required building-year simulations because 26 certificates cross calendar years; all 108 were accepted. The other 25 buildings were excluded from a carrier residual: 24 have mixed or missing carrier evidence and two lack a usable measured period, with one building in both groups.

| Measure across 82 buildings | Ljungskile TMY, same reported EPC denominator | Uddevalla AMY, exact EPC months |
| --- | ---: | ---: |
| Median model / EPC heating | 0.948 | 1.014 |
| Aggregate model / EPC heating | 0.957 | 1.001 |
| Median absolute percentage error | 32.3% | 32.1% |

AMY moved 44 buildings closer to their EPC value and 38 farther away. Its median change in modelled heating relative to TMY was +5.1%, ranging from −15.6% to +23.2%. Thirty-three of 82 AMY predictions are within ±25% of EPC heating; 64 are within ±50%. Three are below half the EPC value and seven exceed twice it. The near-unity median and aggregate ratios therefore reflect cancellation, **not** building-level accuracy. Weather matching corrects the comparison basis but does not explain most of the scatter.

| Carrier | n | Median AMY / EPC | Median absolute error |
| --- | ---: | ---: | ---: |
| District heat | 49 | 1.005 | 28% |
| Direct electricity | 5 | 1.096 | 36% |
| Heat pump | 24 | 0.971 | 42% |
| Fuel | 4 | 1.245 | 41% |

| Building category | n | Median AMY / EPC |
| --- | ---: | ---: |
| Multifamily | 30 | 0.922 |
| Office | 14 | 0.842 |
| School | 21 | 1.062 |
| Single-family | 17 | 1.361 |

The heat-pump and fuel comparisons include assumed conversion efficiency. The model uses a fixed COP of 3 for the heat-pump cases shown here; a carrier residual cannot isolate fabric or geometry error. The single-family bias and later multifamily archetype underprediction warrant deeper examination, while small archetype groups should not be interpreted as stock estimates.

## Target and weather basis

The new `sweden_ubem.epc_heating` module preserves both target tracks and their raw evidence:

- For post-v2.6 EPCs, `Egi*UPPV` is reported delivered heating carrier input over the measured period. The form's `EgiEnergianvandning` can also yield an **inferred normal-year heating value** where source-form totals reconcile exactly after subtracting reported domestic-hot-water and property energy. That inferred value is retained separately and is **never** used as the AMY denominator.
- For pre-v2.6 EPCs, carrier rows combine heating and hot water. A measured-period heating candidate is obtained only where a unique `EgiVVBered` hot-water quantity can be subtracted. No claim of equivalent normal-use treatment across form versions is made.
- Only a unique twelve-month period with `EgiBeraknatVarde=Nej` enters the AMY residual. Calendar years are simulated separately and their hourly heating input is sliced to the EPC months, including leap years and cross-year periods. The raw values, mapping statuses, source regime, period, and exclusions are retained in `epc_targets.jsonl`.

Across all 107 records, 73 defensible post-v2.6 normal-year heating values are retained for a future normal-year analysis. This run does **not** compare those with AMY output or claim that the Ljungskile TMY exactly reproduces Boverket's [climate normalization method](https://www.boverket.se/sv/PBL-kunskapsbanken/regler-om-byggande/energihushallning/energihushallning/byggnadens-energianvandning-baserad-pa-matning/normalisering-av-energianvandningen/).

Twelve calendar AMYs (2014–2025) were built at a documented Uddevalla reference point (58.371241 N, 11.938648 E) with checksum-checked EPWs and provenance sidecars. Sixty-nine compared buildings are within 10 km of this point; the maximum is 23.4 km. The selected meteorological station is Såtenäs, 45.4 km from the reference point, while solar irradiation comes from STRÅNG at the reference coordinates. The weather sidecars report 0.50–3.34% reconstructed observation samples by year. Some years use donor or reanalysis recovery; 2014 explicitly used ERA5 for a small set of samples. These AMYs are period-matched but are **municipality-reference weather**, not site-specific measurements at every building.

All 82 source-form accounting totals reconcile exactly under a nonnegative-component interpretation, although absent alternative fields remain absent and their individual values are not asserted. The EPC field mapping statuses are unverified in the retained database. These are screened, auditable comparison candidates rather than ground truth for a calibrated model.

## Where to inspect next

The largest ratio, 4.26, is a school (`eaaa9147-d3b2-4cab-9dcc-eda06233bd69`) with EPC Atemp 1,500 m² but model conditioned floor area 816 m², EPC three storeys but model one, and assumed heat-pump COP. This is an obvious geometry and carrier-evidence review case; the residual alone cannot identify which assumption is wrong. One multifamily low outlier (`90d82ed1-93a0-4a58-96be-b141ea7a3e1d`, ratio 0.44) used the generic `healthcare` programme fallback, illustrating that programme assignment can also matter. The two single-family heat-pump cases at 2.84 and 2.58 need system-performance and EPC allocation review. These examples do not justify automatic calibration or changing Core physics.

The full [building-level CSV](../output/analysis/uddevalla-consistency-2026-10-13/amy_period_comparison/comparison.csv), [JSON report](../output/analysis/uddevalla-consistency-2026-10-13/amy_period_comparison/comparison.json), [run manifest](../output/analysis/uddevalla-consistency-2026-10-13/amy_period_comparison/manifest.json), and [reproducible runner](../scripts/compare_uddevalla_epc_amy.py) retain the actual values, weather years, source hashes, distances and exclusions. The generated output directory is local and intentionally not committed.
