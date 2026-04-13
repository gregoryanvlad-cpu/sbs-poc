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
const PASSWORD_SALT_ROUNDS = Number(process.env.PASSWORD_SALT_ROUNDS || 12);
const APP_BASE_URL = (process.env.APP_BASE_URL || '').replace(/\/$/, '');
const BOT_USERNAME = (process.env.BOT_USERNAME || 'sbsmanager_bot').replace(/^@/, '');
const TELEGRAM_BOT_LOGIN_SECRET = process.env.TELEGRAM_BOT_LOGIN_SECRET || 'change-me-before-prod';
const WEB_INTERNAL_API_KEY = process.env.WEB_INTERNAL_API_KEY || 'change-me-before-prod';
const COOKIE_SECURE = String(process.env.COOKIE_SECURE || 'true') === 'true';

const dbConfigured = Boolean(DATABASE_URL);

const pool = dbConfigured
  ? new Pool({
      connectionString: DATABASE_URL,
      ssl: process.env.PGSSL === 'disable' ? false : { rejectUnauthorized: false }
    })
  : null;

app.use(express.json());
app.use(express.urlencoded({ extended: false }));
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


async function buildAccountPayload(webAccountId) {
  const { rows } = await query(
    `select a.id, a.email, a.display_name, a.tg_id, a.email_verified_at, a.created_at,
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

  const displayName = account.display_name
    || [account.first_name, account.last_name].filter(Boolean).join(' ')
    || (account.tg_username ? '@' + account.tg_username : null)
    || account.email;

  const isTelegramUser = Boolean(account.tg_id);
  const hasActiveSubscription = Boolean(account.subscription_active);
  const refLink = account.ref_code && BOT_USERNAME
    ? `https://t.me/${BOT_USERNAME}?start=ref_${account.ref_code}`
    : null;
  const botUrl = `https://t.me/${BOT_USERNAME}`;

  const payments = account.tg_id ? (await query(
    `select id, amount, currency, provider, status, paid_at, period_days, period_months
     from payments
     where tg_id = $1
     order by paid_at desc, id desc
     limit 20`,
    [account.tg_id]
  )).rows : [];

  const referrals = account.tg_id ? (await query(
    `select r.id, r.status, r.activated_at, u.first_name, u.last_name, u.tg_username, r.referred_tg_id
     from referrals r
     left join users u on u.tg_id = r.referred_tg_id
     where r.referrer_tg_id = $1
     order by r.created_at desc
     limit 50`,
    [account.tg_id]
  )).rows : [];

  const referralEarnings = account.tg_id ? (await query(
    `select id, earned_rub, status, available_at, paid_at, created_at, percent
     from referral_earnings
     where referrer_tg_id = $1
     order by created_at desc
     limit 50`,
    [account.tg_id]
  )).rows : [];

  const referralActiveCount = account.tg_id ? Number((await query(
    `select count(*)::int as count
     from referrals
     where referrer_tg_id = $1 and status = 'active'`,
    [account.tg_id]
  )).rows[0]?.count || 0) : 0;

  const referralBalances = account.tg_id ? (await query(
    `select
       coalesce(sum(case when status = 'available' then earned_rub else 0 end), 0)::int as available_rub,
       coalesce(sum(case when status = 'pending' then earned_rub else 0 end), 0)::int as pending_rub,
       coalesce(sum(case when status = 'paid' then earned_rub else 0 end), 0)::int as paid_rub,
       coalesce(sum(earned_rub), 0)::int as total_rub
     from referral_earnings
     where referrer_tg_id = $1`,
    [account.tg_id]
  )).rows[0] || null : null;

  const referralOverride = account.tg_id ? (await query(
    `select int_value
     from app_settings
     where key = $1
     limit 1`,
    [`referral_percent_override:${account.tg_id}`]
  )).rows[0] || null : null;

  const levelPercent = (activeReferrals) => {
    if (activeReferrals >= 10) return 17;
    if (activeReferrals >= 4) return 11;
    return 5;
  };

  const nextLevelInfo = (activeReferrals) => {
    if (activeReferrals < 4) return { next_percent: 11, next_target_active: 4 };
    if (activeReferrals < 10) return { next_percent: 17, next_target_active: 10 };
    return { next_percent: null, next_target_active: null };
  };

  const referralOverrideValue = referralOverride && referralOverride.int_value !== null
    ? Math.max(0, Math.min(100, Number(referralOverride.int_value)))
    : null;
  const referralCurrentPercent = referralOverrideValue !== null
    ? referralOverrideValue
    : levelPercent(referralActiveCount);
  const referralNext = referralOverrideValue !== null
    ? { next_percent: null, next_target_active: null }
    : nextLevelInfo(referralActiveCount);

  const yandexMembership = account.tg_id ? (await query(
    `select ym.id, ym.status, ym.invite_link, ym.invite_issued_at, ym.invite_expires_at,
            ym.coverage_end_at, ym.account_label, ym.slot_index, ym.yandex_login,
            ym.yandex_account_id, ym.removed_at,
            ya.label as yandex_account_label, ya.max_slots, ya.used_slots,
            ya.plus_end_at, ya.status as yandex_account_status
     from yandex_memberships ym
     left join yandex_accounts ya on ya.id = ym.yandex_account_id
     where ym.tg_id = $1
     order by ym.id desc
     limit 1`,
    [account.tg_id]
  )).rows[0] || null : null;

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
    yandexSeatsUsed = Number(counted.rows[0]?.count || 0);
  }

  const vpnPeer = account.tg_id ? (await query(
    `select id, client_public_key, server_code, created_at
     from vpn_peers
     where tg_id = $1 and is_active = true and revoked_at is null
     order by created_at desc, id desc
     limit 1`,
    [account.tg_id]
  )).rows[0] || null : null;

  const nowDate = now();
  const yandexCoverageValid = yandexMembership?.coverage_end_at
    ? new Date(yandexMembership.coverage_end_at) > nowDate
    : false;
  const yandexInvitePresent = Boolean(String(yandexMembership?.invite_link || '').trim());
  const yandexIsCurrent = Boolean(
    yandexMembership
    && !yandexMembership.removed_at
    && yandexInvitePresent
    && (yandexCoverageValid || !yandexMembership.coverage_end_at)
  );

  const services = [];
  if (hasActiveSubscription) {
    services.push('Безопасный VPN');
    services.push('Обход блокировок');
    services.push('Антилаг Telegram');
    services.push('VPN LTE');
  }
  if (yandexIsCurrent) {
    services.push('Yandex Plus');
  }

  const familyLink = yandexInvitePresent ? String(yandexMembership.invite_link).trim() : null;

  const actions = [
    { key: 'open_bot', label: 'Открыть бота', type: 'link', href: botUrl },
    { key: 'pay', label: hasActiveSubscription ? 'Продлить подписку' : 'Подключить подписку', type: 'link', href: botUrl },
    { key: 'config', label: hasActiveSubscription ? 'Получить конфиг' : 'Конфиг откроется после оплаты', type: hasActiveSubscription ? 'link' : 'disabled', href: hasActiveSubscription ? botUrl : null },
    { key: 'family', label: yandexIsCurrent ? 'Открыть приглашение в Яндекс Семью' : 'Яндекс Семья', type: familyLink ? 'link' : 'disabled', href: familyLink || null },
    { key: 'ref', label: refLink ? 'Скопировать реферальную ссылку' : 'Реферальная ссылка появится после первой активации', type: refLink ? 'copy' : 'disabled', value: refLink }
  ];

  const progressTotalDays = account.start_at && account.end_at
    ? Math.max(1, Math.ceil((new Date(account.end_at).getTime() - new Date(account.start_at).getTime()) / 86400000))
    : 0;
  const progressElapsedDays = account.start_at
    ? Math.max(0, Math.min(progressTotalDays || 0, Math.ceil((nowDate.getTime() - new Date(account.start_at).getTime()) / 86400000)))
    : 0;
  const progressRemainingDays = account.end_at
    ? Math.max(0, Math.ceil((new Date(account.end_at).getTime() - nowDate.getTime()) / 86400000))
    : 0;

  return {
    account: {
      id: account.id,
      email: account.email,
      email_masked: maskEmail(account.email),
      display_name: displayName,
      tg_id: account.tg_id,
      tg_username: account.tg_username,
      first_name: account.first_name,
      last_name: account.last_name,
      email_verified_at: account.email_verified_at,
      created_at: account.created_at,
      user_type: isTelegramUser ? 'telegram' : 'email',
      ref_code: account.ref_code,
      referral_link: refLink,
      bot_url: botUrl
    },
    subscription: {
      is_active: hasActiveSubscription,
      status: account.subscription_status || 'inactive',
      start_at: account.start_at,
      end_at: account.end_at,
      progress_total_days: progressTotalDays,
      progress_elapsed_days: progressElapsedDays,
      progress_remaining_days: progressRemainingDays,
      can_get_config: hasActiveSubscription,
      config_locked_reason: hasActiveSubscription
        ? null
        : 'Оплатите или продлите подписку, чтобы активировать VPN-конфиг и остальные сервисы.',
      config_key: vpnPeer?.client_public_key || null,
      config_url: hasActiveSubscription ? botUrl : null,
      vpn_peer_id: vpnPeer?.id || null,
      vpn_server_code: vpnPeer?.server_code || null
    },
    services,
    payments,
    referrals,
    referral_earnings: referralEarnings,
    referral_summary: {
      active_count: referralActiveCount,
      current_percent: referralCurrentPercent,
      has_override: referralOverrideValue !== null,
      next_percent: referralNext.next_percent,
      next_target_active: referralNext.next_target_active,
      referrals_left_to_next: referralNext.next_target_active !== null
        ? Math.max(0, referralNext.next_target_active - referralActiveCount)
        : null,
      available_rub: Number(referralBalances?.available_rub || 0),
      pending_rub: Number(referralBalances?.pending_rub || 0),
      paid_rub: Number(referralBalances?.paid_rub || 0),
      total_earned_rub: Number(referralBalances?.total_rub || 0)
    },
    family: {
      group: yandexMembership ? {
        source: 'yandex_memberships',
        id: yandexMembership.id,
        status: yandexMembership.status,
        account_label: yandexMembership.account_label || yandexMembership.yandex_account_label || null,
        slot_index: yandexMembership.slot_index,
        yandex_login: yandexMembership.yandex_login,
        invite_link: familyLink,
        invite_issued_at: yandexMembership.invite_issued_at,
        invite_expires_at: yandexMembership.invite_expires_at,
        coverage_end_at: yandexMembership.coverage_end_at,
        removed_at: yandexMembership.removed_at,
        seats_total: Number(yandexMembership.max_slots || 0) || 5,
        seats_used: Math.max(0, yandexSeatsUsed || Number(yandexMembership.used_slots || 0) || (familyLink ? 1 : 0)),
        plus_end_at: yandexMembership.plus_end_at,
        account_status: yandexMembership.yandex_account_status,
        is_current: yandexIsCurrent
      } : null,
      profiles: []
    },
    actions,
    security: {
      session_transport: 'HTTPS/TLS',
      session_storage: 'sha256(session_token)',
      telegram_links: 'одноразовые токены + HMAC подпись + TTL + single-use',
      encryption_note: 'Это не сквозное шифрование. Корректно: транспорт защищён TLS, а сами токены не хранятся в открытом виде.'
    }
  };
}

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
    `select id, selector, status, requested_from, expires_at, approved_at, consumed_at, tg_id, web_account_id, meta
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

  return res.json({
    ok: true,
    status: expired && loginToken.status === 'pending' ? 'expired' : (loginToken.status === 'used' && !loginToken.consumed_at ? 'approved' : loginToken.status),
    expired,
    approved: loginToken.status === 'approved' || Boolean(loginToken.approved_at),
    consumed: Boolean(loginToken.consumed_at),
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
    const { rows } = await query(
      `insert into web_accounts (email, password_hash, display_name)
       values ($1, $2, $3)
       returning id`,
      [email, passwordHash, displayName]
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

  await query(
    `insert into telegram_login_tokens (selector, token_hash, status, requested_from, expires_at, meta)
     values ($1, $2, 'pending', $3, $4, $5::jsonb)`,
    [rawSelector, tokenHash, source, expiresAt, JSON.stringify({ link_type: linkType })]
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

app.get('/auth/telegram/complete', asyncHandler(async (req, res) => {
  const token = String(req.query.token || '');
  if (!token || !token.includes('.')) {
    return res.status(400).send('Invalid token');
  }
  const tokenHash = sha256(token);
  const { rows } = await query(
    `select id, web_account_id, tg_id, status, expires_at, consumed_at
     from telegram_login_tokens
     where token_hash = $1
     limit 1`,
    [tokenHash]
  );
  const loginToken = rows[0];
  if (!loginToken) return res.status(404).send('Token not found');
  if (loginToken.status !== 'approved' || loginToken.consumed_at || new Date(loginToken.expires_at) <= now()) {
    return res.status(410).send('Token expired or already used');
  }

  let webAccountId = loginToken.web_account_id;
  if (!webAccountId) {
    const existing = await query(`select id from web_accounts where tg_id = $1 limit 1`, [loginToken.tg_id]);
    if (existing.rows[0]) {
      webAccountId = existing.rows[0].id;
    } else {
      const profile = await query(`select tg_username, first_name, last_name from users where tg_id = $1 limit 1`, [loginToken.tg_id]);
      const user = profile.rows[0] || {};
      const fallbackEmail = `tg-${loginToken.tg_id}@telegram.local`;
      const displayName = [user.first_name, user.last_name].filter(Boolean).join(' ') || user.tg_username || `Telegram ${loginToken.tg_id}`;
      const created = await query(
        `insert into web_accounts (email, display_name, tg_id, auth_source, email_verified_at)
         values ($1, $2, $3, 'telegram', now())
         on conflict (tg_id) do update set display_name = excluded.display_name
         returning id`,
        [fallbackEmail, displayName, loginToken.tg_id]
      );
      webAccountId = created.rows[0].id;
    }
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
  res.redirect('/cabinet');
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
  const { rows } = await query(
    `update telegram_login_tokens
     set tg_id = $2, status = 'approved', approved_at = now(), approved_hmac = $3
     where selector = $1 and status = 'pending' and expires_at > now()
     returning id, token_hash`,
    [selector, tgId, hmac(`${selector}:${tgId}`)]
  );
  if (!rows[0]) {
    return res.status(404).json({ ok: false, error: 'TOKEN_NOT_FOUND_OR_EXPIRED' });
  }

  const publicLinkTokenRow = await query(`select token_hash from telegram_login_tokens where id = $1`, [rows[0].id]);
  await writeAudit({ tgId, eventType: 'telegram.login.approve', meta: { selector, token_id: rows[0].id } });
  res.json({ ok: true, selector, tg_id: tgId });
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
