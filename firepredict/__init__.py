"""firepredict — region-agnostic wildfire-ignition *dataset creation*.

Fuses fire records, ERA5-Land weather, and terrain rasters into a single
labelled fire/no-fire table ready for model training. Model code lives in a
separate project; this package stops at the dataset.
"""

__version__ = "0.1.0"
