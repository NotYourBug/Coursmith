"""Ordered installed migrations; previously applied versions stay immutable."""

from . import (v001_baseline, v002_operations, v003_product_lifecycle,
               v004_delivery_storage, v005_legacy_delivery)

MIGRATIONS = (v001_baseline, v002_operations, v003_product_lifecycle,
              v004_delivery_storage, v005_legacy_delivery)
