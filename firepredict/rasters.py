"""Raster sampling helpers."""
from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio


def sample_raster(gdf: gpd.GeoDataFrame, tif_path: str | Path) -> list[float]:
    """Sample a single-band raster at every point geometry in ``gdf``.

    Reprojects points into the raster's CRS before sampling and replaces the
    raster's NoData sentinel with NaN.
    """
    with rasterio.open(tif_path) as src:
        gdf_temp = gdf.to_crs(src.crs)
        coords = [(x, y) for x, y in zip(gdf_temp.geometry.x, gdf_temp.geometry.y)]
        sampled = [val[0] for val in src.sample(coords)]
        nodata = src.nodata if src.nodata is not None else -9999
        return [np.nan if v == nodata else v for v in sampled]
