"""Chequea turnos de un profesional en el portal del Sanatorio Finochietto,
avisa por Telegram si hay alguno dentro de los próximos DAYS_AHEAD días y
ofrece reservarlo con un botón.

Solo usa la biblioteca estándar de Python. Variables de entorno (o archivo .env):
  PORTAL_EMAIL, PORTAL_PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  MAIN_DOCTOR    código|nombre|código de especialidad|id de sede   (el que se puede reservar)
  SECOND_DOCTOR  opcional: código|nombre|código de especialidad|id de sede|fecha límite ISO
                 (solo avisa turnos anteriores a esa fecha; llegada la fecha deja de chequearlo)
Los médicos van como secrets para que no queden a la vista en el repositorio ni en los logs.
Opcionales:
  DAYS_AHEAD (7)       ventana de días a mirar
  STATE_FILE           (state.json)
  RUN_MINUTES (0)      cuánto dura la corrida; 0 = un solo chequeo
  CHECK_EVERY_MINUTES (15)  cada cuánto chequear dentro de la corrida (entre chequeos atiende Telegram)
  DRY_RUN (0)          1 = hace todo menos la confirmación final de la reserva

Uso:
  python check_turnos.py                       # chequeo normal
  python check_turnos.py --chat-id             # muestra el chat_id de quien le escribió al bot
  python check_turnos.py --test                # manda un mensaje de prueba por Telegram
  python check_turnos.py --book 13/01/27 37200 # reserva ese turno (fecha dd/mm/aa, segundos desde 00:00)
"""
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

BASE = "https://turnos.sanatoriofinochietto.com/"
ART = timezone(timedelta(hours=-3))
UA = "turnos-fino/1.0 (+https://github.com/gabolcx/turnos-fino)"
XHR = {"X-Requested-With": "XMLHttpRequest"}
WORKFLOW_FILE = "turnos.yml"
QUIET_FROM, QUIET_TO = 1, 8  # sin chequeos de 01:00 a 08:00 ART


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


def doctor(var):
    """Lee MAIN_DOCTOR / SECOND_DOCTOR: código|nombre|especialidad|sede[|fecha límite]."""
    raw = os.environ.get(var, "").strip()
    if not raw:
        return None
    parts = [x.strip() for x in raw.split("|")]
    d = {"area_code": parts[0], "area_name": parts[1], "component_code": parts[2], "branch_id": parts[3]}
    if len(parts) > 4:
        d["deadline"] = datetime.fromisoformat(parts[4]).replace(tzinfo=ART)
    return d


def env(name, default=None):
    value = os.environ.get(name, default)
    if value is None or value == "":
        sys.exit(f"Falta la variable {name} (cargala en .env o como secret de GitHub)")
    return value


# ---------- Telegram ----------

def telegram(method, payload=None, timeout=30):
    url = f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/{method}"
    data = None
    if payload is not None:
        payload = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in payload.items()}
        data = urllib.parse.urlencode(payload).encode()
    with urllib.request.urlopen(url, data=data, timeout=timeout) as r:
        return json.load(r)


def send(text, buttons=None):
    payload = {"chat_id": env("TELEGRAM_CHAT_ID"), "text": text, "disable_web_page_preview": "true"}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    return telegram("sendMessage", payload)["result"]["message_id"]


def remove_buttons(message_id):
    try:
        telegram("editMessageReplyMarkup", {"chat_id": env("TELEGRAM_CHAT_ID"), "message_id": message_id,
                                            "reply_markup": {"inline_keyboard": []}})
    except Exception:
        pass  # el mensaje pudo haber sido borrado o ya no tener botones


# ---------- Portal ----------

class BookingError(Exception):
    pass


class _Inputs(HTMLParser):
    """Junta los <input id=...> del asistente (datos del paciente precargados)."""
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("id") and not self.values.get(a["id"]):
            self.values[a["id"]] = a.get("value") or ""


class Portal:
    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.wizard = {}

    def request(self, path, body=None, headers=None):
        h = {"User-Agent": UA, **(headers or {})}
        req = urllib.request.Request(BASE + path, data=body, headers=h)
        with self.opener.open(req, timeout=60) as r:
            return r.read().decode("utf-8", "replace")

    def get_json(self, path, params=None, body=None, headers=None):
        if params is not None:
            path += "?" + urllib.parse.urlencode({**params, "_": int(time.time() * 1000)})
        text = self.request(path, body, {**XHR, **(headers or {})})
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError(f"{path.split('?')[0]} no devolvió JSON (¿sesión vencida o cambió el portal?)")

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
        parser = _Inputs()
        parser.feed(self.request("appoint/findAvailability/wizard"))
        self.wizard = parser.values

    def search(self, area_code, area_name, component_code, branch_id, **_):
        """Equivale a elegir el profesional + 'Lo antes posible' + 'Ver agenda'."""
        data = self.get_json("appoint/findAvailability/wizard/doSearch", {
            "attentionType": "P", "searchType": "Area",
            "jsonComponentCodes": json.dumps([component_code]),
            "areaName": urllib.parse.quote(area_name), "areaCode": area_code,
            "branchId": branch_id, "cityId": "", "startDate": "", "daysSelected": "",
            "timeFrom": "", "timeTo": "", "resultOrder": "dateOrder",
            "followedBookingsCount": "1", "checkAreaProfile": "false", "timeZoneId": "",
            "authorizationCode": "", "isMultipleComponent": "false", "programId": "",
        })
        slots = {}
        for key, value in data.items():
            if key.startswith("schedulesDay") and not key.startswith("schedulesDayGrouped") and value:
                for s in value:
                    day = datetime.fromtimestamp(s["date"] / 1000, ART)
                    start = day.replace(hour=0, minute=0, second=0) + timedelta(seconds=s["timeInSeconds"])
                    slots[start] = {"start": start, "date": s["dateFormatted"], "secs": s["timeInSeconds"],
                                    "specialty": s.get("component", {}).get("description", ""),
                                    "componentId": s["componentId"], "areaId": s["areaId"],
                                    "branchId": s["branchId"]}
        return [slots[k] for k in sorted(slots)]

    def release(self):
        try:
            self.request("appoint/prebooking/removePrebookingSchedules?_=1", headers=XHR)
        except Exception:
            pass

    def book(self, doc, date, secs, dry_run=False):
        """Reserva el turno date (dd/mm/aa) + secs. Sigue los mismos pasos que el asistente web."""
        slot = next((s for s in self.search(**doc) if s["date"] == date and s["secs"] == int(secs)), None)
        if not slot:
            raise BookingError("ese turno ya no está disponible")

        v = self.get_json("appoint/parentBooking/validateBookingDate", {
            "bookingComponentId": slot["componentId"], "startDateStr": date, "bookingTime": secs,
            "branchId": slot["branchId"], "areaId": slot["areaId"], "attentionType": "P"})
        code = v.get("result", {}).get("code")
        if code not in ("SUCCESS", "WARNING", "WARNSAMEDAYSCHEDULE"):
            raise BookingError(v["result"].get("description") or f"el portal no lo permite ({code})")

        try:
            pre = self.get_json("appoint/prebooking/addPrebookingSchedule", {
                "timeInSeconds": secs, "bookingComponentId": slot["componentId"], "startDateStr": date,
                "areaId": slot["areaId"], "followedBookingsCount": "", "isReassign": "false"})
            if pre.get("code") != "SUCCESS":
                raise BookingError(pre.get("description") or "no se pudo retener el turno")

            w = self.wizard
            has_payment = w.get("hasPayment") == "true"
            booking = {"bookingComponentId": slot["componentId"], "areaId": slot["areaId"], "areaCode": None,
                       "dateFormatted": date, "timeInSeconds": int(secs), "timeInBetween": "",
                       "hasPayment": has_payment, "paymentAmount": None, "attachmentUrl": None,
                       "attachmentUrls": None, "cityId": None, "address": None, "additionalData": None,
                       "neighborhood": None}
            form = {"branchId": slot["branchId"], "customerIdentification": w.get("bookingCustomerIdentification", ""),
                    "customerIdentificationType": w.get("bookingCustomerIdentificationType", ""),
                    "bookingsDataJson": json.dumps([booking]), "isReassignBooking": "false",
                    "reassignOriginalBookingId": "", "cancelationReasonId": "", "cancelationComment": "",
                    "attentionType": "P", "followedBookingsCount": "1", "labTags": "", "programId": "",
                    "isResearchByParticular": w.get("researchByParticular", "")}
            a = self.get_json("appoint/booking/wizard/assign", body=urllib.parse.urlencode(form).encode(),
                              headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
            if a.get("result", {}).get("code") != "SUCCESS":
                raise BookingError(a.get("result", {}).get("description") or "el portal rechazó el turno")
            if a.get("step") != "bookingConfirm":
                raise BookingError(f"el portal pide un paso extra ({a.get('step')}); reservalo desde la web")
            complete = json.loads(a["completeBookingsDataJson"])
            if dry_run:
                self.release()
                return "SIMULACIÓN: llegó hasta la confirmación final y no reservó."

            bookings = [{"bookingComponentId": c["bookingComponent"]["id"],
                         "areaId": (c.get("area") or {}).get("id"), "areaCode": None,
                         "dateFormatted": c["bookingDateStr"], "timeInSeconds": c["bookingTime"],
                         "timeInBetween": None, "hasPayment": c.get("hasMercadoPagoPayment"),
                         "paymentAmount": c.get("amount"), "attachmentUrl": c.get("attachmentUrl"),
                         "attachmentUrls": c.get("attachmentUrls"), "cityId": None, "address": None,
                         "additionalData": None, "neighborhood": None} for c in complete]
            country = w.get("countryCode") or "ar"
            cell = w.get("bookingCellPhone", "")
            cell_digits = re.sub(r"\D", "", cell[3:] if cell.startswith("+54") else cell)
            land = re.sub(r"\D", "", w.get("bookingPhone2", "").removeprefix("+54"))
            email = w.get("bookingEmail", "")
            if not cell_digits or not email:
                raise BookingError("falta celular o email en tus datos del portal")
            dto = {"bookingsDataJson": json.dumps(bookings), "branchId": str(slot["branchId"]),
                   "customerIdentification": w.get("bookingCustomerIdentification", ""),
                   "customerIdentificationType": w.get("bookingCustomerIdentificationType", ""),
                   "isReassign": "false", "presential": True, "followedBookingsCount": "1",
                   "phoneCountryCode": country,
                   "cellPhone": {"id": None, "phoneNumber": cell_digits, "countryCode": country, "dialCode": "+54"},
                   "landPhone": ({"id": None, "phoneNumber": land, "countryCode": country, "dialCode": "+54"}
                                 if land else None),
                   "email": email, "overBooking": False, "hasPayment": has_payment,
                   "isWizard": True, "programId": w.get("bookingProgramId", "")}
            r = self.get_json("appoint/parentBooking/postBooking", body=json.dumps(dto).encode(),
                              headers={"Content-Type": "application/json"})
        except Exception:
            self.release()
            raise
        results = r.get("bookingResults") or []
        failed = [b for b in results if b.get("result", {}).get("code") != "SUCCESS"]
        if r.get("result", {}).get("code") != "SUCCESS" or failed:
            msgs = [m.get("description") or m.get("code") for b in (failed or [r]) for m in b.get("messages", [])]
            raise BookingError("; ".join(filter(None, msgs)) or r.get("result", {}).get("description") or "falló la reserva")
        if r.get("hasPayment") or r.get("showPaymentMessage"):
            return "Reservado, pero el portal indica un pago pendiente: revisalo en la web."
        return "Reservado."


# ---------- Estado ----------

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def fmt(dt):
    return f"{DIAS[dt.weekday()].capitalize()} {dt:%d/%m} {dt:%H:%M}"


def fmt_short(dt):
    return f"{DIAS[dt.weekday()][:3].capitalize()} {dt:%d/%m %H:%M}"


def slot_start(date, secs):
    return datetime.strptime(date, "%d/%m/%y").replace(tzinfo=ART) + timedelta(seconds=int(secs))


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if isinstance(data, list):  # formato viejo: solo la lista de turnos avisados
        data = {"notified": data}
    data.setdefault("notified", [])
    data.setdefault("offers", [])  # [{"message_id": int, "until": iso}]
    return data


def save_state(path, state):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


# ---------- Respuestas de Telegram ----------

def handle_updates(state, portal_factory, dry_run, wait_seconds=0):
    """Procesa los botones que tocaste en Telegram."""
    doc = doctor("MAIN_DOCTOR")
    chat_id = str(env("TELEGRAM_CHAT_ID"))
    params = {"timeout": wait_seconds}
    if state.get("tg_offset"):
        params["offset"] = state["tg_offset"]
    try:
        updates = telegram("getUpdates", params, timeout=wait_seconds + 30).get("result", [])
    except Exception as e:  # corte pasajero de Telegram: se reintenta en la próxima vuelta
        print(f"Aviso: Telegram no respondió ({e})")
        time.sleep(5)
        return
    for u in updates:
        state["tg_offset"] = u["update_id"] + 1
        q = u.get("callback_query")
        if not q:
            continue
        msg = q.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != chat_id:
            telegram("answerCallbackQuery", {"callback_query_id": q["id"], "text": "No autorizado"})
            continue
        parts = (q.get("data") or "").split("|")
        if parts[0] in ("pause", "continue"):
            remove_buttons(msg.get("message_id"))
            state.pop("question", None)
            if parts[0] == "continue":
                telegram("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Sigo buscando"})
                send("▶️ Sigo buscando turnos cada 15 minutos.")
                continue
            telegram("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Pausando…"})
            try:
                pause_workflow()
                state["paused"] = True
                send("⏸ Chequeo pausado en GitHub. Para retomarlo: pestaña Actions del repo → "
                     "\"Chequear turnos\" → Enable workflow.")
                print("Workflow pausado.")
            except Exception as e:
                send(f"❌ No pude pausar el chequeo: {e}\nPausalo desde la pestaña Actions del repo.")
                print(f"No se pudo pausar: {e}")
            continue
        if parts[0] == "no":
            telegram("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Ok, no reservo nada"})
            remove_buttons(msg.get("message_id"))
            state["offers"] = [o for o in state["offers"] if o["message_id"] != msg.get("message_id")]
            continue
        if parts[0] != "b" or len(parts) != 3:
            continue
        date, secs = parts[1], parts[2]
        when = fmt(slot_start(date, secs))
        telegram("answerCallbackQuery", {"callback_query_id": q["id"], "text": f"Reservando {when}…"})
        print(f"Pedido de reserva: {when}")
        try:
            portal = portal_factory()
            result = portal.book(doc, date, secs, dry_run=dry_run)
        except BookingError as e:
            send(f"❌ No pude reservar {when}: {e}.")
            print(f"Reserva fallida: {e}")
            continue
        except Exception as e:
            send(f"❌ Error inesperado al reservar {when}: {e}\nProbá desde {BASE}portal/home")
            print(f"Reserva con error: {e}")
            continue
        send(f"✅ {result}\n{when} con {doc['area_name']}, Sanatorio Finochietto.\n"
             f"Verificalo en {BASE}portal/home (sección Agenda).")
        print(f"Resultado: {result}")
        if not dry_run:
            for o in state["offers"]:
                remove_buttons(o["message_id"])
            state["offers"] = []
        if state.get("question"):
            remove_buttons(state["question"])
        state["question"] = send("¿Querés pausar el chequeo o que siga buscando turnos?", [
            [{"text": "⏸ Pausar chequeo", "callback_data": "pause"}],
            [{"text": "▶️ Seguir buscando", "callback_data": "continue"}]])


def pause_workflow():
    """Desactiva este workflow en GitHub (necesita GITHUB_TOKEN con permiso actions: write)."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        raise RuntimeError("solo se puede pausar cuando corre en GitHub")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/disable", method="PUT",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": UA})
    urllib.request.urlopen(req, timeout=30).close()


def check_second(portal, state, now):
    """Avisa si hay un turno con SECOND_DOCTOR antes de su fecha límite; después no hace nada."""
    doc = doctor("SECOND_DOCTOR")
    if not doc:
        return
    deadline = doc["deadline"]
    if now >= deadline:
        if not state.get("second_done"):
            send(f"⏹ Terminó el chequeo de {doc['area_name']}: llegó el {fmt(deadline)}. "
                 "El otro chequeo sigue igual.")
            state["second_done"] = True
            print("Médico 2: llegó la fecha límite, chequeo terminado.")
        return
    slots = portal.search(**doc)
    earlier = [s for s in slots if now <= s["start"] < deadline]
    print(f"Médico 2 | turnos antes de la fecha límite: {len(earlier)}")
    already = set(state.get("second_notified", []))
    new = [s for s in earlier if s["start"].isoformat() not in already]
    if new:
        lines = "\n".join(f"• {fmt(s['start'])}" for s in earlier[:20])
        send(f"🩺 Hay turnos con {doc['area_name']} ({earlier[0]['specialty']}) antes de tu turno del "
             f"{fmt(deadline)}:\n{lines}\n\nSi te sirve, cambialo en {BASE}portal/home")
        print(f"Aviso médico 2 enviado: {len(new)} turno(s) nuevo(s).")
    state["second_notified"] = sorted(s["start"].isoformat() for s in earlier)


def quiet_hours(now):
    """Entre la 01:00 y las 08:00 (hora argentina) no se chequea."""
    return QUIET_FROM <= now.hour < QUIET_TO


# ---------- Main ----------

def main():
    load_env()

    if "--chat-id" in sys.argv:
        updates = telegram("getUpdates").get("result", [])
        chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
                 for u in updates if "message" in u}
        print(chats or "Nadie le escribió al bot todavía: mandale un mensaje y volvé a correr esto.")
        return
    if "--test" in sys.argv:
        send("✅ Prueba: el chequeo de turnos puede enviarte mensajes.")
        print("Mensaje de prueba enviado.")
        return

    dry_run = os.environ.get("DRY_RUN") == "1"

    def new_portal():
        p = Portal()
        p.login()
        return p

    if "--book" in sys.argv:
        i = sys.argv.index("--book")
        print(new_portal().book(doctor("MAIN_DOCTOR"), sys.argv[i + 1], sys.argv[i + 2], dry_run=dry_run))
        return

    started = time.time()
    days_ahead = int(os.environ.get("DAYS_AHEAD") or 7)
    run_for = float(os.environ.get("RUN_MINUTES") or 0) * 60
    every = float(os.environ.get("CHECK_EVERY_MINUTES") or 15) * 60
    state_file = os.environ.get("STATE_FILE") or "state.json"
    scheduled = os.environ.get("GITHUB_EVENT_NAME") == "schedule"
    state = load_state(state_file)
    state.pop("paused", None)

    # Corre durante RUN_MINUTES: chequea cada CHECK_EVERY_MINUTES y, entre chequeo y chequeo,
    # atiende los botones de Telegram. Así no depende de que GitHub lance cada corrida a horario.
    failures = 0
    try:
        while True:
            if scheduled and quiet_hours(datetime.now(ART)):
                print("Horario de pausa (01:00 a 08:00 ART): no se chequea.")
                break
            cycle_start = time.time()
            try:
                check_once(state, new_portal, dry_run, days_ahead)
                failures = 0
            except Exception as e:  # portal o Telegram caídos: se reintenta en el próximo ciclo
                failures += 1
                print(f"ERROR en el chequeo ({failures} seguido/s): {e}", file=sys.stderr)
                if failures >= 4:
                    raise
            save_state(state_file, state)

            next_check = cycle_start + (every if failures == 0 else 60)
            if state.get("paused") or next_check > started + run_for:
                break
            while time.time() < next_check and not state.get("paused"):
                handle_updates(state, new_portal, dry_run,
                               wait_seconds=max(1, min(50, int(next_check - time.time()))))
                save_state(state_file, state)
            if state.get("paused"):
                break
    finally:
        save_state(state_file, state)


def check_once(state, new_portal, dry_run, days_ahead):
    # 1) Botones que tocaste desde el último chequeo.
    handle_updates(state, new_portal, dry_run)

    # 2) Buscar turnos.
    doc = doctor("MAIN_DOCTOR")
    if not doc:
        sys.exit("Falta la variable MAIN_DOCTOR (cargala en .env o como secret de GitHub)")
    portal = new_portal()
    slots = portal.search(**doc)
    now = datetime.now(ART)
    limit = now + timedelta(days=days_ahead)
    in_window = [s for s in slots if now <= s["start"] <= limit]
    print(f"{now:%Y-%m-%d %H:%M} ART | médico 1 | turnos dentro de {days_ahead} días: {len(in_window)}",
          flush=True)

    already = set(state["notified"])
    new = [s for s in in_window if s["start"].isoformat() not in already]
    if new:
        lines = "\n".join(f"• {fmt(s['start'])}" for s in in_window[:20])
        more = f"\n(+{len(in_window) - 20} más)" if len(in_window) > 20 else ""
        buttons = [[{"text": f"Reservar {fmt_short(s['start'])}", "callback_data": f"b|{s['date']}|{s['secs']}"}]
                   for s in in_window[:10]]
        buttons.append([{"text": "No, gracias", "callback_data": "no"}])
        msg_id = send(f"🩺 Hay turnos con {doc['area_name']} ({in_window[0]['specialty']}, Finochietto) "
                      f"en los próximos {days_ahead} días:\n{lines}{more}\n\n"
                      "¿Querés que reserve alguno? Tocá el horario.", buttons)
        state["offers"].append({"message_id": msg_id, "until": limit.isoformat()})
        print(f"Aviso enviado: {len(new)} turno(s) nuevo(s).")
    # Guardamos solo los que siguen disponibles, así si uno se libera de nuevo vuelve a avisar.
    state["notified"] = sorted(s["start"].isoformat() for s in in_window)

    # 2b) Segundo médico, con fecha límite. Si falla, no frena el chequeo principal.
    try:
        check_second(portal, state, now)
    except Exception as e:
        print(f"ERROR chequeando médico 2: {e}", file=sys.stderr)

    # 3) Ofertas viejas: sacarles los botones cuando ya no hay turnos en la ventana.
    if not in_window:
        for o in state["offers"]:
            remove_buttons(o["message_id"])
        state["offers"] = []

if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # el error queda en el log y falla el job
        print(f"ERROR: {e}", file=sys.stderr)
        raise
