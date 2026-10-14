# Uddevalla EPC heating versus renovation-state matrix

14 October 2026

The screened panel contains **82 EPC-comparable buildings** (footprint at least 100 m²; eligible single-family, multifamily, school, or office source function; complementary buildings excluded). Existing, standard, and advanced envelope catalogue states were evaluated with the same building geometry, heating carrier, AMY weather files, and exact twelve-month EPC measurement periods. The standard and advanced states added 202 accepted building-year simulations; 14 matching runs from the earlier multifamily SE.01 check were reused. All 216 renovated-state building-year runs were accepted.

For each building, “best” is the state with the smallest absolute percentage difference between modeled delivered space-heating carrier input and the EPC reported-period heating target. Ties favor the less intensive state. The post-v2.6 EPC target uses the UPPV heating fields; the earlier form subtracts recorded hot-water preparation from combined heating where possible. The normal-year EPC value is not used to score AMY runs.

**Overall:** existing wins for 52/82, standard for 19/82, advanced for 11/82. Median absolute error is 32.1% with existing for all buildings, 36.5% with standard for all, 51.3% with advanced for all, and 19.9% when selecting the closest state per building. Buildings within ±25% increase from 33/82 with existing to 45/82 with the closest state. Seven buildings still exceed 50% absolute error in their best state.

Cell counts below are **existing / standard / advanced**. The last column is the median absolute error after choosing each building’s closest state; it is not the error of a single shared state for the cell.

| Building type | Era | n | Best-state counts E / S / A | Median best absolute error |
|---|---:|---:|---:|---:|
| Single family | SE.01 | 3 | 1 / 2 / 0 | 26% |
| Single family | SE.02 | 1 | 0 / 1 / 0 | 7% |
| Single family | SE.03 | 2 | 1 / 1 / 0 | 12% |
| Single family | SE.04 | 4 | 2 / 1 / 1 | 32% |
| Single family | SE.05 | 3 | 1 / 0 / 2 | 60% |
| Single family | SE.06 | 4 | 1 / 0 / 3 | 5% |
| Multifamily | SE.01 | 5 | 1 / 4 / 0 | 12% |
| Multifamily | SE.02 | 5 | 5 / 0 / 0 | 17% |
| Multifamily | SE.03 | 5 | 5 / 0 / 0 | 7% |
| Multifamily | SE.04 | 5 | 4 / 0 / 1 | 45% |
| Multifamily | SE.05 | 5 | 4 / 1 / 0 | 46% |
| Multifamily | SE.06 | 5 | 1 / 2 / 2 | 8% |
| School | SE.01 | 5 | 3 / 2 / 0 | 30% |
| School | SE.02 | 5 | 3 / 1 / 1 | 9% |
| School | SE.03 | 4 | 3 / 1 / 0 | 6% |
| School | SE.04 | 5 | 5 / 0 / 0 | 11% |
| School | SE.05 | 1 | 1 / 0 / 0 | 20% |
| School | SE.06 | 1 | 0 / 0 / 1 | 170% |
| Office | SE.01 | 2 | 2 / 0 / 0 | 26% |
| Office | SE.02 | 3 | 1 / 2 / 0 | 39% |
| Office | SE.03 | 4 | 3 / 1 / 0 | 34% |
| Office | SE.04 | 3 | 3 / 0 / 0 | 20% |
| Office | SE.05 | 0 | 0 / 0 / 0 | — |
| Office | SE.06 | 2 | 2 / 0 / 0 | 46% |

The older multifamily SE.01 cell is the clearest candidate for a renovated envelope: standard wins for 4 of 5 buildings. For multifamily SE.02 and SE.03, existing wins all 10. Single-family SE.06 favors advanced in 3 of 4, but the sample is small. School SE.06 has one building whose best state still overpredicts by 170%; that cell does not support a stock-wide conclusion.

The state choice is an **in-sample diagnostic oracle**: it uses the EPC target both to choose and to score the state. It is not evidence of actual renovations, and the 19.9% median cannot be treated as independent predictive accuracy. Renovation primarily changes the envelope. For the 40 buildings that existing already underpredicts, existing remains closest for 38; an envelope upgrade cannot generally cure those errors. In nine SE.06 buildings, standard gives slightly more heating than existing, so catalogue state labels should not be assumed to imply a strictly decreasing load for new stock. Other residuals can reflect geometry, heated area, operation, systems, and EPC field interpretation.

The generated building-level results, matrix data, figure, and run manifest are retained locally under `docs/analysis/uddevalla-renovation-matrix-2026-10-14/` and excluded from Git. Full simulation outputs and the machine-readable comparison are under `output/analysis/uddevalla-consistency-2026-10-13/amy_period_comparison/renovation_matrix_all/`. The historical result summary above is retained; a fresh clone does not include these generated artifacts.
