"""Design system for the approval screen: tokens, CSS, icons, chips.

Look: bright and modern, on a warm white page. Palette: golden yellow, sunset
orange, coral and deep navy (taken from the Golden Hour Photography logo), with
a sunset glow at the top of the page, white rounded cards with a soft shadow,
navy pill buttons, Geist font, Lucide line icons and status chips with a dot.

Security: every piece of customer-controlled text that ends up in HTML goes
through `esc()`. Emails are untrusted (some are prompt-injection attempts), so
a subject like `<img src=x onerror=...>` must render as text, never as markup.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from urllib.parse import quote

from app.ui_kit.icons import ICONS

# ---------------------------------------------------------------- tokens

NAVY = "#14213D"        # deep navy: text, primary buttons
NAVY_SOFT = "#2B3A5C"
GOLD = "#F5B301"        # golden yellow
GOLD_SOFT = "#FFF3C4"
ORANGE = "#FF7A1A"      # sunset orange
ORANGE_DARK = "#D9570B"
CORAL = "#FF6B6B"
TEAL = "#12A383"        # success
WARM_WHITE = "#FFF9F0"
SUNSET = f"linear-gradient(90deg, {GOLD}, {ORANGE} 55%, {CORAL})"

INK = NAVY
MUTED = "#5B6478"
FAINT = "#98A0B3"
LINE = "#F1E4CF"
PAGE = WARM_WHITE
CARD = "#FFFFFF"


@dataclass(frozen=True)
class StatusStyle:
    label: str
    icon: str
    color: str


STATUS: dict[str, StatusStyle] = {
    "PENDING_APPROVAL": StatusStyle("Pending approval", "clock", GOLD),
    "OWNER_QUEUE": StatusStyle("Owner queue", "user-round", ORANGE),
    "BLOCKED": StatusStyle("Blocked", "shield-x", "#F25C54"),
    "APPROVED": StatusStyle("Approved", "check", TEAL),
    "EDITED_APPROVED": StatusStyle("Edited & approved", "pencil-line", TEAL),
    "SENT": StatusStyle("Sent", "send", TEAL),
    "REJECTED": StatusStyle("Rejected", "circle-x", FAINT),
    "RECEIVED": StatusStyle("In progress", "loader-circle", "#3D5A99"),
    "ROUTED": StatusStyle("In progress", "loader-circle", "#3D5A99"),
    "DRAFTED": StatusStyle("In progress", "loader-circle", "#3D5A99"),
}
IN_PROGRESS = {"RECEIVED", "ROUTED", "DRAFTED"}

ROUTE: dict[str, tuple[str, str]] = {
    "quote_request": ("Quote request", "package"),
    "question": ("Question", "book-open"),
    "reschedule": ("Reschedule", "calendar"),
    "other": ("Other", "mail"),
    "spam": ("Spam", "shield-x"),
}

# Filter presets shown as chips: label -> (statuses, dot colour)
PRESETS: dict[str, tuple[set[str], str]] = {
    "Needs review": ({"PENDING_APPROVAL", "OWNER_QUEUE", "BLOCKED"} | IN_PROGRESS, INK),
    "Pending approval": ({"PENDING_APPROVAL"}, STATUS["PENDING_APPROVAL"].color),
    "Owner queue": ({"OWNER_QUEUE"}, STATUS["OWNER_QUEUE"].color),
    "Blocked": ({"BLOCKED"}, STATUS["BLOCKED"].color),
    "In progress": (IN_PROGRESS, STATUS["ROUTED"].color),
    "Sent": ({"SENT", "APPROVED", "EDITED_APPROVED"}, STATUS["SENT"].color),
    "Rejected": ({"REJECTED"}, STATUS["REJECTED"].color),
    "All": (set(STATUS), FAINT),
}

# ---------------------------------------------------------------- helpers


def esc(text: object) -> str:
    """Escape for HTML. `$` is escaped too so amounts never turn into LaTeX."""
    return html.escape(str(text), quote=True).replace("$", "&#36;")


def icon(name: str, size: int = 16, color: str = INK, stroke: float = 1.75) -> str:
    """A Lucide icon as an <img> with an SVG data URI. st.html sanitises its
    input and drops inline <svg>; images with data URIs are allowed, and keeping
    the sanitiser on matters because customer text is rendered nearby."""
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
           f'stroke="{color}" stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</svg>')
    return f'<img class="ghi" src="data:image/svg+xml;utf8,{quote(svg)}" width="{size}" height="{size}" alt="">'



def _mask_url(name: str) -> str:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="black" '
           f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</svg>')
    return f'url("data:image/svg+xml;utf8,{quote(svg)}")'


def button_icon_css(selector: str, name: str, after: bool = False) -> str:
    """Put a Lucide icon on a Streamlit button (buttons cannot hold SVG, so the
    icon is drawn with a CSS mask in the button's text colour)."""
    pseudo = "::after" if after else "::before"
    margin = "margin-left:6px" if after else "margin-right:6px"
    return (
        f"{selector} button p{pseudo}{{content:'';display:inline-block;width:15px;height:15px;{margin};"
        f"vertical-align:-2px;background-color:currentColor;-webkit-mask:{_mask_url(name)} no-repeat center/contain;"
        f"mask:{_mask_url(name)} no-repeat center/contain}}"
    )


def status_chip(status: str) -> str:
    s = STATUS.get(status, StatusStyle(status.title(), "circle-alert", FAINT))
    return f'<span class="gh-chip"><span class="gh-dot" style="background:{s.color}"></span>{esc(s.label)}</span>'


def route_chip(label: str | None, confidence: float | None = None) -> str:
    if not label:
        return '<span class="gh-muted">Not routed</span>'
    text, ic = ROUTE.get(label, (label, "mail"))
    conf = f'<span class="gh-faint">&nbsp;{confidence:.0%}</span>' if confidence is not None else ""
    return f'<span class="gh-chip gh-chip-plain">{icon(ic, 13, MUTED)}{esc(text)}{conf}</span>'


def card_title(name: str, title: str, extra: str = "") -> str:
    return f'<div class="gh-card-title">{icon(name, 16, MUTED)}<span>{esc(title)}</span>{extra}</div>'


def display_name(sender: str) -> tuple[str, str]:
    """'Jenna Morales <j@x>' -> ('Jenna Morales', 'j@x')."""
    from email.utils import parseaddr

    name, addr = parseaddr(sender)
    return (name or addr or sender), addr


# ---------------------------------------------------------------- CSS

CSS = f"""
@font-face {{ font-family: 'Geist'; src: url('app/static/fonts/Geist-Regular.woff2') format('woff2'); font-weight: 400; font-display: swap; }}
@font-face {{ font-family: 'Geist'; src: url('app/static/fonts/Geist-Medium.woff2') format('woff2'); font-weight: 500; font-display: swap; }}
@font-face {{ font-family: 'Geist'; src: url('app/static/fonts/Geist-SemiBold.woff2') format('woff2'); font-weight: 600; font-display: swap; }}

html, body, .stApp, .stMarkdown, .stHtml, button p, input, textarea, [data-baseweb="select"], [data-baseweb="tab"] {{
  font-family: 'Geist', ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif;
}}
.stApp {{
  background:
    radial-gradient(50% 420px at 8% -80px, rgba(255, 203, 71, 0.42), transparent 70%),
    radial-gradient(45% 380px at 55% -120px, rgba(255, 138, 61, 0.22), transparent 70%),
    radial-gradient(45% 400px at 95% -80px, rgba(255, 107, 107, 0.26), transparent 70%),
    {PAGE};
  color: {INK};
}}
header[data-testid="stHeader"] {{ background: transparent; }}
[data-testid="stDecoration"] {{ display: none; }}
.block-container {{ max-width: 1180px; padding-top: 2.6rem; padding-bottom: 4rem; }}
h1, h2, h3 {{ letter-spacing: -0.02em; font-weight: 600 !important; color: {INK}; }}

/* sidebar */
[data-testid="stSidebar"] {{ background: linear-gradient(180deg, #FFFFFF 0%, #FFF6E6 100%); border-right: 1px solid {LINE}; }}
.gh-logo {{ display: block; width: 100%; max-width: 210px; height: auto; margin: 0 auto 2px; }}
.gh-logo-sub {{ text-align: center; font-size: 11.5px; letter-spacing: .14em; text-transform: uppercase; color: {MUTED}; margin-bottom: 20px; }}
[data-testid="stSidebar"] .block-container, [data-testid="stSidebarContent"] {{ padding-top: 1.2rem; }}
.gh-brand {{ font-size: 18px; font-weight: 600; letter-spacing: -0.02em; color: {INK}; margin: 4px 0 2px; }}
.gh-brand-sub {{ font-size: 12.5px; color: {MUTED}; margin-bottom: 18px; }}
.gh-section {{ font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: {FAINT}; margin: 18px 0 8px; }}
.gh-stat {{ display: flex; align-items: center; gap: 9px; padding: 6px 8px; border-radius: 10px; font-size: 13.5px; color: {INK}; }}
.gh-stat .gh-count {{ margin-left: auto; font-variant-numeric: tabular-nums; color: {NAVY}; font-size: 12.5px; font-weight: 500;
  background: {GOLD_SOFT}; border-radius: 999px; padding: 1px 9px; }}
.gh-footnote {{ display:flex; gap:8px; align-items:flex-start; font-size: 12px; color: {MUTED}; margin-top: 22px; line-height: 1.45; }}

/* nav buttons in the sidebar */
div[class*="st-key-nav_"] .stButton button[kind] {{ justify-content: flex-start; width: 100%; border-radius: 10px !important; border: 0 !important;
  background: transparent !important; color: {MUTED} !important; font-weight: 500; padding: 6px 10px !important; min-height: 34px; }}
div[class*="st-key-nav_"] .stButton button[kind] > div {{ justify-content: flex-start; width: 100%; }}
div[class*="st-key-nav_"] .stButton button[kind]:hover {{ background: #FFF1D6 !important; color: {INK} !important; }}

/* text */
.gh-eyebrow {{ font-size: 12px; text-transform: uppercase; letter-spacing: .12em; color: {ORANGE_DARK}; font-weight: 500; margin-bottom: 6px; display:flex; gap:6px; align-items:center; }}
.gh-title {{ font-size: 30px; font-weight: 600; letter-spacing: -0.03em; line-height: 1.15; color: {INK}; margin: 0 0 6px; }}
.gh-subtitle {{ color: {MUTED}; font-size: 14.5px; margin-bottom: 20px; }}
.gh-muted {{ color: {MUTED}; }}
.gh-faint {{ color: {FAINT}; }}
.gh-mono {{ font-family: ui-monospace, 'SF Mono', Menlo, monospace !important; font-size: 12px; color: {MUTED}; }}
img.ghi {{ display: inline-block; vertical-align: -3px; flex-shrink: 0; }}

/* chips */
.gh-chip {{ display: inline-flex; align-items: center; gap: 7px; padding: 3px 10px 3px 9px; border-radius: 999px;
  background: #FFFFFF; border: 1px solid {LINE}; font-size: 12.5px; color: {INK}; white-space: nowrap; line-height: 1.5; }}
.gh-chip-plain {{ gap: 6px; background: #FFF4E3; border-color: transparent; color: {NAVY_SOFT}; }}
.gh-dot {{ width: 7px; height: 7px; border-radius: 999px; display: inline-block; flex-shrink: 0; }}

/* cards: bordered containers become white rounded cards with a soft shadow */
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > [data-testid="stVerticalBlock"]),
div[class*="st-key-card_"] {{
  background: {CARD}; border: 1px solid {LINE} !important; border-radius: 18px !important;
  box-shadow: 0 1px 2px rgba(20, 33, 61, 0.05), 0 16px 36px -20px rgba(20, 33, 61, 0.24);
}}
div[class*="st-key-card_"] {{ padding: 18px 20px 16px; }}
.gh-card-title {{ display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 14px; color: {INK}; margin-bottom: 10px; }}
.gh-card-title .gh-chip {{ margin-left: auto; font-weight: 400; }}

/* inbox list */
.gh-list-head {{ font-size: 11.5px; text-transform: uppercase; letter-spacing: .08em; color: {FAINT}; }}
div.st-key-list_head {{ border-bottom: 1px solid {LINE}; padding: 0 6px 6px; }}
div[class*="st-key-row_"] {{ border-bottom: 1px solid #F7EDDC; padding: 8px 6px; border-radius: 12px; transition: background .12s; }}
div[class*="st-key-row_"]:hover {{ background: #FFF8EC; }}
div[class*="st-key-row_"] [data-testid="stHorizontalBlock"] {{ align-items: center; }}
.gh-subject {{ font-weight: 500; color: {INK}; font-size: 14px; line-height: 1.35; }}
.gh-from {{ color: {MUTED}; font-size: 12.5px; margin-top: 2px; }}
.gh-note {{ display: flex; gap: 6px; align-items: center; color: {ORANGE_DARK}; font-size: 12px; margin-top: 4px; }}
.gh-date {{ color: {MUTED}; font-size: 12.5px; font-variant-numeric: tabular-nums; }}
.gh-empty {{ text-align: center; color: {MUTED}; padding: 48px 0; }}

/* callouts */
.gh-callout {{ display: flex; gap: 10px; align-items: flex-start; border-radius: 14px; padding: 12px 14px; font-size: 13.5px;
  line-height: 1.5; margin: 6px 0 14px; }}
.gh-callout-warn {{ background: #FFF1E6; border: 1px solid #FFD6B3; color: #8F3A06; }}
.gh-callout-info {{ background: #EEF2FA; border: 1px solid #D5DEF0; color: {NAVY}; }}
.gh-callout-ok {{ background: #E9F8F3; border: 1px solid #C3EBDD; color: #0B6E58; }}

/* email body and key/value grids */
.gh-email {{ white-space: pre-wrap; font-size: 13.5px; line-height: 1.6; color: {NAVY_SOFT}; background: #FFFBF4;
  border-radius: 12px; padding: 14px 16px; max-height: 340px; overflow: auto; border: 1px solid #F7EDDC; }}
.gh-kv {{ margin: 0; padding: 0; display: grid; grid-template-columns: 130px 1fr; row-gap: 8px; column-gap: 12px; font-size: 13.5px; }}
.gh-kv dt {{ color: {MUTED}; }}
.gh-kv dd {{ margin: 0; color: {INK}; }}
.gh-meta {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 2px 0 16px; color: {MUTED}; font-size: 13px; }}
.gh-bar {{ height: 5px; border-radius: 999px; background: #F6EBD9; overflow: hidden; margin: 6px 0 2px; max-width: 220px; }}
.gh-bar > span {{ display: block; height: 100%; background: {SUNSET}; border-radius: 999px; }}

/* evidence rows */
.gh-row {{ display: flex; gap: 10px; align-items: flex-start; padding: 10px 2px; border-bottom: 1px solid #F7EDDC; font-size: 13.5px; }}
.gh-row:last-child {{ border-bottom: 0; }}
.gh-row-main {{ flex: 1; min-width: 0; }}
.gh-row-title {{ font-weight: 500; color: {INK}; }}
.gh-row-sub {{ color: {MUTED}; font-size: 12.5px; margin-top: 2px; overflow-wrap: anywhere; }}
.gh-table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 6px; }}
.gh-table td {{ padding: 5px 0; border-bottom: 1px dashed #F1E4CF; }}
.gh-table td:last-child {{ text-align: right; font-variant-numeric: tabular-nums; }}
.gh-table tr.gh-total td {{ font-weight: 600; border-bottom: 0; padding-top: 8px; }}

/* buttons: black pills for primary, quiet pills for the rest */
.stButton button, .stDownloadButton button, .stFormSubmitButton button {{ border-radius: 999px !important; font-weight: 500; padding: 6px 16px; min-height: 36px;
  box-shadow: none; transition: transform .06s ease, background .12s ease; }}
.stButton button[kind="primary"], .stFormSubmitButton button[kind*="primary"] {{ background: {INK} !important; border: 1px solid {INK} !important; color: #FFFFFF !important; }}
.stButton button[kind="primary"]:hover, .stFormSubmitButton button[kind*="primary"]:hover {{
  background: linear-gradient(135deg, {ORANGE}, {CORAL}) !important; border-color: transparent !important; }}
.stButton button[kind="secondary"] {{ background: #FFFFFF !important; border: 1px solid {LINE} !important; color: {INK} !important; }}
.stButton button[kind="secondary"]:hover {{ border-color: {GOLD} !important; background: #FFFBF2 !important; }}
.stButton button[kind="tertiary"] {{ color: {MUTED} !important; padding-left: 4px; }}
.stButton button[kind="tertiary"]:hover {{ color: {INK} !important; }}
.stButton button:active {{ transform: translateY(1px); }}
.stButton button:disabled {{ opacity: .38; }}
[class*="st-key-open_"] button {{ min-height: 30px; padding: 3px 12px; font-size: 13px; white-space: nowrap; }}
[class*="st-key-open_"] button p {{ overflow: visible; text-overflow: clip; }}

/* inputs, tabs, pills */
.stTextArea textarea, .stTextInput input, [data-baseweb="select"] > div {{ border-radius: 12px !important; background: #FFFFFF !important; }}
div[class*="st-key-card_login"] {{ overflow: hidden; position: relative; }}
div[class*="st-key-card_login"]::before {{ content: ""; position: absolute; left: 0; right: 0; top: 0; height: 4px; background: {SUNSET}; }}
.stTextArea textarea {{ font-size: 14px; line-height: 1.6; }}
.stTabs [data-baseweb="tab-list"] {{ gap: 4px; border-bottom: 1px solid {LINE}; }}
.stTabs [data-baseweb="tab"] {{ height: 38px; padding: 0 12px; color: {MUTED}; }}
.stTabs [aria-selected="true"] {{ color: {INK}; }}
.stTabs [data-baseweb="tab-highlight"] {{ background: {SUNSET}; height: 3px; border-radius: 3px; }}
[data-testid="stButtonGroup"] button {{ border-radius: 999px !important; font-size: 13px; padding: 4px 12px 4px 10px; }}
[data-testid="stButtonGroup"] button[kind*="Active"], [data-testid="stButtonGroup"] button[aria-checked="true"] {{
  background: {INK} !important; color: #fff !important; border-color: {INK} !important; }}
[data-testid="stAlert"] {{ border-radius: 14px; }}
"""

# Coloured dots on the preset chips, in PRESETS order.
PRESET_DOT_CSS = "".join(
    f'.st-key-preset button:nth-of-type({i}) p::before{{content:"";display:inline-block;width:7px;height:7px;'
    f'border-radius:999px;background:{color};margin-right:7px;vertical-align:1px}}'
    for i, (_, color) in enumerate(PRESETS.values(), start=1)
)
