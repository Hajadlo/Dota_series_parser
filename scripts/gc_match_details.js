#!/usr/bin/env node
/*
 * Fetch Dota 2 match details directly from Valve Game Coordinator.
 * This mirrors OpenDota's retriever approach: Steam bot login +
 * k_EMsgGCMatchDetailsRequest + CMsgGCMatchDetailsResponse decode.
 *
 * stdout: single JSON object { ok, match?, source?, error? }
 * stderr: diagnostic logs
 */
const fs = require('fs');
const path = require('path');
const SteamUser = require('steam-user');
const protobuf = require('protobufjs');

const PROJECT_ROOT = path.resolve(__dirname, '..');
const DOTA_APPID = 570;
const MATCH_TIMEOUT_MS = Number(process.env.GC_MATCH_TIMEOUT_MS || 15000);
const OVERALL_TIMEOUT_MS = Number(process.env.GC_OVERALL_TIMEOUT_MS || 45000);

function loadEnvFile(envPath) {
  if (!fs.existsSync(envPath)) return;
  const text = fs.readFileSync(envPath, 'utf8');
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#') || !line.includes('=')) continue;
    const idx = line.indexOf('=');
    const key = line.slice(0, idx).trim();
    let value = line.slice(idx + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    if (!(key in process.env)) process.env[key] = value;
  }
}

function emit(obj, code = 0) {
  process.stdout.write(JSON.stringify(obj) + '\n');
  process.exitCode = code;
}

function fail(error, detail, code = 1) {
  emit({ ok: false, error, detail }, code);
}

function normalizeMatch(obj, matchId) {
  const match = obj && (obj.match || obj);
  if (!match) return null;
  return {
    match_id: Number(match.match_id || matchId),
    cluster: Number(match.cluster || 0) || null,
    replay_salt: Number(match.replay_salt || 0) || null,
    duration: Number(match.duration || 0) || null,
    start_time: Number(match.starttime || match.start_time || 0) || null,
    series_id: Number(match.series_id || 0) || null,
    series_type: Number(match.series_type || 0) || null,
    result: match.result,
  };
}

async function main() {
  const matchId = process.argv[2];
  if (!matchId || !/^\d+$/.test(matchId)) {
    fail('Usage: node scripts/gc_match_details.js <match_id>', null, 2);
    return;
  }

  loadEnvFile(path.join(PROJECT_ROOT, '.env'));
  const username = process.env.BOT_STEAM_USERNAME;
  const password = process.env.BOT_STEAM_PASSWORD;
  if (!username || !password) {
    fail('BOT_STEAM_USERNAME/PASSWORD not set');
    return;
  }

  const protoDir = path.join(__dirname, 'proto');
  const root = new protobuf.Root();
  root.resolvePath = (_origin, target) => path.isAbsolute(target) ? target : path.join(protoDir, target);
  root.loadSync([
    'gcsystemmsgs.proto',
    'enums_clientserver.proto',
    'dota_gcmessages_msgid.proto',
    'dota_gcmessages_client.proto',
  ], { keepCase: true });

  const EGCBaseClientMsg = root.lookupEnum('EGCBaseClientMsg');
  const EDOTAGCMsg = root.lookupEnum('EDOTAGCMsg');
  const RequestType = root.lookupType('CMsgGCMatchDetailsRequest');
  const ResponseType = root.lookupType('CMsgGCMatchDetailsResponse');

  const sentryDir = path.join(PROJECT_ROOT, 'sentry');
  fs.mkdirSync(sentryDir, { recursive: true });
  const client = new SteamUser({ dataDirectory: sentryDir });

  let finished = false;
  let matchTimer = null;
  const finish = (obj, code = 0) => {
    if (finished) return;
    finished = true;
    if (matchTimer) clearTimeout(matchTimer);
    try { client.logOff(); } catch (_) {}
    emit(obj, code);
  };

  const sendHello = () => {
    console.error('GC: sending client hello');
    client.sendToGC(DOTA_APPID, EGCBaseClientMsg.values.k_EMsgGCClientHello, {}, Buffer.alloc(0));
  };

  const requestMatch = () => {
    console.error(`GC: requesting match details ${matchId}`);
    const payload = Buffer.from(RequestType.encode({ match_id: Number(matchId) }).finish());
    matchTimer = setTimeout(() => {
      finish({ ok: false, error: `Dota 2 GC did not return match details within ${MATCH_TIMEOUT_MS / 1000}s` }, 1);
    }, MATCH_TIMEOUT_MS);
    client.sendToGC(DOTA_APPID, EDOTAGCMsg.values.k_EMsgGCMatchDetailsRequest, {}, payload, (_appid, _msgType, body) => {
      if (matchTimer) clearTimeout(matchTimer);
      try {
        const decoded = ResponseType.decode(body);
        const obj = ResponseType.toObject(decoded, { longs: Number, enums: Number, defaults: false });
        const match = normalizeMatch(obj, matchId);
        if (!match) {
          finish({ ok: false, error: 'GC returned no match object', raw: obj }, 1);
        } else if (!match.replay_salt) {
          finish({ ok: false, error: 'GC returned match details without replay_salt', match, raw_result: obj.result ?? obj.match?.result }, 1);
        } else {
          finish({ ok: true, source: 'Dota 2 GC raw Node retriever', match }, 0);
        }
      } catch (err) {
        finish({ ok: false, error: `Failed to decode GC response: ${err.message}` }, 1);
      }
    });
  };

  client.on('loggedOn', () => {
    console.error(`Steam: logged on; publicIP=${client.publicIP || 'unknown'}`);
    client.gamesPlayed(DOTA_APPID, true);
  });

  client.on('appLaunched', (appid) => {
    console.error(`Steam: app launched ${appid}`);
    sendHello();
  });

  client.on('receivedFromGC', (appid, msgType, payload) => {
    console.error(`GC: received msgType=${msgType} appid=${appid} bytes=${payload.length}`);
    if (appid === DOTA_APPID && msgType === EGCBaseClientMsg.values.k_EMsgGCClientWelcome) {
      requestMatch();
    }
  });

  client.on('steamGuard', (domain) => {
    finish({ ok: false, error: `Steam Guard code required for ${domain || 'account'}` }, 1);
  });

  client.on('error', (err) => {
    finish({ ok: false, error: `Steam error: ${err.message || err}`, eresult: err.eresult }, 1);
  });

  setTimeout(() => {
    if (!finished) sendHello();
  }, 5000);

  setTimeout(() => {
    finish({ ok: false, error: `Overall GC retriever timeout after ${OVERALL_TIMEOUT_MS / 1000}s` }, 1);
  }, OVERALL_TIMEOUT_MS);

  client.logOn({ accountName: username, password });
}

main().catch((err) => fail(`Unhandled error: ${err.message}`, err.stack, 1));
