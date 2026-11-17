"""Record existing ownership and product-name decisions with synthetic clauses."""

import argparse
import json
from pathlib import Path

from garmin_ai.intake_assertion import literal_names, name_matches, owner_assertion


def capture(output):
    clauses = [
        "I took Sample-A at 07:30",
        "We took Sample-A today",
        "I have taken 2 mg of Sample-A",
        "She took Sample-A",
        "My sister took Sample-A",
        "Alex took Sample-A",
        "Alex told me I took Sample-A",
        "I never took Sample-A",
        "I have not taken Sample-A",
        "I hadn't taken Sample-A",
        "Did I take Sample-A",
        "Have I taken Sample-A",
        "When I took Sample-A",
        "I took zero pills",
        "I took 0.0 tablets",
        "I took 0 Sample-A pills",
        "I took Sample-A with my mother",
        "I took Sample-A without her",
        "I took Sample-A because the doctor asked",
        "I took Sample-A, I don't remember the dose",
        "I took it again at 10:40",
        "I took it personally",
        "we took them now",
        "I took it easy",
        "I took a blood pressure reading",
        "I took the tram",
        "I took a picture",
        "I took care",
        "I took a break",
        "after breakfast I took Sample-B",
        "2 hours ago I took Sample-B",
        "07:30 took Sample-B",
        "2026-11-10 I took Sample-A",
        "yes, I took Sample-A",
        "took Sample-A",
        "Я принял образец в 07:30",
        "Я принял образец с водой в 08:00",
        "Я принял образец с едой",
        'I took "High Dose" and B-12',
        "Я принял «Тест доза» и B-15",
        "Мы приняли образец вчера",
        "Вчера принимала образец",
        "Она приняла образец",
        "Моя сестра приняла образец",
        "Ирина приняла образец",
        "Я не принимал образец",
        "Я приняла капсулу и не помню дозу",
        "Я принял его снова",
        "Я принял его всерьёз",
        "Я выпил чай",
        "Я выпил препарат",
        "Я проглотил капсулу",
        "Я съел капсулу",
        "Я съел яблоко",
        "Таблетку приняла Ирина",
        "Принял Ирина капсулу",
        "Принял «Образец Плюс»",
        "Принял белую форму",
        "Принял образец форте",
        "После обеда я принял образец",
        "Час назад принял образец",
        "через двадцать минут принял образец",
        "через два часа принял образец",
        "Принял образец от головной боли в 08:00",
        'I took "Trial and Sample" 5 mg at 10:00',
        "I took Sample-A or Sample-B",
        "I took Sample-A and Sample-B",
        "Я принял образец и вторую форму",
        "I took 2 pills of Sample-A",
        "I took unknown medication",
        "Я принял какую-то таблетку",
        "I took both Sample-A and Sample-B, dose 2 mg",
        "I took Sample-A just now with water",
        "I took Sample-A at 07:00 about two hours after food",
        "I took Sample-A on an empty stomach",
        "Я принял образец натощак",
        "I took Sample-A around 6 mg",
        "Я принял образец примерно 5 мг, дозу не помню",
        "I took Sample-A and Sample-A",
        'I took "quotedmedication" and «Trial»',
        "I took Sample/Code",
        "I took Mock’s",
        "I took 5 Sample-A, dose unknown",
        "I took the same medication again",
        "I took",
        "Reminder for tomorrow",
    ]
    aliases = [
        ["Образец", "образца"],
        ["Образец", "образцу"],
        ["Образец", "образцом"],
        ["Образец", "образце"],
        ["Образец", "образцы"],
        ["Проба", "пробу"],
        ["Проба", "пробы"],
        ["Проба", "пробе"],
        ["Проба", "пробой"],
        ["Форма синяя", "форму синюю"],
        ["Красная форма", "красной формы"],
        ["Красная форма", "красною формой"],
        ["Тест 12", "тест-12"],
        ["Trial-12", "TRIAL 12"],
        ["Trial-A", "Trial A"],
        ["Trial", "trial A"],
        ["ſynthetic", "Synthetic"],
        ["Straße", "STRASSE"],
        ["Σ", "ς"],
        ["", ""],
    ]
    output.write_text(
        json.dumps(
            {
                "clauses": [
                    [clause, owner_assertion(clause), sorted(literal_names(clause))]
                    for clause in clauses
                ],
                "aliases": [[name, given, name_matches(name, [given])] for name, given in aliases],
            },
            ensure_ascii=False,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)


if __name__ == "__main__":
    main()
