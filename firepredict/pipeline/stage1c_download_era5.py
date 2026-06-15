"""Stage 1c — download ERA5-Land NetCDFs from Copernicus CDS.

Downloads hourly ERA5-Land for every year in ``config.ERA5_YEARS``, covering
the active region's bbox (``config.ERA5_BBOX_*``) and the variables in
``config.ERA5_VARIABLES``. Each year is split into monthly chunks, and each
chunk into the request groups in ``config.ERA5_REQUEST_GROUPS`` (to stay under
the CDS per-request cost cap). Output is one NetCDF per (year, chunk, group):
``data/era5_land/<region>_<year>_<chunk>_<group>.nc``.

Idempotent — skips any chunk file already present on disk. Safe to re-run.

Setup required once per machine:

1. Register a free account at https://cds.climate.copernicus.eu/
2. Put your API key in ``~/.cdsapirc`` (see CDS "how to api" page).
3. Accept the ERA5-Land licence on
   https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land

After that, ``python -m firepredict.pipeline.stage1c_download_era5`` will
work. Each year is a single CDS job that can take minutes-to-hours on the
server side; the script blocks until the download is done.

NOTE — a full multi-year regional pull is large (many GB) and the CDS jobs can
take hours of server-side queue and processing time. It is often more practical
to run the bulk download once on a long-running machine and copy the resulting
NetCDFs into ``data/era5_land/``. This module works the same either way, as long
as a valid ``~/.cdsapirc`` is present.
"""
from __future__ import annotations

from .. import config
from ..weather_era5 import download_era5_years


def main() -> None:
    config.ensure_output_dirs()
    years = config.ERA5_YEARS
    n_chunks = config.era5_chunks_per_year()
    n_groups = len(config.ERA5_REQUEST_GROUPS)
    total_chunks = len(years) * n_chunks
    total_files = total_chunks * n_groups
    print(
        f"Downloading {config.ERA5_DATASET} for "
        f"{len(years)} years × {n_chunks} chunks/year "
        f"× {n_groups} requests/chunk = {total_chunks} chunks ({total_files} files)",
        flush=True,
    )
    print(f"  region       : {config.REGION}", flush=True)
    print(f"  years        : {years[0]}–{years[-1]}", flush=True)
    print(f"  months/chunk : {config.ERA5_MONTHS_PER_CHUNK}", flush=True)
    print(
        f"  bbox N/W/S/E : {config.ERA5_BBOX_NORTH}/{config.ERA5_BBOX_WEST}/"
        f"{config.ERA5_BBOX_SOUTH}/{config.ERA5_BBOX_EAST}",
        flush=True,
    )
    for name, vars_ in config.ERA5_REQUEST_GROUPS:
        print(
            f"  group {name:<10s}({len(vars_):>2d}): {', '.join(vars_)}",
            flush=True,
        )
    print(f"  destination  : {config.ERA5_DIR}", flush=True)
    print(flush=True)
    paths = download_era5_years(years, max_workers=1)
    present = [p for p in paths if p.exists()]
    print(f"\nDone — {len(present)} / {len(paths)} files on disk", flush=True)


if __name__ == "__main__":
    main()
