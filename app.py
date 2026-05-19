from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from html.parser import HTMLParser
from email.message import EmailMessage
from io import BytesIO
import html
import json
import os
import smtplib
import socket
import ssl
import sys
import textwrap
import time


HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8010"))
USER_AGENT = "SeoAuditTool/1.0 (+https://localhost)"
REQUEST_TIMEOUT = 12


class PageParser(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.meta = {}
        self.headings = {"h1": [], "h2": []}
        self.links = []
        self.images = []
        self.canonicals = []
        self.current_tag = None
        self.current_text = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        tag = tag.lower()
        if tag == "title":
            self.in_title = True
        if tag in ("h1", "h2"):
            self.current_tag = tag
            self.current_text = []
        if tag == "meta":
            name = (attrs.get("name") or attrs.get("property") or "").lower()
            content = attrs.get("content", "")
            if name:
                self.meta[name] = content.strip()
        if tag == "a":
            href = attrs.get("href", "").strip()
            if href and not href.startswith(("mailto:", "tel:", "javascript:", "#")):
                self.links.append(urljoin(self.base_url, href))
        if tag == "img":
            src = attrs.get("src", "").strip()
            self.images.append({
                "src": urljoin(self.base_url, src) if src else "",
                "alt": attrs.get("alt", "").strip(),
                "loading": attrs.get("loading", "").strip().lower(),
            })
        if tag == "link" and "canonical" in attrs.get("rel", "").lower().split():
            href = attrs.get("href", "").strip()
            if href:
                self.canonicals.append(urljoin(self.base_url, href))

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
        if tag == self.current_tag:
            text = " ".join("".join(self.current_text).split())
            if text:
                self.headings[self.current_tag].append(text)
            self.current_tag = None
            self.current_text = []

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.current_tag:
            self.current_text.append(data)


def normalize_url(value):
    value = value.strip()
    if not value:
        raise ValueError("Please enter a URL.")
    parsed = urlparse(value)
    if not parsed.scheme:
        value = "https://" + value
        parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Please enter a valid http or https URL.")
    return value


def fetch_url(url, method="GET"):
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"}
    request = Request(url, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            data = response.read()
            elapsed = time.perf_counter() - started
            return {
                "ok": 200 <= response.status < 400,
                "status": response.status,
                "url": response.geturl(),
                "headers": dict(response.headers),
                "body": data,
                "elapsed": elapsed,
                "error": "",
            }
    except HTTPError as exc:
        elapsed = time.perf_counter() - started
        body = exc.read() if method == "GET" else b""
        return {"ok": False, "status": exc.code, "url": url, "headers": dict(exc.headers), "body": body, "elapsed": elapsed, "error": str(exc)}
    except (URLError, socket.timeout, TimeoutError) as exc:
        elapsed = time.perf_counter() - started
        return {"ok": False, "status": 0, "url": url, "headers": {}, "body": b"", "elapsed": elapsed, "error": str(exc)}


def score_item(ok, label, detail, suggestion=""):
    return {"ok": ok, "label": label, "detail": detail, "suggestion": suggestion}


def analyze_meta(parser):
    title = " ".join(parser.title.split())
    description = parser.meta.get("description", "")
    viewport = parser.meta.get("viewport", "")
    robots = parser.meta.get("robots", "")
    checks = [
        score_item(bool(title), "Title tag", title or "Missing", "Add a unique title tag for the page."),
        score_item(30 <= len(title) <= 60, "Title length", f"{len(title)} characters", "Keep titles around 30-60 characters."),
        score_item(bool(description), "Meta description", description or "Missing", "Add a clear meta description."),
        score_item(70 <= len(description) <= 160, "Description length", f"{len(description)} characters", "Keep descriptions around 70-160 characters."),
        score_item(bool(viewport), "Mobile viewport", viewport or "Missing", "Add a responsive viewport meta tag."),
        score_item("noindex" not in robots.lower(), "Indexability", robots or "No blocking robots meta found", "Remove noindex unless this page should stay out of search."),
        score_item(len(parser.canonicals) == 1, "Canonical URL", parser.canonicals[0] if parser.canonicals else "Missing", "Add one canonical URL to reduce duplicate URL signals."),
    ]
    return {"title": title, "description": description, "checks": checks}


def analyze_headings(parser):
    h1s = parser.headings["h1"]
    h2s = parser.headings["h2"]
    checks = [
        score_item(len(h1s) == 1, "H1 usage", f"{len(h1s)} H1 found", "Use exactly one descriptive H1."),
        score_item(len(h2s) > 0, "H2 structure", f"{len(h2s)} H2 found", "Use H2 headings to organize important page sections."),
    ]
    return {"h1": h1s, "h2": h2s, "checks": checks}


def analyze_images(parser):
    missing_alt = [img for img in parser.images if img["src"] and not img["alt"]]
    lazy = [img for img in parser.images if img["loading"] == "lazy"]
    checks = [
        score_item(not missing_alt, "Image alt text", f"{len(missing_alt)} missing alt text", "Add concise alt text to meaningful images."),
        score_item(len(lazy) >= max(0, len(parser.images) - 2), "Lazy loading", f"{len(lazy)} of {len(parser.images)} images use lazy loading", "Lazy-load below-the-fold images."),
    ]
    return {"total": len(parser.images), "missing_alt": missing_alt[:20], "checks": checks}


def check_links(base_url, links):
    unique = []
    seen = set()
    base_host = urlparse(base_url).netloc.lower()
    for link in links:
        if link not in seen:
            seen.add(link)
            unique.append(link)
    checked = []
    for link in unique[:35]:
        parsed = urlparse(link)
        if parsed.scheme not in ("http", "https"):
            continue
        method = "HEAD"
        result = fetch_url(link, method=method)
        if result["status"] in (405, 403, 0):
            result = fetch_url(link, method="GET")
        checked.append({
            "url": link,
            "status": result["status"] or "Error",
            "ok": result["ok"],
            "internal": parsed.netloc.lower() == base_host,
            "error": result["error"],
        })
    broken = [item for item in checked if not item["ok"]]
    return {
        "total_found": len(unique),
        "checked": checked,
        "broken": broken,
        "checks": [
            score_item(not broken, "Broken links", f"{len(broken)} broken of {len(checked)} checked", "Fix or remove broken internal and external links."),
        ],
    }


def check_robots_sitemap(url):
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    robots_url = origin + "/robots.txt"
    sitemap_url = origin + "/sitemap.xml"
    robots = fetch_url(robots_url)
    sitemap = fetch_url(sitemap_url)
    robots_text = robots["body"].decode("utf-8", errors="replace")[:6000] if robots["body"] else ""
    sitemap_from_robots = ""
    for line in robots_text.splitlines():
        if line.lower().startswith("sitemap:"):
            sitemap_from_robots = line.split(":", 1)[1].strip()
            break
    checks = [
        score_item(robots["ok"], "robots.txt", f"{robots['status'] or 'Error'} at {robots_url}", "Publish robots.txt at the domain root."),
        score_item(bool(sitemap_from_robots) or sitemap["ok"], "Sitemap", sitemap_from_robots or f"{sitemap['status'] or 'Error'} at {sitemap_url}", "Publish sitemap.xml or reference a sitemap in robots.txt."),
    ]
    return {"robots_url": robots_url, "sitemap_url": sitemap_from_robots or sitemap_url, "robots_text": robots_text, "checks": checks}


def local_speed_suggestions(response):
    size_kb = len(response["body"]) / 1024
    content_type = response["headers"].get("Content-Type", "")
    compression = response["headers"].get("Content-Encoding", "")
    cache = response["headers"].get("Cache-Control", "")
    elapsed = response["elapsed"]
    checks = [
        score_item(elapsed <= 2.5, "Server response", f"{elapsed:.2f}s", "Improve hosting, caching, database work, or CDN routing to reduce response time."),
        score_item(size_kb <= 500, "HTML weight", f"{size_kb:.1f} KB", "Reduce unused markup, inline payload, and render-blocking resources."),
        score_item(bool(compression), "Compression", compression or "Not detected", "Enable Brotli or gzip compression."),
        score_item(bool(cache), "Caching", cache or "Not detected", "Add useful Cache-Control headers for static assets."),
        score_item("text/html" in content_type.lower(), "HTML content", content_type or "Unknown", "Make sure the scanned URL returns an HTML page."),
    ]
    return {"mode": "Local checks", "score": None, "checks": checks, "raw": {}}


def pagespeed_insights(url):
    api_key = os.environ.get("PAGESPEED_API_KEY", "").strip()
    if not api_key:
        return None
    query = urlencode({"url": url, "strategy": "mobile", "category": "performance", "key": api_key})
    api_url = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed?" + query
    result = fetch_url(api_url)
    if not result["ok"]:
        return None
    try:
        data = json.loads(result["body"].decode("utf-8"))
    except json.JSONDecodeError:
        return None
    lighthouse = data.get("lighthouseResult", {})
    categories = lighthouse.get("categories", {})
    performance = categories.get("performance", {})
    audits = lighthouse.get("audits", {})
    opportunities = []
    for audit_id in ("render-blocking-resources", "unminified-css", "unminified-javascript", "unused-css-rules", "uses-optimized-images", "uses-text-compression", "server-response-time"):
        audit = audits.get(audit_id, {})
        if audit and audit.get("score") not in (1, None):
            opportunities.append(score_item(False, audit.get("title", audit_id), audit.get("displayValue", "Needs attention"), audit.get("description", "")))
    checks = [
        score_item((performance.get("score") or 0) >= 0.9, "PageSpeed mobile score", f"{round((performance.get('score') or 0) * 100)} / 100", "Improve Lighthouse performance opportunities."),
    ] + opportunities[:8]
    return {"mode": "Google PageSpeed Insights", "score": round((performance.get("score") or 0) * 100), "checks": checks, "raw": data}


def overall_score(sections):
    checks = []
    for section in sections:
        checks.extend(section.get("checks", []))
    if not checks:
        return 0
    return round(sum(1 for check in checks if check["ok"]) / len(checks) * 100)


def audit_url(url):
    normalized = normalize_url(url)
    response = fetch_url(normalized)
    if not response["body"]:
        raise ValueError(f"Could not fetch the page. {response['error'] or response['status']}")
    final_url = response["url"]
    text = response["body"].decode("utf-8", errors="replace")
    parser = PageParser(final_url)
    parser.feed(text)

    meta = analyze_meta(parser)
    headings = analyze_headings(parser)
    images = analyze_images(parser)
    links = check_links(final_url, parser.links)
    robots = check_robots_sitemap(final_url)
    speed = pagespeed_insights(final_url) or local_speed_suggestions(response)
    scan = {
        "checks": [
            score_item(response["ok"], "Page fetch", f"HTTP {response['status']} in {response['elapsed']:.2f}s", "Resolve HTTP errors before optimizing SEO."),
            score_item(len(text) > 500, "Readable content", f"{len(text):,} HTML characters", "Add crawlable, useful page content."),
            score_item(urlparse(final_url).scheme == "https", "HTTPS", urlparse(final_url).scheme.upper(), "Use HTTPS for trust and ranking signals."),
        ]
    }
    sections = [scan, meta, headings, images, links, robots, speed]
    return {
        "url": normalized,
        "final_url": final_url,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "score": overall_score(sections),
        "scan": scan,
        "meta": meta,
        "headings": headings,
        "images": images,
        "links": links,
        "robots": robots,
        "speed": speed,
    }


def esc(value):
    return html.escape(str(value), quote=True)


def render_checks(checks):
    rows = []
    for check in checks:
        badge = "pass" if check["ok"] else "fail"
        status = "Pass" if check["ok"] else "Fix"
        rows.append(f"""
        <tr>
            <td><span class="badge {badge}">{status}</span></td>
            <td>{esc(check['label'])}</td>
            <td>{esc(check['detail'])}</td>
            <td>{esc(check.get('suggestion', ''))}</td>
        </tr>
        """)
    return "\n".join(rows)


def page_shell(content, audit=None, notice=""):
    report_buttons = ""
    if audit:
        report_buttons = f"""
        <div class="actions">
            <form method="post" action="/download-pdf">
                <input type="hidden" name="report" value="{esc(json.dumps(audit))}">
                <button type="submit">Download PDF</button>
            </form>
            <form method="post" action="/email-report" class="email-form">
                <input type="hidden" name="report" value="{esc(json.dumps(audit))}">
                <input name="email" type="email" placeholder="Email report to" required>
                <button type="submit">Send Email</button>
            </form>
        </div>
        """
    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>SEO Audit Tool</title>
    <link rel="stylesheet" href="/static/styles.css">
</head>
<body>
    <header class="topbar">
        <div>
            <h1>SEO Audit Tool</h1>
            <p>Technical SEO, content structure, broken links, robots, sitemap, and speed suggestions.</p>
        </div>
    </header>
    <main>
        <form class="scan-form" method="post" action="/scan">
            <input name="url" type="url" value="{esc(audit['url']) if audit else ''}" placeholder="https://example.com" required>
            <button type="submit">Run Audit</button>
        </form>
        {f'<div class="notice">{esc(notice)}</div>' if notice else ''}
        {report_buttons}
        {content}
    </main>
    <script src="/static/app.js"></script>
</body>
</html>"""


def checks_table(title, checks):
    return f"""
    <section class="panel">
        <h2>{esc(title)}</h2>
        <table>
            <thead><tr><th>Status</th><th>Check</th><th>Result</th><th>Suggestion</th></tr></thead>
            <tbody>{render_checks(checks)}</tbody>
        </table>
    </section>
    """


def render_audit(audit):
    broken_rows = "".join(
        f"<tr><td>{esc(item['status'])}</td><td>{esc('Internal' if item['internal'] else 'External')}</td><td><a href='{esc(item['url'])}'>{esc(item['url'])}</a></td></tr>"
        for item in audit["links"]["broken"]
    ) or "<tr><td colspan='3'>No broken links found in checked sample.</td></tr>"
    missing_alt = "".join(
        f"<li>{esc(img['src'])}</li>" for img in audit["images"]["missing_alt"]
    ) or "<li>No missing alt text found.</li>"
    h1s = "".join(f"<li>{esc(text)}</li>" for text in audit["headings"]["h1"]) or "<li>No H1 found.</li>"
    h2s = "".join(f"<li>{esc(text)}</li>" for text in audit["headings"]["h2"][:20]) or "<li>No H2 found.</li>"
    return f"""
    <section class="summary">
        <div class="score"><span>{audit['score']}</span><small>/100</small></div>
        <div>
            <h2>{esc(audit['final_url'])}</h2>
            <p>Generated {esc(audit['generated_at'])}. Speed source: {esc(audit['speed']['mode'])}.</p>
        </div>
    </section>
    <nav class="tabs" aria-label="Audit sections">
        <button class="active" data-tab="scan">Scan Page SEO</button>
        <button data-tab="headings">Missing H1/H2</button>
        <button data-tab="links">Broken Links</button>
        <button data-tab="meta">Meta Analysis</button>
        <button data-tab="speed">Speed Suggestions</button>
        <button data-tab="robots">Robots/Sitemap</button>
    </nav>
    <div class="tab-panel active" id="scan">
        {checks_table("Scan Page SEO", audit["scan"]["checks"] + audit["images"]["checks"])}
        <section class="panel"><h2>Images Missing Alt Text</h2><ul>{missing_alt}</ul></section>
    </div>
    <div class="tab-panel" id="headings">
        {checks_table("Heading Checks", audit["headings"]["checks"])}
        <section class="grid two"><div class="panel"><h2>H1</h2><ul>{h1s}</ul></div><div class="panel"><h2>H2</h2><ul>{h2s}</ul></div></section>
    </div>
    <div class="tab-panel" id="links">
        {checks_table("Broken Link Check", audit["links"]["checks"])}
        <section class="panel"><h2>Broken Links</h2><table><thead><tr><th>Status</th><th>Type</th><th>URL</th></tr></thead><tbody>{broken_rows}</tbody></table></section>
    </div>
    <div class="tab-panel" id="meta">
        {checks_table("Meta Analysis", audit["meta"]["checks"])}
    </div>
    <div class="tab-panel" id="speed">
        {checks_table("Speed Suggestions", audit["speed"]["checks"])}
    </div>
    <div class="tab-panel" id="robots">
        {checks_table("robots.txt and Sitemap Checker", audit["robots"]["checks"])}
        <section class="panel"><h2>robots.txt Preview</h2><pre>{esc(audit["robots"]["robots_text"] or "No robots.txt content found.")}</pre></section>
    </div>
    """


def create_pdf(audit):
    lines = [
        "SEO Audit Report",
        f"URL: {audit['final_url']}",
        f"Generated: {audit['generated_at']}",
        f"Overall score: {audit['score']}/100",
        "",
    ]
    sections = [
        ("Scan Page SEO", audit["scan"]["checks"]),
        ("Meta Analysis", audit["meta"]["checks"]),
        ("Missing H1/H2", audit["headings"]["checks"]),
        ("Broken Links", audit["links"]["checks"]),
        ("Robots/Sitemap", audit["robots"]["checks"]),
        ("Speed Suggestions", audit["speed"]["checks"]),
    ]
    for title, checks in sections:
        lines.append(title)
        for check in checks:
            status = "PASS" if check["ok"] else "FIX"
            lines.append(f"- {status}: {check['label']} - {check['detail']}")
            if check.get("suggestion") and not check["ok"]:
                lines.append(f"  Suggestion: {check['suggestion']}")
        lines.append("")
    return simple_pdf(lines)


def simple_pdf(lines):
    stream = BytesIO()
    objects = []

    def add_object(data):
        objects.append(data)
        return len(objects)

    font_id = add_object("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids = []
    wrapped = []
    for line in lines:
        wrapped.extend(textwrap.wrap(str(line), width=95) or [""])
    pages = [wrapped[i:i + 46] for i in range(0, len(wrapped), 46)] or [[]]
    for page in pages:
        y = 790
        content = ["BT", "/F1 10 Tf", "14 TL"]
        for line in page:
            safe = str(line).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            content.append(f"1 0 0 1 72 {y} Tm ({safe}) Tj")
            y -= 14
        content.append("ET")
        content_bytes = "\n".join(content).encode("latin-1", errors="replace")
        content_id = add_object(f"<< /Length {len(content_bytes)} >>\nstream\n{content_bytes.decode('latin-1')}\nendstream")
        page_id = add_object(f"<< /Type /Page /Parent 0 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>")
        page_ids.append(page_id)
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    pages_id = add_object(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>")
    catalog_id = add_object(f"<< /Type /Catalog /Pages {pages_id} 0 R >>")
    objects = [obj.replace("/Parent 0 0 R", f"/Parent {pages_id} 0 R") for obj in objects]

    stream.write(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(stream.tell())
        stream.write(f"{index} 0 obj\n{obj}\nendobj\n".encode("latin-1", errors="replace"))
    xref = stream.tell()
    stream.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        stream.write(f"{offset:010d} 00000 n \n".encode())
    stream.write(f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return stream.getvalue()


def send_report_email(to_email, audit, pdf_bytes):
    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", username)
    if not host or not sender:
        raise ValueError("Email is not configured. Set SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, and SMTP_FROM.")

    message = EmailMessage()
    message["Subject"] = f"SEO Audit Report - {audit['final_url']}"
    message["From"] = sender
    message["To"] = to_email
    message.set_content(f"Attached is the SEO audit report for {audit['final_url']}.\nScore: {audit['score']}/100")
    message.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename="seo-audit-report.pdf")

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=REQUEST_TIMEOUT) as smtp:
        smtp.starttls(context=context)
        if username and password:
            smtp.login(username, password)
        smtp.send_message(message)


class SeoAuditHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self.respond_html(page_shell("<section class='empty'><h2>Run a complete SEO audit</h2><p>Enter a URL to scan on-page SEO, headings, links, metadata, robots, sitemap, and speed signals.</p></section>"))
            return
        if self.path == "/static/styles.css":
            self.respond("text/css", STYLES.encode())
            return
        if self.path == "/static/app.js":
            self.respond("text/javascript", SCRIPT.encode())
            return
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        data = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
        try:
            if self.path == "/scan":
                audit = audit_url(data.get("url", [""])[0])
                self.respond_html(page_shell(render_audit(audit), audit=audit))
                return
            if self.path == "/download-pdf":
                audit = json.loads(data.get("report", ["{}"])[0])
                pdf = create_pdf(audit)
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", "attachment; filename=seo-audit-report.pdf")
                self.send_header("Content-Length", str(len(pdf)))
                self.end_headers()
                self.wfile.write(pdf)
                return
            if self.path == "/email-report":
                audit = json.loads(data.get("report", ["{}"])[0])
                pdf = create_pdf(audit)
                send_report_email(data.get("email", [""])[0], audit, pdf)
                self.respond_html(page_shell(render_audit(audit), audit=audit, notice="Report email sent."))
                return
        except Exception as exc:
            self.respond_html(page_shell(f"<section class='error'><h2>Audit could not run</h2><p>{esc(exc)}</p></section>", notice=str(exc)))
            return
        self.send_error(404)

    def log_message(self, fmt, *args):
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))

    def respond_html(self, body):
        self.respond("text/html; charset=utf-8", body.encode("utf-8"))

    def respond(self, content_type, body):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


STYLES = """
:root { color-scheme: light; --ink: #17202a; --muted: #5c6b7a; --line: #d9e2ea; --brand: #0f766e; --brand-dark: #115e59; --soft: #f6f8fb; --danger: #b42318; --ok: #138a45; }
* { box-sizing: border-box; }
body { margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: #ffffff; }
.topbar { background: #102a43; color: white; padding: 30px clamp(18px, 4vw, 52px); border-bottom: 5px solid var(--brand); }
.topbar h1 { margin: 0 0 8px; font-size: clamp(28px, 5vw, 46px); letter-spacing: 0; }
.topbar p { margin: 0; color: #cfe0ef; max-width: 780px; }
main { width: min(1180px, calc(100% - 32px)); margin: 24px auto 60px; }
.scan-form { display: grid; grid-template-columns: 1fr auto; gap: 12px; margin-bottom: 16px; }
input, button { min-height: 44px; border-radius: 7px; font: inherit; }
input { border: 1px solid var(--line); padding: 0 14px; min-width: 0; }
button { border: 0; padding: 0 18px; background: var(--brand); color: white; cursor: pointer; font-weight: 700; }
button:hover { background: var(--brand-dark); }
.actions { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin: 12px 0 20px; }
.actions form { display: flex; gap: 8px; align-items: center; }
.email-form input { width: min(320px, 58vw); }
.notice { border: 1px solid #a7f3d0; background: #ecfdf5; padding: 12px 14px; border-radius: 7px; margin-bottom: 14px; }
.summary { display: flex; align-items: center; gap: 18px; background: var(--soft); border: 1px solid var(--line); padding: 18px; border-radius: 8px; margin: 16px 0; }
.summary h2 { margin: 0 0 6px; font-size: 18px; overflow-wrap: anywhere; }
.summary p { margin: 0; color: var(--muted); }
.score { width: 96px; height: 96px; flex: 0 0 96px; border-radius: 50%; display: grid; place-content: center; border: 8px solid var(--brand); background: white; text-align: center; }
.score span { font-size: 31px; font-weight: 800; line-height: 1; }
.score small { color: var(--muted); }
.tabs { display: flex; flex-wrap: wrap; gap: 8px; border-bottom: 1px solid var(--line); margin-top: 22px; }
.tabs button { min-height: 40px; border-radius: 7px 7px 0 0; background: #e9eef4; color: var(--ink); }
.tabs button.active { background: var(--brand); color: white; }
.tab-panel { display: none; padding-top: 18px; }
.tab-panel.active { display: block; }
.panel { border: 1px solid var(--line); border-radius: 8px; padding: 16px; margin-bottom: 16px; overflow: auto; }
.panel h2, .empty h2, .error h2 { margin: 0 0 12px; font-size: 20px; }
.grid.two { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
table { width: 100%; border-collapse: collapse; min-width: 720px; }
th, td { text-align: left; border-bottom: 1px solid var(--line); padding: 10px 9px; vertical-align: top; }
th { color: var(--muted); font-size: 13px; }
a { color: var(--brand-dark); overflow-wrap: anywhere; }
.badge { display: inline-flex; min-width: 46px; justify-content: center; border-radius: 999px; padding: 3px 8px; color: white; font-size: 12px; font-weight: 800; }
.badge.pass { background: var(--ok); }
.badge.fail { background: var(--danger); }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #f4f7fa; padding: 12px; border-radius: 7px; max-height: 360px; overflow: auto; }
.empty, .error { border: 1px solid var(--line); background: var(--soft); padding: 24px; border-radius: 8px; }
.error { border-color: #fda29b; background: #fff1f0; }
@media (max-width: 720px) {
  .scan-form { grid-template-columns: 1fr; }
  .summary { align-items: flex-start; }
  .grid.two { grid-template-columns: 1fr; }
  .actions, .actions form { align-items: stretch; width: 100%; }
  .actions form { flex-direction: column; }
  .email-form input, .actions button { width: 100%; }
}
"""


SCRIPT = """
document.querySelectorAll('.tabs button').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelectorAll('.tabs button').forEach((item) => item.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach((item) => item.classList.remove('active'));
    button.classList.add('active');
    document.getElementById(button.dataset.tab).classList.add('active');
  });
});
"""


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), SeoAuditHandler)
    print(f"SEO Audit Tool running at http://{HOST}:{PORT}")
    print("Set PAGESPEED_API_KEY to enable Google PageSpeed Insights API checks.")
    server.serve_forever()
