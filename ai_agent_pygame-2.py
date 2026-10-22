# -*- coding: utf-8 -*-
"""
AI-агент с интерфейсом на pygame (9:16) и СВОЕЙ виртуальной клавиатурой
для Pydroid 3. OpenAI API, остальные библиотеки не нужны.

Системная клавиатура телефона не используется и сама не открывается.
Строка ввода и кнопка «Отправить» стоят чуть выше клавиатуры.

Запуск: открой файл в Pydroid 3 -> ▶ -> вставь API-ключ в поле чата (один раз).
Тап по чату скрывает клавиатуру, тап по строке ввода показывает.
При ошибке смотри файл error_log.txt рядом со скриптом.
"""
import ast
import datetime
import json
import operator
import os
import queue
import threading
import urllib.error
import urllib.request
from typing import List, Tuple

import pygame

API_URL = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-4o-mini"  # можно заменить на gpt-4o
BASE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(BASE, "api_key.txt")
NOTES_FILE = os.path.join(BASE, "notes.txt")
MAX_STEPS = 8

SYSTEM = (
    "Ты полезный AI-агент на телефоне пользователя. "
    "Отвечай кратко и по-русски. Используй инструменты, когда они нужны."
)

API_KEY = ""
MESSAGES = []  # история диалога для API


# =====================  АГЕНТ  =====================
def get_api_key():
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    return ""


def save_api_key(key):
    with open(KEY_FILE, "w", encoding="utf-8") as f:
        f.write(key)


OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Pow: operator.pow, ast.USub: operator.neg,
    ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv,
}


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        return OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
        return OPS[type(node.op)](_eval(node.operand))
    raise ValueError("Недопустимое выражение")


def calculator(expression):
    try:
        return str(_eval(ast.parse(expression, mode="eval").body))
    except Exception as e:
        return "Ошибка: %s" % e


def current_time():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def save_note(text):
    with open(NOTES_FILE, "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (current_time(), text))
    return "Заметка сохранена."


def read_notes():
    if not os.path.exists(NOTES_FILE):
        return "Заметок пока нет."
    with open(NOTES_FILE, encoding="utf-8") as f:
        return f.read() or "Заметок пока нет."


TOOL_FUNCS = {
    "calculator": lambda a: calculator(a["expression"]),
    "current_time": lambda a: current_time(),
    "save_note": lambda a: save_note(a["text"]),
    "read_notes": lambda a: read_notes(),
}


def _tool(name, desc, props=None, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": props or {},
                "required": required or [],
            },
        },
    }


TOOLS = [
    _tool("calculator", "Вычисляет математическое выражение, например 2*(3+4)**2.",
          {"expression": {"type": "string"}}, ["expression"]),
    _tool("current_time", "Возвращает текущие дату и время на устройстве."),
    _tool("save_note", "Сохраняет заметку в файл.",
          {"text": {"type": "string"}}, ["text"]),
    _tool("read_notes", "Читает все сохранённые заметки."),
]


def call_api(messages):
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM}] + messages,
        "tools": TOOLS,
    }).encode("utf-8")
    req = urllib.request.Request(
        API_URL, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + API_KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s: %s" % (e.code, e.read().decode("utf-8", "ignore")[:200]))


def run_agent(user_text):
    """Возвращает (ответ, список_вызванных_инструментов)."""
    log = []
    start = len(MESSAGES)
    MESSAGES.append({"role": "user", "content": user_text})
    try:
        for _ in range(MAX_STEPS):
            msg = call_api(MESSAGES)["choices"][0]["message"]
            MESSAGES.append(msg)
            calls = msg.get("tool_calls")
            if not calls:
                return msg.get("content") or "", log
            for c in calls:
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"]["arguments"] or "{}")
                except ValueError:
                    args = {}
                log.append("%s %s" % (name, json.dumps(args, ensure_ascii=False)))
                try:
                    out = TOOL_FUNCS[name](args)
                except Exception as e:
                    out = "Ошибка инструмента: %s" % e
                MESSAGES.append({"role": "tool", "tool_call_id": c["id"], "content": str(out)})
        return "Достигнут лимит шагов.", log
    except Exception:
        del MESSAGES[start:]
        raise


# =====================  ВИРТУАЛЬНАЯ КЛАВИАТУРА  =====================
# Взято из tl_keyboard.py: раскладки RU / EN / символы, Shift, «Вставить».
# Адаптировано: не зависит от tl_core и объекта app, размеры масштабируются.
def read_clipboard():
    """Текст из буфера обмена (Android / pygame / tk)."""
    # 1) Android ClipboardManager
    try:
        from jnius import autoclass
        activity = None
        for name in (
            "org.libsdl.app.SDLActivity",
            "org.kivy.android.PythonActivity",
            "org.renpy.android.PythonActivity",
        ):
            try:
                cls = autoclass(name)
                activity = getattr(cls, "mSingleton", None) or getattr(cls, "mActivity", None)
                if activity is not None:
                    break
            except Exception:
                continue
        if activity is not None:
            Context = autoclass("android.content.Context")
            cm = activity.getSystemService(Context.CLIPBOARD_SERVICE)
            clip = cm.getPrimaryClip()
            if clip is not None and clip.getItemCount() > 0:
                item = clip.getItemAt(0)
                txt = item.coerceToText(activity)
                if txt is not None:
                    return str(txt)
                if item.getText() is not None:
                    return str(item.getText())
    except Exception:
        pass
    # 2) pygame scrap
    try:
        if hasattr(pygame, "scrap") and pygame.scrap.get_init():
            data = pygame.scrap.get(pygame.SCRAP_TEXT)
            if data:
                if isinstance(data, bytes):
                    return data.decode("utf-8", errors="ignore")
                return str(data)
    except Exception:
        pass
    try:
        pygame.scrap.init()
        data = pygame.scrap.get(pygame.SCRAP_TEXT)
        if data:
            if isinstance(data, bytes):
                return data.decode("utf-8", errors="ignore")
            return str(data)
    except Exception:
        pass
    # 3) tkinter
    try:
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        txt = r.clipboard_get()
        r.destroy()
        return str(txt)
    except Exception:
        pass
    return ""


class VirtualKeyboard:
    """Клавиатура как на Android: RU / EN / символы."""

    def __init__(self, px, font_key, font_small, insert, backspace, submit, toast=None):
        self.px = px
        self.font = font_key
        self.sfont = font_small
        self.insert = insert        # insert(text)
        self.backspace = backspace  # backspace()
        self.submit = submit        # submit()
        self.toast = toast          # toast(text, kind) kind: "ok" | "warn"
        self.rect = pygame.Rect(0, 0, 1, 1)
        self.visible = False
        self.btn_rects = []  # type: List[Tuple[pygame.Rect, str]]
        self.key_h = px(46)
        self.key_gap = px(6)
        self.pad_x = px(5)
        self.pad_y = px(6)
        self.layout = "ru"  # ru | en | sym
        self.shift = False

    def height(self):
        return 5 * (self.key_h + self.key_gap) + self.pad_y * 2

    def _letter_rows(self):
        if self.layout == "ru":
            rows = [
                list("1234567890"),
                list("йцукенгшщзхъ"),
                list("фывапролджэ"),
                list("ячсмитьбю"),
            ]
        elif self.layout == "en":
            rows = [
                list("1234567890"),
                list("qwertyuiop"),
                list("asdfghjkl"),
                list("zxcvbnm"),
            ]
        else:  # sym
            rows = [
                list("1234567890"),
                list("@#₽%&-+()"),
                list("*\"':;!/?"),
                list("=<>[]{}"),
            ]
        if self.shift and self.layout in ("ru", "en"):
            rows = [[c.upper() if c.isalpha() else c for c in row] for row in rows]
        return rows

    def press_at(self, pos):
        """Нажатие. Возвращает код клавиши, "" (тап по фону клавиатуры) или None (мимо)."""
        if not self.visible:
            return None
        for rect, key in self.btn_rects:
            if rect.collidepoint(pos):
                self.press(key)
                return key
        return "" if self.rect.collidepoint(pos) else None

    def press(self, key):
        if key == "SYM":
            self.layout = "sym"
            self.shift = False
        elif key == "LANG":
            self.layout = "ru" if self.layout in ("sym", "en") else "en"
            self.shift = False
        elif key == "SHIFT":
            self.shift = not self.shift
        elif key == "BACK":
            self.backspace()
        elif key == "ENTER":
            self.submit()
        elif key == "SPACE":
            self.insert(" ")
        elif key == "PASTE":
            clip = read_clipboard()
            if not clip:
                if self.toast:
                    self.toast("Буфер пуст", "warn")
            else:
                clip = clip.replace("\x00", "")[:4000]
                self.insert(clip)
                if self.toast:
                    self.toast("Вставлено %d симв." % len(clip), "ok")
        else:
            ch = key
            if self.layout in ("ru", "en") and len(ch) == 1 and ch.isalpha():
                ch = ch.upper() if self.shift else ch.lower()
            self.insert(ch)
            if self.shift and self.layout in ("ru", "en"):
                self.shift = False

    def draw(self, surface):
        if not self.visible:
            return
        px = self.px
        w, h = surface.get_size()
        letter_rows = self._letter_rows()
        pad_x, pad_y, gap, key_h = self.pad_x, self.pad_y, self.key_gap, self.key_h
        total_h = self.height()
        self.rect = pygame.Rect(0, h - total_h, w, total_h)

        pygame.draw.rect(surface, (210, 214, 220), self.rect)
        pygame.draw.line(surface, (180, 184, 190), (0, self.rect.y), (w, self.rect.y), 1)

        self.btn_rects = []
        font, sfont = self.font, self.sfont
        big = ("LANG", "SYM", "SPACE", "SHIFT", "BACK", "ENTER", "PASTE")

        def draw_key(x, y, kw, label, code):
            rect = pygame.Rect(int(x), int(y), int(kw), key_h)
            if code == "ENTER":
                bg, fg = (66, 133, 244), (255, 255, 255)
            elif code == "PASTE":
                bg, fg = (80, 140, 100), (255, 255, 255)
            elif code == "SHIFT" and self.shift:
                bg, fg = (120, 160, 220), (30, 30, 30)
            elif code in ("LANG", "SYM", "SHIFT", "BACK"):
                bg, fg = (160, 165, 175), (30, 30, 30)
            else:
                bg, fg = (245, 246, 248), (20, 20, 20)
            pygame.draw.rect(surface, bg, rect, border_radius=px(8))
            pygame.draw.rect(surface, (190, 194, 200), rect, 1, border_radius=px(8))
            if code == "SHIFT":
                text = "SHIFT"
            elif code == "BACK":
                text = "BACK"
            elif code == "ENTER":
                text = "ENTER"
            elif code == "PASTE":
                text = "Вставить"
            elif code == "LANG":
                text = {"ru": "РУС", "en": "ENG", "sym": "ABC"}[self.layout]
            elif code == "SPACE":
                text = {"ru": "Русский", "en": "English", "sym": " "}[self.layout]
            elif code == "SYM":
                text = "#1?"
            else:
                text = label
            f = sfont if code in big else font
            ts = f.render(text, True, fg)
            surface.blit(ts, ts.get_rect(center=rect.center))
            self.btn_rects.append((rect, code if code else label))

        y = self.rect.y + pad_y
        for row in letter_rows:
            n = len(row)
            kw = (w - 2 * pad_x - (n - 1) * gap) / n
            if row is letter_rows[-1] and self.layout != "sym":
                side = max(px(48), w * 0.12)
                mkw = (w - 2 * pad_x - 2 * side - (n + 1) * gap) / max(1, n)
                x = pad_x
                draw_key(x, y, side, "SHIFT", "SHIFT")
                x += side + gap
                for ch in row:
                    draw_key(x, y, mkw, ch, ch)
                    x += mkw + gap
                draw_key(x, y, side, "BACK", "BACK")
            else:
                x = pad_x
                for ch in row:
                    draw_key(x, y, kw, ch, ch)
                    x += kw + gap
            y += key_h + gap

        # нижний ряд: #1? | LANG | , | SPACE | . | Вставить | ENTER
        side = max(px(40), w * 0.10)
        enter_w = max(px(48), w * 0.12)
        paste_w = max(px(44), w * 0.13)
        comma_w = max(px(32), w * 0.07)
        space_w = w - 2 * pad_x - 2 * side - 2 * comma_w - paste_w - enter_w - 6 * gap
        space_w = max(space_w, px(48))
        x = pad_x
        draw_key(x, y, side, "#1?", "SYM" if self.layout != "sym" else "LANG")
        x += side + gap
        draw_key(x, y, side, "LANG", "LANG")
        x += side + gap
        draw_key(x, y, comma_w, ",", ",")
        x += comma_w + gap
        draw_key(x, y, space_w, "SPACE", "SPACE")
        x += space_w + gap
        draw_key(x, y, comma_w, ".", ".")
        x += comma_w + gap
        draw_key(x, y, paste_w, "Вст", "PASTE")
        x += paste_w + gap
        draw_key(x, y, enter_w, "ENTER", "ENTER")


# =====================  ИНТЕРФЕЙС  =====================
def main():
    global API_KEY
    API_KEY = get_api_key()

    pygame.init()
    try:
        pygame.key.stop_text_input()  # системная клавиатура не нужна
    except Exception:
        pass
    screen = pygame.display.set_mode((0, 0))
    pygame.display.set_caption("AI-агент")
    SW, SH = screen.get_size()

    # Рабочая область строго 9:16, по центру экрана
    if SW * 16 <= SH * 9:
        VW, VH = SW, SW * 16 // 9
    else:
        VH, VW = SH, SH * 9 // 16
    VX, VY = (SW - VW) // 2, (SH - VH) // 2
    view = screen.subsurface(pygame.Rect(VX, VY, VW, VH))
    S = VW / 540.0

    def px(v):
        return int(round(v * S))

    # --- шрифты (системный Roboto/Noto поддерживает кириллицу) ---
    def load_font(size, bold=False):
        names = ["Roboto-Regular.ttf", "NotoSans-Regular.ttf", "DroidSans.ttf"]
        if bold:
            names = ["Roboto-Bold.ttf", "NotoSans-Bold.ttf", "DroidSans-Bold.ttf"] + names
        for n in names:
            p = "/system/fonts/" + n
            if os.path.exists(p):
                try:
                    return pygame.font.Font(p, size)
                except Exception:
                    pass
        return pygame.font.Font(None, int(size * 1.15))

    F_TITLE = load_font(px(26))
    F_TEXT = load_font(px(22))
    F_SMALL = load_font(px(17))
    F_KEY = load_font(px(22), True)
    F_KEYS = load_font(px(15), True)

    # --- цвета ---
    BG = (17, 18, 20)
    CARD = (29, 30, 34)
    LINE = (48, 49, 56)
    TXT = (236, 236, 240)
    ME = (61, 123, 255)
    GREY = (138, 138, 147)
    RED = (255, 105, 105)
    GREEN = (110, 210, 140)
    AMBER = (240, 180, 60)

    # --- геометрия (в пикселях области 9:16) ---
    HEADER_H, INPUT_H = px(72), px(96)
    CHAT_R = pygame.Rect(0, HEADER_H, VW, VH - HEADER_H - INPUT_H)
    BAR_R = pygame.Rect(0, VH - INPUT_H, VW, INPUT_H)
    RESET_R = pygame.Rect(VW - px(16) - px(120), px(16), px(120), px(40))
    SEND_R = pygame.Rect(VW - px(16) - px(120), BAR_R.y + px(18), px(120), px(60))
    BOX_R = pygame.Rect(px(16), BAR_R.y + px(18), SEND_R.x - px(8) - px(16), px(60))
    GAP = px(10)
    ABOVE_KB = px(8)  # зазор между строкой ввода и клавиатурой

    # --- текст ---
    def clean(s):
        s = s.replace("\t", " ").replace("**", "").replace("`", "")
        return "".join(
            ch for ch in s
            if ord(ch) <= 0xFFFF and not (0xFE00 <= ord(ch) <= 0xFE0F)
            and not (0x200B <= ord(ch) <= 0x200F) and (ch == "\n" or ch.isprintable())
        )

    def wrap(text, font, max_w):
        lines = []
        for para in text.split("\n"):
            cur = ""
            for w in para.split(" "):
                test = (cur + " " + w) if cur else w
                if font.size(test)[0] <= max_w:
                    cur = test
                    continue
                if cur:
                    lines.append(cur)
                while font.size(w)[0] > max_w and len(w) > 1:
                    i = len(w)
                    while i > 1 and font.size(w[:i])[0] > max_w:
                        i -= 1
                    lines.append(w[:i])
                    w = w[i:]
                cur = w
            lines.append(cur)
        return lines

    # --- сообщения чата ---
    msgs = []

    def layout(m):
        text = clean(m["text"])
        if m["kind"] in ("tool", "err"):
            font = F_SMALL
            lines = wrap(text, font, px(500))
            m["w"] = max(font.size(l)[0] for l in lines)
            m["h"] = len(lines) * font.get_linesize()
        else:
            font, pad = F_TEXT, px(14)
            lines = wrap(text, font, px(540 * 0.78) - 2 * pad)
            m["w"] = max(font.size(l)[0] for l in lines) + 2 * pad
            m["h"] = len(lines) * font.get_linesize() + 2 * pad
        m["lines"] = lines

    def make_msg(kind, text):
        m = {"kind": kind, "text": text}
        layout(m)
        return m

    def add_msg(kind, text):
        m = make_msg(kind, text)
        msgs.append(m)
        return m

    def set_msg(m, kind, text):
        m["kind"], m["text"] = kind, text
        layout(m)

    def greet():
        if API_KEY:
            add_msg("bot", "Привет! Я умею считать, говорить время и вести заметки. Чем помочь?")
        else:
            add_msg("bot", "Привет! Вставь свой API-ключ OpenAI (начинается с sk-) "
                           "в поле ниже и нажми «Отправить». Он сохранится на телефоне.")

    greet()

    # --- состояние ---
    state = {
        "input": "", "active": True, "busy": False, "pending": None,
        "scroll": 0, "bottom": True, "toast": None,
    }
    results = queue.Queue()

    def worker(text):
        try:
            results.put(("ok",) + run_agent(text))
        except Exception as e:
            results.put(("err", str(e), []))

    def send():
        global API_KEY
        text = state["input"].strip()
        if not text or state["busy"]:
            return
        state["input"] = ""
        state["bottom"] = True
        if text == "/key":
            API_KEY = ""
            add_msg("bot", "Вставь новый API-ключ и нажми «Отправить».")
            return
        if not API_KEY:
            API_KEY = text
            save_api_key(text)
            add_msg("bot", "Ключ сохранён. Можно писать. (Сменить ключ: команда /key)")
            return
        add_msg("me", text)
        state["pending"] = add_msg("bot", "Думаю")
        state["busy"] = True
        threading.Thread(target=worker, args=(text,), daemon=True).start()

    def reset():
        if state["busy"]:
            return
        del MESSAGES[:]
        del msgs[:]
        state["scroll"] = 0
        add_msg("bot", "История очищена.")

    # --- клавиатура ---
    def kb_insert(text):
        text = text.replace("\r", " ").replace("\n", " ")
        state["input"] += "".join(ch for ch in text if ord(ch) <= 0xFFFF)

    def kb_backspace():
        state["input"] = state["input"][:-1]

    def kb_toast(msg, kind="ok"):
        color = GREEN if kind == "ok" else AMBER
        state["toast"] = (msg, color, pygame.time.get_ticks() + 1600)

    kb = VirtualKeyboard(px, F_KEY, F_KEYS, kb_insert, kb_backspace, send, kb_toast)
    kb.visible = True

    def set_keyboard(on):
        state["active"] = on
        state["bottom"] = True

    def apply_layout():
        """Клавиатура внизу, строка ввода чуть выше неё, чат — над строкой ввода."""
        kb.visible = state["active"]
        if kb.visible:
            kb.rect = pygame.Rect(0, VH - kb.height(), VW, kb.height())
            bar_y = kb.rect.y - INPUT_H - ABOVE_KB
        else:
            bar_y = VH - INPUT_H
        BAR_R.y = bar_y
        SEND_R.y = bar_y + px(18)
        BOX_R.y = bar_y + px(18)
        CHAT_R.y = HEADER_H
        CHAT_R.h = bar_y - HEADER_H

    def content_h():
        if not msgs:
            return 0
        return px(24) + sum(m["h"] for m in msgs) + GAP * (len(msgs) - 1)

    def max_scroll():
        return max(0, content_h() - CHAT_R.h)

    # --- отрисовка ---
    def draw_lines(lines, font, color, x, y):
        lh = font.get_linesize()
        for l in lines:
            if l:
                view.blit(font.render(l, True, color), (x, y))
            y += lh

    def draw_chat():
        view.set_clip(CHAT_R)
        y = CHAT_R.y + px(12) - state["scroll"]
        for m in msgs:
            h = m["h"]
            if y + h >= CHAT_R.top and y <= CHAT_R.bottom:
                kind, w = m["kind"], m["w"]
                if kind == "me":
                    r = pygame.Rect(VW - px(16) - w, y, w, h)
                    pygame.draw.rect(view, ME, r, border_radius=px(18))
                    draw_lines(m["lines"], F_TEXT, (255, 255, 255), r.x + px(14), r.y + px(14))
                elif kind == "bot":
                    r = pygame.Rect(px(16), y, w, h)
                    pygame.draw.rect(view, CARD, r, border_radius=px(18))
                    pygame.draw.rect(view, LINE, r, width=1, border_radius=px(18))
                    draw_lines(m["lines"], F_TEXT, TXT, r.x + px(14), r.y + px(14))
                elif kind == "tool":
                    draw_lines(m["lines"], F_SMALL, GREY, px(22), y)
                else:  # err
                    draw_lines(m["lines"], F_SMALL, RED, px(22), y)
            y += h + GAP
        view.set_clip(None)

        ms = max_scroll()
        if ms > 0:
            bar_h = max(px(30), int(CHAT_R.h * CHAT_R.h / content_h()))
            bar_y = CHAT_R.y + int((CHAT_R.h - bar_h) * state["scroll"] / ms)
            pygame.draw.rect(view, LINE, (VW - px(5), bar_y, px(3), bar_h), border_radius=px(2))

    def draw_header():
        pygame.draw.rect(view, CARD, (0, 0, VW, HEADER_H))
        pygame.draw.line(view, LINE, (0, HEADER_H - 1), (VW, HEADER_H - 1))
        t = F_TITLE.render("AI-агент", True, TXT)
        view.blit(t, (px(16), (HEADER_H - t.get_height()) // 2))
        pygame.draw.rect(view, LINE, RESET_R, border_radius=px(12))
        t = F_SMALL.render("Очистить", True, TXT)
        view.blit(t, t.get_rect(center=RESET_R.center))

    def draw_input():
        pygame.draw.rect(view, CARD, BAR_R)
        pygame.draw.line(view, LINE, (0, BAR_R.y), (VW, BAR_R.y))
        pygame.draw.rect(view, BG, BOX_R, border_radius=px(30))
        pygame.draw.rect(view, ME if state["active"] else LINE, BOX_R, width=1, border_radius=px(30))
        text = clean(state["input"])
        max_w = BOX_R.w - px(36)
        while text and F_TEXT.size(text)[0] > max_w:
            text = text[1:]
        if text:
            surf = F_TEXT.render(text, True, TXT)
            x_end = BOX_R.x + px(18) + surf.get_width()
            view.blit(surf, (BOX_R.x + px(18), BOX_R.centery - surf.get_height() // 2))
        else:
            surf = F_TEXT.render("Сообщение...", True, GREY)
            x_end = BOX_R.x + px(18)
            view.blit(surf, (x_end, BOX_R.centery - surf.get_height() // 2))
        if state["active"] and (pygame.time.get_ticks() // 500) % 2 == 0:
            pygame.draw.line(view, TXT, (x_end + 1, BOX_R.y + px(14)),
                             (x_end + 1, BOX_R.bottom - px(14)), 2)
        color = GREY if state["busy"] else ME
        pygame.draw.rect(view, color, SEND_R, border_radius=px(30))
        t = F_SMALL.render("Отправить", True, (255, 255, 255))
        view.blit(t, t.get_rect(center=SEND_R.center))

    def draw_toast():
        tt = state["toast"]
        if tt and pygame.time.get_ticks() < tt[2]:
            t = F_SMALL.render(tt[0], True, tt[1])
            view.blit(t, (px(16), BAR_R.y - t.get_height() - px(6)))

    # --- обработка касаний ---
    touch = {"down": None, "last_y": 0, "drag": False}
    hold = {"key": None, "t0": 0, "last": 0}

    def on_tap(pos):
        if RESET_R.collidepoint(pos):
            reset()
        elif SEND_R.collidepoint(pos):
            send()
        elif BOX_R.collidepoint(pos):
            set_keyboard(True)
        elif CHAT_R.collidepoint(pos) and state["active"]:
            set_keyboard(False)

    def local(pos):
        return (pos[0] - VX, pos[1] - VY)

    # --- главный цикл ---
    clock = pygame.time.Clock()
    running = True
    while running:
        events = pygame.event.get()
        has_ti = any(ev.type == pygame.TEXTINPUT for ev in events)
        for e in events:
            if e.type == pygame.QUIT:
                running = False
            elif e.type == pygame.TEXTINPUT:  # физическая клавиатура / отладка на ПК
                kb_insert(e.text)
            elif e.type == pygame.KEYDOWN:
                if e.key == pygame.K_BACKSPACE:
                    kb_backspace()
                elif e.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    send()
                elif not has_ti and getattr(e, "unicode", "") and e.unicode.isprintable():
                    kb_insert(e.unicode)
            elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
                p = local(e.pos)
                code = kb.press_at(p)
                if code is not None:  # попали в клавиатуру
                    touch["down"] = None
                    now = pygame.time.get_ticks()
                    hold.update(key=code if code == "BACK" else None, t0=now, last=now)
                else:
                    touch.update(down=p, last_y=p[1], drag=False)
            elif e.type == pygame.MOUSEMOTION and touch["down"] and e.buttons[0]:
                p = local(e.pos)
                if abs(p[1] - touch["down"][1]) > px(10):
                    touch["drag"] = True
                if touch["drag"] and CHAT_R.collidepoint(touch["down"]):
                    state["scroll"] -= p[1] - touch["last_y"]
                    state["bottom"] = False
                touch["last_y"] = p[1]
            elif e.type == pygame.MOUSEBUTTONUP and e.button == 1:
                hold["key"] = None
                if touch["down"] and not touch["drag"]:
                    on_tap(local(e.pos))
                touch["down"] = None
            elif e.type == pygame.MOUSEWHEEL:
                state["scroll"] -= e.y * px(40)
                state["bottom"] = False

        # автоповтор стирания при удержании BACK
        if hold["key"] == "BACK":
            now = pygame.time.get_ticks()
            if now - hold["t0"] > 450 and now - hold["last"] > 70:
                kb.press("BACK")
                hold["last"] = now

        # результат от агента
        try:
            status, text, tools = results.get_nowait()
            pend = state["pending"]
            idx = msgs.index(pend)
            for t in tools:
                msgs.insert(idx, make_msg("tool", "> " + t))
                idx += 1
            if status == "ok":
                set_msg(pend, "bot", text or "(пустой ответ)")
            else:
                set_msg(pend, "err", "Ошибка: " + text)
            state["busy"], state["pending"], state["bottom"] = False, None, True
        except queue.Empty:
            pass

        # анимация «Думаю...»
        if state["busy"] and state["pending"] is not None:
            dots = "." * (1 + (pygame.time.get_ticks() // 400) % 3)
            if state["pending"]["text"] != "Думаю" + dots:
                set_msg(state["pending"], "bot", "Думаю" + dots)

        apply_layout()
        ms = max_scroll()
        if state["bottom"]:
            state["scroll"] = ms
        state["scroll"] = max(0, min(state["scroll"], ms))

        screen.fill((0, 0, 0))
        view.fill(BG)
        draw_chat()
        draw_header()
        draw_input()
        kb.draw(view)
        draw_toast()
        pygame.display.flip()
        clock.tick(30)

    pygame.quit()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        err = traceback.format_exc()
        try:
            with open(os.path.join(BASE, "error_log.txt"), "w", encoding="utf-8") as f:
                f.write(err)
        except Exception:
            pass
        print(err)
        raise
