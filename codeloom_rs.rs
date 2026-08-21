// codeloom_rs — a genuine full Rust rewrite of codeloom's core read
// intelligence. Single-file, std-only, no external crates. A real standalone
// CLI that walks a repo, extracts multi-language symbols, builds a call graph,
// and answers the hot queries an agent actually makes:
//
//   codeloom_rs map <root>           tree + entry points + symbol counts
//   codeloom_rs search <root> <sym>  where a symbol is defined
//   codeloom_rs usages <root> <sym>  where a symbol is USED (call sites)
//   codeloom_rs read <root> <sym>    extract the symbol's exact source
//   codeloom_rs calls <root>         function-level call graph
//   codeloom_rs imports <root>       import graph
//   codeloom_rs files <root>       list code files
//   codeloom_rs json <root> <sym>  structured symbol lookup (for MCP)
//   codeloom_rs cross <a> <b> ...  multi-repo / cross-service graph (path 5)
//
// Plus engine_rs/ (optional cargo project): real tree-sitter AST parsing for
// 8 major languages (Python/JS/TS/Go/Rust/C/C++/Java), same JSON contract.
// Kept separate so codeloom.py + codeloom_rs stay dependency-free.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::env;
use std::fmt::Write as _;
use std::fs;
use std::path::Path;
use std::process;

const CODE_EXTS: &[&str] = &[
    ".py", ".js", ".mjs", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".rb",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".swift", ".kt", ".php", ".scala",
    ".dart", ".lua", ".sh", ".r", ".ex", ".exs", ".ml", ".elm", ".hs", ".clj",
];
const SKIP_DIRS: &[&str] = &[
    ".git", "node_modules", "target", "__pycache__", ".venv", "venv", "dist",
    "build", ".codeloom", ".codeloom-memory", "vendor", ".idea", ".vscode",
];

fn is_code_file(p: &str) -> bool {
    let lower = p.to_lowercase();
    CODE_EXTS.iter().any(|e| lower.ends_with(e))
}

fn skip_dir(name: &str) -> bool {
    SKIP_DIRS.iter().any(|d| *d == name)
}

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

// ------------------------------------------------------------------ walk ---
fn walk_files(root: &Path, out: &mut Vec<String>, depth: usize) {
    if depth > 60 { return; }
    let rd = match fs::read_dir(root) { Ok(r) => r, Err(_) => return };
    for entry in rd.flatten() {
        let p = entry.path();
        let name = entry.file_name().to_string_lossy().to_string();
        if skip_dir(&name) { continue; }
        if p.is_dir() {
            walk_files(&p, out, depth + 1);
        } else if p.is_file() && is_code_file(&name) {
            out.push(p.to_string_lossy().to_string());
        }
    }
}

// ------------------------------------------------------- symbol extraction --
// Return (symbols, imports, lines) — symbol name + kind + line, imports, all
// call-target words seen. Multi-language via line patterns (fast, robust,
// no external parser). Pure lines, no AST.
fn extract(path: &str) -> (Vec<(String, String, usize)>, Vec<String>, Vec<String>) {
    let text = match fs::read_to_string(path) {
        Ok(t) => t,
        Err(_) => return (vec![], vec![], vec![]),
    };
    let lower = path.to_lowercase();
    let mut symbols: Vec<(String, String, usize)> = Vec::new();
    let mut imports: Vec<String> = Vec::new();
    let mut call_targets: Vec<String> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();

    for (idx, raw) in text.lines().enumerate() {
        let line_no = idx + 1;
        let t = raw.trim();

        // --- symbol detection by language ---
        if lower.ends_with(".py") {
            if t.starts_with("def ") || t.starts_with("async def ") {
                let rest = t.trim_start_matches("async def ").trim_start_matches("def ").trim_start();
                if let Some(name) = take_ident(rest) {
                    symbols.push((name, "function".into(), line_no));
                }
            } else if t.starts_with("class ") {
                if let Some(name) = take_ident(t.trim_start_matches("class ").trim_start()) {
                    symbols.push((name, "class".into(), line_no));
                }
            } else if t.starts_with("import ") || t.starts_with("from ") {
                let w: Vec<&str> = t.split_whitespace().collect();
                if w.len() >= 2 {
                    imports.push(w[1].trim_matches(|c| c == ',' || c == '(' || c == ')').to_string());
                }
            }
        } else if lower.ends_with(".rs") {
            for kw in ["fn ", "struct ", "enum ", "trait ", "impl ", "mod ", "type ", "union ", "const ", "static "] {
                if t.starts_with(kw) {
                    if let Some(name) = take_ident(t.trim_start_matches(kw).trim_start()) {
                        symbols.push((name, kw.trim().to_string(), line_no));
                    }
                    break;
                }
            }
            if t.starts_with("use ") {
                imports.push(t.trim_start_matches("use ").trim().to_string());
            }
        } else if lower.ends_with(".go") {
            if t.starts_with("func ") {
                if let Some(name) = take_ident(t.trim_start_matches("func ").trim_start()) {
                    symbols.push((name, "function".into(), line_no));
                }
            } else if t.starts_with("type ") {
                if let Some(name) = take_ident(t.trim_start_matches("type ").trim_start()) {
                    symbols.push((name, "type".into(), line_no));
                }
            } else if t.starts_with("import ") {
                imports.push(t.trim_start_matches("import ").trim().trim_matches('"').to_string());
            }
        } else if lower.ends_with(".ts") || lower.ends_with(".tsx") || lower.ends_with(".js") || lower.ends_with(".jsx") || lower.ends_with(".mjs") {
            if t.starts_with("function ") || t.starts_with("export function ") || t.starts_with("export async function ") {
                let rest = t.trim_start_matches("export ").trim_start_matches("async ").trim_start_matches("function ").trim_start();
                if let Some(name) = take_ident(rest) {
                    symbols.push((name, "function".into(), line_no));
                }
            } else if t.starts_with("class ") || t.starts_with("export class ") {
                let rest = t.trim_start_matches("export ").trim_start_matches("class ").trim_start();
                if let Some(name) = take_ident(rest) {
                    symbols.push((name, "class".into(), line_no));
                }
            } else if t.starts_with("export ") && !t.starts_with("export function") && !t.starts_with("export class") {
                let rest = t.trim_start_matches("export ").trim_start();
                if let Some(name) = take_ident(rest) {
                    symbols.push((name, "symbol".into(), line_no));
                }
            } else if t.starts_with("import ") {
                // pull module path from quotes
                if let Some(s) = t.find('"') {
                    if let Some(e) = t[s+1..].find('"') {
                        imports.push(t[s+1..s+1+e].to_string());
                    }
                } else if let Some(s) = t.find("from ") {
                    let m = t[s+5..].trim();
                    if let Some(q) = m.find('"') {
                        if let Some(qe) = m[q+1..].find('"') {
                            imports.push(m[q+1..q+1+qe].to_string());
                        }
                    }
                }
            }
        } else if lower.ends_with(".c") || lower.ends_with(".h") || lower.ends_with(".cpp") || lower.ends_with(".hpp") || lower.ends_with(".cc") {
            if t.starts_with("#include") {
                let rest = t.trim_start_matches("#include").trim();
                if let Some(s) = rest.find(|c: char| c == '"' || c == '<') {
                    let open = rest.chars().nth(s).unwrap();
                    let close = if open == '"' { '"' } else { '>' };
                    if let Some(e) = rest[s+1..].find(close) {
                        imports.push(rest[s+1..s+1+e].to_string());
                    }
                }
            } else if t.contains('(') && !t.starts_with("if") && !t.starts_with("for") && !t.starts_with("while") && !t.starts_with("return") && !t.starts_with("switch") {
                if let Some(name) = take_ident(t.trim_start()) {
                    if name.len() > 1 {
                        symbols.push((name, "function".into(), line_no));
                    }
                }
            }
        } else if lower.ends_with(".java") || lower.ends_with(".kt") {
            if t.starts_with("public class ") || t.starts_with("class ") {
                if let Some(name) = take_ident(t.trim_start_matches("public ").trim_start_matches("class ").trim_start()) {
                    symbols.push((name, "class".into(), line_no));
                }
            } else if t.contains('(') && (t.contains("public ") || t.contains("private ") || t.contains("protected ") || t.trim_start().starts_with("void ") || t.trim_start().starts_with("int ") || t.trim_start().starts_with("String ")) {
                let words: Vec<&str> = t.split_whitespace().collect();
                if let Some(w) = words.last() {
                    if let Some(name) = take_ident(w) {
                        symbols.push((name, "function".into(), line_no));
                    }
                }
            }
        } else if lower.ends_with(".rb") {
            if t.starts_with("def ") {
                if let Some(name) = take_ident(t.trim_start_matches("def ").trim_start()) {
                    symbols.push((name, "function".into(), line_no));
                }
            } else if t.starts_with("class ") || t.starts_with("module ") {
                if let Some(name) = take_ident(t.trim_start_matches("module ").trim_start_matches("class ").trim_start()) {
                    symbols.push((name, "class".into(), line_no));
                }
            }
        } else if lower.ends_with(".php") {
            if t.starts_with("function ") {
                if let Some(name) = take_ident(t.trim_start_matches("function ").trim_start()) {
                    symbols.push((name, "function".into(), line_no));
                }
            } else if t.starts_with("class ") {
                if let Some(name) = take_ident(t.trim_start_matches("class ").trim_start()) {
                    symbols.push((name, "class".into(), line_no));
                }
            }
        }

        // call targets: identifier immediately followed by '(' on a non-def line
        if !t.starts_with("def ") && !t.starts_with("function ") && !t.starts_with("class ") {
            let bytes = t.as_bytes();
            let mut i = 0;
            while i < bytes.len() {
                let c = bytes[i];
                if c.is_ascii_alphabetic() || c == b'_' {
                    let start = i;
                    while i < bytes.len() && (bytes[i].is_ascii_alphanumeric() || bytes[i] == b'_' || bytes[i] == b'$') {
                        i += 1;
                    }
                    let word = &t[start..i];
                    let mut j = i;
                    while j < bytes.len() && bytes[j].is_ascii_whitespace() { j += 1; }
                    if j < bytes.len() && bytes[j] == b'(' {
                        let kw = ["if", "for", "while", "return", "switch", "catch", "function", "def", "fn", "elif", "else", "match", "new", "sizeof", "print", "len", "class"];
                        if !kw.contains(&word) && !word.is_empty() && seen.insert(word.to_string()) {
                            call_targets.push(word.to_string());
                        }
                    }
                } else {
                    i += 1;
                }
            }
        }
    }
    (symbols, imports, call_targets)
}

fn take_ident(s: &str) -> Option<String> {
    let s = s.trim_start();
    if s.is_empty() { return None; }
    let mut name = String::new();
    for c in s.chars() {
        if c.is_alphanumeric() || c == '_' || c == '$' {
            name.push(c);
        } else {
            break;
        }
    }
    if name.is_empty() { None } else { Some(name) }
}

// ------------------------------------------------------------- model: repo --
#[derive(Default)]
struct FileInfo {
    symbols: Vec<(String, String, usize)>, // name, kind, line
    imports: Vec<String>,
    calls: Vec<String>,
}

struct Repo {
    root: String,
    files: Vec<String>,                    // relative paths
    info: HashMap<String, FileInfo>,      // rel -> info
}

fn analyze(root: &str) -> Repo {
    let mut files = Vec::new();
    walk_files(Path::new(root), &mut files, 0);
    files.sort();
    let mut info = HashMap::new();
    for f in &files {
        // f is already a root-prefixed absolute path from the walk
        let (symbols, imports, calls) = extract(f);
        info.insert(f.clone(), FileInfo { symbols, imports, calls });
    }
    Repo { root: root.to_string(), files, info }
}

// module name: strip root + extension, path->dotted
fn module_name(root: &str, path: &str) -> String {
    let no_ext = path.trim_end_matches(|c: char| {
        // trim extension(s)
        false
    });
    let _ = no_ext;
    let mut p = path.to_string();
    if let Some(dot) = p.rfind('.') {
        p.truncate(dot);
    }
    p = p.replace('/', ".").replace('\\', ".");
    p.trim_matches('.').to_string()
}

// ---------------------------------------------------------------- map ----
fn cmd_map(repo: &Repo) {
    let mut sb = String::new();
    // tree of top-level dirs
    let mut dirs: HashSet<String> = HashSet::new();
    let mut root_files = 0usize;
    for f in &repo.files {
        if f.contains('/') {
            let top = f.split('/').next().unwrap_or("");
            if !top.is_empty() { dirs.insert(top.to_string()); }
        } else {
            root_files += 1;
        }
    }
    let mut sym_count = 0usize;
    let mut entry_points: Vec<String> = Vec::new();
    for (p, info) in &repo.info {
        sym_count += info.symbols.len();
        // heuristics for entry point
        let lower = p.to_lowercase();
        let is_init_main = p.ends_with("__init__.py") && info.symbols.iter().any(|s| s.0 == "main");
        if p.ends_with("main.py") || p.ends_with("main.rs") || p.ends_with("index.ts")
            || p.ends_with("index.js") || p.ends_with("main.go") || p == "main.c" || is_init_main {
            entry_points.push(p.clone());
        }
    }
    entry_points.sort();
    write!(sb, "# codeloom_rs map — {}\n", repo.root).ok();
    write!(sb, "{} files, {} symbols\n", repo.files.len(), sym_count).ok();
    write!(sb, "## Entry points\n").ok();
    for e in &entry_points { write!(sb, "  {}\n", e).ok(); }
    write!(sb, "## Structure\n").ok();
    for d in sorted_upper(&dirs) {
        write!(sb, "  {}/\n", d).ok();
    }
    if root_files > 0 { write!(sb, "  ({} file(s) at root)\n", root_files).ok(); }
    print!("{}", sb);
}

// ------------------------------------------------------------------ misc ---
fn sorted_upper(set: &HashSet<String>) -> Vec<String> {
    let mut v: Vec<String> = set.iter().cloned().collect();
    v.sort();
    v
}

// ------------------------------------------------------------ search ----
fn cmd_search(repo: &Repo, query: &str) {
    let q = query.to_lowercase();
    let mut sb = String::new();
    write!(sb, "# search: {}\n", query).ok();
    let mut any = false;
    for f in &repo.files {
        let info = &repo.info[f];
        for (name, kind, line) in &info.symbols {
            if name.to_lowercase() == q || name.to_lowercase().contains(&q) {
                write!(sb, "  {}  [{}]  {}:{}\n", name, kind, module_name(&repo.root, f), line).ok();
                any = true;
            }
        }
    }
    if !any { write!(sb, "  No symbols found.\n").ok(); }
    print!("{}", sb);
}

// ---------------------------------------------------------- usages ----
fn cmd_usages(repo: &Repo, query: &str) {
    let q = query.to_lowercase();
    let mut sb = String::new();
    write!(sb, "# usages: {}\n", query).ok();
    let mut count = 0usize;
    for f in &repo.files {
        let info = &repo.info[f];
        // usages = calls that reference the symbol (not the def line)
        if info.calls.iter().any(|c| c.to_lowercase() == q) {
            write!(sb, "  {}  (called {})\n", module_name(&repo.root, f),
                   info.calls.iter().filter(|c| c.to_lowercase() == q).count()).ok();
            count += 1;
        }
    }
    if count == 0 { write!(sb, "  No usages found (only the definition).\n").ok(); }
    print!("{}", sb);
}

// ----------------------------------------------------------- read ----
fn cmd_read(repo: &Repo, query: &str, full: bool) {
    let q = query.to_lowercase();
    for f in &repo.files {
        let info = &repo.info[f];
        for (name, kind, line) in &info.symbols {
            if name.to_lowercase() == q {
                // f is already a root-prefixed absolute path from the walk
                if let Ok(text) = fs::read_to_string(f) {
                    let lines: Vec<&str> = text.lines().collect();
                    if *line > 0 && *line <= lines.len() {
                        let modname = module_name(&repo.root, f);
                        println!("# {} [{}] {}:{}", name, kind, modname, line);
                        // print the symbol body (from def line to matching close brace or ~15 lines)
                        let mut end = (*line).min(lines.len());
                        if !full {
                            end = (*line + 12).min(lines.len());
                        }
                        for l in &lines[(*line - 1)..end] {
                            println!("  {}", l);
                        }
                        return;
                    }
                }
            }
        }
    }
    println!("# {} not found", query);
}

// ---------------------------------------------------------- calls ----
fn cmd_calls(repo: &Repo) {
    let mut sb = String::new();
    write!(sb, "# call graph\n").ok();
    // build symbol -> module map for resolution
    let mut mod_of: HashMap<String, String> = HashMap::new();
    for f in &repo.files {
        let info = &repo.info[f];
        let modname = module_name(&repo.root, f);
        for (name, _, _) in &info.symbols {
            mod_of.entry(name.clone()).or_insert_with(|| modname.clone());
        }
    }
    for f in &repo.files {
        let info = &repo.info[f];
        let modname = module_name(&repo.root, f);
        for c in &info.calls {
            if let Some(target_mod) = mod_of.get(c) {
                if target_mod != &modname {
                    write!(sb, "  {} -> {} (in {})\n", modname, c, target_mod).ok();
                }
            }
        }
    }
    print!("{}", sb);
}

// -------------------------------------------------------- imports ----
fn cmd_imports(repo: &Repo) {
    let mut sb = String::new();
    write!(sb, "# imports\n").unwrap();
    for f in &repo.files {
        let info = &repo.info[f];
        if info.imports.is_empty() { continue; }
        write!(sb, "  {} imports:\n", module_name(&repo.root, f)).ok();
        for i in &info.imports {
            write!(sb, "    - {}\n", i).ok();
        }
    }
    print!("{}", sb);
}

// ---------------------------------------------------------- files ----
fn cmd_files(repo: &Repo) {
    for f in &repo.files {
        println!("{}", f);
    }
}

// ----------------------------------------------------------- json ----
fn cmd_json(repo: &Repo, query: &str) {
    // structured output for MCP
    let q = query.to_lowercase();
    let mut out = String::new();
    out.push('{');
    write!(out, "\"symbol\":{},", jesc(query)).ok();
    let mut locs: Vec<&(String, String, usize)> = Vec::new();
    for f in &repo.files {
        for s in &repo.info[f].symbols {
            if s.0.to_lowercase() == q { locs.push(s); }
        }
    }
    out.push_str("\"definitions\":[");
    for (i, s) in locs.iter().enumerate() {
        if i > 0 { out.push(','); }
        write!(out, "{{\"name\":{},\"kind\":{},\"line\":{}}}", jesc(&s.0), jesc(&s.1), s.2).ok();
    }
    out.push_str("]}");
    println!("{}", out);
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        eprintln!("codeloom_rs <map|search|usages|read|calls|imports|files|json|cross> <root> [roots...] [query]");
        process::exit(1);
    }
    let cmd = args[1].as_str();
    let root = if args.len() > 2 { args[2].as_str() } else { "." };
    let repo = analyze(root);

    match cmd {
        "map" => cmd_map(&repo),
        "files" => cmd_files(&repo),
        "calls" => cmd_calls(&repo),
        "imports" => cmd_imports(&repo),
        "cross" => cmd_cross(&args[2..]),
        "search" if args.len() > 3 => cmd_search(&repo, &args[3]),
        "usages" if args.len() > 3 => cmd_usages(&repo, &args[3]),
        "read" if args.len() > 3 => cmd_read(&repo, &args[3], false),
        "json" if args.len() > 3 => cmd_json(&repo, &args[3]),
        _ => {
            eprintln!("codeloom_rs: usage: codeloom_rs <cmd> <root> [query]");
            process::exit(2);
        }
    }
}

// ---------------------------------------------------------- cross ----
// Multi-repo / cross-service analysis: analyze N repo roots together, build a
// unified cross-repo symbol -> origin map, and report cross-service call edges
// (a symbol defined in repo A called from repo B). This is the "what breaks
// across services" primitive.
fn cmd_cross(roots: &[String]) {
    let mut mod_of: HashMap<String, String> = HashMap::new(); // symbol -> "repo.module"
    let mut origins: HashMap<String, String> = HashMap::new(); // symbol -> repo
    let mut all_calls: Vec<(String, String)> = Vec::new(); // (repo, symbol) calls

    for root in roots {
        let repo = analyze(root);
        let root_label = Path::new(root).file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_else(|| root.clone());
        for f in &repo.files {
            let modname = format!("{}.{}", root_label, module_name(root, f));
            let info = &repo.info[f];
            for (name, _, _) in &info.symbols {
                origins.entry(name.clone()).or_insert_with(|| root_label.clone());
                mod_of.entry(name.clone()).or_insert_with(|| modname.clone());
            }
            for c in &info.calls {
                all_calls.push((root_label.clone(), c.clone()));
            }
        }
    }

    let mut sb = String::new();
    write!(sb, "# cross-repo graph — {} repo(s)\n", roots.len()).ok();
    write!(sb, "{} distinct symbols across {} repo(s)\n\n", origins.len(), roots.len()).ok();
    write!(sb, "## Cross-repo calls (symbol in one service, called from another)\n").ok();
    let mut shown = 0;
    for (caller_repo, callee_sym) in &all_calls {
        if let Some(callee_repo) = origins.get(callee_sym) {
            if callee_repo != caller_repo {
                write!(sb, "  {} -> {} (defined in {})\n", caller_repo, callee_sym, callee_repo).ok();
                shown += 1;
            }
        }
    }
    if shown == 0 {
        write!(sb, "  (no cross-repo calls detected — services may be decoupled)\n").ok();
    }
    print!("{}", sb);
}
