import {
  DEFAULT_CONNECTION_CONFIG,
  fetchLatestBaileysVersion,
  fetchLatestWaWebVersion,
} from "@whiskeysockets/baileys";

function isVersion(value) {
  return Array.isArray(value) && value.length === 3 && value.every((n) => Number.isInteger(n));
}

export async function resolveWhatsAppVersion() {
  try {
    const wa = await fetchLatestWaWebVersion();
    if (isVersion(wa?.version) && !wa.error) {
      return wa.version;
    }
  } catch {}
  try {
    const baileys = await fetchLatestBaileysVersion();
    if (isVersion(baileys?.version)) {
      return baileys.version;
    }
  } catch {}
  return DEFAULT_CONNECTION_CONFIG.version;
}
