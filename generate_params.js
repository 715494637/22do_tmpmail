#!/usr/bin/env node
"use strict";

const fs = require("fs");

function readStdin() {
  try {
    return fs.readFileSync(0, "utf8");
  } catch {
    return "";
  }
}

function fail(message) {
  console.error(message);
  process.exit(1);
}

function emit(value) {
  process.stdout.write(`${JSON.stringify(value, null, 2)}\n`);
}

function parseJson(raw) {
  if (!raw.trim()) {
    return {};
  }
  try {
    return JSON.parse(raw);
  } catch (error) {
    fail(`invalid json input: ${error.message}`);
  }
}

function decodeCfEmail(hex) {
  if (!hex || hex.length < 2 || hex.length % 2 !== 0) {
    return "";
  }
  const key = parseInt(hex.slice(0, 2), 16);
  let output = "";
  for (let index = 2; index < hex.length; index += 2) {
    const value = parseInt(hex.slice(index, index + 2), 16);
    output += String.fromCharCode(value ^ key);
  }
  return output;
}

function decodeProtectedEmails(fragment) {
  return fragment
    .replace(
      /<(?:a|span)\b[^>]*data-cfemail="([0-9a-fA-F]+)"[^>]*>[\s\S]*?<\/(?:a|span)>/gi,
      (_, hex) => decodeCfEmail(hex),
    )
    .replace(/<script\b[\s\S]*?<\/script>/gi, "");
}

function decodeHtmlEntities(text) {
  return text
    .replace(/&#x([0-9a-fA-F]+);/g, (_, hex) => String.fromCodePoint(parseInt(hex, 16)))
    .replace(/&#([0-9]+);/g, (_, dec) => String.fromCodePoint(parseInt(dec, 10)))
    .replace(/&quot;/g, '"')
    .replace(/&#039;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&")
    .replace(/&nbsp;|&#160;/g, " ");
}

function stripTags(text) {
  return text.replace(/<[^>]+>/g, " ");
}

function normalizeText(text) {
  return decodeHtmlEntities(stripTags(decodeProtectedEmails(text)))
    .replace(/\s+/g, " ")
    .trim();
}

function requireEmail(value) {
  if (typeof value !== "string" || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value)) {
    fail("email is required");
  }
  return value.trim();
}

function requireString(value, name) {
  if (typeof value !== "string" || !value.trim()) {
    fail(`${name} is required`);
  }
  return value.trim();
}

function requireNonNegativeInteger(value, name) {
  const numeric =
    typeof value === "number" && Number.isFinite(value)
      ? Math.trunc(value)
      : Number.parseInt(String(value), 10);
  if (!Number.isFinite(numeric) || numeric < 0) {
    fail(`${name} must be a non-negative integer`);
  }
  return numeric;
}

function parseInboxHtml(html) {
  const emailMatch = html.match(/<p class="mb-0 text text-email">([\s\S]*?)<\/p>/i);
  const email = emailMatch ? normalizeText(emailMatch[1]) : "";
  const messages = [];
  const rowRegex =
    /<div class="tr">\s*<div class="item subject"[^>]*viewEml\('([^']+)'\)[^>]*>([\s\S]*?)<\/div>\s*<div class="item from">([\s\S]*?)<\/div>\s*<div class="item time receive-time" data-bs-time="(\d+)">([\s\S]*?)<\/div>/gi;

  for (const match of html.matchAll(rowRegex)) {
    messages.push({
      message_id: match[1],
      subject: normalizeText(match[2]),
      from: normalizeText(match[3]),
      timestamp: Number.parseInt(match[4], 10),
      time_text: normalizeText(match[5]),
      content_path: `/zh/content/${match[1]}`,
    });
  }

  return {
    email,
    messages_count: messages.length,
    messages,
  };
}

function parseContentHtml(html) {
  const messageIdMatch = html.match(/https:\/\/22\.do\/(?:[a-z]{2}\/)?content\/([0-9a-f]{32})/i);
  const valueMatches = [
    ...html.matchAll(
      /<div class="item text">\s*<span class="label">[\s\S]*?<\/span>\s*<span class="con[^"]*"[^>]*>([\s\S]*?)<\/span>/gi,
    ),
  ];
  const viewUrlMatch = html.match(/https:\/\/22\.do\/view\/[A-Za-z0-9+/_=-]+/i);
  const viewIdMatch = html.match(/viewId:\s*'([^']+)'/i);

  return {
    message_id: messageIdMatch ? messageIdMatch[1] : "",
    subject: valueMatches[0] ? normalizeText(valueMatches[0][1]) : "",
    from: valueMatches[1] ? normalizeText(valueMatches[1][1]) : "",
    received_at: valueMatches[2] ? normalizeText(valueMatches[2][1]) : "",
    view_url: viewUrlMatch ? viewUrlMatch[0] : "",
    view_id: viewIdMatch ? viewIdMatch[1] : "",
  };
}

function buildRandomPayload() {
  return { type: "random" };
}

function buildLoginPayload(input) {
  return {
    email: requireEmail(input.email),
    language: typeof input.language === "string" && input.language.trim() ? input.language.trim() : "zh",
  };
}

function buildDownloadPayload(input) {
  return { viewId: requireString(input.view_id, "view_id") };
}

function buildApplyTokenPayload(input) {
  return {
    uuid: requireString(input.uuid, "uuid"),
    cfToken: requireString(input.cfToken, "cfToken"),
  };
}

function buildMessagePayload(input) {
  return {
    email: requireEmail(input.email),
    lastime: requireNonNegativeInteger(input.lastime, "lastime"),
  };
}

function main() {
  const mode = process.argv[2];
  const raw = readStdin();

  switch (mode) {
    case "build-random-payload":
      emit(buildRandomPayload());
      return;
    case "build-login-payload":
      emit(buildLoginPayload(parseJson(raw)));
      return;
    case "build-download-payload":
      emit(buildDownloadPayload(parseJson(raw)));
      return;
    case "build-apply-token-payload":
      emit(buildApplyTokenPayload(parseJson(raw)));
      return;
    case "build-message-payload":
      emit(buildMessagePayload(parseJson(raw)));
      return;
    case "decode-cfemail":
      emit({ email: decodeCfEmail(parseJson(raw).hex || "") });
      return;
    case "parse-inbox":
      emit(parseInboxHtml(raw));
      return;
    case "parse-content":
      emit(parseContentHtml(raw));
      return;
    default:
      fail(
        "usage: node generate_params.js <build-random-payload|build-login-payload|build-download-payload|build-apply-token-payload|build-message-payload|decode-cfemail|parse-inbox|parse-content>",
      );
  }
}

main();
