const express = require('express');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const bcrypt = require('bcryptjs');
const cookieParser = require('cookie-parser');
const { Pool } = require('pg');

const app = express();
const PORT = Number(process.env.PORT || 3000);

function resolveDatabaseUrl() {
  const direct = [
    process.env.DATABASE_URL,
    process.env.DATABASE_PUBLIC_URL,
    process.env.POSTGRES_URL,
    process.env.POSTGRESQL_URL,
    process.env.PG_URL,
    process.env.RAILWAY_DATABASE_URL,
    process.env.RAILWAY_POSTGRESQL_URL
  ].find(Boolean);

  if (direct) return direct;

  const host = process.env.PGHOST || process.env.POSTGRES_HOST || process.env.DB_HOST;
  const port = process.env.PGPORT || process.env.POSTGRES_PORT || process.env.DB_PORT || '5432';
  const database = process.env.PGDATABASE || process.env.POSTGRES_DB || process.env.DB_NAME;
  const user = process.env.PGUSER || process.env.POSTGRES_USER || process.env.DB_USER;
  const password = process.env.PGPASSWORD || process.env.POSTGRES_PASSWORD || process.env.DB_PASSWORD;

  if (host && database && user) {
    const encodedUser = encodeURIComponent(user);
    const encodedPassword = encodeURIComponent(password || '');
    return `postgresql://${encodedUser}:${encodedPassword}@${host}:${port}/${database}`;
  }

  return '';
}

const DATABASE_URL = resolveDatabaseUrl();
const SESSION_COOKIE_NAME = process.env.SESSION_COOKIE_NAME || 'sbs_web_session';
const SESSION_TTL_HOURS = Number(process.env.SESSION_TTL_HOURS || 24 * 30);
const TELEGRAM_LOGIN_TOKEN_TTL_MINUTES = Number(process.env.TELEGRAM_LOGIN_TOKEN_TTL_MINUTES || 15);
const TELEGRAM_LINK_TTL_MINUTES = Number(process.env.TELEGRAM_LINK_TTL_MINUTES || 15);
const TELEGRAM_CODE_TTL_MINUTES = Number(process.env.TELEGRAM_CODE_TTL_MINUTES || 10);
const TELEGRAM_CODE_MAX_ATTEMPTS = Number(process.env.TELEGRAM_CODE_MAX_ATTEMPTS || 5);
const TELEGRAM_CODE_LENGTH = Number(process.env.TELEGRAM_CODE_LENGTH || 6);
const PASSWORD_SALT_ROUNDS = Number(process.env.PASSWORD_SALT_ROUNDS || 12);
const APP_BASE_URL = (process.env.APP_BASE_URL || '').replace(/\/$/, '');
const BOT_USERNAME = (process.env.BOT_USERNAME || 'sbsmanager_bot').replace(/^@/, '');
const TELEGRAM_BOT_LOGIN_SECRET = process.env.TELEGRAM_BOT_LOGIN_SECRET || 'change-me-before-prod';
const WEB_INTERNAL_API_KEY = process.env.WEB_INTERNAL_API_KEY || 'change-me-before-prod';
const COOKIE_SECURE = String(process.env.COOKIE_SECURE || 'true') === 'true';
const PRICE_RUB_DEFAULT = Number(process.env.PRICE_RUB || 199);
function firstPresent(...values) {
  for (const value of values) {
    const normalized = String(value || '').trim();
    if (normalized) return normalized;
  }
  return '';
}
const PLATEGA_MERCHANT_ID = firstPresent(
  process.env.PLATEGA_MERCHANT_ID,
  process.env.MERCHANT_ID,
  '6de47e07-f542-4433-9dee-09b128cfdb64'
);
const PLATEGA_SECRET = firstPresent(
  process.env.PLATEGA_SECRET,
  process.env.PAYMENT_SECRET,
  'E2aussBRiYoSWYMidtT9dkP8qQryLH97gfRSTh7L46Z0UMOvX0xzmqWd51yHl108FxxYQPPWqVuXwExkDNmj3FX9D5ywsG4widEx'
);
const PLATEGA_PAYMENT_METHOD = Number(firstPresent(process.env.PLATEGA_PAYMENT_METHOD, process.env.PAYMENT_METHOD, 2));
const PLATEGA_RETURN_URL = firstPresent(process.env.PLATEGA_RETURN_URL, process.env.PAYMENT_RETURN_URL, `${APP_BASE_URL || ''}/cabinet?payment=success`);
const PLATEGA_FAILED_URL = firstPresent(process.env.PLATEGA_FAILED_URL, process.env.PAYMENT_FAILED_URL, `${APP_BASE_URL || ''}/cabinet?payment=failed`);
const VPN_KEY_ENC_SECRET = (process.env.VPN_KEY_ENC_SECRET || '').trim();
const VPN_BACKEND_BASE_URL = (process.env.VPN_BACKEND_BASE_URL || '').replace(/\/$/, '');
const VPN_BACKEND_SHARED_SECRET = (process.env.VPN_BACKEND_SHARED_SECRET || '').trim();
const FAMILY_SEAT_PRICE_RUB_DEFAULT = Number(process.env.FAMILY_SEAT_PRICE_RUB || 0);

const dbConfigured = Boolean(DATABASE_URL);

const pool = dbConfigured
  ? new Pool({
      connectionString: DATABASE_URL,
      ssl: process.env.PGSSL === 'disable' ? false : { rejectUnauthorized: false }
    })
  : null;

app.use(express.json({ limit: '8mb' }));
app.use(express.urlencoded({ extended: false, limit: '8mb' }));
app.use(cookieParser());
app.use(express.static(path.join(__dirname, 'public')));

function asyncHandler(fn) {
  return function wrapped(req, res, next) {
    Promise.resolve(fn(req, res, next)).catch(next);
  };
}

function now() {
  return new Date();
}

function randomToken(bytes = 32) {
  return crypto.randomBytes(bytes).toString('base64url');
}

function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function randomDigits(length = 6) {
  let out = '';
  while (out.length < length) {
    out += crypto.randomInt(0, 10).toString();
  }
  return out.slice(0, length);
}

function hashTelegramVerificationCode(selector, code) {
  return sha256(`tg-code:${selector}:${code}`);
}

function hmac(value) {
  return crypto.createHmac('sha256', TELEGRAM_BOT_LOGIN_SECRET).update(value).digest('hex');
}

function addMinutes(date, minutes) {
  return new Date(date.getTime() + minutes * 60 * 1000);
}

function addHours(date, hours) {
  return new Date(date.getTime() + hours * 60 * 60 * 1000);
}

function getClientIp(req) {
  return (req.headers['x-forwarded-for'] || req.socket.remoteAddress || '').toString().split(',')[0].trim().slice(0, 128);
}

function getUserAgent(req) {
  return (req.headers['user-agent'] || '').toString().slice(0, 512);
}

function maskEmail(email) {
  if (!email || !email.includes('@')) return email || '';
  const [name, domain] = email.split('@');
  const safeName = name.length <= 2 ? `${name[0] || '*'}*` : `${name.slice(0, 2)}***`;
  return `${safeName}@${domain}`;
}


function safeDateIso(value) {
  if (!value) return null;
  const d = new Date(value);
  return Number.isFinite(d.getTime()) ? d.toISOString() : null;
}

function ensurePublicUploadDir() {
  const dir = path.join(__dirname, 'public', 'uploads', 'avatars');
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function avatarPublicUrl(fileName) {
  return `/uploads/avatars/${fileName}`;
}

function parseDataUrlImage(dataUrl) {
  const raw = String(dataUrl || '').replace(/\s+/g, '');
  const match = raw.match(/^data:(image\/(png|jpeg|jpg|webp));base64,([A-Za-z0-9+/=]+)$/i);
  if (!match) return null;
  const mime = match[1].toLowerCase();
  const ext = mime.includes('png') ? 'png' : mime.includes('webp') ? 'webp' : 'jpg';
  const buffer = Buffer.from(match[3], 'base64');
  if (!buffer.length || buffer.length > 5 * 1024 * 1024) return null;
  return { mime, ext, buffer };
}


async function attachPendingReferrerByCode(referredTgId, refCode) {
  const cleanCode = String(refCode || '').trim();
  const tgId = Number(referredTgId || 0);
  if (!cleanCode || !tgId) return null;
  try {
    const referrer = (await query(`select tg_id from users where ref_code = $1 limit 1`, [cleanCode])).rows[0];
    if (!referrer || Number(referrer.tg_id) === tgId) return null;
    await query(
      `insert into users (tg_id, referred_by_tg_id, referred_at)
       values ($1, $2, now())
       on conflict (tg_id) do nothing`,
      [tgId, Number(referrer.tg_id)]
    );
    const existing = (await query(`select referred_by_tg_id from users where tg_id = $1 limit 1`, [tgId])).rows[0];
    if (existing && existing.referred_by_tg_id) return null;
    await query(`update users set referred_by_tg_id = $2, referred_at = coalesce(referred_at, now()) where tg_id = $1`, [tgId, Number(referrer.tg_id)]);
    return Number(referrer.tg_id);
  } catch (error) {
    console.warn('attachPendingReferrerByCode failed', error?.message || error);
    return null;
  }
}

async function ensureWebAccountRefCode(webAccountId) {
  const accountId = Number(webAccountId || 0);
  if (!accountId) return '';
  const existing = (await query(`select ref_code from web_accounts where id = $1 limit 1`, [accountId])).rows[0]?.ref_code;
  if (existing) return String(existing);
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const code = `w${randomToken(6).replace(/[^a-zA-Z0-9]/g, '').slice(0, 10)}`.toLowerCase();
    if (!code) continue;
    const updated = await query(`update web_accounts set ref_code = $2 where id = $1 and (ref_code is null or ref_code = '') returning ref_code`, [accountId, code]);
    if (updated.rows[0]?.ref_code) return String(updated.rows[0].ref_code);
    const nowExisting = (await query(`select ref_code from web_accounts where id = $1 limit 1`, [accountId])).rows[0]?.ref_code;
    if (nowExisting) return String(nowExisting);
  }
  return '';
}


function urlSafeBase64ToBuffer(value) {
  const normalized = String(value || '').replace(/-/g, '+').replace(/_/g, '/');
  const padding = normalized.length % 4 === 0 ? '' : '='.repeat(4 - (normalized.length % 4));
  return Buffer.from(normalized + padding, 'base64');
}

function resolveFernetRawKey(secret) {
  if (!secret) return null;
  if (secret.length >= 40 && /^[A-Za-z0-9_-]+$/.test(secret)) {
    try { return urlSafeBase64ToBuffer(secret); } catch (_) {}
  }
  return crypto.hkdfSync('sha256', Buffer.from(secret, 'utf8'), Buffer.from('sbs-vpn-key-v1', 'utf8'), Buffer.from('vpn-private-key', 'utf8'), 32);
}

function decryptVpnPrivateKey(value) {
  if (!value) return '';
  if (!VPN_KEY_ENC_SECRET) return String(value);
  try {
    const rawKey = resolveFernetRawKey(VPN_KEY_ENC_SECRET);
    if (!rawKey || rawKey.length !== 32) return String(value);
    const token = urlSafeBase64ToBuffer(value);
    if (token.length < 57 || token[0] !== 0x80) return String(value);
    const signingKey = rawKey.subarray(0, 16);
    const encryptionKey = rawKey.subarray(16, 32);
    const data = token.subarray(0, token.length - 32);
    const sentHmac = token.subarray(token.length - 32);
    const calcHmac = crypto.createHmac('sha256', signingKey).update(data).digest();
    if (!crypto.timingSafeEqual(sentHmac, calcHmac)) return String(value);
    const iv = token.subarray(9, 25);
    const ciphertext = token.subarray(25, token.length - 32);
    const decipher = crypto.createDecipheriv('aes-128-cbc', encryptionKey, iv);
    decipher.setAutoPadding(true);
    const plain = Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString('utf8');
    return plain || String(value);
  } catch (_) {
    return String(value);
  }
}

function getVpnServersConfig() {
  const raw = String(process.env.VPN_SERVERS_JSON || process.env.VPN_SERVERS || '').trim();
  let items = [];
  if (raw) {
    try {
      const parsed = JSON.parse(raw);
      const list = Array.isArray(parsed) ? parsed : (Array.isArray(parsed?.servers) ? parsed.servers : []);
      items = list.filter((item) => item && typeof item === 'object');
    } catch (_) {}
  }
  if (items.length) return items;
  const code = String(process.env.VPN_CODE || 'NL').trim().toUpperCase();
  return [{
    code,
    name: process.env.VPN_NAME || code,
    server_public_key: process.env.VPN_SERVER_PUBLIC_KEY || '',
    endpoint: process.env.VPN_ENDPOINT || '',
    dns: process.env.VPN_DNS || '1.1.1.1'
  }];
}

function resolveVpnServerConfig(serverCode) {
  const code = String(serverCode || '').trim().toUpperCase();
  const servers = getVpnServersConfig();
  const direct = servers.find((item) => String(item.code || '').trim().toUpperCase() === code);
  return direct || servers[0] || null;
}

function buildWireGuardConfig({ privateKey, clientIp, serverCode }) {
  const server = resolveVpnServerConfig(serverCode);
  if (!privateKey || !clientIp || !server) {
    const error = new Error('VPN_CONFIG_UNAVAILABLE');
    error.code = 'VPN_CONFIG_UNAVAILABLE';
    throw error;
  }
  return [
    '[Interface]',
    `PrivateKey = ${privateKey}`,
    `Address = ${clientIp}/32`,
    `DNS = ${server.dns || process.env.VPN_DNS || '1.1.1.1'}`,
    '',
    '[Peer]',
    `PublicKey = ${server.server_public_key || process.env.VPN_SERVER_PUBLIC_KEY || ''}`,
    `Endpoint = ${server.endpoint || process.env.VPN_ENDPOINT || ''}`,
    'AllowedIPs = 0.0.0.0/0',
    'PersistentKeepalive = 25',
    ''
  ].join('\n');
}


async function getSettingInt(key, fallback) {
  try {
    const { rows } = await query(`select int_value from app_settings where key = $1 limit 1`, [key]);
    const value = Number(rows[0]?.int_value || 0);
    return value > 0 ? value : fallback;
  } catch (_) {
    return fallback;
  }
}

async function getPriceRub() {
  try {
    const { rows } = await query(`select int_value from app_settings where key = 'price_rub' limit 1`);
    const value = Number(rows[0]?.int_value || 0);
    return value > 0 ? value : PRICE_RUB_DEFAULT;
  } catch (_) {
    return PRICE_RUB_DEFAULT;
  }
}

async function getFamilySeatPriceRub() {
  return getSettingInt('family_seat_price_rub', FAMILY_SEAT_PRICE_RUB_DEFAULT || PRICE_RUB_DEFAULT);
}

async function upsertAppSetting(key, value) {
  const normalized = value === null || value === undefined || value === '' ? null : String(value);
  await query(
    `insert into app_settings (key, value)
     values ($1, $2)
     on conflict (key) do update set value = excluded.value, updated_at = now()`,
    [key, normalized]
  );
}


async function createPlategaCheckout({ subject = null, tgId = null, amountRub, periodMonths = 1, periodDays = 30, description = null, payloadSource = 'web' }) {
  if (!PLATEGA_MERCHANT_ID || !PLATEGA_SECRET) {
    const error = new Error('PAYMENTS_DISABLED');
    error.code = 'PAYMENTS_DISABLED';
    error.meta = { merchant_present: Boolean(PLATEGA_MERCHANT_ID), secret_present: Boolean(PLATEGA_SECRET) };
    throw error;
  }
  const response = await fetch('https://app.platega.io/transaction/process', {
    method: 'POST',
    headers: {
      'X-MerchantId': PLATEGA_MERCHANT_ID,
      'X-Secret': PLATEGA_SECRET,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      paymentMethod: PLATEGA_PAYMENT_METHOD,
      paymentDetails: { amount: Number(amountRub), currency: 'RUB' },
      description: description || `Подписка SBS CONNECT (${periodMonths} мес, ${subject || (tgId ? `TG ${tgId}` : 'WEB')})`,
      return: PLATEGA_RETURN_URL,
      failedUrl: PLATEGA_FAILED_URL,
      payload: `${subject || (tgId ? `tg_id=${tgId}` : 'subject=web')};period=${periodMonths}m;source=${payloadSource}`
    })
  });
  const data = await response.json().catch(async () => ({ _raw: await response.text().catch(() => '') }));
  if (!response.ok) {
    const error = new Error('PAYMENT_PROVIDER_ERROR');
    error.code = 'PAYMENT_PROVIDER_ERROR';
    error.meta = data;
    throw error;
  }
  const transactionId = String(data.transactionId || data.id || '').trim();
  const paymentUrl = String(data.redirect || '').trim();
  if (!transactionId || !paymentUrl) {
    const error = new Error('PAYMENT_PROVIDER_ERROR');
    error.code = 'PAYMENT_PROVIDER_ERROR';
    error.meta = data;
    throw error;
  }
  return {
    transactionId,
    paymentUrl,
    status: String(data.status || 'PENDING').trim() || 'PENDING',
    periodMonths,
    periodDays,
  };
}

async function query(sql, params = []) {
  if (!pool) {
    const error = new Error('Database is not configured');
    error.code = 'DB_NOT_CONFIGURED';
    throw error;
  }
  return pool.query(sql, params);
}

async function runMigrations() {
  const sqlPath = path.join(__dirname, 'sql', 'init-web-auth.sql');
  const sql = fs.readFileSync(sqlPath, 'utf8');
  await query(sql);
}

async function writeAudit({ accountId = null, tgId = null, eventType, status = 'ok', req = null, meta = {} }) {
  await query(
    `insert into web_audit_log (web_account_id, tg_id, event_type, status, ip_address, user_agent, meta)
     values ($1, $2, $3, $4, $5, $6, $7::jsonb)`,
    [accountId, tgId, eventType, status, req ? getClientIp(req) : null, req ? getUserAgent(req) : null, JSON.stringify(meta || {})]
  );
}

async function getSession(req) {
  const rawToken = req.cookies[SESSION_COOKIE_NAME];
  if (!rawToken) return null;
  const tokenHash = sha256(rawToken);
  const { rows } = await query(
    `select s.id, s.web_account_id, s.expires_at, s.revoked_at, a.email, a.tg_id, a.display_name
     from web_sessions s
     join web_accounts a on a.id = s.web_account_id
     where s.session_token_hash = $1
     limit 1`,
    [tokenHash]
  );
  const session = rows[0];
  if (!session) return null;
  if (session.revoked_at || new Date(session.expires_at) <= now()) {
    return null;
  }
  return session;
}

async function requireSession(req, res, next) {
  const session = await getSession(req);
  if (!session) {
    return res.status(401).json({ ok: false, error: 'AUTH_REQUIRED' });
  }
  req.sessionData = session;
  next();
}

function setSessionCookie(res, token, expiresAt) {
  res.cookie(SESSION_COOKIE_NAME, token, {
    httpOnly: true,
    sameSite: 'lax',
    secure: COOKIE_SECURE,
    expires: expiresAt,
    path: '/'
  });
}

async function createSession(webAccountId, req) {
  const rawToken = randomToken(32);
  const tokenHash = sha256(rawToken);
  const expiresAt = addHours(now(), SESSION_TTL_HOURS);
  await query(
    `insert into web_sessions (web_account_id, session_token_hash, expires_at, ip_address, user_agent)
     values ($1, $2, $3, $4, $5)`,
    [webAccountId, tokenHash, expiresAt, req ? getClientIp(req) : null, req ? getUserAgent(req) : null]
  );
  return { rawToken, expiresAt };
}



async function getOrCreateTelegramWebAccount(tgId) {
  const existing = await query(`select id from web_accounts where tg_id = $1 limit 1`, [tgId]);
  if (existing.rows[0]) return existing.rows[0].id;

  const profile = await query(`select tg_username, first_name, last_name from users where tg_id = $1 limit 1`, [tgId]);
  const user = profile.rows[0] || {};
  const fallbackEmail = `tg-${tgId}@telegram.local`;
  const displayName = [user.first_name, user.last_name].filter(Boolean).join(' ') || user.tg_username || `Telegram ${tgId}`;
  const created = await query(
    `insert into web_accounts (email, display_name, tg_id, auth_source, email_verified_at)
     values ($1, $2, $3, 'telegram', now())
     on conflict (tg_id) do update set display_name = excluded.display_name
     returning id`,
    [fallbackEmail, displayName, tgId]
  );
  return created.rows[0].id;
}

async function consumeTelegramLoginToken({ token, req, res }) {
  const tokenHash = sha256(token);
  const { rows } = await query(
    `select id, selector, web_account_id, tg_id, status, expires_at, consumed_at,
            verification_code_hash, verification_verified_at
     from telegram_login_tokens
     where token_hash = $1
     limit 1`,
    [tokenHash]
  );
  const loginToken = rows[0];
  if (!loginToken) {
    const error = new Error('TOKEN_NOT_FOUND');
    error.code = 'TOKEN_NOT_FOUND';
    throw error;
  }
  if (loginToken.status !== 'approved' || loginToken.consumed_at || new Date(loginToken.expires_at) <= now()) {
    const error = new Error('TOKEN_EXPIRED');
    error.code = 'TOKEN_EXPIRED';
    throw error;
  }
  if (loginToken.verification_code_hash && !loginToken.verification_verified_at) {
    const error = new Error('CODE_REQUIRED');
    error.code = 'CODE_REQUIRED';
    throw error;
  }

  let webAccountId = loginToken.web_account_id;
  if (!webAccountId) {
    webAccountId = await getOrCreateTelegramWebAccount(loginToken.tg_id);
    await query(`update telegram_login_tokens set web_account_id = $1 where id = $2`, [webAccountId, loginToken.id]);
  }

  await query(
    `update telegram_login_tokens
     set status = 'used', consumed_at = now()
     where id = $1`,
    [loginToken.id]
  );

  const session = await createSession(webAccountId, req);
  setSessionCookie(res, session.rawToken, session.expiresAt);
  await writeAudit({ accountId: webAccountId, tgId: loginToken.tg_id, eventType: 'telegram.login.complete', req, meta: { token_id: loginToken.id } });
  return { webAccountId, tgId: loginToken.tg_id };
}

async function buildAccountPayload(webAccountId) {
  const { rows } = await query(
    `select a.id, a.email, a.display_name, a.tg_id, a.email_verified_at, null::timestamptz as created_at,
            a.avatar_url, a.referred_by_code, a.ref_code as web_ref_code,
            u.tg_username, u.first_name, u.last_name, u.ref_code,
            s.is_active as subscription_active, s.status as subscription_status, s.start_at, s.end_at
     from web_accounts a
     left join users u on u.tg_id = a.tg_id
     left join subscriptions s on s.tg_id = a.tg_id
     where a.id = $1
     limit 1`,
    [webAccountId]
  );
  const account = rows[0];
  if (!account) return null;

  if (!account.ref_code && !account.web_ref_code) {
    account.web_ref_code = await ensureWebAccountRefCode(account.id);
  }

  const displayName = account.display_name
    || [account.first_name, account.last_name].filter(Boolean).join(' ')
    || (account.tg_username ? '@' + account.tg_username : null)
    || account.email;

  const isTelegramUser = Boolean(account.tg_id);
  const hasActiveSubscription = Boolean(account.subscription_active);
  const effectiveRefCode = account.ref_code || account.web_ref_code || null;
  const botReferralLink = account.ref_code && BOT_USERNAME ? `https://t.me/${BOT_USERNAME}?start=ref_${account.ref_code}` : null;
  const refLink = effectiveRefCode && APP_BASE_URL ? `${APP_BASE_URL}/r/${effectiveRefCode}` : botReferralLink;
  const botUrl = `https://t.me/${BOT_USERNAME}`;
  const nowDate = now();

  const payments = account.tg_id ? (await query(
    `select id,
            amount,
            coalesce(currency, 'RUB') as currency,
            provider,
            status,
            paid_at,
            period_days,
            period_months
     from payments
     where tg_id = $1
     order by paid_at desc nulls last, id desc
     limit 20`,
    [account.tg_id]
  )).rows : [];

  const referrals = account.tg_id ? (await query(
    `select r.id, r.status, r.activated_at, u.first_name, u.last_name, u.tg_username, r.referred_tg_id
     from referrals r
     left join users u on u.tg_id = r.referred_tg_id
     where r.referrer_tg_id = $1
     order by r.id desc
     limit 50`,
    [account.tg_id]
  )).rows : [];

  const referralEarnings = account.tg_id ? (await query(
    `select id, earned_rub, status, available_at, paid_at, percent
     from referral_earnings
     where referrer_tg_id = $1
     order by coalesce(available_at, paid_at) desc nulls last, id desc
     limit 50`,
    [account.tg_id]
  )).rows : [];

  const referralActiveCount = account.tg_id ? Number((await query(
    `select count(*)::int as count from referrals where referrer_tg_id = $1 and status = 'active'`,
    [account.tg_id]
  )).rows[0]?.count || 0) : 0;

  const referralBalances = account.tg_id ? (await query(
    `select
       coalesce(sum(case when status = 'available' then earned_rub else 0 end), 0)::int as available_rub,
       coalesce(sum(case when status = 'pending' then earned_rub else 0 end), 0)::int as pending_rub,
       coalesce(sum(case when status = 'paid' then earned_rub else 0 end), 0)::int as paid_rub,
       coalesce(sum(earned_rub), 0)::int as total_rub
     from referral_earnings where referrer_tg_id = $1`,
    [account.tg_id]
  )).rows[0] || null : null;

  const referralOverride = account.tg_id ? (await query(
    `select int_value from app_settings where key = $1 limit 1`,
    [`referral_percent_override:${account.tg_id}`]
  )).rows[0] || null : null;

  const levelPercent = (activeReferrals) => activeReferrals >= 10 ? 17 : activeReferrals >= 4 ? 11 : 5;
  const nextLevelInfo = (activeReferrals) => activeReferrals < 4 ? { next_percent: 11, next_target_active: 4 } : activeReferrals < 10 ? { next_percent: 17, next_target_active: 10 } : { next_percent: null, next_target_active: null };
  const referralOverrideValue = referralOverride && referralOverride.int_value !== null ? Math.max(0, Math.min(100, Number(referralOverride.int_value))) : null;
  const referralCurrentPercent = referralOverrideValue !== null ? referralOverrideValue : levelPercent(referralActiveCount);
  const referralNext = referralOverrideValue !== null ? { next_percent: null, next_target_active: null } : nextLevelInfo(referralActiveCount);

  const vpnPeer = account.tg_id ? (await query(
    `select id, client_public_key, server_code
     from vpn_peers
     where tg_id = $1 and is_active = true and revoked_at is null
     order by id desc
     limit 1`,
    [account.tg_id]
  )).rows[0] || null : null;

  let yandexMembership = account.tg_id ? (await query(
    `select ym.id, ym.status, ym.invite_link, ym.invite_issued_at, ym.invite_expires_at,
            ym.coverage_end_at, ym.account_label, ym.slot_index, ym.yandex_login,
            ym.yandex_account_id, ym.removed_at,
            ya.label as yandex_account_label, ya.max_slots, ya.used_slots,
            ya.plus_end_at, ya.status as yandex_account_status
     from yandex_memberships ym
     left join yandex_accounts ya on ya.id = ym.yandex_account_id
     where ym.tg_id = $1
     order by case when ym.removed_at is null then 0 else 1 end asc,
              coalesce(ym.coverage_end_at, ym.invite_expires_at, ym.invite_issued_at) desc nulls last,
              ym.id desc
     limit 1`,
    [account.tg_id]
  )).rows[0] || null : null;

  if (!yandexMembership && account.tg_id) {
    yandexMembership = (await query(
      `select ys.id, 'issued'::text as status, ys.invite_link, ys.issued_at as invite_issued_at, null::timestamptz as invite_expires_at,
              ya.plus_end_at as coverage_end_at, ya.label as account_label, ys.slot_index, null::text as yandex_login,
              ys.yandex_account_id, null::timestamptz as removed_at,
              ya.label as yandex_account_label, ya.max_slots, ya.used_slots,
              ya.plus_end_at, ya.status as yandex_account_status
       from yandex_invite_slots ys
       left join yandex_accounts ya on ya.id = ys.yandex_account_id
       where ys.issued_to_tg_id = $1
       order by coalesce(ys.issued_at, ya.plus_end_at) desc nulls last, ys.id desc
       limit 1`,
      [account.tg_id]
    )).rows[0] || null;
  }

  if (!yandexMembership && account.tg_id) {
    yandexMembership = (await query(
      `select ym.id, ym.status, ym.invite_link, ym.invite_issued_at, ym.invite_expires_at,
              ym.coverage_end_at, ym.account_label, ym.slot_index, ym.yandex_login,
              ym.yandex_account_id, ym.removed_at,
              ya.label as yandex_account_label, ya.max_slots, ya.used_slots,
              ya.plus_end_at, ya.status as yandex_account_status
       from yandex_memberships ym
       left join yandex_accounts ya on ya.id = ym.yandex_account_id
       where ym.tg_id = $1
       order by coalesce(ym.coverage_end_at, ym.invite_expires_at, ym.invite_issued_at, ya.plus_end_at) desc nulls last, ym.id desc
       limit 1`,
      [account.tg_id]
    )).rows[0] || null;
  }

  if (!yandexMembership) {
    yandexMembership = (await query(
      `select ya.id, coalesce(ya.status, 'active') as status, null::text as invite_link, null::timestamptz as invite_issued_at, null::timestamptz as invite_expires_at,
              ya.plus_end_at as coverage_end_at, ya.label as account_label, null::int as slot_index, null::text as yandex_login,
              ya.id as yandex_account_id, null::timestamptz as removed_at,
              ya.label as yandex_account_label, ya.max_slots, ya.used_slots, ya.plus_end_at, ya.status as yandex_account_status
       from yandex_accounts ya
       where coalesce(ya.status, 'active') in ('active','issued','ready')
       order by coalesce(ya.plus_end_at, now()) desc nulls last, ya.id desc
       limit 1`
    )).rows[0] || null;
  }

  let yandexSeatsUsed = 0;
  if (yandexMembership?.yandex_account_id) {
    const counted = await query(
      `select count(*)::int as count
       from yandex_memberships
       where yandex_account_id = $1
         and removed_at is null
         and status in ('awaiting_join', 'issued', 'active')`,
      [yandexMembership.yandex_account_id]
    );
    yandexSeatsUsed = Math.max(0, Number(counted.rows[0]?.count || 0) - 1);
  }

  const yandexCoverageValid = yandexMembership?.coverage_end_at
    ? new Date(yandexMembership.coverage_end_at) > nowDate
    : false;
  const yandexInvitePresent = Boolean(String(yandexMembership?.invite_link || '').trim());
  const yandexStatusNormalized = String(yandexMembership?.status || '').toLowerCase();
  const yandexIsCurrent = Boolean(
    yandexMembership
    && !yandexMembership.removed_at
    && (yandexCoverageValid || yandexInvitePresent || ['active','issued','awaiting_join'].includes(yandexStatusNormalized))
  );
  const yandexInviteLink = yandexInvitePresent ? String(yandexMembership.invite_link).trim() : null;

  let familyGroup = null;
  let familyProfiles = [];
  if (account.tg_id) {
    try {
      familyGroup = (await query(`select id, owner_tg_id, seats_total, active_until, billing_opt_in from family_vpn_groups where owner_tg_id = $1 limit 1`, [account.tg_id])).rows[0] || null;
      familyProfiles = (await query(`select fp.id, fp.slot_no, fp.label, fp.vpn_peer_id, fp.expires_at, fp.is_paused, vp.client_public_key, vp.client_private_key_enc, vp.client_ip, vp.server_code, vp.is_active as vpn_is_active, vp.revoked_at from family_vpn_profiles fp left join vpn_peers vp on vp.id = fp.vpn_peer_id where fp.owner_tg_id = $1 order by fp.slot_no asc`, [account.tg_id])).rows || [];
    } catch (_) {
      familyGroup = null;
      familyProfiles = [];
    }
  }

  const isProfilePaid = (profile) => Boolean(profile?.expires_at && new Date(profile.expires_at) > nowDate);
  const familySeatsTotal = Math.max(0, Number(familyGroup?.seats_total || 0));
  const familyVisibleProfiles = familyProfiles.filter((item) => Number(item.slot_no || 0) > 0 && Number(item.slot_no || 0) <= familySeatsTotal);
  const familyActiveProfiles = familyVisibleProfiles.filter((item) => isProfilePaid(item));
  const familyFreeProfiles = familyActiveProfiles.filter((item) => !item.vpn_peer_id);
  let familyNearestProfile = null;
  for (const item of familyVisibleProfiles) {
    if (!item?.expires_at) continue;
    if (!familyNearestProfile || new Date(item.expires_at) < new Date(familyNearestProfile.expires_at)) familyNearestProfile = item;
  }
  const familyActiveUntil = familyGroup?.active_until || familyActiveProfiles.reduce((acc, item) => !item?.expires_at ? acc : (!acc || new Date(item.expires_at) > new Date(acc) ? item.expires_at : acc), null);
  const familyIsCurrent = Boolean(familyActiveUntil && new Date(familyActiveUntil) > nowDate);
  const familySeatPriceRub = await getFamilySeatPriceRub();

  const services = [];
  if (hasActiveSubscription) {
    services.push('Безопасный VPN');
    services.push('Обход блокировок');
    services.push('Антилаг Telegram');
    services.push('VPN LTE');
  }
  if (familyIsCurrent) services.push('Семейная группа VPN');
  if (yandexIsCurrent) services.push('Yandex Plus');

  const actions = [
    { key: 'pay', label: hasActiveSubscription ? 'Продлить подписку' : 'Оплатить подписку', type: 'api', href: '/api/payments/checkout' },
    { key: 'config_download', label: hasActiveSubscription ? 'Скачать конфиг' : 'Конфиг откроется после оплаты', type: hasActiveSubscription ? 'download' : 'disabled', href: hasActiveSubscription ? '/api/vpn/download-config' : null },
    { key: 'family', label: 'Семейная группа VPN', type: 'view', href: null },
    { key: 'yandex', label: yandexIsCurrent ? 'Открыть приглашение в Яндекс Семью' : 'Яндекс Семья', type: yandexInviteLink ? 'copy' : 'disabled', value: yandexInviteLink, href: yandexInviteLink },
    { key: 'ref', label: refLink ? 'Скопировать реферальную ссылку' : 'Реферальная ссылка появится после первой активации', type: refLink ? 'copy' : 'disabled', value: refLink }
  ];

  const progressTotalDays = account.start_at && account.end_at ? Math.max(1, Math.ceil((new Date(account.end_at).getTime() - new Date(account.start_at).getTime()) / 86400000)) : 0;
  const progressElapsedDays = account.start_at ? Math.max(0, Math.min(progressTotalDays || 0, Math.ceil((nowDate.getTime() - new Date(account.start_at).getTime()) / 86400000))) : 0;
  const progressRemainingDays = account.end_at ? Math.max(0, Math.ceil((new Date(account.end_at).getTime() - nowDate.getTime()) / 86400000)) : 0;

  return {
    account: {
      id: account.id, email: account.email, email_masked: maskEmail(account.email), display_name: displayName, tg_id: account.tg_id,
      tg_username: account.tg_username, first_name: account.first_name, last_name: account.last_name, email_verified_at: account.email_verified_at,
      created_at: account.created_at, user_type: isTelegramUser ? 'telegram' : 'email', ref_code: effectiveRefCode, referral_link: refLink, bot_referral_link: botReferralLink, bot_url: botUrl, avatar_url: account.avatar_url || null
    },
    subscription: {
      is_active: hasActiveSubscription, status: account.subscription_status || 'inactive', start_at: account.start_at, end_at: account.end_at,
      progress_total_days: progressTotalDays, progress_elapsed_days: progressElapsedDays, progress_remaining_days: progressRemainingDays,
      can_get_config: hasActiveSubscription,
      config_locked_reason: hasActiveSubscription ? null : 'Оплатите или продлите подписку, чтобы активировать VPN-конфиг и остальные сервисы.',
      config_key: vpnPeer?.client_public_key || null, config_url: hasActiveSubscription ? '/api/vpn/download-config' : null,
      config_reset_url: hasActiveSubscription ? '/api/vpn/reset-config' : null, can_reset_config: hasActiveSubscription,
      vpn_peer_id: vpnPeer?.id || null, vpn_server_code: vpnPeer?.server_code || null
    },
    services,
    payments,
    referrals,
    referral_earnings: referralEarnings,
    referral_summary: {
      active_count: referralActiveCount, current_percent: referralCurrentPercent, has_override: referralOverrideValue !== null,
      next_percent: referralNext.next_percent, next_target_active: referralNext.next_target_active,
      referrals_left_to_next: referralNext.next_target_active !== null ? Math.max(0, referralNext.next_target_active - referralActiveCount) : null,
      available_rub: Number(referralBalances?.available_rub || 0), pending_rub: Number(referralBalances?.pending_rub || 0), paid_rub: Number(referralBalances?.paid_rub || 0), total_earned_rub: Number(referralBalances?.total_rub || 0)
    },
    family: {
      seat_price_rub: familySeatPriceRub,
      group: familyGroup ? {
        source: 'family_vpn_groups', id: familyGroup.id, seats_total: familySeatsTotal, active_until: familyActiveUntil,
        billing_opt_in: Boolean(familyGroup.billing_opt_in), active_profiles: familyActiveProfiles.length, free_profiles: familyFreeProfiles.length,
        nearest_slot_no: familyNearestProfile ? Number(familyNearestProfile.slot_no || 0) : null, nearest_expires_at: familyNearestProfile?.expires_at || null, is_current: familyIsCurrent
      } : null,
      profiles: familyVisibleProfiles.map((item) => ({
        id: item.id, slot_no: Number(item.slot_no || 0), label: item.label || null, vpn_peer_id: item.vpn_peer_id || null, expires_at: item.expires_at || null,
        is_paused: Boolean(item.is_paused), is_paid: isProfilePaid(item), is_shareable: Boolean(isProfilePaid(item) && !item.is_paused), has_config: Boolean(item.vpn_peer_id),
        server_code: item.server_code || null, vpn_is_active: item.vpn_is_active === true, revoked_at: item.revoked_at || null
      }))
    },
    yandex: {
      enabled: Boolean(yandexMembership),
      group: yandexMembership ? {
        source: 'yandex_memberships',
        id: yandexMembership.id,
        status: yandexMembership.status,
        account_label: yandexMembership.account_label || yandexMembership.yandex_account_label || null,
        slot_index: yandexMembership.slot_index,
        yandex_login: yandexMembership.yandex_login,
        invite_link: yandexInviteLink,
        invite_issued_at: yandexMembership.invite_issued_at,
        invite_expires_at: yandexMembership.invite_expires_at,
        coverage_end_at: yandexMembership.coverage_end_at,
        removed_at: yandexMembership.removed_at,
        seats_total: Number(yandexMembership.max_slots || 0) || 5,
        seats_used: Math.max(0, yandexSeatsUsed || Number(yandexMembership.used_slots || 0) || (yandexInviteLink ? 1 : 0)),
        plus_end_at: yandexMembership.plus_end_at,
        account_status: yandexMembership.yandex_account_status,
        is_current: yandexIsCurrent
      } : null
    },
    actions,
    security: {
      session_transport: 'HTTPS/TLS', session_storage: 'sha256(session_token)', telegram_links: 'одноразовые токены + HMAC подпись + TTL + single-use',
      encryption_note: 'Это не сквозное шифрование. Корректно: транспорт защищён TLS, а сами токены не хранятся в открытом виде.'
    }
  };
}

app.get('/r/:refCode', asyncHandler(async (req, res) => {
  const refCode = String(req.params.refCode || '').trim();
  if (!refCode) return res.redirect('/');
  res.cookie('sbs_ref_code', refCode, {
    httpOnly: false,
    sameSite: 'lax',
    secure: COOKIE_SECURE,
    maxAge: 1000 * 60 * 60 * 24 * 30,
    path: '/'
  });
  return res.redirect(`/?ref=${encodeURIComponent(refCode)}`);
}));

app.post('/api/profile/avatar', requireSession, asyncHandler(async (req, res) => {
  const image = parseDataUrlImage(req.body?.data_url);
  if (!image) return res.status(400).json({ ok: false, error: 'INVALID_IMAGE' });
  const avatarDataUrl = `data:${image.mime};base64,${image.buffer.toString('base64')}`;
  await query(`update web_accounts set avatar_url = $2 where id = $1`, [req.sessionData.web_account_id, avatarDataUrl]);
  await writeAudit({ accountId: req.sessionData.web_account_id, tgId: req.sessionData.tg_id || null, eventType: 'profile.avatar.upload', req, meta: { avatar_url: 'data-url' } });
  return res.json({ ok: true, avatar_url: avatarDataUrl });
}));

app.delete('/api/profile/avatar', requireSession, asyncHandler(async (req, res) => {
  await query(`update web_accounts set avatar_url = null where id = $1`, [req.sessionData.web_account_id]);
  await writeAudit({ accountId: req.sessionData.web_account_id, tgId: req.sessionData.tg_id || null, eventType: 'profile.avatar.remove', req });
  return res.json({ ok: true });
}));

app.get('/healthz', async (_req, res) => {
  if (!dbConfigured) {
    return res.status(503).json({ ok: false, error: 'DB_NOT_CONFIGURED' });
  }
  try {
    await query('select 1');
    return res.json({ ok: true });
  } catch (error) {
    console.error('healthz failed', error);
    return res.status(503).json({ ok: false, error: 'DB_UNAVAILABLE' });
  }
});


app.get('/api/auth/session', asyncHandler(async (req, res) => {
  try {
    const session = await getSession(req);
    if (!session) return res.json({ ok: true, authenticated: false });

    const payload = await buildAccountPayload(session.web_account_id);
    return res.json({
      ok: true,
      authenticated: true,
      account: payload?.account || null,
      subscription: payload?.subscription || null,
      account: payload?.account || null
    });
  } catch (error) {
    console.error('session check failed', error);
    return res.status(500).json({ ok: false, error: 'SESSION_CHECK_FAILED' });
  }
}));

app.get('/api/auth/telegram/status', asyncHandler(async (req, res) => {
  const token = String(req.query.token || '');
  if (!token || !token.includes('.')) {
    return res.status(400).json({ ok: false, error: 'INVALID_TOKEN' });
  }

  const tokenHash = sha256(token);
  const { rows } = await query(
    `select id, selector, status, requested_from, expires_at, approved_at, consumed_at, tg_id, web_account_id, meta,
            verification_code_expires_at, verification_attempts, verification_max_attempts, verification_verified_at, verification_code_hash
     from telegram_login_tokens
     where token_hash = $1
     limit 1`,
    [tokenHash]
  );

  const loginToken = rows[0];
  if (!loginToken) {
    return res.status(404).json({ ok: false, error: 'TOKEN_NOT_FOUND' });
  }

  const expired = new Date(loginToken.expires_at) <= now();
  const linkType = loginToken.meta && typeof loginToken.meta === 'object' ? loginToken.meta.link_type || 'login' : 'login';

  const blocked = Number(loginToken.verification_attempts || 0) >= Number(loginToken.verification_max_attempts || TELEGRAM_CODE_MAX_ATTEMPTS);
  return res.json({
    ok: true,
    status: blocked
      ? 'blocked'
      : expired && loginToken.status === 'pending'
        ? 'expired'
        : loginToken.status,
    expired,
    approved: loginToken.status === 'approved' || Boolean(loginToken.approved_at),
    consumed: Boolean(loginToken.consumed_at),
    verification_required: Boolean(loginToken.verification_code_hash) && !loginToken.verification_verified_at,
    code_expires_at: loginToken.verification_code_expires_at,
    verification_attempts: Number(loginToken.verification_attempts || 0),
    verification_max_attempts: Number(loginToken.verification_max_attempts || TELEGRAM_CODE_MAX_ATTEMPTS),
    tg_id: loginToken.tg_id,
    web_account_id: loginToken.web_account_id,
    requested_from: loginToken.requested_from,
    link_type: linkType,
    expires_at: loginToken.expires_at,
    complete_url: `${APP_BASE_URL}/auth/telegram/complete?token=${encodeURIComponent(token)}`
  });
}));

app.post('/api/auth/register', asyncHandler(async (req, res) => {
  const email = String(req.body.email || '').trim().toLowerCase();
  const password = String(req.body.password || '');
  const displayName = String(req.body.display_name || '').trim() || null;

  if (!email || !/^\S+@\S+\.\S+$/.test(email)) {
    return res.status(400).json({ ok: false, error: 'INVALID_EMAIL' });
  }
  if (password.length < 8) {
    return res.status(400).json({ ok: false, error: 'WEAK_PASSWORD' });
  }

  const passwordHash = await bcrypt.hash(password, PASSWORD_SALT_ROUNDS);
  try {
    const pendingRefCode = String(req.cookies.sbs_ref_code || req.body.ref_code || '').trim() || null;
    const { rows } = await query(
      `insert into web_accounts (email, password_hash, display_name, referred_by_code)
       values ($1, $2, $3, $4)
       returning id`,
      [email, passwordHash, displayName, pendingRefCode]
    );
    const accountId = rows[0].id;
    const session = await createSession(accountId, req);
    setSessionCookie(res, session.rawToken, session.expiresAt);
    await writeAudit({ accountId, eventType: 'register.password', req, meta: { email } });
    res.json({ ok: true, linked_to_telegram: false });
  } catch (error) {
    if (error && (error.code === '23505' || String(error.message || '').includes('web_accounts_email_key'))) {
      return res.status(409).json({ ok: false, error: 'EMAIL_ALREADY_EXISTS' });
    }
    console.error(error);
    return res.status(500).json({ ok: false, error: 'REGISTER_FAILED' });
  }
}));

app.post('/api/auth/login', asyncHandler(async (req, res) => {
  const email = String(req.body.email || '').trim().toLowerCase();
  const password = String(req.body.password || '');
  const { rows } = await query(`select id, password_hash from web_accounts where email = $1 limit 1`, [email]);
  const account = rows[0];
  if (!account || !account.password_hash) {
    await writeAudit({ eventType: 'login.password', status: 'denied', req, meta: { email, reason: 'account_not_found' } });
    return res.status(401).json({ ok: false, error: 'INVALID_CREDENTIALS' });
  }
  const ok = await bcrypt.compare(password, account.password_hash);
  if (!ok) {
    await writeAudit({ accountId: account.id, eventType: 'login.password', status: 'denied', req, meta: { email, reason: 'bad_password' } });
    return res.status(401).json({ ok: false, error: 'INVALID_CREDENTIALS' });
  }
  const session = await createSession(account.id, req);
  setSessionCookie(res, session.rawToken, session.expiresAt);
  await writeAudit({ accountId: account.id, eventType: 'login.password', req, meta: { email } });
  res.json({ ok: true });
}));

app.post('/api/auth/logout', asyncHandler(async (req, res) => {
  const rawToken = req.cookies[SESSION_COOKIE_NAME];
  if (rawToken) {
    await query(`update web_sessions set revoked_at = now() where session_token_hash = $1`, [sha256(rawToken)]);
  }
  res.clearCookie(SESSION_COOKIE_NAME, { path: '/' });
  res.json({ ok: true });
}));

app.get('/api/me', requireSession, asyncHandler(async (req, res) => {
  const payload = await buildAccountPayload(req.sessionData.web_account_id);
  res.json({ ok: true, ...payload });
}));

app.get('/api/header-state', asyncHandler(async (req, res) => {
  const session = await getSession(req);
  if (!session) return res.json({ ok: true, authenticated: false });
  const payload = await buildAccountPayload(session.web_account_id);
  return res.json({ ok: true, authenticated: true, account: payload?.account || null });
}));

app.get('/cabinet', (_req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'cabinet.html'));
});

app.post('/api/auth/telegram/request', asyncHandler(async (req, res) => {
  const source = String(req.body.source || 'site_form').slice(0, 32);
  const linkType = String(req.body.link_type || 'login').slice(0, 32);
  const rawSelector = randomToken(9);
  const rawVerifier = randomToken(24);
  const publicToken = `${rawSelector}.${rawVerifier}`;
  const tokenHash = sha256(publicToken);
  const expiresAt = addMinutes(now(), TELEGRAM_LOGIN_TOKEN_TTL_MINUTES);

  const refCodeFromCookie = String(req.cookies.sbs_ref_code || req.query?.ref || '').trim();
  await query(
    `insert into telegram_login_tokens (selector, token_hash, status, requested_from, expires_at, meta)
     values ($1, $2, 'pending', $3, $4, $5::jsonb)`,
    [rawSelector, tokenHash, source, expiresAt, JSON.stringify({ link_type: linkType, ref_code: refCodeFromCookie || null })]
  );

  const deeplinkPayload = `web_login_${rawSelector}`;
  const botUrl = `https://t.me/${BOT_USERNAME}?start=${encodeURIComponent(deeplinkPayload)}`;
  const completeUrl = `${APP_BASE_URL}/auth/telegram/complete?token=${encodeURIComponent(publicToken)}`;

  await writeAudit({ eventType: 'telegram.login.request', req, meta: { selector: rawSelector, source, linkType } });

  res.json({
    ok: true,
    expires_in_seconds: TELEGRAM_LOGIN_TOKEN_TTL_MINUTES * 60,
    bot_url: botUrl,
    complete_url: completeUrl,
    login_token: publicToken,
    security: {
      protected_by: [
        'HTTPS/TLS при передаче',
        'SHA-256 хеш токена в БД',
        'одноразовость',
        'ограниченный TTL',
        'привязка к selector + verifier',
        'аудит событий'
      ],
      note: 'Сквозное шифрование здесь не применяется. Правильная формулировка: защищённый транспорт + одноразовые подписанные токены.'
    }
  });
}));


app.post('/api/auth/telegram/verify-code', asyncHandler(async (req, res) => {
  const token = String(req.body.token || '').trim();
  const selector = token.split('.')[0] || '';
  const code = String(req.body.code || '').replace(/\D+/g, '').slice(0, TELEGRAM_CODE_LENGTH);
  if (!token || !selector || !code || code.length !== TELEGRAM_CODE_LENGTH) {
    return res.status(400).json({ ok: false, error: 'INVALID_CODE' });
  }

  const tokenHash = sha256(token);
  const { rows } = await query(
    `select id, selector, status, expires_at, consumed_at, tg_id, web_account_id,
            verification_code_hash, verification_code_expires_at, verification_attempts, verification_max_attempts, verification_verified_at
     from telegram_login_tokens
     where token_hash = $1
     limit 1`,
    [tokenHash]
  );
  const loginToken = rows[0];
  if (!loginToken) {
    return res.status(404).json({ ok: false, error: 'TOKEN_NOT_FOUND' });
  }
  if (loginToken.consumed_at || new Date(loginToken.expires_at) <= now()) {
    return res.status(410).json({ ok: false, error: 'TOKEN_EXPIRED' });
  }
  if (loginToken.status !== 'approved' || !loginToken.verification_code_hash) {
    return res.status(409).json({ ok: false, error: 'CODE_REQUIRED' });
  }
  if (loginToken.verification_verified_at) {
    return res.status(409).json({ ok: false, error: 'TOKEN_ALREADY_USED' });
  }
  if (!loginToken.verification_code_expires_at || new Date(loginToken.verification_code_expires_at) <= now()) {
    await query(`update telegram_login_tokens set status = 'expired' where id = $1`, [loginToken.id]);
    return res.status(410).json({ ok: false, error: 'CODE_EXPIRED' });
  }
  const attempts = Number(loginToken.verification_attempts || 0);
  const maxAttempts = Number(loginToken.verification_max_attempts || TELEGRAM_CODE_MAX_ATTEMPTS);
  if (attempts >= maxAttempts) {
    return res.status(429).json({ ok: false, error: 'CODE_ATTEMPTS_EXCEEDED' });
  }

  const incomingHash = hashTelegramVerificationCode(selector, code);
  if (incomingHash !== loginToken.verification_code_hash) {
    const nextAttempts = attempts + 1;
    await query(
      `update telegram_login_tokens
       set verification_attempts = $2,
           status = case when $2 >= verification_max_attempts then 'blocked' else status end
       where id = $1`,
      [loginToken.id, nextAttempts]
    );
    await writeAudit({ accountId: loginToken.web_account_id, tgId: loginToken.tg_id, eventType: 'telegram.login.verify_code', status: 'denied', req, meta: { token_id: loginToken.id, attempts: nextAttempts } });
    return res.status(nextAttempts >= maxAttempts ? 429 : 401).json({ ok: false, error: nextAttempts >= maxAttempts ? 'CODE_ATTEMPTS_EXCEEDED' : 'INVALID_CODE' });
  }

  await query(
    `update telegram_login_tokens
     set verification_verified_at = now(), verification_attempts = verification_attempts + 1
     where id = $1`,
    [loginToken.id]
  );
  await writeAudit({ accountId: loginToken.web_account_id, tgId: loginToken.tg_id, eventType: 'telegram.login.verify_code', req, meta: { token_id: loginToken.id } });
  await consumeTelegramLoginToken({ token, req, res });
  return res.json({ ok: true, redirect_to: '/cabinet' });
}));

app.get('/auth/telegram/complete', asyncHandler(async (req, res) => {
  const token = String(req.query.token || '');
  if (!token || !token.includes('.')) {
    return res.status(400).send('Invalid token');
  }
  try {
    await consumeTelegramLoginToken({ token, req, res });
    return res.redirect('/cabinet');
  } catch (error) {
    if (error.code === 'CODE_REQUIRED') {
      return res.status(409).send('Confirmation code required. Return to the site and enter the code from Telegram.');
    }
    if (error.code === 'TOKEN_NOT_FOUND') {
      return res.status(404).send('Token not found');
    }
    return res.status(410).send('Token expired or already used');
  }
}));

app.post('/internal/telegram/approve', asyncHandler(async (req, res) => {
  const apiKey = String(req.headers['x-internal-api-key'] || '');
  if (apiKey !== WEB_INTERNAL_API_KEY) {
    return res.status(401).json({ ok: false, error: 'UNAUTHORIZED' });
  }
  const selector = String(req.body.selector || '').trim();
  const tgId = Number(req.body.tg_id || 0);
  if (!selector || !Number.isFinite(tgId) || tgId <= 0) {
    return res.status(400).json({ ok: false, error: 'INVALID_INPUT' });
  }
  const verificationCode = randomDigits(TELEGRAM_CODE_LENGTH);
  const verificationHash = hashTelegramVerificationCode(selector, verificationCode);
  const verificationExpiresAt = addMinutes(now(), TELEGRAM_CODE_TTL_MINUTES);
  const { rows } = await query(
    `update telegram_login_tokens
     set tg_id = $2,
         status = 'approved',
         approved_at = now(),
         approved_hmac = $3,
         verification_code_hash = $4,
         verification_code_expires_at = $5,
         verification_attempts = 0,
         verification_max_attempts = $6,
         verification_verified_at = null
     where selector = $1 and status = 'pending' and expires_at > now()
     returning id, token_hash, meta`,
    [selector, tgId, hmac(`${selector}:${tgId}`), verificationHash, verificationExpiresAt, TELEGRAM_CODE_MAX_ATTEMPTS]
  );
  if (!rows[0]) {
    return res.status(404).json({ ok: false, error: 'TOKEN_NOT_FOUND_OR_EXPIRED' });
  }

  const pendingRefCode = rows[0]?.meta && typeof rows[0].meta === 'object' ? rows[0].meta.ref_code : null;
  if (pendingRefCode) {
    await attachPendingReferrerByCode(tgId, pendingRefCode);
  }
  await writeAudit({ tgId, eventType: 'telegram.login.approve', meta: { selector, token_id: rows[0].id, ref_code: pendingRefCode || null } });
  res.json({
    ok: true,
    selector,
    tg_id: tgId,
    verification_code: verificationCode,
    verification_expires_at: verificationExpiresAt,
    verification_ttl_seconds: TELEGRAM_CODE_TTL_MINUTES * 60,
    next_step: 'send_code_to_user'
  });
}));

app.post('/internal/telegram/link', asyncHandler(async (req, res) => {
  const apiKey = String(req.headers['x-internal-api-key'] || '');
  if (apiKey !== WEB_INTERNAL_API_KEY) {
    return res.status(401).json({ ok: false, error: 'UNAUTHORIZED' });
  }
  const tgId = Number(req.body.tg_id || 0);
  const accountId = Number(req.body.web_account_id || 0);
  const context = String(req.body.context || 'bot_to_site').slice(0, 64);
  if (!tgId || !accountId) {
    return res.status(400).json({ ok: false, error: 'INVALID_INPUT' });
  }

  const rawToken = randomToken(32);
  const tokenHash = sha256(rawToken);
  const expiresAt = addMinutes(now(), TELEGRAM_LINK_TTL_MINUTES);

  await query(
    `insert into telegram_web_links (web_account_id, tg_id, link_token_hash, status, context, expires_at)
     values ($1, $2, $3, 'pending', $4, $5)`,
    [accountId, tgId, tokenHash, context, expiresAt]
  );

  await writeAudit({ accountId, tgId, eventType: 'telegram.web_link.created', meta: { context } });
  res.json({
    ok: true,
    auth_url: `${APP_BASE_URL}/auth/telegram/link?token=${encodeURIComponent(rawToken)}`,
    expires_in_seconds: TELEGRAM_LINK_TTL_MINUTES * 60
  });
}));

app.get('/auth/telegram/link', asyncHandler(async (req, res) => {
  const token = String(req.query.token || '');
  if (!token) return res.status(400).send('Invalid token');
  const tokenHash = sha256(token);
  const { rows } = await query(
    `select id, web_account_id, tg_id, expires_at, status, consumed_at
     from telegram_web_links
     where link_token_hash = $1
     limit 1`,
    [tokenHash]
  );
  const link = rows[0];
  if (!link) return res.status(404).send('Link not found');
  if (link.status !== 'pending' || link.consumed_at || new Date(link.expires_at) <= now()) {
    return res.status(410).send('Link expired or already used');
  }

  await query(`update telegram_web_links set status = 'used', consumed_at = now() where id = $1`, [link.id]);
  const session = await createSession(link.web_account_id, req);
  setSessionCookie(res, session.rawToken, session.expiresAt);
  await writeAudit({ accountId: link.web_account_id, tgId: link.tg_id, eventType: 'telegram.web_link.consume', req });
  res.redirect('/cabinet');
}));


app.post('/api/payments/checkout', requireSession, asyncHandler(async (req, res) => {
  const payload = await buildAccountPayload(req.sessionData.web_account_id);
  if (!payload) return res.status(404).json({ ok: false, error: 'ACCOUNT_NOT_FOUND' });

  const tgId = Number(payload.account?.tg_id || 0) || null;
  const product = String(req.body?.product || 'subscription').trim();
  const seatsRequested = Math.max(1, Math.min(10, Number(req.body?.seats || 1)));
  const slotNo = Number(req.body?.slot_no || 0) || null;
  const familySeatPriceRub = await getFamilySeatPriceRub();
  const subscriptionPriceRub = await getPriceRub();
  const isEmailOnly = !tgId;

  if (isEmailOnly && product !== 'subscription') {
    return res.status(403).json({ ok: false, error: 'SUBSCRIPTION_REQUIRED' });
  }

  let amountRub = subscriptionPriceRub;
  let periodMonths = 1;
  let periodDays = 30;
  let provider = 'platega';
  let eventType = 'payment.checkout.created';
  let description = `Подписка SBS CONNECT (${periodMonths} мес, ${tgId ? `TG ${tgId}` : `WEB ${req.sessionData.web_account_id}`})`;
  let payloadSource = 'web_subscription';

  if (product === 'family_buy' || product === 'family_seat') {
    const seats = seatsRequested;
    amountRub = familySeatPriceRub * seats;
    provider = `platega_family_${seats}`;
    eventType = 'family.checkout.created';
    description = `Семейная группа VPN: покупка ${seats} мест (${tgId ? `TG ${tgId}` : `WEB ${req.sessionData.web_account_id}`})`;
    payloadSource = `web_family_buy:${seats}`;
    await upsertAppSetting(`family_pay_mode:${tgId}`, '1');
    await upsertAppSetting(`family_pay_count:${tgId}`, String(seats));
    await upsertAppSetting(`family_pay_slot:${tgId}`, '');
  } else if (product === 'family_renew_one') {
    amountRub = familySeatPriceRub;
    provider = 'platega_family_1';
    eventType = 'family.renew_one.checkout.created';
    description = `Семейная группа VPN: продление ближайшего места (${tgId ? `TG ${tgId}` : `WEB ${req.sessionData.web_account_id}`})`;
    payloadSource = 'web_family_renew_one';
    await upsertAppSetting(`family_pay_mode:${tgId}`, '2');
    await upsertAppSetting(`family_pay_count:${tgId}`, '1');
    await upsertAppSetting(`family_pay_slot:${tgId}`, '');
  } else if (product === 'family_renew_all') {
    const seats = Math.max(1, Number(payload.family?.group?.seats_total || 1));
    amountRub = familySeatPriceRub * seats;
    provider = `platega_family_${seats}`;
    eventType = 'family.renew_all.checkout.created';
    description = `Семейная группа VPN: продление всех мест (${seats}) ${tgId ? `TG ${tgId}` : `WEB ${req.sessionData.web_account_id}`}`;
    payloadSource = `web_family_renew_all:${seats}`;
    await upsertAppSetting(`family_pay_mode:${tgId}`, '3');
    await upsertAppSetting(`family_pay_count:${tgId}`, String(seats));
    await upsertAppSetting(`family_pay_slot:${tgId}`, '');
  } else if (product === 'family_renew_slot') {
    if (!slotNo) return res.status(400).json({ ok: false, error: 'FAMILY_SLOT_REQUIRED' });
    amountRub = familySeatPriceRub;
    provider = 'platega_family_1';
    eventType = 'family.renew_slot.checkout.created';
    description = `Семейная группа VPN: продление места #${slotNo} (${tgId ? `TG ${tgId}` : `WEB ${req.sessionData.web_account_id}`})`;
    payloadSource = `web_family_renew_slot:${slotNo}`;
    await upsertAppSetting(`family_pay_mode:${tgId}`, '4');
    await upsertAppSetting(`family_pay_count:${tgId}`, '1');
    await upsertAppSetting(`family_pay_slot:${tgId}`, String(slotNo));
  }

  const checkout = await createPlategaCheckout({
    subject: tgId ? `tg_id=${tgId}` : `wa_id=${req.sessionData.web_account_id}`,
    tgId,
    amountRub,
    periodMonths,
    periodDays,
    description,
    payloadSource
  });

  let paymentId = null;
  if (tgId) {
    const inserted = await query(
      `insert into payments (tg_id, amount, currency, provider, status, period_days, period_months, provider_payment_id)
       values ($1, $2, 'RUB', $3, 'pending', $4, $5, $6)
       returning id`,
      [tgId, amountRub, provider, periodDays, periodMonths, checkout.transactionId]
    );
    paymentId = inserted.rows[0]?.id || null;
  } else {
    const inserted = await query(
      `insert into web_checkout_orders (web_account_id, product, provider, amount, currency, status, provider_payment_id, payment_url, meta)
       values ($1, $2, $3, $4, 'RUB', 'pending', $5, $6, $7)
       returning id`,
      [req.sessionData.web_account_id, product, provider, amountRub, checkout.transactionId, checkout.paymentUrl, JSON.stringify({ period_months: periodMonths, period_days: periodDays, slot_no: slotNo, seats: seatsRequested })]
    );
    paymentId = inserted.rows[0]?.id || null;
  }

  await writeAudit({
    accountId: req.sessionData.web_account_id,
    tgId,
    eventType,
    req,
    meta: { payment_id: paymentId, transaction_id: checkout.transactionId, amount: amountRub, product, slot_no: slotNo, seats: seatsRequested, provider, account_type: tgId ? 'telegram' : 'email' }
  });

  res.json({
    ok: true,
    payment_url: checkout.paymentUrl,
    transaction_id: checkout.transactionId,
    amount: amountRub,
    product,
    payment_id: paymentId,
    activation_mode: tgId ? 'bot_unified' : 'web_pending'
  });
}));

app.get('/api/vpn/download-config', requireSession, asyncHandler(async (req, res) => {
  const payload = await buildAccountPayload(req.sessionData.web_account_id);
  if (!payload) return res.status(404).json({ ok: false, error: 'ACCOUNT_NOT_FOUND' });
  if (!payload.account?.tg_id) return res.status(403).json({ ok: false, error: 'VPN_NOT_AVAILABLE' });
  if (!payload.subscription?.is_active) return res.status(402).json({ ok: false, error: 'SUBSCRIPTION_REQUIRED' });

  const { rows } = await query(
    `select id, client_private_key_enc, client_ip, server_code
     from vpn_peers
     where tg_id = $1 and is_active = true and revoked_at is null
     order by id desc
     limit 1`,
    [Number(payload.account.tg_id)]
  );
  const peer = rows[0];
  if (!peer) return res.status(404).json({ ok: false, error: 'VPN_CONFIG_NOT_FOUND' });

  const privateKey = decryptVpnPrivateKey(peer.client_private_key_enc);
  const confText = buildWireGuardConfig({ privateKey, clientIp: peer.client_ip, serverCode: peer.server_code });
  const safeName = `sbsVPN${peer.id || Number(payload.account.tg_id)}.conf`;

  await writeAudit({
    accountId: req.sessionData.web_account_id,
    tgId: Number(payload.account.tg_id),
    eventType: 'vpn.config.download',
    req,
    meta: { peer_id: peer.id, server_code: peer.server_code || null }
  });

  res.setHeader('Content-Type', 'text/plain; charset=utf-8');
  res.setHeader('Content-Disposition', `attachment; filename="${safeName}"`);
  res.send(confText);
}));



app.post('/api/vpn/reset-config', requireSession, asyncHandler(async (req, res) => {
  const payload = await buildAccountPayload(req.sessionData.web_account_id);
  if (!payload) return res.status(404).json({ ok: false, error: 'ACCOUNT_NOT_FOUND' });
  if (!payload.account?.tg_id) return res.status(403).json({ ok: false, error: 'VPN_NOT_AVAILABLE' });
  if (!payload.subscription?.is_active) return res.status(402).json({ ok: false, error: 'SUBSCRIPTION_REQUIRED' });
  if (!VPN_BACKEND_BASE_URL || !VPN_BACKEND_SHARED_SECRET) {
    return res.status(503).json({ ok: false, error: 'VPN_RESET_NOT_CONFIGURED' });
  }

  const response = await fetch(`${VPN_BACKEND_BASE_URL}/internal/web/vpn/reset`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Internal-Secret': VPN_BACKEND_SHARED_SECRET
    },
    body: JSON.stringify({
      tg_id: Number(payload.account.tg_id),
      web_account_id: req.sessionData.web_account_id,
      source: 'web_cabinet'
    })
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    return res.status(response.status || 502).json({ ok: false, error: data.error || 'VPN_RESET_FAILED' });
  }

  await writeAudit({
    accountId: req.sessionData.web_account_id,
    tgId: Number(payload.account.tg_id),
    eventType: 'vpn.config.reset',
    req,
    meta: { backend: 'vpn_reset_proxy' }
  });

  return res.json({ ok: true, ...data });
}));


app.get('/api/family/slot/:slotNo/download', requireSession, asyncHandler(async (req, res) => {
  const payload = await buildAccountPayload(req.sessionData.web_account_id);
  if (!payload) return res.status(404).json({ ok: false, error: 'ACCOUNT_NOT_FOUND' });
  if (!payload.account?.tg_id) return res.status(403).json({ ok: false, error: 'FAMILY_NOT_AVAILABLE' });
  if (!payload.subscription?.is_active) return res.status(402).json({ ok: false, error: 'SUBSCRIPTION_REQUIRED' });

  const slotNo = Math.max(1, Number(req.params.slotNo || 0));
  if (!slotNo) return res.status(400).json({ ok: false, error: 'FAMILY_SLOT_REQUIRED' });

  const { rows } = await query(
    `select fp.id, fp.slot_no, fp.label, fp.expires_at, fp.is_paused,
            vp.id as peer_id, vp.client_private_key_enc, vp.client_ip, vp.server_code, vp.is_active, vp.revoked_at
       from family_vpn_profiles fp
       left join vpn_peers vp on vp.id = fp.vpn_peer_id
      where fp.owner_tg_id = $1 and fp.slot_no = $2
      limit 1`,
    [Number(payload.account.tg_id), slotNo]
  );
  const slot = rows[0];
  if (!slot) return res.status(404).json({ ok: false, error: 'FAMILY_SLOT_NOT_FOUND' });
  if (!slot.peer_id || !slot.client_private_key_enc || !slot.client_ip || !slot.server_code || !slot.is_active || slot.revoked_at) {
    return res.status(409).json({ ok: false, error: 'FAMILY_SLOT_CONFIG_NOT_READY' });
  }
  const privateKey = decryptVpnPrivateKey(slot.client_private_key_enc);
  const confText = buildWireGuardConfig({ privateKey, clientIp: slot.client_ip, serverCode: slot.server_code });
  const safeName = `sbsVPN-family-${slotNo}.conf`;

  await writeAudit({
    accountId: req.sessionData.web_account_id,
    tgId: Number(payload.account.tg_id),
    eventType: 'family.slot.download',
    req,
    meta: { slot_no: slotNo, peer_id: slot.peer_id, server_code: slot.server_code || null }
  });

  res.setHeader('Content-Type', 'text/plain; charset=utf-8');
  res.setHeader('Content-Disposition', `attachment; filename="${safeName}"`);
  return res.send(confText);
}));

app.post('/api/family/slot/:slotNo/reset', requireSession, asyncHandler(async (req, res) => {
  const payload = await buildAccountPayload(req.sessionData.web_account_id);
  if (!payload) return res.status(404).json({ ok: false, error: 'ACCOUNT_NOT_FOUND' });
  if (!payload.account?.tg_id) return res.status(403).json({ ok: false, error: 'FAMILY_NOT_AVAILABLE' });
  if (!payload.subscription?.is_active) return res.status(402).json({ ok: false, error: 'SUBSCRIPTION_REQUIRED' });

  const slotNo = Math.max(1, Number(req.params.slotNo || 0));
  if (!slotNo) return res.status(400).json({ ok: false, error: 'FAMILY_SLOT_REQUIRED' });

  if (!VPN_BACKEND_BASE_URL || !VPN_BACKEND_SHARED_SECRET) {
    return res.status(503).json({ ok: false, error: 'FAMILY_RESET_NOT_CONFIGURED' });
  }

  const response = await fetch(`${VPN_BACKEND_BASE_URL}/internal/web/family/reset`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Internal-Secret': VPN_BACKEND_SHARED_SECRET
    },
    body: JSON.stringify({
      tg_id: Number(payload.account.tg_id),
      slot_no: slotNo,
      web_account_id: req.sessionData.web_account_id,
      source: 'web_cabinet'
    })
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    return res.status(response.status || 502).json({ ok: false, error: data.error || 'FAMILY_RESET_FAILED' });
  }

  await writeAudit({
    accountId: req.sessionData.web_account_id,
    tgId: Number(payload.account.tg_id),
    eventType: 'family.slot.reset',
    req,
    meta: { slot_no: slotNo, backend: 'family_reset_proxy' }
  });

  return res.json({ ok: true, ...data });
}));

app.use((error, _req, res, _next) => {
  if (error && error.code === 'DB_NOT_CONFIGURED') {
    return res.status(503).json({ ok: false, error: 'DB_NOT_CONFIGURED' });
  }
  console.error('Unhandled request error', error);
  return res.status(500).json({ ok: false, error: 'INTERNAL_SERVER_ERROR' });
});

async function bootstrap() {
  if (!dbConfigured) {
    console.warn('Database is not configured. Set DATABASE_URL or PG* variables in Railway.');
  } else {
    await runMigrations();
  }

  app.listen(PORT, () => {
    console.log(`SBS web service listening on :${PORT}`);
  });
}

bootstrap().catch((error) => {
  console.error('Bootstrap failed', error);
  process.exit(1);
});
