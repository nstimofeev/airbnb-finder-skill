"""Distances, anchor checks and bounding-box arithmetic."""
from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_R = 6371008.8  # metres
FUZZ_M = 150  # Airbnb shifts listing coordinates by up to ~150 m


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class BBox:
    ne_lat: float
    ne_lng: float
    sw_lat: float
    sw_lng: float

    def split4(self) -> list["BBox"]:
        mid_lat = (self.ne_lat + self.sw_lat) / 2
        mid_lng = (self.ne_lng + self.sw_lng) / 2
        return [
            BBox(self.ne_lat, mid_lng, mid_lat, self.sw_lng),  # NW
            BBox(self.ne_lat, self.ne_lng, mid_lat, mid_lng),  # NE
            BBox(mid_lat, mid_lng, self.sw_lat, self.sw_lng),  # SW
            BBox(mid_lat, self.ne_lng, self.sw_lat, mid_lng),  # SE
        ]

    def size_m(self) -> tuple[float, float]:
        """(height, width) in metres."""
        h = haversine_m(self.sw_lat, self.sw_lng, self.ne_lat, self.sw_lng)
        mid = (self.ne_lat + self.sw_lat) / 2
        w = haversine_m(mid, self.sw_lng, mid, self.ne_lng)
        return h, w

    def key(self) -> str:
        return f"{self.ne_lat:.5f}_{self.ne_lng:.5f}_{self.sw_lat:.5f}_{self.sw_lng:.5f}"


def bbox_around_anchors(anchors: list[dict], margin_m: float = FUZZ_M) -> BBox:
    """Smallest box that covers every anchor's circle (radius + fuzz margin)."""
    lats, lngs = [], []
    for a in anchors:
        r = float(a["max_distance_m"]) + margin_m
        dlat = math.degrees(r / EARTH_R)
        dlng = math.degrees(r / (EARTH_R * math.cos(math.radians(a["lat"]))))
        lats += [a["lat"] - dlat, a["lat"] + dlat]
        lngs += [a["lng"] - dlng, a["lng"] + dlng]
    return BBox(max(lats), max(lngs), min(lats), min(lngs))


def check_anchors(lat: float, lng: float, anchors: list[dict], mode: str = "all",
                  tolerance_m: float = FUZZ_M) -> tuple[bool, bool, dict[str, float]]:
    """Returns (passes, near_boundary, distances).

    A listing inside radius+tolerance passes; if it only passes thanks to the tolerance it is
    flagged near_boundary instead of being dropped (coordinates are fuzzed by Airbnb).
    mode "all": every anchor must pass; "any": at least one.
    """
    if not anchors:
        return True, False, {}
    distances, strict, loose = {}, [], []
    for a in anchors:
        d = haversine_m(lat, lng, a["lat"], a["lng"])
        distances[a["name"]] = round(d)
        strict.append(d <= a["max_distance_m"])
        loose.append(d <= a["max_distance_m"] + tolerance_m)
    agg = all if mode == "all" else any
    if agg(strict):
        return True, False, distances
    if agg(loose):
        return True, True, distances
    return False, False, distances
