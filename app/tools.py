# Инструменты помощника: напоминания, привычки, поиск по прошлым разговорам, «запомни», распознавание речи,
# уведомления Windows. Модель вызывает их сама (tools Ollama), окно — через /api/… (server.py).
# Данные — в data/: reminders.json, habits.json, archive.jsonl. Только стандартная библиотека Python,
# кроме распознавания речи (onnx-asr и numpy — есть в runtime/pyenv, из него сервер и запускается).

import base64
import datetime
import io
import json
import math
import os
import re
import subprocess
import threading
import time
import wave
from pathlib import Path

DATA = None
read_json = write_json = remember_fact = None
lock = threading.RLock()


def init(data_dir, reader, writer, remember):
    """Подключить к серверу: папка данных, чтение и запись JSON, добавление факта в память."""
    global DATA, read_json, write_json, remember_fact
    DATA, read_json, write_json, remember_fact = Path(data_dir), reader, writer, remember
    threading.Thread(target=scheduler, daemon=True).start()


def today():
    return datetime.date.today()


def now():
    return datetime.datetime.now().replace(second=0, microsecond=0)


RU_DAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
RU_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def human(dt):
    """27 октября, 09:00 (сегодня / завтра — словами)."""
    d = dt.date()
    day = "сегодня" if d == today() else "завтра" if d == today() + datetime.timedelta(days=1) else f"{dt.day} {RU_MONTHS[dt.month - 1]}"
    return f"{day} в {dt:%H:%M}"


# ---------- события для окна (сработавшие напоминания, вечерний вопрос о привычках) ----------
events, ev_n = [], 0


def add_event(kind, text):
    global ev_n
    with lock:
        ev_n += 1
        events.append({"n": ev_n, "kind": kind, "text": text})
        del events[:-50]


def get_events():
    with lock:
        return {"n": ev_n, "items": list(events)}


def toast(title, text):
    """Уведомление Windows (видно, даже если окно свёрнуто). PowerShell + WinRT, без сторонних модулей."""
    esc = lambda s: (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "''"))
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml('<toast><visual><binding template=\"ToastGeneric\"><text>{esc(title)}</text><text>{esc(text)}</text></binding></visual>"
        "<audio src=\"ms-winsoundevent:Notification.Reminder\"/></toast>');"
        "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))"
    )
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        pass


# ---------- напоминания: data/reminders.json [{id, text, when "ГГГГ-ММ-ДДTЧЧ:ММ", fired}] ----------
def reminders():
    return read_json(DATA / "reminders.json", [])


def parse_when(when):
    when = str(when or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.datetime.strptime(when, fmt)
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", when)   # только время — сегодня, а если уже прошло — завтра
    if m:
        dt = now().replace(hour=int(m[1]), minute=int(m[2]))
        return dt if dt > now() else dt + datetime.timedelta(days=1)
    return None


def add_reminder(text, when=None, in_minutes=None):
    text = str(text or "").strip()[:200]
    if not text:
        return "Не понял, о чём напомнить.", None
    if in_minutes not in (None, ""):
        try:
            dt = now() + datetime.timedelta(minutes=max(1, int(float(in_minutes))))
        except ValueError:
            dt = None
    else:
        dt = parse_when(when)
    if not dt:
        return "Не понял, когда напомнить: нужна дата и время или через сколько минут.", None
    if dt <= now() - datetime.timedelta(minutes=1):
        return f"Время {human(dt)} уже прошло — уточни у пользователя.", None
    with lock:
        items = reminders()
        rid = max([r["id"] for r in items], default=0) + 1
        items.append({"id": rid, "text": text, "when": dt.strftime("%Y-%m-%dT%H:%M"), "fired": False})
        write_json(DATA / "reminders.json", items)
    return f"Напоминание №{rid} заведено: «{text}» — {human(dt)}.", f"⏰ Напомню {human(dt)}: {text}"


def upcoming():
    return sorted([r for r in reminders() if not r.get("fired")], key=lambda r: r["when"])


def list_reminders():
    items = upcoming()
    if not items:
        return "Напоминаний нет.", None
    return "Напоминания:\n" + "\n".join(f"№{r['id']}: {human(parse_when(r['when']))} — {r['text']}" for r in items), None


def cancel_reminder(id=None, text=None):
    with lock:
        items = reminders()
        keep, gone = [], []
        for r in items:
            hit = not r.get("fired") and ((id not in (None, "") and str(r["id"]) == str(id)) or
                                          (text and str(text).lower() in r["text"].lower()))
            (gone if hit else keep).append(r)
        if not gone:
            return "Такого напоминания нет.", None
        write_json(DATA / "reminders.json", keep)
    names = ", ".join(r["text"] for r in gone)
    return f"Отменено: {names}.", f"⏰ Отменено: {names}"


def remove_reminder(rid):
    with lock:
        write_json(DATA / "reminders.json", [r for r in reminders() if r["id"] != rid])


# ---------- привычки: data/habits.json {"habits": [{id, name, days: ["ГГГГ-ММ-ДД"]}], "check": "21:00", "checked": дата} ----------
def habits():
    h = read_json(DATA / "habits.json", {})
    h.setdefault("habits", [])
    h.setdefault("check", "21:00")
    h.setdefault("checked", "")
    return h


def norm(s):
    return re.sub(r"[^\w ]+", "", str(s).lower().replace("ё", "е")).strip()


def find_habit(h, name):
    n = norm(name)
    for x in h["habits"]:
        xn = norm(x["name"])
        if xn == n or (len(n) >= 4 and (xn.startswith(n[:5]) or n.startswith(xn[:5]))):
            return x
    return None


def streak(days):
    """Серия подряд: до сегодня включительно, или до вчера, если сегодня ещё не отмечено."""
    s, d = set(days), today()
    if d.isoformat() not in s:
        d -= datetime.timedelta(days=1)
    n = 0
    while d.isoformat() in s:
        n += 1
        d -= datetime.timedelta(days=1)
    return n


def add_habit(name):
    name = str(name or "").strip()[:60]
    if not name:
        return "Нужно название привычки.", None
    with lock:
        h = habits()
        if find_habit(h, name):
            return f"Привычка «{name}» уже есть.", None
        h["habits"].append({"id": max([x["id"] for x in h["habits"]], default=0) + 1, "name": name, "days": []})
        write_json(DATA / "habits.json", h)
    return f"Привычка «{name}» добавлена.", f"📅 Новая привычка: {name}"


def mark_habit(habit, day="сегодня", done=True):
    day = str(day or "сегодня").lower()
    d = today() - datetime.timedelta(days=1) if "вчера" in day else today()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        d = datetime.date.fromisoformat(day)
    with lock:
        h = habits()
        x = find_habit(h, habit)
        created = False
        if not x:
            name = str(habit or "").strip()[:60]
            if not name:
                return "Нужно название привычки.", None
            x = {"id": max([y["id"] for y in h["habits"]], default=0) + 1, "name": name, "days": []}
            h["habits"].append(x)
            created = True
        days = set(x["days"])
        (days.add if done else days.discard)(d.isoformat())
        x["days"] = sorted(days)[-400:]
        write_json(DATA / "habits.json", h)
    when = "вчера" if d != today() else "сегодня"
    s = streak(x["days"])
    note = ("новая привычка, " if created else "") + f"серия {s} дн."
    if done:
        return f"Отмечено: «{x['name']}» {when} ({note}).", f"📅 {x['name']} — {when} ✓, серия {s} дн."
    return f"Отметка «{x['name']}» за {when} снята.", f"📅 {x['name']} — {when} снято"


def habits_status():
    h = habits()
    if not h["habits"]:
        return "Привычек пока нет.", None
    t = today().isoformat()
    return "Привычки:\n" + "\n".join(
        f"{x['name']}: сегодня {'✓' if t in x['days'] else 'ещё нет'}, серия {streak(x['days'])} дн., всего {len(x['days'])} раз"
        for x in h["habits"]), None


def habits_prompt():
    h = habits()
    if not h["habits"]:
        return ""
    t = today().isoformat()
    lines = "; ".join(f"{x['name']} (сегодня {'✓' if t in x['days'] else 'нет'}, серия {streak(x['days'])})" for x in h["habits"])
    return "\n\nПривычки собеседника, которые вы отслеживаете: " + lines + "."


def reminders_prompt():
    items = upcoming()[:8]
    if not items:
        return "\n\nЗаведённых напоминаний сейчас нет."
    return "\n\nЗаведённые напоминания: " + "; ".join(f"{human(parse_when(r['when']))} — {r['text']}" for r in items) + "."


def remove_habit(hid):
    with lock:
        h = habits()
        h["habits"] = [x for x in h["habits"] if x["id"] != hid]
        write_json(DATA / "habits.json", h)


def toggle_day(hid, day):
    with lock:
        h = habits()
        for x in h["habits"]:
            if x["id"] == hid:
                days = set(x["days"])
                days.symmetric_difference_update({day})
                x["days"] = sorted(days)
        write_json(DATA / "habits.json", h)


def set_check_time(value):
    with lock:
        h = habits()
        if re.fullmatch(r"\d{1,2}:\d{2}", str(value)) or value == "":
            h["check"] = value
        write_json(DATA / "habits.json", h)


# ---------- проверка раз в 15 секунд: напоминания и вечерний вопрос о привычках ----------
def scheduler():
    while True:
        time.sleep(15)
        try:
            with lock:
                items, fired = reminders(), []
                for r in items:
                    dt = parse_when(r["when"])
                    if not r.get("fired") and dt and dt <= datetime.datetime.now():
                        r["fired"] = True
                        fired.append(r)
                if fired:
                    # сработавшие храним сутки (видно в окне), потом удаляем
                    cut = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
                    write_json(DATA / "reminders.json", [r for r in items if not r.get("fired") or r["when"] >= cut])
            for r in fired:
                add_event("reminder", r["text"])
                toast("⏰ Напоминание", r["text"])
            h = habits()
            if h["habits"] and h["check"] and h["checked"] != today().isoformat() and f"{datetime.datetime.now():%H:%M}" >= h["check"].zfill(5):
                t = today().isoformat()
                left = [x["name"] for x in h["habits"] if t not in x["days"]]
                with lock:
                    h2 = habits()
                    h2["checked"] = t
                    write_json(DATA / "habits.json", h2)
                if left:
                    text = "Как сегодня с привычками? Ещё не отмечено: " + ", ".join(left) + "."
                    add_event("habits", text)
                    toast("📅 Привычки", text)
        except Exception as e:  # одна ошибка не останавливает проверку
            print(f"планировщик: {e}", flush=True)


# ---------- архив разговоров и поиск: data/archive.jsonl, по строке на сообщение ----------
def archive(role, text):
    if not str(text).strip():
        return
    with lock:
        with open(DATA / "archive.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": f"{datetime.datetime.now():%Y-%m-%d %H:%M}", "role": role, "text": str(text)[:4000]},
                               ensure_ascii=False) + "\n")


def archive_bootstrap():
    """Первый запуск: перенести в архив уже имеющийся разговор."""
    if (DATA / "archive.jsonl").exists():
        return
    for m in read_json(DATA / "history.json", []):
        if m.get("role") in ("user", "assistant"):
            with open(DATA / "archive.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": "раньше", "role": m["role"], "text": str(m.get("content", ""))[:4000]}, ensure_ascii=False) + "\n")


def words(text):
    # грубая основа слова для русского: первые 6 букв (дома / домой / домашний → «домаш»…)
    return [w[:6] for w in re.findall(r"[a-zа-яё0-9]{3,}", str(text).lower().replace("ё", "е"))]


def search_history(query, limit=6):
    """BM25 по сообщениям архива: что пользователь и помощник говорили раньше."""
    try:
        rows = [json.loads(l) for l in open(DATA / "archive.jsonl", encoding="utf-8") if l.strip()]
    except OSError:
        rows = []
    q = set(words(query))
    if not rows or not q:
        return "В прошлых разговорах ничего не найдено.", None
    docs = [words(r["text"]) for r in rows]
    avg = sum(len(d) for d in docs) / len(docs) or 1
    df = {w: sum(1 for d in docs if w in d) for w in q}
    scored = []
    for i, d in enumerate(docs):
        s = 0.0
        for w in q:
            tf = d.count(w)
            if tf:
                idf = math.log(1 + (len(docs) - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(d) / avg))
        if s > 0:
            scored.append((s, i))
    if not scored:
        return "В прошлых разговорах ничего не найдено.", None
    out = []
    for _, i in sorted(scored, reverse=True)[:limit]:
        r = rows[i]
        who = "Пользователь" if r["role"] == "user" else "Помощник"
        out.append(f"[{r['at']}] {who}: {r['text'][:400]}")
    return "Нашлось в прошлых разговорах:\n" + "\n".join(out), f"🔎 Поиск в прошлых разговорах: {query}"


# ---------- распознавание речи: GigaAM v3 (onnx-asr, процессор), модель — в runtime/hf ----------
_stt, _stt_lock = None, threading.Lock()


def stt(wav_bytes):
    global _stt
    with _stt_lock:
        if _stt is None:
            os.environ.setdefault("HF_HOME", str(Path(__file__).resolve().parent.parent / "runtime" / "hf"))
            import onnx_asr
            _stt = onnx_asr.load_model("gigaam-v3-e2e-ctc", quantization="int8")
        import numpy
        with wave.open(io.BytesIO(wav_bytes)) as w:
            rate, frames = w.getframerate(), w.readframes(w.getnframes())
        audio = numpy.frombuffer(frames, dtype=numpy.int16).astype(numpy.float32) / 32768.0
        return str(_stt.recognize(audio, sample_rate=rate)).strip()


def stt_b64(b64):
    return stt(base64.b64decode(b64))


# ---------- описание инструментов для модели и их вызов ----------
def fn(name, description, props, required):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": required}}}


TOOLS = [
    fn("add_reminder", "Завести напоминание. Для «через N минут/часов» — in_minutes (часы переведи в минуты), "
       "для конкретного времени — when.",
       {"text": {"type": "string", "description": "о чём напомнить, коротко"},
        "when": {"type": "string", "description": "дата и время ГГГГ-ММ-ДД ЧЧ:ММ"},
        "in_minutes": {"type": "integer", "description": "через сколько минут от текущего момента"}}, ["text"]),
    fn("list_reminders", "Показать будущие напоминания.", {}, []),
    fn("cancel_reminder", "Отменить напоминание по номеру или по словам из текста.",
       {"id": {"type": "integer"}, "text": {"type": "string"}}, []),
    fn("mark_habit", "Отметить, что пользователь выполнил привычку (бег, вода, чтение, зарядка…). Если такой привычки ещё нет — "
       "она появится. Название — короткое существительное: «Бег», «Чтение». Вызывай сразу, как пользователь сказал, что "
       "сделал, — не спрашивай разрешения.",
       {"habit": {"type": "string"}, "day": {"type": "string", "description": "сегодня, вчера или ГГГГ-ММ-ДД"},
        "done": {"type": "boolean", "description": "false — снять отметку"}}, ["habit"]),
    fn("add_habit", "Начать отслеживать новую привычку.", {"name": {"type": "string"}}, ["name"]),
    fn("habits_status", "Состояние привычек: что сделано сегодня, серии.", {}, []),
    fn("search_history", "Найти в прошлых разговорах, что обсуждали раньше (когда пользователь спрашивает о прошлом "
       "или ссылается на то, что уже рассказывал).", {"query": {"type": "string", "description": "ключевые слова"}}, ["query"]),
    fn("remember", "Запомнить факт о пользователе навсегда, когда он прямо просит запомнить.",
       {"text": {"type": "string", "description": "факт одной фразой в третьем лице: «Любит кофе без сахара»"}}, ["text"]),
]

TOOL_RULES = (
    "\n\nУ тебя есть инструменты: напоминания, привычки, поиск по прошлым разговорам, «запомнить». Пользуйся ими, когда "
    "нужно действие. Никогда не пиши «отметил», «добавляю», «записал», «напомню», если в этом ответе не вызвал инструмент. Просят напомнить — add_reminder; рассказывает, что "
    "сделал что-то полезное (спорт, бег, прогулка, зарядка, чтение, вода, медитация, учёба, ранний подъём…) или просит "
    "отметить — сразу, без вопросов, mark_habit с коротким названием («Чтение», «Бег»), даже если такой привычки ещё нет; спрашивает о прошлом — search_history; просит "
    "запомнить — remember; просит отменить напоминание — cancel_reminder. Спрашивает, какие есть напоминания или как дела с "
    "привычками, — отвечай только по списку напоминаний и привычек в последней служебной строке (или вызови list_reminders / habits_status), "
    "ничего не выдумывай. После инструмента ответь коротко и по-человечески."
)


def call(name, args):
    """Выполнить инструмент: (текст для модели, строка для окна или None)."""
    args = args if isinstance(args, dict) else {}
    try:
        if name == "add_reminder":
            return add_reminder(args.get("text"), args.get("when"), args.get("in_minutes"))
        if name == "list_reminders":
            return list_reminders()
        if name == "cancel_reminder":
            return cancel_reminder(args.get("id"), args.get("text"))
        if name == "mark_habit":
            return mark_habit(args.get("habit"), args.get("day") or "сегодня", args.get("done", True) is not False)
        if name == "add_habit":
            return add_habit(args.get("name"))
        if name == "habits_status":
            return habits_status()
        if name == "search_history":
            return search_history(args.get("query", ""))
        if name == "remember":
            text = str(args.get("text", "")).strip()
            if not text:
                return "Нечего запоминать.", None
            remember_fact(text)
            return f"Запомнено: {text}", f"📝 Запомнил: {text}"
    except Exception as e:
        return f"Инструмент {name} не сработал: {e}", None
    return f"Нет инструмента {name}.", None
