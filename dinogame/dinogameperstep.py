import warnings
from enum import Enum, auto
from pathlib import Path
from random import random
from typing import List, Optional, Tuple

import matplotlib.animation as animation
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Circle, Rectangle

# Noto Sans als Standard mit Emoji-Fallback
plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["font.sans-serif"] = [
    "Noto Sans",
    "Noto Color Emoji",
    "Segoe UI Emoji",
    "Apple Color Emoji",
    "DejaVu Sans",
]

# Warnungen für fehlende Glyphen unterdrücken
# warnings.filterwarnings('ignore', category=UserWarning, message='.*Glyph.*missing from font.*')


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

        # Richtungen
        self.North = 1
        self.East = 2
        self.West = 3
        self.South = 4

        self.directions = [self.North, self.South, self.East, self.West]
        self.opposite = {self.North: self.South, self.South: self.North, self.East: self.West, self.West: self.East}
        self.deltas = {self.North: (0, 1), self.South: (0, -1), self.East: (1, 0), self.West: (-1, 0)}

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
            # Schwanz "ernten"
            self.tail_positions = []

    def heuristic(self, x, y, zx, zy):
        """Manhattan-Distanz für A*"""
        return abs(x - zx) + abs(y - zy)

    def can_move_safe(
        self, x, y, direction, tail_positions, prev_pos, ignore_oldest_tail_segment=True, new_apple_found=False
    ):
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

        moves_made = 0
        max_moves = self.world_size * self.world_size * 3

        tail_positions_copy = _tail_positions[0:]
        prev_pos = None

        while moves_made < max_moves:
            # Ziel erreicht?
            if x == zx and y == zy:
                return True, path_stack

            my_new_apple_found = False
            if len(path_stack) == 0:
                my_new_apple_found = new_apple_found

            # Finde beste Richtung
            best_direction = None
            best_score = 1000000

            # Randomisiere Richtungen
            ll = len(self.directions)
            for i in range(ll - 1, 0, -1):
                j = int(random() * ll)
                self.directions[i], self.directions[j] = self.directions[j], self.directions[i]

            # Evaluiere alle Richtungen
            for direction in self.directions:
                if self.can_move_safe(x, y, direction, tail_positions_copy, prev_pos, False, my_new_apple_found):
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
            else:
                # Backtracking
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

            moves_made += 1

        return False, path_stack

    def collect_next_apple(self) -> bool:
        """Sammelt den nächsten Apfel"""
        if not self.current_apple:
            self.game_over = True
            return False

        self.change_hat(Hats.Dinosaur_Hat)

        apple_x, apple_y = self.current_apple

        # Finde Pfad zum Apfel
        success, path_stack = self.find_path_astar(
            self.dino_x, self.dino_y, apple_x, apple_y, self.tail_positions, new_apple_found=True
        )

        if not success:
            self.game_over = True
            return False

        self.path_to_apple = path_stack[:]

        # Führe Bewegungen aus
        for step in path_stack:
            moved = self.move(step)
            if not moved:
                self.game_over = True
                return False

        return True


class DinoGameVisualizer:
    """Visualisiert das Dino-Spiel mit matplotlib"""

    def __init__(self, world_size: int = 8):
        self.sim = DinoGameSimulation(world_size)
        self.fig, self.axes = plt.subplots(1, 2, figsize=(14, 7))
        self.ax_game = self.axes[0]
        self.ax_stats = self.axes[1]

        self.setup_plot()
        self.frame_count = 0
        self.animation_speed = 200  # Millisekunden pro Frame

    def setup_plot(self):
        """Richtet die Plots ein"""
        # Spiel-Plot
        self.ax_game.set_xlim(-0.5, self.sim.world_size - 0.5)
        self.ax_game.set_ylim(-0.5, self.sim.world_size - 0.5)
        self.ax_game.set_aspect("equal")
        self.ax_game.grid(True, alpha=0.3)
        self.ax_game.set_title("Dino-Spiel Simulation", fontsize=14, fontweight="bold")
        self.ax_game.set_xlabel("X")
        self.ax_game.set_ylabel("Y")

        # Statistik-Plot
        self.ax_stats.axis("off")

    def draw_game_state(self):
        """Zeichnet den aktuellen Spielzustand"""
        self.ax_game.clear()
        self.ax_game.set_xlim(-0.5, self.sim.world_size - 0.5)
        self.ax_game.set_ylim(-0.5, self.sim.world_size - 0.5)
        self.ax_game.set_aspect("equal")
        self.ax_game.grid(True, alpha=0.3)

        # Zeichne Gitter-Hintergrund
        for i in range(self.sim.world_size):
            for j in range(self.sim.world_size):
                rect = Rectangle((i - 0.5, j - 0.5), 1, 1, facecolor="lightgray", edgecolor="gray", alpha=0.2)
                self.ax_game.add_patch(rect)

        # Zeichne Schwanz
        for i, (tx, ty) in enumerate(self.sim.tail_positions):
            intensity = 0.3 + (i / max(1, len(self.sim.tail_positions))) * 0.7
            tail_rect = Rectangle(
                (tx - 0.4, ty - 0.4), 0.8, 0.8, facecolor="green", alpha=intensity, edgecolor="darkgreen"
            )
            self.ax_game.add_patch(tail_rect)
            self.ax_game.text(
                tx, ty, str(i + 1), ha="center", va="center", fontsize=8, color="white", fontweight="bold"
            )

        # Zeichne Apfel
        if self.sim.current_apple:
            ax, ay = self.sim.current_apple
            apple_circle = Circle((ax, ay), 0.3, facecolor="red", edgecolor="darkred", linewidth=2)
            self.ax_game.add_patch(apple_circle)
            self.ax_game.text(ax, ay - 0.6, "🍎", ha="center", va="center", fontsize=20)

        # Zeichne Pfad zum Apfel (gestrichelt)
        if self.sim.path_to_apple and self.sim.current_apple:
            path_x, path_y = [self.sim.dino_x], [self.sim.dino_y]
            temp_x, temp_y = self.sim.dino_x, self.sim.dino_y
            for direction in self.sim.path_to_apple:
                dx, dy = self.sim.deltas[direction]
                temp_x += dx
                temp_y += dy
                path_x.append(temp_x)
                path_y.append(temp_y)
            self.ax_game.plot(path_x, path_y, "b--", alpha=0.3, linewidth=2)

        # Zeichne Dinosaurier
        dino_color = "gold" if self.sim.current_hat == Hats.Golden_Cactus_Hat else "purple"
        dino_rect = Rectangle(
            (self.sim.dino_x - 0.45, self.sim.dino_y - 0.45),
            0.9,
            0.9,
            facecolor=dino_color,
            edgecolor="black",
            linewidth=3,
        )
        self.ax_game.add_patch(dino_rect)
        self.ax_game.text(self.sim.dino_x, self.sim.dino_y, "🦖", ha="center", va="center", fontsize=24)

        title = f"Dino-Spiel: {self.sim.apples_collected} Äpfel gesammelt"
        if self.sim.game_over:
            title += " - GAME OVER"
        self.ax_game.set_title(title, fontsize=14, fontweight="bold")

    def draw_statistics(self):
        """Zeichnet Statistiken"""
        self.ax_stats.clear()
        self.ax_stats.axis("off")

        stats_text = f"""
STATISTIKEN

Weltgröße: {self.sim.world_size} × {self.sim.world_size}
Äpfel gesammelt: {self.sim.apples_collected}
Schwanzlänge: {len(self.sim.tail_positions)}
Aktuelle Position: ({self.sim.dino_x}, {self.sim.dino_y})

Hut: {self.sim.current_hat.name.replace('_', ' ')}

Max. Äpfel: {self.sim.world_size * self.sim.world_size - 1}
Fortschritt: {self.sim.apples_collected / max(1, self.sim.world_size * self.sim.world_size - 1) * 100:.1f}%

Status: {'🎮 Spiel läuft' if not self.sim.game_over else '🏁 Game Over'}
        """

        self.ax_stats.text(
            0.1,
            0.9,
            stats_text,
            transform=self.ax_stats.transAxes,
            fontsize=12,
            verticalalignment="top",
            fontfamily="sans-serif",
            bbox=dict(boxstyle="round", facecolor="wheat", alpha=0.5),
        )

        # Legende
        legend_text = """
LEGENDE

🦖 = Dinosaurier
🍎 = Apfel
🟩 = Schwanz
--- = Geplanter Pfad

Gold Hut = Schwanz ernten
Lila Hut = Äpfel sammeln
        """

        self.ax_stats.text(
            0.1,
            0.45,
            legend_text,
            transform=self.ax_stats.transAxes,
            fontsize=10,
            verticalalignment="top",
            fontfamily="sans-serif",
            bbox=dict(boxstyle="round", facecolor="lightblue", alpha=0.5),
        )

    def update(self, frame):
        """Wird für jeden Frame der Animation aufgerufen"""
        print(f"UPDATE CALLED: {frame=}")
        if not self.sim.game_over:
            max_apples = self.sim.world_size * self.sim.world_size - 1
            if self.sim.apples_collected < max_apples:
                success = self.sim.collect_next_apple()
                if not success:
                    print(f"Game Over! {self.sim.apples_collected} Äpfel gesammelt.")
            else:
                self.sim.game_over = True
                print(f"Spiel gewonnen! Alle {self.sim.apples_collected} Äpfel gesammelt!")

        self.draw_game_state()
        self.draw_statistics()
        self.frame_count += 1

    def animate(self, frames=100):
        """Startet die Animation"""
        anim = animation.FuncAnimation(
            self.fig, self.update, frames=frames, interval=self.animation_speed, repeat=False
        )
        plt.tight_layout()
        plt.show()
        return anim


def main():
    """Hauptfunktion"""
    print("Starte Dino-Spiel Visualisierung...")
    print("Schließe das Fenster, um die Simulation zu beenden.")

    plt.ioff()
    plt.switch_backend("Agg")

    # Erstelle Visualisierung mit 8×8 Welt
    visualizer = DinoGameVisualizer(world_size=20)

    # Starte Animation (max 100 Frames = ca. 63 Äpfel möglich)
    anim = visualizer.animate(frames=100)
    # GIF speichern (benötigt pillow)
    sv = Path(Path.home(), "Desktop")
    sv = Path(sv, "dino_game.gif")

    anim.save(sv, writer="pillow", fps=1)

    # plt.show(block=True)
    # from IPython.display import HTML
    #
    # HTML(anim.to_jshtml())


if __name__ == "__main__":
    # import matplotlib.font_manager as fm

    # fonts = [f.name for f in fm.fontManager.ttflist]
    # print(sorted(set(fonts)))

    main()
