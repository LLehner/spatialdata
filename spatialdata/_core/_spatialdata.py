from __future__ import annotations

import hashlib
import os
from collections.abc import Generator
from types import MappingProxyType
from typing import Optional, Union

import pyarrow as pa
import zarr
from anndata import AnnData
from geopandas import GeoDataFrame
from multiscale_spatial_image.multiscale_spatial_image import MultiscaleSpatialImage
from ome_zarr.io import parse_url
from ome_zarr.types import JSONDict
from spatial_image import SpatialImage

from spatialdata._core._spatial_query import (
    BaseSpatialRequest,
    BoundingBoxRequest,
    _bounding_box_query_points_dict,
)
from spatialdata._core.core_utils import SpatialElement, _get_transform, get_dims
from spatialdata._core.models import (
    Image2DModel,
    Image3DModel,
    Labels2DModel,
    Labels3DModel,
    PointsModel,
    PolygonsModel,
    ShapesModel,
    TableModel,
)
from spatialdata._core.transformations import BaseTransformation
from spatialdata._io.write import (
    write_image,
    write_labels,
    write_points,
    write_polygons,
    write_shapes,
    write_table,
)
from spatialdata._logging import logger

# schema for elements
Label2D_s = Labels2DModel()
Label3D_s = Labels3DModel()
Image2D_s = Image2DModel()
Image3D_s = Image3DModel()
Polygon_s = PolygonsModel()
Point_s = PointsModel()
Shape_s = ShapesModel()
Table_s = TableModel()


class SpatialData:
    """
    The SpatialData object.

    The SpatialData object is a modular container for arbitrary combinations of spatial elements. The elements
    can be accesses separately and are stored as standard types (:class:`anndata.AnnData`,
    :class:`geopandas.GeoDataFrame`, :class:`xarray.DataArray`).


    Parameters
    ----------
    images
        Dict of 2D and 3D image elements. The following parsers are available: :class:`~spatialdata.Image2DModel`,
        :class:`~spatialdata.Image3DModel`.
    labels
        Dict of 2D and 3D labels elements. Labels are regions, they can't contain annotation, but they can be
        annotated by a table. The following parsers are available: :class:`~spatialdata.Labels2DModel`,
        :class:`~spatialdata.Labels3DModel`.
    points
        Dict of points elements. Points can contain annotations. The following parsers is available:
        :class:`~spatialdata.PointsModel`.
    polygons
        Dict of 2D polygons elements. They can't contain annotation but they can be annotated
        by a table. The following parsers is available: :class:`~spatialdata.PolygonsModel`.
    shapes
        Dict of 2D shapes elements (circles, squares). Shapes are regions, they can't contain annotation but they
        can be annotated by a table. The following parsers is available: :class:`~spatialdata.ShapesModel`.
    table
        AnnData table containing annotations for regions (labels, polygons, shapes). The following parsers is
        available: :class:`~spatialdata.TableModel`.

    Notes
    -----
    The spatial elements are stored with standard types:

        - images and labels are stored as :class:`spatial_image.SpatialImage` or :class:`multiscale_spatial_image.MultiscaleSpatialImage` objects, which are respectively equivalent to :class:`xarray.DataArray` and to a :class:`datatree.DataTree` of :class:`xarray.DataArray` objects.
        - points and shapes are stored as :class:`anndata.AnnData` objects, with the spatial coordinates stored in the obsm slot.
        - polygons are stored as :class:`geopandas.GeoDataFrame`.
        - the table are stored as :class:`anndata.AnnData` objects, with the spatial coordinates stored in the obsm slot.

    The table can annotate regions (shapes, polygons or labels) and can be used to store additional information.
    Points are not regions but 0-dimensional locations. They can't be annotated by a table, but they can store
    annotation directly.

    The elements need to pass a validation step. To construct valid elements you can use the parsers that we
    provide (:class:`~spatialdata.Image2DModel`, :class:`~spatialdata.Image3DModel`, :class:`~spatialdata.Labels2DModel`, :class:`~spatialdata.Labels3DModel`, :class:`~spatialdata.PointsModel`, :class:`~spatialdata.PolygonsModel`, :class:`~spatialdata.ShapesModel`, :class:`~spatialdata.TableModel`).
    """

    _images: dict[str, Union[SpatialImage, MultiscaleSpatialImage]] = MappingProxyType({})  # type: ignore[assignment]
    _labels: dict[str, Union[SpatialImage, MultiscaleSpatialImage]] = MappingProxyType({})  # type: ignore[assignment]
    _points: dict[str, pa.Table] = MappingProxyType({})  # type: ignore[assignment]
    _polygons: dict[str, GeoDataFrame] = MappingProxyType({})  # type: ignore[assignment]
    _shapes: dict[str, AnnData] = MappingProxyType({})  # type: ignore[assignment]
    _table: Optional[AnnData] = None
    path: Optional[str] = None

    def __init__(
        self,
        images: dict[str, Union[SpatialImage, MultiscaleSpatialImage]] = MappingProxyType({}),  # type: ignore[assignment]
        labels: dict[str, Union[SpatialImage, MultiscaleSpatialImage]] = MappingProxyType({}),  # type: ignore[assignment]
        points: dict[str, pa.Table] = MappingProxyType({}),  # type: ignore[assignment]
        polygons: dict[str, GeoDataFrame] = MappingProxyType({}),  # type: ignore[assignment]
        shapes: dict[str, AnnData] = MappingProxyType({}),  # type: ignore[assignment]
        table: Optional[AnnData] = None,
    ) -> None:
        self.path = None
        if images is not None:
            self._images: dict[str, Union[SpatialImage, MultiscaleSpatialImage]] = {}
            for k, v in images.items():
                self._add_image_in_memory(name=k, image=v)

        if labels is not None:
            self._labels: dict[str, Union[SpatialImage, MultiscaleSpatialImage]] = {}
            for k, v in labels.items():
                self._add_labels_in_memory(name=k, labels=v)

        if polygons is not None:
            self._polygons: dict[str, GeoDataFrame] = {}
            for k, v in polygons.items():
                self._add_polygons_in_memory(name=k, polygons=v)

        if shapes is not None:
            self._shapes: dict[str, AnnData] = {}
            for k, v in shapes.items():
                self._add_shapes_in_memory(name=k, shapes=v)

        if points is not None:
            self._points: dict[str, pa.Table] = {}
            for k, v in points.items():
                self._add_points_in_memory(name=k, points=v)

        if table is not None:
            Table_s.validate(table)
            self._table = table

        self._query = QueryManager(self)

    @property
    def query(self) -> QueryManager:
        return self._query

    def _add_image_in_memory(
        self, name: str, image: Union[SpatialImage, MultiscaleSpatialImage], overwrite: bool = False
    ) -> None:
        if name in self._images:
            if not overwrite:
                raise ValueError(f"Image {name} already exists in the dataset.")
            else:
                del self._images[name]
        ndim = len(get_dims(image))
        if ndim == 3:
            Image2D_s.validate(image)
            self._images[name] = image
        elif ndim == 4:
            Image3D_s.validate(image)
            self._images[name] = image
        else:
            raise ValueError("Only czyx and cyx images supported")

    def _add_labels_in_memory(
        self, name: str, labels: Union[SpatialImage, MultiscaleSpatialImage], overwrite: bool = False
    ) -> None:
        if name in self._labels:
            if not overwrite:
                raise ValueError(f"Labels {name} already exists in the dataset.")
            else:
                del self._labels[name]
        ndim = len(get_dims(labels))
        if ndim == 2:
            Label2D_s.validate(labels)
            self._labels[name] = labels
        elif ndim == 3:
            Label3D_s.validate(labels)
            self._labels[name] = labels
        else:
            raise ValueError(f"Only yx and zyx labels supported, got {ndim} dimensions")

    def _add_polygons_in_memory(self, name: str, polygons: GeoDataFrame, overwrite: bool = False) -> None:
        if name in self._polygons:
            if not overwrite:
                raise ValueError(f"Polygons {name} already exists in the dataset.")
            else:
                del self._polygons[name]
        Polygon_s.validate(polygons)
        self._polygons[name] = polygons

    def _add_shapes_in_memory(self, name: str, shapes: AnnData, overwrite: bool = False) -> None:
        if name in self._shapes:
            if not overwrite:
                raise ValueError(f"Shapes {name} already exists in the dataset.")
            else:
                del self._shapes[name]
        Shape_s.validate(shapes)
        self._shapes[name] = shapes

    def _add_points_in_memory(self, name: str, points: pa.Table, overwrite: bool = False) -> None:
        if name in self._points:
            if not overwrite:
                raise ValueError(f"Points {name} already exists in the dataset.")
            else:
                del self._points[name]
        Point_s.validate(points)
        self._points[name] = points

    def is_backed(self) -> bool:
        """Check if the data is backed by a Zarr storage or it is in-memory."""
        return self.path is not None

    def _get_group_for_element(self, name: str, element_type: str) -> zarr.Group:
        store = parse_url(self.path, mode="r+").store
        root = zarr.group(store=store)
        assert element_type in ["images", "labels", "points", "polygons", "shapes"]
        element_type_group = root.require_group(element_type)
        element_group = element_type_group.require_group(name)
        return element_group

    def _init_add_element(self, name: str, element_type: str, overwrite: bool) -> zarr.Group:
        if self.path is None:
            # in the future we can relax this, but this ensures that we don't have objects that are partially backed
            # and partially in memory
            raise RuntimeError(
                "The data is not backed by a Zarr storage. In order to add new elements after "
                "initializing a SpatialData object you need to call SpatialData.write() first"
            )
        store = parse_url(self.path, mode="r+").store
        root = zarr.group(store=store)
        assert element_type in ["images", "labels", "points", "polygons", "shapes"]
        # not need to create the group for labels as it is already handled by ome-zarr-py
        if element_type != "labels":
            if element_type not in root:
                elem_group = root.create_group(name=element_type)
            else:
                elem_group = root[element_type]
        if overwrite:
            if element_type == "labels":
                if element_type in root:
                    elem_group = root[element_type]
            if name in elem_group:
                del elem_group[name]
        else:
            # bypass is to ensure that elem_group is defined. I don't want to define it as None but either having it
            # or not having it, so if the code tries to access it and it should not be there, it will raise an error
            bypass = False
            if element_type == "labels":
                if element_type in root:
                    elem_group = root[element_type]
                else:
                    bypass = True
            if not bypass:
                if name in elem_group:
                    raise ValueError(f"Element {name} already exists, use overwrite=True to overwrite it")

        if element_type != "labels":
            return elem_group
        else:
            return root

    @staticmethod
    def set_transformation_in_memory(element: SpatialElement, transformation: BaseTransformation) -> None:
        """
        Set/replace the transformation of an element, without writing it to disk.

        Parameters
        ----------
        element
            The element to replace the transformation for.
        transformation
            The new transformation.

        Notes
        -----
        - You can also use the method SpatialData.set_transformation() to set the transformation of an element when it is backed
        """

    def set_transformation(self, element: SpatialElement, transformation: BaseTransformation) -> None:
        """
        Set/replace the transformation of an element, writing it to disk if the SpatialData object is backed.

        Parameters
        ----------
        element
            The element to replace the transformation for.
        transformation
            The new transformation.

        Notes
        -----
        - You can also use the static method SpatialData.set_transformation_in_memory() to set the transformation of an element when it is not backed
        """
        self.set_transformation_in_memory(element, transformation)
        found: list[SpatialElement] = []
        found_element_type: str = ""
        found_element_name: str = ""
        for element_type in ["images", "labels", "points", "polygons", "shapes"]:
            for element_name, element_value in getattr(self, element_type).items():
                if id(element_value) == id(element):
                    found.append(element_value)
                    found_element_type = element_type
                    found_element_name = element_name
        if len(found) == 0:
            raise ValueError("Element not found in the SpatialData object")
        elif len(found) > 1:
            raise ValueError("Element found multiple times in the SpatialData object")

        if self.path is not None:
            group = self._get_group_for_element(name=found_element_name, element_type=found_element_type)
            axes = get_dims(element)
            if isinstance(element, SpatialImage) or isinstance(element, MultiscaleSpatialImage):
                from spatialdata._io.write import (
                    _overwrite_coordinate_transformations_raster,
                )

                if isinstance(element, SpatialImage):
                    transformations = [transformation]
                elif isinstance(element, MultiscaleSpatialImage):
                    # transformations = element.transformations
                    raise NotImplementedError("TODO")
                else:
                    raise ValueError("Unknown element type")
                _overwrite_coordinate_transformations_raster(group=group, axes=axes, transformations=transformations)
            elif isinstance(element, pa.Table) or isinstance(element, GeoDataFrame) or isinstance(element, AnnData):
                from spatialdata._io.write import (
                    _overwrite_coordinate_transformations_non_raster,
                )

                _overwrite_coordinate_transformations_non_raster(group=group, axes=axes, transformation=transformation)
            else:
                raise ValueError("Unknown element type")

    @staticmethod
    def get_transformation(element: SpatialElement) -> Optional[BaseTransformation]:
        """
        Get the transformation of an element.

        Parameters
        ----------
        element
            The element to get the transformation for.

        Returns
        -------
        The transformation of the element.
        """
        return _get_transform(element)

    def add_image(
        self,
        name: str,
        image: Union[SpatialImage, MultiscaleSpatialImage],
        storage_options: Optional[Union[JSONDict, list[JSONDict]]] = None,
        overwrite: bool = False,
        _add_in_memory: bool = True,
    ) -> None:
        """
        Add an image to the SpatialData object.

        Parameters
        ----------
        name
            Key to the element inside the SpatialData object.
        image
            The image to add, the object needs to pass validation (see :class:`~spatialdata.Image2DModel` and :class:`~spatialdata.Image3DModel`).
        storage_options
            Storage options for the Zarr storage.
            See https://zarr.readthedocs.io/en/stable/api/storage.html for more details.
        overwrite
            If True, overwrite the element if it already exists.
        _add_in_memory
            Internal flag, to differentiate between an element added by the user and an element saved to disk by
            write method.

        Notes
        -----
        If the SpatialData object is backed by a Zarr storage, the image will be written to the Zarr storage.
        """
        if _add_in_memory:
            self._add_image_in_memory(name=name, image=image, overwrite=overwrite)
        if self.is_backed():
            elem_group = self._init_add_element(name=name, element_type="images", overwrite=overwrite)
            write_image(
                image=self.images[name],
                group=elem_group,
                name=name,
                storage_options=storage_options,
            )
            # reload the image from the Zarr storage so that now the element is lazy loaded, and most importantly,
            # from the correct storage
            from spatialdata._io.read import _read_multiscale

            assert elem_group.path == "images"
            path = os.path.join(elem_group.store.path, "images", name)
            image = _read_multiscale(path, raster_type="image")
            self._add_image_in_memory(name=name, image=image, overwrite=True)

    def add_labels(
        self,
        name: str,
        labels: Union[SpatialImage, MultiscaleSpatialImage],
        storage_options: Optional[Union[JSONDict, list[JSONDict]]] = None,
        overwrite: bool = False,
        _add_in_memory: bool = True,
    ) -> None:
        """
        Add labels to the SpatialData object.

        Parameters
        ----------
        name
            Key to the element inside the SpatialData object.
        labels
            The labels (masks) to add, the object needs to pass validation (see :class:`~spatialdata.Labels2DModel` and :class:`~spatialdata.Labels3DModel`).
        storage_options
            Storage options for the Zarr storage.
            See https://zarr.readthedocs.io/en/stable/api/storage.html for more details.
        overwrite
            If True, overwrite the element if it already exists.
        _add_in_memory
            Internal flag, to differentiate between an element added by the user and an element saved to disk by
            write method.

        Notes
        -----
        If the SpatialData object is backed by a Zarr storage, the image will be written to the Zarr storage.
        """
        if _add_in_memory:
            self._add_labels_in_memory(name=name, labels=labels, overwrite=overwrite)
        if self.is_backed():
            elem_group = self._init_add_element(name=name, element_type="labels", overwrite=overwrite)
            write_labels(
                labels=self.labels[name],
                group=elem_group,
                name=name,
                storage_options=storage_options,
            )
            # reload the labels from the Zarr storage so that now the element is lazy loaded, and most importantly,
            # from the correct storage
            from spatialdata._io.read import _read_multiscale

            # just a check to make sure that things go as expected
            assert elem_group.path == ""
            path = os.path.join(elem_group.store.path, "labels", name)
            labels = _read_multiscale(path, raster_type="labels")
            self._add_labels_in_memory(name=name, labels=labels, overwrite=True)

    def add_points(
        self,
        name: str,
        points: pa.Table,
        overwrite: bool = False,
        _add_in_memory: bool = True,
    ) -> None:
        """
        Add points to the SpatialData object.

        Parameters
        ----------
        name
            Key to the element inside the SpatialData object.
        points
            The points to add, the object needs to pass validation (see :class:`~spatialdata.PointsModel`).
        storage_options
            Storage options for the Zarr storage.
            See https://zarr.readthedocs.io/en/stable/api/storage.html for more details.
        overwrite
            If True, overwrite the element if it already exists.
        _add_in_memory
            Internal flag, to differentiate between an element added by the user and an element saved to disk by
            write method.

        Notes
        -----
        If the SpatialData object is backed by a Zarr storage, the image will be written to the Zarr storage.
        """
        if _add_in_memory:
            self._add_points_in_memory(name=name, points=points, overwrite=overwrite)
        if self.is_backed():
            elem_group = self._init_add_element(name=name, element_type="points", overwrite=overwrite)
            write_points(
                points=self.points[name],
                group=elem_group,
                name=name,
            )
            # reload the points from the Zarr storage so that now the element is lazy loaded, and most importantly,
            # from the correct storage
            from spatialdata._io.read import _read_points

            points = _read_points(store=elem_group.store)
            self._add_points_in_memory(name=name, points=points, overwrite=True)

    def add_polygons(
        self,
        name: str,
        polygons: GeoDataFrame,
        overwrite: bool = False,
        _add_in_memory: bool = True,
    ) -> None:
        """
        Add polygons to the SpatialData object.

        Parameters
        ----------
        name
            Key to the element inside the SpatialData object.
        polygons
            The polygons to add, the object needs to pass validation (see :class:`~spatialdata.PolygonsModel`).
        storage_options
            Storage options for the Zarr storage.
            See https://zarr.readthedocs.io/en/stable/api/storage.html for more details.
        overwrite
            If True, overwrite the element if it already exists.
        _add_in_memory
            Internal flag, to differentiate between an element added by the user and an element saved to disk by
            write method.

        Notes
        -----
        If the SpatialData object is backed by a Zarr storage, the image will be written to the Zarr storage.
        """
        if _add_in_memory:
            self._add_polygons_in_memory(name=name, polygons=polygons, overwrite=overwrite)
        if self.is_backed():
            elem_group = self._init_add_element(name=name, element_type="polygons", overwrite=overwrite)
            write_polygons(
                polygons=self.polygons[name],
                group=elem_group,
                name=name,
            )
            # no reloading of the file storage since the GeoDataFrame is not lazy loaded

    def add_shapes(
        self,
        name: str,
        shapes: AnnData,
        overwrite: bool = False,
        _add_in_memory: bool = True,
    ) -> None:
        """
        Add shapes to the SpatialData object.

        Parameters
        ----------
        name
            Key to the element inside the SpatialData object.
        shapes
            The shapes to add, the object needs to pass validation (see :class:`~spatialdata.ShapesModel`).
        storage_options
            Storage options for the Zarr storage.
            See https://zarr.readthedocs.io/en/stable/api/storage.html for more details.
        overwrite
            If True, overwrite the element if it already exists.
        _add_in_memory
            Internal flag, to differentiate between an element added by the user and an element saved to disk by
            write method.

        Notes
        -----
        If the SpatialData object is backed by a Zarr storage, the image will be written to the Zarr storage.
        """
        if _add_in_memory:
            self._add_shapes_in_memory(name=name, shapes=shapes, overwrite=overwrite)
        if self.is_backed():
            elem_group = self._init_add_element(name=name, element_type="shapes", overwrite=overwrite)
            write_shapes(
                shapes=self.shapes[name],
                group=elem_group,
                name=name,
            )
            # no reloading of the file storage since the AnnData is not lazy loaded

    def write(
        self, file_path: str, storage_options: Optional[Union[JSONDict, list[JSONDict]]] = None, overwrite: bool = False
    ) -> None:
        """Write the SpatialData object to Zarr."""

        if self.is_backed():
            if self.path == file_path:
                raise ValueError("Can't overwrite the original file")
            elif self.path != file_path and self.path is not None:
                logger.info(f"The Zarr file used for backing will now change from {self.path} to {file_path}")

        if not overwrite and parse_url(file_path, mode="r") is not None:
            raise ValueError("The Zarr store already exists. Use overwrite=True to overwrite the store.")
        else:
            store = parse_url(file_path, mode="w").store
            root = zarr.group(store=store)
            store.close()

        self.path = file_path
        try:
            if len(self.images):
                root.create_group(name="images")
                # add_image will delete and replace the same key in self.images, so we need to make a copy of the
                # keys. Same for the other elements
                keys = list(self.images.keys())
                for el in keys:
                    self.add_image(
                        name=el, image=self.images[el], storage_options=storage_options, _add_in_memory=False
                    )

            if len(self.labels):
                root.create_group(name="labels")
                keys = list(self.labels.keys())
                for el in keys:
                    self.add_labels(
                        name=el, labels=self.labels[el], storage_options=storage_options, _add_in_memory=False
                    )

            if len(self.points):
                root.create_group(name="points")
                keys = list(self.points.keys())
                for el in keys:
                    self.add_points(name=el, points=self.points[el], _add_in_memory=False)

            if len(self.polygons):
                root.create_group(name="polygons")
                keys = list(self.polygons.keys())
                for el in keys:
                    self.add_polygons(name=el, polygons=self.polygons[el], _add_in_memory=False)

            if len(self.shapes):
                root.create_group(name="shapes")
                keys = list(self.shapes.keys())
                for el in keys:
                    self.add_shapes(name=el, shapes=self.shapes[el], _add_in_memory=False)

            if self.table is not None:
                elem_group = root.create_group(name="table")
                write_table(table=self.table, group=elem_group, name="table")
        except Exception as e:  # noqa: B902
            self.path = None
            raise e

    @property
    def table(self) -> AnnData:
        """
        Return the table.

        Returns
        -------
        The table.
        """
        return self._table

    @staticmethod
    def read(file_path: str) -> SpatialData:
        from spatialdata._io.read import read_zarr

        sdata = read_zarr(file_path)
        return sdata

    @property
    def images(self) -> dict[str, Union[SpatialImage, MultiscaleSpatialImage]]:
        """Return images as a Dict of name to image data."""
        return self._images

    @property
    def labels(self) -> dict[str, Union[SpatialImage, MultiscaleSpatialImage]]:
        """Return labels as a Dict of name to label data."""
        return self._labels

    @property
    def points(self) -> dict[str, pa.Table]:
        """Return points as a Dict of name to point data."""
        return self._points

    @property
    def polygons(self) -> dict[str, GeoDataFrame]:
        """Return polygons as a Dict of name to polygon data."""
        return self._polygons

    @property
    def shapes(self) -> dict[str, AnnData]:
        """Return shapes as a Dict of name to shape data."""
        return self._shapes

    # @property
    # def coordinate_systems(self) -> dict[str, NgffCoordinateSystem]:
    #     ##
    #     all_cs: dict[str, NgffCoordinateSystem] = {}
    #     gen = self._gen_elements()
    #     for obj in gen:
    #         ct = _get_transform(obj)
    #         if ct is not None:
    #             cs = ct.output_coordinate_system
    #             if cs is not None:
    #                 assert isinstance(cs, NgffCoordinateSystem)
    #                 if isinstance(cs, NgffCoordinateSystem):
    #                     name = cs.name
    #                     if name in all_cs:
    #                         added = all_cs[name]
    #                         assert cs == added
    #                     else:
    #                         all_cs[name] = cs
    #     ##
    #     return all_cs

    def _non_empty_elements(self) -> list[str]:
        """Get the names of the elements that are not empty.

        Returns
        -------
        non_empty_elements
            The names of the elements that are not empty.
        """
        all_elements = ["images", "labels", "points", "polygons", "shapes", "table"]
        return [
            element
            for element in all_elements
            if (getattr(self, element) is not None) and (len(getattr(self, element)) > 0)
        ]

    def __repr__(self) -> str:
        return self._gen_repr()

    def _gen_repr(
        self,
    ) -> str:
        """
        Generate a string representation of the SpatialData object.
        Returns
        -------
            The string representation of the SpatialData object.
        """

        def rreplace(s: str, old: str, new: str, occurrence: int) -> str:
            li = s.rsplit(old, occurrence)
            return new.join(li)

        def h(s: str) -> str:
            return hashlib.md5(repr(s).encode()).hexdigest()

        descr = "SpatialData object with:"

        non_empty_elements = self._non_empty_elements()
        last_element_index = len(non_empty_elements) - 1
        for attr_index, attr in enumerate(non_empty_elements):
            last_attr = True if (attr_index == last_element_index) else False
            attribute = getattr(self, attr)

            descr += f"\n{h('level0')}{attr.capitalize()}"
            if isinstance(attribute, AnnData):
                descr += f"{h('empty_line')}"
                descr_class = attribute.__class__.__name__
                descr += f"{h('level1.0')}{attribute!r}: {descr_class} {attribute.shape}"
                descr = rreplace(descr, h("level1.0"), "    └── ", 1)
            else:
                for k, v in attribute.items():
                    descr += f"{h('empty_line')}"
                    descr_class = v.__class__.__name__
                    if attr == "shapes":
                        descr += (
                            f"{h(attr + 'level1.1')}{k!r}: {descr_class} with osbm.spatial "
                            f"{v.obsm['spatial'].shape}"
                        )
                    elif attr == "polygons":
                        descr += f"{h(attr + 'level1.1')}{k!r}: {descr_class} " f"shape: {v.shape} (2D polygons)"
                    elif attr == "points":
                        if len(v) > 0:
                            n = len(get_dims(v))
                            dim_string = f"({n}D points)"
                        else:
                            dim_string = ""
                        if descr_class == "Table":
                            descr_class = "pyarrow.Table"
                        descr += f"{h(attr + 'level1.1')}{k!r}: {descr_class} " f"shape: {v.shape} {dim_string}"
                    else:
                        if isinstance(v, SpatialImage):
                            descr += f"{h(attr + 'level1.1')}{k!r}: {descr_class}[{''.join(v.dims)}] {v.shape}"
                        elif isinstance(v, MultiscaleSpatialImage):
                            shapes = []
                            dims: Optional[str] = None
                            for pyramid_level in v.keys():
                                dataset_names = list(v[pyramid_level].keys())
                                assert len(dataset_names) == 1
                                dataset_name = dataset_names[0]
                                vv = v[pyramid_level][dataset_name]
                                shape = vv.shape
                                if dims is None:
                                    dims = "".join(vv.dims)
                                shapes.append(shape)
                            descr += (
                                f"{h(attr + 'level1.1')}{k!r}: {descr_class}[{dims}] " f"{', '.join(map(str, shapes))}"
                            )
                        else:
                            raise TypeError(f"Unknown type {type(v)}")
            if last_attr is True:
                descr = descr.replace(h("empty_line"), "\n  ")
            else:
                descr = descr.replace(h("empty_line"), "\n│ ")

        descr = rreplace(descr, h("level0"), "└── ", 1)
        descr = descr.replace(h("level0"), "├── ")

        for attr in ["images", "labels", "points", "polygons", "table", "shapes"]:
            descr = rreplace(descr, h(attr + "level1.1"), "    └── ", 1)
            descr = descr.replace(h(attr + "level1.1"), "    ├── ")
        return descr

    def _gen_elements(self) -> Generator[SpatialElement, None, None]:
        for element_type in ["images", "labels", "points", "polygons", "shapes"]:
            d = getattr(SpatialData, element_type).fget(self)
            yield from d.values()


class QueryManager:
    """Perform queries on SpatialData objects"""

    def __init__(self, sdata: SpatialData):
        self._sdata = sdata

    def bounding_box(self, request: BoundingBoxRequest) -> SpatialData:
        """Perform a bounding box query on the SpatialData object.

        Parameters
        ----------
        request : BoundingBoxRequest
            The bounding box request.

        Returns
        -------
        requested_sdata : SpatialData
            The SpatialData object containing the requested data.
            Elements with no valid data are omitted.
        """
        requested_points = _bounding_box_query_points_dict(points_dict=self._sdata.points, request=request)

        return SpatialData(points=requested_points)

    def __call__(self, request: BaseSpatialRequest) -> SpatialData:
        if isinstance(request, BoundingBoxRequest):
            return self.bounding_box(request)
        else:
            raise TypeError("unknown request type")
