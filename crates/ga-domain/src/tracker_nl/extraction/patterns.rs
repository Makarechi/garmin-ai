use std::{collections::BTreeMap, sync::OnceLock};

use fancy_regex::Regex;

use super::ExtractionError;

pub(super) const OFFSET: &str =
    r"\s*(?:(?:(?:UTC|GMT)\s*)?([+-])\d{2}:?\d{2}(?!\d)|Z(?!\w)|(?:UTC|GMT)(?!\w|\s*[+-]))";

pub(super) fn regex(name: &str) -> Result<&'static Regex, ExtractionError> {
    static PATTERNS: OnceLock<Result<BTreeMap<&'static str, Regex>, ExtractionError>> =
        OnceLock::new();
    PATTERNS.get_or_init(|| {
        let pairs = [
            ("sentence", r"[!?]|(?<!\d)\.(?!\d)|(?<=\d)\.(?=\s|$)"),
            ("negation", r"(?<!\w)(?:not|no|never|не|нет|ни|(?:did|do|does|have|has|had|was|were|is|are|can|could|would|should|will|must)n['’]t)(?!\w)"),
            ("future", r"(?<!\w)(?:will|shall|gonna|буду|будешь|будет|будем|будете|будут)(?!\w)|(?<!\w)(?:i|you|he|she|it|we|they)['’]ll(?!\w)"),
            ("other", r"(?<!\w)(?:he|she|they|он|она|они)(?!\w)"),
            ("words", r"[^\W_]+"),
            ("number", r"[-+]?(?:\d+(?:[.,]\d+)?|[.,]\d+)"),
            ("whole_number", r"^[-+]?(?:\d+(?:[.,]\d+)?|[.,]\d+)$"),
            ("number_suffix", r"[-+]?(?:\d+(?:[.,]\d+)?|[.,]\d+)$"),
            ("positive", r"(?<!\w)(?:true|yes|да|есть)(?!\w)"),
            ("negative", r"(?<!\w)(?:не\s+было|false|no|нет)(?!\w)"),
            ("date_number", r"(?<!\d)(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4})(?!\d)"),
            ("colon_time", r"(?<![\d:])\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?![\d:])"),
            ("ampm_number", r"(?<![\w.])(?:1[0-2]|0?[1-9])(?:[:.][0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)(?!\w)"),
            ("ampm_suffix", r"^\s*(?:a\.?m\.?|p\.?m\.?)(?!\w)"),
            ("clock", r"(?<![\d:.+])([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d|[.:]\d)"),
            ("at_clock", r"(?:\bat\b|\bв\b)\s+([01]?\d|2[0-3])(?:[:.]([0-5]\d))?(?!\d|[.:]\d)"),
            ("interval", r"^\s*(?:to|until|till|до|по|[-–—])\s*$"),
            ("and", r"^\s*(?:and|и)\s*$"),
            ("between", r"(?<!\w)(?:between|между)\s*$"),
            ("between_start", r"(?<!\w)(?:between|между)\s+"),
            ("hyphen", r"^\s*-\s*$"),
            ("offset_start", r"^\s*(?:(?:(?:UTC|GMT)\s*)?([+-])\d{2}:?\d{2}(?!\d)|Z(?!\w)|(?:UTC|GMT)(?!\w|\s*[+-]))"),
            ("zero_offset", r"^\s*(?:Z|UTC|GMT)\s*$"),
            ("numeric_offset", r"([+-])(0\d|1\d|2[0-3]):?([0-5]\d)(?!\d)"),
            ("approx_before", r"(?<!\w)(?:about|around|approximately|около|примерно)\s*(?:(?:at|в)\s*)?$"),
            ("approx_after", r"^\s*(?:about|around|approximately|около|примерно)\s*(?=$|[,;.!?])"),
            ("now", r"(?<!\w)(?:сейчас|now|только что|just now)(?!\w)"),
            ("iso_day", r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)"),
            ("local_day", r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{4})(?!\d)"),
            ("decimal_quantity", r"^[-+]?\d+\.\d+$"),
        ];
        let mut result = BTreeMap::new();
        for (name, pattern) in pairs {
            result.insert(name, crate::tracker_selection::compile(pattern).map_err(|_| ExtractionError::Contract)?);
        }
        let day = r"(?:(?<!\w)(?:today|yesterday|tomorrow|сегодня|вчера|завтра|now|сейчас)(?!\w)|(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{4})(?!\d))";
        for (name, pattern) in [
            ("before_clock", format!(r"{day}\s*[,:]?\s*(?:(?:T|at|from|between|в|с|между)\s*[,:]?\s*)?")),
            ("after_clock", format!(r"^(?:{OFFSET})?\s*[,:]?\s*(?:(?:on|в)\s+)?{day}")),
            ("prefix_connector", r"^\s*[,:]?\s*(?:(?:T|at|from|between|в|с|между)\s*[,:]?\s*)?$".to_owned()),
            ("suffix_connector", format!(r"^(?:{OFFSET})?\s*[,:]?\s*(?:(?:on|в)\s*[,:]?\s*)?$")),
        ] {
            result.insert(name, crate::tracker_selection::compile(&pattern).map_err(|_| ExtractionError::Contract)?);
        }
        for term in ["сегодня", "today", "вчера", "yesterday", "завтра", "tomorrow"] {
            let pattern = format!(r"(?<!\w){term}(?!\w)");
            result.insert(term, crate::tracker_selection::compile(&pattern).map_err(|_| ExtractionError::Contract)?);
        }
        Ok(result)
    }).as_ref().map_err(|_| ExtractionError::Contract)?.get(name).ok_or(ExtractionError::Contract)
}
