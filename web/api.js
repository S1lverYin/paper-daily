/* GitHub Contents API wrapper for paper likes */

const LIKES_PATH = 'web/data/likes.json';

async function ghApi(method, path, body) {
  const token = typeof GH_TOKEN !== 'undefined' ? GH_TOKEN : '';
  const repo = typeof GH_REPO !== 'undefined' ? GH_REPO : 'S1lverYin/paper-daily';
  const url = `https://api.github.com/repos/${repo}/contents/${path}`;
  const opts = {
    method,
    headers: { Accept: 'application/vnd.github.v3+json' },
  };
  if (token) opts.headers.Authorization = `Bearer ${token}`;
  if (body) opts.headers['Content-Type'] = 'application/json', opts.body = JSON.stringify(body);
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error(`GitHub API ${res.status}: ${res.statusText}`);
  return res.json();
}

async function getLikesFromRepo() {
  try {
    const data = await ghApi('GET', LIKES_PATH);
    const raw = atob(data.content.replace(/\n/g, ''));
    return { likes: JSON.parse(raw).likes || {}, sha: data.sha };
  } catch {
    return { likes: {}, sha: null };
  }
}

async function putLikesToRepo(likes, sha) {
  const content = btoa(unescape(encodeURIComponent(JSON.stringify({ likes }, null, 2))));
  const body = { message: 'data: update likes', content, sha };
  try {
    const data = await ghApi('PUT', LIKES_PATH, body);
    return data.content.sha;
  } catch {
    throw new Error('Failed to sync likes to GitHub');
  }
}
