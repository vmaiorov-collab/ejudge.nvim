#!/usr/bin/env python3
"""Backend of ejudge.nvim: talks to the ejudge server and runs tests.

Only the standard library is used. Every command prints JSON lines to stdout:
  {"type": "progress", "text": "..."}   intermediate information
  {"type": "test", ...}                 one finished test (command `test`)
  {"type": "result", "data": ...}       the final answer
  {"type": "error", "message": "..."}   the command failed
Settings come from the environment: EJUDGE_SERVER, EJUDGE_LOGIN, EJUDGE_PASSWORD.
The folder layout is the same as in the CLion plugin, so both can share one folder of contests.
"""
import http.cookiejar
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html.parser import HTMLParser

MARKER = ".ejudge"
TEMPLATE = "#include <bits/stdc++.h>\nusing namespace std;\n\nint main() {\n    return 0;\n}\n"
HEADERS = ["algorithm", "array", "bitset", "cassert", "cctype", "chrono", "climits", "cmath", "complex", "cstdint",
           "cstdio", "cstdlib", "cstring", "ctime", "deque", "functional", "iomanip", "iostream", "iterator",
           "limits", "list", "map", "memory", "numeric", "queue", "random", "set", "sstream", "stack", "string",
           "tuple", "unordered_map", "unordered_set", "utility", "vector"]
STDCPP_SHIM = "".join("#include <%s>\n" % h for h in HEADERS)
HOME = "https://algocode.ru/"


class EjudgeError(Exception):
    pass


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def progress(text):
    emit({"type": "progress", "text": text})


# ---------------------------------------------------------------- tiny HTML tree

VOID = {"br", "img", "hr", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}
AUTOCLOSE = {"li": {"li"}, "td": {"td", "th"}, "th": {"td", "th"}, "tr": {"td", "th", "tr"},
             "option": {"option"}, "p": {"p"}}


class Node:
    def __init__(self, tag, attrs=None, parent=None, base=""):
        self.tag = tag
        self.attrs = {k: (v or "") for k, v in (attrs or [])}
        self.children = []
        self.parent = parent
        self.base = base

    def get(self, key, default=""):
        return self.attrs.get(key, default)

    @property
    def classes(self):
        return self.get("class").split()

    def iter(self):
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.iter()

    def find_all(self, tag=None, pred=None):
        return [n for n in self.iter() if n is not self and (tag is None or n.tag == tag) and (pred is None or pred(n))]

    def find(self, tag=None, pred=None):
        for n in self.iter():
            if n is not self and (tag is None or n.tag == tag) and (pred is None or pred(n)):
                return n
        return None

    def abs(self, attr="href"):
        v = self.get(attr)
        return urllib.parse.urljoin(self.base, v) if v else ""

    def raw_text(self):
        """Text with line breaks as in the source (for <pre>)."""
        out = []
        for c in self.children:
            if isinstance(c, str):
                out.append(c)
            elif c.tag == "br":
                out.append("\n")
            elif c.tag not in ("script", "style"):
                out.append(c.raw_text())
        return "".join(out)

    def text(self):
        return " ".join(self.raw_text().split())

    def remove(self):
        if self.parent:
            self.parent.children = [c for c in self.parent.children if c is not self]

    def html(self):
        return "".join(node_html(c) for c in self.children)


def esc(s, quote=False):
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return s.replace('"', "&quot;") if quote else s


def node_html(c):
    if isinstance(c, str):
        return esc(c)
    attrs = "".join(' %s="%s"' % (k, esc(v, True)) for k, v in c.attrs.items())
    if c.tag in VOID:
        return "<%s%s>" % (c.tag, attrs)
    return "<%s%s>%s</%s>" % (c.tag, attrs, c.html(), c.tag)


class Builder(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", base=base)
        self.cur = self.root
        self.base = base

    def handle_starttag(self, tag, attrs):
        while self.cur.tag in AUTOCLOSE.get(tag, ()):
            self.cur = self.cur.parent
        node = Node(tag, attrs, self.cur, self.base)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse_html(text, base=""):
    b = Builder(base)
    b.feed(text)
    b.close()
    return b.root


# ---------------------------------------------------------------- TeX to plain text

SYMBOLS = {
    "le": "≤", "leq": "≤", "leqslant": "≤", "ge": "≥", "geq": "≥", "geqslant": "≥", "ne": "≠", "neq": "≠",
    "approx": "≈", "sim": "∼", "equiv": "≡", "ll": "≪", "gg": "≫", "lt": "<", "gt": ">", "cdot": "·",
    "times": "×", "div": "÷", "pm": "±", "mp": "∓", "ldots": "…", "dots": "…", "cdots": "⋯", "vdots": "⋮",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔",
    "leftrightarrow": "↔", "infty": "∞", "sum": "∑", "prod": "∏", "in": "∈", "notin": "∉", "oplus": "⊕",
    "otimes": "⊗", "lvert": "|", "rvert": "|", "vert": "|", "mid": "|", "lVert": "‖", "rVert": "‖", "Vert": "‖",
    "langle": "⟨", "rangle": "⟩", "lceil": "⌈", "rceil": "⌉", "lfloor": "⌊", "rfloor": "⌋", "alpha": "α",
    "beta": "β", "gamma": "γ", "delta": "δ", "varepsilon": "ε", "epsilon": "ε", "theta": "θ", "lambda": "λ",
    "mu": "μ", "pi": "π", "sigma": "σ", "phi": "φ", "varphi": "φ", "omega": "ω", "Delta": "Δ", "Sigma": "Σ",
    "Omega": "Ω", "cup": "∪", "cap": "∩", "subset": "⊂", "subseteq": "⊆", "supset": "⊃", "emptyset": "∅",
    "forall": "∀", "exists": "∃", "land": "∧", "lor": "∨", "neg": "¬", "ell": "ℓ", "circ": "∘", "partial": "∂",
    "nabla": "∇", "bmod": " mod ", "mod": " mod ", "quad": " ", "qquad": "  ",
}
NAMES = {"max", "min", "gcd", "lcm", "log", "ln", "lg", "sin", "cos", "tan", "exp", "sup", "inf", "det", "deg"}
IGNORED = {"limits", "nolimits", "displaystyle", "textstyle", "scriptstyle", "big", "Big", "bigg", "Bigg",
           "bigl", "bigr", "Bigl", "Bigr"}
WRAPPERS = {"mathrm", "text", "mathit", "mathbf", "textbf", "textit", "mathbin", "mathrel", "mathop", "mathcal",
            "mathsf", "operatorname", "boldsymbol", "mbox", "textrm", "mathbb", "overline", "underline", "bf",
            "it", "rm", "texttt", "mathtt"}
SUP = dict(zip("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ"))
SUB = dict(zip("0123456789+-=()aeoxhklmnpstijruv", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₕₖₗₘₙₚₛₜᵢⱼᵣᵤᵥ"))


class Tex:
    def __init__(self, s):
        self.s = s
        self.i = 0

    @staticmethod
    def convert(src):
        return re.sub(r"\s+", " ", Tex(src).sequence(None)).strip()

    def script(self, arg, table, mark):
        if arg and all(c in table for c in arg):
            return "".join(table[c] for c in arg)
        return mark + arg if len(arg) == 1 else "%s(%s)" % (mark, arg)

    def sequence(self, end):
        out = []
        s = self.s
        while self.i < len(s):
            c = s[self.i]
            if end is not None and c == end:
                self.i += 1
                return "".join(out)
            self.i += 1
            if c == "{":
                out.append(self.sequence("}"))
            elif c == "}" or c == "$":
                pass
            elif c == "^":
                out.append(self.script(self.arg(), SUP, "^"))
            elif c == "_":
                out.append(self.script(self.arg(), SUB, "_"))
            elif c == "\\":
                out.append(self.command())
            elif c == "~":
                out.append(" ")
            else:
                out.append(c)
        return "".join(out)

    def arg(self):
        s = self.s
        while self.i < len(s) and s[self.i] == " ":
            self.i += 1
        if self.i >= len(s):
            return ""
        c = s[self.i]
        self.i += 1
        if c == "{":
            return self.sequence("}")
        if c == "\\":
            return self.command()
        return c

    def command(self):
        s = self.s
        if self.i >= len(s):
            return ""
        c = s[self.i]
        if not c.isalpha():
            self.i += 1
            if c in "{}%#_$&|":
                return c
            if c in ",; \\:":
                return " "
            return ""
        start = self.i
        while self.i < len(s) and s[self.i].isalpha():
            self.i += 1
        name = s[start:self.i]
        if name in ("frac", "dfrac", "tfrac"):
            a, b = self.arg(), self.arg()
            wrap = lambda x: "(%s)" % x if re.search(r"[\s+\-·×=]", x) else x
            return "%s/%s" % (wrap(a), wrap(b))
        if name == "sqrt":
            if self.i < len(s) and s[self.i] == "[":
                while self.i < len(s) and s[self.i] != "]":
                    self.i += 1
                self.i += 1
            return "√(%s)" % self.arg()
        if name in ("left", "right"):
            if self.i < len(s) and s[self.i] == ".":
                self.i += 1
            return ""
        if name in IGNORED:
            return ""
        if name in WRAPPERS:
            return self.arg()
        if name in NAMES:
            return name
        return SYMBOLS.get(name, name)


# ---------------------------------------------------------------- statement -> text

BLOCKS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "ul", "ol", "li", "table", "tr", "blockquote", "center"}


def statement_text(body):
    """Plain text (markdown flavoured) of a statement body."""
    out = []

    def inline(n):
        parts = []
        for c in n.children:
            if isinstance(c, str):
                parts.append(c)
            elif c.tag in ("script", "style"):
                continue
            elif "math" in c.classes:
                parts.append(Tex.convert(c.text()))
            elif c.tag == "br":
                parts.append("\n")
            elif c.tag == "img":
                parts.append("[image: %s]" % c.get("src"))
            elif c.tag in ("tt", "code"):
                parts.append("`%s`" % inline(c).strip())
            elif c.tag in ("b", "strong"):
                parts.append(inline(c))
            else:
                parts.append(inline(c))
        return "".join(parts)

    def clean(s):
        return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\s*\n\s*", "\n", s.replace("\xa0", " "))).strip()

    def block(n, depth=0):
        for c in n.children:
            if isinstance(c, str):
                t = c.strip()
                if t:
                    out.append(clean(c))
                continue
            if c.tag in ("script", "style", "head"):
                continue
            if "math" in c.classes:
                out.append(Tex.convert(c.text()))
            elif re.fullmatch(r"h[1-6]", c.tag):
                out.append("")
                out.append("#" * min(int(c.tag[1]) , 3) + " " + clean(inline(c)))
                out.append("")
            elif c.tag == "pre":
                out.append("")
                out.append("```")
                out.append(c.raw_text().strip("\n"))
                out.append("```")
                out.append("")
            elif c.tag == "table":
                out.append("")
                rows = c.find_all("tr")
                simple = "line-table-wb" in c.classes
                for tr in rows:
                    cells = [clean(inline(td)) for td in tr.find_all(pred=lambda x: x.tag in ("td", "th"))]
                    out.append(("%s %s" % (cells[0], " ".join(cells[1:]))) if simple and len(cells) > 1 else "| " + " | ".join(cells) + " |")
                out.append("")
            elif c.tag == "li":
                out.append("- " + clean(inline(c)))
            elif c.tag in ("ul", "ol"):
                out.append("")
                block(c, depth + 1)
                out.append("")
            elif c.tag in BLOCKS or c.tag in ("body", "html"):
                has_block = c.tag in ("body", "html") or any(isinstance(x, Node) and (x.tag in BLOCKS) for x in c.children)
                if has_block:
                    out.append("")
                    block(c, depth + 1)
                    out.append("")
                else:
                    t = clean(inline(c))
                    if t:
                        out.append("")
                        out.append(t)
                        out.append("")
            else:
                t = clean(inline(c))
                if t:
                    out.append(t)

    block(body)
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


# ---------------------------------------------------------------- ejudge client

def form_fields(form):
    m = {}
    for i in form.find_all("input", lambda n: n.get("name")):
        t = i.get("type").lower()
        if t == "hidden":
            m[i.get("name")] = i.get("value")
        elif t == "submit" and i.get("name").startswith("action"):
            if not any(k.startswith("action") and k != "action" for k in m):
                m[i.get("name")] = i.get("value")
    return m


class Run:
    def __init__(self, id, columns, links):
        self.id = id
        self.columns = columns
        self.links = links

    def column(self, pattern):
        for k, v in self.columns.items():
            if re.search(pattern, k, re.I):
                return v
        return ""

    @property
    def result(self):
        return self.column(r"результат|result|status")

    @property
    def in_progress(self):
        r = self.result
        return not r.strip() or bool(re.search(
            r"компилир|выполня|ожида|тестир|проверя|compil|running|judging|pending|waiting|queue|testing", r, re.I))

    @property
    def letter(self):
        v = self.column(r"задача|problem").strip()
        return re.split(r"[ :.]", v)[0] if v else ""

    @property
    def accepted(self):
        return not self.in_progress and bool(re.match(r"(OK|Accepted|Полное|Принято|Зачтено)", self.result, re.I))

    def to_json(self):
        return {"id": self.id, "result": self.result, "accepted": self.accepted, "in_progress": self.in_progress,
                "letter": self.letter, "test": self.column(r"тест|test"), "lang": self.column(r"язык|lang"),
                "time": self.column(r"врем|time|дата|date"), "columns": self.columns}


def short_verdict(r):
    for pat, v in ((r"неправильн|wrong", "WA"), (r"runtime|выполнен", "RE"), (r"врем|time", "TL"),
                   (r"памят|memory", "ML"), (r"компил|compil", "CE"), (r"презент|presentation", "PE")):
        if re.search(pat, r, re.I):
            return v
    return r[:12]


def problem_states(runs, tabs):
    by_letter = {}
    for r in runs:
        if not r.in_progress and r.letter:
            by_letter.setdefault(r.letter, []).append(r)
    result = {}
    for letter, lst in by_letter.items():
        if any(re.match(r"(OK|Accepted|Полное|Принято|Зачтено)", x.result, re.I) for x in lst):
            result[letter] = {"solved": True, "verdict": "OK"}
        else:
            result[letter] = {"solved": False, "verdict": short_verdict(lst[-1].result)}
    for letter, cls in tabs.items():
        if "Ok" in cls:
            result[letter] = {"solved": True, "verdict": "OK"}
        elif "Empty" in cls:
            pass
        elif not result.get(letter, {}).get("solved"):
            result.setdefault(letter, {"solved": False, "verdict": "FAIL"})
    return result


class Client:
    def __init__(self, server, contest_id):
        self.server = server
        self.contest_id = contest_id
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.opener.addheaders = [("User-Agent", "ejudge.nvim")]
        self.main = None
        self.last_html = ""
        self.main_html = ""

    def _open(self, url, data=None, headers=None, timeout=60):
        req = urllib.request.Request(url, data=data, headers=headers or {})
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                body = resp.read()
                return body, resp.geturl(), resp.headers.get_content_charset() or "utf-8"
        except urllib.error.HTTPError as e:
            raise EjudgeError("Server returned HTTP %d for %s" % (e.code, url))
        except urllib.error.URLError as e:
            raise EjudgeError("Cannot reach %s: %s" % (url, e.reason))

    def get(self, url):
        body, final, cs = self._open(url)
        text = body.decode(cs, "replace")
        self.last_html = text
        return parse_html(text, final)

    def post(self, url, data, headers):
        body, final, cs = self._open(url, data, headers)
        return body.decode(cs, "replace"), final

    def login(self, login, password):
        login_url = "%s?contest_id=%d" % (self.server, self.contest_id)
        page = self.get(login_url)
        form = next((f for f in page.find_all("form") if f.find("input", lambda n: n.get("type") == "password")), None)
        if form is None:
            raise EjudgeError("Login form not found at " + login_url)
        fields = form_fields(form)
        fields[form.find("input", lambda n: n.get("type") == "password").get("name")] = password
        li = next((i for i in form.find_all("input", lambda n: n.get("type", "text") == "text") if "login" in i.get("name")), None) \
            or form.find("input", lambda n: n.get("type", "text") == "text")
        if li is None:
            raise EjudgeError("Login field not found")
        fields[li.get("name")] = login
        text, final = self.post(form.abs("action") or login_url, urllib.parse.urlencode(fields).encode(),
                                {"Content-Type": "application/x-www-form-urlencoded"})
        self.main_html = text
        self.main = parse_html(text, final)
        if self.main.find("input", lambda n: n.get("type") == "password"):
            raise EjudgeError("Login failed: check the login, the password and the contest id")

    def problems(self):
        seen = {}
        for a in self.main.find_all("a", lambda n: "prob_id=" in n.get("href")):
            m = re.search(r"prob_id=(\d+)", a.abs("href"))
            if m and m.group(1) not in seen:
                seen[m.group(1)] = {"name": a.text() or m.group(1), "url": a.abs("href"), "id": m.group(1)}
        return list(seen.values())

    def statement(self, problem):
        return self.get(problem["url"])

    def runs(self):
        link = next((a for a in self.main.find_all("a") if re.search(r"посылки|runs|submissions", a.text(), re.I)), None)
        doc = self.get(link.abs("href")) if link else self.main
        all_link = next((a for a in doc.find_all("a") if "all_runs=1" in a.get("href")), None)
        if all_link:
            doc = self.get(all_link.abs("href"))
        if not link and not all_link:
            self.last_html = self.main_html
        for table in doc.find_all("table"):
            rows = table.find_all("tr")
            header_row = next((r for r in rows if len(r.find_all("th")) >= 4), None)
            if header_row is None:
                continue
            header = [t.text() for t in header_row.find_all("th")]
            runs = []
            for r in rows:
                if r is header_row:
                    continue
                tds = r.find_all("td")
                if len(tds) != len(header):
                    continue
                cells = [t.text() for t in tds]
                if not cells[0].isdigit():
                    continue
                links = [(a.text(), a.abs("href")) for a in r.find_all("a", lambda n: n.get("href")) if a.abs("href")]
                runs.append(Run(int(cells[0]), dict(zip(header, cells)), links))
            return sorted(runs, key=lambda x: x.id)
        return []

    def tab_classes(self):
        doc = parse_html(self.last_html, self.server)
        out = {}
        for div in doc.find_all("div", lambda n: n.get("class").startswith("nProb")):
            a = div.find("a", lambda n: "prob_id=" in n.get("href"))
            if a:
                out[a.text()] = div.get("class")
        return out

    def await_run(self, after_id, timeout=120, on_update=None):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            runs = self.runs()
            cand = [r for r in runs if r.id > after_id]
            last = cand[-1] if cand else last
            if last is not None:
                if on_update:
                    on_update(last)
                if not last.in_progress:
                    return last
            time.sleep(3)
        return last

    def run_source(self, run):
        link = next((l for l in run.links if "action=36" in l[1]), None) \
            or next((l for l in run.links if re.search(r"просмотр|view|исходн|source", l[0], re.I)), None) \
            or next((l for l in run.links if "run_id=" in l[1]), None)
        if link is None:
            raise EjudgeError("No link to the source of run #%d" % run.id)
        doc = self.get(link[1])
        pres = doc.find_all("pre")
        if not pres:
            raise EjudgeError("The source of run #%d was not found on %s" % (run.id, link[1]))
        return max((p.raw_text() for p in pres), key=len)

    def _file_form(self, doc):
        return next((f for f in doc.find_all("form") if f.find("input", lambda n: n.get("type") == "file")), None)

    def _find_submit_form(self, problem):
        f = self._file_form(self.main)
        if f is not None:
            return f
        checked = []
        id_re = re.compile(r"prob_id=%s(?!\d)" % re.escape(problem["id"]))
        for a in self.main.find_all("a", lambda n: id_re.search(n.get("href"))):
            doc = self.get(a.abs("href"))
            checked.append(doc.find("title").text() if doc.find("title") else a.abs("href"))
            f = self._file_form(doc)
            if f is not None:
                return f
        skip = re.compile(r"выход|выйти|logout|log out|exit", re.I)
        urls = []
        for a in self.main.find_all("a", lambda n: n.get("href")):
            u = a.abs("href").split("#")[0]
            if skip.search(a.text()) or "logout" in u.lower() or not u.startswith("http") or u in urls:
                continue
            urls.append(u)
        for u in urls[:20]:
            try:
                doc = self.get(u)
            except EjudgeError:
                continue
            checked.append(u)
            f = self._file_form(doc)
            if f is not None:
                return f
        raise EjudgeError("Submit form not found. Pages checked: " + "; ".join(checked))

    def submit(self, problem, path, lang_hint):
        form = self._find_submit_form(problem)
        fields = form_fields(form)

        def pick(name, match):
            el = form.find(pred=lambda n: n.get("name") == name)
            if el is None:
                raise EjudgeError("Field '%s' not in the submit form" % name)
            if el.tag != "select":
                return el.get("value")
            for o in el.find_all("option"):
                if match(o):
                    return o.get("value")
            return None

        pid = pick("prob_id", lambda o: o.get("value") == problem["id"] or o.text().startswith(problem["name"]))
        if pid is None:
            raise EjudgeError("Problem %s is not in the submit form" % problem["name"])
        fields["prob_id"] = pid
        lid = pick("lang_id", lambda o: lang_hint.lower() in o.text().lower())
        if lid is None:
            langs = ", ".join(o.text() for o in form.find_all("option") if o.parent and o.parent.get("name") == "lang_id")
            raise EjudgeError("No language matching '%s'. Available: %s" % (lang_hint, langs))
        fields["lang_id"] = lid
        file_input = form.find("input", lambda n: n.get("type") == "file").get("name")
        boundary = "----ejudge" + uuid.uuid4().hex
        body = b""
        for k, v in fields.items():
            body += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, k, v)).encode()
        body += ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\nContent-Type: application/octet-stream\r\n\r\n'
                 % (boundary, file_input, os.path.basename(path))).encode()
        with open(path, "rb") as f:
            body += f.read()
        body += ("\r\n--%s--\r\n" % boundary).encode()
        text, _ = self.post(form.abs("action"), body, {"Content-Type": "multipart/form-data; boundary=" + boundary})
        doc = parse_html(text)
        msg = doc.find(pred=lambda n: ("message" in n.classes or "error" in n.classes or n.tag in ("h2", "h1")) and n.text())
        return msg.text() if msg else "sent"

    def download(self, url):
        body, _, _ = self._open(url)
        return body


# ---------------------------------------------------------------- catalog and import

def catalog_parallels():
    body, _, cs = Client("", 0)._open(HOME)
    doc = parse_html(body.decode(cs, "replace"), HOME)
    out = []
    for container in doc.find_all(pred=lambda n: "courses" in n.classes):
        for a in container.find_all("a", lambda n: re.fullmatch(r"/[a-z]+\d{4}/", n.get("href"))):
            out.append({"name": a.text(), "url": a.abs("href")})
    return out


def catalog_contests(url):
    body, _, cs = Client("", 0)._open(url)
    doc = parse_html(body.decode(cs, "replace"), url)
    out = []
    for lesson in doc.find_all(pred=lambda n: "lesson" in n.classes):
        d = lesson.find(pred=lambda n: "lesson_date" in n.classes)
        t = lesson.find(pred=lambda n: "lesson_topic" in n.classes)
        for a in lesson.find_all("a", lambda n: "new-client?contest_id=" in n.get("href")):
            m = re.search(r"contest_id=(\d+)", a.get("href"))
            if m:
                out.append({"id": int(m.group(1)), "date": d.text() if d else "", "topic": t.text() if t else ""})
    return out


def statement_body(page):
    area = page.find(pred=lambda n: n.get("id") == "probNavTaskArea-ins")
    if area is None:
        return page.find("body") or page
    for n in area.find_all(pred=lambda n: n.get("id") == "ej-submit-tabs" or n.tag in ("script", "style", "link", "meta")):
        n.remove()
    return area


def localize_images(client, body, folder):
    for img in body.find_all("img", lambda n: n.get("src")):
        src = img.abs("src")
        if not src.startswith("http"):
            continue
        m = re.search(r"[?&]file=([^&]+)", src)
        name = m.group(1) if m else "img%d" % (abs(hash(src)) % 10**8)
        safe = re.sub(r"[^\w.-]", "_", name, flags=re.A)
        if "." not in safe:
            safe += ".png"
        target = os.path.join(folder, "images", safe)
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if not os.path.exists(target):
                with open(target, "wb") as f:
                    f.write(client.download(src))
            img.attrs["src"] = "images/" + safe
            img.attrs.pop("style", None)
        except Exception:
            pass


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def contest_folder(parallel_name, contest):
    return re.sub(r"[^\w.-]+", "_", "%s_%s_%d" % (parallel_name, contest["topic"], contest["id"]))


def write_editor_support(base):
    inc = os.path.join(base, ".include", "bits")
    os.makedirs(inc, exist_ok=True)
    write_text(os.path.join(inc, "stdc++.h"), STDCPP_SHIM)
    clangd = os.path.join(base, ".clangd")
    if not os.path.exists(clangd):
        write_text(clangd, "CompileFlags:\n  Add: [-std=c++17, -I%s]\n" % os.path.join(base, ".include"))


def import_contest(env, base, parallel_name, contest):
    client = Client(env["server"], contest["id"])
    client.login(env["login"], env["password"])
    problems = client.problems()
    if not problems:
        raise EjudgeError("no problems found")
    root = os.path.join(base, contest_folder(parallel_name, contest))
    for p in problems:
        folder = os.path.join(root, re.sub(r"[^\w.-]", "_", p["name"], flags=re.A))
        tests = os.path.join(folder, "tests")
        os.makedirs(tests, exist_ok=True)
        for f in os.listdir(tests):
            if re.fullmatch(r"\d+\.(in|out)", f):
                os.remove(os.path.join(tests, f))
        st = client.statement(p)
        body = statement_body(st)
        localize_images(client, body, folder)
        write_text(os.path.join(folder, "statement.html"),
                   '<html><head><meta charset="utf-8"></head><body>%s</body></html>' % body.html())
        write_text(os.path.join(folder, "statement.txt"), statement_text(body))
        pres = [x.raw_text().rstrip() + "\n" for x in body.find_all("pre")]
        n = 0
        for a, b in zip(pres[0::2], pres[1::2]):
            if a.strip() and b.strip():
                n += 1
                write_text(os.path.join(tests, "%d.in" % n), a)
                write_text(os.path.join(tests, "%d.out" % n), b)
        write_text(os.path.join(folder, MARKER), "%s\n%s\n%d\n" % (p["id"], p["name"], contest["id"]))
        main = os.path.join(folder, "main.cpp")
        if not os.path.exists(main) and not source_of(folder):
            write_text(main, TEMPLATE)


def marker(folder):
    lines = read_text(os.path.join(folder, MARKER)).splitlines()
    return {"id": lines[0], "name": lines[1], "contest": int(lines[2])}


def title_of(folder):
    try:
        doc = parse_html(read_text(os.path.join(folder, "statement.html")))
        hs = [h.text() for h in doc.find_all(pred=lambda n: n.tag in ("h2", "h3"))]
        for h in hs:
            m = re.match(r"^Сдать решение задачи\s*[^-]{1,4}-(.*)$", h)
            if m:
                return m.group(1).strip()
        for h in hs:
            m = re.match(r"^Задача\s*[^:]{1,4}:\s*(.*)$", h)
            if m:
                return m.group(1).strip()
        return hs[0] if hs else ""
    except Exception:
        return ""


def scan(base):
    out = []
    if not os.path.isdir(base):
        return out
    for c in sorted(os.listdir(base), reverse=True):
        cdir = os.path.join(base, c)
        if c.startswith(".") or not os.path.isdir(cdir):
            continue
        probs = []
        for p in sorted(os.listdir(cdir)):
            pdir = os.path.join(cdir, p)
            if os.path.isfile(os.path.join(pdir, MARKER)):
                m = marker(pdir)
                probs.append({"dir": pdir, "letter": m["name"], "title": title_of(pdir), "contest_id": m["contest"]})
        if probs:
            out.append({"name": c.replace("_", " "), "dir": cdir, "problems": probs})
    return out


# ---------------------------------------------------------------- tests

def source_of(folder):
    files = sorted(f for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f)))
    for f in files:
        if f.startswith("main."):
            return os.path.join(folder, f)
    for f in files:
        if f.rsplit(".", 1)[-1] in ("cpp", "cc", "c", "py"):
            return os.path.join(folder, f)
    return None


def limits_of(folder):
    try:
        text = parse_html(read_text(os.path.join(folder, "statement.html"))).text()
    except Exception:
        return None, None
    t = m = None
    x = re.search(r"Ограничение времени:\s*([\d.,]+)\s*(мс|с|сек|ms|s)", text, re.I)
    if x:
        v = float(x.group(1).replace(",", "."))
        t = int(v if x.group(2).lower() in ("мс", "ms") else v * 1000)
    x = re.search(r"Ограничение памяти:\s*([\d.,]+)\s*([KMGКМГ])", text, re.I)
    if x:
        v = float(x.group(1).replace(",", "."))
        u = x.group(2).upper()
        m = int(v if u in ("K", "К") else v * 1024 * 1024 if u in ("G", "Г") else v * 1024)
    return t, m


def test_inputs(folder):
    td = os.path.join(folder, "tests")
    if not os.path.isdir(td):
        return []
    files = [f for f in os.listdir(td) if f.endswith(".in") and read_text(os.path.join(td, f)).strip()]

    def key(f):
        n = f[:-3]
        custom = n.startswith("u")
        num = n.lstrip("u")
        return (custom, int(num) if num.isdigit() else 10**9, f)
    return [os.path.join(td, f) for f in sorted(files, key=key)]


def save_test(folder, inp, out):
    td = os.path.join(folder, "tests")
    os.makedirs(td, exist_ok=True)
    n = 1
    while os.path.exists(os.path.join(td, "u%d.in" % n)):
        n += 1
    write_text(os.path.join(td, "u%d.in" % n), inp if inp.endswith("\n") else inp + "\n")
    if out and out.strip():
        write_text(os.path.join(td, "u%d.out" % n), out if out.endswith("\n") else out + "\n")
    return "u%d" % n


def delete_test(folder, name):
    if not re.fullmatch(r"u\d+", name):
        raise EjudgeError("Only your own tests (u1, u2…) can be deleted")
    for ext in (".in", ".out"):
        p = os.path.join(folder, "tests", name + ext)
        if os.path.exists(p):
            os.remove(p)


def find_tool(*names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def build(src, tmp):
    """Compiles the solution if needed; returns the command to run it."""
    ext = src.rsplit(".", 1)[-1]
    exe = os.path.join(tmp, "sol.exe" if os.name == "nt" else "sol")
    if ext in ("cpp", "cc", "cxx"):
        cc = find_tool("c++", "g++", "clang++")
        if not cc:
            raise EjudgeError("No C++ compiler found (c++, g++ or clang++ in PATH)")
        os.makedirs(os.path.join(tmp, "bits"), exist_ok=True)
        write_text(os.path.join(tmp, "bits", "stdc++.h"), STDCPP_SHIM)
        cmd = [cc, "-O2", "-std=c++17", "-I", tmp, "-o", exe, src]
    elif ext == "c":
        cc = find_tool("cc", "gcc", "clang")
        if not cc:
            raise EjudgeError("No C compiler found (cc or gcc in PATH)")
        cmd = [cc, "-O2", "-o", exe, src, "-lm"]
    elif ext == "py":
        py = find_tool("python3", "python")
        return [py or sys.executable, src]
    else:
        raise EjudgeError("Unsupported language: " + os.path.basename(src))
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", timeout=120)
    if p.returncode != 0:
        raise EjudgeError("Compilation error:\n" + p.stdout)
    return [exe]


def tokens(s):
    return s.split()


def run_one(cmd, inp_path, time_limit, mem_limit, tmp):
    name = os.path.basename(inp_path)[:-3]
    out_path = inp_path[:-3] + ".out"
    expected = read_text(out_path) if os.path.exists(out_path) else ""
    inp = read_text(inp_path)
    return run_process(cmd, inp_path, name, inp, expected, os.path.exists(out_path), time_limit, mem_limit, tmp)


def run_process(cmd, inp_path, name, inp, expected, has_expected, time_limit, mem_limit, tmp):
    act_path = os.path.join(tmp, name + ".actual")
    err_path = os.path.join(tmp, name + ".err")
    timeout = (time_limit or 5000) * 2 / 1000.0 + 1
    kb = -1
    timed_out = False
    start = time.perf_counter()
    with open(inp_path, "rb") as fin, open(act_path, "wb") as fout, open(err_path, "wb") as ferr:
        p = subprocess.Popen(cmd, stdin=fin, stdout=fout, stderr=ferr)
        if hasattr(os, "wait4"):
            while True:
                pid, status, ru = os.wait4(p.pid, os.WNOHANG)
                if pid:
                    break
                if time.perf_counter() - start > timeout:
                    timed_out = True
                    p.kill()
                    pid, status, ru = os.wait4(p.pid, 0)
                    break
                time.sleep(0.002)
            code = os.waitstatus_to_exitcode(status)
            kb = ru.ru_maxrss // 1024 if sys.platform == "darwin" else ru.ru_maxrss
        else:
            try:
                code = p.wait(timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                p.kill()
                code = p.wait()
    ms = int((time.perf_counter() - start) * 1000)
    actual = read_text(act_path)
    err = read_text(err_path)
    warns = []
    if time_limit and ms * 10 > time_limit * 7:
        warns.append("time %d of %d ms" % (ms, time_limit))
    if mem_limit and kb > 0 and kb * 10 > mem_limit * 7:
        warns.append("memory %d of %d MB" % (kb // 1024, mem_limit // 1024))

    def r(verdict, ok, note=""):
        cut = lambda s: s if len(s) < 20000 else s[:20000] + "\n…(cut)"
        return {"name": name, "verdict": verdict, "ok": ok, "ms": ms, "kb": kb, "note": note,
                "warn": ("close to the limit: " + ", ".join(warns)) if ok and warns else "",
                "input": cut(inp), "expected": cut(expected), "actual": cut(actual), "custom": name.startswith("u")}
    if timed_out:
        return r("TL", False, "Time limit exceeded: killed after %.0f s" % timeout)
    if code != 0:
        return r("RE", False, "Exit code %d\n%s" % (code, err[:3000]))
    if mem_limit and kb > mem_limit:
        return r("ML", False, "Memory %d MB, limit %d MB" % (kb // 1024, mem_limit // 1024))
    if time_limit and ms > time_limit:
        return r("TL", False, "Took %d ms here, the limit is %d ms (your computer may be slower or faster than the server)" % (ms, time_limit))
    if not has_expected:
        return r("?", True, "No expected output file; the output is shown only")
    if tokens(actual) == tokens(expected):
        return r("OK", True)
    return r("WA", False)


# ---------------------------------------------------------------- commands

def env_settings():
    return {"server": os.environ.get("EJUDGE_SERVER") or "https://ejudge.algocode.ru/cgi-bin/new-client",
            "login": os.environ.get("EJUDGE_LOGIN", ""), "password": os.environ.get("EJUDGE_PASSWORD", "")}


def logged_in(contest_id):
    env = env_settings()
    if not env["login"] or not env["password"]:
        raise EjudgeError("Login and password are not set. Run :EjudgeLogin")
    c = Client(env["server"], contest_id)
    c.login(env["login"], env["password"])
    return c


def cmd_parallels(a):
    return catalog_parallels()


def cmd_contests(a):
    return catalog_contests(a[0])


def cmd_import(a):
    name, url, base = a[0], a[1], os.path.expanduser(a[2])
    env = env_settings()
    if not env["login"] or not env["password"]:
        raise EjudgeError("Login and password are not set. Run :EjudgeLogin")
    contests = catalog_contests(url)
    if not contests:
        raise EjudgeError("No ejudge contests found for " + name)
    done, failed = 0, []
    for i, c in enumerate(contests, 1):
        progress("%s (%d/%d)" % (c["topic"], i, len(contests)))
        try:
            import_contest(env, base, name, c)
            done += 1
        except Exception as e:
            failed.append("%s: %s" % (c["topic"], e))
    write_editor_support(base)
    return {"done": done, "total": len(contests), "failed": failed}


def cmd_list(a):
    return scan(os.path.expanduser(a[0]))


def cmd_states(a):
    folder = a[0]
    m = marker(folder)
    c = logged_in(m["contest"])
    runs = c.runs()
    return {k: v for k, v in problem_states(runs, c.tab_classes()).items()}


def cmd_runs(a):
    m = marker(a[0])
    c = logged_in(m["contest"])
    return [r.to_json() for r in sorted(c.runs(), key=lambda r: -r.id) if r.letter == m["name"]]


def cmd_source(a):
    folder, run_id = a[0], int(a[1])
    m = marker(folder)
    c = logged_in(m["contest"])
    run = next((r for r in c.runs() if r.id == run_id), None)
    if run is None:
        raise EjudgeError("Run #%d not found" % run_id)
    code = c.run_source(run)
    lang = run.column(r"язык|lang").lower()
    if "python" in lang or "pypy" in lang:
        ext = "py"
    elif "g++" in lang or "c++" in lang:
        ext = "cpp"
    elif "java" in lang and "script" not in lang:
        ext = "java"
    elif "kotlin" in lang:
        ext = "kt"
    elif "rust" in lang:
        ext = "rs"
    elif "gcc" in lang or lang.strip() == "c":
        ext = "c"
    else:
        src = source_of(folder)
        ext = src.rsplit(".", 1)[-1] if src else "cpp"
    hist = os.path.join(folder, "history")
    os.makedirs(hist, exist_ok=True)
    path = os.path.join(hist, "run%d.%s" % (run_id, ext))
    write_text(path, code)
    return {"path": path}


LANG_HINTS = {"cpp": "g++", "cc": "g++", "cxx": "g++", "c": "gcc", "py": "python", "java": "java", "kt": "kotlin", "rs": "rust"}


def cmd_submit(a):
    folder, path = a[0], a[1]
    m = marker(folder)
    c = logged_in(m["contest"])
    before = max((r.id for r in c.runs()), default=0)
    hint = LANG_HINTS.get(path.rsplit(".", 1)[-1].lower(), path.rsplit(".", 1)[-1])
    msg = c.submit({"id": m["id"], "name": m["name"], "url": ""}, path, hint)
    progress("%s sent (%s). Waiting for the verdict…" % (m["name"], msg))
    run = c.await_run(before, on_update=lambda r: progress("#%d: %s" % (r.id, r.result or "…")))
    if run is None:
        return {"found": False}
    return {"found": True, "run": run.to_json(), "in_progress": run.in_progress}


def cmd_test(a):
    folder = a[0]
    src = source_of(folder)
    if not src:
        raise EjudgeError("No solution file (main.cpp / main.py) in " + folder)
    inputs = test_inputs(folder)
    if not inputs:
        raise EjudgeError("No tests in " + os.path.join(folder, "tests"))
    tl, ml = limits_of(folder)
    tmp = tempfile.mkdtemp(prefix="ejudge-run")
    ok = 0
    try:
        progress("Compiling…")
        cmd = build(src, tmp)
        for inp in inputs:
            r = run_one(cmd, inp, tl, ml, tmp)
            ok += r["ok"]
            r["type"] = "test"
            emit(r)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"total": len(inputs), "ok": ok}


def cmd_run(a):
    folder = a[0]
    src = source_of(folder)
    if not src:
        raise EjudgeError("No solution file (main.cpp / main.py) in " + folder)
    data = sys.stdin.read()
    tl, ml = limits_of(folder)
    tmp = tempfile.mkdtemp(prefix="ejudge-run")
    try:
        cmd = build(src, tmp)
        inp = os.path.join(tmp, "custom.in")
        write_text(inp, data)
        return run_one(cmd, inp, tl, ml, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_save_test(a):
    data = json.loads(sys.stdin.read())
    return {"name": save_test(a[0], data["input"], data.get("output"))}


def cmd_delete_test(a):
    delete_test(a[0], a[1])
    return {}


def cmd_solution(a):
    folder = a[0]
    src = source_of(folder)
    if not src:
        src = os.path.join(folder, "main.cpp")
        write_text(src, TEMPLATE)
    return {"path": src}


def cmd_statement(a):
    folder = a[0]
    txt = os.path.join(folder, "statement.txt")
    html = os.path.join(folder, "statement.html")
    if not os.path.exists(txt) or os.path.getmtime(txt) < os.path.getmtime(html):
        body = parse_html(read_text(html))
        write_text(txt, statement_text(body))
    return {"path": txt}


COMMANDS = {"parallels": cmd_parallels, "contests": cmd_contests, "import": cmd_import, "list": cmd_list,
            "states": cmd_states, "runs": cmd_runs, "source": cmd_source, "submit": cmd_submit, "test": cmd_test,
            "run": cmd_run, "save-test": cmd_save_test, "delete-test": cmd_delete_test, "solution": cmd_solution,
            "statement": cmd_statement}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        emit({"type": "error", "message": "Unknown command. Available: " + ", ".join(COMMANDS)})
        return 2
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        emit({"type": "result", "data": COMMANDS[sys.argv[1]](sys.argv[2:])})
        return 0
    except EjudgeError as e:
        emit({"type": "error", "message": str(e)})
    except Exception as e:  # unexpected: show the type to make reports useful
        emit({"type": "error", "message": "%s: %s" % (type(e).__name__, e)})
    return 1


if __name__ == "__main__":
    sys.exit(main())
