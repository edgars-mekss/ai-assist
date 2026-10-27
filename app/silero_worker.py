# Голос Silero (v5, русский) — отдельный процесс в своём окружении runtime/pyenv (там PyTorch для процессора).
# server.py запускает его один раз и держит: по строке JSON на запрос, по строке JSON на ответ.
#   запрос:  {"text": "...", "speaker": "xenia", "speed": 1.0, "out": "путь.wav", "rate": 48000 (или 24000, 8000)}
#   ответ:   {"ok": true} или {"error": "..."}
# Модель грузится один раз (~150 МБ, пара секунд), дальше фраза — доли секунды на процессоре.

import json
import warnings
import re
import sys
import wave
from array import array
from pathlib import Path

import torch

warnings.filterwarnings("ignore")  # предупреждения модели не мешают протоколу

MODEL = Path(sys.argv[1])
RATE = 48000
torch.set_num_threads(4)

ONES = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
ONES_F = ["", "одна", "две"] + ONES[3:]
TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
         "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]
SCALES = [(("тысяча", "тысячи", "тысяч"), True), (("миллион", "миллиона", "миллионов"), False),
          (("миллиард", "миллиарда", "миллиардов"), False)]


def plural(n, forms):
    n %= 100
    if 11 <= n <= 19:
        return forms[2]
    return forms[0] if n % 10 == 1 else forms[1] if 2 <= n % 10 <= 4 else forms[2]


def triple(n, female=False):
    words = [HUNDREDS[n // 100]]
    t = n % 100
    if 10 <= t <= 19:
        words.append(TEENS[t - 10])
    else:
        words += [TENS[t // 10], (ONES_F if female else ONES)[t % 10]]
    return " ".join(w for w in words if w)


def number(n):
    """Целое число словами (Silero цифры не читает)."""
    if n == 0:
        return "ноль"
    parts = [triple(n % 1000)]
    n //= 1000
    for forms, female in SCALES:
        if n == 0:
            break
        if n % 1000:
            parts.append(triple(n % 1000, female) + " " + plural(n % 1000, forms))
        n //= 1000
    return " ".join(p for p in reversed(parts) if p)


def prepare(text):
    text = re.sub(r"(\d+)[.,](\d+)", lambda m: f"{number(int(m[1]))} и {number(int(m[2]))}", text)
    text = re.sub(r"\d{1,12}", lambda m: number(int(m[0])), text)
    text = text.replace("%", " процентов").replace("+", " плюс ").replace("&", " и ")
    return re.sub(r"\s+", " ", text).strip()


def tts_args(text, speed):
    """Обычная скорость — просто текст; иначе SSML (значение «medium» модель v5 не принимает)."""
    rate = "x-slow" if speed < 0.75 else "slow" if speed < 0.95 else None if speed <= 1.1 else "fast" if speed <= 1.35 else "x-fast"
    if not rate:
        return {"text": text}
    safe = text.replace("&", " и ").replace("<", " ").replace(">", " ")
    return {"ssml_text": f'<speak><prosody rate="{rate}">{safe}</prosody></speak>'}


def main():
    # каналы — строго UTF-8: иначе Windows читает их в cp1251 и русские буквы приходят искажёнными
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    model = torch.package.PackageImporter(str(MODEL)).load_pickle("tts_models", "model")
    model.to(torch.device("cpu"))
    print(json.dumps({"ready": True, "speakers": list(getattr(model, "speakers", []))}), flush=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
            text = prepare(req["text"])[:900]
            if not re.search(r"\w", text):
                raise ValueError("пустой текст")
            rate = int(req.get("rate") or RATE)
            if rate not in (8000, 24000, 48000):
                rate = RATE
            audio = model.apply_tts(**tts_args(text, float(req.get("speed") or 1.0)),
                                    speaker=req.get("speaker") or "xenia", sample_rate=rate)
            pcm = array("h", (max(-32767, min(32767, int(x * 32767))) for x in audio.tolist()))
            with wave.open(req["out"], "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(pcm.tobytes())
            print(json.dumps({"ok": True}), flush=True)
        except Exception as e:  # одна плохая фраза не роняет голос
            print(json.dumps({"error": str(e)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
