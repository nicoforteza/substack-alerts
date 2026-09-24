# CLAUDE.md — Bot de alertas de opciones (Substack → Gemini → Telegram)

Guía para Claude Code. Léela entera antes de tocar nada.

## Qué es

Cada 6 h, un workflow de GitHub Actions ejecuta `bot.py`, que:
1. Entra en Gmail por IMAP (contraseña de aplicación) y busca con `X-GM-RAW`
   `from:$SENDER -label:bot-procesado newer_than:${LOOKBACK_DAYS}d`.
2. Pasa el texto de cada email a Gemini (`gemini-flash-latest`, capa gratuita), que devuelve JSON con
   las compras de opciones recomendadas.
3. Valida el JSON (`parse_trades`), filtra por `OPTION_TYPES` (por defecto solo calls) y envía un
   mensaje por operación a Telegram (HTML).
4. Etiqueta el email como `bot-procesado`. Esa etiqueta es TODO el estado; no hay base de datos.
   Si algo falla, el email no se etiqueta y se reintenta en la siguiente ejecución.

Newsletter: Orderflow / tictoctrading (Substack, de pago). Las recomendaciones de calls vienen
enterradas en prosa (p. ej. "these January 2027 RGTI calls are like 50 cents"), por eso se usa un LLM.

## Ficheros

- `bot.py` — todo el código. Solo librería estándar: **no añadir dependencias**.
- `.github/workflows/alertas.yml` — cron (`17 */6 * * *`, UTC), ejecución manual y keepalive.
- `README.md` — puesta en marcha para humanos.
- `samples/` — emails de ejemplo para pruebas locales. **Nunca se sube** (está en `.gitignore`).
- `.env` — claves para pruebas locales. **Nunca se sube ni se lee** (ver reglas).

## Reglas de seguridad (no negociables)

El repo es PÚBLICO y los logs de Actions también.

1. **Nunca leas, muestres ni hagas `cat` de `.env`** ni de ningún secreto. Para usarlo, cárgalo sin
   mostrarlo: `set -a; source .env; set +a`.
2. **Nunca imprimas contenido de la newsletter en el camino de producción** (`run_gmail` sin
   `--dry-run`): ni texto del email, ni asunto, ni operaciones, ni la respuesta del LLM. Solo
   contadores y tipos de error. Si añades logs de depuración, que dependan de `--dry-run` y nunca
   se ejecuten en Actions.
3. **Nunca hagas commit de `samples/`, `.env`, `*.eml` ni `*.txt`.** Antes de cada commit revisa
   `git status` y `git diff --cached --stat`.
4. No crees ni modifiques secrets de GitHub tú mismo: pide al usuario que ejecute
   `gh secret set NOMBRE` en su propia terminal (le pedirá el valor sin mostrarlo).
5. Los mensajes de error que se imprimen se truncan a 200 caracteres; mantenlo así.

## Comandos habituales

```bash
# Lanzar el workflow a mano y seguirlo
gh workflow run alertas.yml
gh run list --workflow=alertas.yml --limit 5
gh run watch                      # sigue la última ejecución
gh run view --log-failed          # solo los pasos fallidos
gh run view <run-id> --log

# Secrets y variables (los secrets los mete el usuario, ver regla 4)
gh secret list
gh variable list
gh variable set LOOKBACK_DAYS --body 30     # variables sí puedes tocarlas
gh variable delete LOOKBACK_DAYS

# Pruebas locales (requieren .env con GEMINI_API_KEY y, para Gmail, GMAIL_*; TELEGRAM_* opcional)
set -a; source .env; set +a
python3 bot.py --file samples/newsletter.txt    # solo extracción, imprime, no envía
python3 bot.py --dry-run                        # lee Gmail, imprime, no envía ni etiqueta
python3 -c "import ast; ast.parse(open('bot.py').read())"   # chequeo de sintaxis
```

Resultado esperado con el email de ejemplo del 2026-09 (si está en `samples/`): dos calls,
RGTI ene-2027 a ≈$0.50 (strike no indicado, subyacente ≈17) y SNAP ene-2028 strike 5 a ≈$1.50.
Ningún nivel del emini, TSLA, KMX, DIS ni MU debe aparecer como operación.

## Configuración (variables de Actions, todas opcionales)

`SENDER` (tictoctrading@substack.com) · `LLM_PROVIDER` (gemini | claude) · `GEMINI_MODEL`
(gemini-flash-latest) · `LOOKBACK_DAYS` (7) · `OPTION_TYPES` (call) · `NOTIFY_EMPTY` (true).
Secrets: `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `GEMINI_API_KEY`.

## Guía de depuración

| Síntoma en el log | Causa probable | Qué hacer |
|---|---|---|
| `AUTHENTICATIONFAILED` / `LOGIN failed` | Contraseña de aplicación mal copiada o sin 2FA | El usuario regenera y vuelve a `gh secret set GMAIL_APP_PASSWORD` |
| `Emails pendientes: 0` y sí hay newsletter | `SENDER` no coincide, email más antiguo que `LOOKBACK_DAYS`, o ya etiquetado | Comprobar el "De" real; subir `LOOKBACK_DAYS`; quitar la etiqueta en Gmail |
| `HTTPError: HTTP Error 404` en un email | Nombre de modelo de Gemini no válido | Revisar la lista de modelos en ai.google.dev y fijar `GEMINI_MODEL` |
| `HTTP Error 400/403` (Gemini) | API key inválida o región sin capa gratuita | Regenerar la key en AI Studio |
| `HTTP Error 429` | Límite de la capa gratuita | Se reintenta sola en la siguiente ejecución; si persiste, reducir frecuencia |
| `KeyError: 'candidates'` | Gemini bloqueó o devolvió respuesta vacía | Probar en local con `--file` y ajustar `PROMPT` |
| `ValueError: respuesta del LLM sin JSON` / `JSONDecodeError` | Salida mal formada | Probar en local; endurecer `PROMPT` o `parse_trades` |
| Telegram `HTTP Error 400` | `chat_id` erróneo o HTML inválido en el mensaje | Verificar `chat_id` con getUpdates; revisar escapado en `format_trade` |
| Telegram `HTTP Error 401` | Token del bot incorrecto | El usuario lo vuelve a meter |
| El cron no se ejecuta | Workflow no está en la rama por defecto, o GitHub lo desactivó | `gh workflow enable alertas.yml`; revisar que el keepalive corre |

Al depurar un fallo de extracción, reprodúcelo SIEMPRE en local con `--file` o `--dry-run`, nunca
añadiendo prints en Actions.

## Convenciones de código

- Python ≥3.10, solo stdlib (`urllib`, `imaplib`, `email`, `json`, `re`, `html`).
- Cada función con docstring de una línea que diga qué devuelve.
- Constantes de configuración en MAYÚSCULAS al principio, leídas de variables de entorno con default.
- Mensajes al usuario y de log en español.
- Un email que falla no debe detener el resto (`try/except` por email ya existente).
- Si cambias `PROMPT` o `parse_trades`, verifica con el email de ejemplo que el resultado esperado
  (sección anterior) sigue saliendo igual.

## Estado de la puesta en marcha

Pendiente de confirmar con el usuario. Pasos: subir estos ficheros al repo → crear bot de Telegram
y obtener `chat_id` → contraseña de aplicación de Gmail → API key de Gemini → comprobar remitente →
`gh secret set` de los 5 secrets → `gh workflow run alertas.yml` → verificar mensaje en Telegram y
etiqueta en Gmail. Actualiza esta sección cuando cambie el estado.
