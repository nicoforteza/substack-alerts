#!/usr/bin/env python3
"""Lee una newsletter de Substack en Gmail, extrae compras de opciones con un LLM y avisa por Telegram.

Estado: cada email procesado recibe una etiqueta de Gmail (PROCESSED_LABEL), así que no hace
falta base de datos. IMPORTANTE: el repo es público y los logs de Actions también, por lo que
este script nunca imprime contenido de la newsletter en modo producción.

Uso:
    python bot.py                      # producción (Gmail -> LLM -> Telegram)
    python bot.py --file email.txt     # prueba local de extracción; imprime, no envía
    python bot.py --dry-run            # lee Gmail pero no envía ni etiqueta; imprime (solo en local)
"""
from __future__ import annotations

import argparse
import email
import html
import imaplib
import json
import os
import re
import sys
import urllib.request
from email.policy import default as default_policy

# ---------------------------------------------------------------- configuración
SENDER = os.environ.get("SENDER", "tictoctrading@substack.com")
PROCESSED_LABEL = os.environ.get("PROCESSED_LABEL", "bot-procesado")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "gemini").lower()   # "gemini" | "claude"
OPTION_TYPES = {s.strip() for s in os.environ.get("OPTION_TYPES", "call").lower().split(",")}
NOTIFY_EMPTY = os.environ.get("NOTIFY_EMPTY", "true").lower() == "true"
MAX_CHARS = 60_000

PROMPT = """You extract OPTION BUY recommendations from a trading newsletter.
Return ONLY a JSON object, no prose, no markdown fences:
{"trades": [ {
  "ticker": str,                       // underlying ticker, uppercase
  "option_type": "call" | "put",
  "expiry": str | null,                // as stated, e.g. "Jan 2027"; ISO date if an exact date is given
  "strike": number | null,
  "premium": number | null,            // option price per share the author suggests paying/considering
  "premium_qualifier": "approx" | "max" | "limit" | null,
  "underlying_price": number | null,   // current underlying price if the author mentions it
  "notes": str,                        // very short, in Spanish (e.g. "lotto", "LEAPS", "especulativa")
  "quote": str                         // verbatim sentence(s) from the text supporting this, max 300 chars
} ] }
Rules:
- Include only explicit suggestions to buy or consider specific option contracts.
- Do NOT include stock/ETF/futures price levels (e.g. "buyer at 7660"), nor past results of earlier calls.
- Never invent strike, expiry or premium: use null when not stated.
- Do not confuse the underlying's price with the option premium or the strike.
- If there is none, return {"trades": []}."""


# ---------------------------------------------------------------- utilidades HTTP
def _post_json(url: str, payload: dict, headers: dict, timeout: int = 120) -> dict:
    """Devuelve la respuesta JSON de un POST."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


# ---------------------------------------------------------------- LLM
def _call_gemini(text: str) -> str:
    """Devuelve el texto de respuesta de Gemini."""
    model = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
    }
    r = _post_json(url, payload, {"x-goog-api-key": os.environ["GEMINI_API_KEY"]})
    return "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])


def _call_claude(text: str) -> str:
    """Devuelve el texto de respuesta de Claude."""
    payload = {
        "model": os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
        "max_tokens": 2000, "temperature": 0, "system": PROMPT,
        "messages": [{"role": "user", "content": text}],
    }
    r = _post_json("https://api.anthropic.com/v1/messages", payload,
                   {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"})
    return "".join(b.get("text", "") for b in r["content"] if b.get("type") == "text")


def _num(x) -> float | None:
    """Devuelve x como float, o None si no es convertible."""
    try:
        return float(str(x).replace("$", "").replace(",", "")) if x is not None else None
    except ValueError:
        return None


def parse_trades(raw: str) -> list[dict]:
    """Devuelve la lista de operaciones validadas a partir de la respuesta cruda del LLM."""
    raw = re.sub(r"```(?:json)?", "", raw).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("respuesta del LLM sin JSON")
    data = json.loads(raw[start:end + 1])
    trades = []
    for t in data.get("trades", []) or []:
        if not isinstance(t, dict) or not str(t.get("ticker") or "").strip():
            continue
        otype = str(t.get("option_type") or "").lower()
        if otype not in OPTION_TYPES:
            continue
        trades.append({
            "ticker": str(t["ticker"]).strip().upper().lstrip("$"),
            "option_type": otype,
            "expiry": (str(t["expiry"]).strip() or None) if t.get("expiry") else None,
            "strike": _num(t.get("strike")),
            "premium": _num(t.get("premium")),
            "premium_qualifier": t.get("premium_qualifier"),
            "underlying_price": _num(t.get("underlying_price")),
            "notes": str(t.get("notes") or "")[:100],
            "quote": str(t.get("quote") or "")[:300],
        })
    return trades


def extract_trades(text: str) -> list[dict]:
    """Devuelve las operaciones de opciones encontradas en el texto."""
    fn = {"gemini": _call_gemini, "claude": _call_claude}[LLM_PROVIDER]
    return parse_trades(fn(text[:MAX_CHARS]))


# ---------------------------------------------------------------- email
def html_to_text(s: str) -> str:
    """Devuelve texto plano aproximado a partir de HTML."""
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|h\d|li|tr)>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"[ \t\xa0]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def email_to_text(msg: email.message.EmailMessage) -> str:
    """Devuelve el cuerpo del email como texto plano, sin URLs."""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    if part.get_content_type() == "text/html":
        content = html_to_text(content)
    return re.sub(r"https?://\S+", "", content)


def _all_mail_folder(imap: imaplib.IMAP4_SSL) -> str:
    """Devuelve el nombre de 'Todos' en Gmail (depende del idioma), o INBOX."""
    _, folders = imap.list()
    for raw in folders or []:
        line = raw.decode(errors="replace")
        if "\\All" in line:
            m = re.search(r'"([^"]+)"\s*$', line)
            if m:
                return m.group(1)
    return "INBOX"


# ---------------------------------------------------------------- Telegram
def fmt_money(x: float | None) -> str:
    """Devuelve el importe formateado o 'no indicado'."""
    return "no indicado" if x is None else f"${x:,.2f}"


def format_trade(t: dict, subject: str) -> str:
    """Devuelve el mensaje de Telegram (HTML) para una operación."""
    e = html.escape
    qual = {"approx": "≈ ", "max": "≤ ", "limit": "límite "}.get(t["premium_qualifier"] or "", "")
    strike = "no indicado" if t["strike"] is None else f"{t['strike']:g}"
    lines = [
        f"🟢 <b>{e(t['ticker'])} {t['option_type'].upper()}</b>" + (f" · {e(t['notes'])}" if t["notes"] else ""),
        f"Vencimiento: {e(t['expiry'] or 'no indicado')} · Strike: {strike}",
        f"Precio: {qual}{fmt_money(t['premium'])}"
        + (f" (subyacente ≈ {fmt_money(t['underlying_price'])})" if t["underlying_price"] else ""),
    ]
    if t["premium"] and t["underlying_price"] and t["premium"] >= t["underlying_price"]:
        lines.append("⚠️ Revisar: la prima es ≥ al precio del subyacente")
    if t["quote"]:
        lines.append(f"<i>«{e(t['quote'])}»</i>")
    lines.append(f"📰 {e(subject)}")
    return "\n".join(lines)


def send_telegram(text: str) -> None:
    """Envía un mensaje al chat configurado."""
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage"
    _post_json(url, {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text[:4000],
                     "parse_mode": "HTML", "disable_web_page_preview": True}, {})


# ---------------------------------------------------------------- flujos
def run_file(path: str) -> None:
    """Prueba local: extrae operaciones de un fichero de texto e imprime los mensajes."""
    with open(path, encoding="utf-8") as f:
        trades = extract_trades(f.read())
    print(f"{len(trades)} operación(es) encontradas\n")
    for t in trades:
        print(format_trade(t, os.path.basename(path)), "\n")


def run_gmail(dry_run: bool) -> int:
    """Procesa los emails pendientes; devuelve el número de errores."""
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"])
    folder = _all_mail_folder(imap)
    typ, _ = imap.select(f'"{folder}"')
    assert typ == "OK", f"no se pudo abrir la carpeta {folder!r}"

    query = f"from:{SENDER} -label:{PROCESSED_LABEL} newer_than:{LOOKBACK_DAYS}d"
    _, data = imap.uid("search", None, "X-GM-RAW", f'"{query}"')
    uids = data[0].split() if data and data[0] else []
    print(f"Emails pendientes: {len(uids)}")

    n_trades, errors = 0, 0
    for i, uid in enumerate(uids, 1):
        try:
            _, msg_data = imap.uid("fetch", uid, "(BODY.PEEK[])")   # PEEK: no marca como leído
            msg = email.message_from_bytes(msg_data[0][1], policy=default_policy)
            subject = str(msg.get("subject", "(sin asunto)"))
            trades = extract_trades(email_to_text(msg))
            messages = [format_trade(t, subject) for t in trades]
            if not trades and NOTIFY_EMPTY:
                messages = [f"📭 Newsletter procesada, sin calls:\n{html.escape(subject)}"]
            for m in messages:
                print(m, "\n") if dry_run else send_telegram(m)
            if not dry_run:
                imap.uid("store", uid, "+X-GM-LABELS", f"({PROCESSED_LABEL})")
            n_trades += len(trades)
            print(f"Email {i}/{len(uids)}: {len(trades)} operación(es)")   # sin contenido: logs públicos
        except Exception as ex:   # noqa: BLE001 — un email roto no debe parar el resto
            errors += 1
            print(f"Email {i}/{len(uids)}: ERROR {type(ex).__name__}: {str(ex)[:200]}")
    imap.logout()

    print(f"Total operaciones: {n_trades} · errores: {errors}")
    if errors and not dry_run:
        send_telegram(f"⚠️ Bot de alertas: {errors} email(s) fallaron. Se reintentará en la próxima ejecución.")
    return errors


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", help="probar la extracción con un fichero de texto (no envía nada)")
    ap.add_argument("--dry-run", action="store_true", help="leer Gmail sin enviar ni etiquetar (solo local)")
    args = ap.parse_args()
    if args.dry_run and os.environ.get("GITHUB_ACTIONS"):
        sys.exit("--dry-run imprime contenido: no usarlo en Actions (logs públicos)")
    if args.file:
        run_file(args.file)
    else:
        sys.exit(1 if run_gmail(args.dry_run) else 0)


if __name__ == "__main__":
    main()
