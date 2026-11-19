#[derive(Eq, PartialEq)]
pub(crate) struct Decimal {
    negative: bool,
    significant: Vec<u8>,
    exponent: i64,
}

impl Decimal {
    pub(crate) fn new(token: &str) -> Option<Self> {
        let negative = token.starts_with('-');
        let token = token.strip_prefix(['-', '+']).unwrap_or(token);
        let (coefficient, exponent) = token.split_once(['e', 'E']).unwrap_or((token, "0"));
        let (integer, fraction) = coefficient.split_once('.').unwrap_or((coefficient, ""));
        let mut digits: Vec<_> = integer
            .bytes()
            .chain(fraction.bytes())
            .skip_while(|&digit| digit == b'0')
            .collect();
        let Some(last) = digits.iter().rposition(|&digit| digit != b'0') else {
            return Some(Self {
                negative: false,
                significant: Vec::new(),
                exponent: 0,
            });
        };
        let exponent = exponent
            .parse::<i64>()
            .ok()?
            .checked_sub(fraction.len() as i64)?
            .checked_add((digits.len() - last - 1) as i64)?;
        digits.truncate(last + 1);
        Some(Self {
            negative,
            significant: digits,
            exponent,
        })
    }

    pub(crate) fn adjusted(&self, zero_exponent: i64) -> Option<i64> {
        if self.significant.is_empty() {
            Some(zero_exponent)
        } else {
            self.exponent.checked_add(self.significant.len() as i64 - 1)
        }
    }

    pub(crate) fn is_integral(&self) -> bool {
        self.significant.is_empty() || self.exponent >= 0
    }

    pub(crate) fn integer_token(&self) -> Option<String> {
        if self.significant.is_empty() {
            return Some("0".to_owned());
        }
        let zeroes = usize::try_from(self.exponent).ok()?;
        let mut value = String::with_capacity(self.significant.len() + zeroes + 1);
        if self.negative {
            value.push('-');
        }
        value.extend(self.significant.iter().copied().map(char::from));
        value.extend(std::iter::repeat_n('0', zeroes));
        Some(value)
    }
}
