"""
Comet Browser MCP Server
========================
An MCP server that lets Claude Desktop control the Comet browser (Perplexity's
AI browser) via Chrome DevTools Protocol. Claude can type searches, navigate
pages, read content, take screenshots, and interact with web elements.

Usage:
  1. Launch Comet with remote debugging:
     comet.exe --remote-debugging-port=9222
  2. Start this MCP server (configured in Claude Desktop's config)
  3. Claude can now control your Comet browser!
"""

import asyncio
import base64
import json
import os
import subprocess
import urllib.request
from typing import Optional
from urllib.parse import quote, urlparse

import sys

from mcp.server.fastmcp import FastMCP
from playwright.async_api import async_playwright, Browser, Page

from content_filter import ContentFilter

_filter = ContentFilter()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CDP_URL = os.environ.get("COMET_CDP_URL", "http://localhost:9222")
DEFAULT_TIMEOUT = int(os.environ.get("COMET_TIMEOUT", "30000"))  # ms
MAX_CONTENT_LENGTH = int(os.environ.get("COMET_MAX_CONTENT", "50000"))  # chars
MAX_WAIT_SECONDS = 120  # hard cap for any sleep/wait parameter


# ── Tab classification helpers ──────────────────────────────────────────

_INTERNAL_URL_PREFIXES = ("chrome://", "chrome-extension://", "devtools://")

def _classify_tab_purpose(url: str) -> str:
    """Classify a tab's purpose based on its URL."""
    if not url or url == "about:blank":
        return "INTERNAL"
    if any(url.startswith(prefix) for prefix in _INTERNAL_URL_PREFIXES):
        return "INTERNAL"
    if "perplexity.ai" in url:
        return "MAIN"
    return "BROWSING"

def _match_domain(page_url: str, domain_query: str) -> bool:
    """Check if a page URL matches a domain query (case-insensitive, substring match)."""
    try:
        hostname = urlparse(page_url).hostname or ""
        return domain_query.lower() in hostname.lower()
    except Exception:
        return False


def _clamp_wait(value: int, default: int = 10) -> int:
    """Clamp a wait/sleep value to [0, MAX_WAIT_SECONDS]."""
    if not isinstance(value, (int, float)):
        return default
    return max(0, min(int(value), MAX_WAIT_SECONDS))

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
_playwright = None
_browser: Optional[Browser] = None
_page: Optional[Page] = None

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------
mcp = FastMCP("comet_mcp")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _find_comet_path() -> Optional[str]:
    """Find Comet executable on Windows."""
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Perplexity", "Comet", "Application", "comet.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Perplexity", "Comet", "Application", "comet.exe"),
        os.environ.get("COMET_PATH", ""),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


async def _launch_comet(port: int = 9222) -> bool:
    """Launch Comet with remote debugging enabled."""
    path = _find_comet_path()
    if not path:
        return False
    try:
        creation_flags = 0
        if hasattr(subprocess, "DETACHED_PROCESS"):
            creation_flags = subprocess.DETACHED_PROCESS
        subprocess.Popen(
            [path, f"--remote-debugging-port={port}"],
            creationflags=creation_flags,
        )
        for _ in range(20):
            try:
                urllib.request.urlopen(f"http://localhost:{port}/json", timeout=1)
                return True
            except Exception:
                await asyncio.sleep(0.5)
    except Exception:
        pass
    return False


async def _ensure_browser() -> Browser:
    """Connect to Comet via CDP. Auto-launch if needed."""
    global _playwright, _browser
    if _browser and _browser.is_connected():
        return _browser

    # Clean up stale state
    if _playwright:
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = None
    _browser = None

    _playwright = await async_playwright().start()

    # First attempt to connect
    try:
        _browser = await _playwright.chromium.connect_over_cdp(CDP_URL)
        return _browser
    except Exception:
        pass

    # Try auto-launching Comet
    launched = await _launch_comet()
    if launched:
        try:
            _browser = await _playwright.chromium.connect_over_cdp(CDP_URL)
            return _browser
        except Exception as e:
            raise ConnectionError(
                f"Comet launched but could not connect via CDP at {CDP_URL}: {e}"
            )

    raise ConnectionError(
        f"Could not connect to Comet at {CDP_URL}. "
        f"Make sure Comet is running with --remote-debugging-port=9222."
    )


async def _get_page() -> Page:
    """Get the active page, or the first available one."""
    global _page
    browser = await _ensure_browser()
    contexts = browser.contexts
    if not contexts:
        raise RuntimeError("No browser contexts found. Is Comet open?")
    pages = contexts[0].pages
    if _page and _page in pages and not _page.is_closed():
        return _page
    if pages:
        _page = pages[-1]
        return _page
    _page = await contexts[0].new_page()
    return _page


async def _extract_text(
    page: Page,
    selector: Optional[str] = None,
    include_links: bool = True,
    max_length: int = MAX_CONTENT_LENGTH,
) -> str:
    """Extract readable text content from the page or a specific element."""
    js_code = """
    (root) => {
        const node = root || document.body;
        const blocks = [];
        const walk = (el) => {
            if (!el) return;
            const tag = el.tagName ? el.tagName.toLowerCase() : '';
            const skip = ['script', 'style', 'noscript', 'svg', 'path'];
            if (skip.includes(tag)) return;
            if (el.nodeType === Node.TEXT_NODE) {
                const t = el.textContent.trim();
                if (t) blocks.push(t);
                return;
            }
            if (tag === 'a' && el.href && INCLUDE_LINKS) {
                const text = el.textContent.trim();
                if (text) {
                    blocks.push(`[${text}](${el.href})`);
                    return;
                }
            }
            for (const child of el.childNodes) walk(child);
            const blockTags = ['div', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                             'li', 'tr', 'br', 'hr', 'section', 'article'];
            if (blockTags.includes(tag)) blocks.push('\\n');
        };
        walk(node);
        return blocks.join(' ').replace(/\\n\\s*\\n/g, '\\n\\n').trim();
    }
    """.replace("INCLUDE_LINKS", "true" if include_links else "false")

    if selector:
        element = await page.query_selector(selector)
        if not element:
            return f"No element found matching selector: {selector}"
        text = await element.evaluate(js_code)
    else:
        text = await page.evaluate(f"({js_code})(null)")

    if len(text) > max_length:
        text = text[:max_length] + f"\n\n... [truncated at {max_length} chars]"
    return text


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool()
async def comet_connect() -> str:
    """Connect to the Comet browser via CDP. Auto-launches Comet if needed.

    Call this before using any other comet tool.
    """
    try:
        browser = await _ensure_browser()
        contexts = browser.contexts
        pages = contexts[0].pages if contexts else []
        page = await _get_page()
        title = await page.title()
        return (
            f"Connected to Comet at {CDP_URL}\n"
            f"Open tabs: {len(pages)}\n"
            f"Active page: {title}"
        )
    except Exception as e:
        error_type = type(e).__name__
        if "closed" in str(e).lower():
            return "Error: Browser connection lost. Use comet_connect to reconnect."
        return f"Error ({error_type}): {e}"


@mcp.tool()
async def comet_search(
    query: str,
    wait_seconds: int = 10,
    mode: str = "search",
) -> str:
    """Search the web using Perplexity via Comet.

    Navigates to perplexity.ai/search?q=QUERY and extracts the AI-generated
    results. No CSS selectors needed -- uses URL-based navigation.

    Args:
        query: The search query.
        wait_seconds: Seconds to wait for AI response generation.
        mode: Search mode -- 'search' or 'research'.
    """
    try:
        if mode not in ("search", "research"):
            return f"Error: mode must be 'search' or 'research', got '{mode}'"
        if not query or not query.strip():
            return "Error: query must not be empty."
        page = await _get_page()
        wait_seconds = _clamp_wait(wait_seconds, default=10)
        search_url = f"https://www.perplexity.ai/search?q={quote(query)}"
        if mode == "research":
            search_url += "&mode=research"
        await page.goto(search_url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
        await asyncio.sleep(wait_seconds)

        title = await page.title()
        url = page.url
        text = await _extract_text(page, include_links=True)

        # Extract external sources
        sources = await page.evaluate("""
            (() => {
                const links = Array.from(document.querySelectorAll('a[href]'));
                return links
                    .filter(a => a.href.startsWith('http') && !a.href.includes('perplexity.ai'))
                    .map(a => ({ text: a.textContent.trim(), url: a.href }))
                    .filter(l => l.text.length > 0)
                    .slice(0, 20);
            })()
        """)

        result = (
            f"## Search Results\n"
            f"**Query**: {query}\n"
            f"**Page Title**: {title}\n"
            f"**URL**: {url}\n\n"
            f"---\n\n{text}"
        )
        if sources:
            result += "\n\n## Sources\n"
            for s in sources:
                result += f"- [{s['text']}]({s['url']})\n"
        scan = _filter.sanitize(result, url)
        if scan.injection_detected:
            print(f"⚠️ INJECTION on {url}: {len(scan.threats)} patterns", file=sys.stderr)
        return scan.text
    except Exception as e:
        error_type = type(e).__name__
        if "closed" in str(e).lower():
            return "Error: Browser connection lost. Use comet_connect to reconnect."
        if "Timeout" in str(e):
            return f"Error: Search timed out. Try increasing wait_seconds. {e}"
        return f"Error ({error_type}): {e}"


@mcp.tool()
async def comet_navigate(
    url: str,
    wait_for: str = "domcontentloaded",
) -> str:
    """Navigate Comet to a specific URL.

    Args:
        url: The URL to navigate to.
        wait_for: Wait condition -- 'load', 'domcontentloaded', or 'networkidle'.
    """
    VALID_WAIT_FOR = ("load", "domcontentloaded", "networkidle")
    try:
        if wait_for not in VALID_WAIT_FOR:
            return f"Error: wait_for must be one of {VALID_WAIT_FOR}, got '{wait_for}'"
        page = await _get_page()
        await page.goto(url, wait_until=wait_for, timeout=DEFAULT_TIMEOUT)
        title = await page.title()
        final_url = page.url
        preview = await _extract_text(page, max_length=3000)
        scan = _filter.sanitize(preview, final_url)
        if scan.injection_detected:
            print(f"⚠️ INJECTION on {final_url}: {len(scan.threats)} patterns", file=sys.stderr)
        return (
            f"**Navigated to**: {final_url}\n"
            f"**Title**: {title}\n\n"
            f"**Preview** (first ~3000 chars):\n{scan.text}"
        )
    except Exception as e:
        error_type = type(e).__name__
        if "closed" in str(e).lower():
            return "Error: Browser connection lost. Use comet_connect to reconnect."
        if "Timeout" in str(e):
            return f"Error: Navigation timed out for {url}. {e}"
        return f"Error ({error_type}): {e}"


@mcp.tool()
async def comet_read_page(
    selector: Optional[str] = None,
    include_links: bool = True,
    max_length: int = 50000,
) -> str:
    """Read the text content of the current page in Comet.

    Args:
        selector: Optional CSS selector to read specific content.
        include_links: Whether to include href URLs from links.
        max_length: Maximum characters to return.
    """
    try:
        max_length = min(max(0, max_length), MAX_CONTENT_LENGTH)
        page = await _get_page()
        title = await page.title()
        url = page.url
        text = await _extract_text(
            page,
            selector=selector,
            include_links=include_links,
            max_length=max_length,
        )
        scan = _filter.sanitize(text, url)
        if scan.injection_detected:
            print(f"⚠️ INJECTION on {url}: {len(scan.threats)} patterns", file=sys.stderr)
        return (
            f"**Page**: {title}\n"
            f"**URL**: {url}\n"
            f"**Selector**: {selector or '(full page)'}\n\n"
            f"---\n\n{scan.text}"
        )
    except Exception as e:
        error_type = type(e).__name__
        if "closed" in str(e).lower():
            return "Error: Browser connection lost. Use comet_connect to reconnect."
        return f"Error ({error_type}): {e}"


@mcp.tool()
async def comet_screenshot(full_page: bool = False) -> str:
    """Take a screenshot of the current Comet page. Returns base64 PNG.

    Args:
        full_page: Whether to capture the full scrollable page.
    """
    try:
        page = await _get_page()
        # Use CDP directly — Playwright's screenshot hangs on Comet's font renderer
        cdp = await page.context.new_cdp_session(page)
        try:
            result = await cdp.send("Page.captureScreenshot", {"format": "png"})
            b64 = result["data"]
        finally:
            await cdp.detach()
        title = await page.title()
        return (
            f"**Screenshot of**: {title}\n"
            f"**URL**: {page.url}\n\n"
            f"![screenshot](data:image/png;base64,{b64})"
        )
    except Exception as e:
        error_type = type(e).__name__
        if "closed" in str(e).lower():
            return "Error: Browser connection lost. Use comet_connect to reconnect."
        return f"Error ({error_type}): {e}"


@mcp.tool()
async def comet_click(selector: str, wait_after: int = 2) -> str:
    """Click an element on the current Comet page.

    Args:
        selector: CSS selector or 'text=Something' for text matching.
        wait_after: Seconds to wait after clicking.
    """
    try:
        page = await _get_page()
        wait_after = _clamp_wait(wait_after, default=2)
        await page.click(selector, timeout=DEFAULT_TIMEOUT)
        if wait_after > 0:
            await asyncio.sleep(wait_after)
        title = await page.title()
        url = page.url
        return (
            f"**Clicked**: {selector}\n"
            f"**Current page**: {title}\n"
            f"**URL**: {url}"
        )
    except Exception as e:
        error_type = type(e).__name__
        if "closed" in str(e).lower():
            return "Error: Browser connection lost. Use comet_connect to reconnect."
        return f"Error ({error_type}): {e}"


@mcp.tool()
async def comet_type(
    selector: str,
    text: str,
    press_enter: bool = False,
    clear_first: bool = True,
) -> str:
    """Type text into an input field in the current Comet page.

    Args:
        selector: CSS selector of the input field.
        text: Text to type into the field.
        press_enter: Whether to press Enter after typing.
        clear_first: Whether to clear the field before typing.
    """
    try:
        page = await _get_page()
        if clear_first:
            await page.fill(selector, "")
        await page.type(selector, text, delay=50)
        if press_enter:
            await page.press(selector, "Enter")
            await asyncio.sleep(1)
        return (
            f"**Typed**: '{text}' into {selector}\n"
            f"**Enter pressed**: {press_enter}"
        )
    except Exception as e:
        error_type = type(e).__name__
        if "closed" in str(e).lower():
            return "Error: Browser connection lost. Use comet_connect to reconnect."
        return f"Error ({error_type}): {e}"


@mcp.tool()
async def comet_tabs(
    action: str = "list",
    tab_index: Optional[int] = None,
    url: Optional[str] = None,
    domain: Optional[str] = None,
) -> str:
    """Manage tabs in the Comet browser.

    Args:
        action: Tab action -- 'list', 'new', 'switch', 'close', 'clean'.
        tab_index: Tab index for 'switch' or 'close' (0-based).
        url: URL for 'new' tab action.
        domain: Domain substring for 'switch' or 'close' (e.g. 'github', 'stackoverflow').
    """
    global _page
    VALID_ACTIONS = ("list", "new", "switch", "close", "clean")
    if action not in VALID_ACTIONS:
        return f"Error: action must be one of {VALID_ACTIONS}, got '{action}'"
    try:
        browser = await _ensure_browser()
        contexts = browser.contexts
        if not contexts:
            return "Error: No browser contexts. Use comet_connect first."
        pages = contexts[0].pages
        if not pages:
            return "Error: No pages open."

        if action == "list":
            lines = []
            hidden_count = 0
            for i, p in enumerate(pages):
                purpose = _classify_tab_purpose(p.url)
                if purpose == "INTERNAL":
                    hidden_count += 1
                    continue
                active_marker = " [ACTIVE]" if p == _page else ""
                try:
                    title = await p.title()
                except Exception:
                    title = "(untitled)"
                lines.append(f"  [{i}] [{purpose}] {title} - {p.url}{active_marker}")
            result = f"**Open tabs** ({len(lines)} visible"
            if hidden_count:
                result += f", {hidden_count} internal hidden"
            result += "):\n"
            result += "\n".join(lines) if lines else "  (no visible tabs)"
            return result

        elif action == "new":
            new_page = await contexts[0].new_page()
            if url:
                await new_page.goto(url, wait_until="domcontentloaded")
            _page = new_page
            return f"**New tab opened**: {url or 'about:blank'}"

        elif action == "switch":
            if domain is not None:
                matches = [(i, p) for i, p in enumerate(pages) if _match_domain(p.url, domain)]
                if not matches:
                    return f"Error: No tab found matching domain '{domain}'."
                if len(matches) > 1:
                    descs = []
                    for idx, pg in matches:
                        try:
                            t = await pg.title()
                        except Exception:
                            t = "(untitled)"
                        descs.append(f"  [{idx}] {t} - {pg.url}")
                    return (
                        f"Error: Multiple tabs match domain '{domain}'. "
                        f"Specify tab_index:\n" + "\n".join(descs)
                    )
                tab_index = matches[0][0]
            if tab_index is None or tab_index < 0 or tab_index >= len(pages):
                return f"Error: Invalid tab index. Available: 0-{len(pages)-1}"
            try:
                _page = pages[tab_index]
                title = await _page.title()
                return f"**Switched to tab [{tab_index}]**: {title} - {_page.url}"
            except (IndexError, Exception) as tab_err:
                return f"Error: Tab {tab_index} is no longer available: {tab_err}"

        elif action == "close":
            if domain is not None:
                matches = [(i, p) for i, p in enumerate(pages) if _match_domain(p.url, domain)]
                if not matches:
                    return f"Error: No tab found matching domain '{domain}'."
                if len(matches) > 1:
                    descs = []
                    for idx, pg in matches:
                        try:
                            t = await pg.title()
                        except Exception:
                            t = "(untitled)"
                        descs.append(f"  [{idx}] {t} - {pg.url}")
                    return (
                        f"Error: Multiple tabs match domain '{domain}'. "
                        f"Specify tab_index:\n" + "\n".join(descs)
                    )
                tab_index = matches[0][0]
            if tab_index is None or tab_index < 0 or tab_index >= len(pages):
                return f"Error: Invalid tab index. Available: 0-{len(pages)-1}"
            if len(pages) <= 1:
                return "Error: Cannot close the last tab."
            try:
                target = pages[tab_index]
                title = await target.title()
                await target.close()
                if _page == target:
                    remaining = contexts[0].pages
                    _page = remaining[-1] if remaining else None
                return f"**Closed tab [{tab_index}]**: {title}"
            except (IndexError, Exception) as tab_err:
                return f"Error: Tab {tab_index} is no longer available: {tab_err}"

        elif action == "clean":
            closed_tabs = []
            protected = []
            for i in range(len(pages) - 1, -1, -1):
                p = pages[i]
                purpose = _classify_tab_purpose(p.url)
                if purpose != "BROWSING":
                    protected.append(f"  [{i}] [{purpose}] {p.url}")
                    continue
                if p == _page:
                    protected.append(f"  [{i}] [ACTIVE] {p.url}")
                    continue
                if len(pages) - len(closed_tabs) <= 1:
                    protected.append(f"  [{i}] [LAST] {p.url}")
                    break
                try:
                    title = await p.title()
                    await p.close()
                    closed_tabs.append(f"  [{i}] {title} - {p.url}")
                except Exception:
                    pass
            result = f"**Cleaned {len(closed_tabs)} browsing tab(s)**\n"
            if closed_tabs:
                result += "Closed:\n" + "\n".join(closed_tabs) + "\n"
            if protected:
                result += "Protected:\n" + "\n".join(protected)
            return result

    except Exception as e:
        if "closed" in str(e).lower():
            return "Error: Browser connection lost. Use comet_connect to reconnect."
        return f"Error ({type(e).__name__}): {e}"


@mcp.tool()
async def comet_evaluate(expression: str) -> str:
    """Evaluate a JavaScript expression in the current Comet page.

    Args:
        expression: JavaScript expression to evaluate in the page context.
    """
    try:
        page = await _get_page()
        result = await page.evaluate(expression)
        if isinstance(result, (dict, list)):
            result_str = json.dumps(result, indent=2, ensure_ascii=False)
        else:
            result_str = str(result)
        scan = _filter.sanitize(result_str, page.url)
        if scan.injection_detected:
            print(f"⚠️ INJECTION in eval on {page.url}: {len(scan.threats)} patterns", file=sys.stderr)
        return scan.text
    except Exception as e:
        error_type = type(e).__name__
        if "closed" in str(e).lower():
            return "Error: Browser connection lost. Use comet_connect to reconnect."
        return f"Error ({error_type}): {e}"


@mcp.tool()
async def comet_wait(
    selector: Optional[str] = None,
    seconds: Optional[int] = None,
) -> str:
    """Wait for a specific element or a fixed duration.

    Args:
        selector: CSS selector to wait for.
        seconds: Fixed seconds to wait.
    """
    try:
        page = await _get_page()
        if selector:
            await page.wait_for_selector(selector, timeout=DEFAULT_TIMEOUT)
            return f"**Element found**: {selector}"
        elif seconds:
            seconds = _clamp_wait(seconds, default=5)
            await asyncio.sleep(seconds)
            return f"**Waited**: {seconds} seconds"
        else:
            return "Error: Provide either 'selector' or 'seconds'."
    except Exception as e:
        error_type = type(e).__name__
        if "Timeout" in str(e):
            return f"**Timeout**: Element '{selector}' did not appear. {e}"
        if "closed" in str(e).lower():
            return "Error: Browser connection lost. Use comet_connect to reconnect."
        return f"Error ({error_type}): {e}"


# ---------------------------------------------------------------------------
# Deep Security Scan Tool
# ---------------------------------------------------------------------------
@mcp.tool()
async def comet_security_scan() -> str:
    """Deep security scan of current page. Detects hidden text,
    CSS-invisible elements, injection attempts, suspicious content."""
    try:
        page = await _get_page()
        url = page.url
        title = await page.title()

        # 1. Detect CSS-hidden elements and HTML comments via JS
        hidden_elements = await page.evaluate("""(() => {
            const suspicious = [];
            for (const el of document.querySelectorAll('*')) {
                const style = getComputedStyle(el);
                const text = el.textContent?.trim() || '';
                if (text.length < 20) continue;
                const isHidden = (
                    style.display === 'none' ||
                    style.visibility === 'hidden' ||
                    style.opacity === '0' ||
                    parseFloat(style.fontSize) < 2 ||
                    parseInt(style.height) === 0 ||
                    parseInt(style.width) === 0 ||
                    (style.position === 'absolute' && parseInt(style.left) < -1000) ||
                    (style.position === 'absolute' && parseInt(style.top) < -1000) ||
                    style.clipPath === 'inset(100%)'
                );
                const isSameColor = style.color === style.backgroundColor;
                if (isHidden || isSameColor) {
                    suspicious.push({
                        tag: el.tagName,
                        classes: el.className?.toString().substring(0, 100) || '',
                        text: text.substring(0, 300),
                        reason: isHidden ? 'css-hidden' : 'same-color-text'
                    });
                }
            }
            const comments = [...document.documentElement.outerHTML.matchAll(/<!--([\\s\\S]*?)-->/g)]
                .filter(m => m[1].trim().length > 50)
                .map(m => ({ tag: 'COMMENT', text: m[1].trim().substring(0, 300), reason: 'html-comment' }));
            return [...suspicious, ...comments];
        })()""")

        # 2. Scan visible content
        visible_text = await _extract_text(page)
        visible_scan = _filter.sanitize(visible_text, url)

        # 3. Scan hidden elements for injections
        hidden_threats = []
        for el in (hidden_elements or []):
            el_threats = _filter.detect_injections(el.get("text", ""))
            if el_threats:
                hidden_threats.append({"element": el, "threats": el_threats})

        # 4. Separate comments from hidden elements
        comments = [el for el in (hidden_elements or []) if el.get("reason") == "html-comment"]
        css_hidden = [el for el in (hidden_elements or []) if el.get("reason") != "html-comment"]

        # 5. Determine overall assessment
        total_threats = len(visible_scan.threats) + sum(len(h["threats"]) for h in hidden_threats)
        has_hidden = len(css_hidden) > 0 or len(comments) > 0
        if total_threats > 0:
            overall = "HOSTILE"
        elif has_hidden:
            overall = "SUSPICIOUS"
        else:
            overall = "CLEAN"

        # 6. Build report
        report = []
        report.append(f"🔍 DEEP SECURITY SCAN: {url}")
        report.append("════════════════════════════════════════")
        report.append(f"\nPage Trust Tier: {visible_scan.trust_tier.value}")
        report.append(f"\nVisible Content:")
        report.append(f"  Injection patterns: {len(visible_scan.threats)} found")
        for t in visible_scan.threats:
            report.append(f'  • [{t.category.value}] {t.pattern_name}: "{t.matched_text[:60]}" (pos {t.position})')

        report.append(f"\nHidden Elements: {len(css_hidden)} found")
        for h in hidden_threats:
            el = h["element"]
            if el.get("reason") == "html-comment":
                continue
            report.append(f'  • [{el.get("reason")}] <{el.get("tag")}>: "{el.get("text", "")[:80]}..."')
            report.append(f"    Injections in hidden text: {len(h['threats'])}")

        report.append(f"\nHTML Comments: {len(comments)} suspicious")
        for c in comments:
            report.append(f'  • "{c.get("text", "")[:80]}..."')
            # Check for injections in comments too
            comment_threats = _filter.detect_injections(c.get("text", ""))
            if comment_threats:
                report.append(f"    Injections found: {len(comment_threats)}")

        report.append(f"\nOverall: {overall}")
        report.append("════════════════════════════════════════")

        return "\n".join(report)
    except Exception as e:
        error_type = type(e).__name__
        if "closed" in str(e).lower():
            return "Error: Browser connection lost. Use comet_connect to reconnect."
        return f"Error ({error_type}): {e}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run()
