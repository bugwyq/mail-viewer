#!/usr/bin/env python3
import datetime as dt
import email
import html
import imaplib
import json
import os
import re
import socket
import sys
import threading
import traceback
import uuid
from email import policy
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse


EMAIL_RE = re.compile(r"^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}$", re.I)
STORE_LOCK = threading.RLock()


class HtmlToText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        text = data.strip()
        if text:
            self.parts.append(text)

    def get_text(self):
        return "\n".join(self.parts)


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    bind = os.environ.get("APP_BIND", "0.0.0.0")
    port = env_int("APP_PORT", 8787)
    public_base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    viewer_token = os.environ.get("VIEWER_TOKEN", "")
    admin_token = os.environ.get("ADMIN_TOKEN", "")
    allow_no_token = env_bool("ALLOW_NO_TOKEN", False)
    config_path = os.environ.get("CONFIG_PATH", "/data/config.json")

    default_imap_host = os.environ.get("DEFAULT_IMAP_HOST", "imap.gmail.com")
    default_imap_port = env_int("DEFAULT_IMAP_PORT", 993)
    default_imap_mailbox = os.environ.get("DEFAULT_IMAP_MAILBOX", "INBOX")
    default_recent_days = env_int("RECENT_DAYS", 30)
    max_results = env_int("MAX_RESULTS", 20)
    default_fetch_limit = env_int("FETCH_LIMIT", 80)
    default_timeout_seconds = env_int("IMAP_TIMEOUT_SECONDS", 25)
    default_enable_gmail_raw = env_bool("ENABLE_GMAIL_RAW", True)
    default_strip_password_spaces = env_bool("STRIP_PASSWORD_SPACES", True)
    default_strict_local_filter = env_bool("STRICT_LOCAL_FILTER", False)

    # Optional legacy seed. If CONFIG_PATH does not exist, these create the first account.
    legacy_imap_host = os.environ.get("IMAP_HOST", default_imap_host)
    legacy_imap_port = env_int("IMAP_PORT", default_imap_port)
    legacy_imap_user = os.environ.get("IMAP_USER", "")
    legacy_imap_password = os.environ.get("IMAP_PASSWORD", "")
    legacy_imap_mailbox = os.environ.get("IMAP_MAILBOX", default_imap_mailbox)

    @classmethod
    def admin_secret(cls):
        return cls.admin_token or cls.viewer_token

    @classmethod
    def validate(cls):
        missing = []
        if not cls.viewer_token and not cls.allow_no_token:
            missing.append("VIEWER_TOKEN")
        if not cls.admin_secret() and not cls.allow_no_token:
            missing.append("ADMIN_TOKEN")
        return missing


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def first_value(form, name, default=""):
    value = form.get(name, [default])[0]
    return str(value).strip()


def int_value(value, default, minimum=0):
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed)


def bool_value(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def form_bool(form, name, default=False):
    if name not in form:
        return default
    return bool_value(form.get(name, [""])[0], default)


def clean_password(password, host, strip_spaces):
    if strip_spaces and "gmail" in str(host).lower():
        return "".join(str(password).split())
    return str(password)


def empty_store():
    return {"version": 1, "accounts": [], "updated_at": now_iso()}


def normalize_account(raw):
    raw = raw or {}
    host = str(raw.get("host") or raw.get("imap_host") or Config.default_imap_host).strip()
    strip_spaces = bool_value(raw.get("strip_password_spaces"), Config.default_strip_password_spaces)
    password = clean_password(raw.get("password") or raw.get("imap_password") or "", host, strip_spaces)
    user = str(raw.get("user") or raw.get("imap_user") or "").strip()
    name = str(raw.get("name") or user or "Mailbox").strip()
    return {
        "id": str(raw.get("id") or uuid.uuid4().hex),
        "name": name,
        "host": host,
        "port": int_value(raw.get("port") or raw.get("imap_port"), Config.default_imap_port, 1),
        "user": user,
        "password": password,
        "mailbox": str(raw.get("mailbox") or raw.get("imap_mailbox") or Config.default_imap_mailbox).strip() or "INBOX",
        "recent_days": int_value(raw.get("recent_days"), Config.default_recent_days, 1),
        "fetch_limit": int_value(raw.get("fetch_limit"), Config.default_fetch_limit, 1),
        "timeout_seconds": int_value(raw.get("timeout_seconds"), Config.default_timeout_seconds, 1),
        "enable_gmail_raw": bool_value(raw.get("enable_gmail_raw"), Config.default_enable_gmail_raw),
        "strip_password_spaces": strip_spaces,
        "strict_local_filter": bool_value(raw.get("strict_local_filter"), Config.default_strict_local_filter),
        "enabled": bool_value(raw.get("enabled"), True),
        "created_at": str(raw.get("created_at") or now_iso()),
        "updated_at": str(raw.get("updated_at") or now_iso()),
    }


def account_from_form(form):
    host = first_value(form, "host", Config.default_imap_host)
    strip_spaces = form_bool(form, "strip_password_spaces", Config.default_strip_password_spaces)
    user = first_value(form, "user")
    name = first_value(form, "name", user or "Mailbox")
    return normalize_account(
        {
            "name": name,
            "host": host,
            "port": first_value(form, "port", str(Config.default_imap_port)),
            "user": user,
            "password": clean_password(first_value(form, "password"), host, strip_spaces),
            "mailbox": first_value(form, "mailbox", Config.default_imap_mailbox),
            "recent_days": first_value(form, "recent_days", str(Config.default_recent_days)),
            "fetch_limit": first_value(form, "fetch_limit", str(Config.default_fetch_limit)),
            "timeout_seconds": first_value(form, "timeout_seconds", str(Config.default_timeout_seconds)),
            "enable_gmail_raw": form_bool(form, "enable_gmail_raw", False),
            "strip_password_spaces": strip_spaces,
            "strict_local_filter": form_bool(form, "strict_local_filter", False),
            "enabled": form_bool(form, "enabled", True),
        }
    )


def validate_account(account):
    errors = []
    if not account["host"]:
        errors.append("IMAP host is required.")
    if not account["user"]:
        errors.append("Mailbox user is required.")
    if not account["password"]:
        errors.append("Mailbox password or app password is required.")
    if not account["mailbox"]:
        errors.append("IMAP mailbox is required.")
    return errors


def seed_store_from_env():
    store = empty_store()
    if Config.legacy_imap_user and Config.legacy_imap_password:
        store["accounts"].append(
            normalize_account(
                {
                    "name": Config.legacy_imap_user,
                    "host": Config.legacy_imap_host,
                    "port": Config.legacy_imap_port,
                    "user": Config.legacy_imap_user,
                    "password": Config.legacy_imap_password,
                    "mailbox": Config.legacy_imap_mailbox,
                    "recent_days": Config.default_recent_days,
                    "fetch_limit": Config.default_fetch_limit,
                    "timeout_seconds": Config.default_timeout_seconds,
                    "enable_gmail_raw": Config.default_enable_gmail_raw,
                    "strip_password_spaces": Config.default_strip_password_spaces,
                    "strict_local_filter": Config.default_strict_local_filter,
                    "enabled": True,
                }
            )
        )
    return store


def load_store():
    with STORE_LOCK:
        path = Path(Config.config_path)
        if not path.exists():
            store = seed_store_from_env()
            if store["accounts"]:
                save_store(store)
            return store

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return empty_store()

        accounts = [normalize_account(account) for account in data.get("accounts", [])]
        return {
            "version": int_value(data.get("version"), 1, 1),
            "accounts": accounts,
            "updated_at": str(data.get("updated_at") or now_iso()),
        }


def save_store(store):
    with STORE_LOCK:
        path = Path(Config.config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        store["version"] = 1
        store["updated_at"] = now_iso()
        payload = json.dumps(store, ensure_ascii=False, indent=2)
        tmp_path = path.with_name(f"{path.name}.tmp")
        tmp_path.write_text(payload + "\n", encoding="utf-8")
        tmp_path.replace(path)


def all_accounts():
    return load_store().get("accounts", [])


def enabled_accounts():
    return [account for account in all_accounts() if account.get("enabled", True)]


def account_label(account):
    name = account.get("name") or "Mailbox"
    user = account.get("user") or ""
    return f"{name} <{user}>" if user and user != name else name


def public_account(account):
    return {
        "id": account.get("id"),
        "name": account.get("name"),
        "host": account.get("host"),
        "port": account.get("port"),
        "user": account.get("user"),
        "mailbox": account.get("mailbox"),
        "enabled": account.get("enabled", True),
        "recent_days": account.get("recent_days"),
        "fetch_limit": account.get("fetch_limit"),
        "enable_gmail_raw": account.get("enable_gmail_raw"),
    }


def imap_quote(value):
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def flatten_search_response(data):
    ids = []
    for item in data or []:
        if isinstance(item, bytes):
            ids.extend(part for part in item.split() if part)
        elif isinstance(item, tuple):
            ids.extend(flatten_search_response(item))
    return ids


def unique_latest(ids):
    seen = set()
    result = []
    for uid in reversed(ids):
        if uid in seen:
            continue
        seen.add(uid)
        result.append(uid)
    return result


def connect_imap(account):
    socket.setdefaulttimeout(int(account["timeout_seconds"]))
    conn = imaplib.IMAP4_SSL(account["host"], int(account["port"]), timeout=int(account["timeout_seconds"]))
    conn.login(account["user"], account["password"])
    status, _ = conn.select(account["mailbox"], readonly=True)
    if status != "OK":
        raise RuntimeError(f"Cannot select mailbox: {account['mailbox']}")
    return conn


def search_uids(conn, address, account):
    since = (dt.date.today() - dt.timedelta(days=int(account["recent_days"]))).strftime("%d-%b-%Y")
    collected = []

    if account.get("enable_gmail_raw") and "gmail" in account.get("host", "").lower():
        raw_query = f"to:{address} OR deliveredto:{address} OR {address}"
        status, data = conn.uid("SEARCH", None, "X-GM-RAW", imap_quote(raw_query))
        if status == "OK":
            collected.extend(flatten_search_response(data))

    searches = [
        ("SINCE", since, "TO", imap_quote(address)),
        ("SINCE", since, "CC", imap_quote(address)),
        ("SINCE", since, "HEADER", "Delivered-To", imap_quote(address)),
        ("SINCE", since, "HEADER", "X-Original-To", imap_quote(address)),
        ("SINCE", since, "TEXT", imap_quote(address)),
    ]
    for criteria in searches:
        status, data = conn.uid("SEARCH", None, *criteria)
        if status == "OK":
            collected.extend(flatten_search_response(data))

    return unique_latest(collected)[: int(account["fetch_limit"])]


def decode_addr_list(value):
    if not value:
        return ""
    return str(value)


def html_to_text(content):
    parser = HtmlToText()
    parser.feed(content or "")
    return parser.get_text()


def extract_body(message):
    plain_parts = []
    html_parts = []

    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue
            disposition = (part.get_content_disposition() or "").lower()
            if disposition == "attachment":
                continue
            content_type = part.get_content_type()
            try:
                content = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                content = payload.decode(charset, errors="replace")
            if content_type == "text/plain":
                plain_parts.append(str(content))
            elif content_type == "text/html":
                html_parts.append(html_to_text(str(content)))
    else:
        try:
            content = message.get_content()
        except Exception:
            payload = message.get_payload(decode=True) or b""
            charset = message.get_content_charset() or "utf-8"
            content = payload.decode(charset, errors="replace")
        if message.get_content_type() == "text/html":
            html_parts.append(html_to_text(str(content)))
        else:
            plain_parts.append(str(content))

    text = "\n\n".join(part.strip() for part in plain_parts if part.strip())
    if not text:
        text = "\n\n".join(part.strip() for part in html_parts if part.strip())
    return text[:20000]


def message_matches(message, body, address):
    needle = address.lower()
    header_names = [
        "to",
        "cc",
        "bcc",
        "delivered-to",
        "x-original-to",
        "resent-to",
        "x-forwarded-to",
    ]
    haystack = "\n".join(str(message.get(name, "")) for name in header_names).lower()
    if needle in haystack:
        return True
    return needle in (body or "").lower()


def parse_message(raw_bytes, address, account):
    message = email.message_from_bytes(raw_bytes, policy=policy.default)
    body = extract_body(message)
    if account.get("strict_local_filter") and not message_matches(message, body, address):
        return None
    return {
        "subject": str(message.get("subject", "(no subject)")),
        "from": decode_addr_list(message.get("from", "")),
        "to": decode_addr_list(message.get("to", "")),
        "date": str(message.get("date", "")),
        "delivered_to": str(message.get("delivered-to", "")),
        "body": body,
        "account_id": account.get("id"),
        "account_name": account_label(account),
        "account_user": account.get("user", ""),
    }


def fetch_messages_from_account(address, account, limit):
    conn = None
    messages = []
    try:
        conn = connect_imap(account)
        for uid in search_uids(conn, address, account):
            status, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
            if status != "OK":
                continue
            for item in data or []:
                if not isinstance(item, tuple) or not item[1]:
                    continue
                parsed = parse_message(item[1], address, account)
                if parsed:
                    parsed["uid"] = uid.decode("ascii", errors="replace")
                    messages.append(parsed)
                    break
            if len(messages) >= limit:
                break
        return messages
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            try:
                conn.logout()
            except Exception:
                pass


def fetch_messages(address):
    accounts = enabled_accounts()
    messages = []
    errors = []
    for account in accounts:
        if len(messages) >= Config.max_results:
            break
        remaining = Config.max_results - len(messages)
        try:
            messages.extend(fetch_messages_from_account(address, account, remaining))
        except Exception as exc:
            errors.append({"account": account_label(account), "error": str(exc)})
    return messages[: Config.max_results], errors, accounts


def test_account_connection(account):
    conn = None
    try:
        conn = connect_imap(account)
        return True, f"{account_label(account)} connected successfully."
    except Exception as exc:
        return False, f"{account_label(account)} failed: {exc}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            try:
                conn.logout()
            except Exception:
                pass


def checked(value):
    return " checked" if value else ""


def template_key(token):
    if Config.allow_no_token:
        return ""
    if token and token == Config.viewer_token:
        return token
    return "YOUR_VIEWER_TOKEN"


def service_base_url():
    return Config.public_base_url or "http://YOUR_SERVER_IP:8787"


def render_nav(active, token):
    key = quote(token) if token else ""
    search_href = f"/?key={key}" if key else "/"
    admin_href = f"/admin?key={key}" if key else "/admin"
    search_class = "active" if active == "search" else ""
    admin_class = "active" if active == "admin" else ""
    return f"""
    <nav class="nav-pills">
      <a class="{search_class}" href="{search_href}">邮件搜索</a>
      <a class="{admin_class}" href="{admin_href}">邮箱配置</a>
    </nav>
    """


def page(title, body, status=HTTPStatus.OK):
    return status, "text/html; charset=utf-8", f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #111827;
      --soft-ink: #344054;
      --muted: #667085;
      --card: rgba(255, 255, 255, .76);
      --card-strong: rgba(255, 255, 255, .92);
      --line: rgba(17, 24, 39, .10);
      --accent: #111827;
      --accent-2: #2563eb;
      --accent-ink: #ffffff;
      --warn: #b45309;
      --danger: #b42318;
      --ok: #067647;
      --field: rgba(255, 255, 255, .72);
      --shadow: 0 24px 80px rgba(31, 41, 55, .12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: ui-sans-serif, "HarmonyOS Sans SC", "Microsoft YaHei", system-ui, sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 9% 4%, rgba(56, 189, 248, .42), transparent 28rem),
        radial-gradient(circle at 88% 10%, rgba(244, 114, 182, .35), transparent 30rem),
        linear-gradient(135deg, #eef9ff 0%, #fff7fb 48%, #f8fbff 100%);
    }}
    main {{
      width: min(1080px, calc(100vw - 34px));
      margin: 34px auto;
      padding: clamp(20px, 4vw, 34px);
      border: 1px solid rgba(255, 255, 255, .72);
      border-radius: 34px;
      background: rgba(255, 255, 255, .42);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
    }}
    .nav-pills {{
      display: inline-flex;
      gap: 8px;
      padding: 8px;
      margin-bottom: 28px;
      border-radius: 999px;
      background: rgba(255, 255, 255, .68);
      box-shadow: 0 10px 30px rgba(17, 24, 39, .08);
    }}
    .nav-pills a {{
      color: var(--soft-ink);
      text-decoration: none;
      font-weight: 800;
      padding: 11px 18px;
      border-radius: 999px;
    }}
    .nav-pills a.active {{
      color: var(--accent-ink);
      background: var(--accent);
      box-shadow: 0 10px 22px rgba(17, 24, 39, .20);
    }}
    .hero {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 18px;
      align-items: end;
      margin-bottom: 20px;
    }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      width: max-content;
      max-width: 100%;
      margin: 0 0 12px;
      padding: 8px 14px;
      border-radius: 999px;
      background: rgba(255, 255, 255, .68);
      color: var(--soft-ink);
      font-size: 14px;
      font-weight: 700;
    }}
    h1 {{
      font-size: clamp(34px, 6vw, 62px);
      line-height: .94;
      margin: 0;
      letter-spacing: -.055em;
    }}
    h2 {{ margin: 0 0 14px; font-size: 20px; }}
    h3 {{ margin: 0 0 10px; font-size: 18px; }}
    p {{ line-height: 1.65; }}
    .muted {{ color: var(--muted); }}
    .card {{
      background: var(--card);
      border: 1px solid rgba(255, 255, 255, .78);
      border-radius: 24px;
      padding: 20px;
      box-shadow: 0 18px 55px rgba(17, 24, 39, .08);
      margin: 16px 0;
      backdrop-filter: blur(12px);
    }}
    .notice {{
      border-color: rgba(37, 99, 235, .20);
      background: rgba(239, 246, 255, .82);
    }}
    .warning {{
      border-color: rgba(245, 158, 11, .30);
      background: rgba(255, 251, 235, .88);
    }}
    .danger-card {{
      border-color: rgba(180, 35, 24, .24);
      background: rgba(254, 243, 242, .86);
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin: 18px 0;
    }}
    .stat {{
      background: rgba(255, 255, 255, .62);
      border: 1px solid rgba(255, 255, 255, .78);
      border-radius: 22px;
      padding: 16px;
      box-shadow: 0 14px 36px rgba(17, 24, 39, .06);
    }}
    .stat b {{ display: block; font-size: 30px; letter-spacing: -.04em; margin-bottom: 4px; }}
    form {{ display: grid; gap: 12px; }}
    .inline-form {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .grid-3 {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }}
    label {{ display: grid; gap: 7px; color: var(--soft-ink); font-weight: 800; font-size: 14px; }}
    .check {{
      display: flex;
      align-items: center;
      gap: 9px;
      min-height: 42px;
      padding: 10px 12px;
      border-radius: 16px;
      background: rgba(255, 255, 255, .50);
    }}
    input, select, textarea {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 13px 14px;
      font: inherit;
      background: var(--field);
      color: var(--ink);
      outline: none;
    }}
    input:focus, select:focus, textarea:focus {{
      border-color: rgba(37, 99, 235, .48);
      box-shadow: 0 0 0 4px rgba(37, 99, 235, .10);
    }}
    input[type="checkbox"] {{ width: auto; }}
    button, .button {{
      border: 0;
      border-radius: 999px;
      padding: 12px 17px;
      font: inherit;
      font-weight: 900;
      background: var(--accent);
      color: var(--accent-ink);
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      min-height: 44px;
      box-shadow: 0 12px 24px rgba(17, 24, 39, .16);
    }}
    .button.secondary, button.secondary {{ background: rgba(255, 255, 255, .76); color: var(--ink); box-shadow: none; border: 1px solid var(--line); }}
    .button.danger, button.danger {{ background: var(--danger); }}
    .button.blue, button.blue {{ background: var(--accent-2); }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 9px; margin-top: 12px; }}
    .account-head {{
      display: flex;
      gap: 12px;
      justify-content: space-between;
      align-items: flex-start;
      flex-wrap: wrap;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 6px 10px;
      font-weight: 900;
      font-size: 13px;
      background: rgba(17, 24, 39, .08);
      color: var(--soft-ink);
    }}
    .badge.ok {{ background: rgba(6, 118, 71, .12); color: var(--ok); }}
    .badge.off {{ background: rgba(180, 35, 24, .10); color: var(--danger); }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: rgba(255, 255, 255, .58);
      border: 1px solid rgba(255, 255, 255, .82);
      border-radius: 18px;
      padding: 16px;
      max-height: 520px;
      overflow: auto;
    }}
    .message h2 {{ margin: 0 0 8px; font-size: 22px; letter-spacing: -.02em; }}
    .meta {{ display: grid; gap: 4px; color: var(--muted); margin-bottom: 14px; }}
    .warn {{ color: var(--warn); font-weight: 900; }}
    .danger-text {{ color: var(--danger); font-weight: 900; }}
    code {{ background: rgba(255, 255, 255, .64); padding: 3px 7px; border-radius: 8px; }}
    @media (max-width: 760px) {{
      main {{ width: calc(100vw - 20px); margin: 10px auto; border-radius: 24px; padding: 16px; }}
      .hero, .grid, .grid-3, .stats {{ grid-template-columns: 1fr; }}
      .nav-pills {{ width: 100%; }}
      .nav-pills a {{ flex: 1; text-align: center; }}
    }}
  </style>
</head>
<body><main>{body}</main></body></html>"""


def render_home(token):
    accounts = all_accounts()
    enabled = [account for account in accounts if account.get("enabled", True)]
    key = template_key(token)
    sample = f"{service_base_url()}/show/{{encodedEmail}}"
    if key:
        sample += f"?key={quote(key)}"
    notice = ""
    if not accounts:
        admin_href = f"/admin?key={quote(token)}" if token else "/admin"
        notice = f"""
        <section class="card warning">
          <p class="warn">还没有配置主邮箱。</p>
          <p class="muted">先去 <a href="{admin_href}">邮箱配置</a> 添加一个或多个主邮箱，然后导出的 iCloud 隐藏邮箱链接才能真正查到邮件。</p>
        </section>
        """

    body = f"""
    {render_nav("search", token)}
    <section class="hero">
      <div>
        <p class="eyebrow">iCloud Hide My Email Viewer</p>
        <h1>按隐藏邮箱查转发邮件。</h1>
      </div>
    </section>
    <section class="stats">
      <div class="stat"><b>{len(accounts)}</b><span class="muted">已配置主邮箱</span></div>
      <div class="stat"><b>{len(enabled)}</b><span class="muted">当前启用</span></div>
      <div class="stat"><b>{Config.max_results}</b><span class="muted">单次最多结果</span></div>
    </section>
    {notice}
    <section class="card">
      <h2>搜索邮件</h2>
      <form action="/show" method="get">
        <div class="grid">
          <label>隐藏邮箱地址
            <input name="to" placeholder="alias@example.icloud.com" autocomplete="off">
          </label>
          <label>访问密钥
            <input name="key" placeholder="VIEWER_TOKEN" value="{html.escape(token)}" autocomplete="off">
          </label>
        </div>
        <div class="actions">
          <button type="submit">开始搜索</button>
        </div>
      </form>
    </section>
    <section class="card notice">
      <h2>Tampermonkey 导出模板</h2>
      <p class="muted">在脚本面板的 <code>查看链接模板</code> 里填下面这一行。若你设置了单独的 <code>ADMIN_TOKEN</code>，这里的 <code>YOUR_VIEWER_TOKEN</code> 要换成 `.env` 里的 <code>VIEWER_TOKEN</code>。</p>
      <pre>{html.escape(sample)}</pre>
    </section>
    """
    return page("Mail viewer", body)


def render_messages(address, token, messages, errors, accounts):
    safe_address = html.escape(address)
    enabled_count = len(accounts)
    errors_html = ""
    if errors:
        items = "".join(
            f"<p><strong>{html.escape(item['account'])}</strong>: {html.escape(item['error'])}</p>"
            for item in errors
        )
        errors_html = f"""
        <section class="card danger-card">
          <h2>部分邮箱搜索失败</h2>
          {items}
        </section>
        """

    form = f"""
    {render_nav("search", token)}
    <section class="hero">
      <div>
        <p class="eyebrow">Search target</p>
        <h1>{safe_address}</h1>
      </div>
      <a class="button secondary" href="/?key={quote(token)}">新的搜索</a>
    </section>
    <section class="stats">
      <div class="stat"><b>{len(messages)}</b><span class="muted">匹配邮件</span></div>
      <div class="stat"><b>{enabled_count}</b><span class="muted">已搜索主邮箱</span></div>
      <div class="stat"><b>{Config.max_results}</b><span class="muted">单次上限</span></div>
    </section>
    <section class="card">
      <form action="/show" method="get">
        <div class="grid">
          <label>隐藏邮箱地址
            <input name="to" value="{safe_address}" autocomplete="off">
          </label>
          <label>访问密钥
            <input name="key" value="{html.escape(token)}" autocomplete="off">
          </label>
        </div>
        <div class="actions">
          <button type="submit">刷新搜索</button>
        </div>
      </form>
      <p class="muted">会在所有已启用主邮箱中搜索，结果里会标出命中的主邮箱账号。</p>
    </section>
    {errors_html}
    """
    if not accounts:
        empty = f"""
        <section class="card warning">
          <p class="warn">还没有启用任何主邮箱。</p>
          <p class="muted">请先进入 <a href="/admin?key={quote(token)}">邮箱配置</a> 添加并启用账号。</p>
        </section>
        """
        return page(address, form + empty)

    if not messages:
        empty = """
        <section class="card">
          <p class="warn">没有找到匹配邮件。</p>
          <p class="muted">如果邮件较旧，请在对应主邮箱配置里调大 <code>最近搜索天数</code>；如果邮件被归档到其他文件夹，请调整 <code>IMAP 文件夹</code>。</p>
        </section>
        """
        return page(address, form + empty)

    cards = []
    for message in messages:
        meta = [
            ("主邮箱", message.get("account_name", "")),
            ("From", message.get("from", "")),
            ("To", message.get("to", "")),
            ("Delivered-To", message.get("delivered_to", "")),
            ("Date", message.get("date", "")),
        ]
        meta_html = "".join(
            f"<div><strong>{html.escape(label)}:</strong> {html.escape(value)}</div>"
            for label, value in meta
            if value
        )
        cards.append(f"""
        <article class="card message">
          <h2>{html.escape(message.get("subject") or "(no subject)")}</h2>
          <div class="meta">{meta_html}</div>
          <pre>{html.escape(message.get("body") or "")}</pre>
        </article>
        """)
    return page(address, form + "\n".join(cards))


def render_admin(token, notice="", error=""):
    accounts = all_accounts()
    enabled = [account for account in accounts if account.get("enabled", True)]
    hidden_key = html.escape(token)
    notice_html = f'<section class="card notice"><p>{html.escape(notice)}</p></section>' if notice else ""
    error_html = f'<section class="card danger-card"><p class="danger-text">{html.escape(error)}</p></section>' if error else ""

    account_cards = []
    for account in accounts:
        status_badge = '<span class="badge ok">已启用</span>' if account.get("enabled", True) else '<span class="badge off">已停用</span>'
        toggle_label = "停用" if account.get("enabled", True) else "启用"
        account_cards.append(f"""
        <section class="card">
          <div class="account-head">
            <div>
              <h3>{html.escape(account_label(account))}</h3>
              <p class="muted">{html.escape(account["host"])}:{account["port"]} / {html.escape(account["mailbox"])} / 最近 {account["recent_days"]} 天</p>
            </div>
            {status_badge}
          </div>
          <p class="muted">密码：••••••••；Gmail 原生搜索：{"开启" if account.get("enable_gmail_raw") else "关闭"}；严格本地过滤：{"开启" if account.get("strict_local_filter") else "关闭"}</p>
          <div class="actions">
            <form class="inline-form" method="post" action="/admin/accounts">
              <input type="hidden" name="key" value="{hidden_key}">
              <input type="hidden" name="account_id" value="{html.escape(account["id"])}">
              <input type="hidden" name="action" value="test">
              <button class="secondary" type="submit">测试连接</button>
            </form>
            <form class="inline-form" method="post" action="/admin/accounts">
              <input type="hidden" name="key" value="{hidden_key}">
              <input type="hidden" name="account_id" value="{html.escape(account["id"])}">
              <input type="hidden" name="action" value="toggle">
              <button class="blue" type="submit">{toggle_label}</button>
            </form>
            <form class="inline-form" method="post" action="/admin/accounts">
              <input type="hidden" name="key" value="{hidden_key}">
              <input type="hidden" name="account_id" value="{html.escape(account["id"])}">
              <input type="hidden" name="action" value="delete">
              <button class="danger" type="submit">删除</button>
            </form>
          </div>
        </section>
        """)

    accounts_html = "\n".join(account_cards) or """
    <section class="card warning">
      <p class="warn">暂无主邮箱账号。</p>
      <p class="muted">添加 Gmail 时建议使用 App Password，不要使用普通登录密码。</p>
    </section>
    """

    body = f"""
    {render_nav("admin", token)}
    <section class="hero">
      <div>
        <p class="eyebrow">Mailbox Pool</p>
        <h1>配置多个主邮箱。</h1>
      </div>
      <a class="button secondary" href="/?key={quote(token)}">返回搜索</a>
    </section>
    <section class="stats">
      <div class="stat"><b>{len(accounts)}</b><span class="muted">账号总数</span></div>
      <div class="stat"><b>{len(enabled)}</b><span class="muted">已启用</span></div>
      <div class="stat"><b>{html.escape(Path(Config.config_path).name)}</b><span class="muted">配置文件</span></div>
    </section>
    {notice_html}
    {error_html}
    <section class="card notice">
      <h2>安全提示</h2>
      <p class="muted">这些账号配置会保存在容器数据卷的 JSON 文件里。公网部署请使用 HTTPS，并把 <code>VIEWER_TOKEN</code> / <code>ADMIN_TOKEN</code> 设置成长随机字符串。</p>
    </section>
    <section class="card">
      <h2>添加主邮箱</h2>
      <form method="post" action="/admin/accounts">
        <input type="hidden" name="key" value="{hidden_key}">
        <input type="hidden" name="action" value="add">
        <div class="grid">
          <label>显示名称
            <input name="name" placeholder="例如：主 Gmail 1">
          </label>
          <label>邮箱账号
            <input name="user" placeholder="your-main-mailbox@gmail.com" autocomplete="username">
          </label>
        </div>
        <div class="grid">
          <label>App Password / IMAP 密码
            <input name="password" type="password" autocomplete="new-password">
          </label>
          <label>IMAP 服务器
            <input name="host" value="{html.escape(Config.default_imap_host)}">
          </label>
        </div>
        <div class="grid-3">
          <label>端口
            <input name="port" type="number" min="1" value="{Config.default_imap_port}">
          </label>
          <label>IMAP 文件夹
            <input name="mailbox" value="{html.escape(Config.default_imap_mailbox)}">
          </label>
          <label>最近搜索天数
            <input name="recent_days" type="number" min="1" value="{Config.default_recent_days}">
          </label>
        </div>
        <div class="grid-3">
          <label>抓取上限
            <input name="fetch_limit" type="number" min="1" value="{Config.default_fetch_limit}">
          </label>
          <label>连接超时秒数
            <input name="timeout_seconds" type="number" min="1" value="{Config.default_timeout_seconds}">
          </label>
          <label class="check">
            <input name="enabled" type="checkbox" value="1" checked>
            添加后启用
          </label>
        </div>
        <div class="grid-3">
          <label class="check">
            <input name="enable_gmail_raw" type="checkbox" value="1"{checked(Config.default_enable_gmail_raw)}>
            Gmail 原生搜索
          </label>
          <label class="check">
            <input name="strip_password_spaces" type="checkbox" value="1"{checked(Config.default_strip_password_spaces)}>
            自动去掉 Gmail 密码空格
          </label>
          <label class="check">
            <input name="strict_local_filter" type="checkbox" value="1"{checked(Config.default_strict_local_filter)}>
            严格本地过滤
          </label>
        </div>
        <div class="actions">
          <button type="submit">保存邮箱账号</button>
        </div>
      </form>
    </section>
    <section>
      <h2>已配置账号</h2>
      {accounts_html}
    </section>
    """
    return page("Mailbox config", body)


class Handler(BaseHTTPRequestHandler):
    server_version = "MailViewer/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), fmt % args))

    def send_payload(self, status, content_type, payload):
        data = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def redirect_admin(self, token, notice="", error=""):
        params = {"key": token}
        if notice:
            params["notice"] = notice
        if error:
            params["error"] = error
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/admin?{urlencode(params)}")
        self.end_headers()

    def read_form(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 200_000:
            raise ValueError("Form is too large.")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return parse_qs(raw)

    def token_from_request(self, query):
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return query.get("key", [""])[0]

    def authorized(self, query):
        if Config.allow_no_token:
            return True
        return bool(Config.viewer_token) and self.token_from_request(query) == Config.viewer_token

    def admin_authorized(self, query):
        if Config.allow_no_token:
            return True
        secret = Config.admin_secret()
        return bool(secret) and self.token_from_request(query) == secret

    def reject(self, message, status=HTTPStatus.UNAUTHORIZED):
        _, content_type, payload = page(
            "Unauthorized",
            f"<section class=\"card danger-card\"><p class=\"danger-text\">{html.escape(message)}</p></section>",
            status,
        )
        self.send_payload(status, content_type, payload)

    def get_address(self, parsed, query):
        if parsed.path == "/show":
            return query.get("to", [""])[0].strip()
        if parsed.path.startswith("/show/"):
            return unquote(parsed.path[len("/show/") :]).strip()
        return ""

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/health":
            missing = Config.validate()
            accounts = all_accounts()
            enabled = [account for account in accounts if account.get("enabled", True)]
            status = HTTPStatus.SERVICE_UNAVAILABLE if missing else HTTPStatus.OK
            payload = {
                "ok": not missing,
                "missing": missing,
                "configured_accounts": len(accounts),
                "enabled_accounts": len(enabled),
                "config_path": Config.config_path,
            }
            self.send_payload(status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False))
            return

        if parsed.path == "/" or parsed.path == "":
            token = self.token_from_request(query)
            status, content_type, payload = render_home(token)
            self.send_payload(status, content_type, payload)
            return

        if parsed.path == "/admin":
            if not self.admin_authorized(query):
                self.reject("Missing or invalid ADMIN_TOKEN. Open /admin?key=your-admin-token.")
                return
            token = self.token_from_request(query)
            notice = query.get("notice", [""])[0]
            error = query.get("error", [""])[0]
            status, content_type, payload = render_admin(token, notice, error)
            self.send_payload(status, content_type, payload)
            return

        if parsed.path.startswith("/api/messages"):
            if not self.authorized(query):
                self.send_payload(HTTPStatus.UNAUTHORIZED, "application/json; charset=utf-8", json.dumps({"error": "unauthorized"}))
                return
            address = query.get("to", [""])[0].strip()
            if not EMAIL_RE.match(address):
                self.send_payload(HTTPStatus.BAD_REQUEST, "application/json; charset=utf-8", json.dumps({"error": "invalid email"}))
                return
            try:
                messages, errors, accounts = fetch_messages(address)
                self.send_payload(
                    HTTPStatus.OK,
                    "application/json; charset=utf-8",
                    json.dumps(
                        {
                            "address": address,
                            "messages": messages,
                            "errors": errors,
                            "searched_accounts": [public_account(account) for account in accounts],
                        },
                        ensure_ascii=False,
                    ),
                )
            except Exception as exc:
                self.send_payload(HTTPStatus.INTERNAL_SERVER_ERROR, "application/json; charset=utf-8", json.dumps({"error": str(exc)}))
            return

        if parsed.path == "/show" or parsed.path.startswith("/show/"):
            if not self.authorized(query):
                self.reject("Missing or invalid VIEWER_TOKEN. Add ?key=your-token to the link.")
                return
            address = self.get_address(parsed, query)
            if not EMAIL_RE.match(address):
                self.reject("Invalid email address.", HTTPStatus.BAD_REQUEST)
                return
            try:
                messages, errors, accounts = fetch_messages(address)
                token = self.token_from_request(query)
                status, content_type, payload = render_messages(address, token, messages, errors, accounts)
                self.send_payload(status, content_type, payload)
            except Exception:
                error = traceback.format_exc(limit=3)
                status, content_type, payload = page(
                    "Error",
                    f"<section class=\"card danger-card\"><p class=\"danger-text\">Mail lookup failed.</p><pre>{html.escape(error)}</pre></section>",
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                self.send_payload(status, content_type, payload)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/admin/accounts":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            form = self.read_form()
        except ValueError as exc:
            self.reject(str(exc), HTTPStatus.BAD_REQUEST)
            return

        token = first_value(form, "key")
        if not self.admin_authorized({"key": [token]}):
            self.reject("Missing or invalid ADMIN_TOKEN.")
            return

        action = first_value(form, "action")
        account_id = first_value(form, "account_id")
        store = load_store()
        accounts = store.get("accounts", [])

        if action == "add":
            account = account_from_form(form)
            errors = validate_account(account)
            if errors:
                self.redirect_admin(token, error=" ".join(errors))
                return
            accounts.append(account)
            store["accounts"] = accounts
            save_store(store)
            self.redirect_admin(token, notice=f"已添加账号：{account_label(account)}")
            return

        target = next((account for account in accounts if account.get("id") == account_id), None)
        if not target:
            self.redirect_admin(token, error="没有找到这个邮箱账号。")
            return

        if action == "delete":
            store["accounts"] = [account for account in accounts if account.get("id") != account_id]
            save_store(store)
            self.redirect_admin(token, notice=f"已删除账号：{account_label(target)}")
            return

        if action == "toggle":
            target["enabled"] = not target.get("enabled", True)
            target["updated_at"] = now_iso()
            save_store(store)
            state = "启用" if target["enabled"] else "停用"
            self.redirect_admin(token, notice=f"已{state}账号：{account_label(target)}")
            return

        if action == "test":
            ok, message = test_account_connection(target)
            if ok:
                self.redirect_admin(token, notice=message)
            else:
                self.redirect_admin(token, error=message)
            return

        self.redirect_admin(token, error="未知操作。")


def main():
    missing = Config.validate()
    if missing:
        print(f"Missing required env vars: {', '.join(missing)}", file=sys.stderr)
        print("Set ALLOW_NO_TOKEN=1 only for private local testing.", file=sys.stderr)
        sys.exit(2)
    server = ThreadingHTTPServer((Config.bind, Config.port), Handler)
    print(f"Mail viewer listening on {Config.bind}:{Config.port}", flush=True)
    print(f"Config path: {Config.config_path}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
