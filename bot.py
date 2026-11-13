import json
import os
import re
import time
from datetime import datetime, timezone, timedelta

import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType


# ============================================================
# НАСТРОЙКИ И JSON
# ============================================================

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")

os.makedirs(DATA_DIR, exist_ok=True)

DEFAULTS = {
    "chats": {},
    "roles": {},
    "bans": {},
    "mutes": {},
    "warns": {},
    "nicks": {},
    "settings": {},
    "words": {},
    "stats": {}
}

DATA = {}

MSK = timezone(timedelta(hours=3))

CHAT_TYPES = {
    "1": "Беседа администрации",
    "2": "Беседа тестировщиков",
    "3": "Беседа игроков"
}


def path(name):
    return os.path.join(DATA_DIR, name + ".json")


def load(name):
    file_path = path(name)

    if not os.path.exists(file_path):
        save(name, DEFAULTS[name])
        return DEFAULTS[name].copy()

    try:
        with open(file_path, "r", encoding="utf-8") as file:
            result = json.load(file)

        return result if isinstance(result, dict) else DEFAULTS[name].copy()

    except (OSError, json.JSONDecodeError):
        return DEFAULTS[name].copy()


def save(name, value):
    file_path = path(name)
    temp_path = file_path + ".tmp"

    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=4)

    os.replace(temp_path, file_path)


def store(name):
    save(name, DATA[name])


for data_name in DEFAULTS:
    DATA[data_name] = load(data_name)


# ============================================================
# РОЛИ
# ============================================================

ROLE_LEVELS = {
    "пользователь": 0,
    "модератор": 1,
    "администратор": 2,
    "старший администратор": 3,
    "специальный администратор": 4,
    "владелец беседы": 5
}

CONFIG = {}
vk = None


def role_of(peer_id, user_id):
    if user_id in CONFIG.get("owner_ids", []):
        return "владелец беседы"

    roles = DATA["roles"].get(str(peer_id), {})
    return roles.get(str(user_id), "пользователь")


def level_of(peer_id, user_id):
    return ROLE_LEVELS.get(role_of(peer_id, user_id), 0)


def allowed(peer_id, user_id, minimum):
    return level_of(peer_id, user_id) >= ROLE_LEVELS[minimum]


def set_role(peer_id, user_id, role):
    DATA["roles"].setdefault(str(peer_id), {})
    DATA["roles"][str(peer_id)][str(user_id)] = role
    store("roles")


def delete_role(peer_id, user_id):
    roles = DATA["roles"].setdefault(str(peer_id), {})
    roles.pop(str(user_id), None)
    store("roles")


# ============================================================
# VK: ИМЕНА, ОТПРАВКА СООБЩЕНИЙ, ID И ЮЗЕРНЕЙМЫ
# ============================================================

def send(peer_id, message):
    vk.messages.send(
        peer_id=peer_id,
        message=message,
        random_id=0
    )


def get_name(user_id):
    nick = DATA["nicks"].get(str(user_id))
    if nick:
        return nick

    try:
        result = vk.users.get(user_ids=[user_id])
        if result:
            user = result[0]
            return user["first_name"] + " " + user["last_name"]
    except Exception:
        pass

    return "id" + str(user_id)


def extract_id(value):
    if not value:
        return None

    value = value.strip()

    patterns = [
        r"\[id(\d+)\|",
        r"vk\.com/id(\d+)",
        r"^id(\d+)$",
        r"^(\d+)$"
    ]

    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return int(match.group(1))

    screen_name = value
    link_match = re.search(r"vk\.com/([a-zA-Z0-9_.]+)", screen_name)
    if link_match:
        screen_name = link_match.group(1)

    screen_name = screen_name.lstrip("@")

    if re.fullmatch(r"[a-zA-Z0-9_.]{3,32}", screen_name):
        try:
            resolved = vk.utils.resolveScreenName(screen_name=screen_name)
            if isinstance(resolved, dict) and resolved.get("type") == "user":
                return int(resolved["object_id"])
        except Exception:
            pass

    return None


def parse_target_reason(text):
    parts = text.strip().split(maxsplit=1)

    if not parts:
        return None, "Не указана"

    target_id = extract_id(parts[0])
    reason = parts[1].strip() if len(parts) > 1 else "Не указана"

    return target_id, reason


def announce(actor_id, action, target_id, location="", reason="Не указана"):
    actor = get_name(actor_id)
    target = get_name(target_id)

    if location:
        first_line = f"{actor} {action} {location} {target}"
    else:
        first_line = f"{actor} {action} {target}"

    return first_line + "\nПричина: " + reason


def remember_chat(peer_id):
    key = str(peer_id)

    if key not in DATA["chats"]:
        DATA["chats"][key] = {
            "peer_id": peer_id,
            "server": None,
            "type": None
        }
        store("chats")


def server_of(peer_id):
    chat = DATA["chats"].get(str(peer_id), {})
    return chat.get("server")


def chat_type_of(peer_id):
    chat = DATA["chats"].get(str(peer_id), {})
    return chat.get("type")


def chats_for_scope(peer_id, scope):
    if scope == "chat":
        return [peer_id]

    if scope == "server":
        server = server_of(peer_id)

        if not server:
            return []

        return [
            int(chat["peer_id"])
            for chat in DATA["chats"].values()
            if str(chat.get("server")) == str(server)
        ]

    if scope == "global":
        return [
            int(chat["peer_id"])
            for chat in DATA["chats"].values()
        ]

    return []


# ============================================================
# ДЕЙСТВИЯ VK
# ============================================================

def remove_from_chat(peer_id, user_id):
    if peer_id < 2_000_000_000:
        return False

    chat_id = peer_id - 2_000_000_000

    try:
        vk.messages.removeChatUser(
            chat_id=chat_id,
            member_id=user_id
        )
        return True
    except Exception as error:
        print("Не удалось исключить", user_id, "из", peer_id, error)
        return False


def delete_message(peer_id, message_id):
    try:
        vk.messages.delete(
            message_ids=message_id,
            delete_for_all=1
        )
        return True
    except Exception:
        return False


# ============================================================
# БЛОКИРОВКИ
# ============================================================

def add_ban(user_id, actor_id, reason, scope, server=None, peer_id=None):
    key = str(user_id)

    DATA["bans"].setdefault(key, [])

    DATA["bans"][key].append({
        "actor": actor_id,
        "reason": reason,
        "scope": scope,
        "server": server,
        "peer_id": peer_id,
        "date": datetime.now(MSK).isoformat()
    })

    store("bans")


def remove_ban(user_id, scope, peer_id=None, server=None):
    key = str(user_id)
    records = DATA["bans"].get(key, [])

    kept = []

    for record in records:
        same_scope = record.get("scope") == scope

        if scope == "chat":
            same_scope = same_scope and record.get("peer_id") == peer_id

        if scope == "server":
            same_scope = same_scope and str(record.get("server")) == str(server)

        if not same_scope:
            kept.append(record)

    if kept:
        DATA["bans"][key] = kept
    else:
        DATA["bans"].pop(key, None)

    store("bans")


def is_banned_here(user_id, peer_id):
    records = DATA["bans"].get(str(user_id), [])
    current_server = server_of(peer_id)

    for record in records:
        scope = record.get("scope")

        if scope == "global":
            return True

        if scope == "chat" and record.get("peer_id") == peer_id:
            return True

        if scope == "server":
            if str(record.get("server")) == str(current_server):
                return True

    return False


def do_ban(peer_id, actor_id, target_id, reason, scope):
    server = server_of(peer_id) if scope == "server" else None

    if scope == "server" and not server:
        send(peer_id, "Сначала укажи сервер командой /server 01.")
        return

    add_ban(
        target_id,
        actor_id,
        reason,
        scope,
        server=server,
        peer_id=peer_id if scope == "chat" else None
    )

    for target_peer in chats_for_scope(peer_id, scope):
        remove_from_chat(target_peer, target_id)

    if scope == "global":
        location = "во всех беседах"
    elif scope == "server":
        location = f"в беседах {server} сервера"
    else:
        location = "в беседе"

    send(
        peer_id,
        announce(
            actor_id,
            "заблокировал",
            target_id,
            location,
            reason
        )
    )


def do_unban(peer_id, actor_id, target_id, reason, scope):
    server = server_of(peer_id) if scope == "server" else None

    if scope == "server" and not server:
        send(peer_id, "Сначала укажи сервер командой /server 01.")
        return

    remove_ban(
        target_id,
        scope,
        peer_id=peer_id if scope == "chat" else None,
        server=server
    )

    if scope == "global":
        location = "во всех беседах"
    elif scope == "server":
        location = f"в беседах {server} сервера"
    else:
        location = "в беседе"

    send(
        peer_id,
        announce(
            actor_id,
            "снял блокировку",
            target_id,
            location,
            reason
        )
    )


# ============================================================
# МУТЫ
# ============================================================

def do_mute(peer_id, actor_id, target_id, reason, minutes):
    DATA["mutes"].setdefault(str(peer_id), {})

    DATA["mutes"][str(peer_id)][str(target_id)] = {
        "actor": actor_id,
        "reason": reason,
        "until": int(time.time()) + minutes * 60
    }

    store("mutes")

    send(
        peer_id,
        f"{get_name(actor_id)} выдал мут {get_name(target_id)} "
        f"на {minutes} минут.\nПричина: {reason}"
    )


def do_unmute(peer_id, actor_id, target_id, reason):
    DATA["mutes"].setdefault(str(peer_id), {})
    DATA["mutes"][str(peer_id)].pop(str(target_id), None)
    store("mutes")

    send(
        peer_id,
        announce(actor_id, "снял мут с", target_id, "", reason)
    )


def has_active_mute(peer_id, user_id):
    mutes = DATA["mutes"].get(str(peer_id), {})
    record = mutes.get(str(user_id))

    if not record:
        return False

    if int(record.get("until", 0)) <= int(time.time()):
        mutes.pop(str(user_id), None)
        store("mutes")
        return False

    return True


def check_mute(peer_id, user_id, message_id):
    if has_active_mute(peer_id, user_id):
        delete_message(peer_id, message_id)


# ============================================================
# КИКИ
# ============================================================

def do_kick(peer_id, actor_id, target_id, reason, scope):
    server = server_of(peer_id) if scope == "server" else None

    if scope == "server" and not server:
        send(peer_id, "Сначала укажи сервер командой /server 01.")
        return

    peers = chats_for_scope(peer_id, scope)

    for target_peer in peers:
        remove_from_chat(target_peer, target_id)

    if scope == "server":
        location = f"из бесед сервера {server}"
    else:
        location = "из беседы"

    send(
        peer_id,
        announce(
            actor_id,
            "исключил",
            target_id,
            location,
            reason
        )
    )


# ============================================================
# ПРЕДУПРЕЖДЕНИЯ
# ============================================================

def do_warn(peer_id, actor_id, target_id, reason):
    DATA["warns"].setdefault(str(peer_id), {})
    DATA["warns"][str(peer_id)].setdefault(str(target_id), [])

    DATA["warns"][str(peer_id)][str(target_id)].append({
        "actor": actor_id,
        "reason": reason,
        "date": datetime.now(MSK).isoformat()
    })

    store("warns")

    send(
        peer_id,
        announce(
            actor_id,
            "выдал предупреждение",
            target_id,
            "",
            reason
        )
    )


def do_unwarn(peer_id, actor_id, target_id, reason):
    warnings = DATA["warns"].setdefault(str(peer_id), {})
    warnings.pop(str(target_id), None)
    store("warns")

    send(
        peer_id,
        announce(
            actor_id,
            "снял предупреждения с",
            target_id,
            "",
            reason
        )
    )


# ============================================================
# СТАТИСТИКА, НИКИ И СПИСКИ
# ============================================================

def update_stats(peer_id, user_id):
    p_key = str(peer_id)
    u_key = str(user_id)

    DATA["stats"].setdefault(p_key, {})
    user_stat = DATA["stats"][p_key].setdefault(u_key, {
        "count": 0,
        "last_message": None
    })

    user_stat["count"] += 1
    user_stat["last_message"] = datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S МСК (UTC+3)")
    store("stats")


def build_stats_report(peer_id, target_id):
    raw_role = role_of(peer_id, target_id)
    display_role = "Участник" if raw_role == "пользователь" else raw_role.capitalize()

    ban_records = DATA["bans"].get(str(target_id), [])
    bans_count = len(ban_records)

    has_global_ban = any(r.get("scope") == "global" for r in ban_records)
    is_players_chat = str(chat_type_of(peer_id)) == "3"
    has_players_ban = has_global_ban and is_players_chat

    warns_count = len(DATA["warns"].get(str(peer_id), {}).get(str(target_id), []))
    is_muted = has_active_mute(peer_id, target_id)

    nick = DATA["nicks"].get(str(target_id))
    if not nick:
        nick = get_name(target_id).replace(" ", "_")

    user_stat = DATA["stats"].get(str(peer_id), {}).get(str(target_id), {})
    msg_count = user_stat.get("count", 0)
    last_msg = user_stat.get("last_message") or "Нет данных"

    return (
        f"Информация о пользователе\n"
        f"Роль: {display_role}\n"
        f"Блокировок: {bans_count}\n"
        f"Общая блокировка в чатах: {'Да' if has_global_ban else 'Нет'}\n"
        f"Общая блокировка в беседах игроков: {'Да' if has_players_ban else 'Нет'}\n"
        f"Активные предупреждения: {warns_count}\n"
        f"Блокировка чата: {'Да' if is_muted else 'Нет'}\n"
        f"Ник: {nick}\n"
        f"Всего сообщений: {msg_count}\n"
        f"Последнее сообщение: {last_msg}"
    )


def set_nick(user_id, nick):
    DATA["nicks"][str(user_id)] = nick
    store("nicks")


def remove_nick(user_id):
    DATA["nicks"].pop(str(user_id), None)
    store("nicks")


def show_staff(peer_id):
    roles = DATA["roles"].get(str(peer_id), {})
    lines = []

    for user_id, role in roles.items():
        lines.append(f"{get_name(int(user_id))} — {role}")

    return "\n".join(lines) if lines else "Список персонала пуст."


def show_warns(peer_id, user_id):
    records = DATA["warns"].get(str(peer_id), {}).get(str(user_id), [])

    if not records:
        return "Предупреждений нет."

    lines = []

    for number, record in enumerate(records, 1):
        lines.append(
            f"{number}. {record['reason']} — "
            f"{get_name(record['actor'])}"
        )

    return "\n".join(lines)


def show_bans(scope=None, server=None):
    lines = []

    for user_id, records in DATA["bans"].items():
        for record in records:
            if scope and record.get("scope") != scope:
                continue

            if scope == "server" and str(record.get("server")) != str(server):
                continue

            lines.append(
                f"{get_name(int(user_id))} — "
                f"{record.get('reason', 'Не указана')}"
            )

    return "\n".join(lines) if lines else "Список пуст."


def mention_all_in_chat(peer_id, text):
    try:
        members = vk.messages.getConversationMembers(peer_id=peer_id)
        profiles = members.get("profiles", [])
        tags = [f"[id{p['id']}|&#8203;]" for p in profiles if p.get("id", 0) > 0]
        header = text if text else "Общий сбор!"
        send(peer_id, f"{header}\n{''.join(tags)}")
    except Exception:
        send(peer_id, text if text else "Не удалось получить список участников для вызова.")


# ============================================================
# КОМАНДЫ
# ============================================================

def handle(peer_id, actor_id, message_text):
    parts = message_text.strip().split(maxsplit=1)

    command = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    # --------------------------------------------------------
    # ПОЛЬЗОВАТЕЛИ
    # --------------------------------------------------------

    if command == "/info":
        send(peer_id, f"Твоя роль: {role_of(peer_id, actor_id)}")
        return

    if command == "/getid":
        target = extract_id(args) if args else actor_id
        send(peer_id, f"ID: {target}" if target else "Пользователь не найден.")
        return

    if command == "/stats":
        target = extract_id(args) if args else actor_id
        if not target:
            send(peer_id, "Пользователь не найден.")
            return
        send(peer_id, build_stats_report(peer_id, target))
        return

    # --------------------------------------------------------
    # ПЕРСОНАЛ
    # --------------------------------------------------------

    if command == "/staff":
        if allowed(peer_id, actor_id, "модератор"):
            send(peer_id, show_staff(peer_id))
        return

    if command == "/addmoder":
        if not allowed(peer_id, actor_id, "администратор"):
            return

        target = extract_id(args)
        if target:
            set_role(peer_id, target, "модератор")
            send(peer_id, f"{get_name(actor_id)} назначил {get_name(target)} модератором.")
        else:
            send(peer_id, "Укажи ID, ссылку или юзернейм пользователя.")
        return

    if command == "/addadmin":
        if not allowed(peer_id, actor_id, "специальный администратор"):
            return

        target = extract_id(args)
        if target:
            set_role(peer_id, target, "администратор")
            send(peer_id, f"{get_name(actor_id)} назначил {get_name(target)} администратором.")
        else:
            send(peer_id, "Укажи ID, ссылку или юзернейм пользователя.")
        return

    if command == "/addsenadm":
        if not allowed(peer_id, actor_id, "специальный администратор"):
            return

        target = extract_id(args)
        if target:
            set_role(peer_id, target, "старший администратор")
            send(peer_id, f"{get_name(actor_id)} назначил {get_name(target)} старшим администратором.")
        else:
            send(peer_id, "Укажи ID, ссылку или юзернейм пользователя.")
        return

    if command == "/addspecadm":
        if not allowed(peer_id, actor_id, "владелец беседы"):
            return

        target = extract_id(args)
        if target:
            set_role(peer_id, target, "специальный администратор")
            send(peer_id, f"{get_name(actor_id)} назначил {get_name(target)} специальным администратором.")
        else:
            send(peer_id, "Укажи ID, ссылку или юзернейм пользователя.")
        return

    if command == "/setowner":
        if not allowed(peer_id, actor_id, "владелец беседы"):
            return

        target = extract_id(args)
        if target:
            set_role(peer_id, actor_id, "администратор")
            set_role(peer_id, target, "владелец беседы")
            send(peer_id, f"{get_name(actor_id)} передал владение беседой {get_name(target)}.")
        else:
            send(peer_id, "Укажи ID, ссылку или юзернейм пользователя.")
        return

    if command in {"/removerole", "/gremoverole"}:
        if not allowed(peer_id, actor_id, "администратор"):
            return

        target = extract_id(args)
        if target:
            delete_role(peer_id, target)
            send(peer_id, f"{get_name(actor_id)} снял роль с {get_name(target)}.")
        else:
            send(peer_id, "Укажи ID, ссылку или юзернейм пользователя.")
        return

    # --------------------------------------------------------
    # СЕРВЕР И ТИП БЕСЕДЫ
    # --------------------------------------------------------

    if command == "/server":
        if not allowed(peer_id, actor_id, "специальный администратор"):
            return

        server = args.strip()

        if not server:
            send(peer_id, "Пример: /server 01")
            return

        remember_chat(peer_id)
        DATA["chats"][str(peer_id)]["server"] = server
        store("chats")

        send(peer_id, f"Беседа привязана к серверу {server}.")
        return

    if command == "/type":
        if not allowed(peer_id, actor_id, "специальный администратор"):
            return

        choice = args.strip()
        if choice not in CHAT_TYPES:
            current = CHAT_TYPES.get(str(chat_type_of(peer_id)), "Не установлен")
            send(
                peer_id,
                f"Текущий тип беседы: {current}\n\n"
                f"Выбери новый тип цифрой:\n"
                f"1 — Беседа администрации\n"
                f"2 — Беседа тестировщиков\n"
                f"3 — Беседа игроков\n"
                f"Пример: /type 3"
            )
            return

        remember_chat(peer_id)
        DATA["chats"][str(peer_id)]["type"] = choice
        store("chats")

        send(peer_id, f"Тип беседы изменён на: {CHAT_TYPES[choice]}.")
        return

    # --------------------------------------------------------
    # БЛОКИРОВКИ
    # --------------------------------------------------------

    ban_commands = {
        "/ban": ("chat", "администратор"),
        "/sban": ("server", "администратор"),
        "/gban": ("global", "специальный администратор")
    }

    if command in ban_commands:
        scope, minimum = ban_commands[command]

        if not allowed(peer_id, actor_id, minimum):
            return

        target, reason = parse_target_reason(args)

        if not target:
            send(peer_id, f"Пример: {command} @юзернейм (или ID) причина")
            return

        do_ban(peer_id, actor_id, target, reason, scope)
        return

    unban_commands = {
        "/unban": ("chat", "администратор"),
        "/sunban": ("server", "администратор"),
        "/gunban": ("global", "специальный администратор")
    }

    if command in unban_commands:
        scope, minimum = unban_commands[command]

        if not allowed(peer_id, actor_id, minimum):
            return

        target, reason = parse_target_reason(args)

        if not target:
            send(peer_id, f"Пример: {command} @юзернейм (или ID) причина")
            return

        do_unban(peer_id, actor_id, target, reason, scope)
        return

    if command == "/banlist":
        if allowed(peer_id, actor_id, "администратор"):
            send(peer_id, show_bans("chat"))
        return

    if command == "/gbanlist":
        if allowed(peer_id, actor_id, "специальный администратор"):
            send(peer_id, show_bans("global"))
        return

    if command == "/sbanlist":
        if allowed(peer_id, actor_id, "администратор"):
            send(peer_id, show_bans("server", server_of(peer_id)))
        return

    # --------------------------------------------------------
    # МУТЫ
    # --------------------------------------------------------

    if command == "/mute":
        if not allowed(peer_id, actor_id, "модератор"):
            return

        parts = args.split(maxsplit=2)

        if len(parts) < 2:
            send(peer_id, "Пример: /mute @юзернейм минуты причина")
            return

        target = extract_id(parts[0])

        if not target:
            send(peer_id, "Пользователь не найден.")
            return

        try:
            minutes = int(parts[1])
        except ValueError:
            send(peer_id, "Время мута должно быть числом минут.")
            return

        reason = parts[2] if len(parts) > 2 else "Не указана"

        if minutes > 0:
            do_mute(peer_id, actor_id, target, reason, minutes)
        return

    if command == "/unmute":
        if not allowed(peer_id, actor_id, "модератор"):
            return

        target, reason = parse_target_reason(args)

        if target:
            do_unmute(peer_id, actor_id, target, reason)
        else:
            send(peer_id, "Пример: /unmute @юзернейм причина")
        return

    # --------------------------------------------------------
    # КИКИ
    # --------------------------------------------------------

    if command in {"/kick", "/skick"}:
        if not allowed(peer_id, actor_id, "модератор"):
            return

        target, reason = parse_target_reason(args)

        if not target:
            send(peer_id, f"Пример: {command} @юзернейм причина")
            return

        scope = "server" if command == "/skick" else "chat"
        do_kick(peer_id, actor_id, target, reason, scope)
        return

    # --------------------------------------------------------
    # ПРЕДУПРЕЖДЕНИЯ
    # --------------------------------------------------------

    if command == "/warn":
        if not allowed(peer_id, actor_id, "модератор"):
            return

        target, reason = parse_target_reason(args)

        if target:
            do_warn(peer_id, actor_id, target, reason)
        else:
            send(peer_id, "Пример: /warn @юзернейм причина")
        return

    if command == "/unwarn":
        if not allowed(peer_id, actor_id, "модератор"):
            return

        target, reason = parse_target_reason(args)

        if target:
            do_unwarn(peer_id, actor_id, target, reason)
        else:
            send(peer_id, "Пример: /unwarn @юзернейм причина")
        return

    if command == "/getwarn":
        target = extract_id(args) if args else actor_id
        if target:
            send(peer_id, show_warns(peer_id, target))
        else:
            send(peer_id, "Пользователь не найден.")
        return

    if command == "/warnlist":
        if not allowed(peer_id, actor_id, "модератор"):
            return

        records = DATA["warns"].get(str(peer_id), {})
        lines = []

        for user_id, warns in records.items():
            lines.append(f"{get_name(int(user_id))}: {len(warns)}")

        send(peer_id, "\n".join(lines) if lines else "Предупреждений нет.")
        return

    if command == "/warnhistory":
        if not allowed(peer_id, actor_id, "модератор"):
            return

        target = extract_id(args)
        if target:
            send(peer_id, show_warns(peer_id, target))
        else:
            send(peer_id, "Пример: /warnhistory @юзернейм")
        return

    # --------------------------------------------------------
    # НИКИ
    # --------------------------------------------------------

    if command == "/setnick":
        if not allowed(peer_id, actor_id, "модератор"):
            return

        parts = args.split(maxsplit=1)

        if len(parts) < 2:
            send(peer_id, "Пример: /setnick @юзернейм НовыйНик")
            return

        target = extract_id(parts[0])
        nick = parts[1].strip()

        if target and nick:
            set_nick(target, nick)
            send(peer_id, f"{get_name(target)} получил ник {nick}.")
        else:
            send(peer_id, "Пользователь не найден.")
        return

    if command in {"/nonick", "/removenick"}:
        if not allowed(peer_id, actor_id, "модератор"):
            return

        target = extract_id(args)

        if target:
            remove_nick(target)
            send(peer_id, f"{get_name(target)}: ник удалён.")
        else:
            send(peer_id, "Укажи ID, ссылку или юзернейм пользователя.")
        return

    if command == "/getnick":
        target = extract_id(args) if args else actor_id
        if target:
            send(peer_id, DATA["nicks"].get(str(target), "Ник не установлен."))
        else:
            send(peer_id, "Пользователь не найден.")
        return

    if command == "/nlist":
        lines = [
            f"{get_name(int(user_id))} — {nick}"
            for user_id, nick in DATA["nicks"].items()
        ]
        send(peer_id, "\n".join(lines) if lines else "Список ников пуст.")
        return

    # --------------------------------------------------------
    # ФИЛЬТР СЛОВ
    # --------------------------------------------------------

    if command == "/addword":
        if not allowed(peer_id, actor_id, "старший администратор"):
            return

        word = args.strip().lower()

        if word:
            DATA["words"].setdefault(str(peer_id), [])
            if word not in DATA["words"][str(peer_id)]:
                DATA["words"][str(peer_id)].append(word)
                store("words")

            send(peer_id, f"Слово добавлено в фильтр: {word}")
        return

    if command == "/delword":
        if not allowed(peer_id, actor_id, "старший администратор"):
            return

        word = args.strip().lower()
        words = DATA["words"].setdefault(str(peer_id), [])

        if word in words:
            words.remove(word)
            store("words")

        send(peer_id, f"Слово удалено из фильтра: {word}")
        return

    if command == "/banwords":
        if not allowed(peer_id, actor_id, "старший администратор"):
            return

        words = DATA["words"].get(str(peer_id), [])
        send(peer_id, "\n".join(words) if words else "Фильтр пуст.")
        return

    if command == "/filter":
        if not allowed(peer_id, actor_id, "старший администратор"):
            return

        settings = DATA["settings"].setdefault(str(peer_id), {})
        settings["filter"] = args.strip().lower() not in {"off", "выкл", "0"}
        store("settings")

        send(peer_id, "Фильтр включён." if settings["filter"] else "Фильтр выключен.")
        return

    # --------------------------------------------------------
    # ОСТАЛЬНЫЕ КОМАНДЫ
    # --------------------------------------------------------

    if command == "/settings":
        settings = DATA["settings"].get(str(peer_id), {})
        c_type = CHAT_TYPES.get(str(chat_type_of(peer_id)), "Не установлен")
        srv = server_of(peer_id) or "Не указан"
        send(
            peer_id,
            f"Сервер: {srv}\n"
            f"Тип беседы: {c_type}\n"
            f"Фильтр слов: {'Вкл' if settings.get('filter') else 'Выкл'}\n"
            f"Режим тишины: {'Вкл' if settings.get('quiet') else 'Выкл'}"
        )
        return

    if command == "/zov":
        if allowed(peer_id, actor_id, "администратор"):
            mention_all_in_chat(peer_id, args)
        return

    if command == "/gzov":
        if allowed(peer_id, actor_id, "специальный администратор"):
            for chat_peer in DATA["chats"]:
                mention_all_in_chat(int(chat_peer), args)
        return

    if command == "/gnlist":
        if allowed(peer_id, actor_id, "специальный администратор"):
            lines = [
                f"id{uid} — {nick}"
                for uid, nick in DATA["nicks"].items()
            ]
            send(peer_id, "\n".join(lines) if lines else "Глобальный список ников пуст.")
        return

    if command == "/gsync":
        if allowed(peer_id, actor_id, "специальный администратор"):
            for chat_peer in list(DATA["chats"].keys()):
                try:
                    members = vk.messages.getConversationMembers(peer_id=int(chat_peer))
                    for item in members.get("items", []):
                        uid = item.get("member_id", 0)
                        if uid > 0 and is_banned_here(uid, int(chat_peer)):
                            remove_from_chat(int(chat_peer), uid)
                except Exception:
                    pass
            send(peer_id, "Глобальная синхронизация бесед и блокировок выполнена.")
        return

    if command == "/sync":
        if allowed(peer_id, actor_id, "старший администратор"):
            remember_chat(peer_id)
            try:
                members = vk.messages.getConversationMembers(peer_id=peer_id)
                for item in members.get("items", []):
                    uid = item.get("member_id", 0)
                    if uid > 0 and is_banned_here(uid, peer_id):
                        remove_from_chat(peer_id, uid)
                send(peer_id, "Текущая беседа синхронизирована.")
            except Exception:
                send(peer_id, "Не удалось получить список участников. Проверь права администратора у бота.")
        return

    if command == "/offblock":
        if allowed(peer_id, actor_id, "специальный администратор"):
            target, reason = parse_target_reason(args)
            if not target:
                send(peer_id, "Пример: /offblock @юзернейм причина")
                return
            do_ban(peer_id, actor_id, target, reason, "global")
        return

    if command == "/quiet":
        if not allowed(peer_id, actor_id, "администратор"):
            return

        settings = DATA["settings"].setdefault(str(peer_id), {})
        settings["quiet"] = args.strip().lower() not in {"off", "выкл", "0"}
        store("settings")

        send(peer_id, "Режим тишины включён." if settings["quiet"] else "Режим тишины выключен.")
        return

    if command == "/clear":
        if not allowed(peer_id, actor_id, "администратор"):
            return

        try:
            history = vk.messages.getHistory(
                peer_id=peer_id,
                count=30
            )

            ids = [
                item["id"]
                for item in history.get("items", [])
                if item.get("from_id") == -CONFIG["group_id"]
            ]

            if ids:
                vk.messages.delete(
                    message_ids=",".join(map(str, ids)),
                    delete_for_all=1
                )

            send(peer_id, "Очистка сообщений бота выполнена.")
        except Exception:
            send(peer_id, "Не удалось очистить сообщения.")
        return

    if command == "/alt":
        target = extract_id(args) if args else actor_id
        if target:
            send(peer_id, f"Пользователь: {get_name(target)}\nID: {target}")
        else:
            send(peer_id, "Пользователь не найден.")
        return

    if command == "/getban":
        target = extract_id(args) if args else actor_id
        if not target:
            send(peer_id, "Пользователь не найден.")
            return
        records = DATA["bans"].get(str(target), [])
        if not records:
            send(peer_id, "Блокировок нет.")
        else:
            lines = [
                f"{record['scope']}: {record['reason']}"
                for record in records
            ]
            send(peer_id, "\n".join(lines))
        return

    if command == "/getacc":
        target = extract_id(args) if args else actor_id
        if target:
            send(peer_id, f"Аккаунт: {get_name(target)}\nID: {target}")
        else:
            send(peer_id, "Пользователь не найден.")
        return

    if command == "/onlinelist":
        try:
            members = vk.messages.getConversationMembers(peer_id=peer_id)
            online_users = [
                f"{p['first_name']} {p['last_name']}"
                for p in members.get("profiles", [])
                if p.get("online") == 1
            ]
            send(peer_id, "Онлайн в беседе:\n" + "\n".join(online_users) if online_users else "Никого нет в сети.")
        except Exception:
            send(peer_id, "Не удалось получить список онлайн.")
        return

    if command == "/online":
        target = extract_id(args) if args else actor_id
        if not target:
            send(peer_id, "Пользователь не найден.")
            return
        try:
            res = vk.users.get(user_ids=[target], fields="online")
            if res:
                status = "В сети" if res[0].get("online") == 1 else "Не в сети"
                send(peer_id, f"{get_name(target)}: {status}")
        except Exception:
            send(peer_id, "Не удалось проверить статус пользователя.")
        return

    if command == "/urkick":
        if not allowed(peer_id, actor_id, "специальный администратор"):
            return

        target, reason = parse_target_reason(args)
        if target:
            do_kick(peer_id, actor_id, target, reason, "chat")
        else:
            send(peer_id, "Пример: /urkick @юзернейм причина")
        return


# ============================================================
# ЗАПУСК И ОБРАБОТКА СООБЩЕНИЙ
# ============================================================

def process_event(message):
    peer_id = message["peer_id"]
    actor_id = message["from_id"]
    text = message.get("text", "").strip()
    message_id = message.get("id")

    remember_chat(peer_id)

    if actor_id < 0:
        return

    update_stats(peer_id, actor_id)

    if is_banned_here(actor_id, peer_id):
        remove_from_chat(peer_id, actor_id)
        return

    if message_id:
        check_mute(peer_id, actor_id, message_id)

    settings = DATA["settings"].get(str(peer_id), {})

    if settings.get("filter", False):
        words = DATA["words"].get(str(peer_id), [])

        if any(word in text.lower() for word in words):
            if level_of(peer_id, actor_id) < ROLE_LEVELS["модератор"]:
                delete_message(peer_id, message_id)
                return

    if settings.get("quiet", False):
        if level_of(peer_id, actor_id) < ROLE_LEVELS["модератор"]:
            delete_message(peer_id, message_id)
            return

    if text.startswith("/"):
        try:
            handle(peer_id, actor_id, text)
        except Exception as error:
            print("Ошибка обработки команды:", repr(error))
            try:
                send(peer_id, "Не удалось выполнить команду. Проверь аргументы и права бота.")
            except Exception:
                pass


def main():
    global CONFIG, vk

    config_path = os.path.join(BASE, "config.json")

    with open(config_path, "r", encoding="utf-8") as file:
        CONFIG = json.load(file)

    session = vk_api.VkApi(token=CONFIG["token"])
    vk = session.get_api()

    longpoll = VkBotLongPoll(
        session,
        CONFIG["group_id"]
    )

    print("Бот запущен.")

    for event in longpoll.listen():
        if event.type != VkBotEventType.MESSAGE_NEW:
            continue

        try:
            message = event.object.message
            process_event(message)
        except Exception as error:
            print("Ошибка события:", repr(error))


if __name__ == "__main__":
    main()
