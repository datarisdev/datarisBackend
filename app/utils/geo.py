# app/utils/geo.py
import rasterio
from rasterio.mask import mask
from shapely.geometry import shape

def get_centroid_from_geojson(geometry: dict) -> tuple[float, float]:
    """
    Returns (lat, lon)
    """
    feature = geometry["features"][0]
    geom = shape(feature["geometry"])
    centroid = geom.centroid

    return centroid.y, centroid.x

def clip_raster(url: str, shapes):
    with rasterio.open(url) as src:
        out_img, out_transform = mask(
            src, shapes=shapes, crop=True
        )

        meta = src.meta.copy()
        meta.update({
            "height": out_img.shape[1],
            "width": out_img.shape[2],
            "transform": out_transform,
            "dtype": "float32",
        })

    return out_img[0].astype("float32"), meta
