#!/usr/bin/env node
import { makeWASocket, useMultiFileAuthState, DisconnectReason } from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import pino from 'pino';
import path from 'path';
import { mkdirSync } from 'fs';
import { resolveWhatsAppVersion } from './version.js';

const args = process.argv.slice(2);
function getArg(name, defaultVal) {
  const idx = args.indexOf(`--${name}`);
  return idx !== -1 && args[idx + 1] ? args[idx + 1] : defaultVal;
}

const SESSION_DIR = getArg('session', path.join(process.cwd(), 'session'));
const QR_TIMEOUT_MS = parseInt(getArg('qr-timeout-ms', '60000'), 10);
mkdirSync(SESSION_DIR, { recursive: true });

const logger = pino({ level: 'silent' });

function emit(event, payload = {}) {
  process.stdout.write(JSON.stringify({ event, ...payload }) + '\n');
}

async function start(attempt = 0) {
  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
  let qrTimer = null;
  let done = false;
  const version = await resolveWhatsAppVersion();

  const sock = makeWASocket({
    version,
    auth: state,
    logger,
    printQRInTerminal: false,
    browser: ['Sokoyuku Local', 'Chrome', '120.0'],
    syncFullHistory: false,
    markOnlineOnConnect: false,
    getMessage: async () => ({ conversation: '' }),
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr && !done) {
      const expiresAt = Date.now() + (Number.isFinite(QR_TIMEOUT_MS) ? QR_TIMEOUT_MS : 60000);
      emit('qr', { qr, expires_at: expiresAt });
      if (qrTimer) clearTimeout(qrTimer);
      qrTimer = setTimeout(() => {
        if (done) return;
        done = true;
        emit('expired', { reason: 'qr_timeout' });
        try { sock.end(undefined); } catch {}
        setTimeout(() => process.exit(1), 200);
      }, Number.isFinite(QR_TIMEOUT_MS) ? QR_TIMEOUT_MS : 60000);
    }

    if (connection === 'open' && !done) {
      done = true;
      if (qrTimer) clearTimeout(qrTimer);
      const id = sock.user?.id || '';
      const userId = String(id).split(':')[0].split('@')[0];
      emit('linked', { user_id: userId || id });
      setTimeout(() => process.exit(0), 1500);
    }

    if (connection === 'close') {
      const reason = new Boom(lastDisconnect?.error)?.output?.statusCode;
      if (done) return;
      if (reason === DisconnectReason.loggedOut) {
        done = true;
        emit('failed', { reason: 'logged_out' });
        process.exit(1);
      }
      // Transient closes before/after first QR: keep waiting or reconnect.
      // 515 restartRequired, 408 timedOut/connectionLost, 428 connectionClosed,
      // 503 unavailableService, 405 stale web version.
      const transient =
        reason === DisconnectReason.restartRequired ||
        reason === DisconnectReason.connectionLost ||
        reason === DisconnectReason.timedOut ||
        reason === DisconnectReason.connectionClosed ||
        reason === DisconnectReason.unavailableService ||
        reason === 405;
      if (transient && qrTimer) {
        return;
      }
      if (transient && attempt < 5) {
        setTimeout(() => start(attempt + 1), 1000);
        return;
      }
      done = true;
      emit('failed', { reason: String(reason || 'closed') });
      process.exit(1);
    }
  });
}

start().catch((err) => {
  emit('failed', { reason: String(err?.message || err || 'start_failed') });
  process.exit(1);
});
