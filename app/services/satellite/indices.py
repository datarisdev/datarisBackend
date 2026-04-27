# app/services/satellite/indices.py
from enum import Enum
import numpy as np

class IndexType(str, Enum):
    NDVI = "NDVI"
    SAVI = "SAVI"
    NDMI = "NDMI"
    NDWI = "NDWI"
    LCI = "LCI"
    LAI = "LAI"
    LCC = "LCC"
    CWC = "CWC"


# Which bands each index needs
INDEX_BANDS_OLD = {
    IndexType.NDVI: ["B04", "B08"],     # red, nir
    IndexType.SAVI: ["B04", "B08"],
    IndexType.NDMI: ["B08", "B11"],     # nir, swir
}

INDEX_BANDS = {
    IndexType.NDVI: ["B04", "B08"],
    IndexType.SAVI: ["B04", "B08"],
    IndexType.NDMI: ["B08", "B11"],
    IndexType.NDWI: ["B03", "B08"],
    IndexType.LCI: ["B04", "B08"],
    IndexType.LAI: ["B04", "B08"],
    IndexType.LCC: ["B04", "B05"],
    IndexType.CWC: ["B03", "B04", "B08"],
}


def ndvi(nir, red):
    np.seterr(divide="ignore", invalid="ignore")
    return (nir - red) / (nir + red)

def savi(nir, red, L=0.5):
    return ((nir - red) / (nir + red + L)) * (1 + L)

def ndmi(nir, swir):
    return (nir - swir) / (nir + swir)

def ndwi(green, nir):
    return (green - nir) / (green + nir)

def lci(nir, red):
    return nir / red - 1

def lai_from_ndvi(ndvi):
    ndvi = np.clip(ndvi, 0, 0.95)
    return -np.log((0.69 - ndvi) / 0.59) / 0.91

def lcc(rededge, red):
    return rededge / red - 1

def cwc(ndvi, ndwi):
    return ndvi * ndwi


def compute_index(index: IndexType, bands: dict):
    np.seterr(divide="ignore", invalid="ignore")

    if index == IndexType.NDVI:
        return ndvi(bands["B08"], bands["B04"])

    if index == IndexType.SAVI:
        return savi(bands["B08"], bands["B04"])

    if index == IndexType.NDMI:
        return ndmi(bands["B08"], bands["B11"])

    if index == IndexType.NDWI:
        return ndwi(bands["B03"], bands["B08"])

    if index == IndexType.LCI:
        return lci(bands["B08"], bands["B04"])

    if index == IndexType.LAI:
        ndvi_arr = ndvi(bands["B08"], bands["B04"])
        return lai_from_ndvi(ndvi_arr)

    if index == IndexType.LCC:
        return lcc(bands["B05"], bands["B04"])

    if index == IndexType.CWC:
        ndvi_arr = ndvi(bands["B08"], bands["B04"])
        ndwi_arr = ndwi(bands["B03"], bands["B08"])
        return cwc(ndvi_arr, ndwi_arr)

    raise ValueError(index)

def compute_index_old(index: IndexType, bands: dict):
    import numpy as np

    np.seterr(divide="ignore", invalid="ignore")

    if index == IndexType.NDVI:
        red, nir = bands["B04"], bands["B08"]
        return (nir - red) / (nir + red)

    if index == IndexType.SAVI:
        red, nir = bands["B04"], bands["B08"]
        L = 0.5
        return ((nir - red) / (nir + red + L)) * (1 + L)

    if index == IndexType.NDMI:
        nir, swir = bands["B08"], bands["B11"]
        return (nir - swir) / (nir + swir)

    raise ValueError(f"Unsupported index {index}")
