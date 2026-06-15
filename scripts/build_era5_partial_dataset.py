"""Build a fire-weather-terrain dataset restricted to the years for which
ERA5-Land data is on disk.

When only a subset of ERA5-Land years has been downloaded, this script builds
the dataset for just those years: it wires stage 2 + stage 3 together against
samples filtered to that subset and writes outputs with a `_era5_<from>_<to>`
suffix so they don't clobber the full-year files. Times each step.

Memory: instead of building the full per-cell weather table (22 vars × 968
cells × 44k hours × 4 B ≈ 3.7 GB just for data, but pandas .join allocates
2× during merges → 14 GB and OOM), the script streams chunk-by-chunk:
load 4 vars at a time, build a tiny per-cell table for those vars, look
up point + 72-h-sequence per sample, accumulate column-wise into master
DataFrames, then discard the chunk's cell table before loading the next.

Usage (from project root):

    .venv/bin/python -u scripts/build_era5_partial_dataset.py 2014 2018
"""
from __future__ import annotations

import gc
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

from firepredict import config
from firepredict.rasters import sample_raster
from firepredict.weather_bulk import lookup_point, lookup_sequence
from firepredict.weather_era5 import era5_to_cell_table, load_era5_dataset


VAR_CHUNK_SIZE = 4  # vars loaded into memory at once (~1.6 GB peak per chunk at 72h lookback; ~3.6 GB at 168h — ensure WSL2 memory ≥ 24 GB for the 7-day build)


def _suffixed(stem: Path, suffix: str) -> Path:
    """Insert ``_<suffix>`` before the extension."""
    return stem.with_name(f"{stem.stem}_{suffix}{stem.suffix}")


def main(year_from: int, year_to: int, lookback_hours: int | None = None) -> None:
    config.ensure_output_dirs()
    if lookback_hours is None:
        lookback_hours = config.WEATHER_LOOKBACK_HOURS

    suffix = f"era5_land_{year_from}_{year_to}"
    if lookback_hours != config.WEATHER_LOOKBACK_HOURS:
        # Make alternative-lookback runs coexist with the default-lookback CSVs.
        suffix = f"{suffix}_lb{lookback_hours // 24}d"
    print(f"Lookback: {lookback_hours}h ({lookback_hours // 24}d)  →  output suffix: '{suffix}'")

    point_csv = _suffixed(config.WEATHER_POINT_CSV, suffix)
    ts_csv = _suffixed(config.WEATHER_TIMESERIES_CSV, suffix)
    terrain_csv = _suffixed(config.TERRAIN_FINAL_CSV, suffix)

    t0 = time.time()
    print(f"Loading {config.SAMPLES_CSV.name} and filtering to {year_from}-{year_to}...")
    df = pd.read_csv(config.SAMPLES_CSV, low_memory=False)
    df["DH_Inicio"] = pd.to_datetime(df["DH_Inicio"], errors="coerce", format="ISO8601")
    df = df.dropna(subset=["DH_Inicio", "lat", "lon", "snapped_lat", "snapped_lon"])
    y = df["DH_Inicio"].dt.year
    df = df[(y >= year_from) & (y <= year_to)].reset_index(drop=True)
    n_cells = df[["snapped_lat", "snapped_lon"]].drop_duplicates().shape[0]
    print(f"  {len(df)} samples, {n_cells} unique cells")
    gdf = gpd.GeoDataFrame(
        df, geometry=gpd.points_from_xy(df.lon, df.lat), crs="EPSG:4326"
    )
    print(f"  [load] {time.time() - t0:.1f}s")

    # Per-chunk intermediates land on disk to keep memory bounded —
    # earlier versions held everything in RAM and OOM'd on chunk 6 (23+
    # GB). With on-disk intermediates, peak memory is one chunk's
    # cell_table + one chunk's apply result + xarray load buffers.
    tmp_dir = config.PROCESSED_DIR / f"_tmp_{suffix}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    print(f"Per-chunk intermediates → {tmp_dir}")

    # We open ERA5 fresh in each iteration so the lazy task graph from the
    # previous chunk is fully released between iterations.
    print("Probing ERA5-Land variable list...")
    ds_probe = load_era5_dataset()
    all_vars = list(ds_probe.data_vars)
    del ds_probe
    gc.collect()
    var_chunks = [all_vars[i:i + VAR_CHUNK_SIZE] for i in range(0, len(all_vars), VAR_CHUNK_SIZE)]
    print(f"  {len(all_vars)} vars in {len(var_chunks)} chunks of ≤{VAR_CHUNK_SIZE}")

    point_paths: list[Path] = []
    ts_paths: list[Path] = []

    for ci, chunk_vars in enumerate(var_chunks, start=1):
        print(f"\n— chunk {ci}/{len(var_chunks)}: {chunk_vars}", flush=True)
        tc = time.time()
        ds_chunk = load_era5_dataset()
        cell_table = era5_to_cell_table(
            gdf, dataset=ds_chunk[chunk_vars], lookback_hours=lookback_hours,
            verbose=True, var_chunk_size=VAR_CHUNK_SIZE,
        )
        del ds_chunk
        gc.collect()
        print(f"  cell table built in {time.time() - tc:.1f}s ({len(cell_table)} cells)")

        tp = time.time()
        chunk_point = gdf.apply(
            lambda row: lookup_point(cell_table, row["snapped_lat"], row["snapped_lon"], row["DH_Inicio"]),
            axis=1,
        ).astype("float32")
        p_path = tmp_dir / f"point_chunk_{ci:02d}.parquet"
        chunk_point.to_parquet(p_path, index=False)
        point_paths.append(p_path)
        del chunk_point
        gc.collect()
        print(f"  point lookup: {time.time() - tp:.1f}s  → {p_path.name}")

        ts_t = time.time()
        chunk_ts = gdf.apply(
            lambda row: lookup_sequence(
                cell_table, row["snapped_lat"], row["snapped_lon"], row["DH_Inicio"],
                lookback_hours=lookback_hours,
            ),
            axis=1,
        ).astype("float32")
        t_path = tmp_dir / f"ts_chunk_{ci:02d}.parquet"
        chunk_ts.to_parquet(t_path, index=False)
        ts_paths.append(t_path)
        del chunk_ts
        gc.collect()
        print(f"  sequence lookup: {time.time() - ts_t:.1f}s  → {t_path.name}")

        del cell_table
        gc.collect()

    # Merge chunk parquets column-wise + write final CSVs.
    print("\nAssembling and writing CSVs...")
    tw = time.time()
    base = pd.DataFrame(gdf.drop(columns=[c for c in ("geometry", "centroid") if c in gdf.columns]))
    base = base.reset_index(drop=True)

    point_parts = [pd.read_parquet(p) for p in point_paths]
    final_point = pd.concat([base] + point_parts, axis=1)
    final_point.to_csv(point_csv, index=False)
    print(f"  [write_point] {time.time() - tw:.1f}s → {point_csv.name} ({final_point.shape[1]} cols)")
    del final_point, point_parts
    gc.collect()

    tw = time.time()
    ts_parts = [pd.read_parquet(p) for p in ts_paths]
    final_ts = pd.concat([base] + ts_parts, axis=1)
    final_ts.to_csv(ts_csv, index=False)
    print(f"  [write_timeseries] {time.time() - tw:.1f}s → {ts_csv.name} ({final_ts.shape[1]} cols)")
    del final_ts, ts_parts
    gc.collect()

    # Clean up tmp parquets.
    for p in point_paths + ts_paths:
        p.unlink(missing_ok=True)
    try:
        tmp_dir.rmdir()
    except OSError:
        pass

    t4 = time.time()
    print(f"\nStage 3 — sampling terrain rasters into {terrain_csv.name}...")
    pts = pd.read_csv(point_csv, low_memory=False)
    geometry = [Point(xy) for xy in zip(pts["lon"], pts["lat"])]
    pts_gdf = gpd.GeoDataFrame(pts, geometry=geometry, crs="EPSG:4326")
    for name, path in config.TERRAIN_FILES.items():
        print(f"  extracting {name} from {path.name}...")
        pts_gdf[name] = sample_raster(pts_gdf, path)
    pts_gdf["aspect_rad"] = np.radians(pts_gdf["aspect"])
    pts_gdf["aspect_sin"] = np.sin(pts_gdf["aspect_rad"])
    pts_gdf["aspect_cos"] = np.cos(pts_gdf["aspect_rad"])
    pd.DataFrame(pts_gdf.drop(columns="geometry")).to_csv(terrain_csv, index=False)
    print(f"  [stage3] {time.time() - t4:.1f}s → {terrain_csv}")

    print(f"\nTotal: {time.time() - t0:.1f}s")
    print(f"Wrote:\n  {point_csv}\n  {ts_csv}\n  {terrain_csv}")


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        print(
            "usage: build_era5_partial_dataset.py YEAR_FROM YEAR_TO [LOOKBACK_DAYS]",
            file=sys.stderr,
        )
        sys.exit(2)
    lb_hours = int(sys.argv[3]) * 24 if len(sys.argv) == 4 else None
    main(int(sys.argv[1]), int(sys.argv[2]), lb_hours)
