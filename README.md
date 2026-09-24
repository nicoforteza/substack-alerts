# Alertas de opciones desde Substack → Telegram

Cada 6 h (GitHub Actions), el bot lee en Gmail los emails nuevos de la newsletter, usa un LLM
para extraer recomendaciones de compra de calls y te las manda por Telegram. Cada email
procesado recibe la etiqueta `bot-procesado` en Gmail, que es todo el estado que necesita.
No tiene dependencias externas (solo librería estándar de Python).

## Puesta en marcha

1. **Telegram**: habla con @BotFather → `/newbot` → guarda el token. Escríbele cualquier cosa a
   tu bot y abre `https://api.telegram.org/bot<TOKEN>/getUpdates`: el número de `chat.id` es tu `TELEGRAM_CHAT_ID`.
2. **Gmail**: activa la verificación en dos pasos y crea una contraseña de aplicación en
   https://myaccount.google.com/apppasswords.
3. **LLM (gratis)**: crea una API key en https://aistudio.google.com (Gemini, capa gratuita).
4. **Remitente**: abre un email de la newsletter y comprueba la dirección exacta del campo "De".
   Si no es `tictoctrading@substack.com`, créala como variable `SENDER`.
5. **Secrets** (Settings → Secrets and variables → Actions → Secrets):
   `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `GEMINI_API_KEY`.
6. **Probar**: pestaña Actions → alertas-opciones → Run workflow.

## Parámetros

| Dónde | Nombre | Por defecto | Qué hace |
|---|---|---|---|
| `alertas.yml` | `cron` | `17 */6 * * *` | Frecuencia (UTC) |
| Variable | `SENDER` | `tictoctrading@substack.com` | Remitente a vigilar |
| Variable | `LLM_PROVIDER` | `gemini` | `gemini` o `claude` (requiere secret `ANTHROPIC_API_KEY`) |
| Variable | `GEMINI_MODEL` | `gemini-flash-latest` | Modelo de Gemini |
| Variable | `LOOKBACK_DAYS` | `7` | Antigüedad máxima de emails a revisar |
| Variable | `OPTION_TYPES` | `call` | `call`, `put` o `call,put` |
| Variable | `NOTIFY_EMPTY` | `true` | Avisar también si la newsletter no trae calls |

## Prueba local

```bash
export GEMINI_API_KEY=...
python3 bot.py --file samples/newsletter.txt   # samples/ está en .gitignore
```

## Notas

- El repo es público y los logs de Actions también: el bot no imprime contenido de la newsletter,
  y nunca debes subir emails de ejemplo al repo.
- Si un email falla (p. ej. error del LLM), no se etiqueta, se reintenta en la siguiente ejecución
  y recibes un aviso por Telegram.
- Para reprocesar un email, quítale la etiqueta `bot-procesado` en Gmail.
