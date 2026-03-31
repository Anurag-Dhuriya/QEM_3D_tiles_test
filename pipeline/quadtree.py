import math


class BoundingBox:
    def __init__(self, min_lon, min_lat, max_lon, max_lat):
        self.min_lon = min_lon
        self.min_lat = min_lat
        self.max_lon = max_lon
        self.max_lat = max_lat

    @property
    def center_lon(self):
        return (self.min_lon + self.max_lon) / 2

    @property
    def center_lat(self):
        return (self.min_lat + self.max_lat) / 2

    @property
    def width(self):
        return self.max_lon - self.min_lon

    @property
    def height(self):
        return self.max_lat - self.min_lat

    def contains(self, lon, lat):
        return (self.min_lon <= lon <= self.max_lon and
                self.min_lat <= lat <= self.max_lat)

    def subdivide(self):
        cx = self.center_lon
        cy = self.center_lat
        return [
            BoundingBox(self.min_lon, cy,          cx,          self.max_lat),  # NW
            BoundingBox(cx,          cy,          self.max_lon, self.max_lat),  # NE
            BoundingBox(self.min_lon, self.min_lat, cx,          cy),           # SW
            BoundingBox(cx,          self.min_lat, self.max_lon, cy)            # SE
        ]

    def to_dict(self):
        return {
            "min_lon": self.min_lon,
            "min_lat": self.min_lat,
            "max_lon": self.max_lon,
            "max_lat": self.max_lat
        }

    def __repr__(self):
        return (f"BBox({self.min_lon:.4f},{self.min_lat:.4f} → "
                f"{self.max_lon:.4f},{self.max_lat:.4f})")


class QuadTreeNode:
    def __init__(self, bounds, depth=0, max_depth=4, max_models_per_cell=4):
        self.bounds             = bounds
        self.depth              = depth
        self.max_depth          = max_depth
        self.max_models_per_cell= max_models_per_cell
        self.models             = []
        self.children           = []
        self.cell_id            = None

    @property
    def is_leaf(self):
        return len(self.children) == 0

    def insert(self, model):
        lon = model["lon"]
        lat = model["lat"]

        if not self.bounds.contains(lon, lat):
            return False

        if self.is_leaf:
            self.models.append(model)
            # Split if over capacity and not at max depth
            if (len(self.models) > self.max_models_per_cell and
                    self.depth < self.max_depth):
                self._split()
            return True

        # Not a leaf — insert into correct child
        for child in self.children:
            if child.insert(model):
                return True

        # Fallback — model on boundary, keep in this node
        self.models.append(model)
        return True

    def _split(self):
        sub_bounds = self.bounds.subdivide()
        self.children = [
            QuadTreeNode(
                bounds             = b,
                depth              = self.depth + 1,
                max_depth          = self.max_depth,
                max_models_per_cell= self.max_models_per_cell
            )
            for b in sub_bounds
        ]

        # Redistribute models to children
        remaining = []
        for model in self.models:
            placed = False
            for child in self.children:
                if child.insert(model):
                    placed = True
                    break
            if not placed:
                remaining.append(model)
        self.models = remaining

    def get_all_leaves(self):
        if self.is_leaf:
            if self.models:
                return [self]
            return []
        leaves = []
        for child in self.children:
            leaves.extend(child.get_all_leaves())
        if self.models:
            leaves.append(self)
        return leaves

    def get_all_nodes(self):
        nodes = [self]
        for child in self.children:
            nodes.extend(child.get_all_nodes())
        return nodes

    def __repr__(self):
        return (f"QTNode(depth={self.depth}, "
                f"models={len(self.models)}, "
                f"children={len(self.children)}, "
                f"bounds={self.bounds})")


def build_quadtree(models, padding=0.01, max_depth=4, max_per_cell=4):
    if not models:
        return None

    # Calculate scene bounds from all model coordinates
    lons = [m["lon"] for m in models]
    lats = [m["lat"] for m in models]

    min_lon = min(lons) - padding
    max_lon = max(lons) + padding
    min_lat = min(lats) - padding
    max_lat = max(lats) + padding

    # Make bounds square for even subdivision
    lon_span = max_lon - min_lon
    lat_span = max_lat - min_lat
    max_span = max(lon_span, lat_span)
    cx       = (min_lon + max_lon) / 2
    cy       = (min_lat + max_lat) / 2
    half     = max_span / 2 + padding

    bounds = BoundingBox(
        cx - half, cy - half,
        cx + half, cy + half
    )

    root = QuadTreeNode(
        bounds             = bounds,
        depth              = 0,
        max_depth          = max_depth,
        max_models_per_cell= max_per_cell
    )

    for model in models:
        root.insert(model)

    # Assign cell IDs to all leaf nodes with models
    leaves = root.get_all_leaves()
    for i, leaf in enumerate(leaves):
        leaf.cell_id = f"cell_{i:04d}"

    print(f"[Quadtree] Built with {len(models)} models → {len(leaves)} cells")
    print(f"[Quadtree] Bounds: {bounds}")

    return root


def assign_cells(models, padding=0.01, max_depth=4, max_per_cell=4):
    root   = build_quadtree(models, padding, max_depth, max_per_cell)
    leaves = root.get_all_leaves()

    # Build a flat lookup: model name → cell_id
    cell_map = {}
    cells    = {}

    for leaf in leaves:
        cell_id = leaf.cell_id
        cells[cell_id] = {
            "cell_id": cell_id,
            "bounds":  leaf.bounds.to_dict(),
            "models":  leaf.models,
            "depth":   leaf.depth
        }
        for model in leaf.models:
            cell_map[model["name"]] = cell_id

    print(f"[Quadtree] Cell assignments: {cell_map}")
    return root, cells, cell_map