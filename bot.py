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
import time
import urllib.error
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
CHUNK_CHARS = int(os.environ.get("CHUNK_CHARS", "12000"))   # textos largos dan 503 en la capa gratuita
CHUNK_OVERLAP = 800                                           # para no partir una recomendación entre trozos
# Emails de Substack que no son la newsletter (bienvenida, recibos, códigos de acceso, hilos del chat).
# Se buscan solo en el asunto y el principio del cuerpo: las newsletters mencionan "chat" en el pie.
SKIP_REGEX = os.environ.get("SKIP_REGEX") or (
    r"good to have you here"                  # bienvenida
    r"|\breceipt\b"                           # recibo de pago
    r"|verification code"                     # código para iniciar sesión
    r"|started a thread|join the chat for"    # hilo del chat de suscriptores
)
SKIP_HEAD_CHARS = 400
RETRY_STATUS = {429, 500, 502, 503, 504, 529}   # errores HTTP temporales (529 = Anthropic saturado)
RETRY_WAITS = (10, 30)                      # segundos de espera entre intentos

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


# ---------------------------------------------------------------- utilidades
def _secret(name: str) -> str:
    """Devuelve la variable de entorno sin espacios ni saltos de línea (frecuentes al pegar secrets)."""
    return os.environ[name].strip()


def _error_detail(ex: Exception) -> str:
    """Devuelve tipo y mensaje del error (con el motivo que da la API si es HTTP), truncado a 200."""
    msg = str(ex)
    if isinstance(ex, urllib.error.HTTPError):
        try:
            body = json.loads(ex.read())
            msg += " · " + str(body.get("error", {}).get("message") or body.get("description") or "")
        except Exception:   # noqa: BLE001 — el cuerpo del error es opcional
            pass
    return f"{type(ex).__name__}: {msg}"[:200]


def _post_json(url: str, payload: dict, headers: dict, timeout: int = 120) -> dict:
    """Devuelve la respuesta JSON de un POST, reintentando los errores HTTP temporales."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    for wait in (*RETRY_WAITS, None):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as ex:
            if wait is None or ex.code not in RETRY_STATUS:
                raise
            time.sleep(wait)


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
    r = _post_json(url, payload, {"x-goog-api-key": _secret("GEMINI_API_KEY")})
    return "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])


def _call_claude(text: str) -> str:
    """Devuelve el texto de respuesta de Claude."""
    model = os.environ.get("ANTHROPIC_MODEL") or "claude-opus-5"
    payload = {
        "model": model, "max_tokens": 16000, "system": PROMPT,
        "messages": [{"role": "user", "content": text}],
    }
    headers = {"x-api-key": _secret("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01"}
    if "haiku" in model:
        payload["temperature"] = 0            # Opus/Sonnet actuales rechazan temperature (400)
    else:
        payload["output_config"] = {"effort": "low"}   # extracción sencilla: menos razonamiento, menos coste
        payload["fallbacks"] = "default"               # si el modelo rechaza la petición, otro la reintenta
        headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
    r = _post_json("https://api.anthropic.com/v1/messages", payload, headers, timeout=300)
    if r.get("stop_reason") == "refusal":
        raise ValueError("Claude rechazó la petición")
    if r.get("stop_reason") == "max_tokens":
        raise ValueError("respuesta de Claude cortada por max_tokens")
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


def split_text(text: str) -> list[str]:
    """Devuelve el texto en trozos de ≤CHUNK_CHARS, cortando por párrafos y con solapamiento."""
    chunks, start = [], 0
    while len(text) - start > CHUNK_CHARS:
        end = start + CHUNK_CHARS
        cut = max(text.rfind("\n\n", start, end), text.rfind("\n", start, end))
        if cut <= start + CHUNK_CHARS // 2:   # sin salto de línea útil: cortar en un espacio
            cut = text.rfind(" ", start, end)
        if cut <= start + CHUNK_CHARS // 2:
            cut = end
        chunks.append(text[start:cut])
        start = max(cut - CHUNK_OVERLAP, start + 1)
    chunks.append(text[start:])
    return chunks


def extract_trades(text: str) -> list[dict]:
    """Devuelve las operaciones de opciones encontradas en el texto, sin duplicados entre trozos."""
    fn = {"gemini": _call_gemini, "claude": _call_claude}[LLM_PROVIDER]
    trades, seen = [], set()
    for chunk in split_text(text[:MAX_CHARS]):
        for t in parse_trades(fn(chunk)):
            key = (t["ticker"], t["option_type"], (t["expiry"] or "").lower(), t["strike"])
            if key not in seen:
                seen.add(key)
                trades.append(t)
    return trades


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


def is_notification(subject: str, text: str) -> bool:
    """Devuelve True si el email es una notificación de Substack y no la newsletter."""
    head = f"{subject}\n{text[:SKIP_HEAD_CHARS]}"
    return re.search(SKIP_REGEX, head, re.IGNORECASE) is not None


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
    url = f"https://api.telegram.org/bot{_secret('TELEGRAM_TOKEN')}/sendMessage"
    _post_json(url, {"chat_id": _secret("TELEGRAM_CHAT_ID"), "text": text[:4000],
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
    imap.login(_secret("GMAIL_USER"), _secret("GMAIL_APP_PASSWORD"))
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
            text = email_to_text(msg)
            if is_notification(subject, text):
                if not dry_run:
                    imap.uid("store", uid, "+X-GM-LABELS", f"({PROCESSED_LABEL})")
                print(f"Email {i}/{len(uids)}: notificación de Substack, omitido")
                continue
            print(f"Email {i}/{len(uids)}: {len(text)} caracteres, "   # solo contadores: logs públicos
                  f"{len(split_text(text[:MAX_CHARS]))} trozo(s)")
            trades = extract_trades(text)
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
            print(f"Email {i}/{len(uids)}: ERROR {_error_detail(ex)}")
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
