"""Project-wide paths, constants, and schema mappings.

All path-producing helpers resolve relative to the repository root so scripts
work regardless of the current working directory.

Layout:

    <PROJECT_ROOT>/
    ├── data/                  raw, immutable inputs (Excel, shapefiles, TIFFs)
    ├── outputs/
    │   ├── processed/         pipeline-generated CSVs
    │   └── figures/           plots and reports
    └── .cache/                runtime caches (Open-Meteo HTTP cache)
"""
from __future__ import annotations

import os
from pathlib import Path

from .region import REGION_REGISTRY

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = PROJECT_ROOT / "data"
ERA5_DIR = DATA_DIR / "era5_land"      # raw NetCDF downloads from Copernicus CDS (ERA5-Land, 0.1°)
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
PROCESSED_DIR = OUTPUTS_DIR / "processed"
FIGURES_DIR = OUTPUTS_DIR / "figures"
CACHE_DIR = PROJECT_ROOT / ".cache"

# -----------------------------------------------------------------------------
# Active region selection
# -----------------------------------------------------------------------------
# The whole pipeline is parameterised by a RegionSpec (see firepredict/region.py).
# FIREPREDICT_REGION picks the registry entry; default 'portugal' resolves every
# value below to today's exact literals, so the Portugal run stays byte-identical.
REGION = os.environ.get("FIREPREDICT_REGION", "portugal")
ACTIVE_SPEC = REGION_REGISTRY[REGION]

# Spec-derived aliases (Portugal → today's literals). bbox is (N, W, S, E).
ERA5_BBOX_NORTH, ERA5_BBOX_WEST, ERA5_BBOX_SOUTH, ERA5_BBOX_EAST = ACTIVE_SPEC.bbox
ERA5_YEARS: tuple[int, ...] = ACTIVE_SPEC.years
TARGET_YEAR = ACTIVE_SPEC.label_year
TARGET_REGION: str = REGION
POSITIVE_CAUSE_LABEL = ACTIVE_SPEC.fire_source.positive_cause_labels[0]


def era5_filename_prefix() -> str:
    """NetCDF filename prefix for the active region (``portugal`` → ``portugal_…``)."""
    return ACTIVE_SPEC.key


def get_terrain_files() -> dict[str, Path]:
    """Logical-name → raster path for the active region (key order preserved)."""
    return ACTIVE_SPEC.terrain_files


def processed_path(stem_with_ext: str) -> Path:
    """Region-namespaced path under ``outputs/processed/``.

    For region=='portugal' the name is returned UNCHANGED (today's exact name,
    no region token). For any other region a ``_<key>`` token is inserted before
    the first ``_`` segment that follows the leading stem, falling back to a
    suffix-before-extension when there's no natural insertion point. Examples
    (region=='spain')::

        cleaned_fires.csv                       -> cleaned_fires_spain.csv
        samples_buffer15.csv                    -> samples_spain_buffer15.csv
        fire_weather_dataset_2024_bulk.csv      -> fire_weather_dataset_spain_2024_bulk.csv
    """
    if REGION == "portugal":
        return PROCESSED_DIR / stem_with_ext

    p = Path(stem_with_ext)
    stem, suffix = p.stem, p.suffix
    token = ACTIVE_SPEC.key
    # Insert the region token just before the first underscore-delimited segment
    # that carries a digit (a year like 2024 or a count like buffer15), keeping
    # the descriptive head intact. If no such segment exists, append the token.
    #   cleaned_fires                  -> cleaned_fires_spain
    #   samples_buffer15               -> samples_spain_buffer15
    #   fire_weather_dataset_2024_bulk -> fire_weather_dataset_spain_2024_bulk
    parts = stem.split("_")
    insert_at = next(
        (i for i, seg in enumerate(parts) if any(ch.isdigit() for ch in seg)),
        None,
    )
    if insert_at is None:
        new_stem = f"{stem}_{token}"
    else:
        parts.insert(insert_at, token)
        new_stem = "_".join(parts)
    return PROCESSED_DIR / f"{new_stem}{suffix}"


SGIF_EXCEL_GLOB = ACTIVE_SPEC.fire_source.excel_glob
ARDIDA_SHP_GLOB = ACTIVE_SPEC.fire_source.shp_glob

CLEANED_FIRES_CSV = processed_path("cleaned_fires.csv")

# Which samples file the downstream stages read. Stage 1b writes one file
# per entry in NEG_BUFFER_DAYS_OPTIONS (see below) — to switch which one the
# pipeline consumes, change ACTIVE_NEG_BUFFER_DAYS and re-run stages 2-4.
ACTIVE_NEG_BUFFER_DAYS = 15
SAMPLES_CSV = processed_path(f"samples_buffer{ACTIVE_NEG_BUFFER_DAYS}.csv")

# The ``_bulk`` suffix distinguishes outputs produced by the Problem 1/2
# pipeline (bulk grid-cell fetcher + negative samples) from the original
# per-fire CSVs the notebooks left on disk. Keep both so results stay
# comparable until the new model run has been validated.
WEATHER_POINT_CSV = processed_path(f"fire_weather_dataset_{TARGET_YEAR}_bulk.csv")
WEATHER_TIMESERIES_CSV = processed_path(f"fire_weather_dataset_timeseries_{TARGET_YEAR}_bulk.csv")
TERRAIN_FINAL_CSV = processed_path(f"final_fire_weather_terrain_{TARGET_YEAR}_bulk.csv")

# SGIF Excel column names → canonical (ICNF shapefile) names.
# The portugal RegionSpec references this mapping; kept here for backwards
# compatibility (docs/other code may import config.SGIF_COLUMN_MAPPING).
SGIF_COLUMN_MAPPING: dict[str, str] = {
    "Codigo_SGIF": "Cod_SGIF",
    "Codigo_ANEPC": "Cod_ANEPC",
    "DataHoraAlerta": "DH_Inicio",
    "DataHora_PrimeiraIntervencao": "DH_1Interv",
    "DataHora_Extincao": "DH_Fim",
    "CodCausa": "Causa_Cod",
    "TipoCausa": "Causa_Tipo",
    "DescricaoCausa": "Causa_Desc",
    "AreaTotal_ha": "AreaHaSGIF",
    "Latitude": "lat",
    "Longitude": "lon",
}

# Placeholder for the Spain EGIF mapping — real columns land in T8/T9.
EGIF_COLUMN_MAPPING: dict[str, str] = {}

# Logical-name → raster path. Spec-derived so the active region's rasters are
# used; key order (roughness, slope, aspect for portugal) is preserved.
TERRAIN_FILES: dict[str, Path] = get_terrain_files()

OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_HOURLY_VARS: list[str] = [
    "temperature_2m",
    "relative_humidity_2m",
    "wind_speed_10m",
    "precipitation",
]

# Primary weather source for stage 2. "era5" reads local NetCDFs downloaded
# from Copernicus CDS (free, no rate limits, same underlying data Open-Meteo
# re-serves). "open_meteo" uses the bulk-cell fetcher in weather_bulk.py,
# which is kept as a fallback but is rate-limited on the free tier.
WEATHER_SOURCE = "era5"

# ERA5 settings. We pull reanalysis-era5-land (0.1° native, ~11×9 km at
# Portugal latitudes) — the highest-resolution public ERA5 product that
# CDS exposes. The original ERA5 single-levels (0.25°) was used earlier
# because a full year × 5 vars exceeded the per-request cost cap; with
# monthly chunks the cost math works out for ERA5-Land too:
#
#   0.1° × 6° × 4° bbox → 60×40 = 2 400 grid points
#   1 month × 24 h × 31 d × 10 vars × 2 400 cells ≈ 1.8 × 10⁷ items
#
# which CDS accepts in a single request. We land mid-pack variable-wise
# to leave headroom for the occasional unusually-long month.
#
# ERA5-Land is the right product for fire work anyway: it's a
# land-specific reanalysis that fills in the 0.25° ERA5 grid with a
# higher-resolution land surface model, so surface temp, soil moisture,
# and LAI are more realistic than re-gridded ERA5.
#
# Our ~11 km snap grid (WEATHER_GRID_STEP=0.1°) now matches the source
# resolution exactly — every snapped fire cell lands on a native
# ERA5-Land grid point, no oversampling on either side.
ERA5_DATASET = "reanalysis-era5-land"
# ERA5-Land has no `product_type` parameter (unlike single-levels).
ERA5_PRODUCT_TYPE: str | None = None
# ERA5_BBOX_* and ERA5_YEARS are spec-derived aliases defined near the top of
# this module (from ACTIVE_SPEC.bbox / ACTIVE_SPEC.years). For portugal they
# resolve to N=42.5, W=-10.0, S=36.5, E=-6.0 and years 2014..2024.
# Variables are split by their CDS sampling semantics so we can submit
# them as two separate requests per chunk:
#   - INSTANT vars are sampled exactly at the valid time.
#   - ACCUM  vars are integrated over the preceding hour (radiation
#     fluxes in J/m², water budgets in m of water-equivalent).
#
# Two requests per chunk instead of one doubles the per-request item
# budget, which is what lets us fit 20 variables into a monthly chunk.
# Same wall time (CDS serves them in parallel) and the downloaded files
# stay cleanly separated on disk (``*_instant.nc`` / ``*_accum.nc``).
#
# Fire-oriented selection (derived downstream column in parentheses):
#   instant:
#     - 2m_temperature                    (temp °C)
#     - 2m_dewpoint_temperature           (dewpoint °C, plus Magnus → humidity %)
#     - 10m_u_component_of_wind           (u10 m/s)
#     - 10m_v_component_of_wind           (v10 m/s, with u10 → wind_speed km/h)
#     - surface_pressure                  (pressure hPa)
#     - skin_temperature                  (skin_temp °C — surface, hotter/drier than 2m air)
#     - soil_temperature_level_1          (soil_temp_1 °C, 0-7 cm)
#     - volumetric_soil_water_layer_1..4  (soil_moist_1..4 m³/m³, 0-7/7-28/28-100/100-289 cm)
#     - leaf_area_index_low_vegetation    (lai_low, grass/shrub fuel load)
#     - leaf_area_index_high_vegetation   (lai_high, tree canopy fuel load)
#   accumulated:
#     - total_precipitation               (precip mm/h)
#     - surface_net_solar_radiation       (solar_net W/m², J/m² ÷ 3600 s)
#     - surface_net_thermal_radiation     (thermal_net W/m²)
#     - surface_latent_heat_flux          (latent_heat W/m² — evapo-cooling)
#     - surface_sensible_heat_flux        (sensible_heat W/m² — surface heating)
#     - total_evaporation                 (evap_total mm/h — actual ET)
#     - potential_evaporation             (evap_pot mm/h — atmospheric demand; FWI input)
# Each "group" becomes one CDS request per monthly chunk. The CDS cost
# cap for ERA5-Land monthly × Portugal bbox rejects requests with more
# than 8 variables (empirically probed: 8 PASS / 9 FAIL), so we split
# the 20-var set into three groups that each stay safely under the cap.
# Group names become file-name suffixes: ``portugal_YYYY_MM_<name>.nc``.
ERA5_REQUEST_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "instant_a",  # 8 vars — core weather + surface state
        (
            "2m_temperature",
            "2m_dewpoint_temperature",
            "10m_u_component_of_wind",
            "10m_v_component_of_wind",
            "surface_pressure",
            "skin_temperature",
            "soil_temperature_level_1",
            "volumetric_soil_water_layer_1",
        ),
    ),
    (
        "instant_b",  # 5 vars — deeper soil + vegetation
        (
            "volumetric_soil_water_layer_2",
            "volumetric_soil_water_layer_3",
            "volumetric_soil_water_layer_4",
            "leaf_area_index_low_vegetation",
            "leaf_area_index_high_vegetation",
        ),
    ),
    (
        "accum",  # 7 vars — hourly-accumulated fluxes + water budget
        (
            "total_precipitation",
            "surface_net_solar_radiation",
            "surface_net_thermal_radiation",
            "surface_latent_heat_flux",
            "surface_sensible_heat_flux",
            "total_evaporation",
            "potential_evaporation",
        ),
    ),
)
# Spec-aware override: a region may supply its own request groups (e.g. a
# different CDS cost cap). When ACTIVE_SPEC.era5_request_groups is None we keep
# the default literal above, so portugal is unchanged.
if ACTIVE_SPEC.era5_request_groups is not None:
    ERA5_REQUEST_GROUPS = ACTIVE_SPEC.era5_request_groups

# Flat view — used by code that just needs the full list (e.g., logging).
ERA5_VARIABLES: tuple[str, ...] = tuple(v for _, vs in ERA5_REQUEST_GROUPS for v in vs)

# CDS cost limits force us to split yearly requests into monthly chunks.
# At 0.1° × 10 vars × 1 month ≈ 1.8 × 10⁷ items per request, which CDS
# accepts; full-year requests at this variable count would be rejected.
# Total for 11 years: 132 chunks. Each chunk's CDS job takes a couple of
# minutes of server-side work plus queue time; expect ~6-12 hours
# end-to-end for the full sequential download on a weekday.
# Spec-aware: a region may override the chunk size; portugal uses the default 1.
ERA5_MONTHS_PER_CHUNK = ACTIVE_SPEC.era5_months_per_chunk


def era5_nc_path(year: int, chunk_idx: int | None = None) -> Path:
    """Return the NetCDF path for a given ERA5 year / chunk download."""
    prefix = era5_filename_prefix()
    if chunk_idx is None:
        return ERA5_DIR / f"{prefix}_{year}.nc"
    return ERA5_DIR / f"{prefix}_{year}_{chunk_idx:02d}.nc"


def era5_chunk_months(chunk_idx: int) -> list[str]:
    """Return the list of month strings (``'01'`` ... ``'12'``) for a chunk.

    Chunks are 1-indexed so that ``chunk_idx=1`` is the first chunk of the
    year. For ``ERA5_MONTHS_PER_CHUNK = 6`` there are two chunks per year:
    chunk 1 → months 1-6, chunk 2 → months 7-12.
    """
    start = (chunk_idx - 1) * ERA5_MONTHS_PER_CHUNK + 1
    end = min(start + ERA5_MONTHS_PER_CHUNK, 13)
    return [f"{m:02d}" for m in range(start, end)]


def era5_chunks_per_year() -> int:
    return (12 + ERA5_MONTHS_PER_CHUNK - 1) // ERA5_MONTHS_PER_CHUNK

# requests_cache appends ".sqlite" to this stem.
OPEN_METEO_CACHE_PATH = CACHE_DIR / "openmeteo"

# Weather fetch strategy (see docs/open-problems.md §1).
# ERA5 native resolution is ~0.25°, Open-Meteo re-grids to ~0.1° (~11 km).
# Snapping to WEATHER_GRID_STEP before caching collapses many fires into one
# API call without losing information relative to the source grid.
WEATHER_GRID_STEP = 0.1
# 3-day history by default (the RNN in Problem 3 will consume it as a
# sequence). Hours derives from days so both knobs stay in sync.
WEATHER_LOOKBACK_DAYS = 3
WEATHER_LOOKBACK_HOURS = 24 * WEATHER_LOOKBACK_DAYS

# Negative-sampling strategy (see docs/open-problems.md §2).
#
# Target: binary fire/no-fire. For each positive fire we sample
# NEG_SAMPLES_PER_POSITIVE random (cell, hour) pairs from the cell pool
# that contains at least one real fire, within the observed time range,
# excluding any hour too close to a real fire in the same cell.
#
# "Too close" is defined as falling inside the forbidden window
#   [DH_Inicio - (lookback + buffer), DH_Fim + (lookback + buffer)]
# where `lookback` is WEATHER_LOOKBACK_DAYS (so a negative's lookback
# window can't touch an active fire) and `buffer` is a safety margin.
#
# We generate one samples file per entry in NEG_BUFFER_DAYS_OPTIONS so the
# two stricter/looser settings can be compared. ACTIVE_NEG_BUFFER_DAYS
# (defined above) selects which one stages 2-4 consume.
NEG_SAMPLES_PER_POSITIVE = 10
NEG_BUFFER_DAYS_OPTIONS: tuple[int, ...] = (15, 30)
SAMPLE_RANDOM_SEED = 42

# POSITIVE_CAUSE_LABEL is defined near the top as a spec-derived alias
# (ACTIVE_SPEC.fire_source.positive_cause_labels[0]) — 'Natural' for portugal.


def samples_csv_for(buffer_days: int) -> Path:
    """Return the samples-CSV path for a given negative-buffer setting."""
    return processed_path(f"samples_buffer{buffer_days}.csv")


def ensure_output_dirs() -> None:
    """Create generated-artifact directories if they don't exist."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ERA5_DIR.mkdir(parents=True, exist_ok=True)
