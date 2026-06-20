from __future__ import annotations

from typing import Literal

REGIONS: dict[str, list[float]] = {
    "top": [0.08, 0.02, 0.92, 0.34],
    "list_rows": [0.05, 0.17, 0.95, 0.92],
    "detail_card": [0.04, 0.38, 0.96, 0.98],
    "cp": [0.18, 0.02, 0.66, 0.11],
    "name": [0.14, 0.32, 0.86, 0.43],
    "hp": [0.22, 0.39, 0.78, 0.48],
    "hp_bar": [0.18, 0.385, 0.82, 0.425],
    "hp_text": [0.18, 0.415, 0.82, 0.49],
    "tag": [0.06, 0.44, 0.94, 0.55],
    "weight": [0.07, 0.535, 0.36, 0.585],
    "height": [0.70, 0.535, 0.96, 0.61],
    "pokemon_art": [0.10, 0.08, 0.90, 0.37],
    # Default fallback regions; final move evidence uses HP-bar/tab-anchored boxes.
    "moves": [0.05, 0.63, 0.95, 0.99],
    "moves_tabs": [0.18, 0.765, 0.82, 0.82],
    "moves_fast_row": [0.05, 0.80, 0.95, 0.88],
    "moves_charged_rows": [0.05, 0.88, 0.95, 0.99],
    "moves_complete_rows": [0.05, 0.87, 0.95, 0.99],
    "moves_completion_footer": [0.05, 0.84, 0.95, 0.98],
    "moves_transition_guard": [0.05, 0.62, 0.45, 0.88],
    "story": [0.03, 0.74, 0.97, 0.98],
    "appraisal_badge": [0.00, 0.53, 0.43, 0.84],
    "iv_panel": [0.08, 0.74, 0.56, 0.90],
    "transition_edges": [0.00, 0.32, 1.00, 0.78],
    "horizontal_swipe_card": [0.00, 0.38, 1.00, 0.95],
    "sequence_motion": [0.10, 0.28, 0.90, 0.48],
    "special_sections": [0.00, 0.58, 1.00, 0.99],
}
INITIAL_APPRAISAL_CP_REGIONS: tuple[list[float], ...] = (
    [0.30, 0.045, 0.64, 0.095],
    [0.28, 0.045, 0.66, 0.105],
)
INITIAL_APPRAISAL_HP_REGIONS: tuple[list[float], ...] = (
    [0.35, 0.435, 0.70, 0.475],
    [0.32, 0.425, 0.73, 0.485],
)
IV_BAR_WINDOWS = {
    "attack": (0.02, 0.20),
    "defense": (0.25, 0.48),
    "stamina": (0.50, 0.73),
}
LOWER_IV_BAR_WINDOWS = {
    "attack": (0.12, 0.35),
    "defense": (0.35, 0.58),
    "stamina": (0.58, 0.84),
}
IV_STAR_ZONES = (
    (0.12, 0.46, 0.30, 0.62),
    (0.26, 0.40, 0.45, 0.55),
    (0.40, 0.35, 0.60, 0.50),
)
IV_AMBER_STAR_RATIO_MIN = 0.24
IV_RED_STAR_RATIO_MIN = 0.08
IV_INACTIVE_STAR_GRAY_RATIO_MIN = 0.08
NUM_TRANSLATION = str.maketrans(
    {
        "O": "0",
        "o": "0",
        "I": "1",
        "l": "1",
        "|": "1",
        "L": "1",
        "S": "5",
        "s": "5",
        "T": "4",
        "t": "4",
        "B": "8",
    }
)
IV_INCOMPLETE_NOTE = "IV evidence was present but not complete."
WEIGHT_PROPAGATED_NOTE = (
    "Weight was propagated from source-local same-HP detail frames."
)
WEIGHT_CORRECTED_NOTE = (
    "Weight was corrected from dominant source-local same-HP detail evidence."
)
type SignalValue = float | int | bool
DYNAMAX_KEYWORDS = ("dynamax", "max move", "max guard", "max spirit")
GIGANTAMAX_KEYWORDS = ("gigantamax", "g max", "gmax")
APPRAISAL_MOVE_STORY_MARKERS = (
    "gyms",
    "raids",
    "trainer battles",
    "weather bonus",
)
POWER_SECTION_CONTEXT_KEYWORDS = (
    "stardust",
    "candy",
    "power up",
    "evolve",
    "mega energy",
    "gyms",
    "raids",
    "trainer battles",
    "weather bonus",
    "new attack",
    "max move",
)
type DetailLayoutMode = Literal["initial_appraisal_overlay", "scrollable_detail"]
