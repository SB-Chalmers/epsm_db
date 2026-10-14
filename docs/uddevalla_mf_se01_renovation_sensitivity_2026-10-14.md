# Uddevalla SE.01 multifamily renovation sensitivity

The five multifamily `SE.01` buildings in the [period-matched Uddevalla AMY comparison](uddevalla_epc_amy_comparison_2026-10-13.md) were rerun with EPSM Data's `standard` and `advanced` renovation packages. Each building keeps the same footprint, storeys, heated-area reference, use programme, ventilation, heating carrier, EPC reporting months and checksum-checked AMY files as its `existing` baseline. The changed model sections are envelope, façade glazing, envelope infiltration and provenance. These are **hypothetical full-package scenarios**; the selected EPC observations do not establish renovation history.

| Building UUID prefix | Carrier | Existing model / EPC | Standard | Advanced |
| --- | --- | ---: | ---: | ---: |
| `d63ecea1` | Heat pump | 2.39 | 0.99 | 0.81 |
| `79fc57ee` | District heat | 1.84 | 0.88 | 0.76 |
| `a82f1042` | District heat | 2.02 | 0.74 | 0.59 |
| `40af4761` | District heat | 1.11 | 0.38 | 0.30 |
| `c396a52f` | District heat | 1.58 | 0.68 | 0.57 |
| **Median** | | **1.84** | **0.74** | **0.59** |
| **Median absolute percentage error** | | **84%** | **26%** | **41%** |

The standard package improves absolute error for four of five buildings. It brings one heat-pump building almost exactly to its reported EPC heating and reduces three district-heated overpredictions substantially. It overcorrects the building whose existing model was already close (`40af4761`: 1.11 to 0.38). Advanced further reduces heating for every building and generally underpredicts more. The energy-weighted ratio shifts from 1.55 (`existing`) to 0.63 (`standard`) and 0.52 (`advanced`). Therefore the original-era assumption is a plausible contributor to SE.01 overprediction, but applying one full renovation package to every SE.01 building would create a new systematic bias.

For an illustrative SE.01 multifamily matrix slot, the standard package changes wall U-value from about 1.49 to 0.25 W/m²K, roof/top from 0.36 to 0.12 W/m²K, window from 2.8 to 1.22 W/m²K, and area-based envelope infiltration from 0.2 to 0.1 L/s/m². Advanced further improves wall, roof and window values. Geometry and occupancy remain fixed in the paired simulations; actual upgrades may be partial and should be assigned from independent building evidence, not selected by whichever EPC residual is smallest. Heat-pump COP remains assumed, so the heat-pump result alone cannot validate the envelope state.

The [paired plot](../output/analysis/uddevalla-consistency-2026-10-13/amy_period_comparison/renovation_sensitivity_mf_se01/renovation_sensitivity.png), [building results](../output/analysis/uddevalla-consistency-2026-10-13/amy_period_comparison/renovation_sensitivity_mf_se01/comparison.csv), [manifest](../output/analysis/uddevalla-consistency-2026-10-13/amy_period_comparison/renovation_sensitivity_mf_se01/manifest.json) and [runner](../scripts/compare_uddevalla_renovation.py) retain the exact values and method. Fourteen new building-year simulations were accepted; the prior seven existing-state AMY building-year runs were reused as the baseline.
