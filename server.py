#!/usr/bin/env python3
"""Iran Post (شرکت ملی پست ایران) tracking MCP server.

Tools:
  track(code)        -> رهگیری یک مرسوله از tracking.post.ir
  track_many(codes)  -> رهگیری چند مرسوله (جدا شده با ویرگول/فاصله)
  status()           -> وضعیت پروکسی و زیرساخت

Notes
-----
tracking.post.ir is geo-restricted to Iran (TCP connects only from Iranian
IPs; every foreign node times out).  From outside Iran we therefore need an
Iranian HTTP proxy: a pool is fetched from public lists (ProxyScrape /
GeoNode, country=IR), probed against the real endpoint, and cached in
state.json.  When the server runs *inside* Iran the direct path is tried
first and usually wins.

The search form is protected by a 4-digit ASP.NET captcha.  It is solved
locally with ddddocr's beta model (bundled under ./site-packages); on a bad
read we simply fetch a new captcha and retry.
"""

from __future__ import annotations

import concurrent.futures as cf
import html as html_lib
import json
import os
import re
import site
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
site.addsitedir(str(BASE_DIR / "site-packages"))

STATE_FILE = BASE_DIR / "state.json"
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(exist_ok=True)

SITE = "https://tracking.post.ir"
SEARCH_URL = SITE + "/search.aspx"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

import itertools

_COUNTER = itertools.count()

STATE_LOCK = threading.Lock()

_ocr_engine = None
_ocr_lock = threading.Lock()


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(patch: dict) -> dict:
    with STATE_LOCK:
        state = load_state()
        state.update(patch)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        return state


# --------------------------------------------------------------------------
# http via curl (curl speaks TLS through CONNECT proxies reliably here)
# --------------------------------------------------------------------------
def _curl(url: str, *, proxy: str | None = None, data: list[tuple[str, str]] | None = None,
          cookie: Path | None = None, referer: str | None = None,
          out: Path | None = None, timeout: int = 45, connect_timeout: int = 8) -> tuple[int, int, str]:
    """Returns (curl_rc, http_code, body_or_path)."""
    cmd = ["curl", "-sS", "--max-time", str(timeout), "--connect-timeout", str(connect_timeout),
           "-A", UA, "--compressed"]
    if proxy:
        cmd += ["--proxy", proxy]
    if cookie:
        cmd += ["-b", str(cookie), "-c", str(cookie)]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/x-www-form-urlencoded"]
        for k, v in data:
            cmd += ["--data-urlencode", f"{k}={v}"]
    if out is None:
        target = TMP_DIR / f"body_{os.getpid()}_{threading.get_ident()}_{next(_COUNTER)}.tmp"
    else:
        target = out
    cmd += ["-o", str(target), "-w", "%{http_code}", url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
    except subprocess.TimeoutExpired:
        return -1, 0, ""
    except Exception:
        return -1, 0, ""
    code = 0
    try:
        code = int((proc.stdout or "0").strip() or 0)
    except Exception:
        code = 0
    if proc.returncode != 0:
        return proc.returncode, code, ""
    if out is None:
        try:
            body = target.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            body = ""
        try:
            target.unlink(missing_ok=True)
        except Exception:
            pass
        return 0, code, body
    return 0, code, str(target)


# --------------------------------------------------------------------------
# proxy pool  (tracking.post.ir only answers from Iranian IPs)
# --------------------------------------------------------------------------
PROXY_SOURCES: list[tuple[str, str]] = [
    # (url, scheme)  -- scheme is what curl should speak
    ("https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=7000&country=IR&ssl=all&anonymity=all", "http"),
    ("https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=7000&country=IR", "socks5h"),
    ("https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&country=ir&protocol=http&format=text", "http"),
    ("https://www.proxy-list.download/api/v1/get?type=http&country=IR", "http"),
    ("https://proxylist.geonode.com/api/proxy-list?country=IR&limit=100&page=1&sort_by=lastChecked&sort_type=desc", "http"),
    ("https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/countries/IR/data.txt", "http"),
]


def _extract_proxies(text: str, scheme: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "//")):
            continue
        raw = line
        if "://" in raw:  # e.g. socks5://1.2.3.4:1080
            raw = raw.split("://", 1)[1]
        raw = raw.split()[0]
        parts = re.split(r"[:\s]", raw)
        if len(parts) >= 2 and re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", parts[0] or "") and parts[1].isdigit():
            cand = f"{scheme}://{parts[0]}:{parts[1]}"
            if cand not in out:
                out.append(cand)
    return out


def fetch_proxy_candidates() -> list[str]:
    found: list[str] = []
    env = os.environ.get("IRAN_PROXY", "").strip()
    if env:
        for p in re.split(r"[,\s]+", env):
            if p:
                found.append(p if "://" in p else "http://" + p)
    for url, scheme in PROXY_SOURCES:
        tmp = TMP_DIR / "proxies.tmp"
        rc, code, _ = _curl(url, out=tmp, timeout=20, connect_timeout=8)
        if rc != 0 or code not in (0, 200):
            continue
        try:
            text = tmp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if url.endswith("geonode.com") or "geonode" in url:
            try:
                rows = json.loads(text).get("data") or []
                for row in rows:
                    cand = f"{scheme}://{row.get('ip')}:{row.get('port')}"
                    if cand not in found:
                        found.append(cand)
                continue
            except Exception:
                pass
        for cand in _extract_proxies(text, scheme):
            if cand not in found:
                found.append(cand)
    return found


def probe_proxy(proxy: str, code: str) -> bool:
    """Fast reachability test: can this proxy complete the real search page?"""
    url = f"{SEARCH_URL}?id={code}"
    rc, http, body = _curl(url, proxy=proxy, timeout=12, connect_timeout=5)
    if rc != 0 or http != 200:
        return False
    if isinstance(body, str) and os.path.exists(body):  # out-file form (never here, but safe)
        try:
            return os.path.getsize(body) > 8000
        except Exception:
            return False
    return isinstance(body, str) and len(body) > 8000


def probe_all(candidates: list[str], code: str, workers: int = 40, budget: float = 55.0) -> list[str]:
    """Probe candidates in parallel, keep the ones that reach the site.

    Free Iranian proxies churn fast: probe everything we have within the
    time budget instead of stopping at the first hit.
    """
    good: list[str] = []
    deadline = time.time() + budget
    if not candidates:
        return good
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(probe_proxy, p, code): p for p in candidates}
        try:
            for fut in cf.as_completed(futs, timeout=max(1.0, budget)):
                p = futs[fut]
                try:
                    if fut.result():
                        good.append(p)
                except Exception:
                    pass
                if time.time() > deadline:
                    break
        except cf.TimeoutError:
            pass
    # probe results may arrive late; give the pool a moment to flush
    if not good:
        time.sleep(0.2)
        for fut, p in futs.items():
            if fut.done() and not fut.cancelled():
                try:
                    if fut.result() and p not in good:
                        good.append(p)
                except Exception:
                    pass
    save_state({"proxies": good, "checked_at": time.time(), "candidates": len(candidates)})
    return good


def get_working_proxies(code: str, force: bool = False) -> list[str]:
    state = load_state()
    cached = state.get("proxies") or []
    fresh = state.get("checked_at") or 0
    if not force and cached and (time.time() - fresh) < 900:
        return cached
    candidates = fetch_proxy_candidates()
    good = probe_all(candidates, code) if candidates else []
    if not good and cached and not force:
        # stale pool is better than nothing: the attempts below will
        # re-probe it and force a fresh scan when it is really dead
        good = cached
    return good


# --------------------------------------------------------------------------
# captcha OCR
# --------------------------------------------------------------------------
# Unambiguous latin look-alikes.  The beta ddddocr model normally returns
# plain digits already; this only cleans the rare letter confusion.
LETTER_MAP = {
    "o": "0", "O": "0", "D": "0", "Q": "0", "０": "0",
    "l": "1", "I": "1", "i": "1", "|": "1", "!": "1", "１": "1",
    "z": "2", "Z": "2", "２": "2",
    "s": "5", "S": "5", "５": "5",
    "b": "6", "G": "6", "б": "6", "６": "6",
    "t": "7", "T": "7", "７": "7",
    "B": "8", "８": "8",
    "g": "9", "q": "9", "Q": "9", "９": "9",
    "A": "4", "４": "4",
}


def normalize_captcha(raw: str, expect_len: int = 4) -> str:
    out = []
    for ch in (raw or "").strip():
        if ch.isdigit():
            out.append(ch)
        elif ch in LETTER_MAP:
            out.append(LETTER_MAP[ch])
        elif ch in "٠١٢٣٤٥٦٧٨٩":
            out.append(str("٠١٢٣٤٥٦٧٨٩".index(ch)))
    s = "".join(out)
    if len(s) != expect_len:
        return ""
    return s


def solve_captcha(png_bytes: bytes) -> str:
    global _ocr_engine
    with _ocr_lock:
        if _ocr_engine is None:
            import ddddocr
            _ocr_engine = ddddocr.DdddOcr(show_ad=False, beta=True)
        raw = _ocr_engine.classification(png_bytes)
    return normalize_captcha(raw)


# --------------------------------------------------------------------------
# page parsing
# --------------------------------------------------------------------------
def _clean(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_tracking_page(page: str, code: str) -> dict:
    idx = page.find('id="pnlResult"')
    if idx < 0:
        return {"code": code, "events": [], "raw_found": False}
    seg = page[idx:]
    for marker in ("لطفا شماره موبایل خود را وارد نمایید", 'id="pnlVote"', "نظر سنجی"):
        cut = seg.find(marker)
        if cut > 0:
            seg = seg[:cut]

    date_re = re.compile(r"<div class='newtdheader col-lg-6[^']*'>(.*?)</div>", re.S)
    row_start_re = re.compile(r"<div class='row newrowdata'>")

    marks: list[tuple[int, str, str]] = []
    for m in date_re.finditer(seg):
        marks.append((m.start(), "date", _clean(m.group(1))))
    for m in row_start_re.finditer(seg):
        marks.append((m.start(), "row", ""))
    marks.sort(key=lambda x: x[0])

    events = []
    current_date = ""
    for i, (pos, kind, val) in enumerate(marks):
        if kind == "date":
            current_date = val
            continue
        end = len(seg)
        for npos, nkind, _ in marks[i + 1:]:
            if npos > pos:
                end = npos
                break
        chunk = seg[pos:end]
        cells = [_clean(c) for c in re.findall(r"<div class='newtddata[^']*'>(.*?)</div>", chunk, re.S)]
        cells = [c for c in cells if c != ""][:4]
        if len(cells) < 4:
            continue
        idx_no, status, location, clock = cells[0], cells[1], cells[2], cells[3]
        if not re.fullmatch(r"\d{1,3}", idx_no or ""):
            continue
        events.append({
            "date": current_date,
            "index": int(idx_no),
            "status": status,
            "location": location,
            "time": clock,
        })

    # sanity: the page echoes the tracked number
    echoed = code in seg
    return {"code": code, "events": events, "raw_found": True, "echoed": echoed}


def parse_error(page: str) -> str:
    for pat in (r"کلاس='alert alert-danger'>(.*?)</div>", r'class="alert alert-danger">(.*?)</div>',
                r"alert-warning'>(.*?)</div>"):
        m = re.search(pat, page, re.S)
        if m:
            return _clean(m.group(1))
    return ""


# --------------------------------------------------------------------------
# the actual tracking flow
# --------------------------------------------------------------------------
def _page_fields(page: str) -> dict:
    fields = {}
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
        m = re.search(r'id="' + name + r'"[^>]*value="([^"]*)"', page)
        if not m:
            m = re.search(r"name='" + name + r"'[^>]*value='([^']*)'", page)
        if not m:
            m = re.search(r'name="' + name + r'"[^>]*value="([^"]*)"', page)
        fields[name] = html_lib.unescape(m.group(1)) if m else ""
    return fields


def _attempt(code: str, proxy: str | None) -> dict:
    """One full search attempt: page -> captcha -> post -> parse."""
    cookie = TMP_DIR / f"cj_{os.getpid()}_{threading.get_ident()}.txt"
    referer = f"{SEARCH_URL}?id={code}"

    rc, http, page = _curl(f"{SEARCH_URL}?id={code}", proxy=proxy, cookie=cookie,
                           referer=SITE + "/", timeout=40)
    if rc != 0 or http != 200 or not isinstance(page, str) or len(page) < 2000:
        raise RuntimeError(f"page fetch failed (rc={rc} http={http})")

    fields = _page_fields(page)

    cap_path = TMP_DIR / f"cap_{os.getpid()}_{threading.get_ident()}.png"
    rc, http, _ = _curl(f"{SEARCH_URL}?captcha=1&t={int(time.time() * 1000)}", proxy=proxy,
                        cookie=cookie, referer=referer, out=cap_path, timeout=30)
    if rc != 0 or http != 200 or not cap_path.exists() or cap_path.stat().st_size < 500:
        raise RuntimeError(f"captcha fetch failed (rc={rc} http={http})")

    png = cap_path.read_bytes()
    answer = solve_captcha(png)
    if not answer:
        raise RuntimeError("captcha unreadable")

    data = [
        ("__EVENTTARGET", "btnSearch"),
        ("__EVENTARGUMENT", ""),
        ("__LASTFOCUS", ""),
        ("txtbSearch", code),
        ("txtCaptcha", answer),
        ("CustomerMob", ""),
        ("txtVoteReason", ""),
        ("txtVoteTel", ""),
        ("__VIEWSTATE", fields.get("__VIEWSTATE", "")),
        ("__VIEWSTATEGENERATOR", fields.get("__VIEWSTATEGENERATOR", "")),
        ("__VIEWSTATEENCRYPTED", ""),
        ("__EVENTVALIDATION", fields.get("__EVENTVALIDATION", "")),
    ]
    out_path = TMP_DIR / f"res_{os.getpid()}_{threading.get_ident()}.html"
    rc, http, _ = _curl(SEARCH_URL + f"?id={code}", proxy=proxy, data=data, cookie=cookie,
                        referer=referer, out=out_path, timeout=45)
    if rc != 0 or http != 200:
        raise RuntimeError(f"post failed (rc={rc} http={http})")
    try:
        body = out_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"cannot read response: {exc}") from exc

    parsed = parse_tracking_page(body, code)
    if parsed.get("events"):
        return parsed
    err = parse_error(body)
    if err:
        raise RuntimeError(f"site said: {err}")
    if 'id="pnlResult"' in body and "newrowdata" not in body:
        raise RuntimeError("captcha rejected or no data for this number")
    raise RuntimeError("empty result")


def track_package(code: str, max_attempts: int = 4) -> dict:
    code = (code or "").strip()
    if not re.fullmatch(r"\d{13}|\d{14}|\d{24}", code):
        raise ValueError("شماره رهگیری باید 13، 14 یا 24 رقم باشد")

    started = time.time()
    deadline = started + 150
    errors: list[str] = []

    # 1) direct (fast path when the server runs inside Iran)
    for attempt in range(max_attempts):
        if time.time() > deadline:
            break
        try:
            res = _attempt(code, None)
            res["via"] = "direct"
            res["attempts"] = attempt + 1
            res["elapsed_s"] = round(time.time() - started, 1)
            return res
        except Exception as exc:
            errors.append(f"direct: {exc}")
            break  # outside Iran the direct route is geo-blocked; fall through to proxies

    # 2) Iranian proxy pool (refresh it once if the whole pool fails)
    tried = 0
    refreshed = False
    for round_no in range(2):
        proxies = get_working_proxies(code, force=(round_no > 0 and not refreshed))
        if round_no > 0:
            refreshed = True
        if not proxies:
            continue
        for proxy in proxies:
            for attempt in range(2):
                if time.time() > deadline:
                    break
                tried += 1
                try:
                    res = _attempt(code, proxy)
                    res["via"] = proxy
                    res["attempts"] = tried
                    res["elapsed_s"] = round(time.time() - started, 1)
                    return res
                except Exception as exc:
                    errors.append(f"{proxy}: {exc}")
                    continue
        if time.time() > deadline:
            break

    return {
        "code": code,
        "events": [],
        "error": "؛ ".join(errors[-4:]) or "no working route to tracking.post.ir",
        "proxies_available": tried,
        "elapsed_s": round(time.time() - started, 1),
    }


# --------------------------------------------------------------------------
# MCP surface
# --------------------------------------------------------------------------
from mcp.server.mcpserver import MCPServer  # noqa: E402

app = MCPServer(
    name="iranpost",
    description="رهگیری مرسولات شرکت ملی پست ایران (tracking.post.ir)",
    version="1.0.0",
)


@app.tool(description="رهگیری یک مرسوله پستی با شماره رهگیری 13/14/24 رقمی از tracking.post.ir")
def track(code: str) -> str:
    """کد رهگیری پستی را بگیر و آخرین وضعیت مرسوله را برگردان."""
    try:
        result = track_impl(code)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    except Exception as exc:  # pragma: no cover
        return json.dumps({"error": f"tracking failed: {exc}"}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False, indent=2)


@app.tool(description="رهگیری چند مرسوله پستی باهم؛ کدها را با ویرگول یا فاصله جدا کن")
def track_many(codes: str) -> str:
    parts = [p for p in re.split(r"[,\s;]+", codes or "") if p]
    if not parts:
        return json.dumps({"error": "no codes given"}, ensure_ascii=False)
    out = []
    for c in parts[:5]:
        try:
            out.append(track_impl(c))
        except Exception as exc:
            out.append({"code": c, "error": str(exc)})
    return json.dumps(out, ensure_ascii=False, indent=2)


@app.tool(description="وضعیت زیرساخت رهگیری: تعداد پروکسی‌های ایرانی کارآمد و زمان آخرین بررسی")
def status() -> str:
    state = load_state()
    checked = state.get("checked_at") or 0
    return json.dumps({
        "site": SITE,
        "geo_restricted_to_iran": True,
        "cached_proxies": len(state.get("proxies") or []),
        "last_proxy_check": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(checked)) if checked else None,
        "captcha_solver": "ddddocr(beta) local",
    }, ensure_ascii=False, indent=2)


def track_impl(code: str) -> dict:
    return track_package(code)


if __name__ == "__main__":
    app.run(transport="stdio")
