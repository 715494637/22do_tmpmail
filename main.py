#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from curl_cffi import requests


ROOT = Path(__file__).resolve().parent
NODE_SCRIPT = ROOT / "generate_params.js"
BASE_URL = "https://22.do"


def run_node(mode: str, *, payload: dict[str, Any] | None = None, raw: str | None = None) -> dict[str, Any]:
    stdin = raw if raw is not None else json.dumps(payload or {}, ensure_ascii=False)
    result = subprocess.run(
        ["node", str(NODE_SCRIPT), mode],
        input=stdin,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"node helper failed: {mode}")
    return json.loads(result.stdout)


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


def resolve_email(client: MailClient, language: str, explicit_email: str | None) -> tuple[str, dict[str, Any] | None]:
    if explicit_email:
        return explicit_email, None

    payload = run_node("build-random-payload")
    response = client.post_json("/action/mailbox/gmail", payload, referer=f"{BASE_URL}/{language}/fake-gmail-generator")
    data = response.json()
    return data["data"]["email"], data


def fetch_inbox_page(client: MailClient, inbox_url: str, *, referer: str) -> tuple[requests.Response, dict[str, Any]]:
    response = client.get(inbox_url, referer=referer)
    data = run_node("parse-inbox", raw=response.text)
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
        content_data = run_node("parse-content", raw=content_response.text)
        download_payload = run_node("build-download-payload", payload={"view_id": content_data["view_id"]})
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
    warmup_url = f"{BASE_URL}/{args.language}/fake-gmail-generator"
    warmup = client.get(warmup_url)

    email_address, random_response = resolve_email(client, args.language, args.email)
    login_payload = run_node(
        "build-login-payload",
        payload={"email": email_address, "language": args.language},
    )
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
    parser = argparse.ArgumentParser(description="22.do temporary mailbox fetcher")
    parser.add_argument("--email", help="existing mailbox address")
    parser.add_argument("--language", default="zh", help="site language, default: zh")
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
