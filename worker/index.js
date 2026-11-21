// pdf-scheduler — 外部定时器（Cloudflare Worker）
// 职责：
//   1. scheduled(cron) 准点触发 GitHub workflow_dispatch（北京 17/19/21 点）
//   2. 提供 POST /trigger 供前端手动下发
//   3. 用 KV 做「5 分钟内相同任务跨终端去重」，避免重复触发
// 环境变量（secret）：
//   GITHUB_TOKEN  — 调用 workflow_dispatch 的 GitHub PAT（repo + workflow）
//   TRIGGER_SECRET — 前端调用 /trigger 时需带的 x-trigger-key（公开 Pages 上会暴露，仅挡随意调用；真正限流靠去重）
// KV binding：DEDUP（namespace: pdf-scheduler-dedup）

const GITHUB_OWNER = 'Kei-cobe';
const GITHUB_REPO = 'GetHtml2Pdf';
const GITHUB_WORKFLOW = 'pdf-delivery.yml';
const DEDUP_TTL = 300; // 5 分钟（秒）
const DEDUP_PREFIX = 'dedup:';

// 对任务内容做归一化指纹：相同任务（忽略顺序/空白）算同一个 key
function fingerprint(tasks) {
  const t = (tasks || '').trim();
  if (!t) return '__ALL__';
  const parts = t.split(';').map((s) => s.trim()).filter(Boolean).sort();
  return parts.join(';') || '__ALL__';
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
  'Access-Control-Allow-Methods': 'POST, OPTIONS, GET',
  'Access-Control-Allow-Headers': 'Content-Type, x-trigger-key',
};

export default {
  // cron 准点触发：执行全部任务（tasks 为空）
  async scheduled(event, env, ctx) {
    const fp = fingerprint('');
    const key = DEDUP_PREFIX + fp;
    try {
      const existing = await env.DEDUP.get(key);
      if (existing) {
        console.log(`[cron] skip (dedup): ${fp}`);
        return;
      }
      await triggerGithub('', '', env.GITHUB_TOKEN);
      await env.DEDUP.put(key, Date.now().toString(), { expirationTtl: DEDUP_TTL });
      console.log(`[cron] dispatched __ALL__`);
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

    // 手动下发端点
    if (url.pathname === '/trigger' && request.method === 'POST') {
      if (env.TRIGGER_SECRET) {
        const provided = request.headers.get('x-trigger-key') || '';
        if (provided !== env.TRIGGER_SECRET) {
          return new Response(JSON.stringify({ ok: false, error: 'unauthorized' }), {
            status: 401,
            headers: { ...CORS, 'Content-Type': 'application/json' },
          });
        }
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
          status: 200,
          headers: { ...CORS, 'Content-Type': 'application/json' },
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
