from typing import Mapping, Sequence, Tuple

import numpy as np
import pytest
from anndata import AnnData
from numpy.random import default_rng

from spatialdata import SpatialData
from spatialdata._core.models import validate_raster
from spatialdata._types import NDArray

RNG = default_rng()


@pytest.fixture()
def images() -> SpatialData:
    return SpatialData(images=_get_raster(3, shape=(3, 64, 64), name="image", dtype="float"))


@pytest.fixture()
def images_multiscale() -> SpatialData:
    return SpatialData(
        images=_get_raster(3, shape=(3, 64, 64), dtype="float", name="image_multiscale", multiscale=True)
    )


@pytest.fixture()
def labels() -> SpatialData:
    return SpatialData(labels=_get_raster(3, shape=(64, 64), name="label", dtype="int"))


@pytest.fixture()
def labels_multiscale() -> SpatialData:
    return SpatialData(labels=_get_raster(3, shape=(64, 64), dtype="int", name="label_multiscale", multiscale=True))


@pytest.fixture()
def points() -> SpatialData:
    return SpatialData(points=_get_points(3))


@pytest.fixture()
def table() -> SpatialData:
    return SpatialData(table=_get_table())


@pytest.fixture()
def empty_images() -> SpatialData:
    pytest.skip("empty images not supported")
    return SpatialData(images={"empty": np.zeros((0, 0, 0))})


@pytest.fixture()
def empty_labels() -> SpatialData:
    pytest.skip("empty labels not supported")
    return SpatialData(labels={"empty": np.zeros((0, 0), dtype=int)})


@pytest.fixture()
def empty_points() -> SpatialData:
    return SpatialData(points={"empty": AnnData(shape=(0, 0), obsm={"spatial": np.zeros((0, 2))})})


@pytest.fixture()
def empty_table() -> SpatialData:
    return SpatialData(table=AnnData(shape=(0, 0)))


@pytest.fixture(
    # params=["labels"]
    params=["full"]
    + ["images", "labels", "points", "table"]
    + ["empty_" + x for x in ["images", "labels", "points", "table"]]
)
def sdata(request) -> SpatialData:
    if request.param == "full":
        s = SpatialData(
            images=_get_raster(3, shape=(3, 64, 64), name="image", dtype="float"),
            labels=_get_raster(3, shape=(64, 64), name="label", dtype="int"),
            points=_get_points(2),
            table=_get_table(),
        )
    else:
        s = request.getfixturevalue(request.param)
    return s


def _get_raster(
    n: int,
    shape: Tuple[int, ...],
    dtype: str,
    name: str,
    multiscale: bool = False,
) -> Mapping[str, Sequence[NDArray]]:
    out = {}
    for i in range(n):
        if dtype == "float":
            arr = RNG.normal(size=shape)
        elif dtype == "int":
            arr = RNG.integers(0, 100, size=shape)
        name = f"{name}{i}"
        if multiscale:
            image = validate_raster(arr, kind="Image", name=name, scale_factors=[2, 4])
        else:
            image = validate_raster(arr, kind="Image", name=name)
        out[name] = image
    return out


def _get_points(n: int) -> Mapping[str, Sequence[NDArray]]:
    return {
        f"points_{i}": AnnData(shape=(100, 0), obsm={"spatial": RNG.integers(0, 10, size=(100, 2))}) for i in range(n)
    }


def _get_table() -> AnnData:
    return AnnData(RNG.normal(size=(100, 10)))
