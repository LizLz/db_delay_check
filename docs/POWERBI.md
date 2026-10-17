# Power BI dashboard: "DB Operational Health"

Power BI Desktop (free, Windows) reads the aggregated Parquet files that `scripts/export_bi.py` writes to `data/bi/`. The dashboard is an offline analytics layer on the same delay data as the web app.

## 1. Export the tables

```powershell
uv run python scripts/export_bi.py
```

(Use `docker compose stop app updater` first only if you get a "file is locked" error on Windows; the script only reads.)

| File | Grain | Use |
| --- | --- | --- |
| `fact_daily_station` | day x station | trend, station ranking, map/region |
| `fact_hour_weekday` | weekday x hour | heatmap matrix (medians valid here) |
| `fact_train_type_hour` | train family x hour | ICE, IC/EC, Regional Express, Regionalbahn, S-Bahn, other regional operators |
| `fact_causes` | day x cause code | cause bar chart |
| `dim_cause` | cause code | readable cause text and category (same mapping as the web app) |
| `dim_station` | station | names and region |
| `dim_date` | day | calendar |

Columns: `arrivals`, `delayed` (6 min or more), `cancelled`, `delay_sum_capped` (delays capped at 60 min, cancelled excluded), `median_delay`.

**Medians cannot be summed or averaged.** `median_delay` is only correct for exactly one row of its table. Use it in the weekday x hour matrix with no other filters. Everything else uses the additive columns below.

## 2. Load and relate

Power BI Desktop: Get data -> Parquet -> select each file from `data/bi/`.
Model view, relationships (many to one, single direction):

- `fact_daily_station[eva]` -> `dim_station[eva]`
- `fact_daily_station[day]` -> `dim_date[date]`
- `fact_causes[day]` -> `dim_date[date]`
- `fact_causes[reason_code]` -> `dim_cause[reason_code]` (both text)

`fact_hour_weekday` and `fact_train_type_hour` stay unrelated (they have their own slicers: weekday, hour, train type).
Set `dim_date[weekday_name]` to sort by `dim_date[weekday]`.

If `dim_station[region]` is empty, the stations file has no region column; tell me its column names and I will derive the region.

## 3. DAX measures

Measure names must differ from column names (the columns are `arrivals`, `delayed`, `cancelled`, and Power BI ignores case, so the measures are called `Total arrivals`, `Delayed arrivals`, `Cancelled arrivals`). Create them in a table `_Measures` (Home -> Enter data -> empty table).

```dax
Total arrivals = SUM ( fact_daily_station[arrivals] )

Delayed arrivals = SUM ( fact_daily_station[delayed] )

Cancelled arrivals = SUM ( fact_daily_station[cancelled] )

Delayed % = DIVIDE ( [Delayed arrivals], [Total arrivals] - [Cancelled arrivals] )

Cancelled % = DIVIDE ( [Cancelled arrivals], [Total arrivals] )

Avg delay (min, capped) =
DIVIDE ( SUM ( fact_daily_station[delay_sum_capped] ), [Total arrivals] - [Cancelled arrivals] )

-- compare with the previous period of the same length
Delayed % previous period =
VAR _days = COUNTROWS ( VALUES ( dim_date[date] ) )
VAR _start = MIN ( dim_date[date] )
RETURN
    CALCULATE (
        [Delayed %],
        DATESBETWEEN ( dim_date[date], _start - _days, _start - 1 )
    )

Delayed % change (points) = ( [Delayed %] - [Delayed % previous period] ) * 100

-- stations with enough traffic to be ranked
Delayed % (min 2000 arrivals) =
IF ( [Total arrivals] >= 2000, [Delayed %] )

Station rank (worst first) =
RANKX ( ALLSELECTED ( dim_station[station_name] ), [Delayed % (min 2000 arrivals)], , DESC, DENSE )

-- weekday x hour matrix (fact_hour_weekday): use only with no other filters
Typical delay (median, min) = AVERAGE ( fact_hour_weekday[median_delay] )   -- one row per cell, so this is the stored median

Delayed % (hour matrix) =
DIVIDE ( SUM ( fact_hour_weekday[delayed] ), SUM ( fact_hour_weekday[arrivals] ) - SUM ( fact_hour_weekday[cancelled] ) )

Delayed % (train type) =
DIVIDE ( SUM ( fact_train_type_hour[delayed] ), SUM ( fact_train_type_hour[arrivals] ) - SUM ( fact_train_type_hour[cancelled] ) )

Cause share % =
DIVIDE ( SUM ( fact_causes[arrivals] ), CALCULATE ( SUM ( fact_causes[arrivals] ), ALLSELECTED ( dim_cause ) ) )
```

## 4. Page layout

1. **KPI row (cards):** Total arrivals, Delayed %, Cancelled %, Avg delay, Delayed % change (points).
2. **Trend (line chart):** `dim_date[date]` on the axis, `Delayed %` as the value.
3. **Drill-down matrix:** rows `dim_station[region]` -> `dim_station[station_name]`, values `Total arrivals`, `Delayed %`, `Cancelled %`; conditional formatting (data bars) on `Delayed %`. Turn on drill down (the forked-arrow button) so a click on a region opens its stations.
4. **Region slicer** (`dim_station[region]`) and a date slicer: they filter the KPI cards, the trend and the matrix together (cross-filtering).
5. **Heatmap matrix (separate page or no slicers on it):** rows `fact_hour_weekday[weekday]`, columns `hour`, value `Delayed % (hour matrix)`, colour scale white -> red. Edit interactions so the date and region slicers do not apply to this visual (its data has no dates).
6. **Train types (clustered bar):** `fact_train_type_hour[train_group]`, value `Delayed % (train type)`.
7. **Causes (bar):** Y-axis `dim_cause[cause]` (not the code), X-axis `Cause share %`, Top 10 by `Cause share %`, sorted descending. Optional: colour by `dim_cause[category]`.

Title the page "DB Operational Health" and add a footer text: "Source: Deutsche Bahn timetable data (CC BY 4.0), via piebro/deutsche-bahn-data. Delayed = arrival 6 min or more late."
