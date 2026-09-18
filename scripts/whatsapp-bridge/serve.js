#!/usr/bin/env node
import http from "http";
import {
  makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  downloadMediaMessage,
} from "@whiskeysockets/baileys";
import { Boom } from "@hapi/boom";
import pino from "pino";
import path from "path";
import { mkdirSync } from "fs";
import { resolveWhatsAppVersion } from "./version.js";

const args = process.argv.slice(2);
function getArg(name, defaultVal) {
  const idx = args.indexOf(`--${name}`);
  return idx !== -1 && args[idx + 1] ? args[idx + 1] : defaultVal;
}

const SESSION_DIR = getArg("session", path.join(process.cwd(), "session"));
const LISTEN_PORT = parseInt(getArg("port", "0"), 10);
mkdirSync(SESSION_DIR, { recursive: true });

const logger = pino({ level: "silent" });
const inbox = [];
const mediaStore = new Map();
let ready = false;
let sock = null;

function jidFromChatId(chatId) {
  const raw = String(chatId || "").trim();
  if (!raw) return "";
  if (raw.includes("@")) return raw;
  return `${raw}@s.whatsapp.net`;
}

function quotedFromReply(jid, replyTo) {
  const id = String(replyTo || "").trim();
  if (!id) return undefined;
  return { key: { remoteJid: jid, id } };
}

function mediaKindFromMessage(message) {
  if (!message) return null;
  if (message.imageMessage) {
    return {
      kind: "photo",
      inner: message.imageMessage,
      filename: message.imageMessage.fileName || "image.jpg",
      mime: message.imageMessage.mimetype || "image/jpeg",
      caption: message.imageMessage.caption || "",
    };
  }
  if (message.videoMessage) {
    return {
      kind: "video",
      inner: message.videoMessage,
      filename: message.videoMessage.fileName || "video.mp4",
      mime: message.videoMessage.mimetype || "video/mp4",
      caption: message.videoMessage.caption || "",
    };
  }
  if (message.audioMessage) {
    const ptt = Boolean(message.audioMessage.ptt);
    return {
      kind: ptt ? "voice" : "audio",
      inner: message.audioMessage,
      filename: ptt ? "voice.ogg" : "audio.ogg",
      mime: message.audioMessage.mimetype || "audio/ogg",
      caption: "",
    };
  }
  if (message.documentMessage) {
    return {
      kind: "document",
      inner: message.documentMessage,
      filename: message.documentMessage.fileName || "file",
      mime: message.documentMessage.mimetype || "application/octet-stream",
      caption: message.documentMessage.caption || "",
    };
  }
  if (message.stickerMessage) {
    return {
      kind: "sticker",
      inner: message.stickerMessage,
      filename: "sticker.webp",
      mime: message.stickerMessage.mimetype || "image/webp",
      caption: "",
    };
  }
  return null;
}

function textFromMessage(message) {
  if (!message) return "";
  return String(
    message.conversation ||
      message.extendedTextMessage?.text ||
      message.imageMessage?.caption ||
      message.videoMessage?.caption ||
      message.documentMessage?.caption ||
      ""
  );
}

function replyToFromMessage(message) {
  if (!message || typeof message !== "object") return "";
  const direct = message.contextInfo?.stanzaId;
  if (direct) return String(direct);
  for (const v of Object.values(message)) {
    if (v && typeof v === "object" && v.contextInfo?.stanzaId) {
      return String(v.contextInfo.stanzaId);
    }
  }
  return "";
}

async function connect() {
  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
  const version = await resolveWhatsAppVersion();
  sock = makeWASocket({
    version,
    auth: state,
    logger,
    printQRInTerminal: false,
    browser: ["Sokoyuku Local", "Chrome", "120.0"],
    syncFullHistory: false,
    markOnlineOnConnect: false,
    getMessage: async () => ({ conversation: "" }),
  });
  sock.ev.on("creds.update", saveCreds);
  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect } = update || {};
    if (connection === "open") {
      ready = true;
    }
    if (connection === "close") {
      ready = false;
      const reason = new Boom(lastDisconnect?.error)?.output?.statusCode;
      if (reason !== DisconnectReason.loggedOut) {
        setTimeout(() => {
          connect().catch(() => {});
        }, 2000);
      }
    }
  });
  sock.ev.on("messages.upsert", async (payload) => {
    const messages = payload?.messages || [];
    for (const msg of messages) {
      if (!msg || msg.key?.fromMe) continue;
      const text = textFromMessage(msg.message);
      const media = mediaKindFromMessage(msg.message);
      if (!String(text || "").trim() && !media) continue;
      const remote = String(msg.key?.remoteJid || "");
      const participant = String(msg.key?.participant || msg.key?.remoteJid || "");
      const messageId = String(msg.key?.id || "");
      let mediaId = "";
      if (media && sock) {
        try {
          const buf = await downloadMediaMessage(msg, "buffer", {}, { logger, reuploadRequest: sock.updateMediaMessage });
          if (buf && buf.length) {
            mediaId = messageId || `${Date.now()}`;
            mediaStore.set(mediaId, { bytes: buf, filename: media.filename, mime: media.mime, kind: media.kind });
          }
        } catch (e) {
          mediaId = "";
        }
      }
      inbox.push({
        chat_id: remote.split("@")[0],
        user_id: participant.split(":")[0].split("@")[0],
        text: String(text || media?.caption || ""),
        caption: String(media?.caption || ""),
        message_id: messageId,
        media_id: mediaId,
        media_kind: media ? media.kind : "",
        filename: media ? media.filename : "",
        mime: media ? media.mime : "",
        reply_to: replyToFromMessage(msg.message),
        ts_ms: Number(msg.messageTimestamp || 0) * 1000 || Date.now(),
      });
    }
  });
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      try {
        const raw = Buffer.concat(chunks).toString("utf8");
        resolve(raw ? JSON.parse(raw) : {});
      } catch (e) {
        reject(e);
      }
    });
    req.on("error", reject);
  });
}

function sendPayload(kind, buf, filename, mime, caption) {
  const type = String(kind || "").trim();
  if (type === "photo" || type === "image") {
    return { image: buf, caption: caption || undefined, mimetype: mime || "image/jpeg" };
  }
  if (type === "video") {
    return { video: buf, caption: caption || undefined, mimetype: mime || "video/mp4" };
  }
  if (type === "voice") {
    return { audio: buf, ptt: true, mimetype: mime || "audio/ogg; codecs=opus" };
  }
  if (type === "audio") {
    return { audio: buf, ptt: false, mimetype: mime || "audio/mpeg" };
  }
  if (type === "animation") {
    return { video: buf, gifPlayback: true, caption: caption || undefined, mimetype: mime || "video/mp4" };
  }
  return {
    document: buf,
    fileName: filename || "file",
    mimetype: mime || "application/octet-stream",
    caption: caption || undefined,
  };
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || "/", "http://127.0.0.1");
  try {
    if (req.method === "GET" && url.pathname === "/health") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ ok: true, ready }));
      return;
    }
    if (req.method === "GET" && url.pathname === "/messages") {
      const out = inbox.splice(0, inbox.length);
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ messages: out }));
      return;
    }
    if (req.method === "GET" && url.pathname.startsWith("/media/")) {
      const id = decodeURIComponent(url.pathname.slice("/media/".length));
      const item = mediaStore.get(id);
      if (!item) {
        res.writeHead(404, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ ok: false, error: "not found" }));
        return;
      }
      res.writeHead(200, {
        "Content-Type": item.mime || "application/octet-stream",
        "Content-Disposition": `attachment; filename="${item.filename || "file"}"`,
      });
      res.end(item.bytes);
      return;
    }
    if (req.method === "POST" && url.pathname === "/send") {
      const body = await readBody(req);
      if (!ready || !sock) {
        res.writeHead(503, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ ok: false, error: "not ready" }));
        return;
      }
      const jid = jidFromChatId(body.chat_id);
      const text = String(body.text || body.message || "");
      if (!jid || !text) {
        res.writeHead(400, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ ok: false, error: "chat_id and text required" }));
        return;
      }
      await sock.sendMessage(jid, { text }, { quoted: quotedFromReply(jid, body.reply_to) });
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ ok: true }));
      return;
    }
    if (req.method === "POST" && url.pathname === "/send-media") {
      const body = await readBody(req);
      if (!ready || !sock) {
        res.writeHead(503, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ ok: false, error: "not ready" }));
        return;
      }
      const jid = jidFromChatId(body.chat_id);
      const data = String(body.data || "");
      if (!jid || !data) {
        res.writeHead(400, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ ok: false, error: "chat_id and data required" }));
        return;
      }
      const buf = Buffer.from(data, "base64");
      const payload = sendPayload(body.type, buf, body.filename, body.mime, body.caption);
      await sock.sendMessage(jid, payload, { quoted: quotedFromReply(jid, body.reply_to) });
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ ok: true }));
      return;
    }
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: false, error: "not found" }));
  } catch (e) {
    res.writeHead(500, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: false, error: String(e?.message || e) }));
  }
});

await connect();
server.listen(LISTEN_PORT, "127.0.0.1", () => {
  const addr = server.address();
  process.stdout.write(JSON.stringify({ event: "listening", port: addr.port }) + "\n");
});
