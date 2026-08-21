// codeloom_engine — optional tree-sitter engine for codeloom.
// Real AST parsing for 8 major languages (Python/JS/TS/Go/Rust/C/C++/Java),
// replacing the regex walker with genuine tree-sitter node traversal.
// Emits the same JSON-per-file contract as the Python/C/Rust cores so it plugs
// into the existing pipeline. Kept as a SEPARATE project so the zero-dep
// single-file codeloom.py and codeloom_rs stay dependency-free.
//
// Build:  cd engine_rs && cargo build --release
// Run:    ./target/release/codeloom_engine --list ROOT
//         ./target/release/codeloom_engine            (scan files from stdin)
//
// MIT © 2026 sloemo01.

use std::collections::HashSet;
use std::env;
use std::fs;
use std::io::Read;
use std::path::Path;
use std::process;

// Language dispatch by extension -> (tree_sitter::Language, symbol node kinds)
fn lang_for(path: &str) -> Option<(tree_sitter::Language, &'static [&'static str])> {
    let lower = path.to_lowercase();
    if lower.ends_with(".py") {
        Some((tree_sitter_python::LANGUAGE.into(), &["function_definition", "class_definition"]))
    } else if lower.ends_with(".js") || lower.ends_with(".mjs") || lower.ends_with(".jsx") {
        Some((tree_sitter_javascript::LANGUAGE.into(), &["function_declaration", "class_declaration", "method_definition"]))
    } else if lower.ends_with(".ts") || lower.ends_with(".tsx") {
        Some((tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into(), &["function_declaration", "class_declaration", "method_signature", "interface_declaration"]))
    } else if lower.ends_with(".go") {
        Some((tree_sitter_go::LANGUAGE.into(), &["function_declaration", "method_declaration", "type_declaration"]))
    } else if lower.ends_with(".rs") {
        Some((tree_sitter_rust::LANGUAGE.into(), &["function_item", "struct_item", "enum_item", "trait_item", "mod_item", "impl_item"]))
    } else if lower.ends_with(".c") || lower.ends_with(".h") {
        Some((tree_sitter_c::LANGUAGE.into(), &["function_definition"]))
    } else if lower.ends_with(".cpp") || lower.ends_with(".cc") || lower.ends_with(".hpp") {
        Some((tree_sitter_cpp::LANGUAGE.into(), &["function_definition", "class_specifier"]))
    } else if lower.ends_with(".java") {
        Some((tree_sitter_java::LANGUAGE.into(), &["method_declaration", "class_declaration", "interface_declaration", "constructor_declaration"]))
    } else if lower.ends_with(".rb") {
        Some((tree_sitter_ruby::LANGUAGE.into(), &["method", "class", "module"]))
    } else if lower.ends_with(".php") {
        Some((tree_sitter_php::LANGUAGE_PHP.into(), &["function_definition", "class_declaration"]))
    } else if lower.ends_with(".cs") {
        Some((tree_sitter_c_sharp::LANGUAGE.into(), &["method_declaration", "class_declaration", "interface_declaration", "constructor_declaration"]))
    } else if lower.ends_with(".scala") || lower.ends_with(".sc") {
        Some((tree_sitter_scala::LANGUAGE.into(), &["function_definition", "class_definition", "object_definition", "trait_definition"]))
    } else if lower.ends_with(".ex") || lower.ends_with(".exs") {
        Some((tree_sitter_elixir::LANGUAGE.into(), &["call"]))
    } else if lower.ends_with(".sh") || lower.ends_with(".bash") || lower.ends_with(".zsh") {
        Some((tree_sitter_bash::LANGUAGE.into(), &["function_definition"]))
    } else if lower.ends_with(".lua") {
        Some((tree_sitter_lua::LANGUAGE.into(), &["function_declaration"]))
    } else if lower.ends_with(".dart") {
        Some((tree_sitter_dart::LANGUAGE.into(), &["function_declaration", "class_declaration", "method_signature"]))
    } else if lower.ends_with(".hs") || lower.ends_with(".lhs") {
        Some((tree_sitter_haskell::LANGUAGE.into(), &["function", "class_declaration", "type_signature"]))
    } else if lower.ends_with(".html") || lower.ends_with(".htm") {
        Some((tree_sitter_html::LANGUAGE.into(), &["element", "script_element", "style_element"]))
    } else if lower.ends_with(".css") || lower.ends_with(".scss") || lower.ends_with(".less") {
        Some((tree_sitter_css::LANGUAGE.into(), &["rule_set", "class_selector", "id_selector"]))
    } else if lower.ends_with(".json") {
        Some((tree_sitter_json::LANGUAGE.into(), &["object", "pair"]))
    } else if lower.ends_with(".yaml") || lower.ends_with(".yml") {
        Some((tree_sitter_yaml::LANGUAGE.into(), &["block_mapping", "block_sequence"]))
    } else if lower.ends_with(".graphql") || lower.ends_with(".gql") {
        Some((tree_sitter_graphql::LANGUAGE.into(), &["object_type_definition", "field_definition"]))
    } else if lower.ends_with(".swift") {
        Some((tree_sitter_swift::LANGUAGE.into(), &["function_declaration", "class_declaration", "struct_declaration", "enum_declaration"]))
    } else if lower.ends_with(".d") {
        Some((tree_sitter_d::LANGUAGE.into(), &["function_declaration", "class_declaration"]))
    } else if lower.ends_with(".ml") || lower.ends_with(".mli") {
        Some((tree_sitter_ocaml::LANGUAGE_OCAML.into(), &["value_definition", "module_definition", "type_definition"]))
    } else if lower.ends_with(".sol") {
        Some((tree_sitter_solidity::LANGUAGE.into(), &["contract_declaration", "function_definition"]))
    } else {
        None
    }
}

fn is_code_file(path: &str) -> bool {
    lang_for(path).is_some()
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

fn walk_files(root: &Path, out: &mut Vec<String>, depth: usize) {
    if depth > 60 { return; }
    let rd = match fs::read_dir(root) { Ok(r) => r, Err(_) => return };
    for entry in rd.flatten() {
        let p = entry.path();
        let name = entry.file_name().to_string_lossy().to_string();
        if name == ".git" || name == "node_modules" || name == "target" || name == "__pycache__" || name == ".venv" || name == "venv" { continue; }
        if p.is_dir() {
            walk_files(&p, out, depth + 1);
        } else if p.is_file() && is_code_file(&p.to_string_lossy()) {
            out.push(p.to_string_lossy().to_string());
        }
    }
}

// Extract symbol name from a declaration node via child field/name pattern.
fn node_name(node: &tree_sitter::Node, src: &[u8]) -> String {
    // common: first child named (the identifier) — e.g. function_definition -> name
    if let Some(child) = node.child_by_field_name("name") {
        if let Ok(text) = child.utf8_text(src) {
            return text.to_string();
        }
    }
    // fallback: find first child that is an identifier-ish leaf
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.is_named() && child.child_count() == 0 {
            if let Ok(text) = child.utf8_text(src) {
                let t = text.trim();
                if !t.is_empty() && t.chars().all(|c| c.is_alphanumeric() || c == '_') {
                    return t.to_string();
                }
            }
        }
    }
    String::new()
}

fn scan_file(path: &str) -> Option<String> {
    let src = fs::read(path).ok()?;
    let (lang, kinds) = lang_for(path)?;
    let mut parser = tree_sitter::Parser::new();
    parser.set_language(&lang).ok()?;
    let tree = parser.parse(&src, None)?;
    let root = tree.root_node();

    let mut symbols: Vec<String> = Vec::new();
    let mut kinds_found: Vec<String> = Vec::new();
    let mut lines: Vec<usize> = Vec::new();
    let mut calls: Vec<String> = Vec::new();
    let mut seen_calls: HashSet<String> = HashSet::new();

    // walk the tree; collect declaration nodes for the language's kinds
    let mut stack: Vec<tree_sitter::Node> = vec![root];
    while let Some(n) = stack.pop() {
        // recurse
        let mut c = n.walk();
        for child in n.children(&mut c) {
            stack.push(child);
        }
        let kind = n.kind();
        if kinds.contains(&kind) {
            let name = node_name(&n, &src);
            if !name.is_empty() {
                symbols.push(name);
                kinds_found.push(kind.to_string());
                lines.push(n.start_position().row + 1);
            }
        }
        // call detection: identifier followed by args in a call/identifier node
        if kind == "call" || kind == "call_expression" {
            let mut cc = n.walk();
            for child in n.children(&mut cc) {
                let ck = child.kind();
                if ck == "identifier" || ck == "field_expression" || ck == "attribute" || ck == "name" {
                    if let Ok(text) = child.utf8_text(&src) {
                        // take last identifier segment for attr/method calls
                        let last = text.split('.').last().unwrap_or(text);
                        if seen_calls.insert(last.to_string()) {
                            calls.push(last.to_string());
                        }
                    }
                }
            }
        }
    }

    // build JSON line
    let mut line = String::from("{\"file\":");
    line.push_str(&jesc(path));
    line.push_str(",\"symbols\":[");
    for i in 0..symbols.len() {
        if i > 0 { line.push(','); }
        line.push_str(&format!("{{\"name\":{},\"kind\":{},\"line\":{}}}", jesc(&symbols[i]), jesc(&kinds_found[i]), lines[i]));
    }
    line.push_str("],\"imports\":[],\"calls\":[");
    for (i, c) in calls.iter().enumerate() {
        if i > 0 { line.push(','); }
        line.push_str(&jesc(c));
    }
    line.push_str("]}");
    Some(line)
}

fn cmd_scan() -> i32 {
    let mut input = String::new();
    if std::io::stdin().lock().read_to_string(&mut input).is_err() { return 1; }
    for f in input.lines() {
        let f = f.trim();
        if f.is_empty() { continue; }
        if let Some(line) = scan_file(f) {
            println!("{}", line);
        }
    }
    0
}

fn cmd_list(root: &str) -> i32 {
    let mut files = Vec::new();
    walk_files(Path::new(root), &mut files, 0);
    for f in files { println!("{}", f); }
    0
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() >= 3 && args[1] == "--list" {
        process::exit(cmd_list(&args[2]));
    }
    process::exit(cmd_scan());
}
