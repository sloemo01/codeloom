// codeloom_core_rs — a real multi-threaded Rust accelerator for codeloom.
// Drop-in alternative to the C core. Reads file paths from stdin (one per
// line) and emits one JSON object per line:
//   {"file": "...", "symbols":[{"name":"...","kind":"..."}], "imports":[...], "calls":[...]}
// Supports `--list ROOT` to walk code files fast. std-only, no external crates.
//
// Build:  rustc -O -o codeloom_core_rs codeloom_core_rs.rs
// Wire:   codeloom --index --engine rust  (auto-detects the binary)
//
// MIT © 2026 sloemo01 — see LICENSE.

use std::collections::HashSet;
use std::env;
use std::fs;
use std::io::{self, Read};
use std::path::Path;
use std::process;
use std::sync::Arc;
use std::thread;

const CODE_EXTS: &[&str] = &[
    ".py", ".js", ".mjs", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".rb",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".swift", ".kt", ".php", ".scala",
    ".dart", ".lua", ".sh", ".r", ".ex", ".exs", ".ml", ".elm", ".hs", ".clj",
];

fn is_code_file(p: &str) -> bool {
    let lower = p.to_lowercase();
    CODE_EXTS.iter().any(|e| lower.ends_with(e))
}

// --- minimal JSON string escaping + object builder (no serde) ---
fn jesc(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 8);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

// --- language scanners (regex-free, brace/word token walk) ---
fn scan_file(path: &str) -> (Vec<(String, String)>, Vec<String>, Vec<String>) {
    let mut symbols: Vec<(String, String)> = Vec::new(); // (name, kind)
    let mut imports: Vec<String> = Vec::new();
    let mut calls: Vec<String> = Vec::new();
    let lower = path.to_lowercase();

    let text = match fs::read_to_string(path) {
        Ok(t) => t,
        Err(_) => return (symbols, imports, calls),
    };

    if lower.ends_with(".py") {
        // symbols: def name / class Name / async def
        for line in text.lines() {
            let t = line.trim();
            if t.starts_with("def ") || t.starts_with("async def ") {
                let rest = t.trim_start_matches("async def ").trim_start_matches("def ").trim_start();
                let name: String = rest.chars().take_while(|c| c.is_alphanumeric() || *c == '_').collect();
                if !name.is_empty() { symbols.push((name, "function".into())); }
            } else if t.starts_with("class ") {
                let rest = t.trim_start_matches("class ").trim_start();
                let name: String = rest.chars().take_while(|c| c.is_alphanumeric() || *c == '_').collect();
                if !name.is_empty() { symbols.push((name, "class".into())); }
            } else if t.starts_with("import ") || t.starts_with("from ") {
                let words: Vec<&str> = t.split_whitespace().collect();
                if !words.is_empty() {
                    let target = words[1].trim_matches(|c| c == ',' || c == '(' || c == ')');
                    imports.push(target.trim().to_string());
                }
            }
        }
    } else if lower.ends_with(".rs") {
        for line in text.lines() {
            let t = line.trim();
            for kw in ["fn ", "struct ", "enum ", "trait ", "impl ", "mod ", "type ", "union ", "const ", "static "] {
                if t.starts_with(kw) {
                    let rest = t.trim_start_matches(kw).trim_start();
                    let name: String = rest.chars().take_while(|c| c.is_alphanumeric() || *c == '_').collect();
                    if !name.is_empty() { symbols.push((name, kw.trim().into())); }
                    break;
                }
            }
            if t.starts_with("use ") {
                let rest = t.trim_start_matches("use ").trim();
                imports.push(rest.to_string());
            }
        }
    } else if lower.ends_with(".go") {
        for line in text.lines() {
            let t = line.trim();
            if t.starts_with("func ") {
                let rest = t.trim_start_matches("func ").trim_start();
                let name: String = rest.chars().take_while(|c| c.is_alphanumeric() || *c == '_').collect();
                if !name.is_empty() { symbols.push((name, "function".into())); }
            } else if t.starts_with("type ") {
                let rest = t.trim_start_matches("type ").trim_start();
                let name: String = rest.chars().take_while(|c| c.is_alphanumeric() || *c == '_').collect();
                if !name.is_empty() { symbols.push((name, "type".into())); }
            } else if t.starts_with("import ") {
                let rest = t.trim_start_matches("import ").trim();
                imports.push(rest.trim_matches('"').to_string());
            }
        }
    } else if lower.ends_with(".ts") || lower.ends_with(".tsx") || lower.ends_with(".js") || lower.ends_with(".jsx") || lower.ends_with(".mjs") {
        // symbols: function name, class Name, export function/class/const, const X = fn
        for line in text.lines() {
            let t = line.trim();
            if t.starts_with("function ") {
                let rest = t.trim_start_matches("function ").trim_start();
                let name: String = rest.chars().take_while(|c| c.is_alphanumeric() || *c == '_' || *c == '$').collect();
                if !name.is_empty() { symbols.push((name, "function".into())); }
            } else if t.starts_with("class ") {
                let rest = t.trim_start_matches("class ").trim_start();
                let name: String = rest.chars().take_while(|c| c.is_alphanumeric() || *c == '_').collect();
                if !name.is_empty() { symbols.push((name, "class".into())); }
            } else if t.starts_with("export ") && (t.contains("function") || t.contains("class") || t.contains("const") || t.contains("let")) {
                let rest = t.trim_start_matches("export ").trim_start();
                let after: &str = if rest.starts_with("function ") { rest.trim_start_matches("function ").trim_start() }
                    else if rest.starts_with("class ") { rest.trim_start_matches("class ").trim_start() }
                    else { rest.split_whitespace().nth(1).unwrap_or("") };
                let name: String = after.chars().take_while(|c| c.is_alphanumeric() || *c == '_' || *c == '$').collect();
                if !name.is_empty() { symbols.push((name, "symbol".into())); }
            } else if t.starts_with("import ") {
                let rest = t.trim_start_matches("import ").trim();
                let quote_start = rest.find('"').map(|i| i + 1);
                if let Some(s) = quote_start {
                    if let Some(e) = rest[s..].find('"') {
                        imports.push(rest[s..s + e].to_string());
                    }
                } else if rest.starts_with('{') {
                    // named import
                    let after_brace = rest.find("from ").map(|i| i + 5);
                    if let Some(s) = after_brace {
                        let m = rest[s..].trim();
                        let q = m.find('"').map(|i| i + 1);
                        if let Some(qs) = q {
                            if let Some(qe) = m[qs..].find('"') {
                                imports.push(m[qs..qs + qe].to_string());
                            }
                        }
                    }
                }
            }
        }
    } else if lower.ends_with(".c") || lower.ends_with(".h") || lower.ends_with(".cpp") || lower.ends_with(".hpp") || lower.ends_with(".cc") {
        // C/C++: function defs at column 0 + #include
        for line in text.lines() {
            let t = line.trim();
            if t.starts_with("#include") {
                let rest = t.trim_start_matches("#include").trim();
                if let Some(s) = rest.find(|c: char| c == '"' || c == '<') {
                    let open = rest.chars().nth(s).unwrap();
                    let close = if open == '"' { '"' } else { '>' };
                    if let Some(e) = rest[s + 1..].find(close) {
                        imports.push(rest[s + 1..s + 1 + e].to_string());
                    }
                }
            }
            // crude: a line with '(' after an identifier that isn't a control word
            let has_paren = t.contains('(');
            if has_paren && !t.starts_with("if") && !t.starts_with("for") && !t.starts_with("while") && !t.starts_with("return") && !t.starts_with("switch") {
                let name: String = t.chars().take_while(|c| c.is_alphanumeric() || *c == '_').collect();
                if !name.is_empty() && name.len() > 1 {
                    symbols.push((name, "function".into()));
                }
            }
        }
    }

    // call detection: identifiers followed by '(' (crude, language-agnostic)
    // skip control keywords; dedupe
    let mut seen_calls: HashSet<String> = HashSet::new();
    let bytes = text.as_bytes();
    let mut i = 0usize;
    while i < bytes.len() {
        let c = bytes[i];
        if c.is_ascii_alphabetic() || c == b'_' {
            let start = i;
            while i < bytes.len() && (bytes[i].is_ascii_alphanumeric() || bytes[i] == b'_' || bytes[i] == b'$') {
                i += 1;
            }
            let word = &text[start..i];
            // skip whitespace and check for '('
            let mut j = i;
            while j < bytes.len() && bytes[j].is_ascii_whitespace() { j += 1; }
            if j < bytes.len() && bytes[j] == b'(' {
                let kw = ["if", "for", "while", "return", "switch", "catch", "function", "def", "fn", "elif", "else", "match", "new", "sizeof", "print", "len"];
                if !kw.contains(&word) && !word.is_empty() {
                    if seen_calls.insert(word.to_string()) {
                        calls.push(word.to_string());
                    }
                }
            }
        } else {
            i += 1;
        }
    }

    (symbols, imports, calls)
}

fn walk_dir(root: &Path, out: &mut Vec<String>, depth: usize) {
    if depth > 40 { return; }
    let rd = match fs::read_dir(root) { Ok(r) => r, Err(_) => return };
    for entry in rd.flatten() {
        let p = entry.path();
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name == ".git" || name == "node_modules" || name == "target" || name == "__pycache__" || name == ".codeloom" || name == ".codeloom-memory" {
            continue;
        }
        if p.is_dir() {
            walk_dir(&p, out, depth + 1);
        } else if p.is_file() && is_code_file(&p.to_string_lossy()) {
            out.push(p.to_string_lossy().to_string());
        }
    }
}

fn cmd_list(root: &str) -> i32 {
    let mut files = Vec::new();
    walk_dir(Path::new(root), &mut files, 0);
    for f in files {
        println!("{}", f);
    }
    0
}

fn cmd_scan() -> i32 {
    let mut input = String::new();
    if io::stdin().lock().read_to_string(&mut input).is_err() {
        return 1;
    }
    let files: Vec<String> = input.lines().map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect();
    if files.is_empty() { return 0; }

    let files = Arc::new(files);
    let nthreads = std::cmp::max(1, std::cmp::min(8, std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4)));
    let chunk = std::cmp::max(1, (files.len() + nthreads - 1) / nthreads);
    let stdout = io::stdout();
    let mut handles = Vec::new();
    let mut cursor = 0usize;

    while cursor < files.len() {
        let end = std::cmp::min(cursor + chunk, files.len());
        let slice: Vec<String> = files[cursor..end].to_vec();
        let h = thread::spawn(move || {
            let mut out_lines: Vec<String> = Vec::with_capacity(slice.len());
            for f in &slice {
                let (symbols, imports, calls) = scan_file(f);
                // build JSON
                let mut line = String::from("{\"file\":");
                line.push_str(&jesc(f));
                line.push_str(",\"symbols\":[");
                for (i, (n, k)) in symbols.iter().enumerate() {
                    if i > 0 { line.push(','); }
                    line.push_str(&format!("{{\"name\":{},\"kind\":{}}}", jesc(n), jesc(k)));
                }
                line.push_str("],\"imports\":[");
                for (i, imp) in imports.iter().enumerate() {
                    if i > 0 { line.push(','); }
                    line.push_str(&jesc(imp));
                }
                line.push_str("],\"calls\":[");
                for (i, c) in calls.iter().enumerate() {
                    if i > 0 { line.push(','); }
                    line.push_str(&jesc(c));
                }
                line.push_str("]}");
                out_lines.push(line);
            }
            out_lines
        });
        handles.push(h);
        cursor = end;
    }

    let mut ordered: Vec<String> = Vec::new();
    for h in handles {
        let res = h.join().unwrap_or_default();
        for l in res {
            ordered.push(l);
        }
    }
    for l in ordered {
        println!("{}", l);
    }
    0
}

fn main() {
    let args: Vec<String> = env::args().collect();
    let code = if args.len() >= 3 && args[1] == "--list" {
        cmd_list(&args[2])
    } else if args.len() >= 2 && args[1] == "--list" {
        cmd_list(".")
    } else {
        cmd_scan()
    };
    process::exit(code);
}
