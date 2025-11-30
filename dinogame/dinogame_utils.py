from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from random import choices, randint, random, shuffle
from typing import Any, Callable, ClassVar, Dict, List, Optional, Set, Tuple

from loguru import logger


## dummy classes /enums
class Hats(Enum):
    Golden_Cactus_Hat = auto()
    Dinosaur_Hat = auto()
    Brown_Hat = auto()


class HedgeSub(Enum):
    HedgeNorth = auto()
    HedgeNorthSouth = auto()
    HedgeNorthWest = auto()

    HedgeEast = auto()
    HedgeEastNorth = auto()
    HedgeEastWest = auto()
    HedgeEastSouth = auto()
    HedgeEastNorthSouth = auto()
    HedgeEastNorthWest = auto()
    HedgeEastSouthWest = auto()
    HedgeNorthSouthWest = auto()

    HedgeWest = auto()

    HedgeSouthWest = auto()

    HedgeSouth = auto()

    # special case
    HedgeNone = auto()


# is_left, is_right, is_top, is_bottom
_hedge_subtype_dict: Dict[Tuple[bool, bool, bool, bool], HedgeSub] = {}
_hedge_subtype_dict_reverse: Dict[HedgeSub, Tuple[bool, bool, bool, bool]] = {}


def _prepare_hedge_subtypes_dicts():
    for is_left in [True, False]:
        for is_right in [True, False]:
            for is_top in [True, False]:
                for is_bottom in [True, False]:

                    hedge: HedgeSub | None = None

                    match (is_left, is_right, is_top, is_bottom):
                        case (True, False, False, False):
                            hedge = HedgeSub.HedgeWest
                        case (True, False, False, True):
                            hedge = HedgeSub.HedgeSouthWest
                        case (False, False, False, True):
                            hedge = HedgeSub.HedgeSouth
                        case (False, True, False, False):
                            hedge = HedgeSub.HedgeEast
                        case (False, False, True, False):
                            hedge = HedgeSub.HedgeNorth
                        case (True, True, False, False):
                            hedge = HedgeSub.HedgeEastWest
                        case (True, False, True, False):
                            hedge = HedgeSub.HedgeNorthWest
                        case (False, True, True, False):
                            hedge = HedgeSub.HedgeEastNorth
                        case (True, True, False, True):
                            hedge = HedgeSub.HedgeEastSouthWest
                        case (True, False, True, True):
                            hedge = HedgeSub.HedgeNorthSouthWest
                        case (False, True, True, True):
                            hedge = HedgeSub.HedgeEastNorthSouth
                        case (True, True, True, False):
                            hedge = HedgeSub.HedgeEastNorthWest
                        case (False, True, False, True):
                            hedge = HedgeSub.HedgeEastSouth
                        case (False, False, True, True):
                            hedge = HedgeSub.HedgeNorthSouth
                        case (False, False, False, False):
                            # special case!
                            hedge = HedgeSub.HedgeNone

                    if hedge is not None:
                        _hedge_subtype_dict[(is_left, is_right, is_top, is_bottom)] = hedge
                        _hedge_subtype_dict_reverse[hedge] = (is_left, is_right, is_top, is_bottom)


_prepare_hedge_subtypes_dicts()


def get_hedge_subtype(is_left: bool, is_right: bool, is_top: bool, is_bottom: bool) -> HedgeSub | None:
    ret: HedgeSub | None = _hedge_subtype_dict.get((is_left, is_right, is_top, is_bottom), None)
    return ret


def get_hedge_tuple_by_hedgesubtype(hedge: HedgeSub) -> Tuple[bool, bool, bool, bool]:
    ret: Tuple[bool, bool, bool, bool] | None = _hedge_subtype_dict_reverse.get(hedge, None)
    return ret


@dataclass
class Maze:
    maze_x_pos: int
    maze_y_pos: int
    maze_width: int
    maze_height: int

    treasure_position: Optional[Tuple[int, int]] = field(init=False)
    hedges_positions_dict: Optional[Dict[Tuple[int, int], HedgeSub]] = field(init=False)

    drone: "Drone"

    def __post_init__(self):
        # 1. erstellt das maze-layout:
        #    - azyklisch
        #    - hecken umranden das maze (also kein ausgang aus dem maze)
        #    - der durchgang ist jeweils nur ein grid (-> azyklisch eben)
        #    - JEDES feld in einem maze ist ein HedgeSub
        #    - eine hedge blockiert also NICHT das feld an sich, sondern nur entsprechend den zugang / ausgang von / nach diesem feld
        # 2. treasure position randomly ausdenken
        # 3. einen ausreichend komplizierten, random-pfad von der aktuellen position zum treasure-position bestimmen
        # 4. "den rest" mit random hedges/passageways auffüllen/so lassen

        # Sicherheitsnetz: Minimalmaße erzwingen (äußerer Rand braucht 1 Feld pro Seite)
        assert self.maze_width >= 3 and self.maze_height >= 3, "Maze muss mindestens 3x3 groß sein"

        self.hedges_positions_dict = {}

        x0, y0 = self.maze_x_pos, self.maze_y_pos
        x1, y1 = x0 + self.maze_width - 1, y0 + self.maze_height - 1

        # Hilfsgrenzen für Innenbereich
        ix0, iy0 = x0 + 1, y0 + 1
        ix1, iy1 = x1 - 1, y1 - 1

        # Kurzschluss: Falls kein echter Innenbereich existiert
        if ix0 > ix1 or iy0 > iy1:
            # Alle Felder sind Rand -> vollständig ummauert
            for yo in range(y0, y1 + 1):
                for xo in range(x0, x1 + 1):
                    self.hedges_positions_dict[(xo, yo)] = HedgeSub.HedgeNone
            self.treasure_position = None
            return

        # 1) Markiere alle Felder als "Gang" (walkable)
        # Wir verwenden ein Set für alle begehbaren Felder
        passages: set[tuple[int, int]] = set()
        for yy in range(iy0, iy1 + 1):
            for xx in range(ix0, ix1 + 1):
                passages.add((xx, yy))

        # Hilfsfunktionen
        def in_inner(nx: int, ny: int) -> bool:
            return ix0 <= nx <= ix1 and iy0 <= ny <= iy1

        # Nachbarn mit 2er-Schritt, um 1-Zellen-breite Gänge zu garantieren
        def neighbors2(cx: int, cy: int):
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = cx + 2 * dx, cy + 2 * dy
                wx, wy = cx + dx, cy + dy  # „Wand dazwischen"
                if in_inner(nx, ny) and in_inner(wx, wy):
                    yield (nx, ny, wx, wy, (dx, dy))

        # 2) Startzelle bestimmen (bevorzugt Drone-Position, falls im Innenbereich und auf Raster)
        sx, sy = self.drone.get_pos_x(), self.drone.get_pos_y()
        if not in_inner(sx, sy):
            sx, sy = (ix0 + ix1) // 2, (iy0 + iy1) // 2
        # Auf ein zulässiges „Raster" ziehen (damit 2er-Schritte gut funktionieren)
        if (sx - ix0) % 2 == 0:
            sx += 1 if sx + 1 <= ix1 else -1
        if (sy - iy0) % 2 == 0:
            sy += 1 if sy + 1 <= iy1 else -1

        # 3) DFS-Backtracker: Erstelle Maze-Struktur
        # Wir tracken welche "Wände" (Übergänge zwischen Zellen) blockiert sind
        # blocked_edges: Set von ((x1,y1), (x2,y2)) - sortiert damit Richtung egal ist
        blocked_edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()

        # Initialisiere: Alle möglichen Übergänge sind blockiert
        for yy in range(iy0, iy1 + 1):
            for xx in range(ix0, ix1 + 1):
                # Prüfe horizontale und vertikale Nachbarn
                for dx, dy in [(1, 0), (0, 1)]:
                    nx, ny = xx + dx, yy + dy
                    if in_inner(nx, ny):
                        edge = tuple(sorted([(xx, yy), (nx, ny)]))
                        blocked_edges.add(edge)

        visited: set[tuple[int, int]] = set()
        stack: list[tuple[int, int]] = [(sx, sy)]

        while stack:
            cx, cy = stack[-1]
            visited.add((cx, cy))

            # Kandidaten zufällig mischen
            cands = list(neighbors2(cx, cy))
            shuffle(cands)

            progressed = False
            for nx, ny, wx, wy, (dx, dy) in cands:
                if (nx, ny) not in visited:
                    # Erstelle Pfad: entferne Blockierungen zwischen aktueller Zelle und Nachbar
                    # Der Pfad geht von (cx,cy) über (wx,wy) zu (nx,ny)
                    edge1 = tuple(sorted([(cx, cy), (wx, wy)]))
                    edge2 = tuple(sorted([(wx, wy), (nx, ny)]))
                    blocked_edges.discard(edge1)
                    blocked_edges.discard(edge2)

                    stack.append((nx, ny))
                    progressed = True
                    break

            if not progressed:
                stack.pop()

        # 4) Erstelle HedgeSub für JEDES Feld basierend auf blockierten Übergängen
        # Für jedes Feld prüfen wir, in welche Richtungen der Zugang blockiert ist
        for yo in range(y0, y1 + 1):
            for xo in range(x0, x1 + 1):
                is_boundary_left = xo == x0
                is_boundary_right = xo == x1
                is_boundary_top = yo == y1
                is_boundary_bottom = yo == y0

                # Für Randfelder: komplette Außenmauer — keine Übergänge in irgendeine Richtung
                # (um sicherzustellen, dass die Hecke das Maze wirklich umrandet und keine Zyklen durch Randzellen entstehen)
                if is_boundary_left or is_boundary_right or is_boundary_top or is_boundary_bottom:
                    blocked_left = True
                    blocked_right = True
                    blocked_top = True
                    blocked_bottom = True
                else:
                    # Innenfeld: prüfe blockierte Übergänge zu Nachbarn
                    # Übergänge zu Zellen außerhalb des Innenbereichs (also Rand) sind grundsätzlich blockiert
                    blocked_left = (not in_inner(xo - 1, yo)) or (
                        tuple(sorted([(xo, yo), (xo - 1, yo)])) in blocked_edges
                    )
                    blocked_right = (not in_inner(xo + 1, yo)) or (
                        tuple(sorted([(xo, yo), (xo + 1, yo)])) in blocked_edges
                    )
                    blocked_top = (not in_inner(xo, yo + 1)) or (
                        tuple(sorted([(xo, yo), (xo, yo + 1)])) in blocked_edges
                    )
                    blocked_bottom = (not in_inner(xo, yo - 1)) or (
                        tuple(sorted([(xo, yo), (xo, yo - 1)])) in blocked_edges
                    )

                # Bestimme HedgeSub basierend auf blockierten Richtungen
                subtype = get_hedge_subtype(blocked_left, blocked_right, blocked_top, blocked_bottom)
                if subtype is None:
                    subtype = HedgeSub.HedgeNone

                self.hedges_positions_dict[(xo, yo)] = subtype

        # 5) Schatzposition bestimmen – weiteste Gangzelle (BFS vom Start)
        def is_passage(px: int, py: int) -> bool:
            return in_inner(px, py)

        def can_reach(from_pos: tuple[int, int], to_pos: tuple[int, int]) -> bool:
            """Prüft ob der Übergang zwischen zwei Zellen nicht blockiert ist"""
            edge = tuple(sorted([from_pos, to_pos]))
            return edge not in blocked_edges

        # BFS um weiteste Zelle zu finden
        q = deque()
        if is_passage(sx, sy):
            q.append((sx, sy, 0))
        seen: set[tuple[int, int]] = set()
        farthest = (sx, sy, 0)

        while q:
            px, py, d = q.popleft()
            if (px, py) in seen:
                continue
            seen.add((px, py))
            if d > farthest[2]:
                farthest = (px, py, d)

            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = px + dx, py + dy
                if is_passage(nx, ny) and (nx, ny) not in seen and can_reach((px, py), (nx, ny)):
                    q.append((nx, ny, d + 1))

        fx, fy, _ = farthest
        # Fallback: falls aus irgendeinem Grund keine Passage gefunden wurde
        if not is_passage(fx, fy):
            # wähle zufällige erreichbare Passage
            reachable_passages = list(seen) if seen else [(sx, sy)]
            self.treasure_position = choices(reachable_passages, k=1)[0] if reachable_passages else None
        else:
            self.treasure_position = (fx, fy)


class Grounds(Enum):
    Grass = auto()
    Soil = auto()


class Entities(Enum):
    Carrot = auto()
    Sunflower = auto()
    Bush = auto()
    Tree = auto()
    Pumpkin = auto()
    Cactus = auto()
    Treasure = auto()
    Apple = auto()
    Hedge = auto()
    Grass = auto()
    _DinoTail = auto()


@dataclass
class EntityAndValues:
    entity: Entities
    size: Optional[int] = None
    growstate_percentage: Optional[float] = None


@dataclass
class GroundsAndValues:
    ground_type: Grounds
    water_percentage: float = 0.0


# class HedgeSub(Enum):
#     HedgeNorth = auto()
#     HedgeNorthSouth = auto()
#     HedgeNorthWest = auto()
#
#     HedgeEast = auto()
#     HedgeEastNorth = auto()
#     HedgeEastWest = auto()
#     HedgeEastSouth = auto()
#     HedgeEastNorthSouth = auto()
#     HedgeEastNorthWest = auto()
#     HedgeEastSouthWest = auto()
#
#     HedgeWest = auto()
#
#     HedgeSouthWest = auto()
#
#     HedgeSouth = auto()


class Items(Enum):
    Carrot = auto()
    Sunflower = auto()
    Wood = auto()
    Hay = auto()
    Gold = auto()
    Bone = auto()
    Cactus = auto()
    Fertilizer = auto()
    Water = auto()
    Strange_Substance = auto()
    Pumpkin = auto()
    Power = auto()


class Directions(Enum):
    North = auto()
    East = auto()
    West = auto()
    South = auto()


North = Directions.North
East = Directions.East
West = Directions.West
South = Directions.South

costs: Dict[Entities, Dict[Items, int]] = {
    Entities.Carrot: {Items.Wood: 1, Items.Hay: 1},
    Entities.Pumpkin: {Items.Carrot: 2},
}


@dataclass
class Drone:
    _next_drone_id: ClassVar[int] = 0

    @classmethod
    def get_next_drone_id(cls) -> int:
        ret = cls._next_drone_id
        cls._next_drone_id += 1
        return ret

    # drone_task: Callable[[[], Any], Any]
    drone_task: Callable[[], Any]
    drone_x_position: int
    drone_y_position: int

    world: "World"

    is_in_maze_mode: bool = False
    maze: Optional[Maze] = None

    is_in_dino_mode: bool = False
    # tail-positions for dino-mode are kept in world?!

    drone_id: int = field(init=False)

    drone_hat: Hats = Hats.Brown_Hat
    drone_task_finished: bool = False
    drone_task_return_value: Any = None

    def __post_init__(self):
        # Assign a unique, sequential ID on every instantiation
        self.drone_id = self.get_next_drone_id()
        self.drone_x_position = 0
        self.drone_y_position = 0

    def is_main_drone(self):
        return self.drone_id == 0

    def harvest(self):
        # check if entity
        ...

    def spawn_drone(task: Callable[[], Any]): ...

    def get_pos_x(self):
        return self.drone_x_position
        # return int((random() *10* _world_size) // 10)

    def get_pos_y(self) -> int:
        return self.drone_y_position

    def measure(self, direction: Optional[Directions] = None) -> Optional[Tuple[int, int] | int]:
        # measure(direction = None)
        # Kann bei einigen Entitäten bestimmte Werte messen. Der Effekt hängt von der Entität ab.
        #
        # Wenn direction nicht None ist, wird die benachbarte Entität in der angegebenen Richtung gemessen.
        #
        # - gibt die Anzahl der Blütenblätter einer Sonnenblume zurück (int)
        # - gibt die nächste Position für einen Schatz oder Apfel zurück (Tuple[int,int])
        # - gibt die Größe eines Kaktus zurück (int)
        # - gibt eine mysteriöse Zahl für einen Kürbis zurück (int)
        # - gibt None für alle anderen Entitäten zurück.

        # Positions-Helper
        x, y = self.get_pos_x(), self.get_pos_y()

        # Sonderfälle je Modus ohne Blickrichtung
        if direction is None:
            if _is_in_dino_mode and (x, y) == _apple_position:
                # In Dino-Mode auf einem Apfel → gebe nächste Apfelposition zurück (oder None)
                return _next_apple_position
            if self.is_in_maze_mode:
                # Im Maze-Mode → gebe Schatz-Position zurück (oder None)
                return self.maze.treasure_position

        # Zielposition bestimmen (aktuelle Zelle oder Nachbar je Richtung)
        if direction is None:
            tx, ty = x, y
        else:
            # Nutze globale deltas (unten definiert) – zur Laufzeit vorhanden
            dx, dy = deltas[direction]
            tx, ty = x + dx, y + dy
            # Außerhalb der Welt? → nichts messbar
            if not (0 <= tx < _world_size and 0 <= ty < _world_size):
                return None

        target: Optional[EntityAndValues] = _entities_on_the_map.get((tx, ty))
        if target is None:
            return None

        ent: Entities = target.entity

        # Apfel / Schatz → Position-Hinweis
        if ent == Entities.Apple:
            # In Dino-Mode: immer nächste Apfelposition (kann None sein)
            if _is_in_dino_mode:
                return _next_apple_position
            # Außerhalb Dino-Mode kein besonderer Effekt
            return None
        if ent == Entities.Treasure:
            # Im Maze-Mode: gebe bekannte Schatz-Position zurück
            return self.maze.treasure_position

        # Sonnenblume → Blütenblätterzahl
        if ent == Entities.Sunflower:
            # Simple Heuristik: Basis 8 Blätter, skaliert mit Größe (falls vorhanden)
            size = target.size if target.size is not None else 1
            petals = max(1, int(8 * size))
            return petals

        # Kaktus → Größe
        if ent == Entities.Cactus:
            return target.size if target.size is not None else 1

        # Kürbis → mysteriöse Zahl (deterministisch an Wachstumsstatus, sonst pseudozufällig im Rahmen)
        if ent == Entities.Pumpkin:
            if target.growstate_percentage is not None:
                # 0..100
                return int(round(max(0.0, min(1.0, target.growstate_percentage)) * 100))
            # Fallback: pseudozufällige Zahl 0..world_size-1
            return int((random() * 10 * _world_size) // 10)

        # Hecke, Gras, Busch, Baum, Karotte, Dino-Schwanz etc. → kein messbarer Wert
        return None

    def _can_move_to(self, new_x, new_y) -> Optional[bool]:
        global _entities_on_the_map, _tail_positions

        # Step 1: Check world boundaries
        if not (0 <= new_x < _world_size and 0 <= new_y < _world_size):
            return False

        if self.is_in_dino_mode:
            # Step 2: Dino mode - check tail collision
            # Check if trying to move into tail position
            if (new_x, new_y) in _tail_positions:
                return False
        elif self.is_in_maze_mode:
            # Step 3: Maze mode - check hedge/wall collision
            # Check if there's a hedge entity at the target position
            entity_at_position = _entities_on_the_map.get((new_x, new_y))
            if entity_at_position is not None and entity_at_position.entity == Entities.Hedge:
                return False

        # Step 4: Default case - movement allowed
        return True

    def can_move(self, direction: Directions) -> Optional[bool]:
        x = self.get_pos_x()
        y = self.get_pos_y()

        dx, dy = deltas[direction]
        new_x = x + dx
        new_y = y + dy

        return self._can_move_to(new_x, new_y)

    def move(self, step) -> bool:
        # global _current_x, _current_y, _apple_position, _next_apple_position, _entities_on_the_map, _tail_positions
        x = self.get_pos_x()
        y = self.get_pos_y()

        dx, dy = deltas[step]
        new_x = x + dx
        new_y = y + dy

        ret: bool = self._can_move_to(new_x, new_y)
        if not ret:
            return False

        if _is_in_dino_mode:
            # dino-mode apple-collection here as well

            # add current position to tail
            self.world.tail_positions.append((x, y))

            if (x, y) == self.world.apple_position:
                # # Add tail entity to map
                # tail_entity = EntityAndValues(entity=Entities._DinoTail)
                # _entities_on_the_map[(x, y)] = tail_entity

                # Remove current apple from map
                if self.world.apple_position in self.world.entities_on_the_map:
                    del self.world.entities_on_the_map[self.world.apple_position]

                # Move apple position to next apple position
                _apple_position = _next_apple_position
            else:
                # Normal movement - add current position to tail if tail exists
                oldest_tail_pos: Optional[Tuple[int, int]] = self.world.tail_positions[0]

                # drop oldest tail element from current tail_positions
                self.world.tail_positions = self.world.tail_positions[1:]

        self.drone_x_position = new_x
        self.drone_y_position = new_y

        return True

    def get_entity_type(self) -> Optional[Entities]:
        ret: Optional[Entities] = self.world.entities_on_the_map.get((self.get_pos_x(), self.get_pos_y()))

        if self.is_in_maze_mode and ret != Entities.Treasure:
            # check for in-maze-bounds
            ret = Entities.Hedge

        return ret


@dataclass
class World:
    # check: max-width: 32
    # _max_world_width: ClassVar[int] = 32
    # _max_world_height: ClassVar[int] = 32
    # _min_world_width: ClassVar[int] = 1
    # _min_world_height: ClassVar[int] = 3
    _max_world_size: ClassVar[int] = 32
    _min_world_size: ClassVar[int] = 3

    _max_execution_speed: ClassVar[int] = 10

    size: int

    main_drone_task: Callable[[], Any]

    main_drone: Drone = field(init=False)
    other_drones: List[Drone] = field(default_factory=list)

    # die main-drone wird nicht mitgezählt
    num_max_drones: int = 31
    num_current_drones: int = 1

    execution_speed: int = _max_execution_speed

    items_in_inventory: Dict[Items, int] = field(default_factory=dict)

    entities_on_the_map: Dict[Tuple[int, int], EntityAndValues] = field(default_factory=dict)
    grounds_on_the_map: Dict[Tuple[int, int], GroundsAndValues] = field(default_factory=dict)

    # check if this is really a world-parameter (since there may only be one dino active at a given time, or per-drone...)
    tail_positions: Optional[List[Tuple[int, int]]] = None
    apple_position: Optional[Tuple[int, int]] = None
    next_apple_position: Optional[Tuple[int, int]] = None

    def __post_init__(self):
        self.main_drone = Drone(drone_task=self.main_drone_task, drone_x_position=0, drone_y_position=0, world=self)

    def max_drones(self) -> int:
        return self.num_max_drones

    def num_drones(self) -> int:
        return self.num_current_drones

    def has_finished(self, droneobject: Drone): ...

    def wait_for(self, droneobject: Drone): ...

    def get_world_size(self) -> int:
        return self.size

    def _add_item_to_inventory(self, item: Items, amount: int = 1):
        # amount may also be negative or 0

        if item not in self.items_in_inventory:
            self.items_in_inventory[item] = amount
            return

        self.items_in_inventory[item] += amount

    def _generate_next_apple_position(self) -> Optional[Tuple[int, int]]:
        if len(_entities_on_the_map) == _world_size**2:
            return None

        next_apple_pos: Tuple[int, int] = int((random() * 10 * _world_size) // 10), int(
            (random() * 10 * _world_size) // 10
        )
        tries_needed: int = 1

        while next_apple_pos in _entities_on_the_map:
            next_apple_pos = int((random() * 10 * _world_size) // 10), int((random() * 10 * _world_size) // 10)
            tries_needed += 1

        print(f"TRIES NEEDED FOR APPLE POSITION: {tries_needed} -> {next_apple_pos=}")

        return next_apple_pos

    def num_items(self, item: Items) -> int:
        return self.items_in_inventory.get(item, 0)

    def set_world_size(self, world_size):
        prev_world_size = self.size

        if world_size < 3:
            self.size = self._max_world_size
        elif world_size <= self._max_world_size:
            self.world_size = world_size

        if self.world_size != prev_world_size:
            self.entities_on_the_map.clear()
            return True

        return False  # already at that size or over max-size

    def set_execution_speed(self, exec_speed: Optional[int]):
        prev_exec_speed = self.execution_speed

        if exec_speed is None or exec_speed <= 0:
            self.execution_speed = self._max_execution_speed

        if prev_exec_speed != self.execution_speed:
            return True

        return False


## END dummy classes /enums


## dummy states
main_drone_task: Optional[Callable[[], Any]] = None
world: World = World(size=32, main_drone_task=main_drone_task)
## END dummy states


## dummy/mockup functions


def change_hat(hat: Hats):
    global _current_hat, _is_in_dino_mode, _tail_positions, _apple_position, _next_apple_position, _entities_on_the_map

    prev_hat = _current_hat
    was_in_dino_mode = _is_in_dino_mode

    if hat == Hats.Dinosaur_Hat:
        _is_in_dino_mode = True
    else:
        _is_in_dino_mode = False

    _current_hat = hat

    if was_in_dino_mode != _is_in_dino_mode:
        if was_in_dino_mode:
            # dino mode from on to off
            tail_len = len(_tail_positions)
            # for tp in _tail_positions:
            #     del _entities_on_the_map[tp]

            # harvest tail-length^2 in bones
            # reset tail positions to empty set
            # -> remove apple from screen
            _add_item_to_inventory(Items.Bone, tail_len * tail_len)
            _tail_positions.clear()

            if _apple_position is not None and _apple_position in _entities_on_the_map:
                del _entities_on_the_map[_apple_position]
                _apple_position = None

            if _next_apple_position is not None and _next_apple_position in _entities_on_the_map:
                del _entities_on_the_map[_next_apple_position]
                _next_apple_position = None
        else:
            # dino mode from off to on
            _apple_position = (get_pos_x(), get_pos_y())
            _next_apple_position = _generate_next_apple_position()

            eta: EntityAndValues = EntityAndValues(entity=Entities.Apple)
            eta_next: EntityAndValues = EntityAndValues(entity=Entities.Apple)

            _entities_on_the_map[_apple_position] = eta
            _entities_on_the_map[_next_apple_position] = eta_next


# TODO create_random_maze implementieren...


## END dummy/mockup functions


directions: List[Directions] = [North, South, East, West]
opposite: Dict[Directions, Directions] = {North: South, South: North, East: West, West: East}
deltas: Dict[Directions, Tuple[int, int]] = {North: (0, 1), South: (0, -1), East: (1, 0), West: (-1, 0)}


def manhattan_heuristic(x, y, zx, zy):
    return abs(x - zx) + abs(y - zy)


def get_pos():
    return get_pos_x(), get_pos_y()


def goto_00():
    move_to(0, 0)


def move_to(x, y):
    cur_x = get_pos_x()
    cur_y = get_pos_y()

    move_x = x - cur_x
    move_y = y - cur_y

    # Determine directions with ternary operators
    x_dir = East if move_x > 0 else West if move_x < 0 else None
    y_dir = North if move_y > 0 else South if move_y < 0 else None

    if x_dir is not None:
        for i in range(0, abs(move_x)):
            if not move(x_dir):
                return False

    if y_dir is not None:
        for i in range(0, abs(move_y)):
            if not move(y_dir):
                return False

    # moved_ok = x==get_pos_x() and y==get_pos_y()

    return True


def sort_array_bubblesort(arr):
    if arr is None:
        return False

    # bubbled = True
    # while(bubbled):
    # 	bubbled = False
    #
    # 	for i in range(len(fa)-1):
    # 		if fa[i] > fa[i+1]:
    # 			fa[i], fa[i+1] = fa[i+1], fa[i]
    # 			bubbled = True
    # return True

    n = len(arr)
    for i in range(n):
        # Flag um zu prüfen, ob getauscht wurde
        swapped = False

        # Letzten i Elemente sind bereits sortiert
        for j in range(0, n - i - 1):
            # Tausche, wenn das aktuelle Element größer ist als das nächste
            if arr[j] > arr[j + 1]:
                arr[j], arr[j + 1] = arr[j + 1], arr[j]
                swapped = True

        # Wenn keine Elemente getauscht wurden, ist die Liste sortiert
        if not swapped:
            break

    return True


def sort_two_arrays_by_first_array_bubblesort(arr, arrb):
    if arr is None or arrb is None or len(arr) != len(arrb):
        return False

    n = len(arr)
    for i in range(n):
        # Flag um zu prüfen, ob getauscht wurde
        swapped = False

        # Letzten i Elemente sind bereits sortiert
        for j in range(0, n - i - 1):
            # Tausche, wenn das aktuelle Element größer ist als das nächste
            if arr[j] > arr[j + 1]:
                arr[j], arr[j + 1] = arr[j + 1], arr[j]
                arrb[j], arrb[j + 1] = arrb[j + 1], arrb[j]
                swapped = True

        # Wenn keine Elemente getauscht wurden, ist die Liste sortiert
        if not swapped:
            break

    return True


def sort_array(arr):
    if arr is None:
        return False

    n = len(arr)

    # Quicksort-Implementierung (in-place, O(n log n) durchschnittlich)
    def quicksort_partition(low, high):
        # Wähle Pivot (mittleres Element für bessere Performance)
        mid = (low + high) // 2
        pivot = arr[mid]

        # Pivot ans Ende bewegen
        arr[mid], arr[high] = arr[high], arr[mid]

        i = low - 1

        for j in range(low, high):
            if arr[j] <= pivot:
                i += 1
                arr[i], arr[j] = arr[j], arr[i]

        # Pivot an richtige Position
        arr[i + 1], arr[high] = arr[high], arr[i + 1]

        return i + 1

    def quicksort_recursive(low, high):
        if low < high:
            pi = quicksort_partition(low, high)
            quicksort_recursive(low, pi - 1)
            quicksort_recursive(pi + 1, high)

    if n > 0:
        quicksort_recursive(0, n - 1)

    return True


def sort_two_arrays_by_first_array(arr, arrb):
    if arr is None or arrb is None or len(arr) != len(arrb):
        return False

    n = len(arr)

    # Quicksort-Implementierung (in-place, O(n log n) durchschnittlich)
    def quicksort_partition(low, high):
        # Wähle Pivot (mittleres Element für bessere Performance)
        mid = (low + high) // 2
        pivot = arr[mid]

        # Pivot ans Ende bewegen
        arr[mid], arr[high] = arr[high], arr[mid]
        arrb[mid], arrb[high] = arrb[high], arrb[mid]

        i = low - 1

        for j in range(low, high):
            if arr[j] <= pivot:
                i += 1
                arr[i], arr[j] = arr[j], arr[i]
                arrb[i], arrb[j] = arrb[j], arrb[i]

        # Pivot an richtige Position
        arr[i + 1], arr[high] = arr[high], arr[i + 1]
        arrb[i + 1], arrb[high] = arrb[high], arrb[i + 1]

        return i + 1

    def quicksort_recursive(low, high):
        if low < high:
            pi = quicksort_partition(low, high)
            quicksort_recursive(low, pi - 1)
            quicksort_recursive(pi + 1, high)

    if n > 0:
        quicksort_recursive(0, n - 1)

    return True


def shuffle_array(to_shuffle):
    ll = len(to_shuffle)

    for i in range(ll - 1, 0, -1):
        j = random() * ll // 1
        to_shuffle[i], to_shuffle[j] = to_shuffle[j], to_shuffle[i]


def shuffle_two_arrays(to_shuffle, shuffle_along):
    if len(to_shuffle) != len(shuffle_along):
        return False

    ll = len(to_shuffle)

    for i in range(ll - 1, 0, -1):
        j = random() * ll // 1
        to_shuffle[i], to_shuffle[j] = to_shuffle[j], to_shuffle[i]
        shuffle_along[i], shuffle_along[j] = shuffle_along[j], shuffle_along[i]

    return True


# move_deltas = {North: (0, 1), South: (0, -1), East: (1, 0), West: (-1, 0)}


def move_to_astar(zx, zy):
    my_directions = [North, South, East, West]

    cur_x = get_pos_x()
    cur_y = get_pos_y()
    ws = get_world_size()

    while cur_x != zx or cur_y != zy:
        shuffle_array(my_directions)

        # bestmoves = []
        # bestscores = []

        best_direction = None
        best_score = 1000000

        for direction in my_directions:
            dx, dy = deltas[direction]
            next_x = cur_x + dx
            next_y = cur_y + dy

            if ws > next_x >= 0 and ws > next_y >= 0:
                # if next_x < ws and next_y < ws and next_x >= 0 and next_y >= 0:
                score = manhattan_heuristic(next_x, next_y, zx, zy)
                if score < best_score:
                    best_score = score
                    best_direction = direction

        if best_direction is not None:
            moved = move(best_direction)
            if moved:
                dx, dy = deltas[best_direction]
                cur_x = cur_x + dx
                cur_y = cur_y + dy
            else:
                print("FAIL TO MOVE")
                return False

    return True


#
# def conditional_watering():
#     ret = 0
#     while (get_water() <= 0.75):
#         # print(get_water())
#         if num_items(Items.Water) > 0:
#             use_item(Items.Water)
#             ret = ret + 1
#         else:
#             break
#
#     return ret
#
#
# def plant_carrot(allow_fertilizer=True, allow_watering=True):
#     if get_ground_type() != Grounds.Soil:
#         till()
#
#     if allow_watering:
#         conditional_watering()
#
#     needed = 2 ** (num_unlocked(Entities.Carrot) - 1)
#
#     if num_items(Items.Hay) >= needed and num_items(Items.Wood) >= needed:
#         ret = plant(Entities.Carrot)
#         if allow_fertilizer:
#             if num_items(Items.Fertilizer) >= 1:
#                 use_item(Items.Fertilizer)
#         return ret
#
#     return False
#
#
# def plant_sunflower(allow_fertilizer=True, allow_watering=True):
#     if get_ground_type() != Grounds.Soil:
#         till()
#
#     if allow_watering:
#         conditional_watering()
#
#     if num_items(Items.Carrot) >= 1:
#         ret = plant(Entities.Sunflower)
#         if allow_fertilizer:
#             if num_items(Items.Fertilizer) >= 1:
#                 use_item(Items.Fertilizer)
#         return ret
#
#     return False
#
#
# def plant_bush(allow_fertilizer=True, allow_watering=True):
#     if allow_watering:
#         conditional_watering()
#
#     ret = plant(Entities.Bush)
#
#     if allow_fertilizer:
#         if num_items(Items.Fertilizer) >= 1:
#             use_item(Items.Fertilizer)
#
#     return ret
#
#
# def plant_tree(allow_fertilizer=True, allow_watering=True):
#     if allow_watering:
#         conditional_watering()
#
#     ret = plant(Entities.Tree)
#     if allow_fertilizer:
#         if num_items(Items.Fertilizer) >= 1:
#             use_item(Items.Fertilizer)
#     return ret
#
#
# def plant_pumpkin(allow_fertilizer=True, allow_watering=True):
#     if get_ground_type() != Grounds.Soil:
#         till()
#
#     if allow_watering:
#         conditional_watering()
#
#     needed = 2 ** (num_unlocked(Entities.Pumpkin) - 1)
#
#     if num_items(Items.Carrot) >= needed:
#         ret = plant(Entities.Pumpkin)
#         if allow_fertilizer:
#             if num_items(Items.Fertilizer) >= 1:
#                 use_item(Items.Fertilizer)
#
#         return ret
#
#     return False
#
#
# def plant_cactus(allow_fertilizer=True, allow_watering=True):
#     if get_ground_type() != Grounds.Soil:
#         till()
#
#     if allow_watering:
#         conditional_watering()
#
#     needed = 2 ** (num_unlocked(Entities.Cactus) - 1)
#
#     if num_items(Items.Pumpkin) >= needed:
#         ret = plant(Entities.Cactus)
#
#         if allow_fertilizer:
#             if num_items(Items.Fertilizer) >= 1:
#                 use_item(Items.Fertilizer)
#         return ret
#
#     return False
#
#
# def plant_grass(allow_fertilizer=True, allow_watering=True):
#     ret = False
#     if get_entity_type() != Entities.Grass:
#         harvest()
#         ret = True
#
#     if get_ground_type() == Grounds.Soil:
#         till()
#         ret = True
#
#     if allow_watering:
#         conditional_watering()
#
#     if allow_fertilizer:
#         if num_items(Items.Fertilizer) >= 1:
#             use_item(Items.Fertilizer)
#
#     return ret
#
#
# def plant_me(plant, allow_fertilizer=True, allow_watering=True):
#     allow_fertilizer = False
#
#     if plant == Entities.Carrot:
#         return plant_carrot(allow_fertilizer, allow_watering)
#     elif plant == Entities.Bush:
#         return plant_bush(allow_fertilizer, allow_watering)
#     elif plant == Entities.Grass:
#         return plant_grass(False, allow_watering)
#     elif plant == Entities.Cactus:
#         return plant_cactus(allow_fertilizer, allow_watering)
#     elif plant == Entities.Pumpkin:
#         return plant_pumpkin(allow_fertilizer, allow_watering)
#     elif plant == Entities.Sunflower:
#         return plant_sunflower(allow_fertilizer, allow_watering)
#     elif plant == Entities.Tree:
#         return plant_tree(allow_fertilizer, allow_watering)
#
#     return False
#
#
# def get_need_to_unlock_map(print_result=False, include_farmers_remains=True, include_top_hat=True):
#     stuff = []
#     stuff.append(Unlocks.Polyculture)
#     stuff.append(Unlocks.Dinosaurs)
#     stuff.append(Unlocks.Pumpkins)
#     stuff.append(Unlocks.Cactus)
#     stuff.append(Unlocks.Mazes)
#     stuff.append(Unlocks.Megafarm)
#
#     if include_farmers_remains:
#         stuff.append(Unlocks.The_Farmers_Remains)
#
#     if include_top_hat:
#         stuff.append(Unlocks.Top_Hat)
#
#     needmap = {}
#     unlocked_one = True
#
#     while (unlocked_one):
#         unlocked_one = False
#         move_to(0, 0)
#
#         for unlock_me in stuff:
#             mecost = get_cost(unlock_me)
#             # print("MECOST [",unlock_me,"]: ", mecost)
#
#             if mecost != None and len(mecost) > 0:
#                 allsatisfied = True
#                 for mc in mecost:
#                     cost = mecost[mc]
#
#                     if cost > num_items(mc):
#                         allsatisfied = False
#
#                     if mc in needmap:
#                         pc = needmap[mc]
#                         needmap[mc] = pc + cost
#                     else:
#                         needmap[mc] = cost
#
#                 if allsatisfied:
#                     print("Trying to unlock: ", unlock_me)
#                     move(North)
#                     res = unlock(unlock_me)
#                     if res:
#                         unlocked_one = True
#
#     for id in [Items.Bone, Items.Carrot, Items.Gold, Items.Pumpkin, Items.Cactus, Items.Wood, Items.Hay]:
#         if not id in needmap:
#             needmap[id] = 0
#         else:
#             needmap[id] = needmap[id] - num_items(id)
#             if needmap[id] < 0:
#                 needmap[id] = 0
#
#     if print_result:
#         move_to(0, 0)
#         print("Needed Carrots ", needmap[Items.Bone])
#
#         move_to(0, 5)
#         print("NEEDED CACTI ", needmap[Items.Cactus])
#
#         move_to(0, 10)
#         print("NEEDED PUMPKINS ", needmap[Items.Pumpkin])
#
#         move_to(0, 15)
#         print("NEEDED GOLD ", needmap[Items.Gold])
#
#     return needmap
#
#
# if __name__ == "__main__":
#     # print(get_cost(Unlocks.Top_Hat))
#     get_need_to_unlock_map(True, True, True)


def max_drones():
    return world.max_drones()


def num_drones():
    return world.num_drones()


def spawn_drone(task: Callable[[], Any]): ...


def wait_for(drone: Drone | Any): ...


def matchtester():
    carrot_1: EntityAndValues = EntityAndValues(entity=Entities.Carrot, size=1, growstate_percentage=0.5)
    carrot_2: EntityAndValues = EntityAndValues(entity=Entities.Carrot, size=5, growstate_percentage=0.2)

    pumpkin_1: EntityAndValues = EntityAndValues(entity=Entities.Carrot, size=5, growstate_percentage=0.1)
    pumpkin_2: EntityAndValues = EntityAndValues(entity=Entities.Pumpkin, size=20, growstate_percentage=0.8)

    for k in [carrot_1, carrot_2, pumpkin_1, pumpkin_2]:
        match k:
            case EntityAndValues(entity=Entities.Carrot):
                print(f"CARROT: {k=}")
            case EntityAndValues(size=sz) if sz > 3:
                print(f"SOMETHING with size >3: {k}")


if __name__ == "__main__":
    matchtester()
