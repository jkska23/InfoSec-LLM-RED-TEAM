import re
import ast
import sys
import html
import traceback
import time
from typing import List, Tuple, Dict

try:
    import chardet
except Exception:
    chardet = None

try:
    import pandas as pd
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
except Exception:
    pd = None
    TfidfVectorizer = None
    LogisticRegression = None


# --------------------- Input Sanitization Layer ---------------------

SUSPICIOUS_PATTERNS = [
    r"ignore previous", r"bypass", r"override safety", r"ignore safety",
    r"execute code", r"eval\(|exec\(|subprocess", r"open\(|write file",
    r"drop table", r"--", r"\bUNION\b", r"\bSELECT\b .* \bFROM\b"
]


def detect_encoding(text_bytes: bytes) -> str:
    """Best-effort encoding detection. Returns encoding name or 'utf-8'."""
    if chardet:
        try:
            res = chardet.detect(text_bytes)
            return res.get("encoding") or "utf-8"
        except Exception:
            return "utf-8"
    return "utf-8"


def sanitize_input(prompt: str) -> Tuple[str, Dict[str, bool]]:
    """Normalize text, remove control characters, and flag suspicious patterns.

    Returns sanitized text and a map of boolean flags for matched heuristics.
    """
    flags: Dict[str, bool] = {}
    p = html.unescape(prompt)
    p = re.sub(r"[\r\t]+", " ", p)
    p = re.sub(r" +", " ", p).strip()
    p = ''.join(ch for ch in p if ch.isprintable())

    for pat in SUSPICIOUS_PATTERNS:
        if re.search(pat, p, flags=re.IGNORECASE):
            flags[f"pat:{pat}"] = True

    if re.search(r"```|<code>|eval\(|exec\(|system\(|os\.popen", p, flags=re.IGNORECASE):
        flags['contains_code_request'] = True

    # entropy heuristic: many non-alphanumeric symbols may indicate obfuscation
    symbol_frac = sum(1 for c in p if not c.isalnum() and not c.isspace()) / max(1, len(p))
    if symbol_frac > 0.15:
        flags['possible_obfuscation'] = True

    return p, flags


# --------------------- Intent Analysis Layer ---------------------

def train_intent_classifier(dataset_path: str = "prompt_dataset.csv"):
    """Train and return a TF-IDF vectorizer and logistic regression classifier.

    If dependencies are missing, raises RuntimeError.
    """
    if pd is None or TfidfVectorizer is None or LogisticRegression is None:
        raise RuntimeError("Missing ML dependencies: install pandas and scikit-learn")

    df = pd.read_csv(dataset_path)
    X = df['prompt']
    y = df['label']
    vec = TfidfVectorizer(max_features=3000)
    Xv = vec.fit_transform(X)
    clf = LogisticRegression(max_iter=1000)
    clf.fit(Xv, y)
    return vec, clf


def analyze_intent(prompt: str, vec, clf) -> Dict[str, object]:
    """Return predicted label, probability distribution, and a short rationale."""
    x = vec.transform([prompt])
    pred = clf.predict(x)[0]
    probs = {}
    try:
        probs = dict(zip(clf.classes_, clf.predict_proba(x)[0].tolist()))
    except Exception:
        probs = {c: float(i == pred) for i, c in enumerate(clf.classes_)}

    rationale = []
    if re.search(r"bypass|evad|ignore", prompt, flags=re.IGNORECASE):
        rationale.append("contains bypass/ignore phrasing")
    if re.search(r"how to.*exploit|attack|vulnerab", prompt, flags=re.IGNORECASE):
        rationale.append("explicit attack keywords")

    return {"predicted_label": pred, "probs": probs, "rationale": rationale}


# --------------------- Output Monitoring Layer ---------------------

def extract_code_blocks(text: str) -> List[Tuple[str, str]]:
    """Extract code blocks from markdown or HTML-like text.

    Returns list of (lang, code). lang may be empty.
    """
    blocks: List[Tuple[str, str]] = []
    for m in re.finditer(r"```(\w+)?\n([\s\S]+?)```", text):
        lang = m.group(1) or ""
        code = m.group(2)
        blocks.append((lang.lower(), code))

    for m in re.finditer(r"<code>([\s\S]+?)</code>", text):
        blocks.append(("", m.group(1)))

    return blocks


def ast_static_scan_python(code: str) -> List[str]:
    """Parse Python code and detect risky constructs using AST inspection.

    Returns a list of finding strings.
    """
    findings: List[str] = []
    try:
        tree = ast.parse(code)
    except Exception as e:
        findings.append(f"parse_error: {e}")
        return findings

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            fname = None
            if isinstance(func, ast.Name):
                fname = func.id
            elif isinstance(func, ast.Attribute):
                parts = []
                cur = func
                while isinstance(cur, ast.Attribute):
                    parts.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    parts.append(cur.id)
                fname = ".".join(reversed(parts))

            if fname and any(tok in fname for tok in ("eval", "exec", "system", "popen", "subprocess", "pickle")):
                findings.append(f"dangerous_call: {fname}")

        if isinstance(node, ast.Import):
            for n in node.names:
                if n.name in ("socket", "requests", "urllib", "pickle", "subprocess", "os"):
                    findings.append(f"imported: {n.name}")

        if isinstance(node, ast.ImportFrom):
            if node.module and any(x in node.module for x in ("socket", "requests", "urllib", "pickle", "subprocess", "os")):
                findings.append(f"from_import: {node.module}")

    # Deduplicate findings while preserving order
    return list(dict.fromkeys(findings))


def static_vuln_scan(code: str, lang: str = "python") -> List[str]:
    """High-level static vulnerability scanner. Supports Python AST checks and
    heuristic regexes for other languages.
    """
    findings: List[str] = []
    if lang in ("py", "python", ""):
        findings.extend(ast_static_scan_python(code))
    else:
        if re.search(r"eval\(|exec\(|system\(|popen\(|subprocess", code, flags=re.IGNORECASE):
            findings.append("heuristic_dangerous_functions_present")

    if re.search(r"passwd|secret|api[_-]?key|ssh-key", code, flags=re.IGNORECASE):
        findings.append("possible_secret_leak")

    if re.search(r"rm\s+-rf|:\s*(){:|forkbomb", code, flags=re.IGNORECASE):
        findings.append("possible_destructive_command")

    return findings


def sandbox_execute(code: str, lang: str = "python", allow_exec: bool = False, timeout: int = 2) -> Dict[str, object]:
    """Conservative sandbox runner. Execution is disabled by default (allow_exec=False).

    If enabled, performs AST checks and disallows obvious unsafe constructs before
    invoking a subprocess to run the code. This is NOT a replacement for OS-level
    sandboxing (containers, VMs, seccomp) and should only be used for trusted
    experiments.
    """
    result: Dict[str, object] = {"executed": False, "stdout": "", "stderr": "", "reason": "execution_blocked"}
    if not allow_exec:
        return result

    if lang not in ("py", "python", ""):
        result['reason'] = 'unsupported_language'
        return result

    issues = ast_static_scan_python(code)
    if any(i.startswith("dangerous_call") or i.startswith("imported: pickle") for i in issues):
        result['reason'] = 'unsafe_code_detected'
        result['issues'] = issues
        return result

    import subprocess, tempfile, os
    try:
        fd, path = tempfile.mkstemp(suffix='.py', text=True)
        os.write(fd, code.encode('utf-8'))
        os.close(fd)
        proc = subprocess.run([sys.executable, path], capture_output=True, timeout=timeout, check=False)
        result['executed'] = True
        result['stdout'] = proc.stdout.decode('utf-8', errors='replace')
        result['stderr'] = proc.stderr.decode('utf-8', errors='replace')
        result['reason'] = 'ok'
    except subprocess.TimeoutExpired:
        result['reason'] = 'timeout'
    except Exception as e:
        result['reason'] = f'error:{e}'
    finally:
        try:
            os.remove(path)
        except Exception:
            pass

    return result


# --------------------- Integration Pipeline / Performance Notes ---------------------

def integration_pipeline(prompt: str, response: str = None, vec_clf: Tuple = None, allow_exec: bool = False) -> Dict[str, object]:
    """Run the three-layer validator and return a structured report.

    Notes on real-time processing and performance:
    - Vectorization + inference: pre-fit vectorizer and classifier, reuse across requests.
    - Static AST parsing is fast for small snippets; for large responses, limit to
      extracted code blocks and apply size caps (e.g., 5k characters).
    - I/O: keep file writes (for sandbox) small and short-lived; prefer streams.
    - For high throughput, run classification in parallel workers and cache features.
    """
    t0 = time.time()
    sanitized, flags = sanitize_input(prompt)

    intent_report = {"predicted_label": None, "probs": {}, "rationale": []}
    if vec_clf:
        vec, clf = vec_clf
        try:
            intent_report = analyze_intent(sanitized, vec, clf)
        except Exception as e:
            intent_report['rationale'] = [str(e)]

    output_report = {"code_blocks": [], "text_warnings": []}
    if response:
        blocks = extract_code_blocks(response)
        if not blocks:
            if re.search(r"bypass|exploit|payload|attack", response, flags=re.IGNORECASE):
                output_report['text_warnings'].append('response_contains_malicious_language')
        else:
            for lang, code in blocks:
                if len(code) > 5000:
                    output_report['code_blocks'].append({'lang': lang, 'issue': 'too_large_for_scan'})
                    continue
                findings = static_vuln_scan(code, lang=lang or 'python')
                exec_res = sandbox_execute(code, lang=lang or 'python', allow_exec=allow_exec)
                output_report['code_blocks'].append({'lang': lang, 'findings': findings, 'exec': exec_res})

    report = {
        'sanitized_prompt': sanitized,
        'sanitization_flags': flags,
        'intent': intent_report,
        'output': output_report,
        'duration_sec': time.time() - t0
    }
    return report


def pretty_print_report(r: Dict[str, object]):
    print("=== Sanitization ===")
    print(r['sanitized_prompt'])
    print("Flags:", r['sanitization_flags'])
    print("\n=== Intent ===")
    print(r['intent'])
    print("\n=== Output ===")
    print(r['output'])
    print(f"\nProcessing time: {r['duration_sec']:.3f}s")


if __name__ == '__main__':
    # CLI for quick experiments
    if len(sys.argv) < 2:
        print("Usage: lab2_detection_assessment.py '<prompt>' [response_file_or_text] [--allow-exec]")
        sys.exit(0)

    prompt_arg = sys.argv[1]
    resp = None
    allow = False
    if len(sys.argv) >= 3 and not sys.argv[2].startswith('--'):
        try:
            with open(sys.argv[2], 'r', encoding='utf-8') as f:
                resp = f.read()
        except Exception:
            resp = sys.argv[2]
    if '--allow-exec' in sys.argv:
        allow = True

    vec_clf = None
    if pd is not None:
        try:
            vec_clf = train_intent_classifier()
        except Exception:
            vec_clf = None

    try:
        report = integration_pipeline(prompt_arg, response=resp, vec_clf=vec_clf, allow_exec=allow)
        pretty_print_report(report)
    except Exception:
        traceback.print_exc()

