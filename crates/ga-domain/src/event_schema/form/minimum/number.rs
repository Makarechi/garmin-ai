use std::cmp::Ordering;

use num_bigint::{BigInt, Sign, ToBigInt};
use serde_json::{Map, Value, json};

use super::{FormProjectionError, serialized_length};
use crate::event_schema::custom::ExactNumber;

const LOWER: &[&str] = &["minimum", "exclusiveMinimum"];
const UPPER: &[&str] = &["maximum", "exclusiveMaximum"];

fn power(exponent: usize) -> BigInt {
    BigInt::from(10).pow(u32::try_from(exponent).unwrap_or(0))
}

fn floor_div(value: &BigInt, step: &BigInt) -> BigInt {
    let quotient = value / step;
    if value.sign() == Sign::Minus && (value % step).sign() != Sign::NoSign {
        quotient - 1
    } else {
        quotient
    }
}

fn ceil_div(value: &BigInt, step: &BigInt) -> BigInt {
    -floor_div(&-value, step)
}

fn digits(value: &BigInt) -> usize {
    value.to_string().trim_start_matches('-').len()
}

pub(super) fn integer(
    node: &Map<String, Value>,
    exact: bool,
    storage: bool,
) -> Result<usize, FormProjectionError> {
    let mut lower = Vec::new();
    let mut upper = Vec::new();
    for &key in LOWER {
        if let Some(bound) = node.get(key) {
            let bound = ExactNumber::read(bound).ok_or(FormProjectionError)?;
            lower.push(if key == "minimum" {
                bound.ceil()
            } else {
                bound.floor() + 1
            });
        }
    }
    for &key in UPPER {
        if let Some(bound) = node.get(key) {
            let bound = ExactNumber::read(bound).ok_or(FormProjectionError)?;
            upper.push(if key == "maximum" {
                bound.floor()
            } else {
                bound.ceil() - 1
            });
        }
    }
    let lower = lower.into_iter().max();
    let upper = upper.into_iter().min();
    if lower
        .as_ref()
        .zip(upper.as_ref())
        .is_some_and(|(lower, upper)| lower > upper)
    {
        return Ok(0);
    }
    if lower
        .as_ref()
        .is_none_or(|value| value.sign() != Sign::Plus)
        && upper
            .as_ref()
            .is_none_or(|value| value.sign() != Sign::Minus)
    {
        return Ok(1);
    }
    let lower = lower.unwrap_or_else(|| -power(upper.as_ref().map_or(1, digits) + 1));
    let upper = upper.unwrap_or_else(|| power(digits(&lower) + 1));
    let width = |value: &BigInt| {
        let spelling = value.to_string();
        let plain = spelling.len();
        let trailing = plain - spelling.trim_end_matches('0').len();
        if storage || trailing == 0 {
            return plain;
        }
        if exact {
            let rounded = spelling
                .parse::<f64>()
                .ok()
                .filter(|value| value.is_finite() && value.fract() == 0.0)
                .and_then(|value| value.to_bigint());
            if !rounded.is_some_and(|value| lower <= value && value <= upper) {
                return plain;
            }
        }
        plain.min(plain - trailing + 1 + trailing.to_string().len())
    };
    let mut shortest = width(&lower).min(width(&upper));
    for exponent in 1..=digits(&lower).max(digits(&upper)) {
        let step = power(exponent);
        let first = ceil_div(&lower, &step) * &step;
        if first <= upper {
            let last = floor_div(&upper, &step) * &step;
            shortest = shortest.min(width(&first)).min(width(&last));
        }
    }
    Ok(shortest)
}

struct Decimal {
    coefficient: BigInt,
    exponent: i32,
}

impl Decimal {
    fn parse(spelling: &str) -> Option<Self> {
        let (fraction, exponent) = spelling
            .split_once(['e', 'E'])
            .map_or(Some((spelling, 0)), |(coefficient, exponent)| {
                Some((coefficient, exponent.parse::<i32>().ok()?))
            })?;
        let places = fraction
            .split_once('.')
            .map_or(0, |(_, fractional)| fractional.len());
        let exponent = exponent.checked_sub(i32::try_from(places).ok()?)?;
        if exponent.unsigned_abs() > 4096 {
            return None;
        }
        Some(Self {
            coefficient: fraction.replace('.', "").parse().ok()?,
            exponent,
        })
    }

    fn cmp(&self, other: &Self) -> Ordering {
        let shared = self.exponent.min(other.exponent);
        let left = &self.coefficient * power(self.exponent.abs_diff(shared) as usize);
        let right = &other.coefficient * power(other.exponent.abs_diff(shared) as usize);
        left.cmp(&right)
    }

    fn scaled(&self, places: i32) -> (BigInt, BigInt) {
        let exponent = self.exponent + places;
        if exponent >= 0 {
            (
                &self.coefficient * power(exponent.unsigned_abs() as usize),
                BigInt::from(1),
            )
        } else {
            (
                self.coefficient.clone(),
                power(exponent.unsigned_abs() as usize),
            )
        }
    }

    fn stored(value: &Value) -> Option<Self> {
        Self::parse(value.as_number()?.as_str())
    }
}

fn fractional(node: &Map<String, Value>, storage: bool) -> Result<usize, FormProjectionError> {
    let lower_open = node.contains_key("exclusiveMinimum");
    let upper_open = node.contains_key("exclusiveMaximum");
    let lower = node
        .get(if lower_open {
            "exclusiveMinimum"
        } else {
            "minimum"
        })
        .and_then(Decimal::stored)
        .ok_or(FormProjectionError)?;
    let upper = node
        .get(if upper_open {
            "exclusiveMaximum"
        } else {
            "maximum"
        })
        .and_then(Decimal::stored)
        .ok_or(FormProjectionError)?;
    for places in 1..350 {
        let (low_num, low_div) = lower.scaled(places);
        let (high_num, high_div) = upper.scaled(places);
        let mut first = ceil_div(&low_num, &low_div);
        let mut last = floor_div(&high_num, &high_div);
        if lower_open && &first * &low_div == low_num {
            first += 1;
        }
        if upper_open && &last * &high_div == high_num {
            last -= 1;
        }
        if first > last {
            continue;
        }
        let midpoint = BigInt::from(0).clamp(first.clone(), last.clone());
        let mut shortest = None;
        for candidate in [first, last, midpoint] {
            let exact = Decimal {
                coefficient: candidate,
                exponent: -places,
            };
            let compare_lower = exact.cmp(&lower);
            let compare_upper = exact.cmp(&upper);
            if compare_lower == Ordering::Less
                || (lower_open && compare_lower == Ordering::Equal)
                || compare_upper == Ordering::Greater
                || (upper_open && compare_upper == Ordering::Equal)
            {
                continue;
            }
            let Some(parsed) = format!("{}e-{}", exact.coefficient, places)
                .parse::<f64>()
                .ok()
                .filter(|number| number.is_finite())
            else {
                continue;
            };
            if Decimal::parse(&parsed.to_string())
                .is_none_or(|stored| stored.cmp(&exact) != Ordering::Equal)
            {
                continue;
            }
            let width = if storage {
                serialized_length(&json!(parsed), true)?
            } else {
                let digits = digits(&exact.coefficient);
                let sign = usize::from(exact.coefficient.sign() == Sign::Minus);
                let places = places.unsigned_abs() as usize;
                let plain = if digits > places {
                    sign + digits + 1
                } else {
                    sign + 2 + places
                };
                plain.min(sign + digits + 2 + places.to_string().len())
            };
            shortest = Some(shortest.map_or(width, |length: usize| length.min(width)));
        }
        if let Some(shortest) = shortest {
            return Ok(shortest);
        }
    }
    Ok(4097)
}

pub(super) fn number(
    node: &Map<String, Value>,
    storage: bool,
) -> Result<usize, FormProjectionError> {
    let size = integer(node, false, storage)?;
    let mut shortest = if size == 0 {
        fractional(node, storage)?
    } else {
        size
    };
    for key in LOWER.iter().chain(UPPER).copied() {
        let Some(bound) = node.get(key).and_then(Value::as_f64) else {
            continue;
        };
        let candidate = match key {
            "exclusiveMinimum" => bound.next_up(),
            "exclusiveMaximum" => bound.next_down(),
            _ => bound,
        };
        if !candidate.is_finite() {
            continue;
        }
        let exact = Decimal::parse(&candidate.to_string()).ok_or(FormProjectionError)?;
        let allowed = LOWER.iter().chain(UPPER).copied().all(|kind| {
            let Some(bound) = node.get(kind) else {
                return true;
            };
            let Some(bound) = Decimal::stored(bound) else {
                return false;
            };
            let order = exact.cmp(&bound);
            match kind {
                "minimum" => order != Ordering::Less,
                "maximum" => order != Ordering::Greater,
                "exclusiveMinimum" => order == Ordering::Greater,
                _ => order == Ordering::Less,
            }
        });
        if allowed {
            shortest = shortest.min(serialized_length(&json!(candidate), true)?);
        }
    }
    Ok(shortest)
}
