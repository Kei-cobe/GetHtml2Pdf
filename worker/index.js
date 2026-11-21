// pdf-scheduler — 外部定时器（Cloudflare Worker）
// 职责：
//   1. 每分钟 tick（cron: * * * * * UTC），命中 KV 中配置的北京时间点则触发 GitHub workflow_dispatch
//   2. 提供 POST /trigger 供前端手动下发
//   3. 用 KV 做「5 分钟内相同任务跨终端去重」，避免重复触发
//   4. 提供 GET/PUT /admin/schedule 供前端读取/修改定时时间（存 KV）
// 环境变量（secret）：
//   GITHUB_TOKEN   — 调用 workflow_dispatch 的 GitHub PAT（repo + workflow）
//   TRIGGER_SECRET — 前端调用 /trigger 与 /admin/schedule 时需带的 x-trigger-key
// KV binding：DEDUP（namespace: pdf-scheduler-dedup）
//   - dedup:<fingerprint>  去重记录，TTL 300s
//   - cfg:schedule         定时时间列表（北京时间 HH:MM 数组），如 ["18:30","20:30"]

const GITHUB_OWNER = 'Kei-cobe';
const GITHUB_REPO = 'GetHtml2Pdf';
const GITHUB_WORKFLOW = 'pdf-delivery.yml';
const DEDUP_TTL = 300; // 5 分钟（秒）
const DEDUP_PREFIX = 'dedup:';
const SCHEDULE_KEY = 'cfg:schedule';
const DEFAULT_SCHEDULE = ['18:30', '20:30']; // 北京时间，KV 无配置时的兜底

// 对任务内容做归一化指纹：相同任务（忽略顺序/空白）算同一个 key
function fingerprint(tasks) {
  const t = (tasks || '').trim();
  if (!t) return '__ALL__';
  const parts = t.split(';').map((s) => s.trim()).filter(Boolean).sort();
  return parts.join(';') || '__ALL__';
}

// Worker 内 Date 为 UTC，换算成北京时间 HH:MM
function beijingNowHHMM() {
  const bj = new Date(Date.now() + 8 * 3600 * 1000);
  return (
    String(bj.getUTCHours()).padStart(2, '0') +
    ':' +
    String(bj.getUTCMinutes()).padStart(2, '0')
  );
}

async function getSchedule(env) {
  try {
    const raw = await env.DEDUP.get(SCHEDULE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) return parsed.filter((t) => typeof t === 'string');
    }
  } catch (e) {
    console.error('[schedule] read error:', e && e.message);
  }
  return DEFAULT_SCHEDULE;
}

async function triggerGithub(tasks, label, githubToken) {
  const res = await fetch(
    `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/${GITHUB_WORKFLOW}/dispatches`,
    {
      method: 'POST',
      headers: {
        Accept: 'application/vnd.github+json',
        Authorization: 'Bearer ' + githubToken,
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': 'application/json',
        'User-Agent': 'pdf-scheduler-worker',
      },
      body: JSON.stringify({ ref: 'main', inputs: { tasks: tasks || '', label: label || '' } }),
    }
  );
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`GitHub dispatch HTTP ${res.status}: ${text.slice(0, 300)}`);
  }
}

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'POST, PUT, GET, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, x-trigger-key',
};

export default {
  // 每分钟 tick：命中配置的北京时间点则触发全部任务
  async scheduled(event, env, ctx) {
    const schedule = await getSchedule(env);
    const now = beijingNowHHMM();
    if (!schedule.includes(now)) return; // 非定时时刻，静默跳过
    const fp = fingerprint('');
    const key = DEDUP_PREFIX + fp;
    try {
      const existing = await env.DEDUP.get(key);
      if (existing) {
        console.log(`[cron] skip (dedup) @ ${now}`);
        return;
      }
      await triggerGithub('', '', env.GITHUB_TOKEN);
      await env.DEDUP.put(key, Date.now().toString(), { expirationTtl: DEDUP_TTL });
      console.log(`[cron] dispatched __ALL__ @ ${now}`);
    } catch (e) {
      console.error('[cron] error:', e && e.message);
      throw e;
    }
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS });
    }

    // 健康检查
    if ((url.pathname === '/' || url.pathname === '/health') && request.method === 'GET') {
      return new Response(JSON.stringify({ ok: true, service: 'pdf-scheduler' }), {
        status: 200,
        headers: { ...CORS, 'Content-Type': 'application/json' },
      });
    }

    // 鉴权：/trigger 与 /admin/schedule 都需 x-trigger-key
    const authed = !env.TRIGGER_SECRET || request.headers.get('x-trigger-key') === env.TRIGGER_SECRET;

    // 读取/修改定时时间（前端「定时设置」用）
    if (url.pathname === '/admin/schedule') {
      if (!authed) {
        return new Response(JSON.stringify({ ok: false, error: 'unauthorized' }), {
          status: 401, headers: { ...CORS, 'Content-Type': 'application/json' },
        });
      }
      if (request.method === 'GET') {
        const schedule = await getSchedule(env);
        return new Response(JSON.stringify({ ok: true, schedule }), {
          status: 200, headers: { ...CORS, 'Content-Type': 'application/json' },
        });
      }
      if (request.method === 'PUT') {
        let body = {};
        try { body = await request.json(); } catch { body = {}; }
        const list = Array.isArray(body.schedule)
          ? body.schedule.map((t) => String(t).trim()).filter((t) => /^\d{2}:\d{2}$/.test(t))
          : [];
        await env.DEDUP.put(SCHEDULE_KEY, JSON.stringify(list));
        return new Response(JSON.stringify({ ok: true, schedule: list }), {
          status: 200, headers: { ...CORS, 'Content-Type': 'application/json' },
        });
      }
    }

    // 手动下发端点
    if (url.pathname === '/trigger' && request.method === 'POST') {
      if (!authed) {
        return new Response(JSON.stringify({ ok: false, error: 'unauthorized' }), {
          status: 401, headers: { ...CORS, 'Content-Type': 'application/json' },
        });
      }
      let body = {};
      try { body = await request.json(); } catch { body = {}; }
      const tasks = body.tasks || '';
      const label = body.label || '';
      const fp = fingerprint(tasks);
      const key = DEDUP_PREFIX + fp;
      try {
        const existing = await env.DEDUP.get(key);
        if (existing) {
          return new Response(
            JSON.stringify({ ok: false, error: 'duplicate', message: '5 分钟内已下发过相同任务' }),
            { status: 429, headers: { ...CORS, 'Content-Type': 'application/json' } }
          );
        }
        await triggerGithub(tasks, label, env.GITHUB_TOKEN);
        await env.DEDUP.put(key, Date.now().toString(), { expirationTtl: DEDUP_TTL });
        return new Response(JSON.stringify({ ok: true }), {
          status: 200, headers: { ...CORS, 'Content-Type': 'application/json' },
        });
      } catch (e) {
        return new Response(
          JSON.stringify({ ok: false, error: 'dispatch_failed', message: (e && e.message) || String(e) }),
          { status: 502, headers: { ...CORS, 'Content-Type': 'application/json' } }
        );
      }
    }

    return new Response('Not found', { status: 404, headers: CORS });
  },
};
