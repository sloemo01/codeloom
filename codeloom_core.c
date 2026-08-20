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
 *   {"file":"...","symbols":[{"name":...,"kind":...}],"imports":["...",...]}
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

/* Max sizes (per-file — a single file won't have this many). */
#define MAX_LINE 16384
#define MAX_SYMS 16384
#define MAX_IMPORTS 16384
#define MAX_CALLS 16384
#define MAX_TARGETS 4096

typedef struct {
    char name[128];
    char kind[16];
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

/* Trim surrounding whitespace in place. */
static char *trim(char *s) {
    while (isspace((unsigned char)*s)) s++;
    char *end = s + strlen(s) - 1;
    while (end > s && isspace((unsigned char)*end)) end--;
    end[1] = '\0';
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
    /* codebase-memory has 158; we cover the agent-workload priority set
       plus the broad request. Each gets the same line-based extraction. */
    static const char *exts[] = {
        /* frontend */
        ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".svelte", ".vue", ".astro",
        ".liquid",
        /* systems */
        ".rs", ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".mm", ".swift",
        /* mobile/UI */
        ".kt", ".kts", ".dart",
        /* backend / scripts */
        ".py", ".go", ".java", ".cs", ".php", ".rb", ".lua", ".erl", ".ex",
        ".exs", ".solidity", ".sol", ".r", ".cob", ".cbl", ".vb", ".nix",
        /* config-as-code */
        ".tf", ".hcl", ".ets", ".metal", ".liquid",
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
    while (fgets(line, sizeof(line), fp)) {
        char *t = trim(line);
        if (t[0] == '\0' || t[0] == '#' || t[0] == '/' || t[0] == '*' || t[0] == ';')
            continue;
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
            snprintf(fr->syms[fr->n_syms].name, sizeof(fr->syms[0].name), "%s", name);
            strcpy(fr->syms[fr->n_syms].kind, "function");
            fr->n_syms++;
            snprintf(current_func, sizeof(current_func), "%s", name);
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
    }
    fclose(fp);
    return 1;
}

static void print_json(const FileResult *fr) {
    printf("{\"file\":\"%s\",\"symbols\":[", fr->file);
    for (int i = 0; i < fr->n_syms; i++) {
        if (i) printf(",");
        /* JSON-escape names (crude but adequate for identifiers) */
        printf("{\"name\":\"%s\",\"kind\":\"%s\"}", fr->syms[i].name, fr->syms[i].kind);
    }
    printf("],\"imports\":[");
    for (int i = 0; i < fr->n_imports; i++) {
        if (i) printf(",");
        printf("\"%s\"", fr->imports[i]);
    }
    printf("],\"calls\":[");
    for (int i = 0; i < fr->n_calls; i++) {
        if (i) printf(",");
        printf("{\"caller\":\"%s\",\"targets\":[", fr->calls[i].caller);
        for (int j = 0; j < fr->calls[i].n; j++) {
            if (j) printf(",");
            printf("\"%s\"", fr->calls[i].targets[j]);
        }
        printf("]}");
    }
    printf("]}\n");
}

int main(int argc, char **argv) {
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
                    if (ndirs >= cap) { cap *= 2; dirs = realloc(dirs, cap * sizeof(char)); }
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
