from station import logger
from station.prototypes.boundary import ext_dict, ext_int, ext_str

def _chat_type_from_opened(chat_opened):
    chat_type = chat_opened.get("chat_type")
    if chat_type is not None:
        chat_type = ext_str("chat_type", chat_type)
        if chat_type:
            return chat_type
    channel_type = chat_opened.get("channel_type")
    if channel_type is None:
        return ""
    if type(channel_type) is str:
        return channel_type.strip()
    if type(channel_type) is bool:
        raise TypeError("channel_type must be str or int")
    channel_type = ext_int("channel_type", channel_type)
    if channel_type == 1:
        return "dm"
    if channel_type == 3:
        return "group_dm"
    if channel_type in (2, 13):
        return "voice"
    if channel_type == 15:
        return "forum"
    if channel_type in (10, 11, 12):
        return "thread"
    return "guild_text"

class PrototypeDiscordVoice:
    def _decode_discord_voice_content(self, raw_content):
        return ext_dict("discord_voice content", raw_content)

    async def on_discord_voice_ready(self, *, guild_id, channel_id, chat_id, acct_id, model_id, push_audio_pcm):
        return None

    async def on_discord_voice_audio_in(self, *, guild_id, channel_id, chat_id, acct_id, model_id, pcm_s16le_mono_48k):
        return None

    async def on_discord_voice_ended(self, *, guild_id, channel_id, chat_id, acct_id, model_id):
        return None

    async def send_discord_voice(self, *, action, channel_id=None, guild_id=None, chat_id=None, acct_id=None, request_id=None, self_mute=False, self_deaf=False):
        params = {
            "action": action.strip() or "join",
            "self_mute": self_mute,
            "self_deaf": self_deaf,
        }
        if channel_id:
            params["channel_id"] = channel_id
        if guild_id:
            params["guild_id"] = guild_id
        return await self.send_proxy(
            method="send_discord_voice",
            chat_id=chat_id,
            request_id=request_id if request_id else None,
            params=params,
            acct_id=acct_id if acct_id else None,
        )

    async def handle_discord_voice_chat_opened(self, chat_opened, *, model_id, chat_id=None, acct_id=None, request_id=None):

        chat_opened = ext_dict("chat_opened", chat_opened)
        if not chat_opened.get("is_voice"):
            return False
        channel_id = ext_str("channel_id", chat_opened.get("channel_id"))
        if not channel_id and chat_id:
            channel_id = chat_id
        platform = ext_str("platform", chat_opened.get("platform"), default="discord") or "discord"
        chat_type = _chat_type_from_opened(chat_opened)
        if not await self._call_support_enabled():
            if channel_id:
                await self.send_outbound(
                    text="This model does not support voice calls. Continuing as a text channel.",
                    chat_id=channel_id,
                    request_id=request_id if request_id else None,
                    acct_id=acct_id if acct_id else None,
                    platform=platform,
                    chat_type=chat_type,
                )
            logger.debug("call_support skipped model_id=%s", model_id)
            return True
        guild_id = ext_str("guild_id", chat_opened.get("guild_id")) or None
        if not channel_id:
            return False
        logger.info(
            "discord auto-join model_id=%s chat_id=%s channel_id=%s guild_id=%s",
            model_id,
            chat_id,
            channel_id,
            guild_id,
        )
        return await self.send_discord_voice(
            action="join",
            channel_id=channel_id,
            guild_id=guild_id,
            chat_id=chat_id or channel_id,
            acct_id=acct_id,
            request_id=request_id,
        )

    async def handle_discord_voice_message(self, data, *, model_id, chat_id=None, acct_id=None, request_id=None):

        discord_voice = data.get("discord_voice")
        if discord_voice is None:
            return False
        discord_voice = ext_dict("discord_voice", discord_voice)
        if not await self._call_support_enabled():
            logger.debug("call_support skipped model_id=%s", model_id)
            return True
        event_type = ext_str("discord_voice.type", discord_voice.get("type"))
        content = self._decode_discord_voice_content(discord_voice.get("content"))
        svc = self.app.get("discord_voice")
        if svc is None:
            return False
        guild_id = ext_str("guild_id", content.get("guild_id"))
        channel_id = ext_str("channel_id", content.get("channel_id"))

        if event_type == "discord.voice.credentials":
            async def on_audio_in(pcm):
                await self.on_discord_voice_audio_in(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    chat_id=chat_id,
                    acct_id=acct_id,
                    model_id=model_id,
                    pcm_s16le_mono_48k=pcm,
                )

            async def on_ready(push_audio):
                await self.on_discord_voice_ready(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    chat_id=chat_id,
                    acct_id=acct_id,
                    model_id=model_id,
                    push_audio_pcm=push_audio,
                )

            async def on_ended():
                await self.on_discord_voice_ended(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    chat_id=chat_id,
                    acct_id=acct_id,
                    model_id=model_id,
                )

            return await svc.handle_credentials(
                content=content,
                model_id=model_id,
                chat_id=chat_id,
                acct_id=acct_id,
                request_id=request_id,
                on_audio_in=on_audio_in,
                on_ready=on_ready,
                on_ended=on_ended,
            )

        if event_type == "discord.voice.left":
            await self.on_discord_voice_ended(
                guild_id=guild_id,
                channel_id=channel_id,
                chat_id=chat_id,
                acct_id=acct_id,
                model_id=model_id,
            )
            return await svc.handle_left(content=content, acct_id=acct_id)

        return False
