"""Record existing medication dose text normalization without provider calls."""

import argparse
import json
from pathlib import Path

from garmin_ai.intake_assertion import ambiguous_dose, normalize_dose_words, parse_dose


def capture(output):
    lexical = [
        "I took 1,250 milligrams of synthetic item",
        "Я принял 1,250 миллиграммов",
        "I took 1 250 mg",
        "I took 1\u00a0250.5 mg",
        "Я принял 1\u202f250,5 мг",
        "1 250 milligrams",
        "17 225 350 мкг",
        "650µg",
        "650 μg",
        "650 ΜG",
        "I took 4.5 micrograms",
        "я принял 3 микрограмма",
        "I took 4 millilitres",
        "I took 4 milliliters",
        "я принял два миллилитра",
        "I took 4.5 grams",
        "Я принял четыре грамма",
        "I took 2 international units",
        "Я принял две международные единицы",
        "I took one half a tablet",
        "I took half a pill",
        "Я принял половину таблетки",
        "I took two and a half pills",
        "I took 2,5 and a half tablets",
        "I took 1/2 tablet",
        "I took 2 1/2 tablets",
        "I took 1/3 drops",
        "I took 5/0 mg",
        "I took 123456789 1/600 mg",
        "I took three capsules",
        "I took ONE capsule",
        "Я принял лексическое слово восемь капель",
        "I took two synthetic tablets",
        "I took 2 coated synthetic trial tablets",
        "I took one unknown tablet",
        "Я принял одну неизвестную таблетку",
        "I took one or two tablets",
        "I wrote the words one synthetic without a dose",
        "0٢ mg",
        "I took ١,٢٥٠ mg",
        "ʃ Unicode test only",
        "I took ſeven mg",
        "Synthetic record uses 07:00 and 12:15",
    ]
    lexical += [
        f"I took {word} mg"
        for word in "one two three four five six seven eight nine ten an a".split()
    ]
    lexical += [
        f"Я принял {word} мг"
        for word in "один одну два две три четыре пять шесть семь восемь девять десять".split()
    ]
    ranges = [
        "synthetic 2-4 mg",
        "synthetic 2–4mg",
        "synthetic 2—4 mg",
        "synthetic 2 to 4 mg",
        "synthetic 2 or 4 mg",
        "2 до 4 мг",
        "2 либо 4 таблеток",
        "2 или 4 капли",
        "2.5 to 3.5 ml",
        "2,5 до 3,5 г",
        "2/4 ml",
        "2 then 4 mg",
        "3 mg",
        "3 to 4 synthetic",
    ]
    direct = [
        "1mg",
        "2 тАбЛеТок",
        "1 таблетка",
        "5 pills",
        "2 capsules",
        "1 капсула",
        "2 drops",
        "3 капли",
        "2.02 мкг",
        "2,05 МЛ",
        "7 г",
        "101 ме",
        "2 IU",
        "1 ml",
        "٢,٥ мг",
        "1.0 unknown",
        "zero",
    ]
    document = {
        "normalize": [[text, normalize_dose_words(text)] for text in lexical],
        "ranges": [[text, ambiguous_dose(text)] for text in ranges],
        "parse": [[text, parse_dose(text) if text != "zero" else None] for text in direct],
    }
    output.write_text(json.dumps(document, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)


if __name__ == "__main__":
    main()
