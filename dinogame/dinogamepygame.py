import pygame
import sys
from enum import Enum, auto
from random import random
from typing import Tuple, List, Optional


class Hats(Enum):
    Golden_Cactus_Hat = auto()
    Dinosaur_Hat = auto()


class DinoGameSimulation:
    """Simuliert das Dino-Spiel mit vollständiger Logik"""

    def __init__(self, world_size: int = 8):
        self.world_size = world_size
        self.dino_x = 0
        self.dino_y = 0
        self.tail_positions = []
        self.apples_collected = 0
        self.current_hat = Hats.Golden_Cactus_Hat
        self.current_apple = None
        self.game_over = False
        self.moves_history = []
        self.path_to_apple = []

        # Für Step-by-Step Visualisierung
        self.planning_steps = []
        self.current_planning_step = 0
        self.is_planning = False
        self.execution_steps = []
        self.current_execution_step = 0
        self.is_executing = False
        self.target_apple = None

        # Richtungen
        self.North = 1
        self.East = 2
        self.West = 3
        self.South = 4

        self.directions = [self.North, self.South, self.East, self.West]
        self.opposite = {
            self.North: self.South,
            self.South: self.North,
            self.East: self.West,
            self.West: self.East
        }
        self.deltas = {
            self.North: (0, 1),
            self.South: (0, -1),
            self.East: (1, 0),
            self.West: (-1, 0)
        }

        self._spawn_new_apple()

    def _spawn_new_apple(self):
        """Spawnt einen neuen Apfel an zufälliger Position"""
        occupied = set(self.tail_positions)
        occupied.add((self.dino_x, self.dino_y))

        available_positions = []
        for x in range(self.world_size):
            for y in range(self.world_size):
                if (x, y) not in occupied:
                    available_positions.append((x, y))

        if available_positions:
            idx = int(random() * len(available_positions))
            self.current_apple = available_positions[idx]
        else:
            self.current_apple = None

    def measure(self) -> Optional[Tuple[int, int]]:
        """Gibt die Position des aktuellen Apfels zurück"""
        return self.current_apple

    def move(self, direction: int) -> bool:
        """Bewegt den Dino in die angegebene Richtung"""
        if self.current_hat != Hats.Dinosaur_Hat:
            return False

        dx, dy = self.deltas[direction]
        new_x = self.dino_x + dx
        new_y = self.dino_y + dy

        # Prüfe Grenzen
        if new_x < 0 or new_x >= self.world_size or new_y < 0 or new_y >= self.world_size:
            return False

        # Prüfe Kollision mit Schwanz
        if (new_x, new_y) in self.tail_positions:
            return False

        # Bewegung ist gültig
        old_pos = (self.dino_x, self.dino_y)
        self.dino_x = new_x
        self.dino_y = new_y

        # Apfel eingesammelt?
        if self.current_apple and (new_x, new_y) == self.current_apple:
            self.tail_positions.append(old_pos)
            self.apples_collected += 1
            self._spawn_new_apple()
        else:
            # Schwanz bewegen
            if self.tail_positions:
                self.tail_positions = self.tail_positions[1:] + [old_pos]

        self.moves_history.append((new_x, new_y))
        return True

    def change_hat(self, hat: Hats):
        """Wechselt den Hut"""
        self.current_hat = hat
        if hat == Hats.Golden_Cactus_Hat and self.tail_positions:
            self.tail_positions = []

    def heuristic(self, x, y, zx, zy):
        """Manhattan-Distanz für A*"""
        return abs(x - zx) + abs(y - zy)

    def can_move_safe(self, x, y, direction, tail_positions, prev_pos,
                      ignore_oldest_tail_segment=True, new_apple_found=False):
        """Prüft ob eine Bewegung sicher ist"""
        dx, dy = self.deltas[direction]
        new_x = x + dx
        new_y = y + dy

        # Prüfe Grenzen
        if new_x < 0 or new_x >= self.world_size or new_y < 0 or new_y >= self.world_size:
            return False

        # Prüfe Schwanz
        tail_positions_cut = tail_positions
        if ignore_oldest_tail_segment and not new_apple_found:
            tail_positions_cut = tail_positions[1:]

        if (new_x, new_y) in tail_positions_cut:
            return False

        return True

    def find_path_astar(self, x, y, zx, zy, _tail_positions, new_apple_found=False):
        """A*-Pfadfindung mit Schwanzkollisionsvermeidung"""
        visited = []
        path_stack = []
        oldest_tail_element_at_stack = []

        self.planning_steps = []

        moves_made = 0
        max_moves = self.world_size * self.world_size * 3

        tail_positions_copy = _tail_positions[0:]
        prev_pos = None

        while moves_made < max_moves:
            if x == zx and y == zy:
                return True, path_stack

            my_new_apple_found = False
            if len(path_stack) == 0:
                my_new_apple_found = new_apple_found

            best_direction = None
            best_score = 1000000

            ll = len(self.directions)
            for i in range(ll - 1, 0, -1):
                j = int(random() * ll)
                self.directions[i], self.directions[j] = self.directions[j], self.directions[i]

            for direction in self.directions:
                if self.can_move_safe(x, y, direction, tail_positions_copy, prev_pos,
                                      False, my_new_apple_found):
                    dx, dy = self.deltas[direction]
                    next_x = x + dx
                    next_y = y + dy

                    if (x, y, next_x, next_y, direction) not in visited:
                        score = self.heuristic(next_x, next_y, zx, zy)
                        if score < best_score:
                            best_score = score
                            best_direction = direction

            current_x = x
            current_y = y

            if best_direction is not None:
                prev_pos = (current_x, current_y)

                oldest_tail_element = None
                if len(tail_positions_copy) > 0:
                    oldest_tail_element = tail_positions_copy[0]
                    tail_positions_copy = tail_positions_copy[1:]

                tail_positions_copy.append(prev_pos)

                dx, dy = self.deltas[best_direction]
                x = x + dx
                y = y + dy

                path_stack.append(best_direction)
                oldest_tail_element_at_stack.append(oldest_tail_element)
                visited.append((current_x, current_y, x, y, best_direction))

                self.planning_steps.append({
                    'type': 'forward',
                    'position': (x, y),
                    'from': (current_x, current_y),
                    'path': path_stack[:],
                    'tail': tail_positions_copy[:]
                })
            else:
                if len(path_stack) == 0:
                    return False, path_stack

                last_move = path_stack.pop()
                oldest_tail_element = None
                if len(oldest_tail_element_at_stack) > 0:
                    oldest_tail_element = oldest_tail_element_at_stack.pop()

                mdir = self.opposite[last_move]
                newest_tail_element = tail_positions_copy.pop()
                prev_pos = newest_tail_element

                dx, dy = self.deltas[mdir]
                x = x + dx
                y = y + dy

                if oldest_tail_element is not None:
                    tail_positions_copy.insert(0, oldest_tail_element)

                self.planning_steps.append({
                    'type': 'backtrack',
                    'position': (x, y),
                    'from': (current_x, current_y),
                    'path': path_stack[:],
                    'tail': tail_positions_copy[:]
                })

            moves_made += 1

        return False, path_stack

    def collect_next_apple(self) -> bool:
        """Sammelt den nächsten Apfel"""
        if not self.current_apple:
            self.game_over = True
            return False

        self.change_hat(Hats.Dinosaur_Hat)

        apple_x, apple_y = self.current_apple
        self.target_apple = self.current_apple

        success, path_stack = self.find_path_astar(
            self.dino_x, self.dino_y,
            apple_x, apple_y,
            self.tail_positions,
            new_apple_found=True
        )

        if not success:
            self.game_over = True
            return False

        self.path_to_apple = path_stack[:]

        self.execution_steps = []
        temp_x, temp_y = self.dino_x, self.dino_y
        temp_tail = self.tail_positions[:]

        for step in path_stack:
            dx, dy = self.deltas[step]
            new_x = temp_x + dx
            new_y = temp_y + dy

            apple_collected = (new_x, new_y) == self.current_apple

            self.execution_steps.append({
                'direction': step,
                'from': (temp_x, temp_y),
                'to': (new_x, new_y),
                'tail_before': temp_tail[:],
                'apple_collected': apple_collected
            })

            if apple_collected:
                temp_tail.append((temp_x, temp_y))
            elif temp_tail:
                temp_tail = temp_tail[1:] + [(temp_x, temp_y)]

            temp_x, temp_y = new_x, new_y

        return True


class DinoGameVisualizer:
    """Visualisiert das Dino-Spiel mit pygame"""

    def __init__(self, world_size: int = 8, cell_size: int = 60):
        pygame.init()

        self.sim = DinoGameSimulation(world_size)
        self.cell_size = cell_size
        self.grid_width = world_size * cell_size
        self.stats_width = 400
        self.screen_width = self.grid_width + self.stats_width
        self.screen_height = world_size * cell_size + 50  # +50 für Titel

        self.screen = pygame.display.set_mode((self.screen_width, self.screen_height))
        pygame.display.set_caption('Dino-Spiel Simulation')

        self.clock = pygame.time.Clock()
        self.fps = 2  # Frames pro Sekunde

        # Farben
        self.COLOR_BG = (240, 240, 240)
        self.COLOR_GRID = (200, 200, 200)
        self.COLOR_CELL_BG = (250, 250, 250)
        self.COLOR_TAIL = (50, 200, 50)
        self.COLOR_TAIL_DARK = (30, 150, 30)
        self.COLOR_APPLE = (255, 50, 50)
        self.COLOR_APPLE_DARK = (200, 0, 0)
        self.COLOR_DINO_GOLD = (255, 215, 0)
        self.COLOR_DINO_PURPLE = (147, 112, 219)
        self.COLOR_PATH = (100, 150, 255)
        self.COLOR_TEXT = (0, 0, 0)
        self.COLOR_TITLE_BG = (70, 130, 180)
        self.COLOR_WHITE = (255, 255, 255)

        # Fonts
        self.font_title = pygame.font.SysFont('Arial', 24, bold=True)
        self.font_stats = pygame.font.SysFont('Arial', 16)
        self.font_emoji = pygame.font.SysFont('Segoe UI Emoji', 32)
        self.font_small = pygame.font.SysFont('Arial', 12)

        # Animationsphasen
        self.current_phase = 'idle'
        self.planning_index = 0
        self.execution_index = 0
        self.frame_count = 0

    def grid_to_screen(self, x: int, y: int) -> Tuple[int, int]:
        """Konvertiert Grid-Koordinaten zu Bildschirm-Koordinaten"""
        # Y-Achse umkehren für pygame (0,0 ist oben links)
        screen_x = x * self.cell_size
        screen_y = (self.sim.world_size - 1 - y) * self.cell_size + 50  # +50 für Titel
        return screen_x, screen_y

    def draw_grid(self):
        """Zeichnet das Spielfeld-Gitter"""
        for i in range(self.sim.world_size):
            for j in range(self.sim.world_size):
                x, y = self.grid_to_screen(i, j)
                rect = pygame.Rect(x, y, self.cell_size, self.cell_size)
                pygame.draw.rect(self.screen, self.COLOR_CELL_BG, rect)
                pygame.draw.rect(self.screen, self.COLOR_GRID, rect, 1)

    def draw_tail(self, tail_positions: List[Tuple[int, int]], alpha: float = 1.0):
        """Zeichnet den Schwanz"""
        for i, (tx, ty) in enumerate(tail_positions):
            x, y = self.grid_to_screen(tx, ty)

            # Intensität basierend auf Position im Schwanz
            intensity = 0.3 + (i / max(1, len(tail_positions))) * 0.7

            # Schwanz-Segment zeichnen
            padding = 6
            rect = pygame.Rect(x + padding, y + padding,
                               self.cell_size - 2 * padding,
                               self.cell_size - 2 * padding)

            color = tuple(int(c * intensity * alpha) for c in self.COLOR_TAIL)
            pygame.draw.rect(self.screen, color, rect, border_radius=5)
            pygame.draw.rect(self.screen, self.COLOR_TAIL_DARK, rect, 2, border_radius=5)

            # Nummer im Segment
            text = self.font_small.render(str(i + 1), True, self.COLOR_WHITE)
            text_rect = text.get_rect(center=(x + self.cell_size // 2, y + self.cell_size // 2))
            self.screen.blit(text, text_rect)

    def draw_apple(self, apple_pos: Optional[Tuple[int, int]]):
        """Zeichnet den Apfel"""
        if apple_pos:
            ax, ay = apple_pos
            x, y = self.grid_to_screen(ax, ay)

            center = (x + self.cell_size // 2, y + self.cell_size // 2)
            radius = self.cell_size // 4

            pygame.draw.circle(self.screen, self.COLOR_APPLE, center, radius)
            pygame.draw.circle(self.screen, self.COLOR_APPLE_DARK, center, radius, 2)

            # Emoji
            emoji = self.font_emoji.render('🍎', True, self.COLOR_TEXT)
            emoji_rect = emoji.get_rect(center=center)
            self.screen.blit(emoji, emoji_rect)

    def draw_path(self, path: List[int], start_x: int, start_y: int):
        """Zeichnet den geplanten Pfad"""
        if not path or len(path) == 0:
            return

        points = []
        temp_x, temp_y = start_x, start_y

        # Startpunkt
        sx, sy = self.grid_to_screen(temp_x, temp_y)
        points.append((sx + self.cell_size // 2, sy + self.cell_size // 2))

        # Pfadpunkte
        for direction in path:
            dx, dy = self.sim.deltas[direction]
            temp_x += dx
            temp_y += dy
            px, py = self.grid_to_screen(temp_x, temp_y)
            points.append((px + self.cell_size // 2, py + self.cell_size // 2))

        # Pfad als Linie zeichnen
        if len(points) > 1:
            pygame.draw.lines(self.screen, self.COLOR_PATH, False, points, 4)

            # Markiere Start
            pygame.draw.circle(self.screen, (0, 255, 0), points[0], 8)

            # Markiere Ziel
            pygame.draw.circle(self.screen, (255, 0, 0), points[-1], 12)
            pygame.draw.circle(self.screen, (255, 255, 0), points[-1], 8)

    def draw_dino(self, x: int, y: int, alpha: float = 1.0):
        """Zeichnet den Dinosaurier"""
        sx, sy = self.grid_to_screen(x, y)

        color = self.COLOR_DINO_GOLD if self.sim.current_hat == Hats.Golden_Cactus_Hat else self.COLOR_DINO_PURPLE

        # Dino-Körper
        padding = 3
        rect = pygame.Rect(sx + padding, sy + padding,
                           self.cell_size - 2 * padding,
                           self.cell_size - 2 * padding)

        if alpha < 1.0:
            # Transparenter Dino für Planung
            surface = pygame.Surface((self.cell_size - 2 * padding, self.cell_size - 2 * padding))
            surface.set_alpha(int(255 * alpha))
            surface.fill(color)
            self.screen.blit(surface, (sx + padding, sy + padding))
            pygame.draw.rect(self.screen, (0, 0, 0), rect, 3, border_radius=5)
        else:
            pygame.draw.rect(self.screen, color, rect, border_radius=5)
            pygame.draw.rect(self.screen, (0, 0, 0), rect, 3, border_radius=5)

        # Dino Emoji
        emoji = self.font_emoji.render('🦖', True, self.COLOR_TEXT)
        emoji.set_alpha(int(255 * alpha))
        emoji_rect = emoji.get_rect(center=(sx + self.cell_size // 2, sy + self.cell_size // 2))
        self.screen.blit(emoji, emoji_rect)

    def draw_game_state(self, planning_state=None):
        """Zeichnet den gesamten Spielzustand"""
        # Gitter zeichnen
        self.draw_grid()

        # Bestimme was gezeichnet werden soll
        if planning_state:
            tail_positions = planning_state['tail']
            dino_x, dino_y = planning_state['position']
            path_to_show = planning_state['path']
            is_planning = True
            apple_pos = self.sim.target_apple
        else:
            tail_positions = self.sim.tail_positions
            dino_x, dino_y = self.sim.dino_x, self.sim.dino_y
            if self.current_phase == 'executing' and self.execution_index < len(self.sim.execution_steps):
                path_to_show = self.sim.path_to_apple[self.execution_index:]
            else:
                path_to_show = []
            is_planning = False
            apple_pos = self.sim.current_apple

        # Zeichne Komponenten
        self.draw_tail(tail_positions)
        self.draw_apple(apple_pos)

        if path_to_show and len(path_to_show) > 0:
            self.draw_path(path_to_show, dino_x, dino_y)

        if is_planning:
            # Zeige echten Dino transparent
            self.draw_dino(self.sim.dino_x, self.sim.dino_y, alpha=0.2)
            # Zeige Planungs-Dino
            self.draw_dino(dino_x, dino_y, alpha=0.4)
        else:
            self.draw_dino(dino_x, dino_y)

    def draw_title(self):
        """Zeichnet die Titelleiste"""
        title_rect = pygame.Rect(0, 0, self.grid_width, 50)
        pygame.draw.rect(self.screen, self.COLOR_TITLE_BG, title_rect)

        phase_emoji = {
            'idle': '⏸️',
            'planning': '🧠',
            'executing': '🏃'
        }

        title_text = f"Dino-Spiel: {self.sim.apples_collected} Äpfel"
        if self.current_phase in phase_emoji:
            title_text += f" {phase_emoji[self.current_phase]}"
        if self.sim.game_over:
            title_text += " 🏁 GAME OVER"

        text = self.font_title.render(title_text, True, self.COLOR_WHITE)
        text_rect = text.get_rect(center=(self.grid_width // 2, 25))
        self.screen.blit(text, text_rect)

    def draw_statistics(self):
        """Zeichnet die Statistik-Sidebar"""
        stats_x = self.grid_width
        stats_rect = pygame.Rect(stats_x, 0, self.stats_width, self.screen_height)
        pygame.draw.rect(self.screen, (250, 250, 230), stats_rect)
        pygame.draw.line(self.screen, self.COLOR_GRID, (stats_x, 0), (stats_x, self.screen_height), 2)

        y_offset = 20
        line_height = 25

        # Titel
        title = self.font_title.render("STATISTIKEN", True, self.COLOR_TEXT)
        self.screen.blit(title, (stats_x + 20, y_offset))
        y_offset += 40

        # Statistiken
        stats = [
            f"Weltgröße: {self.sim.world_size} × {self.sim.world_size}",
            f"Äpfel: {self.sim.apples_collected}",
            f"Schwanzlänge: {len(self.sim.tail_positions)}",
            f"Position: ({self.sim.dino_x}, {self.sim.dino_y})",
            "",
            f"Hut: {self.sim.current_hat.name.replace('_', ' ')}",
        ]

        # Phase
        if self.current_phase == 'planning':
            stats.append(f"Phase: Planung {self.planning_index + 1}/{len(self.sim.planning_steps)}")
        elif self.current_phase == 'executing':
            stats.append(f"Phase: Ausführung {self.execution_index + 1}/{len(self.sim.execution_steps)}")
        else:
            stats.append("Phase: Bereit")

        stats.append("")
        max_apples = self.sim.world_size * self.sim.world_size - 1
        progress = (self.sim.apples_collected / max(1, max_apples)) * 100
        stats.append(f"Max. Äpfel: {max_apples}")
        stats.append(f"Fortschritt: {progress:.1f}%")

        for stat in stats:
            text = self.font_stats.render(stat, True, self.COLOR_TEXT)
            self.screen.blit(text, (stats_x + 20, y_offset))
            y_offset += line_height

        # Legende
        y_offset += 20
        legend_title = self.font_title.render("LEGENDE", True, self.COLOR_TEXT)
        self.screen.blit(legend_title, (stats_x + 20, y_offset))
        y_offset += 40

        legend = [
            "🦖 = Dinosaurier",
            "🍎 = Apfel",
            "🟩 = Schwanz",
            "━━ = Pfad",
            "",
            "Gold = Schwanz ernten",
            "Lila = Äpfel sammeln",
        ]

        for item in legend:
            text = self.font_small.render(item, True, self.COLOR_TEXT)
            self.screen.blit(text, (stats_x + 20, y_offset))
            y_offset += 20

    def update(self):
        """Update-Logik für jeden Frame"""
        if self.sim.game_over:
            return False

        # Phase 1: Neue Planung starten
        if self.current_phase == 'idle':
            max_apples = self.sim.world_size * self.sim.world_size - 1
            if self.sim.apples_collected < max_apples:
                success = self.sim.collect_next_apple()
                if not success:
                    print(f"Game Over! {self.sim.apples_collected} Äpfel gesammelt.")
                    self.sim.game_over = True
                    return False
                else:
                    self.current_phase = 'planning'
                    self.planning_index = 0
                    self.execution_index = 0
            else:
                self.sim.game_over = True
                print(f"Spiel gewonnen! Alle {self.sim.apples_collected} Äpfel gesammelt!")
                return False

        # Phase 2: Planungsschritte visualisieren
        elif self.current_phase == 'planning':
            if self.planning_index < len(self.sim.planning_steps):
                self.planning_index += 1
                return True
            else:
                self.current_phase = 'executing'
                self.execution_index = 0

        # Phase 3: Ausführungsschritte visualisieren
        elif self.current_phase == 'executing':
            if self.execution_index < len(self.sim.execution_steps):
                step = self.sim.execution_steps[self.execution_index]
                self.sim.move(step['direction'])
                self.execution_index += 1
                return True
            else:
                self.current_phase = 'idle'
                self.sim.path_to_apple = []
                self.sim.target_apple = None

        return True

    def run(self):
        """Hauptschleife"""
        running = True

        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        running = False

            # Hintergrund
            self.screen.fill(self.COLOR_BG)

            # Spielzustand zeichnen
            if self.current_phase == 'planning' and self.planning_index > 0:
                planning_state = self.sim.planning_steps[self.planning_index - 1]
                self.draw_game_state(planning_state)
            else:
                self.draw_game_state()

            # UI zeichnen
            self.draw_title()
            self.draw_statistics()

            pygame.display.flip()

            # Update Logik
            if not self.sim.game_over:
                self.update()

            self.clock.tick(self.fps)
            self.frame_count += 1

        pygame.quit()
        sys.exit()


def main():
    """Hauptfunktion"""
    print("Starte Dino-Spiel Visualisierung mit pygame...")
    print("Drücke ESC oder schließe das Fenster zum Beenden.")

    # Erstelle Visualisierung
    visualizer = DinoGameVisualizer(world_size=8, cell_size=60)
    visualizer.run()


if __name__ == "__main__":
    main()