from station.errors import ExternalError

MESSAGE_CONTENT_FLAG = 1 << 18
MESSAGE_CONTENT_LIMITED_FLAG = 1 << 19
QQ_SETTINGS_DENIED_CODE = 11253

TELEGRAM_PRIVACY_NOTICE = (
    "Privacy mode is on, so this bot does not receive ordinary group messages. "
    "It still receives commands, replies to its messages, and mentions, and all messages when it is a group admin. "
    "In @BotFather run /setprivacy, disable privacy mode, then remove this bot from the group and add it again."
)

DISCORD_MESSAGE_CONTENT_NOTICE = (
    "The Message Content intent is off, so this bot does not receive message text in servers. "
    "Mentions and direct messages still arrive. "
    "In the Discord Developer Portal, enable Message Content under Privileged Gateway Intents, then restart the bot."
)

QQ_ONLY_MENTION_NOTICE = (
    "This group only delivers @ mentions to the bot. "
    "In the group bot settings, set the messages the bot can access to all group messages."
)

QQ_MENTION_CONTEXT_NOTICE = (
    "This group only delivers @ mentions and nearby messages to the bot. "
    "In the group bot settings, set the messages the bot can access to all group messages."
)

TELEGRAM_ENSURE_NOTICE = (
    "Make sure this bot can receive ordinary group messages. "
    "In @BotFather run /setprivacy and disable privacy mode, then remove the bot from the group and add it again."
)

DISCORD_ENSURE_NOTICE = (
    "Make sure this bot can receive message text in servers. "
    "In the Discord Developer Portal, enable Message Content under Privileged Gateway Intents, then restart the bot."
)

QQ_ENSURE_NOTICE = (
    "Make sure this bot can receive group messages. "
    "In the group bot settings, set the messages the bot can access to all group messages."
)

_GROUP_VISIBILITY_PLATFORMS = {
    "telegram": TELEGRAM_ENSURE_NOTICE,
    "discord": DISCORD_ENSURE_NOTICE,
    "qq": QQ_ENSURE_NOTICE,
}


def controls_group_messages(platform):
    return (platform or "").strip().lower() in _GROUP_VISIBILITY_PLATFORMS


def ensure_group_notice(platform):
    return _GROUP_VISIBILITY_PLATFORMS.get((platform or "").strip().lower(), "")


def telegram_group_notice(can_read_all_group_messages):
    if can_read_all_group_messages:
        return ""
    return TELEGRAM_PRIVACY_NOTICE


def discord_group_notice(flags):
    if flags & MESSAGE_CONTENT_FLAG or flags & MESSAGE_CONTENT_LIMITED_FLAG:
        return ""
    return DISCORD_MESSAGE_CONTENT_NOTICE


def qq_group_notice(recv_msg_setting):
    setting = (recv_msg_setting or "").strip()
    if setting == "all":
        return ""
    if setting == "only_mention":
        return QQ_ONLY_MENTION_NOTICE
    if setting == "mention_and_context":
        return QQ_MENTION_CONTEXT_NOTICE
    raise ExternalError("unexpected qq recv_msg_setting %s" % setting)


def qq_settings_denied(status, code, message):
    if code == QQ_SETTINGS_DENIED_CODE:
        return True
    if status in (403, 404, 429):
        return True
    text = (message or "").lower()
    if "无接口访问权限" in text or "whitelist" in text:
        return True
    return False
