"""Chequea turnos del Dr. Fugazzotto en el portal del Sanatorio Finochietto
y avisa por Telegram si hay alguno dentro de los próximos DAYS_AHEAD días.

Solo usa la biblioteca estándar de Python. Variables de entorno (o archivo .env):
  PORTAL_EMAIL, PORTAL_PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Opcionales: DAYS_AHEAD (7), STATE_FILE (state.json)

Uso:
  python check_turnos.py            # chequeo normal
  python check_turnos.py --chat-id  # muestra el chat_id de quien le escribió al bot
  python check_turnos.py --test     # manda un mensaje de prueba por Telegram
"""
import http.cookiejar
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://turnos.sanatoriofinochietto.com/"
AREA_CODE = "160073"  # Fugazzotto de la Cruz Juan Jesus
AREA_NAME = "Fugazzotto de la cruz Juan Jesus"
COMPONENT_CODE = "31"  # Gastroenterología
BRANCH_ID = "2"  # Sanatorio Finochietto, Boulogne Sur Mer 972
ART = timezone(timedelta(hours=-3))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def load_env(path=".env"):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if v.strip() and not os.environ.get(k.strip()):
                    os.environ[k.strip()] = v.strip()


def env(name, default=None):
    value = os.environ.get(name, default)
    if value is None or value == "":
        sys.exit(f"Falta la variable {name} (cargala en .env o como secret de GitHub)")
    return value


# ---------- Telegram ----------

def telegram(method, payload=None):
    url = f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/{method}"
    data = urllib.parse.urlencode(payload).encode() if payload else None
    with urllib.request.urlopen(url, data=data, timeout=30) as r:
        return json.load(r)


def send(text):
    telegram("sendMessage", {"chat_id": env("TELEGRAM_CHAT_ID"), "text": text,
                             "disable_web_page_preview": "true"})


# ---------- Portal ----------

class Portal:
    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, path, body=None, headers=None):
        h = {"User-Agent": UA, **(headers or {})}
        req = urllib.request.Request(BASE + path, data=body, headers=h)
        with self.opener.open(req, timeout=60) as r:
            return r.read().decode("utf-8", "replace")

    def login(self):
        self.request("portal/authentication/login")
        body = json.dumps({"username": env("PORTAL_EMAIL"),
                           "password": env("PORTAL_PASSWORD")}).encode()
        resp = json.loads(self.request("portal/authentication/login", body,
                                       {"Content-Type": "application/json"}))
        if resp.get("result", {}).get("code") != "SUCCESS":
            msgs = "; ".join(m.get("description") or m.get("code") for m in resp.get("messages", []))
            raise RuntimeError(f"Login fallido: {msgs or resp}")
        # Abre el asistente de turnos como lo hace el portal, para inicializar la sesión.
        self.request("portal/embed/newBooking")
        self.request("appoint/findAvailability/wizard")

    def search(self):
        """Equivale a elegir el profesional + 'Lo antes posible' + 'Ver agenda'."""
        params = {
            "attentionType": "P", "searchType": "Area",
            "jsonComponentCodes": json.dumps([COMPONENT_CODE]),
            "areaName": urllib.parse.quote(AREA_NAME), "areaCode": AREA_CODE,
            "branchId": BRANCH_ID, "cityId": "", "startDate": "", "daysSelected": "",
            "timeFrom": "", "timeTo": "", "resultOrder": "dateOrder",
            "followedBookingsCount": "1", "checkAreaProfile": "false", "timeZoneId": "",
            "authorizationCode": "", "isMultipleComponent": "false", "programId": "",
            "_": str(int(time.time() * 1000)),
        }
        text = self.request("appoint/findAvailability/wizard/doSearch?" + urllib.parse.urlencode(params),
                            headers={"X-Requested-With": "XMLHttpRequest"})
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError("La búsqueda no devolvió JSON (¿sesión vencida o cambió el portal?)")
        slots = []
        for key, value in data.items():
            if key.startswith("schedulesDay") and not key.startswith("schedulesDayGrouped") and value:
                for s in value:
                    day = datetime.fromtimestamp(s["date"] / 1000, ART)
                    start = day.replace(hour=0, minute=0, second=0) + timedelta(seconds=s["timeInSeconds"])
                    slots.append(start)
        return sorted(set(slots))


# ---------- Main ----------

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def fmt(dt):
    return f"{DIAS[dt.weekday()].capitalize()} {dt:%d/%m} {dt:%H:%M}"


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_state(path, keys):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(keys), f)


def main():
    load_env()

    if "--chat-id" in sys.argv:
        updates = telegram("getUpdates").get("result", [])
        chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
                 for u in updates if "message" in u}
        print(chats or "Nadie le escribió al bot todavía: mandale un mensaje y volvé a correr esto.")
        return
    if "--test" in sys.argv:
        send("✅ Prueba: el chequeo de turnos de Fugazzotto puede enviarte mensajes.")
        print("Mensaje de prueba enviado.")
        return

    days_ahead = int(os.environ.get("DAYS_AHEAD") or 7)
    state_file = os.environ.get("STATE_FILE") or "state.json"

    portal = Portal()
    portal.login()
    slots = portal.search()

    now = datetime.now(ART)
    limit = now + timedelta(days=days_ahead)
    in_window = [s for s in slots if now <= s <= limit]
    first = fmt(slots[0]) if slots else "ninguno"
    print(f"{now:%Y-%m-%d %H:%M} ART | primer turno: {first} | dentro de {days_ahead} días: {len(in_window)}")

    keys = {s.isoformat() for s in in_window}
    already = load_state(state_file)
    new = sorted(s for s in in_window if s.isoformat() not in already)
    if new:
        lines = "\n".join(f"• {fmt(s)}" for s in new[:20])
        more = f"\n(+{len(new) - 20} más)" if len(new) > 20 else ""
        send("🩺 Hay turnos nuevos con Fugazzotto (Gastroenterología, Finochietto):\n"
             f"{lines}{more}\n\nReservá en {BASE}portal/home")
        print(f"Aviso enviado: {len(new)} turno(s) nuevo(s).")
    # Guardamos solo los que siguen disponibles, así si uno se libera de nuevo vuelve a avisar.
    save_state(state_file, keys)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # el error queda en el log y falla el job
        print(f"ERROR: {e}", file=sys.stderr)
        raise
