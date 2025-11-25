from dinogame_utils import *


class SimpleDeque:
    """Simple deque implementation using two lists for O(1) operations at both ends"""

    def __init__(self, iterable=None):
        self.left = []
        self.right = []
        if iterable:
            self.right.extend(iterable)

    def append(self, item):
        self.right.append(item)

    def appendleft(self, item):
        self.left.append(item)

    def pop(self):
        if self.right:
            return self.right.pop()
        elif self.left:
            self.left.reverse()
            self.right = self.left
            self.left = []
            return self.right.pop()
        else:
            raise IndexError("pop from empty deque")

    def popleft(self):
        if self.left:
            return self.left.pop()
        elif self.right:
            self.right.reverse()
            self.left = self.right
            self.right = []
            return self.left.pop()
        else:
            raise IndexError("pop from empty deque")

    def __len__(self):
        return len(self.left) + len(self.right)

    def __bool__(self):
        return bool(self.left or self.right)

    def to_list(self):
        result = self.left[:]
        result.reverse()
        result.extend(self.right)
        return result


def create_simple_deque(iterable=None):
    # State stored in closure
    state = {"left": [], "right": []}

    if iterable is not None:
        state["right"] += iterable

    def reverse_list(lst):
        # """Custom reverse function since .reverse() is not available"""
        n = len(lst)
        for i in range(n // 2):
            lst[i], lst[n - 1 - i] = lst[n - 1 - i], lst[i]

    def appendright(item):
        state["right"].append(item)

    def appendleft(item):
        state["left"].append(item)

    def popright():
        if state["right"]:
            return state["right"].pop()
        elif state["left"]:
            reverse_list(state["left"])
            state["right"] = state["left"]
            state["left"] = []
            r = state["right"]
            return r.pop()
        # else:
        #     raise IndexError("pop from empty deque")
        return None

    def popleft():
        if state["left"]:
            return state["left"].pop()
        elif state["right"]:
            reverse_list(state["right"])
            state["left"] = state["right"]
            state["right"] = []
            l = state["left"]
            return l.pop()
        # else:
        #     raise IndexError("pop from empty deque")
        return None

    def get_length():
        return len(state["left"]) + len(state["right"])

    def is_empty():
        return not (state["left"] or state["right"])

    def get_bool():
        return not is_empty()

    def to_list():
        result = state["left"][:]
        reverse_list(result)
        result = result + state["right"]
        return result

    def contains_no_skip_first(item):
        if item in state["left"] or item in state["right"]:
            return True

        return False

    def contains_skip_first(item):
        # """Check if item exists in deque, ignoring the first element"""
        total_length = len(state["left"]) + len(state["right"])

        # If deque has 0 or 1 elements, nothing to check after skipping first
        if total_length <= 1:
            return False

        # The first element is either:
        # - Last element of left list (if left is not empty)
        # - First element of right list (if left is empty)

        if state["left"]:
            # First element is state['left'][-1]
            # Check remaining elements in left (excluding the last one)
            ll = len(state["left"])
            if ll > 1:
                for i in range(ll - 1):
                    if state["left"][i] == item:
                        return True

            # Check all elements in right
            if item in state["right"]:
                return True
        else:
            # First element is state['right'][0]
            # Check remaining elements in right (excluding the first one)
            lr = len(state["right"])
            if lr > 1:
                for i in range(1, lr):
                    if state["right"][i] == item:
                        return True

        return False

    # Return dictionary with bound methods
    return {
        "appendright": appendright,
        "appendleft": appendleft,
        "popright": popright,
        "popleft": popleft,
        "get_length": get_length,
        "bool": get_bool,
        "to_list": to_list,
        "contains_skip_first": contains_skip_first,
        "contains_no_skip_first": contains_no_skip_first,
        "__len__": get_length,
        "__bool__": get_bool,
        "__state__": state,
    }


# def can_move_safe(x, y, direction, tail_positions, farm_x, farm_y, prev_pos, ignore_oldest_tail_segment=True,
#                   new_apple_found=False):
#     dx, dy = deltas[direction]
#     new_x = x + dx
#     new_y = y + dy
#
#     # Prüfe Farm-Grenzen
#     if new_x < 0 or new_x >= farm_x or new_y < 0 or new_y >= farm_y:
#         return False
#
#     # if (new_x, new_y) == prev_pos:
#     #	return False
#
#     # Prüfe Schwanz (außer letztes Segment - das älteste am Anfang, da es sich wegbewegt)
#     tail_positions_cut = tail_positions
#     if ignore_oldest_tail_segment and not new_apple_found:
#         tail_positions_cut = tail_positions[1:]
#
#     if (new_x, new_y) in tail_positions_cut:
#         return False
#
#     return True


def can_move_safe(
    x,
    y,
    direction,
    tail_positions_deque,
    farm_x,
    farm_y,
    prev_pos,
    ignore_oldest_tail_segment=True,
    new_apple_found=False,
):
    dx, dy = deltas[direction]
    new_x = x + dx
    new_y = y + dy

    # Prüfe Farm-Grenzen
    if new_x < 0 or new_x >= farm_x or new_y < 0 or new_y >= farm_y:
        return False

    # Prüfe Schwanz (außer letztes Segment - das älteste am Anfang, da es sich wegbewegt)
    if ignore_oldest_tail_segment and not new_apple_found:
        found = tail_positions_deque["contains_skip_first"]((new_x, new_y))
        return not found

    found = tail_positions_deque["contains_no_skip_first"]((new_x, new_y))
    return not found


def find_path_astar_with_tail_collision_avoidance(
    x, y, zx, zy, _tail_positions_deque, farm_x, farm_y, new_apple_found=False, allow_drone_spawn=True, _visited=None
):
    visited = _visited  # should be array -> may visit same twice (<- different tail!)
    if visited == None:
        visited = []

    path_stack = []  # Stack für Backtracking
    oldest_tail_element_at_stack = []

    moves_made = 0
    backtracks = 0
    max_moves = farm_x * farm_y * 3  # Sicherheitslimit

    # tail_positions_copy = _tail_positions[0:]
    # tail_positions_offset = len(tail_positions_copy)

    # Verwende dict-basierte Deque für O(1) Operationen
    # tail_deque = create_simple_deque(_tail_positions)
    tail_deque = create_simple_deque(_tail_positions_deque["to_list"]())

    prev_pos = None

    last_ok_move_at = None

    while moves_made < max_moves:
        # Ziel erreicht?
        if x == zx and y == zy:
            return True, path_stack, visited

        my_new_apple_found = False
        if len(path_stack) == 0:
            # reset...
            my_new_apple_found = new_apple_found

        # Finde beste nächste Richtung basierend auf Heuristik
        best_direction = None
        best_score = 1000000

        # Randomisiere Richtungen für Variabilität
        ll = len(directions)
        for i in range(ll - 1, 0, -1):
            j = random() * ll // 1
            directions[i], directions[j] = directions[j], directions[i]

        ok_directions = []
        # Evaluiere alle Richtungen
        for direction in directions:
            if can_move_safe(x, y, direction, tail_deque, farm_x, farm_y, prev_pos, False, my_new_apple_found):
                dx, dy = deltas[direction]
                next_x = x + dx
                next_y = y + dy

                # visited.append((current_x, current_y, x, y, best_direction))
                if (x, y, next_x, next_y, direction) not in visited:
                    score = manhattan_heuristic(next_x, next_y, zx, zy)
                    ok_directions.append(direction)
                    if score < best_score:
                        best_score = score
                        best_direction = direction

        # Speichere aktuelle Position für Schwanz (wird zum neuesten Segment)
        current_x = x  # get_pos_x()
        current_y = y  # get_pos_y()

        if best_direction != None:
            prev_pos = (current_x, current_y)

            dx, dy = deltas[best_direction]
            new_x = x + dx
            new_y = y + dy

            # sollte eigentlich sogar die tail-positions mit einbeziehen!!!
            visited.append((current_x, current_y, new_x, new_y, best_direction))

            # TODO hier könnte ich jetzt sogar noch eine drone spawnen...
            if allow_drone_spawn:
                spawned_drones = []
                for other_dir in ok_directions:
                    if other_dir == best_direction:
                        continue
                    available_drones = max_drones() - num_drones()
                    if available_drones == 0:
                        break

                    def mytask():
                        # angeblich ist visited hier schon eine copy...
                        # angeblich ist _tail_positions hier schon eine copy...
                        return find_path_astar_with_tail_collision_avoidance(
                            current_x,
                            current_y,
                            zx,
                            zy,
                            _tail_positions_deque,
                            farm_x,
                            farm_y,
                            my_new_apple_found,
                            True,
                            visited,
                        )

                    medrone = spawn_drone(mytask)
                    spawned_drones.append(medrone)

                for dm in spawned_drones:
                    if dm == None:
                        # failsafe if spawn has failed...
                        continue
                    found_other, path_stack_other, visited_other = wait_for(dm)
                    if found_other:
                        return True, path_stack + path_stack_other, visited_other
                    else:
                        # combine visited_other with current one // or just replace current one?!
                        visited = visited_other

            # Bewege in beste Richtung

            oldest_tail_element = None
            if tail_deque["get_length"]() > 0:
                oldest_tail_element = tail_deque["popleft"]()  # tail_positions_copy[0]
            # tail_positions_copy = tail_positions_copy[1:]

            # Füge aktuelle Position am Ende hinzu (neustes Segment)
            # tail_positions_copy.append(prev_pos)
            tail_deque["appendright"](prev_pos)

            x = new_x
            y = new_y

            path_stack.append(best_direction)
            oldest_tail_element_at_stack.append(oldest_tail_element)

            # sollte eigentlich sogar die tail-positions mit einbeziehen!!!
            # visited.append((current_x, current_y, x, y, best_direction))

            last_ok_move_at = (current_x, current_y, x, y)
        else:
            # Backtracking: Kein unbesuchter Nachbar gefunden
            if len(path_stack) == 0:
                return False, path_stack, visited  # Kein Pfad gefunden

            # from_x, from_y, visited_x, vistited_y, visited_by_pos = visited.pop()

            last_move = path_stack.pop()
            oldest_tail_element = None
            if len(oldest_tail_element_at_stack) > 0:
                oldest_tail_element = oldest_tail_element_at_stack.pop()

            mdir = opposite[last_move]

            # do not actually move here!
            # jüngstes element aus Schwanz entfernen -> ist ja backtracking -> "ein schritt zurück"
            newest_tail_element = tail_deque["popright"]()  # tail_positions_copy.pop()
            prev_pos = newest_tail_element

            dx, dy = deltas[mdir]
            x = x + dx
            y = y + dy

            # visited.remove((x, y))

            # tail_positions_copy.insert(0, (x, y))
            if oldest_tail_element != None:
                # tail_positions_copy.insert(0, oldest_tail_element)
                tail_deque["appendleft"](oldest_tail_element)

            backtracks += 1

        moves_made += 1

    # Max moves erreicht
    print("MAX MOVES REACHED: ", max_moves)
    return False, path_stack, visited


def can_still_move_anywhere(
    x, y, tail_positions_deque, farm_x, farm_y, prev_pos, ignore_oldest_tail_segment=True, new_apple_found=False
):
    for direction in directions:
        if can_move_safe(
            x, y, direction, tail_positions_deque, farm_x, farm_y, prev_pos, ignore_oldest_tail_segment, new_apple_found
        ):
            return True

    return False


def collect_apples_astar():
    # Rüste Dinosaurier-Hut aus
    move_to(0, 0)

    change_hat(Hats.Dinosaur_Hat)

    # Farm-Dimensionen
    farm_x = get_world_size()
    farm_y = get_world_size()

    # Schwanz-Tracking
    # tail_positions = []
    tail_positions_deque = create_simple_deque()

    tail_length = 0
    apples_collected = 0

    # Sammle Äpfel bis die Farm voll ist
    max_possible_apples = farm_x * farm_y - 1

    actual_x = get_pos_x()
    actual_y = get_pos_y()

    prev_pos = None

    while apples_collected < max_possible_apples:
        # Hole Position des nächsten Apfels
        new_apple_found = True

        tapple = measure()

        if tapple == None:
            print("NO APPLE")
            break

        apple_x, apple_y = tapple
        tappple = None

        # Bewege zum Apfel mit A*
        success, path_stack, visited = find_path_astar_with_tail_collision_avoidance(
            actual_x, actual_y, apple_x, apple_y, tail_positions_deque, farm_x, farm_y, new_apple_found
        )

        if not success:
            print("Kann nicht zum Apfel bewegen!")
            break

        movefail = False
        for step in path_stack:
            moved = move(step)
            if moved:
                prev_pos = (actual_x, actual_y)

                if new_apple_found:
                    # first step
                    # tail_positions.append((actual_x, actual_y))
                    tail_positions_deque["appendright"]((actual_x, actual_y))

                    apples_collected += 1
                    new_apple_found = False
                else:
                    # popped_tail = tail_positions[0]
                    # tail_positions = tail_positions[1:]
                    # tail_positions.append(prev_pos)
                    popped_tail = tail_positions_deque["popleft"]()
                    tail_positions_deque["appendright"](prev_pos)

                dx, dy = deltas[step]
                actual_x = actual_x + dx
                actual_y = actual_y + dy
            else:
                movefail = True
                print("MOVEFAIL::", step)
                # move(South)
                break

        if actual_x != get_pos_x() or actual_y != get_pos_y():
            print("MISMATCH: ", actual_x, "!=", get_pos_x(), " ", actual_y, "!=", get_pos_y())

        # Prüfe ob wir uns noch bewegen können
        # new_apple_found -> sollte hier eigentlich immer False sein...
        if not can_still_move_anywhere(
            actual_x, actual_y, tail_positions_deque, farm_x, farm_y, prev_pos, False, new_apple_found
        ):
            print("Keine Bewegung mehr möglich - Farm ist voll!")
            break

    # Ernte den Schwanz durch Wechseln des Huts
    change_hat(Hats.Golden_Cactus_Hat)

    bones = tail_length**2

    return apples_collected, bones


def controller(world_size=-1, exec_speed=-1, do_harvest=False, maxloops=None, reset_wordl_size_afterwards=True):
    if world_size > 3:
        set_world_size(world_size)

    ws = get_world_size()

    # if do_harvest:
    #     from companionplanter import multi_harvest
    #     multi_harvest(0, 0, ws, ws)

    if exec_speed > 0:
        set_execution_speed(exec_speed)

    change_hat(Hats.Golden_Cactus_Hat)

    loopcounter = 0
    while True:
        apples, bones = collect_apples_astar()
        loopcounter += 1

        if maxloops != None and loopcounter >= maxloops:
            break

    if world_size > 3 and reset_wordl_size_afterwards:
        set_world_size(-1)


def testmoves():
    move_to(3, 3)
    set_execution_speed(1)
    move(North)

    change_hat(Hats.Dinosaur_Hat)
    move(North)
    medir = [South, East, North, West]

    for i in medir:
        moved = move(i)
        if not moved:
            print("FAILED: ", i)

    change_hat(Hats.Cactus_Hat)


if __name__ == "__main__":
    # testmoves()

    deque = create_simple_deque([1, 2, 3, 4])
    result1 = deque["contains_skip_first"](3)  # Returns True (ignores first element 1)
    print(f"{result1=}")

    result2 = deque["contains_skip_first"](1)  # Returns True (ignores first element 1)
    print(f"{result2=}")

    # controller(8, -1, False)

    # change_hat(Hats.Dinosaur_Hat)
    # moved = move(North)
    # moved = move(South)
    # if not moved:
    # 	print("MOVE FAIL SOUTH")
    # change_hat(Hats.Golden_Cactus_Hat)
