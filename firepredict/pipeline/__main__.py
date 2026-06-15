"""Build the full labelled dataset end-to-end (every stage in order).

    python -m firepredict.pipeline

Runs in order: stage1 (clean_fires) → stage1b (generate_samples) →
stage1c (download_era5, idempotent) → stage2 (add_weather) →
stage3 (add_terrain). Each stage reads the previous stage's CSV, so the
chain must run in sequence. The final stage writes the labelled
fire/no-fire dataset (``final_fire_weather_terrain_<year>.csv``) ready for
model training in a separate project. Individual stages are still runnable
on their own via ``python -m firepredict.pipeline.stage<N>_<name>``.
"""
from __future__ import annotations

from . import (
    stage1_clean_fires,
    stage1b_generate_samples,
    stage1c_download_era5,
    stage2_add_weather,
    stage3_add_terrain,
)

STAGES = (
    ("stage1_clean_fires", stage1_clean_fires.main),
    ("stage1b_generate_samples", stage1b_generate_samples.main),
    ("stage1c_download_era5", stage1c_download_era5.main),
    ("stage2_add_weather", stage2_add_weather.main),
    ("stage3_add_terrain", stage3_add_terrain.main),
)


def main() -> None:
    for name, stage_main in STAGES:
        banner = f" {name} ".center(72, "=")
        print(f"\n{banner}\n")
        stage_main()
    print("\n" + " pipeline complete ".center(72, "=") + "\n")


if __name__ == "__main__":
    main()
