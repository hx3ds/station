import json

from station.conductor.platforms.policy import normalize_platform
from station import logger
from station.prototypes.boundary import ext_bool, ext_dict, ext_list, ext_str

class PrototypeWebRTC:
    def _decode_webrtc_content(self, raw_content):
        return ext_dict("webrtc content", raw_content)

    def _via_consul(self, data):
        return normalize_platform(ext_str("platform", data.get("platform"))) == "sokoyuku"

    async def _send_webrtc_event(self, event_type, content, *, chat_id, request_id=None, acct_id=None, via_consul=False):
        if via_consul:
            from station.client.consul import push_webrtc
            ctx = self.client_context
            session = self.app["session"]
            if ctx and ctx.consul_url and ctx.token:
                await push_webrtc(
                    session=session,
                    consul_url=ctx.consul_url,
                    token=ctx.token,
                    chat_id=chat_id,
                    webrtc_type=event_type,
                    webrtc_content=content,
                )
                return
        await self.send_proxy(
            method="send_webrtc",
            chat_id=chat_id,
            request_id=request_id if request_id else None,
            params={
                "webrtc_type": event_type,
                "webrtc_content": json.dumps(content),
            },
            acct_id=acct_id if acct_id else None,
        )

    async def on_webrtc_call_ready(self, *, call_id, chat_id, acct_id, model_id, push_audio_pcm):
        return None

    async def on_webrtc_audio_in(self, *, call_id, chat_id, acct_id, model_id, pcm_s16le_mono_48k):
        return None

    async def on_webrtc_call_ended(self, *, call_id, chat_id, acct_id, model_id):
        return None

    async def _handle_webrtc_invite(self, svc, content, turn, *, model_id, chat_id=None, acct_id=None, request_id=None, via_consul=False):
        offer = content.get("offer")
        if offer is None:
            return False
        offer = ext_dict("webrtc offer", offer)
        offer_sdp = offer.get("sdp")
        if offer_sdp is not None:
            offer_sdp = ext_str("webrtc offer.sdp", offer_sdp)
        call_id = content.get("call_id")
        if call_id is not None:
            call_id = ext_str("webrtc call_id", call_id)
        call_version = content.get("version")
        if not (offer_sdp and offer_sdp.strip()):
            return False
        if svc is None or not self.client_context:
            return False

        async def send_webrtc_event(event_type, event_content):
            await self._send_webrtc_event(
                event_type,
                event_content,
                chat_id=chat_id,
                request_id=request_id,
                acct_id=acct_id,
                via_consul=via_consul,
            )

        async def on_audio_in(pcm):
            await self.on_webrtc_audio_in(
                call_id=call_id,
                chat_id=chat_id,
                acct_id=acct_id,
                model_id=model_id,
                pcm_s16le_mono_48k=pcm,
            )

        async def on_ready(push_audio):
            await self.on_webrtc_call_ready(
                call_id=call_id,
                chat_id=chat_id,
                acct_id=acct_id,
                model_id=model_id,
                push_audio_pcm=push_audio,
            )

        async def on_ended():
            await self.on_webrtc_call_ended(
                call_id=call_id,
                chat_id=chat_id,
                acct_id=acct_id,
                model_id=model_id,
            )

        answer_sdp = await svc.handle_offer(
            offer_sdp=offer_sdp,
            model_id=model_id,
            chat_id=chat_id if chat_id is not None else "",
            acct_id=acct_id if acct_id is not None else "",
            request_id=request_id if request_id is not None else "",
            call_id=call_id,
            call_version=call_version,
            send_webrtc=send_webrtc_event,
            turn=turn,
            on_audio_in=on_audio_in,
            on_ready=on_ready,
            on_ended=on_ended,
        )
        answer_content = {
            "answer": {"type": "answer", "sdp": answer_sdp},
        }
        if "call_id" in content:
            answer_content["call_id"] = content.get("call_id")
        if "version" in content:
            answer_content["version"] = content.get("version")
        await self._send_webrtc_event(
            "m.call.answer",
            answer_content,
            chat_id=chat_id,
            request_id=request_id,
            acct_id=acct_id,
            via_consul=via_consul,
        )
        return True

    async def handle_webrtc_message(self, data, *, model_id, chat_id=None, acct_id=None, request_id=None):

        webrtc = data.get("webrtc")
        if webrtc is None:
            return False
        webrtc = ext_dict("webrtc", webrtc)
        webrtc_type = webrtc.get("type")
        if webrtc_type is not None:
            webrtc_type = ext_str("webrtc type", webrtc_type)
        platform = ext_str("platform", data.get("platform"))
        chat_type = ext_str("chat_type", data.get("chat_type"))
        if not await self._call_support_enabled():
            if webrtc_type == "m.call.invite" and chat_id:
                await self.send_outbound(
                    text="This model does not support voice calls. Continuing as a text channel.",
                    chat_id=chat_id,
                    request_id=request_id if request_id else None,
                    acct_id=acct_id if acct_id else None,
                    platform=platform,
                    chat_type=chat_type,
                )
                content = self._decode_webrtc_content(webrtc.get("content"))
                call_id = content.get("call_id")
                if call_id is not None:
                    call_id = ext_str("webrtc call_id", call_id)
                if call_id:
                    hangup = {"call_id": call_id, "version": content.get("version", 0)}
                    await self._send_webrtc_event(
                        "m.call.hangup",
                        hangup,
                        chat_id=chat_id,
                        request_id=request_id,
                        acct_id=acct_id,
                        via_consul=self._via_consul(data),
                    )
            logger.debug("call_support skipped model_id=%s", model_id)
            return True
        via_consul = self._via_consul(data)
        content = self._decode_webrtc_content(webrtc.get("content"))
        turn = webrtc.get("turn")
        svc = self.app.get("webrtc")
        if webrtc_type == "m.call.invite":
            return await self._handle_webrtc_invite(
                svc,
                content,
                turn,
                model_id=model_id,
                chat_id=chat_id,
                acct_id=acct_id,
                request_id=request_id,
                via_consul=via_consul,
            )
        if webrtc_type == "m.call.candidates":
            call_id = content.get("call_id")
            if call_id is not None:
                call_id = ext_str("webrtc call_id", call_id)
            candidates = content.get("candidates")
            if candidates is not None:
                candidates = ext_list("webrtc candidates", candidates)
            if svc is not None and call_id and candidates is not None:
                await svc.handle_candidates(call_id=call_id, candidates=candidates, request_id=request_id if request_id else None)
            return True
        if webrtc_type == "m.call.hangup":
            call_id = content.get("call_id")
            if call_id is not None:
                call_id = ext_str("webrtc call_id", call_id)
            if svc is not None and call_id:
                await svc.handle_hangup(call_id=call_id, request_id=request_id if request_id else None)
            return True
        return False

    async def _call_support_enabled(self):
        ctx = self.client_context
        if not ctx or not ctx.db or ctx.prototype_id is None:
            return False
        info = await ctx.db.get_prototype_info(ctx.prototype_id)
        if not info:
            return False
        call_support = info.get("call_support")
        if call_support is None:
            return False
        return ext_bool("call_support", call_support)
