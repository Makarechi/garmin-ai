use std::sync::LazyLock;

use serde_json::Value;

/// Read the application catalog for the language and fall back to English.
#[must_use]
pub fn catalog_text(locale: &str, key: &str) -> Option<&'static str> {
    static EN: LazyLock<Value> = LazyLock::new(|| {
        serde_json::from_str(include_str!("../../../src/garmin_ai/locales/en.json"))
            .unwrap_or(Value::Null)
    });
    static RU: LazyLock<Value> = LazyLock::new(|| {
        serde_json::from_str(include_str!("../../../src/garmin_ai/locales/ru.json"))
            .unwrap_or(Value::Null)
    });
    static SK: LazyLock<Value> = LazyLock::new(|| {
        serde_json::from_str(include_str!("../../../src/garmin_ai/locales/sk.json"))
            .unwrap_or(Value::Null)
    });
    let language = locale.split('-').next().unwrap_or_default();
    let catalog = if language.eq_ignore_ascii_case("ru") {
        &RU
    } else if language.eq_ignore_ascii_case("sk") {
        &SK
    } else {
        &EN
    };
    catalog[key].as_str().or_else(|| EN[key].as_str())
}
