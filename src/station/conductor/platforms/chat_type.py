def station_chat_type(platform, native):
    platform = (platform or "").strip().lower()
    native = (native or "").strip().lower()
    if native in ("private", "group"):
        return native
    if platform == "telegram" and native == "supergroup":
        return "group"
    if platform == "discord" and native == "dm":
        return "private"
    if platform == "discord" and native in ("group_dm", "guild_text", "voice", "forum", "thread"):
        return "group"
    if platform == "matrix" and native == "direct":
        return "private"
    if platform == "matrix" and native == "room":
        return "group"
    if platform == "qq" and native == "c2c":
        return "private"
    if platform in ("whatsapp", "whatsapp_cloud") and native in ("one-to-one", "direct"):
        return "private"
    return ""
