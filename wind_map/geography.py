"""A coordinate is display information, never a prerequisite for counting MW."""
import math
from .core import WindError

def wgs84(x, y, crs="EPSG:4326"):
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (x, y)):
        raise WindError("Non-finite coordinate")
    if crs != "EPSG:4326":
        from pyproj import Transformer
        x, y = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform(x, y)
    if not (124 <= x <= 132 and 32 <= y <= 40):
        raise WindError("Coordinate outside Korean bounds; check axes and CRS")
    return {"longitude": x, "latitude": y, "source_crs": crs}

def coordinate_for_display(record):
    if record.get("kind") not in {"official", "turbine", "parcel", "site_representative"} or record.get("address_role") == "head_office":
        raise WindError("Coordinate has no supported site-location provenance")
    point = wgs84(record["x"], record["y"], record.get("crs", "EPSG:4326"))
    if record.get("region_check") not in {"inside", "boundary_exception"}:
        raise WindError("Administrative containment has not been reviewed")
    if record["region_check"] == "boundary_exception" and not record.get("exception_reason"):
        raise WindError("Boundary exception needs evidence")
    if record.get("region_geometry") and not contains_point(record["region_geometry"], point["longitude"], point["latitude"]):
        if record["region_check"] != "boundary_exception":
            raise WindError("Coordinate falls outside the administrative polygon")
    return {**point, "precision": record["kind"], "approximate": record["kind"] == "parcel", "evidence_id": record["evidence_id"]}

def contains_point(geometry, longitude, latitude):
    """WGS84 GeoJSON Polygon/MultiPolygon, including holes; boundaries count as inside."""
    def ring_contains(ring):
        if len(ring) < 4 or ring[0] != ring[-1]:
            raise WindError("Administrative polygon ring is not closed")
        inside = False
        for a, b in zip(ring, ring[1:]):
            ax, ay = a; bx, by = b
            if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in (ax,ay,bx,by)):
                raise WindError("Invalid administrative geometry")
            cross = (longitude-ax)*(by-ay) - (latitude-ay)*(bx-ax)
            if abs(cross) < 1e-10 and min(ax,bx)-1e-10 <= longitude <= max(ax,bx)+1e-10 and min(ay,by)-1e-10 <= latitude <= max(ay,by)+1e-10:
                return True
            if (ay > latitude) != (by > latitude) and longitude < (bx-ax)*(latitude-ay)/(by-ay)+ax:
                inside = not inside
        return inside
    kind = geometry.get("type")
    polygons = [geometry["coordinates"]] if kind == "Polygon" else geometry.get("coordinates") if kind == "MultiPolygon" else None
    if not polygons:
        raise WindError("Unsupported administrative geometry")
    return any(polygon and ring_contains(polygon[0]) and not any(ring_contains(hole) for hole in polygon[1:]) for polygon in polygons)
