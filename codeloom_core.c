/*
 * codeloom_core.c — optional C-accelerated scanner for codeloom.
 *
 * Pure-Python codeloom is the zero-dependency default. When you want the
 * Linux-kernel-class speed of a compiled scanner, build this once:
 *
 *     cc -O3 -o codeloom_core codeloom_core.c
 *
 * then run codeloom with:
 *
 *   codeloom --engine c --index .
 *
 * It reads a list of file paths (one per line on stdin, or as argv), scans
 * each for symbol definitions and import statements, and emits a JSON array
 * of per-file results on stdout. No dependencies, no tree-sitter — a fast,
 * line-based multi-language scanner (C-compatible definition/import regexes).
 *
 * Output line per file:
 *   {"file":"...","symbols":[{"name":...,"kind":...,"line":N,
 *    "start_byte":N,"end_byte":N,"sig":"..."}],"imports":["...",...]}
 * The per-symbol byte range spans the def line through the body end
 * (dedent-based for Python, brace-balance for brace languages), which lets
 * --engine c build the same full persistent records (byte offsets + source)
 * as the pure-Python engine.
 *
 * This is honest: it accelerates the scanning that Python regex does, at C
 * speed. It is NOT a tree-sitter parser — that stays an optional Python
 * enrichment. Single-file, stdlib-only is preserved for the Python core.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <dirent.h>
#include <fcntl.h>
#include <unistd.h>
#ifdef __APPLE__
#include <sys/event.h>
#include <sys/time.h>
#endif

/* Max sizes (per-file — a single file won't have this many). */
#define MAX_LINE 16384
#define MAX_SYMS 16384
#define MAX_IMPORTS 16384
#define MAX_CALLS 16384
#define MAX_TARGETS 4096

typedef struct {
    char name[128];
    char kind[16];
    int line;               /* 1-based definition line */
    long start_byte;        /* byte offset of the definition line start */
    long end_byte;          /* byte offset just past the body end */
    char sig[256];          /* first non-empty line of the span (def line) */
} Sym;

typedef struct {
    char caller[128];
    char *targets[MAX_TARGETS];
    int n;
} CallRec;

typedef struct {
    char file[4096];
    Sym syms[MAX_SYMS];
    int n_syms;
    char *imports[MAX_IMPORTS];
    int n_imports;
    CallRec calls[MAX_CALLS];
    int n_calls;
} FileResult;

/* Trim surrounding whitespace in place. Safe on empty strings. */
static char *trim(char *s) {
    while (isspace((unsigned char)*s)) s++;
    size_t len = strlen(s);
    while (len > 0 && isspace((unsigned char)s[len - 1])) len--;
    s[len] = '\0';
    return s;
}

/* Lowercase a string in place. */
static void lower(char *s) {
    for (; *s; s++) *s = (char)tolower((unsigned char)*s);
}

/* Match a "def name(...)" line. Returns name (heap) or NULL. */
static char *match_def(const char *line) {
    const char *p = line;
    while (*p == ' ' || *p == '\t') p++;
    /* Python: async def name( or def name( */
    if (strncmp(p, "async", 5) == 0) {
        p += 5;
        while (*p == ' ' || *p == '\t') p++;
    }
    /* Python: def name( */
    if (strncmp(p, "def ", 4) == 0) {
        const char *n = p + 4;
        while (isspace((unsigned char)*n)) n++;
        const char *start = n;
        while (isalnum((unsigned char)*n) || *n == '_') n++;
        if (n > start) {
            size_t len = (size_t)(n - start);
            char *r = malloc(len + 1);
            memcpy(r, start, len);
            r[len] = '\0';
            return r;
        }
        return NULL;
    }
    /* class name: */
    if (strncmp(p, "class ", 6) == 0) {
        const char *n = p + 6;
        while (isspace((unsigned char)*n)) n++;
        const char *start = n;
        while (isalnum((unsigned char)*n) || *n == '_') n++;
        if (n > start) {
            size_t len = (size_t)(n - start);
            char *r = malloc(len + 1);
            memcpy(r, start, len);
            r[len] = '\0';
            return r;
        }
        return NULL;
    }
    return NULL;
}

/* Try to match a C/JS/Go/Rust/etc function definition.
   Heuristic: "name(" at start (after return type), or "name = function(",
   or "type name func(" for Go, or "pub fn name(". Returns name or NULL. */
static char *match_cdef(const char *line) {
    const char *p = line;
    while (*p == ' ' || *p == '\t' || *p == '*' || *p == '&') p++;
    /* Rust: pub fn name( */
    if (strncmp(p, "pub fn ", 7) == 0) {
        p += 7;
        const char *start = p;
        while (isalnum((unsigned char)*p) || *p == '_') p++;
        if (p > start) {
            size_t len = (size_t)(p - start);
            char *r = malloc(len + 1);
            memcpy(r, start, len); r[len] = '\0';
            return r;
        }
        return NULL;
    }
    /* Go: func name( */
    if (strncmp(p, "func ", 5) == 0) {
        p += 5;
        while (isspace((unsigned char)*p)) p++;
        const char *start = p;
        while (isalnum((unsigned char)*p) || *p == '_') p++;
        if (p > start) {
            size_t len = (size_t)(p - start);
            char *r = malloc(len + 1);
            memcpy(r, start, len); r[len] = '\0';
            return r;
        }
        return NULL;
    }
    /* JS/TS: "name = function" or "function name(" or "name: function" */
    /* find "function" and backtrack to the name, or read name after "function" */
    const char *fn = strstr(line, "function");
    if (fn) {
        /* after "function" there may be a name */
        const char *n = fn + 8;
        while (*n == ' ' || *n == '\t') n++;
        if (isalnum((unsigned char)*n) || *n == '_') {
            const char *start = n;
            while (isalnum((unsigned char)*n) || *n == '_') n++;
            size_t len = (size_t)(n - start);
            char *r = malloc(len + 1);
            memcpy(r, start, len); r[len] = '\0';
            return r;
        }
        /* anonymous function — maybe "name = function" */
        const char *b = fn - 1;
        while (b >= line && (*b == ' ' || *b == '\t')) b--;
        const char *nb = b;
        while (nb >= line && (isalnum((unsigned char)*nb) || *nb == '_')) nb--;
        if (b > nb) {
            size_t len = (size_t)(b - nb);
            char *r = malloc(len + 1);
            memcpy(r, nb + 1, len); r[len] = '\0';
            return r;
        }
        return NULL;
    }
    /* C/C++/Rust/JS: "name(" where name is a plain identifier at start,
       and there's a '{' or the line ends with ')' (function decl). Avoid
       keywords: if/for/while/switch/return. */
    {
        const char *start = p;
        while (isalnum((unsigned char)*p) || *p == '_') p++;
        if (p > start && *p == '(') {
            size_t len = (size_t)(p - start);
            char word[256];
            if (len < 256) {
                memcpy(word, start, len); word[len] = '\0';
                static const char *kw[] = {"if","for","while","switch","return",
                                           "sizeof","typeof","int","char","void",
                                           "struct","union","enum","sizeof"};
                for (int i = 0; i < 14; i++) {
                    if (strcmp(word, kw[i]) == 0) return NULL;
                }
                char *r = malloc(len + 1);
                memcpy(r, word, len); r[len] = '\0';
                return r;
            }
        }
    }
    return NULL;
}

/* Rust: struct / enum / trait / impl / fn / const / static / type / mod.
   Returns symbol name (heap) + kind via out_kind, or NULL. */
static char *match_rust(const char *line, char *kind_out) {
    const char *p = line;
    while (*p == ' ' || *p == '\t' || *p == '#' || *p == '!') p++;
    /* skip attribute lines like #[derive(...)] */
    if (*p == '[') return NULL;
    /* strip a leading 'pub ' / 'pub(crate) ' visibility */
    if (strncmp(p, "pub ", 4) == 0) {
        p += 4;
        while (*p == ' ' || *p == '\t') p++;
    } else if (strncmp(p, "pub(", 4) == 0) {
        while (*p && *p != ')') p++;
        if (*p == ')') p++;
        while (*p == ' ' || *p == '\t') p++;
    }
    const char *kw[] = {"fn ", "struct ", "enum ", "trait ", "impl ", "const ",
                        "static ", "mod ", "type ", "union "};
    const char *kwkind[] = {"fn", "struct", "enum", "trait", "impl", "const",
                            "static", "mod", "type", "union"};
    for (int i = 0; i < 10; i++) {
        size_t klen = strlen(kw[i]);
        if (strncmp(p, kw[i], klen) == 0) {
            /* impl blocks: 'impl Foo for Bar {' — capture the first type */
            if (kw[i][0] == 'i') {
                const char *t = p + klen;
                while (*t == ' ' || *t == '\t') t++;
                if (*t && *t != '{') {
                    const char *start = t;
                    while (isalnum((unsigned char)*t) || *t == '_') t++;
                    size_t len = (size_t)(t - start);
                    char *r = malloc(len + 1);
                    memcpy(r, start, len); r[len] = '\0';
                    snprintf(kind_out, 16, "impl");
                    return r;
                }
                return NULL;
            }
            const char *n = p + klen;
            while (*n == ' ' || *n == '\t') n++;
            const char *start = n;
            while (isalnum((unsigned char)*n) || *n == '_') n++;
            if (n > start) {
                size_t len = (size_t)(n - start);
                char *r = malloc(len + 1);
                memcpy(r, start, len); r[len] = '\0';
                snprintf(kind_out, 16, "%s", kwkind[i]);
                return r;
            }
            return NULL;
        }
    }
    return NULL;
}

/* TypeScript / JavaScript: export function/const/class/interface/type/async. */
static char *match_ts(const char *line, char *kind_out) {
    const char *p = line;
    while (*p == ' ' || *p == '\t') p++;
    const char *n = p;
    if (strncmp(n, "export ", 7) == 0) n += 7;
    while (*n == ' ' || *n == '\t') n++;
    /* export default function name */
    if (strncmp(n, "default ", 8) == 0) n += 8;
    while (*n == ' ' || *n == '\t') n++;
    if (strncmp(n, "async ", 6) == 0) n += 6;
    while (*n == ' ' || *n == '\t') n++;
    if (strncmp(n, "function ", 9) == 0) {
        const char *s = n + 9;
        while (*s == ' ' || *s == '\t') s++;
        if (isalnum((unsigned char)*s) || *s == '_') {
            const char *start = s;
            while (isalnum((unsigned char)*s) || *s == '_') s++;
            size_t len = (size_t)(s - start);
            char *r = malloc(len + 1); memcpy(r, start, len); r[len] = '\0';
            snprintf(kind_out, 16, "function"); return r;
        }
        return NULL;
    }
    if (strncmp(n, "class ", 6) == 0 || strncmp(n, "interface ", 10) == 0 ||
        strncmp(n, "enum ", 5) == 0 || strncmp(n, "type ", 5) == 0 ||
        strncmp(n, "abstract class ", 15) == 0) {
        const char *s = n;
        while (*s && *s != ' ' && *s != '\t') s++;
        while (*s == ' ' || *s == '\t') s++;
        if (isalnum((unsigned char)*s) || *s == '_') {
            const char *start = s;
            while (isalnum((unsigned char)*s) || *s == '_') s++;
            size_t len = (size_t)(s - start);
            char *r = malloc(len + 1); memcpy(r, start, len); r[len] = '\0';
            snprintf(kind_out, 16, "class"); return r;
        }
        return NULL;
    }
    /* const/let/var name = arrow fn or function — only when it IS a function */
    if (strncmp(n, "const ", 6) == 0 || strncmp(n, "let ", 4) == 0 ||
        strncmp(n, "var ", 4) == 0) {
        /* must be a function: `name = (...) =>` or `name = function` */
        const char *arrow = strstr(line, "=>");
        const char *fnk = strstr(line, "= function");
        const char *fnk2 = strstr(line, "=async ");
        if (!arrow && !fnk && !fnk2) return NULL;
        const char *s = n;
        while (*s && *s != ' ' && *s != '\t') s++;
        while (*s == ' ' || *s == '\t') s++;
        const char *start = s;
        while (isalnum((unsigned char)*s) || *s == '_' || *s == '$') s++;
        if (s > start) {
            size_t len = (size_t)(s - start);
            char *r = malloc(len + 1); memcpy(r, start, len); r[len] = '\0';
            snprintf(kind_out, 16, "function"); return r;
        }
        return NULL;
    }
    return NULL;
}

/* Detect an import/include line and return the imported target (heap) or NULL.
   Handles: import x; import {a} from 'x'; require('x'); #include <x.h>; using x; */
static char *match_import(const char *line) {
    const char *p = trim((char *)line);
    char low[MAX_LINE];
    strncpy(low, p, MAX_LINE - 1); low[MAX_LINE - 1] = '\0';
    lower(low);
    /* #include */
    if (strncmp(low, "#include", 8) == 0) {
        const char *q = strchr(p, '<');
        const char *q2 = strchr(p, '"');
        const char *b = q ? q : q2;
        if (b) {
            b++;
            const char *e = strchr(b, '>');
            if (!e) e = strchr(b, '"');
            if (e) {
                size_t len = (size_t)(e - b);
                char *r = malloc(len + 1);
                memcpy(r, b, len); r[len] = '\0';
                return r;
            }
        }
        return NULL;
    }
    /* require('x') */
    if (strstr(low, "require(")) {
        const char *q = strchr(p, '\'');
        if (!q) q = strchr(p, '"');
        if (q) {
            q++;
            const char *e = strchr(q, '\'');
            if (!e) e = strchr(q, '"');
            if (e) {
                size_t len = (size_t)(e - q);
                char *r = malloc(len + 1);
                memcpy(r, q, len); r[len] = '\0';
                return r;
            }
        }
        return NULL;
    }
    /* import ... from 'x' or import 'x' */
    if (strncmp(low, "import", 6) == 0) {
        const char *from = strstr(low, "from ");
        const char *q = NULL;
        if (from) {
            const char *src = from + 5;
            while (*src == ' ' || *src == '\t') src++;
            if (*src == '\'' || *src == '"') q = src;
        } else {
            const char *src = p + 6;
            while (*src == ' ' || *src == '\t') src++;
            if (*src == '\'' || *src == '"') q = src;
        }
        if (q) {
            q++;
            const char *e = strchr(q, '\'');
            if (!e) e = strchr(q, '"');
            if (e) {
                size_t len = (size_t)(e - q);
                char *r = malloc(len + 1);
                memcpy(r, q, len); r[len] = '\0';
                return r;
            }
        }
        return NULL;
    }
    /* Rust: use crate::x; use std::collections::HashMap; use x::y::z; */
    if (strncmp(low, "use ", 4) == 0 && strstr(low, "::")) {
        const char *q = p + 4;
        while (*q == ' ' || *q == '\t') q++;
        const char *start = q;
        while (isalnum((unsigned char)*q) || *q == '_' || *q == ':' || *q == ':') q++;
        if (q > start) {
            size_t len = (size_t)(q - start);
            char *r = malloc(len + 1);
            memcpy(r, start, len); r[len] = '\0';
            return r;
        }
        return NULL;
    }
    return NULL;
}

static int is_c_ext(const char *ext) {
    /* Broad extension registry (~150 languages). Every language gets the same
       line-based symbol + import extraction + cross-file resolution into one
       graph, no per-language setup. This is honest breadth: recognition and
       structural extraction, not tree-sitter depth for each — those are
       opt-in where available. */
    static const char *exts[] = {
        /* web / frontend */
        ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".jsm", ".es6", ".es", ".html",
        ".htm", ".css", ".scss", ".sass", ".less", ".styl", ".vue", ".svelte",
        ".astro", ".jsx", ".qwik", ".liquid", ".twig", ".ejs", ".hbs", ".handlebars",
        ".pug", ".jade", ".php", ".php3", ".php4", ".php5", ".phtml", ".pl", ".pm",
        ".t", ".py", ".pyw", ".pyi", ".rb", ".rbw", ".rake", ".gemspec", ".erl",
        ".hrl", ".ex", ".exs", ".eex", ".leex", ".heex",
        /* systems / native */
        ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".c++", ".hh", ".hxx", ".ino",
        ".rs", ".rlib", ".go", ".goc", ".swift", ".m", ".mm", ".metal", ".cu",
        ".cuh", ".s", ".asm", ".d", ".ada", ".adb", ".ads", ".f", ".f90", ".f95",
        ".f03", ".f08", ".v", ".sv", ".vh", ".vhd",
        /* JVM / .NET */
        ".java", ".kt", ".kts", ".scala", ".sc", ".groovy", ".gradle", ".clj",
        ".cljs", ".cljc", ".edn", ".cs", ".vb", ".fs", ".fsx", ".fsi", ".fsharp",
        ".razor", ".csproj", ".vbproj", ".xaml",
        /* data / query / config-as-code */
        ".sql", ".plsql", ".pgsql", ".psql", ".dart", ".graphql", ".gql", ".proto",
        ".sol", ".solidity", ".tf", ".tfvars", ".hcl", ".nix", ".bzl", ".mk",
        ".cmake", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
        ".dockerfile", ".xaml",
        /* scripting / shell / misc */
        ".sh", ".bash", ".zsh", ".ksh", ".fish", ".ps1", ".bat", ".cmd", ".awk",
        ".sed", ".pl", ".r", ".rmd", ".m", ".jl", ".jl", ".octave", ".scilab",
        ".cob", ".cbl", ".pco", ".cobol", ".for", ".f", ".pas", ".pp", ".dpr",
        ".nix", ".nw", ".e", ".eq", ".coffee", ".litcoffee", ".s", ".sv", ".v",
        ".co", ".tcl", ".zsh", ".jq", ".awk", ".sed", ".php", ".htm",
        ".ets", ".metal", ".liquid",
    };
    for (size_t i = 0; i < sizeof(exts) / sizeof(exts[0]); i++) {
        if (strcmp(ext, exts[i]) == 0) return 1;
    }
    return 0;
}

/* Record a call target under the current caller (dedupe). */
static void add_call(FileResult *fr, const char *caller, const char *callee) {
    if (fr->n_calls >= MAX_CALLS) return;
    int cidx = -1;
    for (int i = 0; i < fr->n_calls; i++) {
        if (strcmp(fr->calls[i].caller, caller) == 0) { cidx = i; break; }
    }
    if (cidx == -1) {
        cidx = fr->n_calls;
        snprintf(fr->calls[cidx].caller, sizeof(fr->calls[0].caller), "%s", caller);
        fr->calls[cidx].n = 0;
        fr->n_calls++;
    }
    if (fr->calls[cidx].n >= MAX_TARGETS) return;
    for (int i = 0; i < fr->calls[cidx].n; i++) {
        if (strcmp(fr->calls[cidx].targets[i], callee) == 0) return;
    }
    char *copy = strdup(callee);
    if (copy) fr->calls[cidx].targets[fr->calls[cidx].n++] = copy;
}

/* Scan one file. Returns 1 if OK, 0 on error. */
static int scan_file(const char *path, const char *ext, FileResult *fr) {
    FILE *fp = fopen(path, "rb");
    if (!fp) return 0;
    snprintf(fr->file, sizeof(fr->file), "%s", path);
    fr->n_syms = 0;
    fr->n_imports = 0;
    fr->n_calls = 0;
    char line[MAX_LINE];
    int is_py = (strcmp(ext, ".py") == 0);
    int is_rust = (strcmp(ext, ".rs") == 0);
    int is_ts = (strcmp(ext, ".ts") == 0 || strcmp(ext, ".tsx") == 0 ||
                 strcmp(ext, ".js") == 0 || strcmp(ext, ".jsx") == 0 ||
                 strcmp(ext, ".mjs") == 0 || strcmp(ext, ".cjs") == 0 ||
                 strcmp(ext, ".svelte") == 0 || strcmp(ext, ".vue") == 0);
    char current_func[128] = "";
    /* Open symbol-body spans. Python closes on dedent below the def's own
       indent; brace languages close when the brace depth returns to zero.
       This gives each symbol a real byte range (def line .. body end) so the
       persistent index can serve full source slices like the Python engine. */
#define MAX_OPEN 512
    int open_sym[MAX_OPEN];
    int open_indent[MAX_OPEN];
    int open_depth[MAX_OPEN];
    int n_open = 0;
    long line_start = 0;   /* byte offset of the current line start */
    int lineno = 0;        /* 1-based */
    while (fgets(line, sizeof(line), fp)) {
        int this_len = (int)strlen(line);
        int this_indent = 0;
        while (this_indent < this_len &&
               (line[this_indent] == ' ' || line[this_indent] == '\t'))
            this_indent++;
        int brace_delta = 0;
        for (int b = 0; b < this_len; b++) {
            if (line[b] == '{') brace_delta++;
            else if (line[b] == '}') brace_delta--;
        }
        char *t = trim(line);
        int is_code = (t[0] != '\0' && t[0] != '#' && t[0] != '/' &&
                       t[0] != '*' && t[0] != ';');
        /* close spans whose body ended on this line */
        if (is_py) {
            while (n_open > 0 && is_code &&
                   this_indent <= open_indent[n_open - 1]) {
                fr->syms[open_sym[n_open - 1]].end_byte = line_start;
                n_open--;
            }
        } else {
            while (n_open > 0) {
                int top = n_open - 1;
                open_depth[top] += brace_delta;
                if (open_depth[top] > 0) break;
                /* depth back to zero: body ends at end of this line */
                fr->syms[open_sym[top]].end_byte = line_start + this_len;
                n_open--;
                if (brace_delta < 0) break;  /* only one def closes per '}' */
            }
        }
        if (!is_code) {
            goto next_line;
        }
        char *name = NULL;
        if (is_py) {
            name = match_def(t);
        } else if (is_rust) {
            char kind[16];
            name = match_rust(t, kind);
        } else if (is_ts) {
            char kind[16];
            name = match_ts(t, kind);
        } else {
            name = match_cdef(t);
        }
        if (name && fr->n_syms < MAX_SYMS) {
            int i = fr->n_syms++;
            snprintf(fr->syms[i].name, sizeof(fr->syms[0].name), "%s", name);
            strcpy(fr->syms[i].kind, "function");
            fr->syms[i].line = lineno + 1;   /* 1-based, matches Python engine */
            fr->syms[i].start_byte = line_start;
            fr->syms[i].end_byte = line_start + this_len;
            snprintf(fr->syms[i].sig, sizeof(fr->syms[0].sig), "%s", t);
            snprintf(current_func, sizeof(current_func), "%s", name);
            if (n_open < MAX_OPEN) {
                open_sym[n_open] = i;
                open_indent[n_open] = this_indent;
                if (is_py) {
                    open_depth[n_open] = 1; /* stays open until dedent */
                    n_open++;
                } else {
                    open_depth[n_open] = brace_delta;
                    n_open++;
                    /* one-line body (e.g. `int f(){return 1;}`): closed
                       immediately; a pure decl (`void f(int);`) closes too */
                    if (brace_delta <= 0) {
                        fr->syms[i].end_byte = line_start + this_len;
                        n_open--;
                    }
                }
            }
            free(name);
        }
        char *imp = match_import(t);
        if (imp && fr->n_imports < MAX_IMPORTS) {
            int dup = 0;
            for (int i = 0; i < fr->n_imports; i++) {
                if (strcmp(fr->imports[i], imp) == 0) { dup = 1; break; }
            }
            if (!dup) {
                fr->imports[fr->n_imports] = imp;
                fr->n_imports++;
            } else {
                free(imp);
            }
        } else if (imp) {
            free(imp);
        }
        /* record calls: "word(" within the current function */
        if (current_func[0] != '\0') {
            const char *p = t;
            while (*p) {
                if (isalpha((unsigned char)*p) || *p == '_') {
                    const char *start = p;
                    while (isalnum((unsigned char)*p) || *p == '_') p++;
                    /* skip whitespace to '(' */
                    const char *q = p;
                    while (*q == ' ' || *q == '\t') q++;
                    if (*q == '(' && (size_t)(p - start) < 128) {
                        char callee[128];
                        memcpy(callee, start, (size_t)(p - start));
                        callee[p - start] = '\0';
                        if (strcmp(callee, current_func) != 0 &&
                            strcmp(callee, "if") != 0 && strcmp(callee, "for") != 0 &&
                            strcmp(callee, "while") != 0 && strcmp(callee, "switch") != 0 &&
                            strcmp(callee, "return") != 0 && strcmp(callee, "sizeof") != 0) {
                            add_call(fr, current_func, callee);
                        }
                    }
                    p = q;
                } else {
                    p++;
                }
            }
        }
next_line:
        line_start += this_len;
        lineno++;
    }
    /* EOF: close every still-open span at end-of-file */
    while (n_open > 0) {
        fr->syms[open_sym[--n_open]].end_byte = line_start;
    }
    fclose(fp);
    return 1;
}

/* Emit a JSON string literal with proper escaping (quotes, backslash,
   control chars). The old code printed raw %s — a file path or identifier
   containing a quote or backslash produced invalid JSON that the Python
   side silently dropped. */
static void json_quote(FILE *out, const char *s) {
    fputc('"', out);
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        switch (*p) {
            case '"':  fputs("\\\"", out); break;
            case '\\': fputs("\\\\", out); break;
            case '\n': fputs("\\n", out); break;
            case '\r': fputs("\\r", out); break;
            case '\t': fputs("\\t", out); break;
            default:
                if (*p < 0x20) fprintf(out, "\\u%04x", *p);
                else fputc(*p, out);
        }
    }
    fputc('"', out);
}

static void print_json(const FileResult *fr) {
    printf("{\"file\":");
    json_quote(stdout, fr->file);
    printf(",\"symbols\":[");
    for (int i = 0; i < fr->n_syms; i++) {
        if (i) printf(",");
        printf("{\"name\":");
        json_quote(stdout, fr->syms[i].name);
        printf(",\"kind\":");
        json_quote(stdout, fr->syms[i].kind);
        printf(",\"line\":%d,\"start_byte\":%ld,\"end_byte\":%ld",
               fr->syms[i].line, fr->syms[i].start_byte, fr->syms[i].end_byte);
        if (fr->syms[i].sig[0] != '\0') {
            printf(",\"sig\":");
            json_quote(stdout, fr->syms[i].sig);
        }
        printf("}");
    }
    printf("],\"imports\":[");
    for (int i = 0; i < fr->n_imports; i++) {
        if (i) printf(",");
        json_quote(stdout, fr->imports[i]);
    }
    printf("],\"calls\":[");
    for (int i = 0; i < fr->n_calls; i++) {
        if (i) printf(",");
        printf("{\"caller\":");
        json_quote(stdout, fr->calls[i].caller);
        printf(",\"targets\":[");
        for (int j = 0; j < fr->calls[i].n; j++) {
            if (j) printf(",");
            json_quote(stdout, fr->calls[i].targets[j]);
        }
        printf("]}");
    }
    printf("]}\n");
}

/* Native file watcher: kqueue (macOS/BSD) or inotify (Linux). Watches a
   directory tree recursively and prints code file changes as JSON lines
   matching the --watch-merge contract:
     {"file":"<abs path>"}                     modified or created
     {"file":"<abs path>","removed":true}      deleted or renamed away
   The banner goes to stderr so stdout stays a clean JSON pipe. */
static void watch_emit_file(const char *path) {
    printf("{\"file\":");
    json_quote(stdout, path);
    printf("}\n");
    fflush(stdout);
}

static void watch_emit_removed(const char *path) {
    printf("{\"file\":");
    json_quote(stdout, path);
    printf(",\"removed\":true}\n");
    fflush(stdout);
}

#ifdef __APPLE__
/* Recursive kqueue watcher: registers every file and directory under root,
   catching content edits (NOTE_WRITE) AND create/delete/rename. When a dir
   changes, re-scan it to pick up newly created files. */
#define MAX_WATCH 8192
#define MAX_DIRS 4096
static int g_fds[MAX_WATCH];
static char *g_paths[MAX_WATCH];
static int g_nfds = 0;
static int g_dfds[MAX_DIRS];
static char *g_dpaths[MAX_DIRS];
static int g_ndirs = 0;

static int add_file_watch(int kq, const char *full) {
    if (g_nfds >= MAX_WATCH) return 0;
    int fd = open(full, O_RDONLY);
    if (fd < 0) return 0;
    struct kevent ev;
    EV_SET(&ev, (uintptr_t)fd, EVFILT_VNODE, EV_ADD | EV_ENABLE | EV_CLEAR,
           NOTE_WRITE | NOTE_EXTEND | NOTE_RENAME | NOTE_DELETE, 0, NULL);
    if (kevent(kq, &ev, 1, NULL, 0, NULL) < 0) { close(fd); return 0; }
    g_fds[g_nfds] = fd;
    g_paths[g_nfds] = strdup(full);
    g_nfds++;
    return 1;
}

static void drop_file_watch(int i) {
    if (i < 0 || i >= g_nfds) return;
    close(g_fds[i]);
    free(g_paths[i]);
    g_fds[i] = g_fds[g_nfds - 1];
    g_paths[i] = g_paths[g_nfds - 1];
    g_nfds--;
}

static int path_is_watched_file(const char *full) {
    for (int i = 0; i < g_nfds; i++)
        if (strcmp(g_paths[i], full) == 0) return 1;
    return 0;
}

static int dir_is_watched(const char *dir) {
    for (int i = 0; i < g_ndirs; i++)
        if (strcmp(g_dpaths[i], dir) == 0) return 1;
    return 0;
}

/* Track a directory fd in the dir table. */
static void add_dir_watch(int kq, const char *dir) {
    if (g_ndirs >= MAX_DIRS || dir_is_watched(dir)) return;
    int fd = open(dir, O_RDONLY);
    if (fd < 0) return;
    struct kevent ev;
    EV_SET(&ev, (uintptr_t)fd, EVFILT_VNODE, EV_ADD | EV_ENABLE | EV_CLEAR,
           NOTE_WRITE | NOTE_DELETE | NOTE_RENAME, 0, NULL);
    if (kevent(kq, &ev, 1, NULL, 0, NULL) < 0) { close(fd); return; }
    g_dfds[g_ndirs] = fd;
    g_dpaths[g_ndirs] = strdup(dir);
    g_ndirs++;
}

static void drop_dir_watch(int i) {
    if (i < 0 || i >= g_ndirs) return;
    close(g_dfds[i]);
    free(g_dpaths[i]);
    g_dfds[i] = g_dfds[g_ndirs - 1];
    g_dpaths[i] = g_dpaths[g_ndirs - 1];
    g_ndirs--;
}

/* Register watches for dir and everything under it. When `emit` is set,
   newly found code files are emitted as created (used when a dir event
   fires); the initial scan passes emit=0 so startup stays quiet. */
static void scan_dir_tree(int kq, const char *dir, int emit) {
    add_dir_watch(kq, dir);
    DIR *dp = opendir(dir);
    if (!dp) return;
    struct dirent *de;
    while ((de = readdir(dp)) != NULL) {
        if (de->d_name[0] == '.') continue;
        char full[4096];
        snprintf(full, sizeof(full), "%s/%s", dir, de->d_name);
        if (de->d_type == DT_DIR) {
            scan_dir_tree(kq, full, emit);
        } else if (de->d_type == DT_REG) {
            const char *ext = strrchr(de->d_name, '.');
            if (ext && is_c_ext(ext) && !path_is_watched_file(full)) {
                if (add_file_watch(kq, full) && emit) watch_emit_file(full);
            }
        }
    }
    closedir(dp);
}

static void handle_dir_event(int kq, int didx) {
    /* files created inside dir; new subdirs get registered recursively */
    scan_dir_tree(kq, g_dpaths[didx], 1);
}

static void watch_dir(const char *root) {
    int kq = kqueue();
    if (kq < 0) { perror("kqueue"); return; }
    scan_dir_tree(kq, root, 0);
    fprintf(stderr, "watching %s (kqueue, %d files)…\n", root, g_nfds);
    fflush(stderr);
    struct kevent out;
    for (;;) {
        struct timespec ts = {0, 250000000}; /* 250ms */
        int n = kevent(kq, NULL, 0, &out, 1, &ts);
        if (n <= 0) continue;
        int fi = -1;
        for (int i = 0; i < g_nfds; i++)
            if ((uintptr_t)g_fds[i] == out.ident) { fi = i; break; }
        if (fi >= 0) {
            if (out.fflags & (NOTE_DELETE | NOTE_RENAME)) {
                watch_emit_removed(g_paths[fi]);
                drop_file_watch(fi);
            } else {
                watch_emit_file(g_paths[fi]);
            }
            continue;
        }
        int di = -1;
        for (int i = 0; i < g_ndirs; i++)
            if ((uintptr_t)g_dfds[i] == out.ident) { di = i; break; }
        if (di >= 0) {
            if (out.fflags & NOTE_DELETE) {
                /* dir removed: emit removed for every registered file under it */
                const char *dp = g_dpaths[di];
                size_t dlen = strlen(dp);
                for (int i = g_nfds - 1; i >= 0; i--) {
                    if (strncmp(g_paths[i], dp, dlen) == 0 &&
                        (g_paths[i][dlen] == '/' || g_paths[i][dlen] == '\0')) {
                        watch_emit_removed(g_paths[i]);
                        drop_file_watch(i);
                    }
                }
                drop_dir_watch(di);
            } else {
                handle_dir_event(kq, di);
            }
        }
    }
}
#elif defined(__linux__)
#include <sys/inotify.h>
#define MAX_WD 4096
static int g_wds[MAX_WD];
static char *g_wpaths[MAX_WD];
static int g_nwds = 0;

static int add_inotify_watch(int fd, const char *dir) {
    if (g_nwds >= MAX_WD) return 0;
    int wd = inotify_add_watch(fd, dir, IN_MODIFY | IN_CLOSE_WRITE | IN_CREATE |
                               IN_DELETE | IN_MOVED_TO | IN_MOVED_FROM);
    if (wd < 0) return 0;
    g_wds[g_nwds] = wd;
    g_wpaths[g_nwds] = strdup(dir);
    g_nwds++;
    return 1;
}

static const char *wd_path(int wd) {
    for (int i = 0; i < g_nwds; i++)
        if (g_wds[i] == wd) return g_wpaths[i];
    return NULL;
}

static void add_inotify_tree(int fd, const char *dir) {
    add_inotify_watch(fd, dir);
    DIR *dp = opendir(dir);
    if (!dp) return;
    struct dirent *de;
    while ((de = readdir(dp)) != NULL) {
        if (de->d_name[0] == '.') continue;
        char full[4096];
        snprintf(full, sizeof(full), "%s/%s", dir, de->d_name);
        if (de->d_type == DT_DIR) add_inotify_tree(fd, full);
    }
    closedir(dp);
}

static void watch_dir(const char *root) {
    int fd = inotify_init();
    if (fd < 0) { perror("inotify"); return; }
    add_inotify_tree(fd, root);
    fprintf(stderr, "watching %s (inotify)…\n", root);
    fflush(stderr);
    char buf[8192];
    for (;;) {
        ssize_t len = read(fd, buf, sizeof(buf));
        if (len < 0) continue;
        for (ssize_t i = 0; i < len; ) {
            struct inotify_event *ev = (struct inotify_event *)&buf[i];
            if (ev->len && ev->name[0] != '.') {
                const char *base = wd_path(ev->wd);
                char full[4096];
                if (base) snprintf(full, sizeof(full), "%s/%s", base, ev->name);
                else snprintf(full, sizeof(full), "%s", ev->name);
                int is_dir = (ev->mask & IN_ISDIR) != 0;
                const char *ext = strrchr(ev->name, '.');
                int code = (!is_dir && ext && is_c_ext(ext));
                if ((ev->mask & (IN_DELETE | IN_MOVED_FROM)) && !is_dir) {
                    if (code) { watch_emit_removed(full); }
                } else if (ev->mask & (IN_CREATE | IN_MOVED_TO | IN_CLOSE_WRITE)) {
                    if (is_dir) {
                        add_inotify_tree(fd, full);   /* watch the new tree */
                    } else if (code) {
                        watch_emit_file(full);
                    }
                }
            }
            i += sizeof(struct inotify_event) + ev->len;
        }
    }
    close(fd);
}
#else
static void watch_dir(const char *root) {
    fprintf(stderr, "watch: platform not supported (kqueue/inotify)\n");
}
#endif

/* --serve ROOT: load the JSON index once into memory, answer lookups instantly.
   For each `symbol` line on stdin, find `"symbol":` in the buffer and print the
   matching location line. This gives resident sub-ms lookups from a C process
   with zero Python startup per query. */
static void serve_index(const char *root) {
    char path[4096];
    snprintf(path, sizeof(path), "%s/.codeloom-index.json", root);
    FILE *fp = fopen(path, "rb");
    if (!fp) { fprintf(stderr, "serve: no index at %s (run --index first)\n", path); return; }
    fseek(fp, 0, SEEK_END);
    long size = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    char *buf = malloc(size + 1);
    fread(buf, 1, size, fp);
    buf[size] = '\0';
    fclose(fp);
    printf("serve: %ld bytes resident (%s) — type a symbol, one per line\n", size, path);
    fflush(stdout);
    char line[256];
    while (fgets(line, sizeof(line), stdin)) {
        line[strcspn(line, "\r\n")] = '\0';
        if (line[0] == '\0') continue;
        /* find "symbol": [ ... ] in the JSON and extract just the value block */
        char needle[300];
        snprintf(needle, sizeof(needle), "\"%s\":", line);
        char *hit = strstr(buf, needle);
        if (hit) {
            /* skip past the colon, find the value */
            char *v = hit + strlen(needle);
            while (*v == ' ' || *v == '\t' || *v == '\n' || *v == '\r') v++;
            if (*v == '[') {
                /* print balanced [...] value */
                int depth = 0;
                char *e = v;
                for (; *e; e++) {
                    if (*e == '[') depth++;
                    else if (*e == ']') { depth--; if (depth == 0) { e++; break; } }
                    else if (*e == '"') { /* skip string */ e++; while (*e && *e != '"') { if (*e == '\\') e++; e++; } }
                }
                fwrite(v, 1, (size_t)(e - v), stdout);
                printf("\n");
            } else {
                /* scalar value: up to comma or newline */
                char *e = v;
                while (*e && *e != ',' && *e != '\n' && *e != '\r') e++;
                fwrite(v, 1, (size_t)(e - v), stdout);
                printf("\n");
            }
        } else {
            printf("%s: not found\n", line);
        }
        fflush(stdout);
    }
    free(buf);
}

int main(int argc, char **argv) {
    /* --serve ROOT : resident index server. Loads the persistent index once,
       answers `symbol` lines on stdin with their definition instantly (sub-ms,
       no Python startup). This is the C-resident no-daemon answer to
       codebase-memory's sub-ms lookups. */
    if (argc >= 3 && strcmp(argv[1], "--serve") == 0) {
        serve_index(argv[2]);
        return 0;
    }
    /* --watch ROOT : native file watcher. Prints changed/created/removed code
       file paths as they happen, using kqueue (macOS) or inotify (Linux).
       Runs until killed. This is the native watcher that codegraph has and
       the Python `--watch` can't match for event latency. */
    if (argc >= 3 && strcmp(argv[1], "--watch") == 0) {
        const char *root = argv[2];
        watch_dir(root);
        return 0;
    }
    /* --list ROOT : walk the tree in C, print code file paths (one per line).
       Much faster than Python os.walk + per-file gitignore matching on huge
       repos. Skips hidden dirs (.git, node_modules-like) for speed. */
    if (argc >= 3 && strcmp(argv[1], "--list") == 0) {
        const char *root = argv[2];
        char path[4096];
        /* simple recursive walk via a manual stack of directories */
        char **dirs = malloc(16384 * sizeof(char*));
        int ndirs = 0, cap = 16384;
        dirs[ndirs++] = strdup(root);
        while (ndirs > 0) {
            const char *d = dirs[--ndirs];
            snprintf(path, sizeof(path), "%s", d);
            DIR *dp = opendir(d);
            if (!dp) { free((void*)d); continue; }
            struct dirent *de;
            while ((de = readdir(dp)) != NULL) {
                if (de->d_name[0] == '.') continue; /* hidden incl. .git */
                char full[4096];
                snprintf(full, sizeof(full), "%s/%s", d, de->d_name);
                if (de->d_type == DT_DIR) {
                    /* skip node_modules / build / dist for index speed */
                    if (strcmp(de->d_name, "node_modules") == 0 ||
                        strcmp(de->d_name, "build") == 0 ||
                        strcmp(de->d_name, "dist") == 0 ||
                        strcmp(de->d_name, ".venv") == 0 ||
                        strcmp(de->d_name, "venv") == 0) continue;
                    if (ndirs >= cap) { cap *= 2; dirs = realloc(dirs, cap * sizeof(char *)); }
                    dirs[ndirs++] = strdup(full);
                } else if (de->d_type == DT_REG) {
                    const char *ext = strrchr(de->d_name, '.');
                    if (ext && is_c_ext(ext)) printf("%s\n", full);
                }
            }
            closedir(dp);
            free((void*)d);
        }
        free(dirs);
        return 0;
    }
    /* Read file paths from argv (skip argv[0]) or stdin (one per line). */
    char path[4096];
    if (argc > 1) {
        for (int i = 1; i < argc; i++) {
            snprintf(path, sizeof(path), "%s", argv[i]);
            const char *ext = strrchr(path, '.');
            if (!ext) continue;
            if (!is_c_ext(ext)) continue;
            FileResult *fr = calloc(1, sizeof(FileResult));
            if (scan_file(path, ext, fr)) {
                print_json(fr);
            }
            free(fr);
        }
        return 0;
    }
    /* stdin mode */
    while (fgets(path, sizeof(path), stdin)) {
        path[strcspn(path, "\r\n")] = '\0';
        if (path[0] == '\0') continue;
        const char *ext = strrchr(path, '.');
        if (!ext) continue;
        if (!is_c_ext(ext)) continue;
        FileResult *fr = calloc(1, sizeof(FileResult));
        if (scan_file(path, ext, fr)) {
            print_json(fr);
        }
        free(fr);
    }
    return 0;
}
