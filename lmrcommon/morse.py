"""CW station identification: text -> 8 kHz PCM16 Morse tones."""
from __future__ import annotations

from . import audio

CODE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.", "H": "....", "I": "..",
    "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-", "5": ".....", "6": "-....",
    "7": "--...", "8": "---..", "9": "----.", "/": "-..-.", "-": "-....-",
}


def timing(text: str) -> list[tuple[bool, int]]:
    """(key down?, units) sequence using standard spacing: 1 dit, 3 dah/letter gap, 7 word gap."""
    out: list[tuple[bool, int]] = []
    for w, word in enumerate(text.upper().split()):
        if w:
            out.append((False, 7))
        letters = [c for c in word if c in CODE]
        for i, ch in enumerate(letters):
            if i:
                out.append((False, 3))
            for j, sym in enumerate(CODE[ch]):
                if j:
                    out.append((False, 1))
                out.append((True, 1 if sym == "." else 3))
    return out


def to_pcm(text: str, wpm: int = 20, freq: float = 800.0, amp: float = 0.25) -> bytes:
    unit_ms = 1200 // wpm  # PARIS standard
    return audio.tone_sequence([(freq if on else 0, n * unit_ms) for on, n in timing(text)], amp)
