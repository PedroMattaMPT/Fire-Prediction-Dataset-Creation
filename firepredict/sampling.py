"""Negative-sample generation for fire-occurrence prediction.

See ``docs/open-problems.md §2``. The approach:

1. **Cell pool**: the snapped 0.1° grid cells that contain at least one
   real fire (case-control). Cells that have never burned are excluded —
   they're trivially negative and would bias the classifier toward
   "does this cell ever burn" instead of "when will it burn".

2. **Forbidden windows**: for every fire in a cell, the window
   ``[DH_Inicio - halo, DH_Fim + halo]`` is off-limits for negatives in
   that cell, where ``halo = lookback_days + buffer_days``. The
   ``lookback`` contribution prevents a negative's 3-day feature history
   from intersecting an active fire (data leak). The ``buffer`` is an
   additional safety margin because weather right before a fire
   contributes to ignition — labelling that moment as "no fire" would
   teach the model the opposite of what we want.

3. **Sampling**: for each positive, draw ``n_per_positive`` uniform random
   ``(cell, hour)`` pairs from the pool × observed time range. Reject any
   draw that falls inside the cell's forbidden windows. Dedupe accidental
   collisions. Warn (not crash) if the requested count can't be satisfied
   — hot cells with large buffers can squeeze the available hours hard.
"""
from __future__ import annotations

from typing import Dict, Tuple

import numpy as np
import pandas as pd

from . import config
from .weather_bulk import snap_to_grid


def _attach_snapped(fires_df: pd.DataFrame) -> pd.DataFrame:
    if "snapped_lat" in fires_df.columns and "snapped_lon" in fires_df.columns:
        return fires_df
    snapped = fires_df[["lat", "lon"]].apply(
        lambda row: snap_to_grid(row["lat"], row["lon"]),
        axis=1,
        result_type="expand",
    )
    out = fires_df.copy()
    out["snapped_lat"] = snapped[0]
    out["snapped_lon"] = snapped[1]
    return out


def _cell_key(slat: float, slon: float) -> Tuple[float, float]:
    return (round(float(slat), 6), round(float(slon), 6))


def _to_int64_ns(values: pd.Series | pd.Index) -> np.ndarray:
    """Convert a datetime series/index to int64 nanoseconds since epoch.

    The CSV loader yields ``datetime64[us]`` on some pandas/numpy versions.
    Calling ``.astype('int64')`` on a microsecond datetime gives
    microseconds-since-epoch, not nanoseconds — so we force the unit first.
    """
    return np.asarray(values, dtype="datetime64[ns]").astype("int64")


def _build_forbidden_map(
    positives: pd.DataFrame,
    lookback_days: int,
    buffer_days: int,
) -> Dict[Tuple[float, float], Tuple[np.ndarray, np.ndarray]]:
    """Return ``{(slat, slon): (starts_ns, ends_ns)}`` — sorted, merged.

    Missing ``DH_Fim`` is filled with ``DH_Inicio + 1 day`` so fires of
    unknown duration still contribute a reasonable forbidden window.
    """
    df = positives.copy()
    dh_inicio = pd.to_datetime(df["DH_Inicio"], errors="coerce")
    if "DH_Fim" in df.columns:
        dh_fim = pd.to_datetime(df["DH_Fim"], errors="coerce")
    else:
        dh_fim = pd.Series([pd.NaT] * len(df), index=df.index)
    dh_fim = dh_fim.fillna(dh_inicio + pd.Timedelta(days=1))

    halo = pd.Timedelta(days=lookback_days + buffer_days)
    df["_forbidden_start"] = _to_int64_ns(dh_inicio - halo)
    df["_forbidden_end"] = _to_int64_ns(dh_fim + halo)
    df = df.dropna(subset=["snapped_lat", "snapped_lon"])

    result: Dict[Tuple[float, float], Tuple[np.ndarray, np.ndarray]] = {}
    for (slat, slon), group in df.groupby(["snapped_lat", "snapped_lon"]):
        pairs = sorted(
            zip(
                group["_forbidden_start"].to_numpy(),
                group["_forbidden_end"].to_numpy(),
            )
        )
        merged_starts: list[int] = []
        merged_ends: list[int] = []
        cur_start, cur_end = pairs[0]
        for s, e in pairs[1:]:
            if s <= cur_end:
                cur_end = max(cur_end, e)
            else:
                merged_starts.append(cur_start)
                merged_ends.append(cur_end)
                cur_start, cur_end = s, e
        merged_starts.append(cur_start)
        merged_ends.append(cur_end)
        result[_cell_key(slat, slon)] = (
            np.array(merged_starts, dtype="int64"),
            np.array(merged_ends, dtype="int64"),
        )
    return result


def _forbidden_fraction(
    forbidden_map: Dict[Tuple[float, float], Tuple[np.ndarray, np.ndarray]],
    total_span_ns: int,
) -> pd.Series:
    """Per-cell fraction of the time range that is forbidden. Debug stat."""
    rows = {}
    for key, (starts, ends) in forbidden_map.items():
        forbidden_ns = int((ends - starts).sum())
        rows[key] = min(1.0, forbidden_ns / total_span_ns) if total_span_ns else 0.0
    return pd.Series(rows)


def _filter_forbidden(
    candidates: pd.DataFrame,
    forbidden_map: Dict[Tuple[float, float], Tuple[np.ndarray, np.ndarray]],
) -> np.ndarray:
    """Vectorised rejection mask: True = candidate survives (not forbidden)."""
    n = len(candidates)
    if n == 0:
        return np.zeros(0, dtype=bool)
    times_ns = _to_int64_ns(candidates["DH_Inicio"])
    mask = np.ones(n, dtype=bool)

    candidates = candidates.reset_index(drop=True)
    for (slat, slon), group in candidates.groupby(["snapped_lat", "snapped_lon"]):
        key = _cell_key(slat, slon)
        entry = forbidden_map.get(key)
        if entry is None:
            continue
        starts, ends = entry
        idxs = group.index.to_numpy()
        ts = times_ns[idxs]
        pos = np.searchsorted(starts, ts, side="right") - 1
        hit = np.zeros(len(ts), dtype=bool)
        valid = pos >= 0
        if valid.any():
            hit[valid] = ts[valid] <= ends[pos[valid]]
        mask[idxs] &= ~hit
    return mask


def generate_negatives(
    positives: pd.DataFrame,
    n_per_positive: int = config.NEG_SAMPLES_PER_POSITIVE,
    lookback_days: int = config.WEATHER_LOOKBACK_DAYS,
    buffer_days: int = 15,
    random_state: int = config.SAMPLE_RANDOM_SEED,
    *,
    verbose: bool = True,
) -> pd.DataFrame:
    """Return synthetic non-fire samples.

    ``positives`` must contain ``DH_Inicio``, ``lat``, ``lon``, and
    optionally ``DH_Fim`` and ``snapped_lat``/``snapped_lon`` (auto-snapped
    if missing). A negative is rejected if it lies inside the forbidden
    window of any fire in the same cell (see module docstring).
    """
    positives = _attach_snapped(positives).copy()
    positives["DH_Inicio"] = pd.to_datetime(positives["DH_Inicio"], errors="coerce")
    positives = positives.dropna(subset=["DH_Inicio", "snapped_lat", "snapped_lon"])

    n_target = len(positives) * n_per_positive
    if n_target == 0:
        return pd.DataFrame(
            columns=[
                "label",
                "source",
                "DH_Inicio",
                "lat",
                "lon",
                "snapped_lat",
                "snapped_lon",
            ]
        )

    forbidden_map = _build_forbidden_map(positives, lookback_days, buffer_days)

    cells = (
        positives[["snapped_lat", "snapped_lon"]].drop_duplicates().reset_index(drop=True)
    )
    n_cells = len(cells)

    min_time = positives["DH_Inicio"].min().floor("h")
    max_time = positives["DH_Inicio"].max().ceil("h")
    total_hours = int((max_time - min_time).total_seconds() // 3600)
    if total_hours <= 0:
        raise ValueError(
            "Observed positives span less than one hour — cannot sample negatives."
        )

    if verbose:
        span_ns = int((max_time - min_time).value)
        forbid_frac = _forbidden_fraction(forbidden_map, span_ns)
        print(
            f"  buffer={buffer_days}d lookback={lookback_days}d: "
            f"{n_cells} cells, forbidden fraction median "
            f"{forbid_frac.median():.1%}, max {forbid_frac.max():.1%}"
        )

    rng = np.random.default_rng(random_state)
    accepted: list[pd.DataFrame] = []
    accepted_total = 0
    # Over-sample aggressively because hot cells reject most draws.
    batch_size = max(n_target * 2, 50_000)
    max_attempts = n_target * 40
    attempts = 0

    seen_keys: set[Tuple[float, float, int]] = set()

    while accepted_total < n_target and attempts < max_attempts:
        n_draw = batch_size
        cell_idx = rng.integers(0, n_cells, size=n_draw)
        hour_off = rng.integers(0, total_hours, size=n_draw)
        slats = cells["snapped_lat"].to_numpy()[cell_idx]
        slons = cells["snapped_lon"].to_numpy()[cell_idx]
        times = min_time + pd.to_timedelta(hour_off, unit="h")

        batch = pd.DataFrame(
            {
                "snapped_lat": slats,
                "snapped_lon": slons,
                "DH_Inicio": times,
            }
        )
        batch = batch.drop_duplicates(
            subset=["snapped_lat", "snapped_lon", "DH_Inicio"]
        )
        keep = _filter_forbidden(batch, forbidden_map)
        batch = batch[keep]

        batch_keys = list(
            zip(
                batch["snapped_lat"].round(6).tolist(),
                batch["snapped_lon"].round(6).tolist(),
                _to_int64_ns(batch["DH_Inicio"]).tolist(),
            )
        )
        novel = [k not in seen_keys for k in batch_keys]
        batch = batch[novel]
        seen_keys.update(k for k, ok in zip(batch_keys, novel) if ok)

        accepted.append(batch)
        accepted_total += len(batch)
        attempts += n_draw

    if accepted_total == 0:
        raise RuntimeError(
            "Could not sample any negatives — every candidate was forbidden. "
            "Check buffer_days / lookback_days."
        )

    combined = pd.concat(accepted, ignore_index=True)
    if len(combined) > n_target:
        combined = combined.iloc[:n_target].copy()
    elif len(combined) < n_target and verbose:
        print(
            f"  warning: wanted {n_target} negatives, produced {len(combined)} "
            f"(buffer={buffer_days}d may be too tight for hot cells)"
        )

    combined["label"] = 0
    combined["source"] = "negative"
    combined["lat"] = combined["snapped_lat"]
    combined["lon"] = combined["snapped_lon"]

    return combined[
        [
            "label",
            "source",
            "DH_Inicio",
            "lat",
            "lon",
            "snapped_lat",
            "snapped_lon",
        ]
    ]


def label_positives(positives: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of ``positives`` with ``label=1`` and a slim schema."""
    df = _attach_snapped(positives).copy()
    df["DH_Inicio"] = pd.to_datetime(df["DH_Inicio"], errors="coerce")
    df = df.dropna(subset=["DH_Inicio", "snapped_lat", "snapped_lon"])
    df["label"] = 1
    df["source"] = "fire"

    cols = [
        "label",
        "source",
        "Cod_SGIF",
        "Causa_Tipo",
        "DH_Inicio",
        "DH_Fim",
        "lat",
        "lon",
        "snapped_lat",
        "snapped_lon",
    ]
    for c in cols:
        if c not in df.columns:
            df[c] = pd.NA
    return df[cols]


def build_samples_table(
    positives: pd.DataFrame,
    n_per_positive: int = config.NEG_SAMPLES_PER_POSITIVE,
    lookback_days: int = config.WEATHER_LOOKBACK_DAYS,
    buffer_days: int = 15,
    random_state: int = config.SAMPLE_RANDOM_SEED,
    *,
    verbose: bool = True,
) -> pd.DataFrame:
    """Return positives + negatives for one ``buffer_days`` setting."""
    pos = label_positives(positives)
    neg = generate_negatives(
        positives,
        n_per_positive=n_per_positive,
        lookback_days=lookback_days,
        buffer_days=buffer_days,
        random_state=random_state,
        verbose=verbose,
    )

    # Negatives don't have the fire-only metadata columns.
    for c in ("Cod_SGIF", "Causa_Tipo", "DH_Fim"):
        if c not in neg.columns:
            neg[c] = pd.NA
    neg = neg[pos.columns]

    samples = pd.concat([pos, neg], ignore_index=True)
    samples.insert(0, "sample_id", np.arange(len(samples), dtype=np.int64))
    return samples
