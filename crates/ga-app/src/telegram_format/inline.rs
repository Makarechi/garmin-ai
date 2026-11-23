//! Static inline-only host for the parser shared with markdown-it-py.

use std::cell::RefCell;
use std::collections::HashSet;
use std::rc::Rc;

use markdown_it_rust_core::options_env::{EnvData, OptionsData};
use markdown_it_rust_core::punycode;
use markdown_it_rust_core::rules_inline::{
    InlineRule, ParserInline, skip_token_with, tokenize_with,
};
use markdown_it_rust_core::{
    EnvSnapshot, Host, LinkifyMatch, SharedBlockState, SharedCoreState, SharedInlineState,
    SharedOptions, SharedToken,
};

const URL_SAFE: &[u8] = b";/?:@&=+$,-_.!~*'()#";
const URL_KEEP_ESCAPED: &[u8] = b";/?:@&=+$,#%";
const HEX: &[u8; 16] = b"0123456789ABCDEF";

pub(super) fn parse(source: &str) -> Vec<SharedToken> {
    let options = Rc::new(RefCell::new(OptionsData {
        html: false,
        ..OptionsData::default()
    }));
    let mut parser = ParserInline::new();
    let (rules, terminators) = parser.snapshot();
    let host = Rc::new(InlineOnly { rules, terminators });
    parser.parse_fresh(source, options, host, EnvData::default())
}

struct InlineOnly {
    rules: Vec<InlineRule>,
    terminators: HashSet<char>,
}

impl Host for InlineOnly {
    fn normalize_link(&self, url: &str) -> String {
        let url = recode_host(url, punycode::to_ascii);
        let mut result = String::with_capacity(url.len());
        let mut bytes = url.as_bytes().iter().copied();
        while let Some(byte) = bytes.next() {
            if byte.is_ascii_alphanumeric() || URL_SAFE.contains(&byte) {
                result.push(char::from(byte));
            } else if byte == b'%' && percent_byte(bytes.clone().take(2)).is_some() {
                result.push('%');
            } else {
                result.push('%');
                result.push(char::from(HEX[(byte >> 4) as usize]));
                result.push(char::from(HEX[(byte & 15) as usize]));
            }
        }
        result
    }

    fn normalize_link_text(&self, url: &str) -> String {
        let url = recode_host(url, |host| {
            punycode::to_unicode(host).unwrap_or_else(|_| host.to_owned())
        });
        let mut output = Vec::with_capacity(url.len());
        let mut remaining = url.as_bytes();
        while let Some((&byte, rest)) = remaining.split_first() {
            if byte == b'%'
                && let Some(decoded) = percent_byte(rest.iter().take(2).copied())
            {
                if URL_KEEP_ESCAPED.contains(&decoded) {
                    output.extend_from_slice(&[
                        b'%',
                        HEX[(decoded >> 4) as usize],
                        HEX[(decoded & 15) as usize],
                    ]);
                } else {
                    output.push(decoded);
                }
                remaining = &rest[2..];
                continue;
            }
            output.push(byte);
            remaining = rest;
        }
        let mut text = String::with_capacity(output.len());
        for chunk in output.utf8_chunks() {
            text.push_str(chunk.valid());
            // mdurl replaces each invalid byte in a percent-encoded sequence.
            text.extend(std::iter::repeat_n('\u{fffd}', chunk.invalid().len()));
        }
        text
    }

    fn validate_link(&self, url: &str, _validator: Option<u64>) -> bool {
        let url = url.trim().to_ascii_lowercase();
        match url.split_once(':') {
            Some(("javascript" | "vbscript" | "file", _)) => false,
            Some(("data", payload)) => ["image/gif;", "image/png;", "image/jpeg;", "image/webp;"]
                .iter()
                .any(|prefix| payload.starts_with(prefix)),
            _ => true,
        }
    }

    fn inline_skip_token(&self, state: &SharedInlineState) {
        let host = state.borrow().md.host.clone();
        skip_token_with(&self.rules, &self.terminators, state, &host);
    }

    fn inline_tokenize(&self, state: &SharedInlineState) {
        let host = state.borrow().md.host.clone();
        tokenize_with(&self.rules, &self.terminators, state, &host);
    }

    fn inline_parse_content(
        &self,
        _parent: &SharedInlineState,
        _content: &str,
    ) -> Vec<SharedToken> {
        // The Telegram formatter takes the raw image alt text and ignores child tokens.
        Vec::new()
    }

    // The static chain has no callbacks, block rules, linkification, or HTML renderer.
    fn tokenize_block(&self, _: &SharedBlockState, _: usize, _: usize) {}
    fn call_block_rule(&self, _: u64, _: &SharedBlockState, _: usize, _: usize, _: bool) -> bool {
        false
    }
    fn call_inline_rule(&self, _: u64, _: &SharedInlineState, _: bool) -> bool {
        false
    }
    fn call_inline2_rule(&self, _: u64, _: &SharedInlineState) {}
    fn call_core_rule(&self, _: u64, _: &SharedCoreState) {}
    fn linkify_pretest(&self, _: &str) -> Option<bool> {
        None
    }
    fn linkify_test(&self, _: &str) -> Option<bool> {
        None
    }
    fn linkify_match_at_start(&self, _: &str) -> Option<LinkifyMatch> {
        None
    }
    fn linkify_match(&self, _: &str) -> Vec<LinkifyMatch> {
        Vec::new()
    }
    fn has_linkify(&self) -> bool {
        false
    }
    fn call_highlight(&self, _: &str, _: &str, _: &str) -> Option<String> {
        None
    }
    fn render_override(&self, _: &str) -> Option<u64> {
        None
    }
    fn call_render_rule(
        &self,
        _: u64,
        _: &[SharedToken],
        _: usize,
        _: &SharedOptions,
        _: &EnvData,
    ) -> String {
        String::new()
    }
    fn html_tag_match(&self, _: &str, _: usize) -> Option<String> {
        None
    }
    fn block_terminates(&self, _: &str, _: &SharedBlockState, _: usize, _: usize) -> bool {
        false
    }
    fn core_block_parse(&self, _: &SharedCoreState) {}
    fn core_inline_parse(&self, _: &SharedCoreState, _: &SharedToken) {}
    fn env_snapshot(&self, _: &EnvData) -> EnvSnapshot {
        EnvSnapshot::default()
    }
    fn env_merge_back(&self, _: &mut EnvData, _: EnvSnapshot) {}
}

fn percent_byte(mut digits: impl Iterator<Item = u8>) -> Option<u8> {
    let high = u8::try_from(char::from(digits.next()?).to_digit(16)?).ok()?;
    let low = u8::try_from(char::from(digits.next()?).to_digit(16)?).ok()?;
    Some((high << 4) | low)
}

fn recode_host(url: &str, convert: impl FnOnce(&str) -> String) -> String {
    let url = url.trim();
    let start = if url.starts_with("//") {
        2
    } else if let Some((scheme, rest)) = url.split_once(':') {
        match (scheme, rest.strip_prefix("//")) {
            ("http" | "https" | "mailto", Some(_)) => scheme.len() + 3,
            ("mailto", None) => scheme.len() + 1,
            _ => return url.to_owned(),
        }
    } else {
        return url.to_owned();
    };
    let end = start
        + url[start..]
            .find(['/', '?', ';', '#'])
            .unwrap_or(url.len() - start);
    let auth = &url[start..end];
    let host_start = auth.rfind('@').map_or(start, |at| start + at + 1);
    let authority = &url[host_start..end];
    if authority.starts_with('[') {
        return url.to_owned();
    }
    let host_end = authority
        .find([':', '%', ' '])
        .map_or(end, |stop| host_start + stop);
    let recoded = convert(&url[host_start..host_end]);
    format!("{}{recoded}{}", &url[..host_start], &url[host_end..])
}
