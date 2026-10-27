# Личный помощник: маленький сервер окна чата.
# Только стандартная библиотека Python. Отдаёт страницу (web/), пересылает разговор в Ollama (Qwen) потоком
# и озвучивает текст голосом Piper. Слушает только 127.0.0.1 — снаружи ПК недоступен.
#   python server.py  — пути и порты из переменных окружения (их ставит start.ps1)

import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tools.py рядом (python -I не добавляет папку скрипта)
import agent  # noqa: E402
import tools  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "app" / "web"
DATA = Path(os.environ.get("ASSISTANT_DATA") or ROOT / "data")   # другая папка данных — для проверок
PIPER = ROOT / "runtime" / "piper" / "piper.exe"
VOICES = ROOT / "runtime" / "voices"
SILERO_PY = ROOT / "runtime" / "pyenv" / "Scripts" / "python.exe"
SILERO_MODEL = ROOT / "runtime" / "silero" / "v5_6_ru.pt"
SILERO_VOICES = {"xenia": "Ксения", "baya": "Байя", "kseniya": "Ксения 2", "aidar": "Айдар", "eugene": "Евгений"}
PIPER_VOICES = {"irina": "Ирина", "dmitri": "Дмитрий", "denis": "Денис", "ruslan": "Руслан"}

PORT = int(os.environ.get("ASSISTANT_PORT", "8765"))
OLLAMA = os.environ.get("ASSISTANT_OLLAMA", "http://127.0.0.1:11435")

# ---------- модели ----------
# встроенные — здесь; свои добавляются из окна (⚙ → «Модели») и лежат в data/models.json: [{"id", "name", "label", "gpu"}].
# name — имя в Ollama: из каталога ollama.com/library («qwen3:14b») или GGUF с Hugging Face («hf.co/автор/репо:Q4_K_M»).
# gpu: false — только процессор (num_gpu 0), видеокарта остаётся играм. num_ctx у GigaChat — как у Евы (p2_ainpc).
# think — рассуждать перед ответом (для моделей, которые умеют); меняется в окне, выбор — settings["think"][id].
# Замер 26.10 (8 фраз «сделал полезное»): Qwen 3.5 9B без рассуждений отмечала привычку 1 раз из 8, с рассуждениями — 6
# (ответ ~4,7 с вместо 1,5); GigaChat без рассуждений — 8 из 8.
BUILTIN_MODELS = [
    {"id": "qwen9b", "name": "qwen3.5:9b", "label": "Qwen 3.5 9B", "gpu": True, "think": True},
    {"id": "gigachat", "name": "hf.co/ai-sage/GigaChat3.1-10B-A1.8B-GGUF:Q4_K_M", "label": "GigaChat 3.1", "gpu": False},
]
DEFAULT_MODEL = "qwen9b"
NUM_CTX = 8192
# сколько конца разговора отдавать модели. Остальное окно — характер, память, инструменты (~1800 токенов), рассуждения
# и ответ. 26.10 разговор в 26 тыс. символов не влез в 8192 токена: Qwen начала рассуждать и замолчала без ответа.
# Старое не теряется — оно в архиве, его находит поиск по прошлому.
HISTORY_CHARS = 10000
NAME_RE = re.compile(r"[\w.\-/:]{2,200}")
caps = {}    # имя модели → что умеет по словам Ollama: tools, thinking, vision …
pulls = {}   # имя модели → как идёт скачивание: {"status", "completed", "total", "error", "done"}


def models():
    return [dict(m, builtin=True) for m in BUILTIN_MODELS] + [dict(m, builtin=False) for m in read_json(DATA / "models.json", [])]


def model_cfg():
    ms = models()
    want = settings().get("model")
    return next((m for m in ms if m["id"] == want), None) or next(m for m in ms if m["id"] == DEFAULT_MODEL)


def think_on(m):
    return bool(settings().get("think", {}).get(m["id"], m.get("think", False)))


def model_label(m):
    return f"{m['label']} · {'видеокарта' if m.get('gpu') else 'процессор'}"


def ollama(path, body=None, method=None, timeout=30):
    req = urllib.request.Request(OLLAMA + path, data=None if body is None else json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def installed():
    try:
        return {m["name"] for m in ollama("/api/tags").get("models", [])}
    except Exception:
        return set()


def capabilities(name):
    if name not in caps:
        try:
            caps[name] = ollama("/api/show", {"model": name}).get("capabilities") or []
        except Exception:
            return []   # модели ещё нет или Ollama не ответила — спросить в следующий раз
    return caps[name]


def model_body(**extra):
    """Начало запроса к Ollama для выбранной модели."""
    m = model_cfg()
    body = {"model": m["name"], "keep_alive": "60m", "options": {"num_ctx": NUM_CTX}}
    if not m.get("gpu"):
        body["options"]["num_gpu"] = 0
    can = capabilities(m["name"])
    body["think"] = think_on(m)
    body.update(extra)
    if "thinking" not in can:
        body.pop("think")       # модель не умеет рассуждать — Ollama отказала бы
    if "tools" in body and "tools" not in can:
        body.pop("tools")       # модель без инструментов: просто разговор, без напоминаний и привычек
    return body


def warm():
    """Загрузить выбранную модель заранее (в фоне) — первый ответ без долгого ожидания."""
    def run():
        try:
            if model_cfg()["name"] in installed():
                ollama("/api/chat", model_body(messages=[], stream=False), timeout=600)
        except Exception as e:
            print(f"прогрев модели: {e}", flush=True)
    threading.Thread(target=run, daemon=True).start()


def pull(name):
    """Скачать модель в Ollama (в фоне); ход — в pulls, окно спрашивает /api/models."""
    if name in pulls and not pulls[name].get("done"):
        return

    def run():
        pulls[name] = {"status": "начинаю"}
        try:
            req = urllib.request.Request(OLLAMA + "/api/pull", data=json.dumps({"model": name, "stream": True}).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3600) as resp:
                for line in resp:
                    if not line.strip():
                        continue
                    part = json.loads(line)
                    if part.get("error"):
                        raise RuntimeError(part["error"])
                    pulls[name] = {"status": part.get("status", ""), "completed": part.get("completed"), "total": part.get("total")}
            pulls[name] = {"status": "готово", "done": True}
            caps.pop(name, None)
            if model_cfg()["name"] == name:
                warm()
        except Exception as e:
            pulls[name] = {"status": "ошибка", "error": str(e), "done": True}
    threading.Thread(target=run, daemon=True).start()


def models_state():
    have = installed()
    out = []
    for m in models():
        ok = m["name"] in have
        can = capabilities(m["name"]) if ok else []
        out.append({**m, "place": model_label(m), "installed": ok, "pull": pulls.get(m["name"]),
                    "tools": "tools" in can, "thinking": "thinking" in can, "think": think_on(m)})
    return {"models": out, "current": model_cfg()["id"]}


def models_edit(data):
    op = data.get("op")
    with lock:
        custom = read_json(DATA / "models.json", [])
        if op == "add":
            name = str(data.get("name") or "").strip()
            name = name.removeprefix("ollama run ").removeprefix("ollama pull ").strip()   # скопировали команду целиком
            if not NAME_RE.fullmatch(name):
                return "Имя модели — как в Ollama: «qwen3:14b» или «hf.co/автор/репозиторий:Q4_K_M»."
            if any(m["name"] == name for m in models()):
                return "Такая модель уже есть в списке."
            n = max([int(m["id"][1:]) for m in custom if m["id"][1:].isdigit()], default=0) + 1
            custom.append({"id": f"m{n}", "name": name, "label": str(data.get("label") or "").strip()[:40] or name.split("/")[-1],
                           "gpu": bool(data.get("gpu", True))})
            write_json(DATA / "models.json", custom)
            if name not in installed():
                pull(name)
        elif op == "remove":
            m = next((x for x in custom if x["id"] == data.get("id")), None)
            if not m:
                return "Встроенные модели не удаляются."
            write_json(DATA / "models.json", [x for x in custom if x is not m])
            if settings().get("model") == m["id"]:
                s = settings()
                s["model"] = DEFAULT_MODEL
                write_json(DATA / "settings.json", s)
            if data.get("files"):
                try:
                    ollama("/api/delete", {"model": m["name"]}, method="DELETE")
                except Exception as e:
                    return f"Из списка убрана, но файлы не удалились: {e}"
        elif op == "think":
            s = settings()
            s["think"] = {**s.get("think", {}), str(data.get("id")): bool(data.get("on"))}
            write_json(DATA / "settings.json", s)
        elif op == "pull":
            m = next((x for x in models() if x["id"] == data.get("id")), None)
            if m:
                pull(m["name"])
    return ""


DEFAULT_SETTINGS = {
    "model": DEFAULT_MODEL,
    "think": {},
    "system": (
        "Ты — личный помощник и собеседник. Говори по-русски, тепло и по-человечески, на «ты». "
        "Отвечай по делу и не слишком длинно, если не просят подробнее. "
        "Не выдумывай факты: если не знаешь — так и скажи."
    ),
    "voice": "silero:xenia",
    "speak": False,
    "speed": 1.0,
}

lock = threading.Lock()


# ---------- память «Что я о тебе знаю» ----------
# data/memory.json: {"facts": [{"id", "text", "at"}], "next_id", "seen"(сколько сообщений разговора уже разобрано),
#                    "news": [{"n", "text"}] — последние изменения для подсказки в окне, "n" — их счётчик}
# После ответа, когда в чате тихо MEMORY_PAUSE секунд, модель читает новые сообщения и сама правит список фактов.
MEMORY = DATA / "memory.json"
MEMORY_PAUSE = 20
MEMORY_MAX = 80
MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "add": {"type": "array", "items": {"type": "string"}},
        "update": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
                                              "required": ["id", "text"]}},
        "remove": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["add", "update", "remove"],
}
MEMORY_RULES = (
    "Ты ведёшь записную книжку личного помощника о его хозяине (пользователе). Тебе дают то, что уже записано, "
    "и новый кусок разговора. Верни, что изменить в записной книжке.\n"
    "Записывай только то, что пригодится в будущих разговорах: имя, возраст, город, работа и учёба, семья и близкие, "
    "питомцы, увлечения, цели и планы (с датами, если названы), договорённости и обещания, предпочтения — что любит "
    "и не любит, как ему удобнее, чтобы помощник отвечал. Только то, что сказал сам пользователь о себе, — не слова "
    "помощника и не общие сведения из разговора. Не записывай мимолётное (что спросил сейчас, погоду, шутки).\n"
    "Каждый факт — одна короткая фраза по-русски в третьем лице без местоимения «пользователь»: «Зовут Алекс», "
    "«Работает программистом», «Хочет бросить курить до нового года». Если новое уточняет или опровергает записанное — "
    "update с номером записи; если запись устарела или неверна — remove. Ничего нового — пустые списки."
)
mem_lock = threading.Lock()
mem_timer = None
chatting = threading.Event()

# ---------- доступ к ПК (agent.py): карточки «Разрешить / Отклонить» и окно 🔐 ----------
pending = {}          # номер карточки → {"ev": Event, "ok": bool}; номер знает только поток ответа окна
CONFIRM_WAIT = 300    # не ответили за 5 минут — «нет»


def access_state():
    a = agent.access()
    return {"enabled": a["enabled"], "folders": a["folders"],
            "tools": [{"name": n, "title": i["title"], "mode": agent.mode(n) if a["enabled"] else a["tools"].get(n, i["default"]),
                       "modes": [[m, agent.MODE_NAMES[m]] for m in i["modes"]]} for n, i in agent.TOOL_INFO.items()],
            "mail": agent.mail_public(), "log": agent.log_tail()}


def access_edit(data):
    op = data.get("op")
    if op == "mail":
        return agent.mail_save(data)
    if op == "mail_test":
        return agent.mail_test()
    if op == "mail_forget":
        agent.mail_forget()
        return ""
    with lock:
        a = agent.access()
        if op == "enabled":
            a["enabled"] = data.get("on") is True
        elif op == "mode":
            name, m = data.get("name"), data.get("mode")
            if name in agent.TOOL_INFO and m in agent.TOOL_INFO[name]["modes"]:
                a["tools"][name] = m
        elif op == "folder_add":
            p = Path(os.path.expandvars(str(data.get("path") or "").strip().strip('"')))
            if not p.is_absolute() or not p.is_dir():
                return "Нет такой папки — нужен полный путь, например D:\\Работа."
            real = os.path.realpath(p)
            if agent.BLOCKED.search(real + "\\"):
                return "Эту папку открыть нельзя: системная или с паролями и ключами."
            if real not in a["folders"]:
                a["folders"].append(real)
        elif op == "folder_remove":
            a["folders"] = [f for f in a["folders"] if f != data.get("path")]
        write_json(DATA / "access.json", a)
    return ""


def memory():
    m = read_json(MEMORY, {})
    m.setdefault("facts", [])
    m.setdefault("next_id", max([f["id"] for f in m["facts"]], default=0) + 1)
    m.setdefault("seen", 0)
    m.setdefault("news", [])
    m.setdefault("n", 0)
    return m


def memory_note(m, text):
    m["n"] += 1
    m["news"] = (m["news"] + [{"n": m["n"], "text": text}])[-20:]


def memory_prompt():
    facts = memory()["facts"]
    if not facts:
        return ""
    lines = "\n".join("- " + f["text"] for f in facts)
    return ("\n\nЧто ты знаешь о собеседнике из прошлых разговоров (учитывай естественно, не перечисляй без повода, "
            "не говори «я помню, что…» на каждом шагу):\n" + lines)


def now_line():
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    t = datetime.datetime.now()
    return f"\n\nСейчас: {days[t.weekday()]}, {t.day} {months[t.month - 1]} {t.year}, {t:%H:%M}."


def memory_later():
    """Перезапустить отсчёт: память разбирается, когда в чате тихо MEMORY_PAUSE секунд."""
    global mem_timer
    if mem_timer:
        mem_timer.cancel()
    mem_timer = threading.Timer(MEMORY_PAUSE, memory_update)
    mem_timer.daemon = True
    mem_timer.start()


def memory_update():
    if chatting.is_set():
        return memory_later()
    with mem_lock:
        m = memory()
        history = read_json(DATA / "history.json", [])
        if m["seen"] > len(history):   # разговор начали заново
            m["seen"] = 0
        new = history[m["seen"]:]
        if not any(x.get("role") == "user" for x in new):
            return
        new = new[-24:]
        known = "\n".join(f"{f['id']}. {f['text']}" for f in m["facts"]) or "(пока пусто)"
        dialog = "\n".join(("Пользователь: " if x["role"] == "user" else "Помощник: ") + str(x.get("content", ""))[:1500]
                           for x in new if x.get("role") in ("user", "assistant"))
        body = model_body(stream=False, think=False, format=MEMORY_SCHEMA, messages=[
            {"role": "system", "content": MEMORY_RULES},
            {"role": "user", "content": f"Уже записано:\n{known}\n\nНовый разговор:\n{dialog}"}])
        body["options"]["temperature"] = 0.2
        try:
            req = urllib.request.Request(OLLAMA + "/api/chat", json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                ops = json.loads(json.load(r)["message"]["content"])
        except Exception as e:  # модель занята или ответила не по схеме — попробуем в следующий раз
            print(f"память: не разобрано ({e})", flush=True)
            return
        today = datetime.date.today().isoformat()
        by_id = {f["id"]: f for f in m["facts"]}
        for fid in ops.get("remove") or []:
            if fid in by_id:
                memory_note(m, "Забыл: " + by_id[fid]["text"])
                m["facts"].remove(by_id.pop(fid))
        for u in ops.get("update") or []:
            f = by_id.get(u.get("id"))
            text = str(u.get("text", "")).strip()
            if f and text and text != f["text"]:
                f["text"], f["at"] = text[:200], today
                memory_note(m, "Уточнил: " + f["text"])
        texts = {f["text"].lower() for f in m["facts"]}
        for text in ops.get("add") or []:
            text = str(text).strip()[:200]
            if text and text.lower() not in texts and len(m["facts"]) < MEMORY_MAX:
                m["facts"].append({"id": m["next_id"], "text": text, "at": today})
                m["next_id"] += 1
                texts.add(text.lower())
                memory_note(m, "Запомнил: " + text)
        m["seen"] = len(history)
        write_json(MEMORY, m)


def memory_edit(data):
    """Правка из окна: add / update / remove / clear."""
    with mem_lock:
        m = memory()
        op, today = data.get("op"), datetime.date.today().isoformat()
        text = str(data.get("text", "")).strip()[:200]
        if op == "add" and text:
            m["facts"].append({"id": m["next_id"], "text": text, "at": today})
            m["next_id"] += 1
        elif op == "update" and text:
            for f in m["facts"]:
                if f["id"] == data.get("id"):
                    f["text"], f["at"] = text, today
        elif op == "remove":
            m["facts"] = [f for f in m["facts"] if f["id"] != data.get("id")]
        elif op == "clear":
            m["facts"] = []
        write_json(MEMORY, m)
        return m


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, value):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def settings():
    return {**DEFAULT_SETTINGS, **read_json(DATA / "settings.json", {})}


def voices():
    out = []
    if SILERO_PY.exists() and SILERO_MODEL.exists():
        out += [{"id": "silero:" + k, "name": v + " (Silero)"} for k, v in SILERO_VOICES.items()]
    for p in sorted(VOICES.glob("ru_RU-*-medium.onnx")):
        k = p.name.split("-")[1]
        out.append({"id": k, "name": PIPER_VOICES.get(k, k) + " (Piper)"})
    return out


class Silero:
    """Процесс silero_worker.py: запускается один раз, фразы — по одной строке JSON."""

    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()

    def start(self):
        if self.proc and self.proc.poll() is None:
            return True
        if not (SILERO_PY.exists() and SILERO_MODEL.exists()):
            return False
        self.proc = subprocess.Popen(
            [str(SILERO_PY), "-I", str(ROOT / "app" / "silero_worker.py"), str(SILERO_MODEL)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        ready = self.proc.stdout.readline()
        return '"ready"' in ready

    def synth(self, text, speaker, speed):
        with self.lock:
            if not self.start():
                return b""
            fd, out = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            try:
                self.proc.stdin.write(json.dumps({"text": text, "speaker": speaker, "speed": speed, "out": out}, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
                answer = json.loads(self.proc.stdout.readline() or "{}")
                return Path(out).read_bytes() if answer.get("ok") else b""
            except (OSError, ValueError):
                self.proc = None
                return b""
            finally:
                try:
                    os.remove(out)
                except OSError:
                    pass


silero = Silero()


def synth(text, voice, speed):
    """Текст → WAV: «silero:имя» — Silero, иначе — Piper. Пустой результат — если голос недоступен."""
    if voice.startswith("silero:"):
        return silero.synth(text, voice.split(":", 1)[1], speed)
    model = VOICES / f"ru_RU-{voice}-medium.onnx"
    if not PIPER.exists() or not model.exists():
        return b""
    fd, out = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        subprocess.run(
            [str(PIPER), "--model", str(model), "--output_file", out,
             "--length_scale", f"{1.0 / max(0.5, min(2.0, speed)):.2f}", "--quiet"],
            input=text.encode("utf-8"), cwd=str(PIPER.parent), capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return Path(out).read_bytes()
    except (OSError, subprocess.SubprocessError):
        return b""
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # без строки на каждый запрос
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def trusted(self, post):
        """Запрос — из нашего окна, а не со стороннего сайта в браузере. Host — только наш (против подмены DNS);
        у POST — JSON (браузер не пошлёт его с чужого сайта без спроса) и Origin, если есть, — наш."""
        ours = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        if self.headers.get("Host") not in ours:
            return False
        if post:
            origin = self.headers.get("Origin")
            if origin and origin.removeprefix("http://") not in ours:
                return False
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return False
        return True

    def do_GET(self):
        if not self.trusted(False):
            return self.send(403, {"error": "чужой запрос"})
        path = self.path.split("?")[0]
        if path == "/api/access":
            return self.send(200, access_state())
        if path == "/api/state":
            return self.send(200, {
                **models_state(),
                "settings": settings(),
                "voices": voices(),
                "history": read_json(DATA / "history.json", []),
                "memory": memory()["facts"],
                "stt": True,
            })
        if path == "/api/memory":
            m = memory()
            return self.send(200, {"facts": m["facts"], "news": m["news"], "n": m["n"]})
        if path == "/api/models":
            return self.send(200, models_state())
        if path == "/api/events":
            return self.send(200, tools.get_events())
        if path == "/api/reminders":
            return self.send(200, {"items": tools.upcoming()})
        if path == "/api/habits":
            h = tools.habits()
            for x in h["habits"]:
                x["streak"] = tools.streak(x["days"])
            return self.send(200, {**h, "today": tools.today().isoformat()})
        name = "index.html" if path == "/" else path.lstrip("/")
        file = (WEB / name).resolve()
        if WEB not in file.parents or not file.is_file():
            return self.send(404, b"not found", "text/plain")
        types = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}
        self.send(200, file.read_bytes(), types.get(file.suffix, "application/octet-stream") + "; charset=utf-8")

    def do_POST(self):
        if not self.trusted(True):
            return self.send(403, {"error": "чужой запрос"})
        path = self.path.split("?")[0]
        try:
            data = self.body()
        except ValueError:
            return self.send(400, {"error": "плохой запрос"})

        if path == "/api/confirm":     # ответ на карточку «Разрешить / Отклонить»: {"id", "ok"}
            p = pending.get(str(data.get("id")))
            if p:
                p["ok"] = data.get("ok") is True
                p["ev"].set()
            return self.send(200, {"ok": bool(p)})

        if path == "/api/access":      # окно 🔐: выключатель, папки, режимы инструментов, почта
            msg = access_edit(data)
            return self.send(200, {**access_state(), "msg": msg})

        if path == "/api/settings":
            with lock:
                s = settings()
                old = s.get("model")
                if "model" in data and not any(m["id"] == data["model"] for m in models()):
                    data.pop("model")
                s.update({k: data[k] for k in DEFAULT_SETTINGS if k in data})
                write_json(DATA / "settings.json", s)
            if s.get("model") != old:
                warm()
            return self.send(200, s)

        if path == "/api/models":      # {"op": "add", "name", "label", "gpu"} / "remove" {id, files} / "pull" {id}
            msg = models_edit(data)
            return self.send(200, {**models_state(), "msg": msg})

        if path == "/api/history":
            with lock:
                write_json(DATA / "history.json", data.get("messages", [])[-400:])
            return self.send(200, {"ok": True})

        if path == "/api/memory":
            m = memory_edit(data)
            return self.send(200, {"facts": m["facts"], "news": m["news"], "n": m["n"]})

        if path == "/api/reminders":   # из окна: {"op": "add", "text", "when"} или {"op": "remove", "id"}
            msg = ""
            if data.get("op") == "add":
                said, ok = tools.add_reminder(data.get("text"), data.get("when"))
                msg = "" if ok else said.replace(" — уточни у пользователя", "")
            elif data.get("op") == "remove":
                tools.remove_reminder(data.get("id"))
            return self.send(200, {"items": tools.upcoming(), "msg": msg})

        if path == "/api/habits":      # {"op": "add", "name"} / "remove" {id} / "toggle" {id, day} / "check" {time}
            op = data.get("op")
            if op == "add":
                tools.add_habit(data.get("name"))
            elif op == "remove":
                tools.remove_habit(data.get("id"))
            elif op == "toggle":
                tools.toggle_day(data.get("id"), str(data.get("day")))
            elif op == "check":
                tools.set_check_time(str(data.get("time", "")))
            h = tools.habits()
            for x in h["habits"]:
                x["streak"] = tools.streak(x["days"])
            return self.send(200, {**h, "today": tools.today().isoformat()})

        if path == "/api/stt":         # голос: {"wav": base64 WAV 16 кГц} → {"text"}
            try:
                return self.send(200, {"text": tools.stt_b64(data.get("wav") or "")})
            except Exception as e:
                return self.send(503, {"error": f"распознавание недоступно: {e}"})

        if path == "/api/tts":
            s = settings()
            wav = synth(str(data.get("text", ""))[:2000], data.get("voice") or s["voice"], float(data.get("speed") or s["speed"]))
            return self.send(200, wav, "audio/wav") if wav else self.send(503, {"error": "голос недоступен"})

        if path == "/api/chat":
            return self.chat(data)

        self.send(404, {"error": "нет такого"})

    def chat(self, data):
        """Поток ответа: строки JSON {"t": "кусок текста"}, {"tool": "что сделал"} … {"done": true}.
        Модель может вызвать инструменты (напоминание, привычка, поиск, «запомни») — сервер выполняет их и даёт
        модели результат, она отвечает дальше (до 8 кругов). Действия на ПК — agent.py; если нужно разрешение — строка
        {"confirm": {...}} и ожидание /api/confirm; снимок экрана — {"image": "data:…"}."""
        s = settings()
        # характер + что помощник знает о тебе + как пользоваться инструментами
        system = s["system"] + memory_prompt() + tools.TOOL_RULES + agent.rules()
        toolset = tools.TOOLS + agent.tools_for(capabilities(model_cfg()["name"]))
        history = [m for m in data.get("messages", []) if m.get("role") in ("user", "assistant")][-40:]
        size = 0
        for i in range(len(history) - 1, -1, -1):   # с конца, пока помещается (последняя реплика — всегда)
            size += len(str(history[i].get("content", "")))
            if size > HISTORY_CHARS and i < len(history) - 1:
                history = history[i + 1:]
                break
        # то, что меняется каждую минуту (время, напоминания, привычки). На процессоре — перед последней репликой: начало
        # разговора остаётся прежним, и модель не перечитывает его заново (иначе 10 с). На видеокарте перечитать — полсекунды,
        # а Qwen со строкой посреди разговора путается (не вызывает инструменты) — там она в начале.
        now = (now_line() + tools.reminders_prompt() + tools.habits_prompt()).strip()
        if model_cfg().get("gpu"):
            messages = [{"role": "system", "content": system + "\n\n" + now}] + history
        else:
            messages = [{"role": "system", "content": system}] + history[:-1] + [{"role": "system", "content": now}] + history[-1:]
        if history and history[-1]["role"] == "user":
            tools.archive("user", history[-1].get("content", ""))
        chatting.set()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def out(obj):
            self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()

        def ask(card):
            """Карточка «Разрешить / Отклонить» в окне; ждём ответ (не дольше CONFIRM_WAIT)."""
            cid = os.urandom(16).hex()
            pending[cid] = {"ev": threading.Event(), "ok": False}
            try:
                out({"confirm": {"id": cid, **card}})
                pending[cid]["ev"].wait(CONFIRM_WAIT)
                ok = pending[cid]["ok"]
                out({"confirmed": {"id": cid, "ok": ok}})
                return ok
            finally:
                pending.pop(cid, None)

        answer = ""
        try:
            plain = False   # повтор шага без рассуждений (см. ниже)
            for _ in range(9):   # поручение может занять несколько шагов: найти файл → прочитать → написать письмо
                extra = {"think": False} if plain else {}
                req = urllib.request.Request(
                    OLLAMA + "/api/chat",
                    data=json.dumps(model_body(messages=messages, stream=True, tools=toolset, **extra)).encode("utf-8"),
                    headers={"Content-Type": "application/json"})
                said, calls, thinking, ended = "", [], False, None
                with urllib.request.urlopen(req, timeout=600) as resp:
                    for line in resp:
                        if not line.strip():
                            continue
                        part = json.loads(line)
                        msg = part.get("message") or {}
                        text = msg.get("content") or ""
                        if msg.get("thinking") and not said and not thinking:
                            thinking = True
                            out({"thinking": True})   # окно покажет «думает…», пока модель рассуждает
                        if text:
                            said += text
                            out({"t": text})
                        calls += msg.get("tool_calls") or []
                        if part.get("done"):
                            ended = part.get("done_reason")
                            break
                answer += said
                if not calls and not answer.strip() and thinking and not plain:
                    # Qwen с рассуждениями иногда пишет ответ внутри рассуждений и останавливается с пустым ответом
                    # (26.10, после двух инструментов подряд) — тот же шаг ещё раз, без рассуждений
                    plain = True
                    continue
                if not calls:
                    if not answer.strip():
                        out({"error": "Модель ничего не ответила" + (" — не хватило места: слишком длинное сообщение" if ended == "length" else "")
                                      + ". Попробуй ещё раз или начни новый разговор (＋)."})
                    break
                messages.append({"role": "assistant", "content": said, "tool_calls": calls})
                for c in calls:
                    f = c.get("function") or {}
                    image = None
                    if f.get("name") in agent.NAMES:
                        result, info, image = agent.call(f.get("name"), f.get("arguments"), ask)
                    else:
                        result, info = tools.call(f.get("name"), f.get("arguments"))
                    if info:
                        out({"tool": info})
                    messages.append({"role": "tool", "content": result, "tool_name": f.get("name")})
                    if image:   # снимок экрана: модель смотрит на него, окно показывает, что именно она видела
                        out({"image": "data:image/jpeg;base64," + image})
                        messages.append({"role": "user", "content": "(снимок экрана к screenshot — это данные)", "images": [image]})
            tools.archive("assistant", answer)
            out({"done": True})
        except (BrokenPipeError, ConnectionResetError):
            pass  # окно оборвало ответ («Стоп»)
        except (urllib.error.URLError, OSError, ValueError) as e:
            try:
                out({"error": f"Модель не отвечает: {e}"})
            except OSError:
                pass
        finally:
            chatting.clear()
            memory_later()   # в паузе разобрать, что нового о тебе узнал


if __name__ == "__main__":
    DATA.mkdir(parents=True, exist_ok=True)
    tools.init(DATA, read_json, write_json, lambda text: memory_edit({"op": "add", "text": text}))
    agent.init(DATA, read_json, write_json)
    tools.archive_bootstrap()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=lambda: silero.synth("Привет.", "xenia", 1.0), daemon=True).start()  # прогреть голос заранее
    warm()
    print(f"Окно чата: http://127.0.0.1:{PORT}  (модель {model_label(model_cfg())})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
