#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from curl_cffi import requests


ROOT = Path(__file__).resolve().parent
BASE_URL = "https://22.do"

# ---------------------------------------------------------------------------
# Mailbox type → API endpoint / referer / domain pool mapping
# ---------------------------------------------------------------------------

MAILBOX_TYPES = {
    "gmail": {
        "endpoint": "/action/mailbox/gmail",
        "referer_path": "/fake-gmail-generator",
        "domains": ["gmail.com", "googlemail.com"],
        "description": "Gmail / Google Mail",
    },
    "microsoft": {
        "endpoint": "/action/mailbox/microsoft",
        "referer_path": "/temporary-outlook",
        "domains": ["hotmail.com", "outlook.com"],
        "description": "Hotmail / Outlook",
    },
    "domain": {
        "endpoint": "/action/mailbox/domain",
        "referer_path": "/temp-mail-generator",
        "domains": [
            "linshiyou.com", "colabeta.com", "youxiang.dev",
            "colaname.com", "usdtbeta.com", "tnbeta.com", "fft.edu.do",
        ],
        "description": "22.do 自有域名临时邮箱",
    },
    "random": {
        "endpoint": "/action/mailbox/create",
        "referer_path": "/",
        "domains": [],  # server decides
        "description": "随机分配 (gmail/microsoft/domain)",
    },
}

VALID_TYPES = list(MAILBOX_TYPES.keys())


# ---------------------------------------------------------------------------
# HTML / Cloudflare email helpers  (replaces generate_params.js)
# ---------------------------------------------------------------------------

def decode_cf_email(hex_str: str) -> str:
    """Decode a Cloudflare-obfuscated email from its hex representation."""
    if not hex_str or len(hex_str) < 2 or len(hex_str) % 2 != 0:
        return ""
    key = int(hex_str[:2], 16)
    return "".join(
        chr(int(hex_str[i : i + 2], 16) ^ key)
        for i in range(2, len(hex_str), 2)
    )


def decode_protected_emails(fragment: str) -> str:
    """Replace Cloudflare email-protection tags with decoded addresses."""
    fragment = re.sub(
        r'<(?:a|span)\b[^>]*data-cfemail="([0-9a-fA-F]+)"[^>]*>[\s\S]*?</(?:a|span)>',
        lambda m: decode_cf_email(m.group(1)),
        fragment,
        flags=re.IGNORECASE,
    )
    fragment = re.sub(r"<script\b[\s\S]*?</script>", "", fragment, flags=re.IGNORECASE)
    return fragment


def decode_html_entities(text: str) -> str:
    """Decode common HTML entities and numeric character references."""
    text = re.sub(
        r"&#x([0-9a-fA-F]+);",
        lambda m: chr(int(m.group(1), 16)),
        text,
    )
    text = re.sub(
        r"&#([0-9]+);",
        lambda m: chr(int(m.group(1))),
        text,
    )
    text = (
        text
        .replace("&quot;", '"')
        .replace("&#039;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("&nbsp;", " ")
        .replace("&#160;", " ")
    )
    return text


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text)


def normalize_text(text: str) -> str:
    """Decode CF emails, strip tags, decode entities, collapse whitespace."""
    return re.sub(r"\s+", " ", decode_html_entities(strip_tags(decode_protected_emails(text)))).strip()


# ---------------------------------------------------------------------------
# Inbox / content HTML parsers
# ---------------------------------------------------------------------------

_INBOX_ROW_RE = re.compile(
    r'<div class="tr">\s*'
    r'<div class="item subject"[^>]*viewEml\(\'([^\']+)\'\)[^>]*>([\s\S]*?)</div>\s*'
    r'<div class="item from">([\s\S]*?)</div>\s*'
    r'<div class="item time receive-time" data-bs-time="(\d+)">([\s\S]*?)</div>',
    re.IGNORECASE,
)

_INBOX_EMAIL_RE = re.compile(
    r'<p class="mb-0 text text-email">([\s\S]*?)</p>',
    re.IGNORECASE,
)


def parse_inbox_html(raw_html: str) -> dict[str, Any]:
    email_match = _INBOX_EMAIL_RE.search(raw_html)
    email = normalize_text(email_match.group(1)) if email_match else ""

    messages: list[dict[str, Any]] = []
    for m in _INBOX_ROW_RE.finditer(raw_html):
        messages.append(
            {
                "message_id": m.group(1),
                "subject": normalize_text(m.group(2)),
                "from": normalize_text(m.group(3)),
                "timestamp": int(m.group(4)),
                "time_text": normalize_text(m.group(5)),
                "content_path": f"/zh/content/{m.group(1)}",
            }
        )

    return {
        "email": email,
        "messages_count": len(messages),
        "messages": messages,
    }


def parse_content_html(raw_html: str) -> dict[str, Any]:
    message_id_match = re.search(
        r"https://22\.do/(?:[a-z]{2}/)?content/([0-9a-f]{32})", raw_html, re.IGNORECASE
    )
    value_matches = list(
        re.finditer(
            r'<div class="item text">\s*<span class="label">[\s\S]*?</span>\s*'
            r'<span class="con[^"]*"[^>]*>([\s\S]*?)</span>',
            raw_html,
            re.IGNORECASE,
        )
    )
    view_url_match = re.search(r"https://22\.do/view/[A-Za-z0-9+/_=-]+", raw_html, re.IGNORECASE)
    view_id_match = re.search(r"viewId:\s*'([^']+)'", raw_html, re.IGNORECASE)

    return {
        "message_id": message_id_match.group(1) if message_id_match else "",
        "subject": normalize_text(value_matches[0].group(1)) if len(value_matches) > 0 else "",
        "from": normalize_text(value_matches[1].group(1)) if len(value_matches) > 1 else "",
        "received_at": normalize_text(value_matches[2].group(1)) if len(value_matches) > 2 else "",
        "view_url": view_url_match.group(0) if view_url_match else "",
        "view_id": view_id_match.group(1) if view_id_match else "",
    }


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------

def build_random_payload() -> dict[str, Any]:
    return {"type": "random"}


def build_login_payload(email: str, language: str = "zh") -> dict[str, Any]:
    return {"email": email.strip(), "language": language.strip() or "zh"}


def build_download_payload(view_id: str) -> dict[str, Any]:
    return {"viewId": view_id.strip()}


# ---------------------------------------------------------------------------
# EML parsing & helpers
# ---------------------------------------------------------------------------

def sanitize_preview(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def parse_eml(raw_bytes: bytes, preview_limit: int) -> dict[str, Any]:
    message = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    headers = {}
    for key in ("Subject", "From", "To", "Date", "Return-Path", "Message-ID"):
        value = message.get(key)
        if value:
            headers[key.lower()] = str(value)

    preview = ""
    try:
        if message.is_multipart():
            body = message.get_body(preferencelist=("plain", "html"))
            if body is not None:
                preview = body.get_content()
            if not preview.strip():
                for part in message.walk():
                    if part.get_content_maintype() == "text":
                        candidate = part.get_content()
                        if candidate.strip():
                            preview = candidate
                            break
        else:
            preview = message.get_content()
    except Exception:
        preview = raw_bytes.decode("utf-8", errors="ignore")

    return {
        "headers": headers,
        "preview": sanitize_preview(preview, preview_limit),
    }


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class MailClient:
    def __init__(self, impersonate: str, timeout: int) -> None:
        self.session = requests.Session(impersonate=impersonate)
        self.timeout = timeout

    def get(self, url: str, *, referer: str | None = None):
        headers = {}
        if referer:
            headers["referer"] = referer
        return self.session.get(url, headers=headers, timeout=self.timeout)

    def post_json(self, path: str, payload: dict[str, Any], *, referer: str):
        return self.session.post(
            f"{BASE_URL}{path}",
            json=payload,
            headers={"origin": BASE_URL, "referer": referer},
            timeout=self.timeout,
        )


# ---------------------------------------------------------------------------
# Business logic
# ---------------------------------------------------------------------------

def resolve_email(
    client: MailClient,
    language: str,
    explicit_email: str | None,
    mailbox_type: str = "gmail",
) -> tuple[str, dict[str, Any] | None]:
    """Create or reuse a mailbox.

    *mailbox_type* selects which API endpoint to hit when generating a new
    random address.  Accepted values: gmail, microsoft, domain, random.
    When *explicit_email* is provided the type is ignored (we already have
    an address).
    """
    if explicit_email:
        return explicit_email, None

    type_cfg = MAILBOX_TYPES.get(mailbox_type)
    if type_cfg is None:
        raise ValueError(
            f"Unknown mailbox type '{mailbox_type}'. "
            f"Choose from: {', '.join(VALID_TYPES)}"
        )

    payload = build_random_payload()
    referer = f"{BASE_URL}/{language}{type_cfg['referer_path']}"
    response = client.post_json(type_cfg["endpoint"], payload, referer=referer)
    data = response.json()
    if not data.get("status"):
        raise RuntimeError(f"Mailbox creation failed: {data}")
    return data["data"]["email"], data


def fetch_inbox_page(client: MailClient, inbox_url: str, *, referer: str) -> tuple[requests.Response, dict[str, Any]]:
    response = client.get(inbox_url, referer=referer)
    data = parse_inbox_html(response.text)
    return response, data


def fetch_message_details(
    client: MailClient,
    *,
    language: str,
    inbox_url: str,
    preview_chars: int,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    messages = []
    for item in items:
        content_url = f"{BASE_URL}/{language}/content/{item['message_id']}"
        content_response = client.get(content_url, referer=inbox_url)
        content_data = parse_content_html(content_response.text)
        download_payload = build_download_payload(content_data["view_id"])
        download_response = client.post_json("/action/mailbox/download", download_payload, referer=content_url)
        eml_data = parse_eml(download_response.content, preview_chars)

        messages.append(
            {
                "inbox_entry": item,
                "content_page": {
                    "status_code": content_response.status_code,
                    "url": content_url,
                    "parsed": content_data,
                },
                "download": {
                    "status_code": download_response.status_code,
                    "content_type": download_response.headers.get("content-type"),
                    "size": len(download_response.content),
                    "eml": eml_data,
                },
            }
        )
    return messages


def normalized(value: str | None) -> str:
    return value.casefold().strip() if value else ""


def message_matches(item: dict[str, Any], *, match_subject: str | None, match_from: str | None) -> bool:
    subject_filter = normalized(match_subject)
    sender_filter = normalized(match_from)
    subject = normalized(item.get("subject", ""))
    sender = normalized(item.get("from", ""))

    if subject_filter and subject_filter not in subject:
        return False
    if sender_filter and sender_filter not in sender:
        return False
    return True


def select_polled_messages(
    messages: list[dict[str, Any]],
    *,
    limit: int,
    baseline_timestamp: int,
    seen_ids: set[str],
    match_subject: str | None,
    match_from: str | None,
) -> list[dict[str, Any]]:
    matches = []
    for item in messages:
        message_id = item.get("message_id", "")
        timestamp = int(item.get("timestamp", 0))
        if message_id in seen_ids:
            continue
        if timestamp < baseline_timestamp:
            continue
        if not message_matches(item, match_subject=match_subject, match_from=match_from):
            continue
        matches.append(item)
        if len(matches) >= limit:
            break
    return matches


def poll_inbox_until_match(
    client: MailClient,
    *,
    inbox_url: str,
    initial_response: requests.Response,
    initial_data: dict[str, Any],
    poll_interval: float,
    wait_timeout: int,
    limit: int,
    match_subject: str | None,
    match_from: str | None,
    since_timestamp: int | None,
) -> tuple[requests.Response, dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    if limit < 1:
        raise RuntimeError("--limit must be at least 1 when --wait-mail is set")

    baseline_timestamp = since_timestamp
    if baseline_timestamp is None:
        baseline_timestamp = max((int(item.get("timestamp", 0)) for item in initial_data.get("messages", [])), default=0)
        seen_ids = {str(item.get("message_id", "")) for item in initial_data.get("messages", [])}
        since_mode = "after-initial-snapshot"
    else:
        seen_ids = set()
        since_mode = "explicit-timestamp"

    attempts = 1
    deadline = time.time() + wait_timeout
    current_response = initial_response
    current_data = initial_data

    while True:
        matched_items = select_polled_messages(
            current_data.get("messages", []),
            limit=limit,
            baseline_timestamp=baseline_timestamp,
            seen_ids=seen_ids,
            match_subject=match_subject,
            match_from=match_from,
        )
        if matched_items:
            return current_response, current_data, matched_items, {
                "enabled": True,
                "matched": True,
                "attempts": attempts,
                "interval_seconds": poll_interval,
                "timeout_seconds": wait_timeout,
                "baseline_timestamp": baseline_timestamp,
                "since_mode": since_mode,
                "filters": {
                    "subject": match_subject,
                    "from": match_from,
                },
                "matched_message_ids": [item["message_id"] for item in matched_items],
            }

        if time.time() >= deadline:
            break

        time.sleep(poll_interval)
        attempts += 1
        current_response, current_data = fetch_inbox_page(client, inbox_url, referer=inbox_url)

    last_seen_timestamp = max((int(item.get("timestamp", 0)) for item in current_data.get("messages", [])), default=0)
    return current_response, current_data, [], {
        "enabled": True,
        "matched": False,
        "attempts": attempts,
        "interval_seconds": poll_interval,
        "timeout_seconds": wait_timeout,
        "baseline_timestamp": baseline_timestamp,
        "since_mode": since_mode,
        "filters": {
            "subject": match_subject,
            "from": match_from,
        },
        "last_seen_timestamp": last_seen_timestamp,
    }


def fetch_mailbox(args: argparse.Namespace) -> dict[str, Any]:
    client = MailClient(impersonate=args.impersonate, timeout=args.timeout)

    # Warmup: visit the page that matches the requested mailbox type
    type_cfg = MAILBOX_TYPES.get(args.type, MAILBOX_TYPES["gmail"])
    warmup_url = f"{BASE_URL}/{args.language}{type_cfg['referer_path']}"
    warmup = client.get(warmup_url)

    email_address, random_response = resolve_email(
        client, args.language, args.email, mailbox_type=args.type,
    )
    login_payload = build_login_payload(email_address, args.language)
    login_response = client.post_json("/action/mailbox/login", login_payload, referer=warmup_url)
    login_data = login_response.json()
    inbox_url = login_data["redirect"]

    inbox_response, inbox_data = fetch_inbox_page(client, inbox_url, referer=warmup_url)
    selected_items = list(inbox_data.get("messages", []))[: args.limit]
    polling: dict[str, Any] | None = None

    if args.wait_mail:
        inbox_response, inbox_data, selected_items, polling = poll_inbox_until_match(
            client,
            inbox_url=inbox_url,
            initial_response=inbox_response,
            initial_data=inbox_data,
            poll_interval=args.poll_interval,
            wait_timeout=args.wait_timeout,
            limit=args.limit,
            match_subject=args.match_subject,
            match_from=args.match_from,
            since_timestamp=args.since_timestamp,
        )

    messages = fetch_message_details(
        client,
        language=args.language,
        inbox_url=inbox_url,
        preview_chars=args.preview_chars,
        items=selected_items,
    )

    result: dict[str, Any] = {
        "warmup": {
            "status_code": warmup.status_code,
            "url": warmup.url,
        },
        "random": random_response,
        "login": login_data,
        "inbox": {
            "status_code": inbox_response.status_code,
            "url": inbox_response.url,
            "parsed": inbox_data,
        },
        "messages": messages,
    }
    if polling is not None:
        result["polling"] = polling

    return result


def build_parser() -> argparse.ArgumentParser:
    type_help_lines = [f"  {k}: {v['description']}" for k, v in MAILBOX_TYPES.items()]
    type_help = "mailbox type:\n" + "\n".join(type_help_lines)

    parser = argparse.ArgumentParser(
        description="22.do temporary mailbox fetcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Supported mailbox types (--type):\n{type_help}",
    )
    parser.add_argument("--email", help="existing mailbox address (skip creation)")
    parser.add_argument(
        "--type", choices=VALID_TYPES, default="gmail",
        help="mailbox type to generate (default: gmail). "
             "Ignored when --email is provided.",
    )
    parser.add_argument("--language", default="zh", help="site language (default: zh)")
    parser.add_argument("--limit", type=int, default=3, help="how many messages to fetch, default: 3")
    parser.add_argument("--preview-chars", type=int, default=800, help="message preview length, default: 800")
    parser.add_argument("--impersonate", default="chrome136", help="curl_cffi browser fingerprint, default: chrome136")
    parser.add_argument("--timeout", type=int, default=30, help="request timeout seconds, default: 30")
    parser.add_argument("--wait-mail", action="store_true", help="poll inbox HTML until a matching new email appears")
    parser.add_argument("--poll-interval", type=float, default=5.0, help="seconds between inbox polls, default: 5")
    parser.add_argument("--wait-timeout", type=int, default=180, help="max seconds to wait when --wait-mail is set, default: 180")
    parser.add_argument(
        "--since-timestamp",
        type=int,
        help="only consider emails with timestamp >= this unix timestamp; default is to wait for mail newer than the initial inbox snapshot",
    )
    parser.add_argument("--match-subject", help="case-insensitive substring filter on the inbox subject")
    parser.add_argument("--match-from", help="case-insensitive substring filter on the inbox sender")
    return parser


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = fetch_mailbox(args)
    except Exception as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
