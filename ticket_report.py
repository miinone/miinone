from __future__ import annotations

import gzip
import os
import random
import re
import subprocess
import sys
import time
import zlib
from collections import deque
from datetime import datetime, timedelta
from html import escape, unescape
from pathlib import Path
from typing import Any, Iterable, Mapping

REQUIRED_PACKAGES: dict[str, str] = {
    "requests": "requests",
    "bs4": "beautifulsoup4",
    "playwright": "playwright",
}


def _ensure_runtime_dependencies() -> None:
    print("Checking required modules...")
    for module_name, pip_name in REQUIRED_PACKAGES.items():
        try:
            __import__(module_name)
        except ModuleNotFoundError:
            print(f"  Dependency missing: {module_name} ({pip_name}). Installing now...")
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", pip_name],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(
                    f"Failed to install required dependency '{pip_name}'.\n"
                    f"Please run: {sys.executable} -m pip install --upgrade --disable-pip-version-check {pip_name}\n"
                    f"Command output:\n{exc.stderr or exc.stdout or 'No output available'}"
                ) from exc
    print("Required module check complete.\n")


_ensure_runtime_dependencies()

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


def extract_airline_prices(text: str, limit: int = 12) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    generic = {
        "airlines",
        "smart filters",
        "filter flights",
        "nonstop",
        "1 stop",
        "2+ stops",
        "stops",
        "amenities",
        "carry-on bag",
        "checked bag",
        "times",
        "take-off",
        "landing",
        "airports",
        "depart/return same",
        "search",
        "book now",
        "usd",
        "round-trip",
        "one-way",
        "flights",
        "stays",
        "cars",
        "packages",
        "cruises",
        "plan with ai",
    }
    for idx, line in enumerate(lines[:-1]):
        candidate = line.strip()
        if not candidate or "$" in candidate:
            continue
        low = candidate.lower()
        if len(candidate) > 60 or low in generic:
            continue
        next_line = lines[idx + 1].strip()
        if not re.search(r"^\$\d+(?:,\d{3})*(?:\.\d{1,2})?$", next_line):
            continue
        key = f"{candidate}|{next_line}"
        if key in seen:
            continue
        seen.add(key)
        items.append({"airline": candidate, "price": next_line})
        if len(items) >= limit:
            break
    return items


def _normalize_ticket(ticket: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(ticket)
    data.setdefault("title", "Untitled ticket")
    data.setdefault("price", "Price not listed")
    data.setdefault("source", "")
    data.setdefault("source_url", data["source"])
    data.setdefault("image_url", "")
    return data


def _ticket_summary(ticket: Mapping[str, Any]) -> str:
    title = ticket.get("title", "Untitled ticket")
    price = ticket.get("price", "Price not listed")
    source = ticket.get("source") or ticket.get("source_url") or ""
    return f"{title} — {price}\nSource: {source}"


def _ticket_detail_lines(item: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    has_leg_details = any(item.get(f"{direction}_travel_time") for direction in ("outbound", "return"))
    if has_leg_details:
        for direction in ("outbound", "return"):
            route = item.get(f"{direction}_route") or direction.title()
            for field_name, label in (("travel_time", "Travel time"), ("stops", "Stops"), ("layovers", "Layovers")):
                value = item.get(f"{direction}_{field_name}")
                if value and value != "N/A":
                    lines.append(f"{label} ({route}): {value}")
        return lines

    for field_name in ("travel_time", "layovers", "stops"):
        value = item.get(field_name)
        if value:
            lines.append(f"{field_name.replace('_', ' ').title()}: {value}")
    return lines


def _send_discord_notification(webhook_url: str, summary: str, tickets: Iterable[Mapping[str, Any]]) -> None:
    ticket_list = [dict(ticket) for ticket in tickets]
    lines = [summary]
    if ticket_list:
        best_ticket = min(
            ticket_list,
            key=lambda ticket: _parse_price_value(ticket.get("price"))
            if _parse_price_value(ticket.get("price")) is not None else float("inf"),
        )
        lines.extend(("", f"**{best_ticket.get('title', 'Flight result')}**", f"**Price:** {best_ticket.get('price', 'Price not listed')}"))
        lines.extend(_ticket_detail_lines(best_ticket))
        source = best_ticket.get("source") or best_ticket.get("source_url")
        if source:
            lines.append(f"Source: <{source}>")

    content = "\n".join(lines)
    if len(content) > 2000:
        content = content[:1997] + "..."
    try:
        response = requests.post(
            webhook_url,
            json={"content": content, "allowed_mentions": {"parse": []}},
            timeout=20,
        )
    except requests.RequestException:
        raise RuntimeError("Discord webhook delivery failed.") from None
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Discord webhook returned HTTP {response.status_code}.")


def _export_txt(tickets: Iterable[Mapping[str, Any]]) -> str:
    lines: list[str] = ["Ticket report", "=" * 20, ""]
    for idx, ticket in enumerate(tickets, start=1):
        item = _normalize_ticket(ticket)
        lines.append(f"Ticket {idx}: {item['title']}")
        lines.append(f"Price: {item['price']}")
        lines.extend(_ticket_detail_lines(item))
        source = item.get("source") or item.get("source_url") or "No source provided"
        lines.append(f"Source: {source}")
        if item.get("image_url"):
            lines.append(f"Image: {item['image_url']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _export_rich_html(tickets: Iterable[Mapping[str, Any]]) -> str:
    ticket_cards: list[str] = []
    for ticket in tickets:
        item = _normalize_ticket(ticket)
        title = escape(str(item["title"]))
        price = escape(str(item["price"]))
        source = item.get("source") or item.get("source_url") or ""
        source_text = escape(source) if source else "No source provided"
        image_url = item.get("image_url") or ""
        image_html = ""
        if image_url:
            image_html = (
                f'<div class="ticket-image"><img src="{escape(image_url, quote=True)}" '
                'alt="Ticket image" /></div>'
            )
        extra_fields_html = [
            f"<p><strong>{escape(line.split(':', 1)[0])}:</strong> {escape(line.split(':', 1)[1].strip())}</p>"
            for line in _ticket_detail_lines(item)
        ]
        source_link = ""
        if source:
            source_link = (
                f'<p><strong>Source:</strong> '
                f'<a href="{escape(source, quote=True)}" target="_blank" rel="noopener noreferrer">'
                f'{source_text}</a></p>'
            )
        ticket_cards.append(
            "<article class=\"ticket-card\">"
            f"<h2>{title}</h2>"
            f"<p><strong>Price:</strong> {price}</p>"
            f"{''.join(extra_fields_html)}"
            f"{image_html}"
            f"{source_link}"
            "</article>"
        )

    body = "\n".join(ticket_cards) if ticket_cards else "<p>No tickets to display.</p>"
    return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <title>Ticket report</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 2rem; background: #f5f7fb; color: #1f2933; }}
    .ticket-card {{ background: white; border-radius: 12px; padding: 1rem 1.25rem; margin-bottom: 1rem; box-shadow: 0 2px 8px rgba(15, 23, 42, 0.08); }}
    .ticket-card h2 {{ margin-top: 0; }}
    .ticket-image {{ margin: 0.75rem 0; }}
    .ticket-image img {{ max-width: 100%; border-radius: 10px; border: 1px solid #d9e2ec; }}
    a {{ color: #0b57d0; }}
  </style>
</head>
<body>
  <h1>Ticket report</h1>
  {body}
</body>
</html>
"""


def _as_pdf_bytes(text_lines: list[str], image_path: str | None = None) -> bytes:
    """Create a minimal PDF containing the report text.

    A local image can be provided for future extension, but the minimal reliable
    PDF export is text-based and valid across PDF readers.
    """
    _ = image_path

    escaped_lines = []
    for line in text_lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        escaped_lines.append(escaped)

    page_content = "BT\n/F1 12 Tf\n50 760 Td\n"
    for idx, line in enumerate(escaped_lines[:25]):
        page_content += f"({line}) Tj\n"
        if idx < len(escaped_lines[:25]) - 1:
            page_content += "0 -18 Td\n"
    page_content += "ET"
    content_bytes = page_content.encode("latin-1", errors="replace")

    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content_bytes)} >>\nstream\n{page_content}\nendstream",
    ]

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n{obj}\nendobj\n".encode("latin-1", errors="replace"))

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))
    pdf.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("latin-1"))
    return bytes(pdf)


def _script_output_dir() -> Path:
    return Path(__file__).resolve().parent


def _resolve_output_path(raw_path: str | Path | None, default_name: str) -> Path:
    if raw_path is None:
        return _script_output_dir() / default_name
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = _script_output_dir() / candidate
    return candidate


def export_report(tickets: Iterable[Mapping[str, Any]], output_path: str | Path, format_name: str = "txt") -> Path:
    """Export a ticket list to a simple txt or rich PDF report.

    Each ticket record may include title, price, source/source_url, and image_url.
    The source is kept in the exported report and remains clickable in rich mode.
    """
    tickets = list(tickets)
    path = _resolve_output_path(output_path, "ticket_report.txt") if not str(output_path) else _resolve_output_path(output_path, "ticket_report.txt")
    format_name = (format_name or "txt").lower()

    if format_name == "txt":
        content = _export_txt(tickets)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    if format_name == "rich":
        content = _export_rich_html(tickets)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    if format_name in {"pdf", "rich-pdf", "rich_pdf"}:
        lines: list[str] = []
        for idx, ticket in enumerate(tickets, start=1):
            item = _normalize_ticket(ticket)
            lines.append(f"Ticket {idx}: {item['title']}")
            lines.append(f"Price: {item['price']}")
            lines.extend(_ticket_detail_lines(item))
            source = item.get("source") or item.get("source_url") or "No source provided"
            lines.append(f"Source: {source}")
            if item.get("image_url"):
                lines.append(f"Image: {item['image_url']}")
            lines.append("")
        image_path = None
        if tickets and (tickets[0].get("image_url") or tickets[0].get("image_url") == ""):
            candidate = tickets[0].get("image_url")
            if candidate:
                image_path = str(Path(candidate).expanduser()) if not candidate.startswith("http") else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_as_pdf_bytes(lines, image_path=image_path))
        return path

    raise ValueError("Unsupported export format. Use 'txt', 'rich', or 'pdf'.")


def _coerce_bool(value: str | Any | None, default: bool = False) -> bool:
    if value is None:
        return default
    lowered = str(value).strip().lower()
    if lowered in {"", "y", "yes", "true", "1"}:
        return True
    if lowered in {"n", "no", "false", "0"}:
        return False
    return default


_INPUT_BUFFER: deque[str] = deque()


def _prime_input_buffer() -> None:
    if _INPUT_BUFFER or sys.stdin is None or sys.stdin.isatty():
        return
    try:
        remaining = sys.stdin.read()
    except (OSError, ValueError):
        return
    if remaining:
        _INPUT_BUFFER.extend(remaining.splitlines())


def _peek_input() -> str:
    _prime_input_buffer()
    return _INPUT_BUFFER[0] if _INPUT_BUFFER else ""


def _read_input(prompt: str) -> str:
    _prime_input_buffer()
    if _INPUT_BUFFER:
        value = _INPUT_BUFFER.popleft()
        if prompt:
            print(prompt, end="", flush=True)
        return value
    try:
        return input(prompt)
    except EOFError:
        return ""


def _prompt_yes_no(prompt: str, default: bool = True) -> bool:
    accepted = {"y": True, "yes": True, "n": False, "no": False}
    while True:
        raw_value = _read_input(prompt)
        value = raw_value.strip().lower()
        if value == "":
            return default
        if value in accepted:
            return accepted[value]
        print("Please enter y or n.")


def _load_env_file(path: str | Path | None = None) -> dict[str, str]:
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path).expanduser())
    script_dir = Path(__file__).resolve().parent
    candidates.extend([
        script_dir / ".env",
        script_dir / "ticket_report.env",
        script_dir / ".ticketreport.env",
    ])

    seen: set[Path] = set()
    values: dict[str, str] = {}
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if not candidate.exists() or not candidate.is_file():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, value = [part.strip() for part in line.split(":", 1)]
            if key:
                values[key.lower()] = value
    return values


def _parse_price_value(raw_price: str | None) -> float | None:
    if not raw_price:
        return None
    cleaned = str(raw_price).strip()
    if not cleaned:
        return None

    candidate_patterns = [
        r"(?i)(?:USD|US\$|\$|EUR|€|GBP|£|JPY|¥|AUD)\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)",
        r"(?i)(?:CAD)\s+(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)",
    ]
    for pattern in candidate_patterns:
        match = re.search(pattern, cleaned)
        if not match:
            continue
        value = match.group(1).replace(",", "")
        try:
            return float(value)
        except ValueError:
            continue
    return None


def _extract_price_hint(text: str) -> str | None:
    if not text:
        return None
    matches: list[tuple[float, str]] = []
    candidate_patterns = [
        r"(?i)(?:USD|US\$|\$|EUR|€|GBP|£|JPY|¥|AUD)\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)",
        r"(?i)(?:CAD)\s+(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)",
    ]
    for pattern in candidate_patterns:
        for match in re.finditer(pattern, text):
            raw_value = match.group(1).replace(",", "")
            try:
                value = float(raw_value)
            except ValueError:
                continue
            matches.append((value, match.group(0).strip()))
    if not matches:
        return None
    return min(matches, key=lambda item: item[0])[1]


def _extract_flight_source_url(domain: str, text: str) -> str:
    if not text:
        return domain
    for match in re.finditer(r'href=["\']?(https?://[^"\'\s>]+)["\']?', text, flags=re.IGNORECASE):
        url = match.group(1).strip().rstrip('/').lower()
        if any(keyword in url for keyword in ("flight", "air", "search", "fare", "booking", "trip", "travel")):
            return match.group(1).strip().rstrip('/')
    return domain


def summarize_scan_results(origin: str, destination: str, round_trip: bool, results: Iterable[Mapping[str, Any]]) -> str:
    route_label = f"{origin} -> {destination}{' (round trip)' if round_trip else ' (one way)'}"
    result_list = [dict(item) for item in results if item.get("status") == "found"]
    if not result_list:
        return (
            f"No useful flight data was found for {route_label}. "
            "The scanned sites either blocked access, returned no clear fare information, or did not show a usable result."
        )

    priced = [(result, _parse_price_value(result.get("price_hint"))) for result in result_list]
    priced = [(result, price) for result, price in priced if price is not None]
    if not priced:
        return (
            f"A travel site responded for {route_label}, but no clear price was visible on the scanned pages. "
            "The cheapest fare could not be confirmed from the available data."
        )

    best_result, best_price = min(priced, key=lambda item: item[1])
    site = str(best_result.get("domain") or "a travel search page")
    return (
        f"Cheapest visible price for {route_label}: {best_result.get('price_hint')} via {site}. "
        f"This is the lowest fare the script could confirm from the scanned results."
    )


def _parse_duration_to_minutes(value: str) -> int:
    match = re.search(r"(\d+)\s*h\s*(\d+)\s*m", value, flags=re.IGNORECASE)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))
    match = re.search(r"(\d+)\s*h", value, flags=re.IGNORECASE)
    if match:
        return int(match.group(1)) * 60
    match = re.search(r"(\d+)\s*m", value, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return 0


def parse_kayak_trip_details(text: str, origin: str = "", destination: str = "") -> dict[str, str | None]:
    normalized = text.replace("\xa0", " ")
    price_values = re.findall(r"\$\d[\d,]*(?:\.\d{2})?", normalized)
    if price_values:
        price = min(price_values, key=lambda value: _parse_price_value(value) or float("inf"))
    else:
        price = None

    price_anchor = re.search(re.escape(str(price)) if price else r"$", normalized)
    if price and price_anchor:
        window_start = max(0, price_anchor.start() - 250)
        window_end = min(len(normalized), price_anchor.end() + 1200)
        window = normalized[window_start:window_end]
    else:
        window = normalized

    durations = re.findall(r"\d+\s*h\s*\d+\s*m", window, flags=re.IGNORECASE)
    if durations:
        travel_time = max(durations, key=_parse_duration_to_minutes)
    else:
        travel_time = None

    stop_matches = re.findall(r"(\d+)\+?\s*stops?", text, flags=re.IGNORECASE)
    if stop_matches:
        stops = f"{max(map(int, stop_matches))} stops"
    else:
        stops = None

    layover_matches = re.findall(r"(\d+\s*h\s*\d+\s*m)(?:.*?(?:layover|self-transfer|change planes))", window, flags=re.IGNORECASE)
    layovers = "; ".join(dict.fromkeys(match.strip() for match in layover_matches)) if layover_matches else None
    if not layovers:
        if "long layover" in window.lower():
            layovers = "Long layover"
    if not layovers and "self-transfer" in window.lower():
        layovers = "Self-transfer"

    parsed = {
        "price": price,
        "travel_time": travel_time,
        "stops": stops,
        "layovers": layovers,
    }
    parsed.update(_parse_kayak_trip_legs(normalized, origin, destination))
    return parsed


def _parse_kayak_leg_details(text: str) -> dict[str, str | None]:
    durations = list(re.finditer(r"\b(\d+\s*h\s*\d+\s*m)\b", text, flags=re.IGNORECASE))
    travel_durations = [
        match.group(1)
        for match in durations
        if "layover" not in text[match.end():match.end() + 20].lower()
    ]
    stop_match = re.search(r"\b(\d+)\s+stops?\b", text, flags=re.IGNORECASE)
    stops = None
    if stop_match:
        stop_count = stop_match.group(1)
        stops = f"{stop_count} stop" if stop_count == "1" else f"{stop_count} stops"
    layovers = re.findall(r"\b(\d+\s*h\s*\d+\s*m)\s+layover\b", text, flags=re.IGNORECASE)
    return {
        "travel_time": travel_durations[-1] if travel_durations else None,
        "stops": stops,
        "layovers": "; ".join(layovers) if layovers else None,
    }


def _parse_kayak_trip_legs(text: str, origin: str, destination: str) -> dict[str, str | None]:
    route_pattern = re.compile(r"(?<![A-Z0-9])([A-Z]{3})\s*[-–]\s*([A-Z]{3})(?![A-Z0-9])", re.IGNORECASE)
    route_matches = list(route_pattern.finditer(text))
    routes = {
        (origin.upper(), destination.upper()): "outbound",
        (destination.upper(), origin.upper()): "return",
    }
    details: dict[str, str | None] = {}
    previous_route_end = 0
    for route_match in route_matches:
        leg_text = text[previous_route_end:route_match.start()]
        previous_route_end = route_match.end()
        prefix = routes.get((route_match.group(1).upper(), route_match.group(2).upper()))
        if prefix:
            details.update({f"{prefix}_{key}": value for key, value in _parse_kayak_leg_details(leg_text).items()})
    return details


def _kayak_result_nodes(soup: BeautifulSoup) -> list[Any]:
    itinerary_nodes = soup.select("div.nrc6")
    if itinerary_nodes:
        return itinerary_nodes
    return [node for selector in ("article", "li", "div", "section") for node in soup.select(selector)]


def _is_paid_placement(text: str) -> bool:
    return bool(re.search(r"\b(?:paid placement|sponsored|advertisement)\b", text, flags=re.IGNORECASE))


def _extract_airline_name(text: str) -> str:
    match = re.search(
        r"\b([A-Z][A-Za-z&'-]*(?:\s+[A-Z][A-Za-z&'-]*){0,2})\s+\d+\s+stops?\b",
        text,
    )
    return match.group(1) if match else "Kayak flight offer"


def _add_ticket_leg_details(ticket: dict[str, Any], item: Mapping[str, Any], origin: str, destination: str) -> None:
    for direction, route in (("outbound", f"{origin} to {destination}"), ("return", f"{destination} to {origin}")):
        ticket[f"{direction}_route"] = route
        for field_name in ("travel_time", "stops", "layovers"):
            value = item.get(f"{direction}_{field_name}")
            if value and value != "N/A":
                ticket[f"{direction}_{field_name}"] = value


def build_ticket_rows_from_results(origin: str, destination: str, round_trip: bool, results: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    route_label = f"{origin} to {destination}{' round trip' if round_trip else ' one way'}"
    found = [dict(item) for item in results if item.get("status") == "found"]
    if not found:
        return [{
            "title": route_label,
            "price": "Price not listed",
            "source": "https://www.kayak.com/",
            "image_url": "",
            "travel_time": "N/A",
            "layovers": "N/A",
        }]

    tickets: list[dict[str, Any]] = []
    for item in found:
        price = item.get("price_hint") or "Price not listed"
        source = item.get("source_url") or item.get("domain") or "https://www.kayak.com/"
        airline = item.get("airline") or item.get("provider") or "Flight offer"
        title = f"{route_label} • {airline}" if airline and airline.lower() not in route_label.lower() else route_label
        travel_time = item.get("travel_time") or "N/A"
        layovers = item.get("layovers") or item.get("stops") or "N/A"
        stops = item.get("stops") or "N/A"
        if item.get("details"):
            parsed = parse_kayak_trip_details(item["details"])
            if parsed.get("travel_time"):
                travel_time = str(parsed["travel_time"])
            if parsed.get("layovers"):
                layovers = str(parsed["layovers"])
            if parsed.get("stops"):
                stops = str(parsed["stops"])
            if parsed.get("price"):
                price = str(parsed["price"])
        ticket = {
            "title": title,
            "price": price,
            "source": source,
            "image_url": item.get("image_url") or "",
            "travel_time": travel_time,
            "layovers": layovers,
            "stops": stops,
            "details": item.get("details") or "",
        }
        _add_ticket_leg_details(ticket, item, origin, destination)
        tickets.append(ticket)
    return tickets


def _best_ticket_from_results(origin: str, destination: str, round_trip: bool, results: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    tickets = build_ticket_rows_from_results(origin, destination, round_trip, results)
    if not tickets:
        return {
            "title": f"{origin} to {destination}{' round trip' if round_trip else ' one way'}",
            "price": "Price not listed",
            "source": "https://www.kayak.com/",
            "image_url": "",
        }

    best_result = min(
        tickets,
        key=lambda ticket: _parse_price_value(ticket.get("price")) if _parse_price_value(ticket.get("price")) is not None else float("inf"),
    )
    return {**best_result, "source": best_result.get("source") or "https://www.kayak.com/"}


KAYAK_SEARCH_URL_TEMPLATE = "https://www.kayak.com/flights/{origin}-{destination}/{departure_date}/{return_date}"
GOOGLE_FLIGHTS_URL_TEMPLATE = "https://www.google.com/travel/flights?hl=en&curr=USD&gl=US&flightType=ROUND_TRIP&departure={origin}&arrival={destination}&date={departure_date}&returnDate={return_date}"

DEFAULT_SEARCH_DOMAINS = [
    "https://www.kayak.com/",
    "https://www.google.com/travel/flights",
]


def _build_browser_headers() -> dict[str, str]:
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.2478.67",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    ]
    return {
        "User-Agent": random.choice(user_agents),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "max-age=0",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "sec-ch-ua": '"Chromium";v="125", "Google Chrome";v="125", ";Not A Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Referer": "https://www.google.com/",
    }


def _normalize_date_input(raw_value: str | None, default_offset_days: int = 0) -> str:
    if not raw_value or not raw_value.strip():
        today = datetime.now()
        target = datetime(today.year, today.month, today.day) + timedelta(days=default_offset_days)
        return target.strftime("%Y-%m-%d")

    value = raw_value.strip()
    formats = (
        "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y",
        "%Y%m%d", "%m%d%Y", "%m%d%y",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if parsed.strftime(fmt) == value:
            return parsed.strftime("%Y-%m-%d")
    accepted = "YYYY-MM-DD, MM/DD/YYYY, MM-DD-YYYY, MM/DD/YY, MM-DD-YY, YYYYMMDD, MMDDYYYY, or MMDDYY"
    raise ValueError(f"Invalid date '{value}'. Use one of these formats: {accepted}.")


def _looks_like_date(value: str | None) -> bool:
    if value is None:
        return False
    candidate = value.strip()
    if not candidate:
        return False
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}-\d{1,2}-\d{2,4}", candidate))


def _looks_like_url(value: str | None) -> bool:
    if value is None:
        return False
    candidate = value.strip()
    return bool(re.fullmatch(r"https?://\S+", candidate, flags=re.IGNORECASE))


def _read_departure_date(prompt: str, default_offset_days: int = 7) -> str:
    while True:
        raw_value = _read_input(prompt).strip()
        try:
            return _normalize_date_input(raw_value or None, default_offset_days=default_offset_days)
        except ValueError as exc:
            print(f"Error: {exc}")


def _build_kayak_search_url(origin: str, destination: str, departure_date: str, return_date: str | None = None) -> str:
    if return_date:
        return KAYAK_SEARCH_URL_TEMPLATE.format(origin=origin, destination=destination, departure_date=departure_date, return_date=return_date)
    return f"https://www.kayak.com/flights/{origin}-{destination}/{departure_date}"


def _score_kayak_block(text: str, origin: str = "", destination: str = "") -> int:
    score = 0
    lowered = text.lower()
    if re.search(r"cheapest|best|quickest", lowered):
        score += 60
    if re.search(r"\$\d[\d,]*(?:\.\d{2})?", text):
        score += 25
    if re.search(r"\d+\s*h\s*\d+\s*m|\d+\s*h\s*\d+\s*m", text, flags=re.IGNORECASE):
        score += 20
    if re.search(r"\d+\s+stops?", text, flags=re.IGNORECASE):
        score += 10
    if re.search(r"layover|self-transfer|change planes|YYZ|YVR|FLL|NRT", text, flags=re.IGNORECASE):
        score += 20
    if origin:
        score += 15 if origin.lower() in lowered else -25
    if destination:
        score += 15 if destination.lower() in lowered else -25
    return score


def _extract_kayak_cheapest_price_from_html(html: str) -> str | None:
    if not html:
        return None
    cleaned = re.sub(r"(?is)<script.*?</script>", " ", html)
    cleaned = re.sub(r"(?is)<style.*?</style>", " ", cleaned)
    cleaned = unescape(cleaned)

    try:
        soup = BeautifulSoup(cleaned, "html.parser")
        for block in soup.select('[class*="Hv20-content"], [class*="hv20-content"]'):
            text = re.sub(r"\s+", " ", block.get_text(" ", strip=True))
            lowered = text.lower()
            if any(token in lowered for token in ("cheapest", "best", "quickest")):
                match = re.search(r"\$(\d[\d,]*(?:\.\d{1,2})?)", text)
                if match:
                    return f"${match.group(1)}"

        for text_block in soup.stripped_strings:
            text = " ".join(soup.stripped_strings)
            lowered = text.lower()
            if any(token in lowered for token in ("cheapest", "best", "quickest")):
                match = re.search(r"\$(\d[\d,]*(?:\.\d{1,2})?)", text)
                if match:
                    return f"${match.group(1)}"
    except Exception:
        pass

    hv20_blocks: list[str] = []
    for block_match in re.finditer(r"(?is)<[^>]+class=[\"'][^\"']*Hv20-content[^\"']*[\"'][^>]*>(.*?)</div>", cleaned):
        block_html = block_match.group(1)
        text = re.sub(r"<[^>]+>", " ", block_html)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            hv20_blocks.append(text)

    for block_text in hv20_blocks:
        lowered = block_text.lower()
        if "cheapest" in lowered or "best" in lowered or "quickest" in lowered:
            numeric_match = re.search(r"\$(\d[\d,]*(?:\.\d{1,2})?)", block_text)
            if numeric_match:
                return f"${numeric_match.group(1)}"

    direct_patterns = [
        r"(?is)Hv20-content.*?\$(\d[\d,]*(?:\.\d{1,2})?)",
        r"(?is)class=[\"'][^\"']*Hv20-content[^\"']*[\"'].*?\$(\d[\d,]*(?:\.\d{1,2})?)",
        r"(?is)(?:cheapest|best|quickest).*?\$(\d[\d,]*(?:\.\d{1,2})?)",
    ]
    for pattern in direct_patterns:
        match = re.search(pattern, cleaned)
        if match:
            return f"${match.group(1)}"

    text = re.sub(r"<[^>]+>", " ", cleaned)
    text = re.sub(r"\s+", " ", text)
    lowered = text.lower()
    if "cheapest" not in lowered and "best" not in lowered and "quickest" not in lowered:
        return None

    candidates = [
        r"(?is)(?:cheapest|best|quickest)[^$]{0,200}\$(\d[\d,]*(?:\.\d{1,2})?)",
        r"(?is)(?:cheapest|best|quickest)[^0-9]{0,200}(\$\d[\d,]*(?:\.\d{1,2})?)",
    ]
    for pattern in candidates:
        match = re.search(pattern, text)
        if match:
            return match.group(1) if match.group(1).startswith("$") else f"${match.group(1)}"

    numeric_match = re.search(r"\$(\d[\d,]*(?:\.\d{1,2})?)", text)
    return f"${numeric_match.group(1)}" if numeric_match else None


def _extract_kayak_result_cards(html: str) -> list[dict[str, str]]:
    if not html:
        return []

    cleaned = re.sub(r"(?is)<script.*?</script>", " ", html)
    cleaned = re.sub(r"(?is)<style.*?</style>", " ", cleaned)
    soup = BeautifulSoup(cleaned, "html.parser")
    candidate_blocks = _kayak_result_nodes(soup)

    cards: list[dict[str, str]] = []
    for node in candidate_blocks:
        classes = " ".join(node.get("class", [])).lower()
        test_id = str(node.get("data-testid") or node.get("data-test-id") or "").lower()
        attrs = f"{classes} {test_id}"
        if not any(token in attrs for token in ["nrc6", "result", "card", "flight", "offer", "option", "item", "row", "trip"]):
            continue
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
        if not text or "$" not in text or _is_paid_placement(text):
            continue

        price_match = re.search(r"\$(\d[\d,]*(?:\.\d{1,2})?)", text)
        if not price_match:
            continue

        source_url = ""
        anchor = node.select_one("a[href]")
        if anchor and anchor.get("href"):
            href = anchor.get("href", "").strip()
            if href.startswith("http"):
                source_url = href

        details = text[:2000]
        cards.append({
            "price": f"${price_match.group(1)}",
            "details": details,
            "source_url": source_url,
            "airline": _extract_airline_name(text),
        })

    if cards:
        unique: list[dict[str, str]] = []
        seen: set[str] = set()
        for card in cards:
            signature = f"{card['price']}|{card['source_url']}|{card['details'][:120]}"
            if signature not in seen:
                seen.add(signature)
                unique.append(card)
        return unique

    return []


def _write_debug_html(url: str, label: str, html: str) -> Path | None:
    if not html or not html.strip():
        return None
    script_dir = Path(__file__).resolve().parent
    filename = f"{label}-{int(time.time() * 1000)}.html"
    path = script_dir / filename
    try:
        path.write_text(html, encoding="utf-8", errors="replace")
        return path
    except Exception:
        return None


def _save_raw_debug_response(label: str, response: requests.Response) -> tuple[Path, Path] | tuple[None, None]:
    script_dir = Path(__file__).resolve().parent
    stamp = int(time.time() * 1000)
    body_path = script_dir / f"{label}-{stamp}.raw"
    headers_path = script_dir / f"{label}-{stamp}.headers.txt"
    try:
        body_path.write_bytes(response.content)
        header_text = "\n".join(f"{key}: {value}" for key, value in response.headers.items()) + "\n"
        headers_path.write_text(header_text, encoding="utf-8", errors="replace")
        return body_path, headers_path
    except Exception:
        return None, None


def _decode_response_text(response: requests.Response | Any) -> str:
    raw = getattr(response, "content", b"") or b""
    if not raw:
        return ""

    content_encoding = (getattr(response, "headers", {}) or {}).get("Content-Encoding", "").lower()
    payload = raw

    if "br" in content_encoding:
        try:
            import brotli
            payload = brotli.decompress(raw)
        except Exception:
            payload = raw
    elif "gzip" in content_encoding or raw.startswith(b"\x1f\x8b"):
        try:
            payload = gzip.decompress(raw)
        except Exception:
            payload = raw
    elif "deflate" in content_encoding or raw[:2] in (b"\x78\x9c", b"\x78\x01", b"\x78\xda"):
        try:
            payload = zlib.decompress(raw)
        except Exception:
            payload = raw

    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        for guess in ("latin-1", "cp1252", "utf-8-sig"):
            try:
                return payload.decode(guess)
            except UnicodeDecodeError:
                continue
    return payload.decode("utf-8", errors="replace")


def _looks_like_readable_html(content: str) -> bool:
    text = content.lstrip().lower()
    if len(text) < 200:
        return False
    if "\x00" in content or "\ufffd" in content:
        return False
    markers = ("<!doctype html", "<html", "<head", "<body", "<script", "<div", "<title")
    return any(marker in text for marker in markers)


def _latest_debug_html_file() -> Path | None:
    script_dir = Path(__file__).resolve().parent
    patterns = ("kayak_debug-*.html", "curl_kayak_debug-*.html")
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(script_dir.glob(pattern))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime)


def _read_debug_html_if_valid(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    if not text.strip() or not _looks_like_readable_html(text):
        return None
    return text


def _cleanup_debug_html(paths: Iterable[Path | str]) -> None:
    for candidate in paths:
        path = Path(candidate)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass


def _matching_kayak_row_count(page: Any, origin: str, destination: str, round_trip: bool) -> int:
    outbound = re.compile(rf"\b{re.escape(origin)}\s*[-–]\s*{re.escape(destination)}\b", re.IGNORECASE)
    returning = re.compile(rf"\b{re.escape(destination)}\s*[-–]\s*{re.escape(origin)}\b", re.IGNORECASE)
    try:
        row_texts = page.locator("div.nrc6").all_inner_texts()
    except Exception:
        return 0
    return sum(
        bool("$" in text and outbound.search(text) and (not round_trip or returning.search(text)))
        for text in row_texts
    )


def _watch_kayak_flight_requests(page: Any) -> dict[str, Any]:
    activity: dict[str, Any] = {
        "active_requests": set(),
        "completed": 0,
        "last_event_at": None,
        "last_request_url": "",
        "flight_poll_succeeded": False,
        "flight_poll_status": None,
    }

    def is_flight_request(request: Any) -> bool:
        request_url = request.url.lower()
        if "/i/api/search/perf/" in request_url:
            return False
        return any(marker in request_url for marker in (
            "/i/api/search/",
            "/i/api/xsell/v1/results",
            "/i/api/priceprediction/flights/",
        ))

    def record_activity(request: Any) -> None:
        activity["last_event_at"] = time.monotonic()
        activity["last_request_url"] = request.url

    def on_request(request: Any) -> None:
        if is_flight_request(request):
            activity["active_requests"].add(id(request))
            record_activity(request)

    def on_response(response: Any) -> None:
        request = response.request
        if not is_flight_request(request):
            return
        record_activity(request)
        if "/i/api/search/dynamic/flights/poll" in request.url.lower():
            activity["flight_poll_status"] = response.status
            activity["flight_poll_succeeded"] = 200 <= response.status < 300

    def on_request_finished(request: Any) -> None:
        if is_flight_request(request):
            activity["active_requests"].discard(id(request))
            activity["completed"] += 1
            record_activity(request)

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfinished", on_request_finished)
    page.on("requestfailed", on_request_finished)
    return activity


def _wait_for_kayak_results(
    page: Any,
    activity: dict[str, Any],
    origin: str,
    destination: str,
    round_trip: bool,
    timeout_ms: int = 30000,
    quiet_ms: int = 1500,
    poll_ms: int = 250,
) -> dict[str, Any]:
    started_at = time.monotonic()
    deadline = started_at + timeout_ms / 1000
    while time.monotonic() < deadline:
        now = time.monotonic()
        matching_rows = _matching_kayak_row_count(page, origin, destination, round_trip)
        last_event_at = activity["last_event_at"]
        quiet_for_ms = int((now - last_event_at) * 1000) if last_event_at is not None else 0
        active_count = len(activity["active_requests"])
        ready = (
            activity["flight_poll_succeeded"]
            and matching_rows > 0
            and active_count == 0
            and quiet_for_ms >= quiet_ms
        )
        _print_payload_progress(activity["completed"], active_count, finished=ready)
        if ready:
            return {
                "status": "ready",
                "elapsed_ms": int((now - started_at) * 1000),
                "flight_poll_status": activity["flight_poll_status"],
                "matching_rows": matching_rows,
                "completed_requests": activity["completed"],
                "last_flight_request": activity["last_request_url"],
            }
        remaining_ms = max(1, int((deadline - now) * 1000))
        page.wait_for_timeout(min(poll_ms, remaining_ms))

    _print_payload_progress(activity["completed"], len(activity["active_requests"]), finished=True)
    return {
        "status": "timeout",
        "elapsed_ms": timeout_ms,
        "flight_poll_status": activity["flight_poll_status"],
        "matching_rows": _matching_kayak_row_count(page, origin, destination, round_trip),
        "completed_requests": activity["completed"],
        "last_flight_request": activity["last_request_url"],
    }


def _scan_kayak(origin: str, destination: str, round_trip: bool, departure_date: str, return_date: str | None = None) -> dict[str, str]:
    url = _build_kayak_search_url(origin, destination, departure_date, return_date if round_trip else None)
    url_with_sort = url + "?sort=price_a"
    debug_html_paths: list[Path] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context(
                viewport={"width": 1440, "height": 1200},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                locale="en-US",
                timezone_id="America/New_York",
                java_script_enabled=True,
                ignore_https_errors=True,
            )
            page = context.new_page()
            flight_activity = _watch_kayak_flight_requests(page)

            print(f"  Opening Kayak browser render: {url_with_sort}")
            page.goto(url_with_sort, wait_until="domcontentloaded", timeout=30000)
            print("  Waiting for successful flight poll, matching itinerary rows, and flight-request quiet...")
            load_state = _wait_for_kayak_results(page, flight_activity, origin, destination, round_trip)
            print(f"  Kayak result readiness: {load_state}")

            body_text = page.locator("body").inner_text()
            bot_like = any(token in body_text.lower() for token in ["what is a bot", "bot", "verify you are human", "access denied", "captcha", "cloudflare"])
            if bot_like:
                browser.close()
                return {"domain": url_with_sort, "status": "botblocked", "details": "Kayak bot challenge page detected", "price_hint": None, "source_url": url_with_sort}

            page_html = page.content()
            downloaded_path = _write_debug_html(url_with_sort, "kayak_debug", page_html)
            if downloaded_path is not None:
                debug_html_paths.append(downloaded_path)
                print(f"  Captured temporary DOM snapshot: {downloaded_path.name}")

            price_hint = _extract_kayak_cheapest_price_from_html(page_html)
            cards = _extract_kayak_result_cards(page_html)
            if cards:
                best_card = min(cards, key=lambda item: _parse_price_value(item.get("price")) if _parse_price_value(item.get("price")) is not None else float("inf"))
                card_details = parse_kayak_trip_details(best_card.get("details") or page_html, origin, destination)
                browser.close()
                return {
                    "domain": url_with_sort,
                    "status": "found",
                    "details": "Kayak hydrated result cards loaded and the cheapest itinerary was parsed from the final DOM",
                    "price_hint": best_card.get("price"),
                    "source_url": best_card.get("source_url") or url_with_sort,
                    "travel_time": card_details.get("travel_time") or "N/A",
                    "layovers": card_details.get("layovers") or "N/A",
                    "stops": card_details.get("stops") or "N/A",
                    "outbound_travel_time": card_details.get("outbound_travel_time") or "N/A",
                    "outbound_stops": card_details.get("outbound_stops") or "N/A",
                    "outbound_layovers": card_details.get("outbound_layovers") or "N/A",
                    "return_travel_time": card_details.get("return_travel_time") or "N/A",
                    "return_stops": card_details.get("return_stops") or "N/A",
                    "return_layovers": card_details.get("return_layovers") or "N/A",
                    "airline": best_card.get("airline") or "Kayak flight offer",
                }

            if price_hint:
                browser.close()
                return {
                    "domain": url_with_sort,
                    "status": "found",
                    "details": "Kayak final DOM parsed for the cheapest visible price",
                    "price_hint": price_hint,
                    "source_url": url_with_sort,
                    "travel_time": "N/A",
                    "layovers": "N/A",
                    "stops": "N/A",
                    "airline": "Kayak flight offer",
                }

            browser.close()
            return {
                "domain": url_with_sort,
                "status": "no info",
                "details": "Kayak browser render completed but no usable flight cards or price data were visible in the final DOM",
                "price_hint": None,
                "source_url": url_with_sort,
            }
    except Exception as exc:
        try:
            response = requests.get(url_with_sort, headers=_build_browser_headers(), timeout=20)
            raw_debug_file, raw_headers_file = _save_raw_debug_response("kayak_debug", response)
            if raw_debug_file is not None:
                print(f"  Saved raw response: {raw_debug_file.name}")
            if raw_headers_file is not None:
                print(f"  Saved headers: {raw_headers_file.name}")
            page_html = _decode_response_text(response)
            saved_path = _write_debug_html(url_with_sort, "kayak_debug", page_html)
            if saved_path is not None:
                debug_html_paths.append(saved_path)
                print(f"  Saved decoded fallback HTML locally: {saved_path.name}")
            price_hint = _extract_kayak_cheapest_price_from_html(page_html)
            if price_hint:
                return {
                    "domain": url_with_sort,
                    "status": "found",
                    "details": "Kayak request fallback parsed a visible price after the browser render failed",
                    "price_hint": price_hint,
                    "source_url": url_with_sort,
                    "travel_time": "N/A",
                    "layovers": "N/A",
                    "stops": "N/A",
                    "airline": "Kayak flight offer",
                }
        except Exception:
            pass
        return {"domain": url_with_sort, "status": "failed", "details": f"Kayak browser load failed: {exc}", "price_hint": None, "source_url": url_with_sort}
    finally:
        _cleanup_debug_html(debug_html_paths)


def _scan_site(domain: str, origin: str, destination: str, round_trip: bool, departure_date: str | None = None, return_date: str | None = None) -> dict[str, str]:
    if "kayak.com" in domain.lower():
        departure_value = _normalize_date_input(departure_date, default_offset_days=7)
        return_value = _normalize_date_input(return_date, default_offset_days=9) if round_trip else None
        return _scan_kayak(origin, destination, round_trip, departure_value, return_value)

    session = requests.Session()
    session.headers.update(_build_browser_headers())
    time.sleep(random.uniform(0.2, 0.7))
    try:
        response = session.get(domain, timeout=12, allow_redirects=True)
        status = response.status_code
        text = _decode_response_text(response)
        page_text = text.lower()
        price_hint = _extract_price_hint(text)
        source_url = _extract_flight_source_url(domain, text)
        if status in {403, 429} or "captcha" in page_text or "verify you are human" in page_text:
            return {"domain": domain, "status": "botblocked", "details": f"HTTP {status} / bot challenge detected", "price_hint": None, "source_url": source_url}
        if status >= 400:
            return {"domain": domain, "status": "failed", "details": f"HTTP {status}", "price_hint": None, "source_url": source_url}
        if "flight" in page_text or "airline" in page_text or "airport" in page_text or "search" in page_text:
            return {"domain": domain, "status": "found", "details": f"HTTP {status} / search page reachable", "price_hint": price_hint, "source_url": source_url}
        return {"domain": domain, "status": "no info", "details": f"HTTP {status} / page loaded but no obvious flight data", "price_hint": None, "source_url": source_url}
    except requests.RequestException as exc:
        return {"domain": domain, "status": "failed", "details": str(exc), "price_hint": None, "source_url": domain}


def _print_payload_progress(completed: int, active: int, finished: bool = False) -> None:
    estimated_total = completed + active + 6
    percent = 100 if finished else min(95, int(completed / estimated_total * 100))
    filled = "#" * (percent // 5)
    spaces = " " * (20 - len(filled))
    ending = "\n" if finished else ""
    print(
        f"\r  Kayak payloads: [{filled}{spaces}] {percent}% "
        f"({completed} requests complete, {active} active)",
        end=ending,
        flush=True,
    )


def _scan_known_domains(origin: str, destination: str, round_trip: bool, departure_date: str | None = None, return_date: str | None = None) -> list[dict[str, str]]:
    if os.environ.get("TICKETPY_SKIP_SCAN", "").lower() in {"1", "true", "yes", "on"}:
        return [{"domain": domain, "status": "no info", "details": "Scan disabled in this environment", "price_hint": None} for domain in DEFAULT_SEARCH_DOMAINS]

    results: list[dict[str, str]] = []
    domains = list(DEFAULT_SEARCH_DOMAINS)
    kayak_domains = [domain for domain in domains if "kayak.com" in domain.lower()]
    fallback_domains = [domain for domain in domains if "kayak.com" not in domain.lower()]

    for domain in kayak_domains:
        print(f"  Primary source: {domain}")
        result = _scan_site(domain, origin, destination, round_trip, departure_date, return_date)
        status = result["status"]
        if status == "found":
            print(f"  Result: found info on {domain}")
        elif status == "botblocked":
            print(f"  Result: botblocked on {domain}")
        elif status == "no info":
            print(f"  Result: no useful flight info on {domain}")
        else:
            print(f"  Result: failed to scan {domain}")
        results.append(result)
        if status == "found":
            return results

    for domain in fallback_domains:
        print(f"  Kayak did not return a usable result; trying fallback: {domain}")
        result = _scan_site(domain, origin, destination, round_trip, departure_date, return_date)
        status = result["status"]
        if status == "found":
            print(f"  Result: found info on {domain}")
        elif status == "botblocked":
            print(f"  Result: botblocked on {domain}")
        elif status == "no info":
            print(f"  Result: no useful flight info on {domain}")
        else:
            print(f"  Result: failed to scan {domain}")
        results.append(result)
    return results


def _run_interactive_report(env_values: Mapping[str, str] | None = None) -> None:
    env_values = dict(env_values or {})
    print("\nTicket report setup")
    env_origin = (env_values.get("from") or env_values.get("origin") or "").strip().upper()
    env_destination = (env_values.get("to") or env_values.get("destination") or "").strip().upper()
    env_round_trip = env_values.get("round trip") or env_values.get("round_trip") or env_values.get("round trip? (y/n)") or ""
    env_departure = env_values.get("departure date") or env_values.get("depart date") or env_values.get("departure_date") or env_values.get("depart_date") or ""
    env_return = env_values.get("return date") or env_values.get("return_date") or env_values.get("return flight date") or ""
    env_image = env_values.get("optional screenshot url") or env_values.get("optional screenshot") or env_values.get("image_url") or ""
    if isinstance(env_image, str) and env_image.strip().lower() in {"n", "no", "false", "none", "null"}:
        env_image = ""
    env_report = env_values.get("export report") or env_values.get("export report? (y/n)") or env_values.get("report") or env_values.get("report export") or ""
    env_report_format = env_values.get("report format") or env_values.get("format") or env_values.get("export format") or ""

    origin = (env_origin or _read_input("From airport code (example: LAX): ").strip().upper() or "LAX").strip().upper()
    destination = (env_destination or _read_input("To airport code (example: JFK): ").strip().upper() or "JFK").strip().upper()
    if env_round_trip:
        round_trip = env_round_trip.strip().lower() in {"y", "yes"}
    else:
        round_trip = _prompt_yes_no("Round trip? (y/n): ", default=True)

    if env_image:
        image_url = env_image.strip()
    else:
        image_url = ""

    if not env_image and not env_departure and not env_return:
        next_value = _peek_input()
        if _looks_like_url(next_value):
            image_url = _read_input("Optional screenshot URL to include in the report (not scanned automatically): ").strip()
            departure_date = _normalize_date_input(None, default_offset_days=7)
            return_date = _normalize_date_input(None, default_offset_days=9) if round_trip else None
        else:
            departure_date = _read_departure_date("Departure date (YYYY-MM-DD, MM/DD/YYYY, YYYYMMDD, or MMDDYY): ", default_offset_days=7)
            if round_trip:
                return_date = _read_departure_date("Return date (YYYY-MM-DD, MM/DD/YYYY, YYYYMMDD, or MMDDYY): ", default_offset_days=9)
            else:
                return_date = None

            if not env_image and _looks_like_url(_peek_input()):
                image_url = _read_input("Optional screenshot URL to include in the report (not scanned automatically): ").strip()
    else:
        departure_date = _normalize_date_input(env_departure or None, default_offset_days=7) if env_departure else _normalize_date_input(None, default_offset_days=7)
        return_date = None
        if round_trip:
            return_date = _normalize_date_input(env_return or None, default_offset_days=9) if env_return else _normalize_date_input(None, default_offset_days=9)

    if env_departure and not env_image:
        departure_date = _normalize_date_input(env_departure, default_offset_days=7)
    if env_return and round_trip and not env_image:
        return_date = _normalize_date_input(env_return, default_offset_days=9)

    if env_image:
        image_url = env_image.strip()

    print(f"\nScanning known travel domains for {origin} -> {destination} ({'round trip' if round_trip else 'one way'})")
    print(f"Trip dates: departure={departure_date}, return={return_date or 'N/A'}")
    results = _scan_known_domains(origin, destination, round_trip, departure_date, return_date)
    found = sum(1 for item in results if item["status"] == "found")
    blocked = sum(1 for item in results if item["status"] == "botblocked")
    failed = sum(1 for item in results if item["status"] == "failed")
    no_info = sum(1 for item in results if item["status"] == "no info")
    print(f"\nScan summary: found={found}, botblocked={blocked}, failed={failed}, no_info={no_info}")
    print(f"\nBest result: {summarize_scan_results(origin, destination, round_trip, results)}")

    tickets = build_ticket_rows_from_results(origin, destination, round_trip, results)
    for ticket in tickets:
        ticket["image_url"] = image_url
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if webhook_url:
        _send_discord_notification(
            webhook_url,
            summarize_scan_results(origin, destination, round_trip, results),
            tickets,
        )
        print("Discord notification sent.")

    wants_report = _coerce_bool(env_report, default=False) if env_report else _prompt_yes_no("Would you like to export a report? (y/n): ", default=False)
    if not wants_report:
        print("Report export skipped. Script finished.")
        return

    final_format = "txt"
    if env_report_format:
        final_format = "pdf" if env_report_format.strip().lower() in {"pdf", "rich", "rich pdf", "rich-pdf"} else "txt"
    else:
        export_choice = _read_input("Report format? [pdf/txt]: ").strip().lower()
        final_format = "pdf" if export_choice in {"pdf", "rich", "rich pdf", "rich-pdf"} else "txt"

    output_name = f"{origin}-{destination}{'-roundtrip' if round_trip else '-oneway'}"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    default_output_name = f"{output_name}-{timestamp}.{final_format}"
    output_path_raw = ""
    if not env_report:
        output_path_raw = _read_input(f"Save report as (default: {default_output_name}): ").strip()
    output_path = _resolve_output_path(output_path_raw or default_output_name, default_output_name)

    if output_path.suffix.lower() not in {".txt", ".pdf"}:
        output_path = output_path.with_suffix(f".{final_format}")

    export_report(tickets or [_best_ticket_from_results(origin, destination, round_trip, results)], output_path, format_name=final_format)
    print(f"\nexported to {output_path.resolve()}")
    print(f"Included tickets: {len(tickets)}")
    if tickets:
        print(f"Cheapest source: {min(tickets, key=lambda item: _parse_price_value(item.get('price')) if _parse_price_value(item.get('price')) is not None else float('inf')).get('source')}")


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Export a ticket report to text, rich HTML, or PDF.")
    parser.add_argument("output", nargs="?", help="Path to the output file.")
    parser.add_argument("--format", dest="format_name", choices=["txt", "rich", "pdf"], default="txt", help="Plain text, rich HTML, or PDF export.")
    parser.add_argument("--depart-date", dest="depart_date", help="Departure date as YYYY-MM-DD, MM/DD/YYYY, YYYYMMDD, or MMDDYY.")
    parser.add_argument("--return-date", dest="return_date", help="Return date as YYYY-MM-DD, MM/DD/YYYY, YYYYMMDD, or MMDDYY for round trips.")
    parser.add_argument("--round-trip", dest="round_trip", action="store_true", help="Use a round-trip search when scanning a route.")
    parser.add_argument("--details", nargs="*", metavar="VALUE", help="Pass ticket fields directly as: title price source image_url")
    parser.add_argument("--title", action="append", default=[], help="Ticket title. Repeat for multiple tickets.")
    parser.add_argument("--price", action="append", default=[], help="Ticket price. Repeat for multiple tickets.")
    parser.add_argument("--source", action="append", default=[], help="Source URL. Repeat for multiple tickets.")
    parser.add_argument("--image", action="append", default=[], help="Image URL. Repeat for multiple tickets.")
    parser.add_argument("--env-file", dest="env_file", help="Optional .env-style file with From, To, Round trip? (y/n), and Optional screenshot URL values.")

    args = parser.parse_args()

    env_values = _load_env_file(args.env_file) if args.env_file else _load_env_file()
    if len(sys.argv) == 1 or (len(sys.argv) == 2 and args.env_file is None and not any(arg.startswith("-") for arg in sys.argv[1:])):
        try:
            _run_interactive_report(env_values)
        except ValueError as exc:
            parser.error(str(exc))
        raise SystemExit(0)


    if args.details:
        if len(args.details) < 4:
            parser.error("--details requires 4 values: title price source image_url")
        ticket = {
            "title": args.details[0],
            "price": args.details[1],
            "source": args.details[2],
            "image_url": args.details[3],
        }
        output_path = _resolve_output_path(args.output, "ticket_report.txt")
        export_report([ticket], output_path, args.format_name)
        print(f"exported to {output_path.resolve()}")
        raise SystemExit(0)

    ticket_count = max(len(args.title), len(args.price), len(args.source), len(args.image))
    tickets = []
    for i in range(ticket_count):
        ticket = {
            "title": args.title[i] if i < len(args.title) else f"Ticket {i + 1}",
            "price": args.price[i] if i < len(args.price) else "Price not listed",
            "source": args.source[i] if i < len(args.source) else "",
            "image_url": args.image[i] if i < len(args.image) else "",
        }
        tickets.append(ticket)

    output_path = _resolve_output_path(args.output, "ticket_report.txt")
    export_report(tickets, output_path, args.format_name)
    print(f"exported to {output_path.resolve()}")
