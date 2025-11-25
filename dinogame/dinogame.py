from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.animation as animation
from matplotlib.patches import Rectangle, Circle
import numpy as np
from enum import Enum, auto
from random import random
from typing import Tuple, List, Optional

import warnings

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

        # Neu: Für Step-by-Step Visualisierung
        self.planning_steps = []  # Speichert jeden Planungsschritt
        self.current_planning_step = 0
        self.is_planning = False
        self.execution_steps = []  # Speichert jeden Ausführungsschritt
        self.current_execution_step = 0
        self.is_executing = False

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

        # Neu: Liste für Visualisierung der Planungsschritte
        self.planning_steps = []

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

                # Neu: Speichere diesen Planungsschritt
                self.planning_steps.append(
                    {
                        "type": "forward",
                        "position": (x, y),
                        "from": (current_x, current_y),
                        "path": path_stack[:],
                        "tail": tail_positions_copy[:],
                    }
                )
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

                # Neu: Speichere Backtracking-Schritt
                self.planning_steps.append(
                    {
                        "type": "backtrack",
                        "position": (x, y),
                        "from": (current_x, current_y),
                        "path": path_stack[:],
                        "tail": tail_positions_copy[:],
                    }
                )

            moves_made += 1

        return False, path_stack

    def collect_next_apple(self) -> bool:
        """Sammelt den nächsten Apfel"""
        if not self.current_apple:
            self.game_over = True
            return False

        self.change_hat(Hats.Dinosaur_Hat)

        apple_x, apple_y = self.current_apple
        # Speichere das Ziel für die Visualisierung
        self.target_apple = self.current_apple

        # Finde Pfad zum Apfel
        success, path_stack = self.find_path_astar(
            self.dino_x, self.dino_y, apple_x, apple_y, self.tail_positions, new_apple_found=True
        )

        if not success:
            self.game_over = True
            return False

        self.path_to_apple = path_stack[:]

        # Neu: Bereite Ausführungsschritte vor
        self.execution_steps = []
        temp_x, temp_y = self.dino_x, self.dino_y
        temp_tail = self.tail_positions[:]

        for step in path_stack:
            dx, dy = self.deltas[step]
            new_x = temp_x + dx
            new_y = temp_y + dy

            # Prüfe ob Apfel erreicht wird
            apple_collected = (new_x, new_y) == self.current_apple

            self.execution_steps.append(
                {
                    "direction": step,
                    "from": (temp_x, temp_y),
                    "to": (new_x, new_y),
                    "tail_before": temp_tail[:],
                    "apple_collected": apple_collected,
                }
            )

            # Simuliere die Bewegung für nächsten Schritt
            if apple_collected:
                temp_tail.append((temp_x, temp_y))
            elif temp_tail:
                temp_tail = temp_tail[1:] + [(temp_x, temp_y)]

            temp_x, temp_y = new_x, new_y

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
        self.animation_speed = 500  # Millisekunden pro Frame (langsamer für bessere Visualisierung)

        # Neu: Verwaltung der Animationsphasen
        self.current_phase = "idle"  # 'idle', 'planning', 'executing'
        self.planning_index = 0
        self.execution_index = 0

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

    def draw_game_state(self, planning_state=None):
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

        # Verwende entweder den Planungszustand oder den aktuellen Zustand
        if planning_state:
            tail_positions = planning_state["tail"]
            dino_x, dino_y = planning_state["position"]
            # Während Planung: Zeige den aktuellen Planungspfad
            path_to_show = planning_state["path"]
            is_planning = True
            # Zeige das Ziel-Apfel während der Planung
            apple_pos = self.sim.target_apple
        else:
            tail_positions = self.sim.tail_positions
            dino_x, dino_y = self.sim.dino_x, self.sim.dino_y
            # Während Ausführung: Zeige verbleibenden Pfad
            if self.current_phase == "executing" and self.execution_index < len(self.sim.execution_steps):
                path_to_show = self.sim.path_to_apple[self.execution_index :]
            else:
                path_to_show = []
            is_planning = False
            apple_pos = self.sim.current_apple

        # Zeichne Schwanz
        for i, (tx, ty) in enumerate(tail_positions):
            intensity = 0.3 + (i / max(1, len(tail_positions))) * 0.7
            tail_rect = Rectangle(
                (tx - 0.4, ty - 0.4), 0.8, 0.8, facecolor="green", alpha=intensity, edgecolor="darkgreen"
            )
            self.ax_game.add_patch(tail_rect)
            self.ax_game.text(
                tx, ty, str(i + 1), ha="center", va="center", fontsize=8, color="white", fontweight="bold"
            )

        # Zeichne Apfel
        if apple_pos:
            ax, ay = apple_pos
            apple_circle = Circle((ax, ay), 0.3, facecolor="red", edgecolor="darkred", linewidth=2)
            self.ax_game.add_patch(apple_circle)
            self.ax_game.text(ax, ay - 0.6, "🍎", ha="center", va="center", fontsize=20)

        # Zeichne geplanten Pfad - NUR wenn es einen Pfad gibt
        if path_to_show and len(path_to_show) > 0:
            path_x, path_y = [dino_x], [dino_y]
            temp_x, temp_y = dino_x, dino_y

            for direction in path_to_show:
                dx, dy = self.sim.deltas[direction]
                temp_x += dx
                temp_y += dy
                path_x.append(temp_x)
                path_y.append(temp_y)

            # Dickere, besser sichtbare Linie
            self.ax_game.plot(path_x, path_y, "b-", alpha=0.6, linewidth=3, marker="o", markersize=4, label="Pfad")

            # Markiere Start und Ziel des Pfads
            if len(path_x) > 1:
                # Startpunkt
                self.ax_game.plot(path_x[0], path_y[0], "go", markersize=8, alpha=0.7)
                # Zielpunkt
                self.ax_game.plot(path_x[-1], path_y[-1], "r*", markersize=15, alpha=0.7)

        # Zeichne Dinosaurier
        dino_color = "gold" if self.sim.current_hat == Hats.Golden_Cactus_Hat else "purple"

        if is_planning:
            # Während Planung: Zeige "virtuellen" Dino transparent
            dino_alpha = 0.4
            # Zeige auch den echten Dino (aber noch transparenter)
            real_dino_rect = Rectangle(
                (self.sim.dino_x - 0.45, self.sim.dino_y - 0.45),
                0.9,
                0.9,
                facecolor=dino_color,
                edgecolor="black",
                linewidth=2,
                alpha=0.2,
                linestyle="--",
            )
            self.ax_game.add_patch(real_dino_rect)
        else:
            dino_alpha = 1.0

        dino_rect = Rectangle(
            (dino_x - 0.45, dino_y - 0.45),
            0.9,
            0.9,
            facecolor=dino_color,
            edgecolor="black",
            linewidth=3,
            alpha=dino_alpha,
        )
        self.ax_game.add_patch(dino_rect)
        self.ax_game.text(dino_x, dino_y, "🦖", ha="center", va="center", fontsize=24, alpha=dino_alpha)

        # Titel anpassen
        title = f"Dino-Spiel: {self.sim.apples_collected} Äpfel gesammelt"
        if is_planning:
            title += " 🧠 PLANUNG"
        elif self.current_phase == "executing":
            title += " 🏃 AUSFÜHRUNG"
        if self.sim.game_over:
            title += " - 🏁 GAME OVER"
        self.ax_game.set_title(title, fontsize=14, fontweight="bold")

    def draw_statistics(self):
        """Zeichnet Statistiken"""
        self.ax_stats.clear()
        self.ax_stats.axis("off")

        phase_text = {
            "idle": "Bereit",
            "planning": f"Planung: Schritt {self.planning_index + 1}/{len(self.sim.planning_steps)}",
            "executing": f"Ausführung: Schritt {self.execution_index + 1}/{len(self.sim.execution_steps)}",
        }

        stats_text = f"""
STATISTIKEN

Weltgröße: {self.sim.world_size} × {self.sim.world_size}
Äpfel gesammelt: {self.sim.apples_collected}
Schwanzlänge: {len(self.sim.tail_positions)}
Aktuelle Position: ({self.sim.dino_x}, {self.sim.dino_y})

Hut: {self.sim.current_hat.name.replace('_', ' ')}

Phase: {phase_text.get(self.current_phase, 'Unbekannt')}

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
  (transparent = Planung)
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
        print(f"UPDATE CALLED: frame={frame}, phase={self.current_phase}")

        if self.sim.game_over:
            self.draw_game_state()
            self.draw_statistics()
            return

        # Phase 1: Neue Planung starten
        if self.current_phase == "idle":
            max_apples = self.sim.world_size * self.sim.world_size - 1
            if self.sim.apples_collected < max_apples:
                success = self.sim.collect_next_apple()
                if not success:
                    print(f"Game Over! {self.sim.apples_collected} Äpfel gesammelt.")
                    self.sim.game_over = True
                else:
                    self.current_phase = "planning"
                    self.planning_index = 0
                    self.execution_index = 0
            else:
                self.sim.game_over = True
                print(f"Spiel gewonnen! Alle {self.sim.apples_collected} Äpfel gesammelt!")

        # Phase 2: Planungsschritte visualisieren
        elif self.current_phase == "planning":
            if self.planning_index < len(self.sim.planning_steps):
                planning_state = self.sim.planning_steps[self.planning_index]
                self.draw_game_state(planning_state)
                self.draw_statistics()
                self.planning_index += 1
                return
            else:
                # Planung abgeschlossen, zur Ausführung wechseln
                self.current_phase = "executing"
                self.execution_index = 0

        # Phase 3: Ausführungsschritte visualisieren
        elif self.current_phase == "executing":
            if self.execution_index < len(self.sim.execution_steps):
                step = self.sim.execution_steps[self.execution_index]

                # Führe den tatsächlichen Move aus
                self.sim.move(step["direction"])

                self.draw_game_state()
                self.draw_statistics()
                self.execution_index += 1
                return
            else:
                # Ausführung abgeschlossen, zurück zu idle
                self.current_phase = "idle"
                # Lösche den Pfad nach Abschluss
                self.sim.path_to_apple = []
                self.sim.target_apple = None

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
