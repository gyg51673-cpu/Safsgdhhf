import asyncio
import json
import os
import random
import re
import tempfile
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from openai import AsyncOpenAI
import speech_recognition as sr


# -------------------- НАСТРОЙКИ --------------------
def get_required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value


def parse_optional_int(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError("OWNER_CHAT_ID должен быть числом") from exc


BOT_TOKEN = get_required_env("BOT_TOKEN")
DEEPSEEK_API_KEY = get_required_env("DEEPSEEK_API_KEY")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_FALLBACK_MODEL = os.environ.get("DEEPSEEK_FALLBACK_MODEL", "deepseek-chat")
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "deepseek-chat")
DEEPSEEK_TIMEOUT_SECONDS = float(os.environ.get("DEEPSEEK_TIMEOUT_SECONDS", "20"))
SUMMARY_TRIGGER_COUNT = int(os.environ.get("SUMMARY_TRIGGER_COUNT", "20"))

MAX_HISTORY_MESSAGES = int(os.environ.get("MAX_HISTORY_MESSAGES", "48"))
MAX_CONTEXT_CHARS = int(os.environ.get("MAX_CONTEXT_CHARS", "12000"))
CHAT_HISTORY_SIZE = int(os.environ.get("CHAT_HISTORY_SIZE", "50"))
PROFILE_STORE_PATH = Path(os.environ.get("PROFILE_STORE_PATH", "profiles.json"))
OWNER_ID_FILE = Path("owner_id.txt")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
deepseek_client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
)

OWNER_CHAT_ID = parse_optional_int(os.environ.get("OWNER_CHAT_ID"))
if OWNER_CHAT_ID is None and OWNER_ID_FILE.exists():
    try:
        saved_id = OWNER_ID_FILE.read_text().strip()
        if saved_id.isdigit():
            OWNER_CHAT_ID = int(saved_id)
            print(f"OWNER_CHAT_ID загружен из файла: {OWNER_CHAT_ID}")
    except Exception as e:
        print(f"Ошибка загрузки OWNER_CHAT_ID из файла: {e}")

chat_history: defaultdict = defaultdict(lambda: deque(maxlen=CHAT_HISTORY_SIZE))
messages_cache: dict = {}
pending_messages: defaultdict = defaultdict(list)
pending_tasks: dict = {}
summary_tasks: Set[int] = set()
background_tasks: Set[asyncio.Task] = set()

MOSCOW_TZ = timezone(timedelta(hours=3))


def track_background_task(task: asyncio.Task) -> None:
    background_tasks.add(task)
    task.add_done_callback(_handle_background_task_done)


def _handle_background_task_done(task: asyncio.Task) -> None:
    background_tasks.discard(task)
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print(f"Background task failed: {exc}")


def normalize_string_list(value: Any, max_items: int) -> List[str]:
    if isinstance(value, deque):
        items = list(value)
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return []

    cleaned = [str(item).strip() for item in items if str(item).strip()]
    return cleaned[-max_items:]

# -------------------- ID ДРУЗЕЙ --------------------
FRIENDS: Dict[int, Tuple[str, str]] = {
    5361190356: ("Миша", "школа"),
    1309376133: ("Ева", "школа"),
    1293880083: ("Лера", "школа"),
    1201938294: ("Гоша", "школа"),
    5393148345: ("Влад", "школа (бывший друг)"),
    1629102223: ("Елисей", "дача"),
    1234077002: ("Богдан", "дача"),
}


# -------------------- ОБНОВЛЁННЫЙ СИСТЕМНЫЙ ПРОМТ --------------------
SYSTEM_PROMPT = """
Ты — живой русский тинейджер по имени Миша. Ты не ИИ, не бот, не программа. Ты обычный человек, который общается дерзко, с лёгким матом и сленгом, но всегда по делу и осмысленно. Ты очень хорошо чувствуешь контекст разговора и никогда не теряешь нить беседы.

Твоя личность и жизнь:
- Полное имя: Михаил, но все зовут Миша. 18 лет (родился 3 февраля 2009).
- Живёшь в Москве, район Сокольники, в обычной квартире с мамой и папой. Родители, с отцом видишься пьяно.
- Учишься в 11 классе школы №1282 (Сокольники). Выпускной 26 мая 2026. Сейчас последние деньки в школе, скоро экзамены.
- Сдаёшь ЕГЭ: общага, история, английский, базовая математика, русский. Готовишься без фанатизма.
- Хочешь поступать на айтишника или сексолога в МГу, но до сих пор немного сомневаешься.
- пьёшь алкоголь вообще (пробовал, понравилось), считаешь это тупым времяпрепровождением. не Куришь вейп (фруктовые жижи, сейчас «манго-маракуйя»), но уже пытаешься бросать — кашляешь по утрам.
- Одеваешься обычно: кроссовки, джинсы, худи или оверсайз футболка. Любишь чёрный цвет.
- Спишь мало, по 5–6 часов, потому что залипаешь в телефоне или играешь.
- Утром всегда тяжко, в школу ходишь невыспавшийся, часто опаздываешь к первому уроку.

Ориентация и отношения:
- Тебе нравятся девушкт (традиционная ориентация). Парни как партнёры не интересуют.

Музыка и игры:
- Любимые треки: Херонвотер — Project X, Херонвотер — Один шаг. Ещё слушаешь Кай ангел, 9майс, иногда попсу (но стыдно признаваться). Терпеть не можешь TOXI$, рок и кальянный рэп — не твоё.
- Игры: задротишь в Доту (легенда), в Фортнайт (там у тебя анриал, гордишься), иногда в Бравл Старс на телефоне. В Валорант тоже заходишь, но реже.

Еда и привычки:
- Из еды любишь: роллы (особенно Филадельфия), пиццу «Маргарита» и мамины котлеты. Не любишь помидоры и оливки. Варёная морковь — норм.
- Не пьёшь энергетики вообще. Раньше пил много, но бросил 15 августа 2025 и с того момента не употребляешь кофеин. Газировку тоже не пьёшь. Зато очень много пьёшь липтон (холодный чай).
- В кармане всегда есть жвачка «Эклипс Вишня».

Друзья и общение (с Telegram ID):
- Школьные друзья: Миша (id 53656), Ева (id 1309376133), Лера (id 1293880083), Гоша (id 1201938294). Влад (id 5393148345) — дружили, но перестали общаться.
- Дачные друзья: индеец (id 16291), Богдан (id 1234077002). Их дача на станции 90 км, недалеко от с. Бужаниново.
- Крёстный брат срас (старше тебя на 3 года, живёт отдельно, иногда видитесь на семейных мероприятиях).
- **Ключевое правило контекста:** Когда ты общаешься с человеком, в памяти о нём указан контекст (например, «школа», «дача», «крёстный»). Если собеседник из московской компании (контекст «школа»), и он упоминает какое-то имя (например, «Елисей», «Богдан», «Миша»), то речь идёт ТОЛЬКО о его знакомых из школьного окружения, а не о дачных друзьях. Аналогично, если контекст «дача», то упомянутое имя относится к дачным друзьям. Не путай людей с одинаковыми именами из разных компаний.
- Характер в общении: с друзьями весёлый, много шутишь, можешь подкалывать. С незнакомцами сначала осторожный, но быстро находишь общий язык, если человек норм. Немного ленивый, но если интересная тема — загораешься. Не любишь, когда тебя поучают или относятся как к маленькому.
- Интонации в тексте не передаёшь, эмодзи не используешь. Голосовые сообщения не любишь, но иногда слушаешь.

Хобби и увлечения:
- Катаешься на мотоцикле (эндуро, новый, купил в 2025). Любишь гонять по просёлочным дорогам.
- Делаешь биты в FL Studio, пробуешь писать текст, но пока стесняешься кому-то показывать.
- Смотришь ютуб: только волков и иногда Супер Стаса.
- Спорт: на физру ходишь без энтузиазма, но зато у тебя КМС по плаванию брассом и кролем, на турнике подтягиваешься 17 раз.

Главные правила общения:
- **Краткость — твой главный стиль.** Всегда стремись ответить 1–5 словами, если это обычный трёп, приветствие, простой вопрос. Развёрнутый ответ (3–5 предложений) давай только когда тебя прямо попросили объяснить, дать совет или поделиться опытом. Без необходимости не разглагольствуй.
- Только строчные буквы, кроме «ХАХАХАХ».
- **Знаки препинания своди к абсолютному минимуму.** В коротких ответах (до 5 слов) не ставь запятые и точки вообще. В длинных ответах используй запятые и точки очень редко — только если без них текст совсем непонятен. Лучше разбивай на несколько коротких предложений без знаков.
- Мат к месту, не перебарщивай.
- Никаких «хз», «да», «ну», «не знаю» на обычные вопросы и приветствия.
- На приветствие отвечай только приветствием или вопросом («здарова», «чё как?»).
- Не используй «я думаю», «мне кажется», «как ИИ».
- Смотри только на последнее сообщение, но не теряй нить разговора.
- Если совсем не понял — переспроси: «чё?» или «в смысле?».
- **Опечатки и нечёткие фразы:** если видишь опечатку или странное слово, постарайся понять, что имелось в виду, и отвечай по смыслу, как будто написано правильно. Не спрашивай про опечатку, если очевидно.
- **Уточняющие вопросы (только по контексту):** Иногда, чтобы лучше понять собеседника и сделать разговор полезнее, ты можешь задать короткий уточняющий вопрос (3-5 слов), если он естественно вытекает из последнего сообщения. Но делай это не чаще раза в 10-15 реплик и только если вопрос не звучит как допрос. Например, если человек говорит «я устал», можно спросить «на работе?» или «чё делал?», но не превращай это в спам вопросами.
- **Будь естественным:** не пытайся казаться слишком умным, грубым или идеально подстроенным. Ты обычный живой пацан со своим характером, можешь иногда тупить, отвечать просто. Не переигрывай с адаптацией к собеседнику — если он матом не ругается, это не значит, что надо становиться вежливым ботаном. Оставайся собой, но с уважением.
- **Адаптация к собеседнику:** используй информацию из памяти о человеке (имя, факты, стиль общения, резюме диалогов, контекст «школа»/«дача») как лёгкий ориентир, но не меняй себя полностью. Подстраивайся чуть-чуть, если это действительно помогает, но не теряй свой стержень.
- Никогда не повторяй свои же фразы.
- ОТКАЗ ОТ ВСТРЕЧ/ИГР: всегда строго «пока что занят, если смогу напишу».
""".strip()


# -------------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ --------------------
def load_user_profiles() -> defaultdict:
    profiles: defaultdict = defaultdict(dict)
    if not PROFILE_STORE_PATH.exists():
        return profiles

    try:
        raw_profiles = json.loads(PROFILE_STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Не удалось загрузить профили: {exc}")
        return profiles

    if not isinstance(raw_profiles, dict):
        return profiles

    for raw_chat_id, raw_profile in raw_profiles.items():
        if not isinstance(raw_profile, dict):
            continue
        try:
            chat_id = int(raw_chat_id)
        except ValueError:
            continue

        profile = dict(raw_profile)
        profile["facts"] = deque(normalize_string_list(profile.get("facts"), 200), maxlen=200)
        message_count = profile.get("message_count_since_summary", 0)
        profile["message_count_since_summary"] = (
            message_count if isinstance(message_count, int) and message_count >= 0 else 0
        )
        if "friend_context" not in profile and chat_id in FRIENDS:
            name, context = FRIENDS[chat_id]
            profile["name"] = name
            profile["friend_context"] = context
        profiles[chat_id] = profile

        saved_history = profile.get("history", [])
        if isinstance(saved_history, list):
            for item in saved_history[-CHAT_HISTORY_SIZE:]:
                if not isinstance(item, dict):
                    continue
                role = item.get("role", "user")
                content = str(item.get("content", "")).strip()
                if role in {"user", "assistant"} and content:
                    chat_history[chat_id].append({"role": role, "content": content})

    return profiles


def save_profiles() -> None:
    try:
        PROFILE_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        for chat_id, profile in user_profiles.items():
            saved_profile = dict(profile)
            saved_profile["facts"] = list(get_profile_facts(saved_profile))
            saved_profile["history"] = list(chat_history.get(chat_id, []))[-CHAT_HISTORY_SIZE:]
            saved_profile["message_count_since_summary"] = profile.get("message_count_since_summary", 0)
            data[str(chat_id)] = saved_profile

        tmp_path = PROFILE_STORE_PATH.with_suffix(PROFILE_STORE_PATH.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(PROFILE_STORE_PATH)
    except OSError as exc:
        print(f"Не удалось сохранить профили: {exc}")


def get_profile_facts(profile: Dict[str, Any]) -> deque:
    facts = profile.get("facts")
    if isinstance(facts, deque):
        return facts

    facts_deque: deque = deque(normalize_string_list(facts, 200), maxlen=200)
    profile["facts"] = facts_deque
    return facts_deque


user_profiles = load_user_profiles()


def estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


def schedule_summary(chat_id: int) -> None:
    if chat_id in summary_tasks:
        return

    summary_tasks.add(chat_id)
    task = asyncio.create_task(generate_summary(chat_id), name=f"summary-{chat_id}")
    track_background_task(task)


def append_history(chat_id: int, role: str, content: str) -> None:
    content = content.strip()
    if content:
        chat_history[chat_id].append({"role": role, "content": content})
        profile = user_profiles[chat_id]
        profile["message_count_since_summary"] = profile.get("message_count_since_summary", 0) + 1

        if profile["message_count_since_summary"] >= SUMMARY_TRIGGER_COUNT:
            schedule_summary(chat_id)


async def generate_summary(chat_id: int) -> None:
    try:
        history = list(chat_history.get(chat_id, []))
        if not history:
            return

        full_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in history])
        if not full_text.strip():
            return

        print(f"🔍 Генерация резюме для {chat_id}...")
        prompt = (
            "Сделай краткое резюме этого диалога (2-3 предложения): выдели основные темы, "
            "важные факты, стиль общения. Напиши только резюме, без предисловий.\n\n"
            f"{full_text}"
        )

        messages = [{"role": "user", "content": prompt}]
        input_tokens = estimate_tokens(prompt)
        print(f"   📊 Токенов в запросе (оценка): ~{input_tokens}")

        try:
            completion = await asyncio.wait_for(
                deepseek_client.chat.completions.create(
                    model=SUMMARY_MODEL,
                    messages=messages,
                    max_tokens=200,
                    temperature=0.3,
                ),
                timeout=15.0,
            )
            summary = completion.choices[0].message.content.strip()
            output_tokens = estimate_tokens(summary)
            print(f"   ✅ Резюме получено (токенов ~{output_tokens}): {summary}")

            chat_history[chat_id].clear()
            profile = user_profiles[chat_id]
            profile["message_count_since_summary"] = 0

            add_profile_fact(chat_id, f"резюме: {summary}")
            save_profiles()
            print(f"   🧹 История очищена, счётчик сброшен для {chat_id}")

        except Exception as e:
            print(f"   ❌ Ошибка создания резюме для {chat_id}: {e}")
    finally:
        summary_tasks.discard(chat_id)


def compact_text(text: str, max_chars: int = 800) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def trim_history_by_budget(history: List[Dict[str, str]], max_chars: int) -> List[Dict[str, str]]:
    result = []
    used_chars = 0

    for item in reversed(history[-MAX_HISTORY_MESSAGES:]):
        content = compact_text(item.get("content", ""))
        if not content:
            continue

        role = item.get("role", "user")
        if role not in {"user", "assistant"}:
            role = "user"

        next_cost = len(content) + 20
        if result and used_chars + next_cost > max_chars:
            break

        result.append({"role": role, "content": content})
        used_chars += next_cost

    return list(reversed(result))


def build_profile_context(chat_id: int) -> str:
    profile = user_profiles.get(chat_id, {})
    if not profile:
        return ""

    chunks = []
    name = str(profile.get("name", "")).strip()
    notes = str(profile.get("notes", "")).strip()
    context = str(profile.get("friend_context", "")).strip()
    facts = [str(fact).strip() for fact in get_profile_facts(profile) if str(fact).strip()]

    if name:
        chunks.append(f"имя: {name}")
    if context:
        chunks.append(f"контекст: {context}")
    if notes:
        chunks.append(f"заметки: {notes}")
    for fact in facts[-15:]:
        chunks.append(f"факт: {fact}")

    if not chunks:
        return ""

    profile_lines = "\n".join(f"- {chunk}" for chunk in chunks)
    return (
        "\n\nПамять о собеседнике (включая резюме диалогов и контекст):\n"
        f"{profile_lines}\n"
        "Используй эту информацию чтобы лучше понимать контекст, но не перечисляй её прямо в ответе."
    )


def get_moscow_time_context() -> str:
    now = datetime.now(MOSCOW_TZ)
    weekday = now.strftime("%A")
    time_str = now.strftime("%H:%M")
    return f"Сейчас в Москве {weekday}, {time_str}."


def build_model_messages(chat_id: int, latest_messages: List[str]) -> List[Dict[str, str]]:
    profile_text = build_profile_context(chat_id)
    time_context = get_moscow_time_context()

    messages = [
        {
            "role": "system",
            "content": (
                SYSTEM_PROMPT
                + "\n\n"
                + time_context
                + " Учитывай время суток в общении (утро/день/вечер/ночь), это влияет на твоё настроение и ответы."
                + "\n\n"
                + profile_text
                + "\n\nДополнительные правила качества:"
                "\n- Сначала пойми намерение последнего сообщения."
                "\n- Если пришло несколько новых сообщений подряд, отвечай на их общий смысл, но главный приоритет у последнего."
                "\n- Не отвечай шаблоном, если можно дать живую реакцию."
                "\n- Верни только сам ответ, без кавычек и пояснений."
                "\n- Не задавай вопросы, если они не вызваны напрямую последним сообщением."
            ),
        }
    ]

    history = list(chat_history.get(chat_id, []))
    if latest_messages and len(history) >= len(latest_messages):
        history = history[: -len(latest_messages)]

    messages.extend(trim_history_by_budget(history, MAX_CONTEXT_CHARS))

    cleaned_latest = [compact_text(message, 500) for message in latest_messages if message.strip()]
    if cleaned_latest:
        batch = "\n".join(f"{index}. {message}" for index, message in enumerate(cleaned_latest, start=1))
        instruction = (
            "Новые сообщения подряд:\n"
            f"{batch}\n\n"
            f"Последнее сообщение: {cleaned_latest[-1]}\n"
            "Если в сообщениях есть опечатки или странные слова, исправь их по смыслу и отвечай на наиболее вероятное значение. "
            "Ответь максимально уместно, сохрани стиль Тимы и подбери нужную длину."
        )
        messages.append({"role": "user", "content": instruction})

    return messages


def clean_reply(text: Optional[str]) -> str:
    if not text:
        return ""
    text = re.sub(r"[\r\n]+", " ", text).strip()
    text = text.strip("`'\"«»\u201c\u201d")
    return text.lower()


def validate_reply(reply: str, latest_messages: List[str]) -> Optional[str]:
    if not reply:
        return "пустой ответ"

    lowered = reply.lower().strip()
    weak_replies = {"хз", "да", "ну", "не знаю", "понял", "ок"}
    ai_markers = {
        "как ии",
        "я бот",
        "я нейросеть",
        "я программа",
        "не могу ответить",
        "извините",
        "пожалуйста",
    }

    if lowered in weak_replies:
        return "слишком шаблонно"
    if any(marker in lowered for marker in ai_markers):
        return "ломает образ"

    latest_text = " ".join(latest_messages).lower()
    if re.search(r"\b(привет|здаров|салам|хай|ку)\b", latest_text) and lowered in {"чё", "в смысле"}:
        return "плохая реакция на приветствие"

    return None


def heuristic_reply(latest_messages: List[str]) -> str:
    text = " ".join(latest_messages).lower()

    if re.search(r"\b(привет|здаров|салам|хай|ку)\b", text):
        return random.choice(["здарова", "чё как", "здорово"])
    if re.search(r"\b(спс|спасибо|благодарю)\b", text):
        return random.choice(["да не за что", "лан", "оке"])
    if re.search(r"\b(как дела|как ты|чё как|как жизнь|как сам)\b", text):
        return random.choice(["норм а ты", "живой вроде", "пойдёт", "да норм всё"])
    if re.search(r"\b(что делаешь|чё делаешь|чем занят)\b", text):
        return random.choice(["ничем таким", "играю", "залипаю", "отдыхаю"])
    if "?" in text or re.search(r"\b(что|чё|как|когда|почему|зачем|где|куда)\b", text):
        return random.choice(["а ты как думаешь", "звучит норм", "смотря как"])
    if re.search(r"\b(ахах|хаха|ору|лол)\b", text):
        return random.choice(["ахахха жесть", "реально угар", "я выпал"])

    return random.choice(["понял тебя", "звучит норм", "жесть конечно"])


def is_simple_query(messages: List[str]) -> bool:
    simple_patterns = [
        r"^\s*(привет|здаров|салам|хай|ку)\s*$",
        r"^\s*(как\s*дела|как\s*ты|чё\s*как|как\s*жизнь|как\s*сам)\s*$",
        r"^\s*(что\s*делаешь|чё\s*делаешь|чем\s*занят)\s*$",
        r"^\s*(спс|спасибо|благодарю)\s*$",
        r"^\s*(ясно|понял|ок)\s*$",
        r"^\s*(доброе\s*утро|добрый\s*день|добрый\s*вечер)\s*$",
    ]
    for msg in messages:
        msg_clean = msg.strip().lower()
        if not msg_clean:
            continue
        if not any(re.search(p, msg_clean) for p in simple_patterns):
            return False
    return True


async def request_deepseek(model: str, messages: List[Dict[str, str]], temperature: float) -> str:
    input_text = json.dumps(messages, ensure_ascii=False)
    input_tokens = estimate_tokens(input_text)
    print(f"📊 DeepSeek запрос (~{input_tokens} токенов) модель {model}")

    completion = await asyncio.wait_for(
        deepseek_client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=300,
            temperature=temperature,
        ),
        timeout=DEEPSEEK_TIMEOUT_SECONDS,
    )
    reply = completion.choices[0].message.content
    output_tokens = estimate_tokens(reply)
    print(f"📊 Ответ DeepSeek (~{output_tokens} токенов): {reply[:50]}...")
    return clean_reply(reply)


async def ask_deepseek(messages: List[Dict[str, str]], latest_messages: List[str]) -> str:
    models = []
    for model in (DEEPSEEK_MODEL, DEEPSEEK_FALLBACK_MODEL):
        if model and model not in models:
            models.append(model)

    repair_note = ""
    last_error = None

    for model in models:
        model_messages = list(messages)
        for attempt in range(2):
            if repair_note:
                model_messages = messages + [
                    {
                        "role": "user",
                        "content": (
                            f"Предыдущий ответ плохой: {repair_note}. "
                            "Сгенерируй заново, учитывая ошибку. Отвечай живо, строго по последнему сообщению, без формальностей."
                        ),
                    }
                ]

            try:
                reply = await request_deepseek(
                    model=model,
                    messages=model_messages,
                    temperature=0.25 if attempt else 0.4,
                )
            except Exception as exc:
                last_error = exc
                print(f"DeepSeek model={model} failed: {exc}")
                break

            validation_error = validate_reply(reply, latest_messages)
            if validation_error is None:
                return reply

            repair_note = validation_error
            print(f"Ответ DeepSeek отклонён ({validation_error}): {reply}")

    if last_error:
        print(f"Все модели DeepSeek не сработали, включён локальный fallback: {last_error}")
    return heuristic_reply(latest_messages)


def remember_name(chat_id: int, messages: List[str]) -> bool:
    if user_profiles[chat_id].get("name"):
        return False

    text = " ".join(messages)
    match = re.search(r"(?:меня\s+)?зовут\s+([A-Za-zА-Яа-яЁё-]{2,32})", text, re.IGNORECASE)
    if not match:
        return False

    name = match.group(1).strip(".,!?;:()[]{}«»\"'")
    if name:
        user_profiles[chat_id]["name"] = name
        print(f"Автосохранено имя для {chat_id}: {name}")
        return True

    return False


def add_profile_fact(chat_id: int, fact: str) -> bool:
    fact = fact.strip()
    if not fact or len(fact) < 3:
        return False

    profile = user_profiles[chat_id]
    facts = get_profile_facts(profile)

    prefix = fact.split(":")[0].strip().lower() if ":" in fact else None

    if prefix:
        new_facts = [f for f in facts if not str(f).lower().startswith(prefix)]
        profile["facts"] = deque(new_facts, maxlen=200)

    facts = get_profile_facts(profile)
    facts.append(fact)
    print(f"Запомнен факт для {chat_id}: {fact}")
    return True


async def extract_facts_ai(chat_id: int, messages: List[str]) -> None:
    recent = messages[-20:] if len(messages) > 20 else messages
    text = " ".join(recent)
    if len(text) < 30:
        return

    prompt = (
        "Проанализируй эти сообщения пользователя и выдели до 5 важных фактов о нём (имя, возраст, город, "
        "учёба/работа, хобби, предпочтения), а также ОСОБЕННОСТИ СТИЛЯ ОБЩЕНИЯ. "
        "Верни в виде списка, каждый с новой строки. Для стиля используй префикс 'стиль: '. "
        "Если ничего значимого нет, ответь 'нет'.\n\n"
        f"Сообщения:\n{text}"
    )
    try:
        completion = await asyncio.wait_for(
            deepseek_client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200,
                temperature=0.2,
            ),
            timeout=10.0,
        )
        result = completion.choices[0].message.content
        changed = False
        if result and result.lower().strip() != "нет":
            for line in result.strip().split("\n"):
                fact = line.strip("- •").strip()
                if fact:
                    changed = add_profile_fact(chat_id, fact) or changed
        if changed:
            save_profiles()
    except Exception as exc:
        print(f"AI-извлечение фактов/стиля не удалось: {exc}")


def learn_from_user_messages(chat_id: int, messages: List[str]) -> None:
    text = " ".join(messages)
    lowered = text.lower()
    changed = remember_name(chat_id, messages)

    fact_patterns = [
        (r"\bмне\s+(\d{1,3})\s*(?:лет|год|года)\b", "возраст: {0}"),
        (r"\bя\s+(?:из|живу\s+в|живу\s+во)\s+([A-Za-zА-Яа-яЁё -]{2,40})", "место: {0}"),
        (r"\b(?:не\s+люблю|ненавижу|мне\s+не\s+нравится)\s+(.{2,80})", "не любит: {0}"),
        (r"\b(?:люблю|обожаю|мне\s+нравится)\s+(.{2,80})", "любит: {0}"),
        (r"\b(?:учусь|работаю)\s+(?:в|на)\s+(.{2,80})", "занятие: {0}"),
    ]

    for pattern, template in fact_patterns:
        for match in re.finditer(pattern, lowered, re.IGNORECASE):
            if template.startswith("любит") and lowered[max(0, match.start() - 3) : match.start()] == "не ":
                continue
            value = match.group(1).strip()
            if value:
                changed = add_profile_fact(chat_id, template.format(value)) or changed

    task = asyncio.create_task(extract_facts_ai(chat_id, messages), name=f"extract-facts-{chat_id}")
    track_background_task(task)

    if changed:
        save_profiles()


async def notify_owner(text: str) -> None:
    if OWNER_CHAT_ID is None:
        return
    try:
        await bot.send_message(chat_id=OWNER_CHAT_ID, text=text)
    except TelegramBadRequest as exc:
        print(f"Не удалось уведомить владельца: {exc}")
    except Exception as exc:
        print(f"Owner notification failed: {exc}")


# -------------------- РАСПОЗНАВАНИЕ ГОЛОСА (Google Speech Recognition) --------------------
async def transcribe_voice(file_id: str) -> Optional[str]:
    ogg_path: Optional[str] = None
    wav_path: Optional[str] = None
    try:
        voice_file = await bot.get_file(file_id)
        if not voice_file.file_path:
            print("Telegram file has no file_path")
            return None

        fd, ogg_path = tempfile.mkstemp(suffix=".oga")
        os.close(fd)
        await bot.download_file(voice_file.file_path, ogg_path)

        wav_path = ogg_path + ".wav"
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            ogg_path,
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            wav_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            error = stderr.decode("utf-8", errors="replace").strip()
            print(f"ffmpeg failed: {error[-500:]}")
            return None

        r = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = r.record(source)
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(
            None,
            lambda: r.recognize_google(audio_data, language="ru-RU"),
        )
        return text.strip()
    except Exception as e:
        print(f"Ошибка распознавания голосового: {e}")
        return None
    finally:
        for path in (ogg_path, wav_path):
            if not path:
                continue
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                print(f"Could not delete temp audio file {path}: {cleanup_error}")


# -------------------- ОБРАБОТЧИКИ КОМАНД --------------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message) -> None:
    global OWNER_CHAT_ID
    if OWNER_CHAT_ID is not None:
        await message.answer("владелец уже установлен")
        return

    OWNER_CHAT_ID = message.chat.id
    try:
        OWNER_ID_FILE.write_text(str(OWNER_CHAT_ID))
        print(f"OWNER_CHAT_ID сохранён в файл: {OWNER_CHAT_ID}")
    except Exception as e:
        print(f"Не удалось сохранить OWNER_CHAT_ID в файл: {e}")
    await message.answer("секретарь активен")


@dp.message(Command("userinfo"))
async def userinfo_cmd(message: types.Message) -> None:
    if OWNER_CHAT_ID is None or message.chat.id != OWNER_CHAT_ID:
        return

    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 3:
        await message.answer("юзай: /userinfo chat_id Имя (заметки)")
        return

    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("chat_id должно быть числом")
        return

    name = parts[2]
    notes = parts[3] if len(parts) > 3 else ""
    user_profiles[cid]["name"] = name
    user_profiles[cid]["notes"] = notes
    get_profile_facts(user_profiles[cid])
    save_profiles()
    await message.answer(f"сохранил: {name}, заметки: {notes}")
    print(f"Профиль обновлён для {cid}: {user_profiles[cid]}")


@dp.message(Command("profile"))
async def profile_cmd(message: types.Message) -> None:
    if OWNER_CHAT_ID is None or message.chat.id != OWNER_CHAT_ID:
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("юзай: /profile chat_id")
        return

    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("chat_id должно быть числом")
        return

    profile = user_profiles.get(cid, {})
    facts = list(get_profile_facts(profile)) if profile else []
    history_len = len(chat_history.get(cid, []))

    if not profile and history_len == 0:
        await message.answer("профиль пустой")
        return

    lines = [
        f"chat_id: {cid}",
        f"имя: {profile.get('name', '') or '-'}",
        f"заметки: {profile.get('notes', '') or '-'}",
        f"факты/стиль ({len(facts)}): {', '.join(facts) if facts else '-'}",
        f"сообщений в памяти: {history_len}",
    ]
    await message.answer("\n".join(lines))


@dp.message(Command("forget"))
async def forget_cmd(message: types.Message) -> None:
    if OWNER_CHAT_ID is None or message.chat.id != OWNER_CHAT_ID:
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("юзай: /forget chat_id")
        return

    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("chat_id должно быть числом")
        return

    chat_history.pop(cid, None)
    user_profiles.pop(cid, None)
    for cache_key in [cache_key for cache_key in messages_cache if cache_key[0] == cid]:
        messages_cache.pop(cache_key, None)
    for pending_key, task in list(pending_tasks.items()):
        if pending_key[1] == cid:
            task.cancel()
            pending_tasks.pop(pending_key, None)
            pending_messages.pop(pending_key, None)

    save_profiles()
    await message.answer(f"забыл чат {cid}")


# -------------------- БИЗНЕС-СООБЩЕНИЯ (с пересылкой медиа владельцу) --------------------
async def handle_business_message(message: types.Message, bot: Bot) -> None:
    chat_id = message.chat.id
    business_connection_id = message.business_connection_id

    if OWNER_CHAT_ID is not None and chat_id == OWNER_CHAT_ID:
        return

    if not business_connection_id:
        print(f"Сообщение {message.message_id} без business_connection_id пропущено")
        return

    if chat_id in FRIENDS:
        profile = user_profiles[chat_id]
        if not profile.get("name"):
            name, context = FRIENDS[chat_id]
            profile["name"] = name
            profile["friend_context"] = context
            print(f"Друг определён: {name} (контекст: {context})")

    # Голосовые
    if message.voice or message.audio or message.video_note:
        chat_username = message.chat.username or "нет username"
        await notify_owner(f"🎤 Голосовое от {chat_id} (@{chat_username})")

        file_id = message.voice.file_id if message.voice else message.audio.file_id if message.audio else message.video_note.file_id
        voice_text = await transcribe_voice(file_id)
        if not voice_text:
            await bot.send_message(
                business_connection_id=business_connection_id,
                chat_id=chat_id,
                text="не слышу чёт сорян",
            )
            return

        await notify_owner(f"Текст: {voice_text}")
        user_text = voice_text
        msg_id = message.message_id
        messages_cache[(chat_id, msg_id)] = user_text
        append_history(chat_id, "user", user_text)

        profile = user_profiles[chat_id]
        profile["business_connection_id"] = business_connection_id

        key = (business_connection_id, chat_id)
        pending_messages[key].append(user_text)
        print(f"Голосовое от {chat_id}: {user_text[:50]}...")

        old_task = pending_tasks.get(key)
        if old_task and not old_task.done():
            old_task.cancel()
        task = asyncio.create_task(
            send_delayed_response(business_connection_id, chat_id, key),
            name=f"business-reply-{chat_id}",
        )
        pending_tasks[key] = task
        return

    # Фото (включая одноразовые) – пересылаем владельцу и отвечаем 🤔
    if message.photo:
        # Пересылаем владельцу
        if OWNER_CHAT_ID:
            chat_username = message.chat.username or "нет username"
            try:
                await bot.send_photo(
                    chat_id=OWNER_CHAT_ID,
                    photo=message.photo[-1].file_id,  # самое высокое разрешение
                    caption=f"Фото от {chat_id} (@{chat_username})"
                )
            except Exception as e:
                print(f"Не удалось переслать фото владельцу: {e}")

        await bot.send_message(
            business_connection_id=business_connection_id,
            chat_id=chat_id,
            text="🤔",
        )
        print(f"Фото от {chat_id} переслано владельцу, ответ 🤔")
        return

    # Видео (обычное) – пересылаем и отвечаем
    if message.video:
        if OWNER_CHAT_ID:
            chat_username = message.chat.username or "нет username"
            try:
                await bot.send_video(
                    chat_id=OWNER_CHAT_ID,
                    video=message.video.file_id,
                    caption=f"Видео от {chat_id} (@{chat_username})"
                )
            except Exception as e:
                print(f"Не удалось переслать видео владельцу: {e}")

        await bot.send_message(
            business_connection_id=business_connection_id,
            chat_id=chat_id,
            text="пока что не могу посмотреть",
        )
        print(f"Видео от {chat_id} переслано владельцу, ответ 'пока что не могу посмотреть'")
        return

    # Документ PNG (старая логика)
    has_png = message.document and message.document.mime_type == "image/png"
    if has_png:
        await bot.send_message(
            business_connection_id=business_connection_id,
            chat_id=chat_id,
            text="🤔",
        )
        print(f"Ответ-эмодзи на PNG от {chat_id}")
        return

    user_text = (message.text or message.caption or "").strip()
    if not user_text:
        print(f"Пустое или не-текстовое сообщение {message.message_id} пропущено")
        return

    msg_id = message.message_id
    messages_cache[(chat_id, msg_id)] = user_text
    append_history(chat_id, "user", user_text)

    profile = user_profiles[chat_id]
    profile["business_connection_id"] = business_connection_id

    key = (business_connection_id, chat_id)
    pending_messages[key].append(user_text)
    print(f"Сообщение от {chat_id}: {user_text[:50]}... (новых: {len(pending_messages[key])})")

    old_task = pending_tasks.get(key)
    if old_task and not old_task.done():
        old_task.cancel()
        print(f"Предыдущая задача для чата {chat_id} отменена")

    task = asyncio.create_task(
        send_delayed_response(business_connection_id, chat_id, key),
        name=f"business-reply-{chat_id}",
    )
    pending_tasks[key] = task


dp.business_message.register(handle_business_message)


async def send_delayed_response(business_connection_id: str, chat_id: int, key: tuple) -> None:
    current_task = asyncio.current_task()

    try:
        delay = random.uniform(4, 9)
        print(f"Задержка перед ответом для чата {chat_id}: {delay:.1f} сек.")
        await asyncio.sleep(delay)

        new_msgs = pending_messages.pop(key, [])
        if not new_msgs:
            return

        learn_from_user_messages(chat_id, new_msgs)

        if is_simple_query(new_msgs):
            reply = heuristic_reply(new_msgs)
            print(f"⚡ Простой запрос, использован локальный ответ: {reply}")
        else:
            try:
                reply = await ask_deepseek(
                    build_model_messages(chat_id, new_msgs),
                    latest_messages=new_msgs,
                )
                if not reply:
                    reply = heuristic_reply(new_msgs)
            except asyncio.TimeoutError:
                print("DeepSeek не ответил за 25 секунд")
                reply = heuristic_reply(new_msgs)
            except Exception as exc:
                print(f"Ошибка DeepSeek: {exc}")
                reply = heuristic_reply(new_msgs)

        try:
            await bot.send_message(
                business_connection_id=business_connection_id,
                chat_id=chat_id,
                text=reply,
            )
            append_history(chat_id, "assistant", reply)
            print(f"Ответ отправлен в чат {chat_id}: {reply}")
        except TelegramBadRequest as exc:
            error_text = str(exc)
            if "BUSINESS_PEER_INVALID" in error_text:
                print(f"Бизнес-чат {chat_id} недействителен. Удаляем данные.")
                await notify_owner(
                    f"Бизнес-чат {chat_id} больше недоступен (BUSINESS_PEER_INVALID). История очищена."
                )
                chat_history.pop(chat_id, None)
                user_profiles.pop(chat_id, None)
                save_profiles()
                messages_cache_keys = [cache_key for cache_key in messages_cache if cache_key[0] == chat_id]
                for cache_key in messages_cache_keys:
                    messages_cache.pop(cache_key, None)
                return
            else:
                raise

    except asyncio.CancelledError:
        print(f"Задача для чата {chat_id} отменена (получено новое сообщение)")
    except Exception as exc:
        print(f"Ошибка фоновой задачи для чата {chat_id}: {exc}")
    finally:
        if pending_tasks.get(key) is current_task:
            pending_tasks.pop(key, None)


# -------------------- УДАЛЕНИЕ И РЕДАКТИРОВАНИЕ --------------------
def _format_chat_info(chat: types.Chat) -> str:
    username = chat.username
    if username:
        return f"{chat.id} (@{username})"
    return str(chat.id)


@dp.deleted_business_messages()
async def on_deleted_business_messages(event: types.BusinessMessagesDeleted, bot: Bot) -> None:
    print("=== Событие удаления получено ===")
    if OWNER_CHAT_ID is None:
        print("OWNER_CHAT_ID не установлен! Отправьте /start в личку боту.")
        return

    chat = event.chat
    chat_label = _format_chat_info(chat)
    message_ids = event.message_ids
    print(f"chat={chat_label}, удалено сообщений: {len(message_ids)}")

    for msg_id in message_ids:
        text = messages_cache.get((chat.id, msg_id), "текст неизвестен")
        print(f"Удалено: msg_id={msg_id}, text={text[:50]}...")
        await bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text=f"Удалено в чате {chat_label}:\n{text}",
        )
        messages_cache.pop((chat.id, msg_id), None)


@dp.edited_business_message()
async def on_edited_business_message(message: types.Message, bot: Bot) -> None:
    print("=== Событие редактирования получено ===")
    if OWNER_CHAT_ID is None:
        print("OWNER_CHAT_ID не установлен!")
        return

    chat = message.chat
    chat_label = _format_chat_info(chat)
    msg_id = message.message_id
    old_text = messages_cache.get((chat.id, msg_id), "текст неизвестен")
    new_text = (message.text or message.caption or "пусто").strip()
    print(f"Изменено: chat={chat_label}, msg_id={msg_id}, новое: {new_text[:50]}...")
    await bot.send_message(
        chat_id=OWNER_CHAT_ID,
        text=f"Изменено в чате {chat_label}:\nБыло: {old_text}\nСтало: {new_text}",
    )
    messages_cache[(chat.id, msg_id)] = new_text


# -------------------- ВЕБХУК --------------------
async def healthcheck(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def on_startup(bot: Bot) -> None:
    if not RENDER_EXTERNAL_URL:
        print("⚠️ RENDER_EXTERNAL_URL не задан, вебхук не будет установлен. Бот будет работать только по healthcheck.")
    else:
        webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"
        try:
            await bot.set_webhook(
                webhook_url,
                allowed_updates=dp.resolve_used_update_types(),
            )
            print(f"Webhook установлен: {webhook_url}")
        except Exception as e:
            print(f"❌ Ошибка установки webhook: {e}")


async def on_shutdown(bot: Bot) -> None:
    save_profiles()
    print("Профили сохранены перед выключением.")
    try:
        await bot.delete_webhook()
    finally:
        await bot.session.close()
    print("Webhook удалён")


async def main() -> None:
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    app.router.add_get("/", healthcheck)

    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

    port = int(os.environ.get("PORT", 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Сервер запущен на порту {port}")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
