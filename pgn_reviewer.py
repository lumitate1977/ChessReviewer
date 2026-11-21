
import io
import os
import math
import shutil
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import chess
    import chess.pgn
    import chess.engine
except ImportError:
    raise SystemExit("Install dependencies: py -m pip install python-chess")


# ============================================================
# CONFIGURATION
# ============================================================

ENGINE_PATH = ""
DEFAULT_DEPTH = 16
DEFAULT_RATING = 1200

PIECE_VALUES = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
    chess.KING: 0,
}

PIECES = {
    "K": "♔", "Q": "♕", "R": "♖",
    "B": "♗", "N": "♘", "P": "♙",
    "k": "♚", "q": "♛", "r": "♜",
    "b": "♝", "n": "♞", "p": "♟",
}

COLORS = {
    "BRILLIANT": "#159447",
    "GREAT": "#13a0a0",
    "BEST": "#239447",
    "EXCELLENT": "#398bd1",
    "GOOD": "#888888",
    "INACCURACY": "#d49b16",
    "MISTAKE": "#e47724",
    "BLUNDER": "#d32f2f",
    "MISS": "#bd55bd",
}


# ============================================================
# STOCKFISH
# ============================================================

def find_engine():
    if ENGINE_PATH and os.path.isfile(ENGINE_PATH):
        return ENGINE_PATH

    found = shutil.which("stockfish")
    if found:
        return found

    candidates = [
        "C:/Program Files/Stockfish/stockfish.exe",
        "C:/stockfish/stockfish.exe",
        os.path.expanduser("~/Downloads/stockfish/stockfish.exe"),
        os.path.expanduser("~/Downloads/stockfish.exe"),
    ]

    for path in candidates:
        if os.path.isfile(path):
            return path

    return ""


def score_cp(score, color):
    score = score.pov(color)

    if score.is_mate():
        mate = score.mate()
        return 100000 if mate and mate > 0 else -100000

    return score.score()


# ============================================================
# EXPECTED POINTS MODEL
# ============================================================

def expected_points_from_cp(cp):
    """
    Convert centipawn evaluation into approximate expected points.

    This is a smooth logistic approximation.
    It is NOT Chess.com's proprietary expected-points formula.
    """

    if cp >= 100000:
        return 1.0

    if cp <= -100000:
        return 0.0

    # Avoid numerical overflow.
    cp = max(-10000, min(10000, cp))

    return 1.0 / (1.0 + math.exp(-cp / 400.0))


def classify_move(
    expected_points_lost,
    brilliant=False,
    great=False,
    miss=False
):
    if brilliant:
        return "BRILLIANT"

    if great:
        return "GREAT"

    if miss:
        return "MISS"

    loss = max(0.0, expected_points_lost)

    if loss < 0.005:
        return "BEST"
    elif loss < 0.02:
        return "EXCELLENT"
    elif loss < 0.05:
        return "GOOD"
    elif loss < 0.10:
        return "INACCURACY"
    elif loss < 0.20:
        return "MISTAKE"
    else:
        return "BLUNDER"


# ============================================================
# RATING AND MATERIAL HELPERS
# ============================================================

def get_rating(game, header, fallback=DEFAULT_RATING):
    try:
        rating = int(game.headers.get(header, fallback))

        if 100 <= rating <= 4000:
            return rating

    except (ValueError, TypeError):
        pass

    return fallback


def material(board, color):
    total = 0

    for piece_type, value in PIECE_VALUES.items():
        total += (
            len(board.pieces(piece_type, color)) * value
        )

    return total


def material_difference(board, color):
    return material(board, color) - material(board, not color)


def is_capture_of_moved_piece(board_after, move):
    """
    Checks whether the opponent can legally capture the piece
    that just moved.

    The opponent does not have to actually play that capture.
    """

    for reply in board_after.legal_moves:
        if reply.to_square != move.to_square:
            continue

        if board_after.is_capture(reply):
            return True

    return False


# ============================================================
# SPECIAL MOVE DETECTION
# ============================================================

def detect_brilliant(
    engine,
    board_before,
    board_after,
    move,
    best_move,
    before_score,
    after_score,
    mover
):
    """
    Conservative Brilliant estimate.

    Looks for a strong non-pawn move that offers a piece for
    capture while retaining a good evaluation.

    This is not Chess.com's official Brilliant algorithm.
    """

    if best_move != move:
        return False

    if board_before.legal_moves.count() <= 1:
        return False

    piece = board_before.piece_at(move.from_square)

    if piece is None or piece.piece_type == chess.PAWN:
        return False

    before_expected = expected_points_from_cp(before_score)
    after_expected = expected_points_from_cp(after_score)

    if before_expected - after_expected > 0.02:
        return False

    # Avoid calling an ordinary move Brilliant when the player
    # already had an overwhelmingly winning position.
    if before_expected > 0.90:
        return False

    if not is_capture_of_moved_piece(board_after, move):
        return False

    # Check the evaluation if the opponent captures the offered piece.
    for reply in board_after.legal_moves:
        if reply.to_square != move.to_square:
            continue

        if not board_after.is_capture(reply):
            continue

        captured_position = board_after.copy()
        captured_position.push(reply)

        info = engine.analyse(
            captured_position,
            chess.engine.Limit(depth=max(10, DEFAULT_DEPTH - 2))
        )

        captured_score = score_cp(info["score"], mover)
        captured_expected = expected_points_from_cp(captured_score)

        # The sacrifice must leave the player with a reasonable position.
        if captured_expected >= 0.40:
            return True

    return False


def detect_great(
    engine,
    board_before,
    move,
    best_move,
    before_expected,
    after_expected,
    mover,
    depth
):
    """
    Heuristic Great detection.

    Looks for a critical position where the played move is the
    engine's best move and significantly improves the position.
    """

    if best_move != move:
        return False

    if board_before.legal_moves.count() <= 1:
        return False

    improvement = after_expected - before_expected

    if improvement < 0.20:
        return False

    # Check whether alternative moves are substantially worse.
    alternatives = []

    for alternative in board_before.legal_moves:
        if alternative == move:
            continue

        test_board = board_before.copy()
        test_board.push(alternative)

        info = engine.analyse(
            test_board,
            chess.engine.Limit(depth=max(8, depth - 2))
        )

        alternative_score = score_cp(info["score"], mover)
        alternatives.append(
            expected_points_from_cp(alternative_score)
        )

    if not alternatives:
        return False

    best_alternative = max(alternatives)

    return after_expected - best_alternative >= 0.10


def detect_miss(
    best_expected,
    played_expected,
    before_expected
):
    """
    Heuristic Miss detection.

    Looks for a missed opportunity to reach a clearly winning
    expected-points position.
    """

    if best_expected >= 0.85 and played_expected < 0.75:
        return True

    # Also detect a large missed opportunity from an equal position.
    if (
        0.35 <= before_expected <= 0.65
        and best_expected >= 0.90
        and played_expected < 0.80
    ):
        return True

    return False


# ============================================================
# CHESSBOARD WIDGET
# ============================================================

class ChessBoard(tk.Canvas):

    def __init__(self, parent, size=480):
        super().__init__(
            parent,
            width=size,
            height=size,
            bg="#202020",
            highlightthickness=0
        )

        self.size = size
        self.square = size / 8
        self.board = chess.Board()
        self.move = None
        self.best_move = None

        self.draw()

    def set_position(self, fen, move=None, best_move=None):
        self.board = chess.Board(fen)
        self.move = move
        self.best_move = best_move
        self.draw()

    def draw(self):
        self.delete("all")

        sq = self.square

        light = "#f0d9b5"
        dark = "#b58863"

        for row in range(8):
            for col in range(8):

                x1 = col * sq
                y1 = row * sq
                x2 = x1 + sq
                y2 = y1 + sq

                square = chess.square(col, 7 - row)

                color = light if (row + col) % 2 == 0 else dark

                if self.move:
                    if square == self.move.from_square:
                        color = "#f6e65c"

                    elif square == self.move.to_square:
                        color = "#e5c842"

                self.create_rectangle(
                    x1, y1, x2, y2,
                    fill=color,
                    outline=""
                )

                piece = self.board.piece_at(square)

                if piece:
                    symbol = PIECES[piece.symbol()]

                    self.create_text(
                        x1 + sq / 2,
                        y1 + sq / 2,
                        text=symbol,
                        font=("Segoe UI Symbol", int(sq * 0.72)),
                        fill="#fffdf4" if piece.color else "#171717"
                    )

                if col == 0:
                    self.create_text(
                        x1 + 5,
                        y1 + 5,
                        anchor="nw",
                        text=str(8 - row),
                        font=("Arial", 9, "bold"),
                        fill="#333333"
                    )

                if row == 7:
                    self.create_text(
                        x2 - 5,
                        y2 - 5,
                        anchor="se",
                        text=chess.FILE_NAMES[col],
                        font=("Arial", 9, "bold"),
                        fill="#333333"
                    )

        # Blue arrow = Stockfish's recommended move.
        if self.best_move:
            start = self.best_move.from_square
            end = self.best_move.to_square

            x1 = (
                chess.square_file(start) + 0.5
            ) * sq

            y1 = (
                7 - chess.square_rank(start) + 0.5
            ) * sq

            x2 = (
                chess.square_file(end) + 0.5
            ) * sq

            y2 = (
                7 - chess.square_rank(end) + 0.5
            ) * sq

            self.create_line(
                x1, y1, x2, y2,
                fill="#1688ff",
                width=6,
                arrow=tk.LAST,
                arrowshape=(16, 20, 7)
            )


# ============================================================
# MAIN APPLICATION
# ============================================================

class PGNReviewer:

    def __init__(self, root):
        self.root = root

        self.root.title("PGN Chess Reviewer")
        self.root.geometry("1500x920")
        self.root.minsize(1100, 720)

        self.engine_path = find_engine()
        self.busy = False
        self.rows = []
        self.last_report = None

        self.build_ui()

    # --------------------------------------------------------
    # USER INTERFACE
    # --------------------------------------------------------

    def build_ui(self):

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="PGN Chess Reviewer",
            font=("Segoe UI", 21, "bold")
        ).pack(anchor="w")

        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x", pady=10)

        ttk.Button(
            toolbar,
            text="Choose Stockfish",
            command=self.choose_engine
        ).pack(side="left")

        ttk.Label(
            toolbar,
            text="Depth:"
        ).pack(side="left", padx=(15, 4))

        self.depth = tk.IntVar(value=DEFAULT_DEPTH)

        ttk.Spinbox(
            toolbar,
            from_=8,
            to=30,
            width=5,
            textvariable=self.depth
        ).pack(side="left")

        ttk.Button(
            toolbar,
            text="Load PGN",
            command=self.load_pgn
        ).pack(side="right")

        self.engine_label = ttk.Label(
            outer,
            text="Stockfish: " + (
                self.engine_path if self.engine_path
                else "Not selected"
            )
        )

        self.engine_label.pack(anchor="w")

        ttk.Label(
            outer,
            text="Paste your PGN:",
            font=("Segoe UI", 10, "bold")
        ).pack(anchor="w", pady=(8, 3))

        self.pgn_text = tk.Text(
            outer,
            height=7,
            font=("Consolas", 10),
            wrap="word"
        )

        self.pgn_text.pack(fill="x")

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=8)

        self.review_button = ttk.Button(
            actions,
            text="Review Game",
            command=self.start_review
        )

        self.review_button.pack(side="left")

        ttk.Button(
            actions,
            text="Clear",
            command=lambda: self.pgn_text.delete("1.0", "end")
        ).pack(side="left", padx=5)

        ttk.Button(
            actions,
            text="Save Report",
            command=self.save_report
        ).pack(side="right")

        self.progress = ttk.Progressbar(
            outer,
            mode="determinate"
        )

        self.progress.pack(fill="x", pady=5)

        self.status = tk.StringVar(
            value="Paste a PGN and click Review Game."
        )

        ttk.Label(
            outer,
            textvariable=self.status
        ).pack(anchor="w")

        main = ttk.PanedWindow(
            outer,
            orient="horizontal"
        )

        main.pack(fill="both", expand=True, pady=10)

        board_frame = ttk.Frame(main, padding=5)
        moves_frame = ttk.Frame(main, padding=5)

        main.add(board_frame, weight=1)
        main.add(moves_frame, weight=2)

        ttk.Label(
            board_frame,
            text="Position",
            font=("Segoe UI", 13, "bold")
        ).pack(anchor="w", pady=5)

        self.board = ChessBoard(board_frame, 480)
        self.board.pack(anchor="center")

        self.position_label = tk.StringVar(
            value="Select a move to see its position."
        )

        ttk.Label(
            board_frame,
            textvariable=self.position_label,
            wraplength=460
        ).pack(anchor="w", pady=8)

        self.recommendation = tk.StringVar(value="")

        ttk.Label(
            board_frame,
            textvariable=self.recommendation,
            wraplength=460,
            font=("Segoe UI", 10, "bold")
        ).pack(anchor="w")

        ttk.Label(
            moves_frame,
            text="Move-by-move review",
            font=("Segoe UI", 13, "bold")
        ).pack(anchor="w", pady=5)

        columns = (
            "move",
            "side",
            "played",
            "classification",
            "eval",
            "loss",
            "best"
        )

        self.results = ttk.Treeview(
            moves_frame,
            columns=columns,
            show="headings",
            height=22
        )

        headings = {
            "move": "Move",
            "side": "Player",
            "played": "Played",
            "classification": "Review",
            "eval": "Eval",
            "loss": "EP lost",
            "best": "Best move"
        }

        widths = {
            "move": 75,
            "side": 75,
            "played": 75,
            "classification": 110,
            "eval": 75,
            "loss": 75,
            "best": 85
        }

        for col in columns:
            self.results.heading(
                col,
                text=headings[col]
            )

            self.results.column(
                col,
                width=widths[col],
                anchor="center"
            )

        scrollbar = ttk.Scrollbar(
            moves_frame,
            orient="vertical",
            command=self.results.yview
        )

        self.results.configure(
            yscrollcommand=scrollbar.set
        )

        self.results.pack(
            side="left",
            fill="both",
            expand=True
        )

        scrollbar.pack(
            side="right",
            fill="y"
        )

        for label, color in COLORS.items():
            self.results.tag_configure(
                label,
                foreground=color
            )

        self.results.bind(
            "<<TreeviewSelect>>",
            self.select_move
        )

        self.summary = tk.StringVar(value="")

        ttk.Label(
            outer,
            textvariable=self.summary,
            font=("Segoe UI", 10, "bold"),
            wraplength=1350
        ).pack(anchor="w")

        ttk.Label(
            outer,
            text=(
                "Chess.com-style estimates. "
                "The expected-points formula and special-move "
                "detection are approximations."
            ),
            foreground="#777777"
        ).pack(anchor="w", pady=4)

    # --------------------------------------------------------
    # FILE AND ENGINE CONTROLS
    # --------------------------------------------------------

    def choose_engine(self):

        path = filedialog.askopenfilename(
            title="Select Stockfish",
            filetypes=[
                ("Stockfish executable", "*.exe"),
                ("All files", "*.*")
            ]
        )

        if path:
            self.engine_path = path

            self.engine_label.config(
                text="Stockfish: " + path
            )

    def load_pgn(self):

        path = filedialog.askopenfilename(
            filetypes=[("PGN files", "*.pgn")]
        )

        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                self.pgn_text.delete("1.0", "end")
                self.pgn_text.insert("1.0", f.read())

        except Exception as e:
            messagebox.showerror("Error", str(e))

    # --------------------------------------------------------
    # START REVIEW
    # --------------------------------------------------------

    def start_review(self):

        if self.busy:
            return

        pgn_data = self.pgn_text.get(
            "1.0",
            "end"
        ).strip()

        if not pgn_data:
            messagebox.showwarning(
                "Missing PGN",
                "Paste a PGN first."
            )
            return

        if (
            not self.engine_path
            or not os.path.isfile(self.engine_path)
        ):
            messagebox.showwarning(
                "Stockfish missing",
                "Download Stockfish and select stockfish.exe."
            )
            return

        try:
            depth = int(self.depth.get())

            if not 8 <= depth <= 30:
                raise ValueError

        except ValueError:
            messagebox.showerror(
                "Invalid depth",
                "Use a depth from 8 to 30."
            )
            return

        self.busy = True
        self.review_button.config(state="disabled")

        self.results.delete(
            *self.results.get_children()
        )

        self.summary.set("")
        self.progress["value"] = 0

        self.rows = []
        self.last_report = None

        threading.Thread(
            target=self.review_worker,
            args=(pgn_data, depth),
            daemon=True
        ).start()

    # --------------------------------------------------------
    # ANALYSIS WORKER
    # --------------------------------------------------------

    def review_worker(self, pgn_data, depth):

        engine = None

        try:
            stream = io.StringIO(pgn_data)
            games = []

            while True:
                game = chess.pgn.read_game(stream)

                if game is None:
                    break

                if game.errors:
                    raise ValueError(
                        "PGN errors: "
                        + "; ".join(map(str, game.errors))
                    )

                games.append(game)

            if not games:
                raise ValueError(
                    "No valid games found in the PGN."
                )

            engine = chess.engine.SimpleEngine.popen_uci(
                self.engine_path
            )

            rows = []
            counts = {}

            total = sum(
                len(list(game.mainline_moves()))
                for game in games
            )

            completed = 0

            for game_number, game in enumerate(games, 1):

                board = game.board()
                moves = list(game.mainline_moves())

                white_rating = get_rating(
                    game,
                    "WhiteElo"
                )

                black_rating = get_rating(
                    game,
                    "BlackElo"
                )

                for ply, move in enumerate(moves):

                    color = board.turn
                    move_number = board.fullmove_number

                    player = (
                        "White" if color else "Black"
                    )

                    player_rating = (
                        white_rating if color
                        else black_rating
                    )

                    san = board.san(move)
                    fen_before = board.fen()

                    # Position before the move.
                    before_info = engine.analyse(
                        board,
                        chess.engine.Limit(depth=depth)
                    )

                    before_score = score_cp(
                        before_info["score"],
                        color
                    )

                    best_move = before_info.get(
                        "pv",
                        [None]
                    )[0]

                    forced = (
                        board.legal_moves.count() == 1
                    )

                    # Position after the played move.
                    after = board.copy()
                    after.push(move)

                    after_info = engine.analyse(
                        after,
                        chess.engine.Limit(depth=depth)
                    )

                    after_score = score_cp(
                        after_info["score"],
                        color
                    )

                    # Expected points calculation.
                    before_expected = (
                        expected_points_from_cp(
                            before_score
                        )
                    )

                    after_expected = (
                        expected_points_from_cp(
                            after_score
                        )
                    )

                    expected_points_lost = max(
                        0.0,
                        before_expected - after_expected
                    )

                    # Evaluate the best alternative position.
                    best_expected = after_expected

                    if best_move:
                        best_board = board.copy()
                        best_board.push(best_move)

                        best_info = engine.analyse(
                            best_board,
                            chess.engine.Limit(
                                depth=max(8, depth - 2)
                            )
                        )

                        best_score = score_cp(
                            best_info["score"],
                            color
                        )

                        best_expected = (
                            expected_points_from_cp(
                                best_score
                            )
                        )

                    # Special classifications.
                    brilliant = False
                    great = False
                    miss = False

                    if not forced:

                        brilliant = detect_brilliant(
                            engine,
                            board,
                            after,
                            move,
                            best_move,
                            before_score,
                            after_score,
                            color
                        )

                        if not brilliant:
                            great = detect_great(
                                engine,
                                board,
                                move,
                                best_move,
                                before_expected,
                                after_expected,
                                color,
                                depth
                            )

                        if not brilliant and not great:
                            miss = detect_miss(
                                best_expected,
                                after_expected,
                                before_expected
                            )

                    label = classify_move(
                        expected_points_lost,
                        brilliant=brilliant,
                        great=great,
                        miss=miss
                    )

                    # SAN for the best move.
                    best_san = "-"

                    if (
                        best_move
                        and best_move in board.legal_moves
                    ):
                        best_san = board.san(best_move)

                    # Evaluation display.
                    if abs(after_score) >= 100000:
                        eval_text = "Mate"
                    else:
                        eval_text = (
                            f"{after_score / 100:+.2f}"
                        )

                    loss_text = (
                        f"{expected_points_lost:.3f}"
                    )

                    rows.append({
                        "move_number": move_number,
                        "player": player,
                        "rating": player_rating,
                        "san": san,
                        "label": label,
                        "eval": eval_text,
                        "loss": loss_text,
                        "game": game_number,
                        "fen": fen_before,
                        "move_uci": move.uci(),
                        "best_uci": (
                            best_move.uci()
                            if best_move else None
                        ),
                        "best_san": best_san,
                        "before_expected": before_expected,
                        "after_expected": after_expected,
                        "best_expected": best_expected,
                    })

                    counts[label] = (
                        counts.get(label, 0) + 1
                    )

                    board.push(move)
                    completed += 1

                    self.root.after(
                        0,
                        self.update_progress,
                        completed,
                        total,
                        game_number
                    )

            engine.quit()
            engine = None

            self.root.after(
                0,
                self.show_results,
                rows,
                counts,
                len(games)
            )

        except Exception as e:

            if engine:
                try:
                    engine.quit()
                except Exception:
                    pass

            self.root.after(
                0,
                self.review_failed,
                str(e)
            )

    # --------------------------------------------------------
    # PROGRESS
    # --------------------------------------------------------

    def update_progress(
        self,
        completed,
        total,
        game_number
    ):

        self.progress["maximum"] = max(total, 1)
        self.progress["value"] = completed

        self.status.set(
            f"Analyzing {completed}/{total} moves "
            f"| Game {game_number}"
        )

    # --------------------------------------------------------
    # DISPLAY RESULTS
    # --------------------------------------------------------

    def show_results(
        self,
        rows,
        counts,
        game_count
    ):

        self.rows = rows

        for index, row in enumerate(rows):

            move_label = (
                f"G{row['game']} {row['move_number']}"
            )

            if row["player"] == "Black":
                move_label += "..."

            self.results.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    move_label,
                    row["player"],
                    row["san"],
                    row["label"],
                    row["eval"],
                    row["loss"],
                    row["best_san"]
                ),
                tags=(row["label"],)
            )

        order = [
            "BRILLIANT",
            "GREAT",
            "BEST",
            "EXCELLENT",
            "GOOD",
            "INACCURACY",
            "MISTAKE",
            "BLUNDER",
            "MISS"
        ]

        summary = " | ".join(
            f"{name}: {counts.get(name, 0)}"
            for name in order
            if counts.get(name, 0)
        )

        self.summary.set(
            f"{len(rows)} moves reviewed across "
            f"{game_count} game(s). "
            + summary
        )

        self.status.set("Review complete.")

        self.review_button.config(state="normal")
        self.busy = False

        self.last_report = (
            rows,
            counts,
            game_count
        )

        if rows:
            self.results.selection_set("0")
            self.results.focus("0")
            self.select_move()

    # --------------------------------------------------------
    # SELECT MOVE
    # --------------------------------------------------------

    def select_move(self, event=None):

        selection = self.results.selection()

        if not selection:
            return

        index = int(selection[0])

        if index >= len(self.rows):
            return

        row = self.rows[index]

        self.board.set_position(
            row["fen"],
            chess.Move.from_uci(
                row["move_uci"]
            ),
            chess.Move.from_uci(
                row["best_uci"]
            ) if row["best_uci"] else None
        )

        self.position_label.set(
            f"Game {row['game']} | "
            f"{row['player']} | "
            f"Move {row['move_number']}"
        )

        self.recommendation.set(
            f"Played: {row['san']}\n"
            f"Stockfish best: {row['best_san']}\n"
            f"Classification: {row['label']}\n"
            f"Player rating: {row['rating']}\n"
            f"Expected points before: "
            f"{row['before_expected']:.3f}\n"
            f"Expected points after: "
            f"{row['after_expected']:.3f}\n"
            f"Expected points lost: "
            f"{row['loss']}\n"
            f"Best move expected points: "
            f"{row['best_expected']:.3f}\n\n"
            f"Blue arrow = Stockfish's recommended move."
        )

    # --------------------------------------------------------
    # ERROR HANDLING
    # --------------------------------------------------------

    def review_failed(self, error):

        self.busy = False

        self.review_button.config(
            state="normal"
        )

        self.status.set("Review failed.")

        messagebox.showerror(
            "Review failed",
            error
        )

    # --------------------------------------------------------
    # SAVE REPORT
    # --------------------------------------------------------

    def save_report(self):

        if not self.last_report:
            messagebox.showinfo(
                "No report",
                "Review a game first."
            )
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[
                ("Text report", "*.txt")
            ]
        )

        if not path:
            return

        rows, counts, game_count = self.last_report

        with open(
            path,
            "w",
            encoding="utf-8"
        ) as f:

            f.write(
                f"PGN Chess Review - {game_count} games\n"
            )

            f.write("=" * 80 + "\n\n")

            f.write(
                "Classification method: "
                "Chess.com-style expected-points approximation\n\n"
            )

            for row in rows:

                f.write(
                    f"Game {row['game']} | "
                    f"Move {row['move_number']} | "
                    f"{row['player']} | "
                    f"Rating: {row['rating']} | "
                    f"{row['san']} | "
                    f"{row['label']} | "
                    f"Eval: {row['eval']} | "
                    f"EP lost: {row['loss']} | "
                    f"Best: {row['best_san']}\n"
                )

            f.write("\nSUMMARY\n")
            f.write("=" * 40 + "\n")

            for label, count in sorted(counts.items()):
                f.write(
                    f"{label}: {count}\n"
                )

        messagebox.showinfo(
            "Saved",
            "Report saved successfully."
        )


# ============================================================
# START APPLICATION
# ============================================================

if __name__ == "__main__":

    root = tk.Tk()

    app = PGNReviewer(root)

    root.mainloop()
