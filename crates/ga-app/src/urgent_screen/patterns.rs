//! Literal safety patterns from the existing diary form rules.

pub(super) const RUSSIAN_HISTORY: &str = r#"(?:\s+|[,;]\s*)(?:был(?:а|и|о)?\s+)?(?:(?P<count>\d+|два|три|четыре|пять|шесть|семь|восемь|девять|десять)\s+(?:год|года|лет)\s+назад|в\s+(?P<year>(?:19|20)\d{2})\s+году|(?P<old>в\s+анамнезе|был(?:а|и|о)?\s+давно|давно))\b"#;
pub(super) const RUSSIAN_RECURRENCE: &str = r#"\b(?:снова|опять|повтор\w*)\s+(?:сейчас|сегодня)\b|\b(?:сейчас|сегодня)\s+(?:у\s+(?:него|не[её]|них)\s+)?(?:снова|опять|повтор\w*)\b|\b(?:сейчас|сегодня)\b.{0,40}\b(?:судорог\w*|приступ\w*|инсульт|инфаркт)\b"#;
pub(super) const RUSSIAN_TRAILING_CLAUSE: &str = r#"\s+[а-яё]"#;
pub(super) const THIRD_PARTY: &str = r#"\b(?:(?:my|our)\s+(?:husband|wife|partner|child|son|daughter|mother|father|friend|parent|baby)|someone|somebody|a person|he|she|they)(?:'s)?\s+(?:(?:is|are)\s+)?(?:having|has|experiencing|just\s+had|has\s+just\s+had)\s+(?:a\s+)?(?:stroke|heart attack|seizure)\b(?!\s+(?:disorder|history|risk|medication|recovery)\b)|\b(?:(?:my|our)\s+(?:husband|wife|partner|child|son|daughter|mother|father|friend|parent|baby)|someone|somebody|he|she|they)(?:'s)?\s+(?:(?:is|are)\s+)?(?:bleeding heavily|unable to breathe|can't breathe|isn't breathing|aren't breathing|not breathing|stopped breathing|has stopped breathing)\b|\bу котор(?:ого|ой)\s+(?:инсульт|инфаркт|сердечный приступ)\b|\b(?:(?:my|our)\s+(?:husband|wife|partner|child|son|daughter|mother|father|friend|parent|baby)|someone|somebody|a person|he|she|they)(?:'s)?\s+(?:(?:is|are)\s+)?(?:with|has|having|experiencing)\s+(?:sudden\s+)?severe(?:\s+\w+){0,3}\s+pain\b|\b(?:человек\w*|реб[её]нк\w*)\s+с\s+(?:сильн\w*|нестерпим\w*)\s+бол\w*\b|\bу\s+(?:мамы|папы|матери|отца|реб[её]нка|сына|дочери|мужа|жены|друга|подруги|него|неё|человека)\s+(?:(?:сейчас|сегодня|начал\w*)\s+){0,2}(?:(?:случил(?:ся|ась|ось)|произош[её]л(?:а|о)?|начал(?:ся|ась|ось))\s+)?(?:инсульт|инфаркт|сердечный приступ|судороги|эпилептический приступ|(?:сильн\w*|нестерпим\w*)\s+бол\w*)\b"#;
pub(super) const RUSSIAN_HISTORY_PRELUDE: &str = r#"\b(?:в\s+анамнезе|давно|раньше|(?:\d+|два|три|четыре|пять|шесть|семь|восемь|девять|десять)\s+(?:год|года|лет)\s+назад|в\s+(?:19|20)\d{2}\s+году)\b"#;
pub(super) const RUSSIAN_CLAUSE_SEPARATOR: &str = r#"[.!?;]|,\s*(?:а|но|и)\s+|\b(?:а|но)\s+"#;
pub(super) const RUSSIAN_CURRENT_PRELUDE: &str = r#"\b(?:сейчас|сегодня|снова|опять|теперь)\b"#;
pub(super) const PRIOR_WEEK: &str = r#"\s*(?:(?:log|record|track)\s+|i had\s+)severe(?:\s+\w+){0,3}\s+pain\s+(?:from\s+)?(?:last week|yesterday|\d+\s+days?\s+ago)\b"#;
pub(super) const BREATHING: &str = r#"\b(?:can't|cannot|can\s+not)\s+breathe\b|\bне\s+могу\s+дышать\b|\bi(?:'m| am| feel)\s+unable\s+to\s+breathe\b"#;
pub(super) const BLEEDING: &str =
    r#"\b(?:i(?:'m| am)|i have(?: been)?|i've been)\s+bleeding\s+(?:heavily|a lot)\b"#;
pub(super) const CHEST_AND_SWEAT: &str =
    r#"\bsudden\s+crushing\s+chest\s+(?:pressure|pain)\b.{0,60}\bcold\s+sweat\b"#;
pub(super) const AGAIN_NOW: &str =
    r#"\s*[,;]?\s*(?:(?:and|but)\s+)?again\s+(?:now|today|tonight)\b"#;
pub(super) const RECURRENCE_NOW: &str = r#"\b(?:now|today|currently)\s+(?:it|this)\s+(?:(?:is|was)\s+)?(?:happening|starting|occurring|returning|recurring)\s+again\b"#;
pub(super) const REPEATED_EMERGENCY_NOW: &str = r#"\b(?:now|today|currently|still)\s+having\s+(?:another\s+)?(?:one|them|stroke|strokes|heart attack|heart attacks|seizure|seizures)\b"#;
pub(super) const RECURRENCE_AFTER: &str = r#"\b(?:it|this)\s+(?:(?:is|was)\s+)?(?:happening|happened|starting|started|occurring|occurred)\s+again\s+(?:now|today|tonight)\b"#;
pub(super) const REPEATED_SYMPTOM_NOW: &str = r#"\b(?:stroke|heart attack|seizure|pain|symptoms?)\s+(?:(?:is|are|has)\s+)?(?:happening|happened|occurring|occurred|starting|started|coming|returning|recurring)\s+again\s+(?:now|today|tonight)\b"#;
pub(super) const FAST_CURRENT_PRELUDE: &str =
    r#"\b(?:(?:but|and)\s+)?(?:now|today|tonight|currently)\s*,?\s*$"#;
pub(super) const HISTORICAL_MARKER: &str = r#"\b(?:as a child|in (?:my )?childhood|when i was (?:a child|young)|back then|long ago|many years ago)\b"#;
pub(super) const GENERAL_QUESTION: &str = r#"\s*(?:what|which|why|how|are|is|do|does|did|can you|could you|please explain|explain|tell me|какие|что|почему|как|объясни|расскажи)\b"#;
pub(super) const PAIN_ONGOING: &str = r#"\s*[,;]?\s*(?:(?:and|but)\s+)?(?:it|the pain|pain)\s+(?:(?:is|feels)\s+(?:still\s+)?(?:severe|ongoing|continuing)|still\s+(?:hurts|continues))\b"#;
pub(super) const PAIN_NOW: &str = r#"\b(?:now|today|currently|still)\s+(?:i\s+)?(?:feel|have|am\s+having)\s+(?:severe\s+)?(?:chest\s+)?pain\b"#;
pub(super) const SYMPTOM_RETURNED: &str = r#"\b(?:(?:it'?s|it\s+is|pain\s+is)\s+back|(?:the|my|these|those)\s+symptoms?\s+(?:are|is)\s+back\s+(?:now|today|tonight)|(?:now|today|tonight)\s+(?:(?:the|my|these|those)\s+)?symptoms?\s+(?:are|is)\s+back|pain\s+(?:has\s+)?returned|(?:i(?:'m| am)\s+having|am\s+having|i\s+have)\s+(?:one|it|another(?:\s+one)?)\s+(?:now|again)|(?:now|today|currently|still)\s+(?:i(?:'m| am)\s+having|am\s+having|i\s+have)\s+(?:one|it|another(?:\s+one)?)|(?:i(?:'m| am)\s+having|i\s+have)\s+(?:another\s+)?(?:stroke|heart attack|seizure))\b"#;
pub(super) const DATE_SUFFIX: &str = r#"\s*,?\s*(?:(?:in|back in)\s+((?:19|20)\d{2})|(\d+|two|three|four|five|six|seven|eight|nine|ten)\s+years?\s+ago)\b"#;
pub(super) const SELF_REPORT: &str =
    r#"\b(?:i|we|я)\b|у меня|\b(?:my|our)\s+(?:severe|crushing|sudden)\b"#;
pub(super) const ABSENT: &str =
    r#"\s*(?:(?:is|are|was|were)\s+)?(?:not\s+present|absent|denied|нет|не было)\b"#;
pub(super) const DOUBLE_NEGATIVE: &str = r#"\bnot\s+(?:only|without)\b"#;
pub(super) const NEGATED_FAST_SIGN: &str =
    r#"\b(?:no|not|without|don't|doesn't|do\s+not|does\s+not)\s+(?:(?:have|any)\s+){0,2}$"#;
pub(super) const NEGATIVE_PREFIX: &str = r#"(?:\bno\b|\bnot\b|\bwithout\b|\bdon't\b|\bdidn't\b|\bhaven't\b|\bhasn't\b|\bisn't\b|\bwasn't\b|\bнет\b|\bбез\b|\bне было\b)\s+(?:\w+\s+){0,4}$"#;
pub(super) const AGAIN_IN_HISTORY: &str = r#"\bagain\s+(?:now|today|tonight)\b"#;
pub(super) const DATE: &str = r#"(?:(?:in|back in)\s+((?:19|20)\d{2})|(\d+|two|three|four|five|six|seven|eight|nine|ten)\s+years?\s+ago)\b"#;
pub(super) const FACE: &str = r#"\b(?:my|his|her|their|someone's)\s+(?:(?:left|right)\s+side\s+of\s+(?:(?:my|his|her|their)\s+)?)?(?:(?:left|right)\s+)?face\s+(?:(?:is|was)\s+)?droop\w*"#;
pub(super) const ARM: &str = r#"(?:\b(?:(?:my|his|her|their|one|the|a)\s+)?(?:(?:left|right)\s+)?arm\s+(?:(?:is|was|feels|felt)\s+)?weak\b|\bweakness\s+in\s+(?:(?:one|my|his|her|their|the|a|left|right)\s+)?arm\b)"#;
pub(super) const DISTANT_HISTORY_1: &str = r#"(?:^\s*(?:(?:log|record|add|track)(?:\s+that)?\s+)?|(?<=[;.!?])\s*)i had (?:a )?(?:stroke|heart attack|seizure)\s+(?:(?:in|back in)\s+((?:19|20)\d{2})|(\d+|two|three|four|five|six|seven|eight|nine|ten)\s+years?\s+ago)\b"#;
pub(super) const DISTANT_HISTORY_2: &str = r#"(?:^|(?<=[;.!?]))\s*i had severe(?:\s+\w+){0,3}\s+pain\s+(?:(?:in|back in)\s+((?:19|20)\d{2})|(\d+|two|three|four|five|six|seven|eight|nine|ten)\s+years?\s+ago)\b"#;
pub(super) const DISTANT_HISTORY_3: &str = r#"(?:^\s*(?:(?:log|record|add|track)(?:\s+that)?\s+)?|(?<=[;.!?])\s*)(?:i had\s+)?sudden\s+crushing\s+chest\s+(?:pressure|pain)\b[^.;!?]{0,60}?\bcold\s+sweat\s+(?:(?:in|back in)\s+((?:19|20)\d{2})|(\d+|two|three|four|five|six|seven|eight|nine|ten)\s+years?\s+ago)\b"#;
pub(super) const DISTANT_HISTORY_4: &str = r#"(?:^\s*(?:(?:log|record|add|track)(?:\s+that)?\s+)?|(?<=[;.!?])\s*)(?:i had\s+)?sudden\s+crushing\s+chest\s+(?:pressure|pain)\s+(?:(?:in|back in)\s+((?:19|20)\d{2})|(\d+|two|three|four|five|six|seven|eight|nine|ten)\s+years?\s+ago)\b[^.;!?]{0,60}?\bcold\s+sweat\b"#;
pub(super) const DISTANT_HISTORY_5: &str = r#"\b(?:passed out|lost consciousness)\s+(?:(?:in|back in)\s+((?:19|20)\d{2})|(\d+|two|three|four|five|six|seven|eight|nine|ten)\s+years?\s+ago)\b"#;

pub(super) const URGENT: &[&str] = &[
    r#"\b(?:сильн\w*|нестерпим\w*)\s+бол\w*\b"#,
    r#"\bsevere(?:\s+\w+){0,3}\s+pain\b"#,
    r#"\bcrushing\s+chest\s+(?:pressure|pain)\b.{0,60}\bcold\s+sweat\b"#,
    r#"\b(?:signs? of (?:a )?stroke|stroke symptoms?)\b"#,
    r#"\b(?:severe bleeding|uncontrolled bleeding)\b"#,
    r#"\bсильн\w* кровотечен\w*\b"#,
    r#"\b(?:i(?:'m| am) having|i have|i had|i(?:'ve| have)? just had|i(?:'m| am) experiencing) (?:a )?(?:stroke|heart attack|seizure)\b(?!\s+(?:disorder|history|risk|medication|recovery)\b)"#,
    r#"\bi\s+(?:think\s+i(?:'m| am)|may\s+be)\s+having\s+(?:a\s+)?(?:stroke|heart attack|seizure)\b(?!\s+(?:disorder|history|risk|medication|recovery)\b)"#,
    r#"\b(?:признак\w* инсульта|потерял\w* сознание|теряю сознание)\b"#,
    r#"\bу меня\s+(?:(?:сейчас|сегодня|снова|опять|внезапно|начал\w*|случил(?:ся|ась|ось|ись)|произош(?:[её]л(?:а|о)?|ли))\s+){0,2}(?:инсульт|инфаркт|сердечный приступ|судорог\w*|эпилептическ\w*\s+приступ)\b"#,
    r#"\b(?:lost consciousness|passed out)\b"#,
];
