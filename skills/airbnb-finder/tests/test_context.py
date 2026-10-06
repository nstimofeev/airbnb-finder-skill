from src import step1b_context as c


def test_surface_class():
    assert c.surface_class({"highway": "residential", "surface": "asphalt"}) == "paved"
    assert c.surface_class({"highway": "unclassified", "surface": "gravel"}) == "unpaved"
    assert c.surface_class({"highway": "track"}) == "unpaved"
    assert c.surface_class({"highway": "unclassified", "tracktype": "grade3"}) == "unpaved"
    assert c.surface_class({"highway": "secondary"}) == "paved"
    assert c.surface_class({"highway": "unclassified"}) == "unknown"


def test_route_surface_first_km_only():
    ways = [{"nodes": [1, 2, 3], "tags": {"highway": "track"}},
            {"nodes": [3, 4], "tags": {"highway": "tertiary", "surface": "asphalt"}}]
    idx = c.edge_index(ways)
    # route goes 1→2→3 (track, 300+400 m) then 3→4 (asphalt, 9000 m) then an unmapped edge 4→5
    r = c.route_surface([1, 2, 3, 4, 5], [300, 400, 9000, 50], idx, first_m=5000)
    assert r == {"paved": 4300, "unpaved": 700, "unknown": 0}
    # direction doesn't matter, unmapped edges are unknown
    r = c.route_surface([4, 3, 2, 9], [100, 400, 50], idx)
    assert r == {"paved": 100, "unpaved": 400, "unknown": 50}


def test_count_within():
    assert c.count_within(0, 0, [(0, 0.001), (0, 0.01)], 150) == 1


def test_building_cells_cover_neighbours_across_the_edge():
    # a listing 50 m inside its cell: buildings 300 m away in the next cell must be in the padded query box
    lat, lng = 43.95 + 0.00045, 19.40 + 0.0005
    (cell,) = c.grid_cells([(lat, lng)])
    b = c.cell_bbox(cell, 400)
    assert b.sw_lat < lat - 0.0027 and b.sw_lng < lng - 0.0037


def test_poi_name_prefers_english():
    assert c.poi_name({"name": "Кафана", "name:en": "Kafana"}) == "Kafana"
    assert c.poi_name({"name": "Kafana"}) == "Kafana"
